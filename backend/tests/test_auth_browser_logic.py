"""
test_auth_browser_logic.py — Direct unit tests for _Session phase-2 logic.

Tests _location_ready(), get_auth_cookies(), and auth_status_message() using
lightweight mock Playwright objects.  No real Chromium process; no conftest
FakeBrowserSession shortcut — these tests exercise the actual method code.

Coverage:
  Blinkit: cookie OR logic + localStorage fallback
  Zepto:   minimum value-length guard for serviceability cookie
  auth_status_message: all three state transitions
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import auth_browser
from auth_browser import _Session, _STORE_CONFIG


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_context(cookies: dict) -> MagicMock:
    """Return a mock Playwright context whose cookies() returns `cookies`."""
    ctx = MagicMock()
    ctx.cookies = AsyncMock(
        return_value=[{"name": k, "value": v} for k, v in cookies.items()]
    )
    return ctx


def _make_page(local_storage: dict | None = None, raise_exc: Exception | None = None) -> MagicMock:
    """Return a mock Playwright page whose evaluate() returns localStorage JSON."""
    page = MagicMock()
    if raise_exc is not None:
        page.evaluate = AsyncMock(side_effect=raise_exc)
    else:
        ls = local_storage or {}
        page.evaluate = AsyncMock(return_value=json.dumps(ls))
    return page


def _session(store: str, cookies: dict, local_storage: dict | None = None,
             ls_exc: Exception | None = None) -> _Session:
    ctx = _make_context(cookies)
    page = _make_page(local_storage, ls_exc)
    s = _Session.__new__(_Session)
    s.user_id = "test-user"
    s.store = store
    s._context = ctx
    s._page = page
    s._browser = MagicMock()
    return s


# ── Blinkit: _location_ready via get_auth_cookies ────────────────────────────

class TestBlinkitLocationReady:
    BLINKIT_AUTH = {"gr_1_accessToken": "tok"}

    @pytest.mark.asyncio
    async def test_no_auth_cookie_returns_none(self):
        s = _session("blinkit", {})
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_auth_only_no_location_returns_none(self):
        s = _session("blinkit", self.BLINKIT_AUTH)
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_lat_cookie_completes_phase2(self):
        cookies = {**self.BLINKIT_AUTH, "lat": "12.9716"}
        s = _session("blinkit", cookies)
        result = await s.get_auth_cookies()
        assert result is not None
        assert result["gr_1_accessToken"] == "tok"

    @pytest.mark.asyncio
    async def test_dlat_not_in_wait_for_but_ls_can_save_it(self):
        # dlat is not in wait_for cookies, but if it's in localStorage it works
        cookies = {**self.BLINKIT_AUTH}
        s = _session("blinkit", cookies, local_storage={"dlat": "12.9716"})
        # dlat is not in wait_for_ls either — should remain None
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_gr1_merchant_id_cookie_completes_phase2(self):
        cookies = {**self.BLINKIT_AUTH, "gr_1_merchantId": "m123"}
        s = _session("blinkit", cookies)
        assert await s.get_auth_cookies() is not None

    @pytest.mark.asyncio
    async def test_merchant_id_cookie_completes_phase2(self):
        cookies = {**self.BLINKIT_AUTH, "merchant_id": "m456"}
        s = _session("blinkit", cookies)
        assert await s.get_auth_cookies() is not None

    @pytest.mark.asyncio
    async def test_localStorage_merchant_id_completes_phase2(self):
        s = _session("blinkit", self.BLINKIT_AUTH,
                     local_storage={"merchant_id": "m789"})
        assert await s.get_auth_cookies() is not None

    @pytest.mark.asyncio
    async def test_localStorage_delivery_address_completes_phase2(self):
        s = _session("blinkit", self.BLINKIT_AUTH,
                     local_storage={"delivery_address": '{"lat":12.9}'})
        assert await s.get_auth_cookies() is not None

    @pytest.mark.asyncio
    async def test_localStorage_current_location_completes_phase2(self):
        s = _session("blinkit", self.BLINKIT_AUTH,
                     local_storage={"current_location": "Bangalore"})
        assert await s.get_auth_cookies() is not None

    @pytest.mark.asyncio
    async def test_localStorage_throws_falls_back_to_none(self):
        s = _session("blinkit", self.BLINKIT_AUTH,
                     ls_exc=RuntimeError("page crashed"))
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_empty_localStorage_returns_none(self):
        s = _session("blinkit", self.BLINKIT_AUTH, local_storage={})
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_unrelated_localStorage_key_returns_none(self):
        s = _session("blinkit", self.BLINKIT_AUTH,
                     local_storage={"theme": "dark", "fontSize": "14"})
        assert await s.get_auth_cookies() is None


# ── Zepto: minimum value-length guard ────────────────────────────────────────

class TestZeptoLocationReady:
    ZEPTO_AUTH = {"accessToken": "ztok"}

    @pytest.mark.asyncio
    async def test_no_auth_returns_none(self):
        s = _session("zepto", {})
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_empty_serviceability_returns_none(self):
        # Zepto sets serviceability = "{}" early — must NOT complete phase 2
        cookies = {**self.ZEPTO_AUTH, "serviceability": "{}"}
        s = _session("zepto", cookies)
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_short_serviceability_returns_none(self):
        # Any value under 20 chars is treated as placeholder
        cookies = {**self.ZEPTO_AUTH, "serviceability": '{"a":1}'}
        s = _session("zepto", cookies)
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_exactly_19_chars_returns_none(self):
        cookies = {**self.ZEPTO_AUTH, "serviceability": "x" * 19}
        s = _session("zepto", cookies)
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_exactly_20_chars_completes_phase2(self):
        cookies = {**self.ZEPTO_AUTH, "serviceability": "x" * 20}
        s = _session("zepto", cookies)
        assert await s.get_auth_cookies() is not None

    @pytest.mark.asyncio
    async def test_real_serviceability_value_completes_phase2(self):
        real_val = '{"store_id":"ZPT001","lat":12.97,"lng":77.59,"city":"Bangalore"}'
        cookies = {**self.ZEPTO_AUTH, "serviceability": real_val}
        s = _session("zepto", cookies)
        result = await s.get_auth_cookies()
        assert result is not None
        assert result["accessToken"] == "ztok"

    @pytest.mark.asyncio
    async def test_absent_serviceability_returns_none(self):
        s = _session("zepto", self.ZEPTO_AUTH)
        assert await s.get_auth_cookies() is None


# ── auth_status_message transitions ──────────────────────────────────────────

class TestAuthStatusMessage:
    @pytest.mark.asyncio
    async def test_blinkit_no_login_returns_waiting(self):
        s = _session("blinkit", {})
        assert await s.auth_status_message() == "Waiting for login…"

    @pytest.mark.asyncio
    async def test_zepto_no_login_returns_waiting(self):
        s = _session("zepto", {})
        assert await s.auth_status_message() == "Waiting for login…"

    @pytest.mark.asyncio
    async def test_blinkit_logged_in_no_location_returns_hint(self):
        s = _session("blinkit", {"gr_1_accessToken": "tok"})
        msg = await s.auth_status_message()
        assert msg == _STORE_CONFIG["blinkit"]["wait_hint"]
        assert "location pin" in msg

    @pytest.mark.asyncio
    async def test_zepto_empty_serviceability_returns_hint(self):
        cookies = {"accessToken": "ztok", "serviceability": "{}"}
        s = _session("zepto", cookies)
        msg = await s.auth_status_message()
        assert msg == _STORE_CONFIG["zepto"]["wait_hint"]
        assert "location pin" in msg

    @pytest.mark.asyncio
    async def test_blinkit_location_set_returns_empty(self):
        cookies = {"gr_1_accessToken": "tok", "lat": "12.97"}
        s = _session("blinkit", cookies)
        assert await s.auth_status_message() == ""

    @pytest.mark.asyncio
    async def test_zepto_real_serviceability_returns_empty(self):
        real_val = '{"store_id":"ZPT001","lat":12.97,"lng":77.59,"city":"Bangalore"}'
        cookies = {"accessToken": "ztok", "serviceability": real_val}
        s = _session("zepto", cookies)
        assert await s.auth_status_message() == ""

    @pytest.mark.asyncio
    async def test_blinkit_ls_location_returns_empty(self):
        cookies = {"gr_1_accessToken": "tok"}
        s = _session("blinkit", cookies, local_storage={"merchant_id": "m001"})
        assert await s.auth_status_message() == ""


# ── _location_ready: store with no wait_for always passes ────────────────────

class TestNoWaitForConfig:
    @pytest.mark.asyncio
    async def test_store_with_no_wait_for_passes_immediately(self):
        # Temporarily patch a store with no location requirements
        orig = _STORE_CONFIG.get("blinkit").copy()
        _STORE_CONFIG["blinkit_test"] = {
            "auth_cookie": "tok",
            "url": "https://example.com",
        }
        try:
            s = _session.__new__(_Session) if False else object.__new__(_Session)
            s.user_id = "u"
            s.store = "blinkit_test"
            s._context = _make_context({"tok": "v"})
            s._page = _make_page()
            s._browser = MagicMock()
            kv = {"tok": "v"}
            result = await s._location_ready(kv)
            assert result is True
        finally:
            del _STORE_CONFIG["blinkit_test"]
