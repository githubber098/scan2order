"""Backend API tests — no real network calls, no Playwright.

All tests use the synchronous FastAPI TestClient (which runs the full
ASGI stack in-process).  External dependencies are mocked:
  - auth_browser.start/get/close  — avoid Playwright / Chromium
  - ranker.compare_one_item       — avoid httpx calls to Ollama + stores
  - stores.*.is_session_valid     — used in /api/compare pre-flight check

The in-memory SQLite DB is set up by the _patch_db fixture in conftest.py.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# Health / version
# ─────────────────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "version" in body

    def test_api_version_present(self, client):
        r = client.get("/api/version")
        assert r.status_code == 200
        assert "version" in r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Web UI
# ─────────────────────────────────────────────────────────────────────────────

class TestWebUI:
    def test_root_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "scan2order" in r.text

    def test_root_contains_connect_buttons(self, client):
        r = client.get("/")
        html = r.text
        # All three store names should appear in the page
        for store in ("BigBasket", "Blinkit", "Zepto"):
            assert store in html, f"{store} missing from homepage"


# ─────────────────────────────────────────────────────────────────────────────
# Auth: connect / status
# ─────────────────────────────────────────────────────────────────────────────

_USER_AUTH = "test-auth-user-aaa01"


class TestAuthConnect:
    def test_missing_user_id(self, client):
        r = client.post("/api/auth/connect",
                        json={"store": "blinkit", "cookies": {"k": "v"}})
        assert r.status_code == 200
        assert r.json()["success"] is False

    def test_unknown_store(self, client):
        r = client.post("/api/auth/connect",
                        json={"user_id": _USER_AUTH, "store": "notastore",
                              "cookies": {"k": "v"}})
        assert r.json()["success"] is False
        assert "unknown store" in r.json()["error"].lower()

    def test_empty_cookies(self, client):
        r = client.post("/api/auth/connect",
                        json={"user_id": _USER_AUTH, "store": "blinkit",
                              "cookies": {}})
        assert r.json()["success"] is False

    def test_connect_blinkit_success(self, client):
        r = client.post("/api/auth/connect", json={
            "user_id": _USER_AUTH,
            "store": "blinkit",
            "cookies": {"gr_1_accessToken": "fake-blinkit-tok"},
        })
        body = r.json()
        assert body["success"] is True
        assert body["store"] == "blinkit"
        assert body["user_id"] == _USER_AUTH

    def test_status_reflects_connected_store(self, client):
        # Ensure blinkit is connected (from previous test — same user)
        client.post("/api/auth/connect", json={
            "user_id": _USER_AUTH,
            "store": "blinkit",
            "cookies": {"gr_1_accessToken": "tok"},
        })
        r = client.get(f"/api/auth/status/{_USER_AUTH}")
        data = r.json()
        assert data["connected_stores"]["blinkit"]["connected"] is True
        assert "bigbasket" not in data["connected_stores"]
        assert "zepto" not in data["connected_stores"]

    def test_status_unknown_user_has_empty_stores(self, client):
        r = client.get("/api/auth/status/nobody-xyz-999")
        data = r.json()
        assert data["connected_stores"] == {}

    def test_connect_all_three_stores(self, client):
        uid = "test-auth-user-all3"
        for store, cookie_key, tok in [
            ("bigbasket", "BBAUTHTOKEN", "bb-tok"),
            ("blinkit",   "gr_1_accessToken", "bl-tok"),
            ("zepto",     "accessToken", "zt-tok"),
        ]:
            r = client.post("/api/auth/connect", json={
                "user_id": uid, "store": store,
                "cookies": {cookie_key: tok},
            })
            assert r.json()["success"], f"connect {store} failed"

        status = client.get(f"/api/auth/status/{uid}").json()
        for store in ("bigbasket", "blinkit", "zepto"):
            assert store in status["connected_stores"], f"{store} not in status"


# ─────────────────────────────────────────────────────────────────────────────
# Auth: link codes
# ─────────────────────────────────────────────────────────────────────────────

_USER_LINK = "test-link-user-bbb01"


class TestLinkCodes:
    def test_create_link_code(self, client):
        r = client.post("/api/auth/link", json={"user_id": _USER_LINK})
        body = r.json()
        assert body["success"] is True
        code = body["code"]
        assert isinstance(code, str)
        assert len(code) >= 6

    def test_link_code_round_trip(self, client):
        # Create a fresh code
        create_r = client.post("/api/auth/link", json={"user_id": _USER_LINK})
        code = create_r.json()["code"]

        # Resolve it — should succeed and return the original user_id
        resolve_r = client.get(f"/api/auth/link/{code}")
        body = resolve_r.json()
        assert body["success"] is True
        assert body["user_id"] == _USER_LINK
        assert "stores" in body

    def test_link_code_is_single_use(self, client):
        create_r = client.post("/api/auth/link", json={"user_id": _USER_LINK})
        code = create_r.json()["code"]

        first = client.get(f"/api/auth/link/{code}")
        assert first.json()["success"] is True

        # Same code a second time must fail
        second = client.get(f"/api/auth/link/{code}")
        assert second.json()["success"] is False

    def test_link_code_invalid(self, client):
        r = client.get("/api/auth/link/NOTREAL")
        assert r.json()["success"] is False

    def test_create_link_missing_user(self, client):
        r = client.post("/api/auth/link", json={})
        assert r.json()["success"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Browser auth relay (mocked Playwright)
# ─────────────────────────────────────────────────────────────────────────────

_USER_BROWSER = "test-browser-user-ccc01"
_SESSION_ID   = f"{_USER_BROWSER}--blinkit"


class TestBrowserAuth:
    # ── start ──────────────────────────────────────────────────────────────

    def test_start_unknown_store_returns_error(self, client):
        with patch("auth_browser.start", new_callable=AsyncMock) as m:
            m.side_effect = ValueError("Unknown store: notastore")
            r = client.post("/api/auth/browser/start/notastore",
                            json={"user_id": _USER_BROWSER})
            body = r.json()
            assert body["success"] is False
            assert "error" in body

    def test_start_valid_store_returns_session(self, client):
        with patch("auth_browser.start", new_callable=AsyncMock) as m:
            m.return_value = _SESSION_ID
            r = client.post("/api/auth/browser/start/blinkit",
                            json={"user_id": _USER_BROWSER})
            body = r.json()
            assert body["success"] is True
            assert body["session_id"] == _SESSION_ID
            assert "user_id" in body

    def test_start_with_geolocation(self, client):
        """Geolocation dict should be parsed and forwarded to auth_browser.start."""
        with patch("auth_browser.start", new_callable=AsyncMock) as m:
            m.return_value = _SESSION_ID
            r = client.post("/api/auth/browser/start/zepto", json={
                "user_id": _USER_BROWSER,
                "geolocation": {"latitude": 12.9716, "longitude": 77.5946},
            })
            assert r.json()["success"] is True
            # start() should have been called with geolocation dict
            _, kwargs = m.call_args
            geo = m.call_args.args[2] if len(m.call_args.args) > 2 else kwargs.get("geolocation")
            assert geo is not None
            assert abs(geo["latitude"] - 12.9716) < 0.001

    def test_start_invalid_geolocation_ignored(self, client):
        """Malformed geolocation should be silently dropped (geolocation=None)."""
        with patch("auth_browser.start", new_callable=AsyncMock) as m:
            m.return_value = _SESSION_ID
            r = client.post("/api/auth/browser/start/zepto", json={
                "user_id": _USER_BROWSER,
                "geolocation": {"latitude": "bad", "longitude": "data"},
            })
            assert r.json()["success"] is True
            # geolocation arg should be None
            geo = m.call_args.args[2] if len(m.call_args.args) > 2 else m.call_args.kwargs.get("geolocation")
            assert geo is None

    # ── screenshot ─────────────────────────────────────────────────────────

    def test_screenshot_session_not_found_returns_404(self, client):
        with patch("auth_browser.get", return_value=None):
            r = client.get("/api/auth/browser/screenshot/no-such-session")
            assert r.status_code == 404

    def test_screenshot_returns_jpeg(self, client):
        # Minimal syntactically plausible JPEG header bytes
        fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        mock_session = MagicMock()
        mock_session.screenshot_jpeg = AsyncMock(return_value=fake_jpeg)
        with patch("auth_browser.get", return_value=mock_session):
            r = client.get(f"/api/auth/browser/screenshot/{_SESSION_ID}")
            assert r.status_code == 200
            assert "image/jpeg" in r.headers["content-type"]
            assert r.content == fake_jpeg

    def test_screenshot_has_no_cache_header(self, client):
        mock_session = MagicMock()
        mock_session.screenshot_jpeg = AsyncMock(return_value=b"\xff\xd8\xff")
        with patch("auth_browser.get", return_value=mock_session):
            r = client.get(f"/api/auth/browser/screenshot/{_SESSION_ID}")
            assert "no-store" in r.headers.get("cache-control", "")

    # ── events ─────────────────────────────────────────────────────────────

    def test_event_session_not_found(self, client):
        with patch("auth_browser.get", return_value=None):
            r = client.post("/api/auth/browser/event/gone",
                            json={"type": "click", "nx": 0.5, "ny": 0.5})
            assert r.json()["success"] is False

    def test_event_click_forwarded(self, client):
        mock_s = MagicMock()
        mock_s.click = AsyncMock()
        with patch("auth_browser.get", return_value=mock_s):
            r = client.post(f"/api/auth/browser/event/{_SESSION_ID}",
                            json={"type": "click", "nx": 0.3, "ny": 0.7})
            assert r.json()["success"] is True
            mock_s.click.assert_awaited_once_with(0.3, 0.7)

    def test_event_type_forwarded(self, client):
        mock_s = MagicMock()
        mock_s.type_text = AsyncMock()
        with patch("auth_browser.get", return_value=mock_s):
            r = client.post(f"/api/auth/browser/event/{_SESSION_ID}",
                            json={"type": "type", "text": "hello world"})
            assert r.json()["success"] is True
            mock_s.type_text.assert_awaited_once_with("hello world")

    def test_event_key_forwarded(self, client):
        mock_s = MagicMock()
        mock_s.key_press = AsyncMock()
        with patch("auth_browser.get", return_value=mock_s):
            r = client.post(f"/api/auth/browser/event/{_SESSION_ID}",
                            json={"type": "key", "key": "Enter"})
            assert r.json()["success"] is True
            mock_s.key_press.assert_awaited_once_with("Enter")

    def test_event_scroll_forwarded(self, client):
        mock_s = MagicMock()
        mock_s.scroll = AsyncMock()
        with patch("auth_browser.get", return_value=mock_s):
            r = client.post(f"/api/auth/browser/event/{_SESSION_ID}",
                            json={"type": "scroll", "delta_y": 250.0})
            assert r.json()["success"] is True
            mock_s.scroll.assert_awaited_once_with(250.0)

    def test_event_unknown_type_returns_error(self, client):
        mock_s = MagicMock()
        with patch("auth_browser.get", return_value=mock_s):
            r = client.post(f"/api/auth/browser/event/{_SESSION_ID}",
                            json={"type": "teleport", "destination": "moon"})
            assert r.json()["success"] is False
            assert "unknown event" in r.json()["error"].lower()

    # ── check ──────────────────────────────────────────────────────────────

    def test_check_session_not_found(self, client):
        with patch("auth_browser.get", return_value=None):
            r = client.get("/api/auth/browser/check/gone")
            body = r.json()
            assert body["done"] is False
            assert "error" in body

    def test_check_not_done_yet_touches_session(self, client):
        mock_s = MagicMock()
        mock_s.get_auth_cookies = AsyncMock(return_value=None)
        with patch("auth_browser.get", return_value=mock_s):
            r = client.get(f"/api/auth/browser/check/{_SESSION_ID}")
            assert r.json()["done"] is False
            # touch() keeps the session alive
            mock_s.touch.assert_called_once()

    def test_check_done_saves_cookies_and_closes(self, client):
        uid = "browser-done-user-001"
        sid = f"{uid}--blinkit"
        mock_s = MagicMock()
        mock_s.user_id = uid
        mock_s.store = "blinkit"
        mock_s.get_auth_cookies = AsyncMock(
            return_value={"gr_1_accessToken": "real-token-xyz"})
        with patch("auth_browser.get", return_value=mock_s), \
             patch("auth_browser.close", new_callable=AsyncMock) as close_m:
            r = client.get(f"/api/auth/browser/check/{sid}")
            body = r.json()
            assert body["done"] is True
            assert body["user_id"] == uid
            assert body["store"] == "blinkit"
            close_m.assert_awaited_once_with(sid)

        # Verify cookies were actually saved to the in-memory DB
        from storage import user_store
        cookies = user_store.get_store_cookies(uid, "blinkit")
        assert cookies.get("gr_1_accessToken") == "real-token-xyz"

    # ── close ──────────────────────────────────────────────────────────────

    def test_delete_session_calls_close(self, client):
        with patch("auth_browser.close", new_callable=AsyncMock) as m:
            r = client.delete(f"/api/auth/browser/session/{_SESSION_ID}")
            assert r.json()["success"] is True
            m.assert_awaited_once_with(_SESSION_ID)


# ─────────────────────────────────────────────────────────────────────────────
# Compare progress
# ─────────────────────────────────────────────────────────────────────────────

class TestCompareProgress:
    def test_progress_default_when_no_job_running(self, client):
        r = client.get(f"/api/compare/progress?user_id={_USER_AUTH}")
        body = r.json()
        assert body["running"] is False
        assert body["total"] == 0
        assert body["done"] == 0
        assert body["current"] == ""

    def test_progress_missing_user_returns_defaults(self, client):
        r = client.get("/api/compare/progress?user_id=never-ran-anything")
        body = r.json()
        assert body["total"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Compare: endpoint validation
# ─────────────────────────────────────────────────────────────────────────────

_USER_CMP = "test-compare-user-ddd01"


class TestCompare:
    def test_compare_missing_user_id(self, client):
        r = client.post("/api/compare",
                        json={"items": [{"name": "milk"}]})
        assert "error" in r.json()

    def test_compare_empty_items(self, client):
        r = client.post("/api/compare",
                        json={"user_id": _USER_CMP, "items": []})
        assert "error" in r.json()

    def test_compare_no_stores_connected(self, client):
        """User with no connected stores should get a 'no stores' error."""
        uid = "unconnected-user-xyz"
        r = client.post("/api/compare",
                        json={"user_id": uid, "items": [{"name": "milk"}]})
        body = r.json()
        assert "error" in body
        assert "no stores" in body["error"].lower()

    def test_compare_with_connected_store(self, client):
        """Full compare path with mocked ranker — verifies wiring without HTTP."""
        uid = "compare-connected-user-001"
        # Connect a store so the pre-flight check passes
        client.post("/api/auth/connect", json={
            "user_id": uid, "store": "blinkit",
            "cookies": {"gr_1_accessToken": "tok"},
        })

        fake_entry = {
            "item": {"name": "milk"},
            "search_query": "milk",
            "prices": {
                "blinkit": [{"product_id": "p1", "name": "Amul Toned Milk",
                             "sale_price": 55.0, "unit": "500 ml"}],
            },
            "cheapest_app": "blinkit",
            "cheapest_price": 55.0,
            "selected_pid": "p1",
        }

        with patch("ranker.compare_one_item", new_callable=AsyncMock) as m, \
             patch("stores.blinkit.is_session_valid", return_value=True):
            m.return_value = fake_entry
            r = client.post("/api/compare", json={
                "user_id": uid,
                "items": [{"name": "milk"}],
            })

        body = r.json()
        assert "error" not in body
        assert "comparison" in body
        assert len(body["comparison"]) == 1
        assert body["comparison"][0]["cheapest_app"] == "blinkit"


# ─────────────────────────────────────────────────────────────────────────────
# Mobile compatibility endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestMobileCompat:
    def test_account_status_unknown_user(self, client):
        r = client.get("/account-status/nobody-ever-abc999")
        body = r.json()
        assert body["bigbasket_connected"] is False
        assert body["blinkit_connected"] is False
        assert body["zepto_connected"] is False

    def test_connect_account_mobile(self, client):
        r = client.post("/connect-account", json={
            "user_id": "mobile-user-001",
            "store": "zepto",
            "cookies": {"accessToken": "fake-zepto-tok"},
        })
        assert r.json()["success"] is True

    def test_account_status_after_mobile_connect(self, client):
        uid = "mobile-status-user-002"
        client.post("/connect-account", json={
            "user_id": uid, "store": "bigbasket",
            "cookies": {"BBAUTHTOKEN": "fake-bb-tok"},
        })
        r = client.get(f"/account-status/{uid}")
        body = r.json()
        assert body["bigbasket_connected"] is True
        assert body["blinkit_connected"] is False
        assert body["zepto_connected"] is False

    def test_mobile_connect_missing_user(self, client):
        r = client.post("/connect-account",
                        json={"store": "blinkit", "cookies": {"k": "v"}})
        assert r.json()["success"] is False

    def test_mobile_connect_unknown_store(self, client):
        r = client.post("/connect-account",
                        json={"user_id": "u1", "store": "whatever",
                              "cookies": {"k": "v"}})
        assert r.json()["success"] is False

    def test_mobile_connect_missing_cookies(self, client):
        r = client.post("/connect-account",
                        json={"user_id": "u1", "store": "blinkit", "cookies": {}})
        assert r.json()["success"] is False


# ─────────────────────────────────────────────────────────────────────────────
# /api/apps
# ─────────────────────────────────────────────────────────────────────────────

class TestApiApps:
    def test_apps_lists_all_three_stores(self, client):
        r = client.get("/api/apps")
        assert r.status_code == 200
        body = r.json()
        for store in ("bigbasket", "blinkit", "zepto"):
            assert store in body
            assert "display_name" in body[store]
            assert "base_url" in body[store]
