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
import json
import time
from typing import Optional
from urllib.parse import unquote

_VIEWPORT_W = 430
_VIEWPORT_H = 700
_SESSION_TIMEOUT = 600  # 10 minutes

# Navigate to the homepage for every store so WAFs don't see a bot
# landing directly on a login/auth URL (Akamai blocks that pattern).
#
# BigBasket is NOT listed here — Akamai's Bot Manager hard-blocks Playwright
# regardless of stealth settings (TLS fingerprint mismatch).  BigBasket must
# be connected via the mobile app WebView (undetectable) and shared to the
# web UI via the 8-char link code.
#
# wait_for   : extra cookies that must appear before the session is closed.
#              auth_cookie signals "login complete"; wait_for signals "setup
#              complete" (delivery address saved).  Without a delivery address
#              the store BFF APIs return empty search results.
# wait_hint  : message shown in the browser relay UI while waiting.
_STORE_CONFIG = {
    "blinkit": {
        "url": "https://blinkit.com/",
        "auth_cookie": "gr_1_accessToken",
        # Ground truth from live DevTools (2026-05-29): after setting a delivery
        # address Blinkit sets gr_1_lat, gr_1_lon, gr_1_locality, gr_1_landmark.
        # The plain 'lat' cookie is NOT set; neither is merchant_id.
        # Keep 'lat' and 'merchant_id' as fallbacks in case the API changes.
        "wait_for": ["gr_1_lat", "lat", "gr_1_merchantId", "merchant_id"],
        "wait_for_ls": [
            "merchant_id", "lat", "gr_1_merchantId",
            "delivery_address", "current_location", "userAddress",
        ],
        "wait_hint": (
            "✅ Login detected!  Now tap the location pin at the top of the page "
            "and save a delivery address.  The window will close automatically."
        ),
    },
    "zepto": {
        "url": "https://www.zeptonow.com/",
        "auth_cookie": "accessToken",
        # Zepto sets serviceability immediately on page load with only a
        # timestamp: {"timeSaved":1779990039045}  (27 chars, no storeId).
        # After the user confirms an address it becomes a full object:
        # {"primaryStore":{"serviceable":true,"storeId":"..."},...}
        # Requiring the substring "storeId" in the URL-decoded value cleanly
        # distinguishes the two forms regardless of value length.
        "wait_for": ["serviceability"],
        "wait_for_cookie_contains": {"serviceability": "storeId"},
        # Fallback: Zepto writes user-position to localStorage when an address
        # is confirmed.  The value transitions from {userPosition: null} to
        # {userPosition: {latitude: ..., longitude: ...}}.  Checking for the
        # key '"latitude"' (with quotes, as it appears in the JSON string) is
        # sufficient to distinguish real coordinates from the null placeholder.
        "wait_for_ls": ["user-position"],
        "wait_for_ls_contains": {"user-position": '"latitude"'},
        "wait_hint": (
            "✅ Login detected!  Now tap the location pin at the top of the page "
            "and confirm your delivery address.  The window will close automatically."
        ),
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

    async def _location_ready(self, kv: dict) -> bool:
        """Return True when phase-2 (delivery address) requirements are met.

        Checks in order:
          1. Cookie OR logic: any wait_for cookie whose URL-decoded value
             passes the optional minimum-length (wait_for_val_len) and
             optional substring check (wait_for_cookie_contains).
          2. localStorage OR logic: any wait_for_ls key whose stored string
             passes the optional substring check (wait_for_ls_contains).
             Blinkit keeps delivery context in localStorage, not cookies.
             Zepto writes user-position with lat/lng to localStorage when an
             address is confirmed.

        Logs all present cookies (excluding auth) when still waiting so the
        server log reveals the correct key name if our candidates are wrong.
        """
        cfg = _STORE_CONFIG[self.store]
        wait_for = cfg.get("wait_for", [])
        val_len = cfg.get("wait_for_val_len", {})
        cookie_contains = cfg.get("wait_for_cookie_contains", {})
        wait_for_ls = cfg.get("wait_for_ls", [])
        ls_contains = cfg.get("wait_for_ls_contains", {})

        if not wait_for and not wait_for_ls:
            return True

        def _cookie_ok(k: str) -> bool:
            raw = kv.get(k, "")
            if not raw:
                return False
            # Playwright returns cookie values as the browser stores them —
            # typically URL-encoded for JSON-value cookies set via
            # document.cookie.  Decode before length / substring checks.
            try:
                decoded = unquote(raw)
            except Exception:
                decoded = raw
            if len(decoded) < val_len.get(k, 1):
                return False
            required = cookie_contains.get(k)
            if required and required not in decoded:
                return False
            return True

        if wait_for and any(_cookie_ok(k) for k in wait_for):
            return True

        if wait_for_ls:
            try:
                ls_raw = await self._page.evaluate(
                    "() => JSON.stringify(Object.fromEntries("
                    "  Array.from({length: localStorage.length}, (_, i) => "
                    "    [localStorage.key(i), localStorage.getItem(localStorage.key(i))]"
                    ")))"
                )
                ls: dict = json.loads(ls_raw or "{}")
                for k in wait_for_ls:
                    val = ls.get(k)
                    if not val:
                        continue
                    required_substr = ls_contains.get(k)
                    if required_substr:
                        if required_substr in val:
                            return True
                    else:
                        return True  # presence alone is sufficient
            except Exception as exc:
                print(f"[browser] {self.store}: localStorage check failed: {exc}")

        present = [k for k in kv if k != cfg["auth_cookie"]]
        print(
            f"[browser] {self.store}: phase-2 waiting. "
            f"wait_for={wait_for} cookie_contains={cookie_contains} "
            f"wait_for_ls={wait_for_ls}. "
            f"Cookies present ({len(present)}): {present[:30]}"
        )
        return False

    async def get_auth_cookies(self) -> Optional[dict]:
        """Return all cookies only when the session is fully ready, else None.

        Phase 1 — wait for auth_cookie (login complete).
        Phase 2 — wait for delivery address via _location_ready().
        Only when both phases are done do we save cookies and close the session.
        """
        cfg = _STORE_CONFIG[self.store]
        auth_key = cfg["auth_cookie"]

        cookies = await self._context.cookies()
        kv = {c["name"]: c["value"] for c in cookies}

        if not kv.get(auth_key):
            return None                     # phase 1: not logged in yet

        if not await self._location_ready(kv):
            return None                     # phase 2: no delivery address yet

        # For Blinkit: if the response interceptor captured a merchant_id that
        # Blinkit never wrote as a cookie, inject it so search_item_api can use
        # it as a header (required for /v2/search to route correctly).
        captured = getattr(self._page, "_blinkit_captured", {})
        if captured.get("merchant_id") and not kv.get("merchant_id"):
            kv["merchant_id"] = captured["merchant_id"]
            print(f"[browser] blinkit: injecting captured "
                  f"merchant_id={kv['merchant_id']!r} into saved cookies")

        return kv                           # all done — close session

    async def auth_status_message(self) -> str:
        """Return a human-readable status string for the browser relay UI.

        Called by the /check endpoint on every poll so the user knows what
        step they're on without reading the Playwright screenshot carefully.
        """
        cfg = _STORE_CONFIG[self.store]
        auth_key = cfg["auth_cookie"]
        wait_hint = cfg.get("wait_hint", "")

        cookies = await self._context.cookies()
        kv = {c["name"]: c["value"] for c in cookies}

        if not kv.get(auth_key):
            return "Waiting for login…"

        if not await self._location_ready(kv):
            return wait_hint                # phase 2: no delivery address yet

        return ""                           # done (caller checks get_auth_cookies)

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

    ctx_kwargs: dict = dict(
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
        # Only grant geolocation when we have real coordinates to back it up.
        # Granting without coordinates makes Playwright return (0,0) which
        # stores interpret as "outside service area" and show error messages.
        ctx_kwargs["geolocation"] = geolocation
        ctx_kwargs["permissions"] = ["geolocation"]

    context = await browser.new_context(**ctx_kwargs)

    page = await context.new_page()

    # For Blinkit: intercept all JSON API responses to capture merchant_id.
    # Blinkit's web app automatically calls a store-discovery endpoint after
    # seeing lat/lng cookies; the response contains the merchant_id needed for
    # search API calls.  We log every URL + merchant-related field so we can
    # see exactly which endpoint returns it.
    if store == "blinkit":
        _captured: dict = {}

        async def _capture_blinkit_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "json" not in ct:
                    return
                url = response.url
                # Skip analytics, fonts, images — only log actual API calls
                if not any(x in url for x in ("/v1/", "/v2/", "/v3/",
                                               "/api/", "/location/",
                                               "/listing/", "/search")):
                    return
                body = await response.json()
                snippet = str(body)[:400]
                print(f"[browser] blinkit API {response.status}: "
                      f"{url.split('?')[0]} → {snippet}")
                # Search for merchant_id anywhere in the response
                body_str = str(body)
                import re as _re
                m = _re.search(r'["\']merchant_id["\']\s*[:=]\s*["\']?(\w+)', body_str)
                if m:
                    mid = m.group(1)
                    print(f"[browser] blinkit: captured merchant_id={mid!r}")
                    _captured["merchant_id"] = mid
            except Exception:
                pass

        page.on("response", _capture_blinkit_response)
        # Store reference so _location_ready can access captured data
        page._blinkit_captured = _captured  # type: ignore[attr-defined]

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
