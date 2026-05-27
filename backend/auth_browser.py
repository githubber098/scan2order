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

# Supplemental stealth patches applied before every page navigation.
# playwright-stealth handles the heavy lifting (canvas, WebGL, timing).
# This script covers the remaining mobile-specific signals.
_STEALTH_SCRIPT = """
(function() {
    try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch(e) {}
    try { if (!window.chrome) window.chrome = {}; window.chrome.runtime = window.chrome.runtime || {}; } catch(e) {}
    try {
        Object.defineProperty(navigator, 'plugins', {
            get: () => { const a = [1, 2, 3]; a.__proto__ = PluginArray.prototype; return a; }
        });
    } catch(e) {}
    try { Object.defineProperty(navigator, 'languages', { get: () => ['en-IN', 'en', 'hi'] }); } catch(e) {}
    try {
        const brands = [
            { brand: 'Google Chrome', version: '131' },
            { brand: 'Chromium',      version: '131' },
            { brand: 'Not_A Brand',   version: '24'  },
        ];
        Object.defineProperty(navigator, 'userAgentData', {
            get: () => ({
                brands,
                mobile: true,
                platform: 'Android',
                getHighEntropyValues: async () => ({
                    brands: [
                        { brand: 'Google Chrome', version: '131.0.6778.135' },
                        { brand: 'Chromium',      version: '131.0.6778.135' },
                        { brand: 'Not_A Brand',   version: '24.0.0.0' },
                    ],
                    mobile: true, model: 'Pixel 8', platform: 'Android',
                    platformVersion: '14', uaFullVersion: '131.0.6778.135',
                }),
            }),
        });
    } catch(e) {}
    try {
        const _pq = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = p =>
            p.name === 'notifications'
                ? Promise.resolve({ state: (typeof Notification !== 'undefined' ? Notification.permission : 'default'), onchange: null })
                : _pq(p);
    } catch(e) {}
})();
"""

# Analytics/tracking domains that generate constant background network
# traffic and keep Chromium CPU-bound, hurting screenshot framerate.
_BLOCKED_PATTERNS = [
    "**/google-analytics.com/**",
    "**/googletagmanager.com/**",
    "**/doubleclick.net/**",
    "**/googlesyndication.com/**",
    "**/facebook.net/**",
    "**/fbcdn.net/**",
    "**/hotjar.com/**",
    "**/segment.io/**",
    "**/segment.com/**",
    "**/mixpanel.com/**",
    "**/*.mp4",
    "**/*.webm",
]

_pw = None
_sessions: dict[str, "_Session"] = {}


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
        return await self._page.screenshot(type="jpeg", quality=50)

    async def click(self, nx: float, ny: float):
        """nx, ny are 0-1 normalised coordinates relative to the viewport."""
        await self._page.mouse.click(int(nx * _VIEWPORT_W), int(ny * _VIEWPORT_H))
        self.touch()

    async def type_text(self, text: str):
        # delay=0: JS side batches chars (30 ms debounce) so text arrives
        # as short strings; Playwright types them all at once, no per-char delay.
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
    global _pw
    if _pw is None:
        from playwright.async_api import async_playwright
        _pw = await async_playwright().start()
    return _pw


async def start(user_id: str, store: str,
                geolocation: Optional[dict] = None) -> str:
    """Launch a headless Chromium session and navigate to the store homepage.

    Args:
        user_id:     owner of this session
        store:       one of 'bigbasket', 'blinkit', 'zepto'
        geolocation: optional {"latitude": float, "longitude": float} obtained
                     from the user's real browser GPS — forwarded to Playwright
                     so store location prompts resolve correctly.
                     If None, no geolocation permission is granted; the store
                     will fall back to IP-based location detection.

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
            "--disable-blink-features=AutomationControlled",
        ],
    )

    ctx_kwargs: dict = dict(
        viewport={"width": _VIEWPORT_W, "height": _VIEWPORT_H},
        user_agent=_MOBILE_UA,
        is_mobile=True,
        has_touch=True,
        device_scale_factor=1,
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )
    if geolocation:
        # Only grant geolocation when we have real coordinates to back it up.
        # Granting without coordinates makes Playwright return (0,0) which
        # stores interpret as "outside service area" and show error messages.
        ctx_kwargs["geolocation"] = geolocation
        ctx_kwargs["permissions"] = ["geolocation"]

    context = await browser.new_context(**ctx_kwargs)

    page = await context.new_page()

    # Block analytics/tracking to reduce background CPU and improve framerate
    for pattern in _BLOCKED_PATTERNS:
        await page.route(pattern, lambda r: r.abort())

    # playwright-stealth: patches canvas, WebGL, timing, and dozens of other
    # signals that Akamai and similar WAFs use for bot detection.
    try:
        from playwright_stealth import stealth_async
        await stealth_async(page)
    except ImportError:
        pass  # falls back to manual patches below

    # Additional mobile-specific stealth patches on top of playwright-stealth
    await page.add_init_script(_STEALTH_SCRIPT)

    # BigBasket uses Akamai Bot Manager which runs a JS challenge.
    # "networkidle" waits for all network activity to stop (up to 30 s) so
    # the challenge script has time to execute and set its clearance cookie.
    wait_for = "networkidle" if store == "bigbasket" else "domcontentloaded"
    await page.goto(
        _STORE_CONFIG[store]["url"],
        wait_until=wait_for,
        timeout=30000,
    )

    # BigBasket retry: if the Akamai block page loaded, wait 2 s and retry.
    # The challenge cookie set during the first load often clears the block.
    if store == "bigbasket":
        try:
            content = await page.content()
            if "Access Denied" in content or "edgesuite.net" in content:
                print(f"[browser] {session_id}: Akamai block on first load, retrying…")
                await asyncio.sleep(2)
                await page.goto(
                    _STORE_CONFIG[store]["url"],
                    wait_until="networkidle",
                    timeout=30000,
                )
        except Exception as e:
            print(f"[browser] {session_id}: retry check failed: {e}")

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
