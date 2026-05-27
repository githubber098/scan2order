"""
test_user_store.py — Unit tests for storage/user_store.py.

All tests use the `clean_db` fixture (in-memory SQLite) so nothing
touches the real sessions.db file on disk.

Covers:
  connect_store / get_store_cookies / get_store_session / get_user_stores
  disconnect_store / is_store_connected / update_store_cookies
  create_link_code / get_user_id_by_code / consume_link_code
  TTL / expiry behaviour
"""

import time
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


UID = "store-test-user-aabb"
BB_COOKIES = {
    "BBAUTHTOKEN": "test-bb-auth-0000000000000000",
    "csurftoken": "test-csrf",
}
BL_COOKIES = {
    "gr_1_accessToken": "test-bl-access-0000000000000000",
}
LS = {"deviceId": "local-device-123"}


# ── connect_store ─────────────────────────────────────────────────────────────

class TestConnectStore:
    def test_connect_creates_row(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        assert user_store.is_store_connected(UID, "bigbasket")

    def test_connect_stores_cookies(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        cookies = user_store.get_store_cookies(UID, "bigbasket")
        assert cookies["BBAUTHTOKEN"] == BB_COOKIES["BBAUTHTOKEN"]

    def test_connect_stores_local_storage(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "zepto", {}, local_storage=LS)
        ls = user_store.get_store_local_storage(UID, "zepto")
        assert ls["deviceId"] == "local-device-123"

    def test_connect_twice_overwrites(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        new_token = "new-token-xxxxxxxxxxxxxxxx"
        user_store.connect_store(UID, "bigbasket", {"BBAUTHTOKEN": new_token})
        cookies = user_store.get_store_cookies(UID, "bigbasket")
        assert cookies["BBAUTHTOKEN"] == new_token

    def test_connect_multiple_stores(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        user_store.connect_store(UID, "blinkit", BL_COOKIES)
        stores = user_store.get_user_stores(UID)
        assert "bigbasket" in stores
        assert "blinkit" in stores

    def test_connect_without_local_storage_defaults_to_empty(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        ls = user_store.get_store_local_storage(UID, "bigbasket")
        assert ls == {}


# ── get_store_session ─────────────────────────────────────────────────────────

class TestGetStoreSession:
    def test_returns_cookies_and_ls(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "zepto", {"accessToken": "tok"}, LS)
        sess = user_store.get_store_session(UID, "zepto")
        assert sess["cookies"]["accessToken"] == "tok"
        assert sess["local_storage"]["deviceId"] == "local-device-123"

    def test_unknown_user_returns_empty(self, clean_db):
        from storage import user_store
        sess = user_store.get_store_session("nobody", "bigbasket")
        assert sess["cookies"] == {}
        assert sess["local_storage"] == {}


# ── disconnect_store ──────────────────────────────────────────────────────────

class TestDisconnectStore:
    def test_disconnect_removes_row(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        assert user_store.is_store_connected(UID, "bigbasket")
        user_store.disconnect_store(UID, "bigbasket")
        assert not user_store.is_store_connected(UID, "bigbasket")

    def test_disconnect_only_removes_target_store(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        user_store.connect_store(UID, "blinkit", BL_COOKIES)
        user_store.disconnect_store(UID, "bigbasket")
        assert not user_store.is_store_connected(UID, "bigbasket")
        assert user_store.is_store_connected(UID, "blinkit")

    def test_disconnect_nonexistent_is_no_op(self, clean_db):
        from storage import user_store
        user_store.disconnect_store("nobody", "bigbasket")  # must not raise

    def test_cookies_unavailable_after_disconnect(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        user_store.disconnect_store(UID, "bigbasket")
        cookies = user_store.get_store_cookies(UID, "bigbasket")
        assert cookies == {}


# ── update_store_cookies ──────────────────────────────────────────────────────

class TestUpdateStoreCookies:
    def test_update_merges_new_keys(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        user_store.update_store_cookies(UID, "bigbasket", {"newKey": "newVal"})
        cookies = user_store.get_store_cookies(UID, "bigbasket")
        assert cookies["BBAUTHTOKEN"] == BB_COOKIES["BBAUTHTOKEN"]
        assert cookies["newKey"] == "newVal"

    def test_update_overwrites_existing_key(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        user_store.update_store_cookies(UID, "bigbasket", {"BBAUTHTOKEN": "updated"})
        cookies = user_store.get_store_cookies(UID, "bigbasket")
        assert cookies["BBAUTHTOKEN"] == "updated"


# ── get_user_stores ───────────────────────────────────────────────────────────

class TestGetUserStores:
    def test_empty_for_new_user(self, clean_db):
        from storage import user_store
        assert user_store.get_user_stores("new-user-xyz") == {}

    def test_returns_all_connected_stores(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        user_store.connect_store(UID, "blinkit", BL_COOKIES)
        stores = user_store.get_user_stores(UID)
        assert len(stores) == 2

    def test_each_store_has_connected_true(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        stores = user_store.get_user_stores(UID)
        assert stores["bigbasket"]["connected"] is True


# ── Link codes ────────────────────────────────────────────────────────────────

class TestLinkCodes:
    def test_create_returns_8_char_code(self, clean_db):
        from storage import user_store
        import re
        code = user_store.create_link_code(UID)
        assert len(code) == 8
        # token_urlsafe uses base64url alphabet: A-Z, 0-9, -, _
        # isalnum() would reject hyphens/underscores — use a regex instead.
        assert re.fullmatch(r"[A-Z0-9_\-]+", code), \
            f"Code '{code}' contains unexpected characters"

    def test_get_user_id_by_valid_code(self, clean_db):
        from storage import user_store
        code = user_store.create_link_code(UID)
        uid = user_store.get_user_id_by_code(code)
        assert uid == UID

    def test_get_user_id_by_invalid_code(self, clean_db):
        from storage import user_store
        assert user_store.get_user_id_by_code("NOTEXIST") is None

    def test_consume_returns_user_id(self, clean_db):
        from storage import user_store
        code = user_store.create_link_code(UID)
        uid = user_store.consume_link_code(code)
        assert uid == UID

    def test_consume_deletes_code(self, clean_db):
        from storage import user_store
        code = user_store.create_link_code(UID)
        user_store.consume_link_code(code)
        # Second consume should return None
        assert user_store.consume_link_code(code) is None

    def test_consume_invalid_code_returns_none(self, clean_db):
        from storage import user_store
        assert user_store.consume_link_code("BADCODE1") is None

    def test_code_is_case_insensitive(self, clean_db):
        """Codes are stored and matched as uppercase."""
        from storage import user_store
        code = user_store.create_link_code(UID)
        uid = user_store.get_user_id_by_code(code.lower())
        assert uid == UID

    def test_codes_are_unique_per_generation(self, clean_db):
        from storage import user_store
        codes = {user_store.create_link_code(UID) for _ in range(10)}
        # Extremely unlikely to collide; with 8 chars from token_urlsafe
        # the collision probability is negligible
        assert len(codes) >= 9  # allow at most 1 collision in 10


# ── TTL / expiry ──────────────────────────────────────────────────────────────

class TestExpiry:
    def test_expired_session_not_returned(self, clean_db):
        """A session older than 30 days must not appear in get_user_stores."""
        from storage import user_store
        thirty_one_days_ago = time.time() - (31 * 24 * 3600)
        clean_db.execute(
            """INSERT INTO sessions
                   (user_id, store, cookies, local_storage, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (UID, "bigbasket",
             '{"BBAUTHTOKEN":"old-token"}', "{}", thirty_one_days_ago),
        )
        clean_db.commit()
        stores = user_store.get_user_stores(UID)
        assert "bigbasket" not in stores

    def test_fresh_session_is_returned(self, clean_db):
        from storage import user_store
        user_store.connect_store(UID, "bigbasket", BB_COOKIES)
        stores = user_store.get_user_stores(UID)
        assert "bigbasket" in stores

    def test_expired_link_code_not_returned(self, clean_db):
        """A link code older than 24 h should not resolve."""
        from storage import user_store
        twenty_five_hours_ago = time.time() - (25 * 3600)
        clean_db.execute(
            "INSERT INTO link_codes (code, user_id, created_at) VALUES (?, ?, ?)",
            ("OLDCODE1", UID, twenty_five_hours_ago),
        )
        clean_db.commit()
        assert user_store.get_user_id_by_code("OLDCODE1") is None
