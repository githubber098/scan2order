"""
test_auth.py — Tests for auth connect / disconnect / link-code endpoints
and mobile compatibility shims.

Covers:
  POST /api/auth/connect      — success + error cases
  POST /api/auth/link         — code generation
  GET  /api/auth/link/{code}  — consume (one-time) + expired/invalid
  POST /connect-account       — mobile shim
  GET  /account-status/{uid}  — mobile shim
  Disconnect removes store
"""

import pytest


# ── /api/auth/connect ─────────────────────────────────────────────────────────

class TestApiAuthConnect:
    def test_connect_bigbasket_success(self, client, user_id, bb_cookies):
        r = client.post("/api/auth/connect", json={
            "user_id": user_id,
            "store": "bigbasket",
            "cookies": bb_cookies,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["store"] == "bigbasket"

    def test_connect_zepto_with_local_storage(self, client, user_id, zepto_cookies):
        r = client.post("/api/auth/connect", json={
            "user_id": user_id,
            "store": "zepto",
            "cookies": zepto_cookies,
            "local_storage": {"deviceId": "stored-ls-device"},
        })
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_connect_persists_to_db(self, client, user_id, bb_cookies, clean_db):
        client.post("/api/auth/connect", json={
            "user_id": user_id,
            "store": "bigbasket",
            "cookies": bb_cookies,
        })
        from storage import user_store
        cookies = user_store.get_store_cookies(user_id, "bigbasket")
        assert cookies.get("BBAUTHTOKEN") == bb_cookies["BBAUTHTOKEN"]

    def test_connect_missing_user_id(self, client, bb_cookies):
        r = client.post("/api/auth/connect", json={
            "store": "bigbasket",
            "cookies": bb_cookies,
        })
        data = r.json()
        assert data["success"] is False
        assert "user_id" in data["error"].lower()

    def test_connect_unknown_store(self, client, user_id, bb_cookies):
        r = client.post("/api/auth/connect", json={
            "user_id": user_id,
            "store": "amazon",
            "cookies": bb_cookies,
        })
        data = r.json()
        assert data["success"] is False
        assert "unknown store" in data["error"].lower()

    def test_connect_missing_cookies(self, client, user_id):
        r = client.post("/api/auth/connect", json={
            "user_id": user_id,
            "store": "bigbasket",
            "cookies": {},
        })
        data = r.json()
        assert data["success"] is False
        assert "cookies" in data["error"].lower()

    def test_connect_store_name_is_case_insensitive(self, client, user_id, bb_cookies):
        r = client.post("/api/auth/connect", json={
            "user_id": user_id,
            "store": "BigBasket",
            "cookies": bb_cookies,
        })
        assert r.json()["success"] is True

    def test_reconnect_overwrites_cookies(self, client, user_id, bb_cookies, clean_db):
        """A second connect for the same store replaces cookies rather than appending."""
        client.post("/api/auth/connect", json={
            "user_id": user_id, "store": "bigbasket", "cookies": bb_cookies,
        })
        new_token = "new-token-xxxxxxxxxxxxxxxxx"
        client.post("/api/auth/connect", json={
            "user_id": user_id,
            "store": "bigbasket",
            "cookies": {**bb_cookies, "BBAUTHTOKEN": new_token},
        })
        from storage import user_store
        cookies = user_store.get_store_cookies(user_id, "bigbasket")
        assert cookies["BBAUTHTOKEN"] == new_token


# ── /api/auth/link ────────────────────────────────────────────────────────────

class TestLinkCode:
    def test_generate_link_code(self, client, connected_user_bb, user_id):
        r = client.post("/api/auth/link", json={"user_id": user_id})
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        code = data["code"]
        # Codes are 8 uppercase alphanumeric chars
        assert len(code) == 8
        assert code.isupper() or code.isalnum()

    def test_generate_link_code_missing_user_id(self, client, clean_db):
        r = client.post("/api/auth/link", json={})
        data = r.json()
        assert data["success"] is False

    def test_resolve_link_code_success(self, client, connected_user_bb, user_id):
        code = client.post("/api/auth/link", json={"user_id": user_id}).json()["code"]
        r = client.get(f"/api/auth/link/{code}")
        data = r.json()
        assert data["success"] is True
        assert data["user_id"] == user_id

    def test_resolve_link_code_is_one_time(self, client, connected_user_bb, user_id):
        """A link code can only be used once; second use returns error."""
        code = client.post("/api/auth/link", json={"user_id": user_id}).json()["code"]
        client.get(f"/api/auth/link/{code}")  # first use
        r = client.get(f"/api/auth/link/{code}")  # second use
        data = r.json()
        assert data["success"] is False

    def test_resolve_invalid_code(self, client, clean_db):
        r = client.get("/api/auth/link/BADCODE1")
        data = r.json()
        assert data["success"] is False

    def test_resolve_returns_store_list(self, client, connected_user_bb, user_id):
        code = client.post("/api/auth/link", json={"user_id": user_id}).json()["code"]
        data = client.get(f"/api/auth/link/{code}").json()
        assert "stores" in data
        assert data["stores"]["bigbasket"] is True
        assert data["stores"].get("blinkit") is False or data["stores"].get("blinkit") is None

    def test_link_code_case_insensitive(self, client, connected_user_bb, user_id):
        """Codes are stored uppercase; lowercase input should still resolve."""
        code = client.post("/api/auth/link", json={"user_id": user_id}).json()["code"]
        r = client.get(f"/api/auth/link/{code.lower()}")
        assert r.json()["success"] is True


# ── Mobile compat shims ───────────────────────────────────────────────────────

class TestMobileCompat:
    def test_connect_account_success(self, client, user_id, bb_cookies, clean_db):
        r = client.post("/connect-account", json={
            "user_id": user_id,
            "store": "bigbasket",
            "cookies": bb_cookies,
        })
        assert r.status_code == 200
        assert r.json()["success"] is True

    def test_connect_account_missing_user_id(self, client, bb_cookies, clean_db):
        r = client.post("/connect-account", json={
            "store": "bigbasket",
            "cookies": bb_cookies,
        })
        assert r.json()["success"] is False

    def test_account_status_new_user(self, client, clean_db):
        r = client.get("/account-status/no-such-user")
        data = r.json()
        assert data["bigbasket_connected"] is False
        assert data["blinkit_connected"] is False
        assert data["zepto_connected"] is False

    def test_account_status_bb_connected(self, client, connected_user_bb, user_id):
        r = client.get(f"/account-status/{user_id}")
        data = r.json()
        assert data["bigbasket_connected"] is True
        assert data["blinkit_connected"] is False
        assert data["zepto_connected"] is False

    def test_account_status_all_connected(self, client, connected_user_all, user_id):
        r = client.get(f"/account-status/{user_id}")
        data = r.json()
        assert data["bigbasket_connected"] is True
        assert data["blinkit_connected"] is True
        assert data["zepto_connected"] is True


# ── Disconnect ────────────────────────────────────────────────────────────────

class TestDisconnect:
    def test_disconnect_removes_store(self, client, connected_user_bb, user_id, clean_db):
        from storage import user_store
        assert user_store.is_store_connected(user_id, "bigbasket")
        user_store.disconnect_store(user_id, "bigbasket")
        assert not user_store.is_store_connected(user_id, "bigbasket")

    def test_disconnect_doesnt_affect_other_stores(
            self, client, connected_user_all, user_id, clean_db):
        from storage import user_store
        user_store.disconnect_store(user_id, "bigbasket")
        assert not user_store.is_store_connected(user_id, "bigbasket")
        assert user_store.is_store_connected(user_id, "blinkit")
        assert user_store.is_store_connected(user_id, "zepto")

    def test_disconnect_idempotent(self, client, connected_user_bb, user_id, clean_db):
        from storage import user_store
        user_store.disconnect_store(user_id, "bigbasket")
        user_store.disconnect_store(user_id, "bigbasket")  # should not raise
        assert not user_store.is_store_connected(user_id, "bigbasket")
