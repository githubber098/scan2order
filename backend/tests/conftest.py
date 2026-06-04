"""
conftest.py — shared fixtures for the scan2order test suite.

All tests run against an isolated in-memory SQLite database so they
never touch the real sessions.db file.  Real HTTP calls to
BigBasket / Blinkit / Zepto are replaced by lightweight async mocks,
and the Playwright browser relay is mocked with a fake _Session object.
"""

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

# ── Ensure backend/ is on sys.path (import root for server.py etc.) ──────────
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ── Database schema (mirrored from storage/user_store.py) ────────────────────

_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    user_id       TEXT NOT NULL,
    store         TEXT NOT NULL,
    cookies       TEXT NOT NULL DEFAULT '{}',
    local_storage TEXT NOT NULL DEFAULT '{}',
    updated_at    REAL NOT NULL,
    PRIMARY KEY (user_id, store)
);
CREATE TABLE IF NOT EXISTS link_codes (
    code       TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    phone      TEXT,
    email      TEXT,
    name       TEXT,
    theme      TEXT NOT NULL DEFAULT 'fresh',
    created_at REAL NOT NULL,
    last_login REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE TABLE IF NOT EXISTS otp_codes (
    target     TEXT PRIMARY KEY,
    code       TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS comparisons (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    query_text  TEXT NOT NULL,
    items_json  TEXT NOT NULL,
    grand_total REAL NOT NULL DEFAULT 0,
    savings     REAL NOT NULL DEFAULT 0,
    stores_json TEXT NOT NULL DEFAULT '[]'
);
"""

TEST_USER_ID = "test-user-aaaabbbb"


def _make_in_memory_db() -> sqlite3.Connection:
    """Create a fresh in-memory SQLite connection with the correct schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DB_SCHEMA)
    conn.commit()
    return conn


# ── Core DB fixture (used by almost every test) ───────────────────────────────

@pytest.fixture()
def clean_db(monkeypatch):
    """Replace user_store._conn with an isolated in-memory SQLite.

    Each test function gets a fresh database; monkeypatch restores the
    original module-level connection after the test.
    """
    from storage import user_store
    conn = _make_in_memory_db()
    monkeypatch.setattr(user_store, "_conn", conn)
    yield conn
    conn.close()


# ── FastAPI TestClient ────────────────────────────────────────────────────────

@pytest.fixture()
def client(clean_db):
    """Return a synchronous TestClient backed by the in-memory database."""
    from server import app
    with TestClient(app) as c:
        yield c


# ── Convenience user/cookie fixtures ─────────────────────────────────────────

@pytest.fixture()
def user_id() -> str:
    return TEST_USER_ID


@pytest.fixture()
def bb_cookies() -> dict:
    return {
        "BBAUTHTOKEN": "fake-bb-auth-token-longerthan20chars",
        "csurftoken": "fake-csrf-token-123",
        "_bb_mid": "fake-mid-value",
    }


@pytest.fixture()
def bl_cookies() -> dict:
    return {
        "gr_1_accessToken": "fake-blinkit-access-token-longerthan20chars",
        "gr_1_deviceId": "fake-blinkit-device-id",
    }


@pytest.fixture()
def zepto_cookies() -> dict:
    # normkey("XSRF-TOKEN") == "xsrftoken"
    # normkey("deviceId")   == "deviceid"
    return {
        "XSRF-TOKEN": "fake-xsrf-token-abc",
        "deviceId": "fake-device-id-zepto",
        "sessionId": "fake-session-id-zepto",
        "accessToken": "fake-zepto-access-token",
    }


@pytest.fixture()
def connected_user_bb(clean_db, user_id, bb_cookies):
    """User with only BigBasket connected."""
    from storage import user_store
    user_store.connect_store(user_id, "bigbasket", bb_cookies)
    return user_id


@pytest.fixture()
def im_cookies() -> dict:
    return {
        "tid": "fake-swiggy-tid-jwt-token",
        "sid": "fake-swiggy-sid",
        "deviceId": "s%3Afake-device-uuid-1234.sig",
    }


@pytest.fixture()
def fm_cookies() -> dict:
    """Minimal valid Flipkart Minutes session (flid = user identity)."""
    return {
        "flid": "fake-flipkart-flid-longerthan20chars",
        "T":    "fake-flipkart-T-session-token",
        "SN":   "1",
    }


@pytest.fixture()
def fm_ls_with_pincode() -> dict:
    """localStorage blob that includes a 6-digit delivery pincode."""
    import json
    return {
        "fkUserSelectedAddress": json.dumps({
            "addressId": "addr-001",
            "pinCode": "560034",
            "city": "Bangalore",
        })
    }


@pytest.fixture()
def connected_user_all(clean_db, user_id, bb_cookies, bl_cookies, zepto_cookies,
                       im_cookies, fm_cookies):
    """User with all five stores connected."""
    from storage import user_store
    user_store.connect_store(user_id, "bigbasket", bb_cookies)
    user_store.connect_store(user_id, "blinkit", bl_cookies)
    user_store.connect_store(user_id, "zepto", zepto_cookies)
    user_store.connect_store(user_id, "instamart", im_cookies)
    user_store.connect_store(user_id, "flipkart_minutes", fm_cookies)
    return user_id


# ── Sample product catalogue (used by store mocks) ───────────────────────────

BB_PRODUCTS = [
    {
        "product_id": "bb-001",
        "name": "Amul Butter 100g",
        "unit": "100g",
        "sale_price": 55.0,
        "price": 60.0,
        "fc_id": 10,
    },
    {
        "product_id": "bb-002",
        "name": "Amul Butter 500g",
        "unit": "500g",
        "sale_price": 250.0,
        "price": 270.0,
        "fc_id": 10,
    },
]

BL_PRODUCTS = [
    {
        "product_id": "bl-001",
        "name": "Amul Butter 100g",
        "unit": "100g",
        "sale_price": 58.0,
        "price": 62.0,
    },
]

Z_PRODUCTS = [
    {
        "product_id": "z-001",
        "name": "Amul Butter 100g",
        "unit": "100g",
        "sale_price": 52.0,
        "price": 56.0,
    },
    {
        "product_id": "z-002",
        "name": "Amul Butter 500g",
        "unit": "500g",
        "sale_price": 245.0,
        "price": 260.0,
    },
]


# ── Store search / cart mocks ─────────────────────────────────────────────────

@pytest.fixture()
def mock_stores(monkeypatch):
    """Patch all store search and cart functions to avoid real HTTP calls.

    Returns a namespace object whose attributes can be inspected in tests
    (e.g. mock_stores.bb_search.call_count).
    """
    from stores import bigbasket, blinkit, zepto, instamart, flipkart_minutes

    class _Mocks:
        pass

    m = _Mocks()

    # --- BigBasket ---
    async def _bb_search(user_id, query):
        return [p for p in BB_PRODUCTS if "amul butter" in query.lower()]

    async def _bb_cart(user_id, product_id, count=1, fc_id=None):
        if not fc_id:
            return {"success": False, "reason": "fc_id missing from product"}
        return {"success": True, "count_added": count}

    m.bb_search = AsyncMock(side_effect=_bb_search)
    m.bb_cart = AsyncMock(side_effect=_bb_cart)
    monkeypatch.setattr(bigbasket, "search_item_api", m.bb_search)
    monkeypatch.setattr(bigbasket, "add_to_cart_api", m.bb_cart)

    # --- Blinkit ---
    async def _bl_search(user_id, query):
        return [p for p in BL_PRODUCTS if "amul butter" in query.lower()]

    async def _bl_cart(user_id, product_id, count=1):
        return {"success": True, "count_added": count}

    async def _bl_cart_all(user_id, items):
        # Blinkit now batches via /v5/carts (add_all_to_cart_api).
        return {
            "success": True,
            "items": [{"success": True,
                       "count_added": i.get("count", i.get("quantity", 1))}
                      for i in items],
        }

    m.bl_search = AsyncMock(side_effect=_bl_search)
    m.bl_cart = AsyncMock(side_effect=_bl_cart)
    m.bl_cart_all = AsyncMock(side_effect=_bl_cart_all)
    monkeypatch.setattr(blinkit, "search_item_api", m.bl_search)
    monkeypatch.setattr(blinkit, "add_to_cart_api", m.bl_cart)
    monkeypatch.setattr(blinkit, "add_all_to_cart_api", m.bl_cart_all)

    # --- Zepto ---
    async def _z_search(user_id, query):
        return [p for p in Z_PRODUCTS if "amul butter" in query.lower()]

    async def _z_cart(user_id, items):
        return {
            "success": True,
            "items": [{"success": True, "count_added": i.get("count", 1)}
                      for i in items],
        }

    m.z_search = AsyncMock(side_effect=_z_search)
    m.z_cart = AsyncMock(side_effect=_z_cart)
    monkeypatch.setattr(zepto, "search_item_api", m.z_search)
    monkeypatch.setattr(zepto, "add_all_to_cart_api", m.z_cart)

    # --- Instamart ---
    IM_PRODUCTS = [
        {
            "product_id": "im-001",
            "name": "Amul Butter 100g",
            "unit": "100g",
            "sale_price": 54.0,
            "price": 58.0,
            "app": "instamart",
            "app_name": "Instamart",
        },
    ]

    async def _im_search(user_id, query):
        return [p for p in IM_PRODUCTS if "amul butter" in query.lower()]

    async def _im_cart(user_id, items):
        return {
            "success": True,
            "items": [{"success": True, "count_added": i.get("count", 1)}
                      for i in items],
        }

    m.im_search = AsyncMock(side_effect=_im_search)
    m.im_cart = AsyncMock(side_effect=_im_cart)
    monkeypatch.setattr(instamart, "search_item_api", m.im_search)
    monkeypatch.setattr(instamart, "add_all_to_cart_api", m.im_cart)

    # --- Flipkart Minutes ---
    FM_PRODUCTS = [
        {
            "product_id": "fm-001",
            "name": "Amul Butter 100g",
            "unit": "100g",
            "sale_price": 57.0,
            "price": 60.0,
            "app": "flipkart_minutes",
            "app_name": "Flipkart Minutes",
        },
    ]

    async def _fm_search(user_id, query):
        return [p for p in FM_PRODUCTS if "amul butter" in query.lower()]

    async def _fm_cart(user_id, items):
        return {
            "success": True,
            "items": [{"success": True, "count_added": i.get("count", 1)}
                      for i in items],
        }

    m.fm_search = AsyncMock(side_effect=_fm_search)
    m.fm_cart = AsyncMock(side_effect=_fm_cart)
    monkeypatch.setattr(flipkart_minutes, "search_item_api", m.fm_search)
    monkeypatch.setattr(flipkart_minutes, "add_all_to_cart_api", m.fm_cart)

    return m


# ── Browser auth mock ─────────────────────────────────────────────────────────

class FakeBrowserSession:
    """Lightweight stand-in for auth_browser._Session.

    By default get_auth_cookies() returns None (not yet logged in).
    Set authed=True to simulate a completed login.
    """

    def __init__(self, user_id: str, store: str, authed: bool = False):
        self.user_id = user_id
        self.store = store
        self.started_at = time.time()
        self.last_active = time.time()
        self._authed = authed

    def touch(self):
        self.last_active = time.time()

    def expired(self) -> bool:
        return False

    async def screenshot_jpeg(self) -> bytes:
        return b"\xff\xd8\xff\xe0" + b"\x00" * 100  # minimal JPEG-like header

    async def start_screencast(self, on_frame, **kwargs) -> bool:
        return False   # no CDP in tests → WS endpoint uses the screenshot fallback

    async def stop_screencast(self) -> None:
        pass

    async def click(self, nx: float, ny: float) -> None:
        self.touch()

    async def type_text(self, text: str) -> None:
        self.touch()

    async def key_press(self, key: str) -> None:
        self.touch()

    async def scroll(self, delta_y: float) -> None:
        self.touch()

    async def get_auth_cookies(self) -> dict | None:
        if self._authed:
            return {"gr_1_accessToken": "fake-authed-token", "gr_1_deviceId": "fake",
                    "merchant_id": "fake-merchant", "serviceability": "{}"}
        return None

    async def auth_status_message(self) -> str:
        """Return the current phase message (matches the real _Session interface)."""
        if self._authed:
            return ""
        return "Waiting for login…"

    async def get_local_storage(self) -> dict:
        return {}

    async def get_current_cookies(self) -> dict:
        return {}

    async def close(self) -> None:
        pass


@pytest.fixture()
def mock_browser(monkeypatch):
    """Patch auth_browser so no real Playwright process is started.

    Returns a dict that tests can mutate to change session behaviour.
    Usage:
        mock_browser["authed"] = True  → next /check call returns done=true
        mock_browser["start_raises"] = True  → /start returns error
    """
    import auth_browser

    state = {"authed": False, "start_raises": False}
    _fake_sessions: dict = {}

    async def _start(user_id, store, geolocation=None):
        if state.get("start_raises"):
            raise ValueError("Fake start error")
        if store not in auth_browser._STORE_CONFIG:
            raise ValueError(f"Unknown store: {store}")
        session_id = f"{user_id}--{store}"
        _fake_sessions[session_id] = FakeBrowserSession(
            user_id, store, authed=state.get("authed", False)
        )
        return session_id

    async def _close(session_id):
        _fake_sessions.pop(session_id, None)

    def _get(session_id):
        s = _fake_sessions.get(session_id)
        if s and s.expired():
            return None
        return s

    monkeypatch.setattr(auth_browser, "start", _start)
    monkeypatch.setattr(auth_browser, "close", _close)
    monkeypatch.setattr(auth_browser, "get", _get)

    state["_sessions"] = _fake_sessions
    return state
