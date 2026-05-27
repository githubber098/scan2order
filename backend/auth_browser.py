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

_VIEWPORT_W = 1280
_VIEWPORT_H = 800
_SESSION_TIMEOUT = 600  # 10 minutes

_STORE_CONFIG = {
    "bigbasket": {
        "url": "https://www.bigbasket.com/accounts/login/",
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
        return await self._page.screenshot(type="jpeg", quality=65)

    async def click(self, nx: float, ny: float):
        """nx, ny are 0-1 normalised coordinates relative to the viewport."""
        await self._page.mouse.click(int(nx * _VIEWPORT_W), int(ny * _VIEWPORT_H))
        self.touch()

    async def type_text(self, text: str):
        await self._page.keyboard.type(text, delay=40)
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


async def start(user_id: str, store: str) -> str:
    """Launch a headless Chromium session and navigate to the store login page.

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
    context = await browser.new_context(
        viewport={"width": _VIEWPORT_W, "height": _VIEWPORT_H},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    await page.goto(
        _STORE_CONFIG[store]["url"],
        wait_until="domcontentloaded",
        timeout=20000,
    )

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
