"""
test_browser_auth.py — Tests for the Playwright browser relay endpoints.

All Playwright calls are mocked via the `mock_browser` fixture in conftest.py;
no real Chromium process is started.

Covers:
  POST   /api/auth/browser/start/{store}       — launch session
  GET    /api/auth/browser/screenshot/{sid}    — JPEG stream
  POST   /api/auth/browser/event/{sid}         — mouse / keyboard / scroll
  GET    /api/auth/browser/check/{sid}         — poll for auth completion
  DELETE /api/auth/browser/session/{sid}       — cancel session
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


SESSION_STORE = "blinkit"
SESSION_USER = "browser-test-user-111"


# ── /start ────────────────────────────────────────────────────────────────────

class TestBrowserStart:
    def test_start_valid_store(self, client, mock_browser, clean_db):
        r = client.post(f"/api/auth/browser/start/{SESSION_STORE}",
                        json={"user_id": SESSION_USER})
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert "session_id" in data
        assert SESSION_USER in data["session_id"]
        assert SESSION_STORE in data["session_id"]

    def test_start_generates_user_id_when_omitted(self, client, mock_browser, clean_db):
        r = client.post(f"/api/auth/browser/start/{SESSION_STORE}", json={})
        data = r.json()
        assert data["success"] is True
        assert data["user_id"]  # some UUID was generated

    def test_start_invalid_store(self, client, mock_browser, clean_db):
        r = client.post("/api/auth/browser/start/amazon", json={"user_id": SESSION_USER})
        data = r.json()
        assert data["success"] is False
        assert "error" in data

    def test_start_accepts_geolocation(self, client, mock_browser, clean_db):
        r = client.post(f"/api/auth/browser/start/{SESSION_STORE}", json={
            "user_id": SESSION_USER,
            "geolocation": {"latitude": 12.9716, "longitude": 77.5946},
        })
        assert r.json()["success"] is True

    def test_start_ignores_invalid_geolocation(self, client, mock_browser, clean_db):
        # Bad geolocation dict → silently ignored, session still starts
        r = client.post(f"/api/auth/browser/start/{SESSION_STORE}", json={
            "user_id": SESSION_USER,
            "geolocation": {"latitude": "not-a-float", "longitude": 77.5946},
        })
        assert r.json()["success"] is True

    def test_start_supported_stores(self, client, mock_browser, clean_db):
        """Blinkit and Zepto are supported in the browser relay."""
        for store in ("blinkit", "zepto"):
            r = client.post(f"/api/auth/browser/start/{store}",
                            json={"user_id": f"user-{store}"})
            assert r.json()["success"] is True, f"Failed to start {store}"

    def test_bigbasket_is_mobile_only(self, client, mock_browser, clean_db):
        """BigBasket is excluded from the browser relay (Akamai blocks Playwright)."""
        r = client.post("/api/auth/browser/start/bigbasket",
                        json={"user_id": "user-bb"})
        assert r.json()["success"] is False
        assert "bigbasket" in r.json().get("error", "").lower() or \
               "unknown" in r.json().get("error", "").lower()


# ── /screenshot ───────────────────────────────────────────────────────────────

class TestBrowserScreenshot:
    def _start_session(self, client, mock_browser) -> str:
        return client.post(f"/api/auth/browser/start/{SESSION_STORE}",
                           json={"user_id": SESSION_USER}).json()["session_id"]

    def test_screenshot_returns_jpeg(self, client, mock_browser, clean_db):
        sid = self._start_session(client, mock_browser)
        r = client.get(f"/api/auth/browser/screenshot/{sid}")
        assert r.status_code == 200
        assert "image/jpeg" in r.headers["content-type"]

    def test_screenshot_has_no_cache_header(self, client, mock_browser, clean_db):
        sid = self._start_session(client, mock_browser)
        r = client.get(f"/api/auth/browser/screenshot/{sid}")
        assert r.headers.get("cache-control") == "no-store"

    def test_screenshot_unknown_session_returns_404(self, client, mock_browser, clean_db):
        r = client.get("/api/auth/browser/screenshot/does-not-exist--blinkit")
        assert r.status_code == 404


# ── /event ────────────────────────────────────────────────────────────────────

class TestBrowserEvent:
    def _start_session(self, client, mock_browser) -> str:
        return client.post(f"/api/auth/browser/start/{SESSION_STORE}",
                           json={"user_id": SESSION_USER}).json()["session_id"]

    def test_event_click(self, client, mock_browser, clean_db):
        sid = self._start_session(client, mock_browser)
        r = client.post(f"/api/auth/browser/event/{sid}",
                        json={"type": "click", "nx": 0.5, "ny": 0.3})
        assert r.json()["success"] is True

    def test_event_type_text(self, client, mock_browser, clean_db):
        sid = self._start_session(client, mock_browser)
        r = client.post(f"/api/auth/browser/event/{sid}",
                        json={"type": "type", "text": "hello"})
        assert r.json()["success"] is True

    def test_event_key_press(self, client, mock_browser, clean_db):
        sid = self._start_session(client, mock_browser)
        r = client.post(f"/api/auth/browser/event/{sid}",
                        json={"type": "key", "key": "Enter"})
        assert r.json()["success"] is True

    def test_event_scroll(self, client, mock_browser, clean_db):
        sid = self._start_session(client, mock_browser)
        r = client.post(f"/api/auth/browser/event/{sid}",
                        json={"type": "scroll", "delta_y": 300.0})
        assert r.json()["success"] is True

    def test_event_unknown_type(self, client, mock_browser, clean_db):
        sid = self._start_session(client, mock_browser)
        r = client.post(f"/api/auth/browser/event/{sid}",
                        json={"type": "teleport"})
        data = r.json()
        assert data["success"] is False
        assert "unknown event type" in data["error"].lower()

    def test_event_expired_session(self, client, mock_browser, clean_db):
        r = client.post("/api/auth/browser/event/nonexistent--blinkit",
                        json={"type": "click", "nx": 0.5, "ny": 0.5})
        data = r.json()
        assert data["success"] is False
        assert "session not found" in data["error"].lower()


# ── /check ────────────────────────────────────────────────────────────────────

class TestBrowserCheck:
    def test_check_not_done_while_not_logged_in(self, client, mock_browser, clean_db):
        """Polling before auth completes returns done=false."""
        sid = client.post(f"/api/auth/browser/start/{SESSION_STORE}",
                          json={"user_id": SESSION_USER}).json()["session_id"]
        r = client.get(f"/api/auth/browser/check/{sid}")
        assert r.json()["done"] is False

    def test_check_done_when_authed(self, client, mock_browser, clean_db):
        """Setting authed=True simulates a successful login; /check saves cookies."""
        mock_browser["authed"] = True
        sid = client.post(f"/api/auth/browser/start/{SESSION_STORE}",
                          json={"user_id": SESSION_USER}).json()["session_id"]
        r = client.get(f"/api/auth/browser/check/{sid}")
        data = r.json()
        assert data["done"] is True
        assert data["user_id"] == SESSION_USER
        assert data["store"] == SESSION_STORE

    def test_check_saves_cookies_on_done(self, client, mock_browser, clean_db):
        """After a successful check the auth cookies are persisted in SQLite."""
        mock_browser["authed"] = True
        sid = client.post(f"/api/auth/browser/start/{SESSION_STORE}",
                          json={"user_id": SESSION_USER}).json()["session_id"]
        client.get(f"/api/auth/browser/check/{sid}")  # triggers save

        from storage import user_store
        cookies = user_store.get_store_cookies(SESSION_USER, SESSION_STORE)
        assert cookies.get("gr_1_accessToken") == "fake-authed-token"

    def test_check_unknown_session(self, client, mock_browser, clean_db):
        r = client.get("/api/auth/browser/check/no-such-session--zepto")
        data = r.json()
        assert data["done"] is False
        assert "error" in data

    def test_check_calls_touch_while_pending(self, client, clean_db):
        """touch() must be called on each poll while auth is not yet complete.

        Uses a raw MagicMock (not the conftest fixture) to assert the exact
        call.  touch() keeps the 10-min inactivity timer from expiring while
        the user is actively looking at the screenshot.
        """
        mock_s = MagicMock()
        mock_s.get_auth_cookies = AsyncMock(return_value=None)
        mock_s.auth_status_message = AsyncMock(return_value="Waiting for login…")
        with patch("auth_browser.get", return_value=mock_s):
            r = client.get("/api/auth/browser/check/any-session--blinkit")
            assert r.json()["done"] is False
            mock_s.touch.assert_called_once()

    def test_check_done_calls_close_with_session_id(self, client, clean_db):
        """When auth completes, close() must be awaited with the exact session_id
        so the Playwright browser is actually torn down and not left hanging.
        """
        uid = "close-verify-user-aaa"
        sid = f"{uid}--blinkit"
        mock_s = MagicMock()
        mock_s.user_id = uid
        mock_s.store = "blinkit"
        mock_s.get_auth_cookies = AsyncMock(
            return_value={"gr_1_accessToken": "tok-close-test"})
        with patch("auth_browser.get", return_value=mock_s), \
             patch("auth_browser.close", new_callable=AsyncMock) as close_mock:
            r = client.get(f"/api/auth/browser/check/{sid}")
            assert r.json()["done"] is True
            close_mock.assert_awaited_once_with(sid)


# ── DELETE /session ───────────────────────────────────────────────────────────

class TestBrowserClose:
    def test_delete_session(self, client, mock_browser, clean_db):
        sid = client.post(f"/api/auth/browser/start/{SESSION_STORE}",
                          json={"user_id": SESSION_USER}).json()["session_id"]
        r = client.delete(f"/api/auth/browser/session/{sid}")
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_delete_removes_session(self, client, mock_browser, clean_db):
        """After DELETE the session should no longer be accessible."""
        sid = client.post(f"/api/auth/browser/start/{SESSION_STORE}",
                          json={"user_id": SESSION_USER}).json()["session_id"]
        client.delete(f"/api/auth/browser/session/{sid}")
        # Screenshot should return 404 now
        r = client.get(f"/api/auth/browser/screenshot/{sid}")
        assert r.status_code == 404

    def test_delete_nonexistent_session_ok(self, client, mock_browser, clean_db):
        """Deleting a session that doesn't exist is a no-op, not an error."""
        r = client.delete("/api/auth/browser/session/ghost--zepto")
        assert r.json()["success"] is True
