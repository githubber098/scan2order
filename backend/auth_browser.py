"""
auth_browser.py - Playwright Chromium sessions for store account linking.

Spins up a headless Chromium instance per Connect request. The web UI polls
for JPEG screenshots, forwards mouse/keyboard events, and calls the check
endpoint until the store's auth cookie appears. On success the cookies are
saved to SQLite via user_store.connect_store() and the browser is closed.

Session ID format: "{user_id}--{store}"  (URL-safe, no colons)
Session expires after 10 min of inactivity.
"""

import asyncio
import time
from typing import Optional

_VIEWPORT_W = 430
_VIEWPORT_H = 700
_SESSION_TIMEOUT = 600  # 10 minutes

# Navigate to the homepage for every store so WAFs don't see a bot
# landing directly on a login/auth URL (Akamai blocks that pattern).
_STORE_CONFIG = {
    "bigbasket": {
        "url": "https://www.bigbasket.com/",
        "auth_cookie": "BBAUTHTOKEN",
    },
    "blinkit": {
        "url": "https://blinkit.com/",
        "auth_cookie": "gr_1_accessToken",
    },
    "zepto": {
        "url": "https://www.zeptonow.com/",
        "auth_cookie": "accessToken",
    },
}

_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.6778.135 Mobile Safari/537.36"
)

# Sec-CH-UA Client Hints that MUST match the UA string above.
# Akamai Bot Manager cross-checks brands/version against the User-Agent header;
# a mismatch is an immediate block.  These headers are set at the browser
# context level so they appear on every outgoing request automatically.
_CLIENT_HINTS = {
    "Sec-CH-UA": (
        '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
    ),
    "Sec-CH-UA-Mobile": "?1",
    "Sec-CH-UA-Platform": '"Android"',
}

# Injected before every page navigation to suppress all Playwright/automation
# detection signals. Wrapped in try/catch blocks so a single failure never
# breaks the whole script (some properties are non-configurable on some pages).
_STEALTH_SCRIPT = """
(function() {
    // 1. navigator.webdriver — the strongest bot signal; must be undefined
    try {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    } catch(e) {}

    // 2. window.chrome — headless omits chrome.runtime
    try {
        if (!window.chrome) window.chrome = {};
        if (!window.chrome.runtime) window.chrome.runtime = {};
        if (!window.chrome.app) window.chrome.app = { isInstalled: false };
    } catch(e) {}

    // 3. navigator.plugins — headless has 0; real Chrome has several
    try {
        Object.defineProperty(navigator, 'plugins', {
            get: () => { const a = [1, 2, 3]; a.__proto__ = PluginArray.prototype; return a; }
        });
    } catch(e) {}

    // 4. navigator.languages — consistent with locale=en-IN
    try {
        Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en', 'hi'] });
    } catch(e) {}

    // 5. navigator.userAgentData — Akamai cross-checks brand list against the
    //    UA string. Our UA claims Chrome/131 so brands must say Chrome 131 too.
    //    Playwright sets this automatically from the UA, but the brand strings
    //    can differ; overriding ensures exact consistency.
    try {
        const brands = [
            { brand: 'Google Chrome', version: '131' },
            { brand: 'Chromium',      version: '131' },
            { brand: 'Not_A Brand',   version: '24'  },
        ];
        const fullBrands = [
            { brand: 'Google Chrome', version: '131.0.6778.135' },
            { brand: 'Chromium',      version: '131.0.6778.135' },
            { brand: 'Not_A Brand',   version: '24.0.0.0'      },
        ];
        Object.defineProperty(navigator, 'userAgentData', {
            get: () => ({
                brands,
                mobile: true,
                platform: 'Android',
                getHighEntropyValues: async () => ({
                    brands: fullBrands,
                    fullVersionList: fullBrands,
                    mobile: true,
                    model: 'Pixel 8',
                    platform: 'Android',
                    platformVersion: '14',
                    uaFullVersion: '131.0.6778.135',
                }),
            }),
        });
    } catch(e) {}

    // 6. navigator.permissions — some WAFs probe notification permission state
    try {
        const _pq = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = p =>
            p.name === 'notifications'
                ? Promise.resolve({ state: (typeof Notification !== 'undefined' ? Notification.permission : 'default'), onchange: null })
                : _pq(p);
    } catch(e) {}
})();
"""

_pw = None
# asyncio.Lock protecting _pw initialization.
# Without this, two concurrent browser-start requests both see _pw is None,
# both call async_playwright().start(), and two Playwright instances are
# created — the first is overwritten and leaked.
# Double-checked: fast path (no lock) when _pw is already set; slow path
# (with lock) only on first initialization.
_pw_lock: asyncio.Lock | None = None
_sessions: dict[str, "_Session"] = {}


def _get_pw_lock() -> asyncio.Lock:
    """Return the module-level asyncio.Lock, creating it on first call.

    Creating the Lock lazily avoids binding it to whatever event loop happens
    to exist at import time.  The creation itself is safe because it is a
    sync call — only one coroutine runs between await points in asyncio.
    """
    global _pw_lock
    if _pw_lock is None:
        _pw_lock = asyncio.Lock()
    return _pw_lock


