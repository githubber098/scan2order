"""
test_ui_ux.py — Tests for web UI and infrastructure endpoints.

Covers:
  - GET /         → HTML response with expected content
  - GET /health   → status:ok + version field
  - GET /api/version
  - GET /api/apps → store registry is present
  - GET /api/auth/status/{user_id} → correct shape for new / connected users
"""

import auth
import pytest


class TestWebUI:
    def test_root_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_root_contains_scan2order_branding(self, client):
        r = client.get("/")
        body = r.text.lower()
        assert "scan2order" in body

    def test_root_contains_store_connect_ui(self, client):
        """Web UI must have connection elements for all three stores."""
        r = client.get("/")
        body = r.text.lower()
        assert "bigbasket" in body
        assert "blinkit" in body
        assert "zepto" in body

    def test_root_renders_swap_popup(self, client):
        """The swap popup element must actually render. It once sat outside any
        Jinja block in index.html, so inheritance silently dropped it and the
        Swap button became a no-op (openSwapPopup bailed on the null element)."""
        r = client.get("/")
        assert 'id="swap-popup"' in r.text
        assert 'id="swap-popup-body"' in r.text


class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"

    def test_health_has_version(self, client):
        r = client.get("/health")
        assert "version" in r.json()

    def test_api_version_endpoint(self, client):
        r = client.get("/api/version")
        assert r.status_code == 200
        assert "version" in r.json()

    def test_health_and_api_version_agree(self, client):
        health_ver = client.get("/health").json()["version"]
        api_ver = client.get("/api/version").json()["version"]
        assert health_ver == api_ver


class TestApiApps:
    def test_returns_all_three_stores(self, client):
        r = client.get("/api/apps")
        assert r.status_code == 200
        data = r.json()
        assert "bigbasket" in data
        assert "blinkit" in data
        assert "zepto" in data

    def test_each_store_has_display_name_and_base_url(self, client):
        data = client.get("/api/apps").json()
        for store_name, info in data.items():
            assert "display_name" in info, f"{store_name} missing display_name"
            assert "base_url" in info, f"{store_name} missing base_url"
            assert info["base_url"].startswith("https://"), \
                f"{store_name} base_url should be https"

    def test_display_names_are_human_readable(self, client):
        data = client.get("/api/apps").json()
        assert data["bigbasket"]["display_name"] == "BigBasket"
        assert data["blinkit"]["display_name"] == "Blinkit"
        assert data["zepto"]["display_name"] == "Zepto"


class TestAuthStatus:
    def test_new_user_has_no_connected_stores(self, client):
        r = client.get("/api/auth/status/brand-new-user-xyz")
        assert r.status_code == 200
        data = r.json()
        assert data["connected_stores"] == {}

    def test_connected_user_shows_store(self, client, connected_user_bb, user_id):
        r = client.get(f"/api/auth/status/{user_id}")
        data = r.json()
        assert "bigbasket" in data["connected_stores"]
        assert data["connected_stores"]["bigbasket"]["connected"] is True

    def test_connected_user_doesnt_show_other_stores(self, client, connected_user_bb, user_id):
        data = client.get(f"/api/auth/status/{user_id}").json()
        assert "blinkit" not in data["connected_stores"]
        assert "zepto" not in data["connected_stores"]

    def test_all_stores_connected(self, client, connected_user_all, user_id):
        data = client.get(f"/api/auth/status/{user_id}").json()
        cs = data["connected_stores"]
        assert "bigbasket" in cs
        assert "blinkit" in cs
        assert "zepto" in cs

    def test_compare_header_counts_fm_connected_even_when_unhealthy(
            self, client, clean_db, user_id, bl_cookies, zepto_cookies):
        from storage import user_store

        user_store.connect_store(user_id, "blinkit", bl_cookies)
        user_store.connect_store(user_id, "zepto", zepto_cookies)
        user_store.connect_store(user_id, "flipkart_minutes", {
            "at": "fake-flipkart-at-token-" + "x" * 30,
            "SN": "1",
        })
        client.cookies.set(auth.COOKIE_NAME, auth.create_session_token(user_id))

        status = client.get(f"/api/auth/status/{user_id}").json()["connected_stores"]
        assert status["flipkart_minutes"]["connected"] is True
        assert status["flipkart_minutes"]["healthy"] is False

        body = client.get("/").text
        assert "3 stores connected" in body

    def test_status_includes_user_id(self, client, user_id, clean_db):
        data = client.get(f"/api/auth/status/{user_id}").json()
        assert data["user_id"] == user_id

    def test_empty_user_id_gracefully_handled(self, client):
        # An empty-string path segment results in 404 (FastAPI routing).
        # We expect either a 404 or a JSON error — not a 500.
        r = client.get("/api/auth/status/   ")
        assert r.status_code in (200, 404)
