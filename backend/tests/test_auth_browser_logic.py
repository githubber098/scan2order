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


# ── Zepto: storeId content check + localStorage fallback ─────────────────────
#
# Ground truth from DevTools:
#   Before address: serviceability = {"timeSaved":1779990039045}   (27 chars, no storeId)
#   After  address: serviceability = {"primaryStore":{"serviceable":true,"storeId":"..."},...}
#
# The cookie is URL-encoded in the browser; Playwright returns it encoded.
# Zepto also writes user-position to localStorage:
#   Before: {"state":{"userPosition":null,...},"version":0}
#   After:  {"state":{"userPosition":{"latitude":12.99,"longitude":77.72,...},...},...}

class TestZeptoLocationReady:
    ZEPTO_AUTH = {"accessToken": "ztok"}

    @pytest.mark.asyncio
    async def test_no_auth_returns_none(self):
        s = _session("zepto", {})
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_absent_serviceability_returns_none(self):
        s = _session("zepto", self.ZEPTO_AUTH)
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_time_saved_only_cookie_returns_none(self):
        """The early placeholder {"timeSaved":...} has no storeId → still waiting."""
        cookies = {**self.ZEPTO_AUTH,
                   "serviceability": '{"timeSaved":1779990039045}'}
        s = _session("zepto", cookies)
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_url_encoded_time_saved_returns_none(self):
        """URL-encoded form of the early placeholder — as Playwright actually returns it."""
        cookies = {**self.ZEPTO_AUTH,
                   "serviceability": "%7B%22timeSaved%22%3A1779990039045%7D"}
        s = _session("zepto", cookies)
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_real_serviceability_decoded_completes_phase2(self):
        """Full serviceability JSON with storeId (decoded form) → done."""
        real_val = ('{"primaryStore":{"serviceable":true,'
                    '"storeId":"03b26203-507f-4489-b0ff-b34eb42a2215",'
                    '"etaInMinutes":10},'
                    '"storeDetailedInfo":{"city":"Bengaluru"}}')
        cookies = {**self.ZEPTO_AUTH, "serviceability": real_val}
        s = _session("zepto", cookies)
        result = await s.get_auth_cookies()
        assert result is not None
        assert result["accessToken"] == "ztok"

    @pytest.mark.asyncio
    async def test_real_serviceability_url_encoded_completes_phase2(self):
        """Full serviceability JSON URL-encoded (exactly as Playwright returns it) → done."""
        from urllib.parse import quote
        real_val = ('{"primaryStore":{"serviceable":true,'
                    '"storeId":"03b26203-507f-4489-b0ff-b34eb42a2215"}}')
        cookies = {**self.ZEPTO_AUTH, "serviceability": quote(real_val)}
        s = _session("zepto", cookies)
        assert await s.get_auth_cookies() is not None

    # ── localStorage fallback ─────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_ls_user_position_null_returns_none(self):
        """user-position LS key present but userPosition is null → still waiting."""
        ls_val = '{"state":{"userPosition":null,"userGpsCoords":null,"_hasHydrated":true},"version":0}'
        s = _session("zepto", self.ZEPTO_AUTH,
                     local_storage={"user-position": ls_val})
        assert await s.get_auth_cookies() is None

    @pytest.mark.asyncio
    async def test_ls_user_position_with_coords_completes_phase2(self):
        """user-position LS key with real latitude/longitude → done."""
        ls_val = ('{"state":{"userPosition":{"latitude":12.9922995,'
                  '"longitude":77.7299169,"placeId":"jRTwmZ9rmw","name":"Hoodi"},'
                  '"_hasHydrated":true},"version":0}')
        s = _session("zepto", self.ZEPTO_AUTH,
                     local_storage={"user-position": ls_val})
        assert await s.get_auth_cookies() is not None

    @pytest.mark.asyncio
    async def test_ls_takes_precedence_when_cookie_absent(self):
        """Cookie check fails, but valid LS → still completes phase 2."""
        ls_val = ('{"state":{"userPosition":{"latitude":12.99,"longitude":77.72},'
                  '"_hasHydrated":true},"version":0}')
        # No serviceability cookie at all
        s = _session("zepto", self.ZEPTO_AUTH,
                     local_storage={"user-position": ls_val})
        assert await s.get_auth_cookies() is not None


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
        # Must use "storeId" (camelCase) — the actual Zepto key from DevTools.
        real_val = ('{"primaryStore":{"serviceable":true,'
                    '"storeId":"03b26203-507f-4489-b0ff-b34eb42a2215"},'
                    '"storeDetailedInfo":{"city":"Bengaluru"}}')
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