class _Session:
    def __init__(self, user_id: str, store: str, browser, context, page):
        self.user_id = user_id
        self.store = store
        self._browser = browser
        self._context = context
        self._page = page
        self.started_at = time.time()
        self.last_active = time.time()

    def touch(self):
        self.last_active = time.time()

    def expired(self) -> bool:
        return time.time() - self.last_active > _SESSION_TIMEOUT

    async def screenshot_jpeg(self) -> bytes:
        return await self._page.screenshot(type="jpeg", quality=60)

    async def click(self, nx: float, ny: float):
        """nx, ny are 0-1 normalised coordinates relative to the viewport."""
        await self._page.mouse.click(int(nx * _VIEWPORT_W), int(ny * _VIEWPORT_H))
        self.touch()

    async def type_text(self, text: str):
        # delay=0: characters are typed instantaneously in Playwright.
        # The JS side already batches characters (30 ms debounce) so we
        # receive them as short strings rather than one char at a time.
        await self._page.keyboard.type(text, delay=0)
        self.touch()

    async def key_press(self, key: str):
        await self._page.keyboard.press(key)
        self.touch()

    async def scroll(self, delta_y: float):
        await self._page.mouse.wheel(0, delta_y)
        self.touch()

    async def get_auth_cookies(self) -> Optional[dict]:
        """Return all cookies if the store's auth cookie is present, else None."""
        auth_key = _STORE_CONFIG[self.store]["auth_cookie"]
        cookies = await self._context.cookies()
        kv = {c["name"]: c["value"] for c in cookies}
        return kv if kv.get(auth_key) else None

    async def close(self):
        for obj in (self._context, self._browser):
            try:
                await obj.close()
            except Exception:
                pass


async def _get_playwright():
    """Return the shared Playwright instance, initialising it at most once.

    Uses double-checked locking:
      1. Fast path: if _pw is already set, return it without acquiring the lock.
      2. Slow path: acquire the lock, check again (another coroutine may have
         initialised _pw while we were waiting), then start Playwright.
    This guarantees exactly one Playwright instance even when two browser-auth
    requests arrive simultaneously.
    """
    global _pw
    if _pw is not None:
        return _pw
    async with _get_pw_lock():
        if _pw is None:  # re-check inside the lock
            from playwright.async_api import async_playwright
            _pw = await async_playwright().start()
    return _pw


async def start(user_id: str, store: str,
                geolocation: Optional[dict] = None) -> str:
    """Launch a headless Chromium session and navigate to the store homepage.

    Args:
        user_id:     owner of this session
        store:       one of 'bigbasket', 'blinkit', 'zepto'
        geolocation: optional {"latitude": float, "longitude": float} from
                     the user's browser — passed to the Playwright context so
                     that store location prompts ("Use my location") work.

    Returns the session_id string used by all other functions.
    Closes any existing session for the same (user_id, store) pair first.
    """
    if store not in _STORE_CONFIG:
        raise ValueError(f"Unknown store: {store}")

    session_id = f"{user_id}--{store}"
    if session_id in _sessions:
        await _sessions[session_id].close()

    pw = await _get_playwright()
    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            # Suppress the strongest WebDriver detection signal.
            "--disable-blink-features=AutomationControlled",
            # Reduce differences from a real Chrome binary.
            "--disable-ipc-flooding-protection",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-client-side-phishing-detection",
            "--password-store=basic",
            "--use-mock-keychain",
        ],
    )

    ctx_kwargs = dict(
        viewport={"width": _VIEWPORT_W, "height": _VIEWPORT_H},
        user_agent=_MOBILE_UA,
        is_mobile=True,
        has_touch=True,
        device_scale_factor=2,        # real Pixel 8 has 2× density
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        # Sec-CH-UA Client Hints — Akamai validates these against the UA string.
        # Without them, or if they don't match, Akamai hard-blocks on first request.
        extra_http_headers=_CLIENT_HINTS,
    )
    if geolocation:
        ctx_kwargs["geolocation"] = geolocation
        ctx_kwargs["permissions"] = ["geolocation"]

    context = await browser.new_context(**ctx_kwargs)

    # Always grant geolocation even without coordinates so the browser
    # doesn't show a permission prompt blocking the login flow.
    if not geolocation:
        await context.grant_permissions(["geolocation"])

    page = await context.new_page()
    await page.add_init_script(_STEALTH_SCRIPT)

    # wait_until="load" (not "domcontentloaded") so Akamai's sensor SDK has
    # time to run, collect browser telemetry, and set the _abck cookie before
    # we hand the session back.  "domcontentloaded" is too early — the sensor
    # SDK fires on window.onload and later, so Akamai sees no telemetry and
    # immediately hard-blocks.  Timeout raised to 30 s for slow networks.
    await page.goto(
        _STORE_CONFIG[store]["url"],
        wait_until="load",
        timeout=30000,
    )

    # Brief idle period: Akamai's sensor continues collecting interaction data
    # for ~2 s after load.  A small random mouse drift mimics real touch input.
    try:
        await page.mouse.move(
            _VIEWPORT_W * 0.3 + 10, _VIEWPORT_H * 0.4,
        )
        await page.wait_for_timeout(500)
        await page.mouse.move(
            _VIEWPORT_W * 0.5, _VIEWPORT_H * 0.5,
        )
        await page.wait_for_timeout(1000)
    except Exception:
        pass   # non-fatal if the page closed or errored

    _sessions[session_id] = _Session(user_id, store, browser, context, page)
    print(f"[browser] started session {session_id}")
    return session_id


def get(session_id: str) -> Optional[_Session]:
    s = _sessions.get(session_id)
    if s is None:
        return None
    if s.expired():
        asyncio.create_task(close(session_id))
        return None
    return s


async def close(session_id: str):
    s = _sessions.pop(session_id, None)
    if s:
        await s.close()
        print(f"[browser] closed session {session_id}")


async def cleanup_expired():
    expired = [k for k, s in list(_sessions.items()) if s.expired()]
    for k in expired:
        await close(k)
