"""
test_capacitor_seams.py — backend seams for the Capacitor (Path B) conversion.

Two additive backend changes, both pre-requisites for the bundled static
frontend + WebView checkout flow:

  1. GET /api/bootstrap — one-shot client bootstrap returning everything the
     server-rendered Jinja templates inject today (user, connected-store flags +
     health, asset version, guest flag, store display names). Lets the upcoming
     static/bundled frontend stop reading Jinja `{{ ... }}` globals.

  2. sessions.cookies_full — a new column storing the FULL cookie objects
     (name/value/domain/path/secure/httpOnly/expires) captured at login, so a
     future checkout flow can re-inject the exact session into a store-domain
     WebView. The pre-existing name->value `cookies` dict is left untouched.
     Includes the legacy-DB migration (ALTER TABLE) that the live homeserver
     sessions.db will run on next deploy.
"""

import sqlite3
import time

import auth
from storage import user_store


def _login(client, user_id):
    client.cookies.set(auth.COOKIE_NAME, auth.create_session_token(user_id))


# ── /api/bootstrap ────────────────────────────────────────────────────────────

class TestBootstrap:
    def test_guest_bootstrap(self, client):
        """No session → guest payload the static frontend can boot from."""
        r = client.get("/api/bootstrap")
        assert r.status_code == 200
        d = r.json()
        assert d["guest"] is True
        assert d["user_id"] is None
        assert d["user"] is None
        assert d["stores"] == {
            "bigbasket": False, "blinkit": False, "zepto": False,
            "instamart": False, "flipkart_minutes": False,
        }
        assert d["connected_stores"] == {}
        # Display names + version metadata are always present.
        assert d["store_display"]["flipkart_minutes"] == "Flipkart Minutes"
        assert d["version"] and isinstance(d["version"], str)
        assert d["asset_v"]

    def test_authed_bootstrap_reflects_user_and_stores(self, client, clean_db, bb_cookies):
        uid = user_store.get_or_create_user("email", "cap@test.com")
        user_store.connect_store(uid, "bigbasket", bb_cookies)
        _login(client, uid)

        d = client.get("/api/bootstrap").json()
        assert d["guest"] is False
        assert d["user_id"] == uid
        assert d["user"]["email"] == "cap@test.com"
        assert d["user"]["theme"] == "fresh"
        # is_session_valid flags
        assert d["stores"]["bigbasket"] is True
        assert d["stores"]["blinkit"] is False
        # BigBasket only needs BBAUTHTOKEN → healthy; absent stores not listed.
        assert d["connected_stores"]["bigbasket"] == {
            "connected": True, "healthy": True, "reason": "",
        }
        assert "blinkit" not in d["connected_stores"]

    def test_bootstrap_health_matches_auth_status(self, client, clean_db,
                                                  bb_cookies, bl_cookies):
        """/api/bootstrap and /api/auth/status must report identical health
        (they now share _connected_store_health)."""
        uid = user_store.get_or_create_user("email", "cap2@test.com")
        user_store.connect_store(uid, "bigbasket", bb_cookies)
        user_store.connect_store(uid, "blinkit", bl_cookies)  # no lat/lon → unhealthy
        _login(client, uid)

        b = client.get("/api/bootstrap").json()
        s = client.get(f"/api/auth/status/{uid}").json()
        assert b["connected_stores"] == s["connected_stores"]
        # Blinkit connected but unhealthy (no delivery location).
        assert b["connected_stores"]["blinkit"]["connected"] is True
        assert b["connected_stores"]["blinkit"]["healthy"] is False
        assert b["connected_stores"]["blinkit"]["reason"]


# ── sessions.cookies_full storage ─────────────────────────────────────────────

class TestCookiesFull:
    _FULL = [
        {"name": "gr_1_accessToken", "value": "abc", "domain": ".blinkit.com",
         "path": "/", "secure": True, "httpOnly": True, "expires": 1893456000.0,
         "sameSite": "Lax"},
        {"name": "gr_1_lat", "value": "12.9", "domain": ".blinkit.com", "path": "/"},
    ]

    def test_roundtrip(self, clean_db):
        user_store.connect_store(
            "u1", "blinkit", {"gr_1_accessToken": "abc"},
            {"pos": "x"}, cookies_full=self._FULL,
        )
        # Full objects come back exactly as stored…
        assert user_store.get_store_cookies_full("u1", "blinkit") == self._FULL
        # …and the name->value dict + local_storage are unaffected.
        assert user_store.get_store_cookies("u1", "blinkit") == {"gr_1_accessToken": "abc"}
        assert user_store.get_store_local_storage("u1", "blinkit") == {"pos": "x"}

    def test_empty_when_omitted(self, clean_db):
        """connect_store without cookies_full (today's mobile app / legacy path)
        reads back as [] — never None or an error."""
        user_store.connect_store("u2", "zepto", {"xsrf": "y"})
        assert user_store.get_store_cookies_full("u2", "zepto") == []

    def test_empty_for_absent_row(self, clean_db):
        assert user_store.get_store_cookies_full("nobody", "zepto") == []

    def test_cookie_only_update_preserves_full(self, clean_db):
        """update_store_cookies() refreshes only the name->value blob (e.g.
        Blinkit's api_auth_key refresh); it must NOT wipe cookies_full."""
        user_store.connect_store("u3", "bigbasket", {"a": "1"}, cookies_full=self._FULL)
        user_store.update_store_cookies("u3", "bigbasket", {"b": "2"})
        assert user_store.get_store_cookies("u3", "bigbasket") == {"a": "1", "b": "2"}
        assert user_store.get_store_cookies_full("u3", "bigbasket") == self._FULL

    def test_reconnect_replaces_full(self, clean_db):
        user_store.connect_store("u4", "zepto", {"x": "1"}, cookies_full=self._FULL)
        newer = [{"name": "fresh", "value": "2", "domain": ".zeptonow.com"}]
        user_store.connect_store("u4", "zepto", {"x": "2"}, cookies_full=newer)
        assert user_store.get_store_cookies_full("u4", "zepto") == newer


# ── POST /api/auth/connect (mobile / Capacitor capture) ───────────────────────

class TestConnectEndpointCookiesFull:
    def test_accepts_cookies_full(self, client, clean_db):
        full = [{"name": "BBAUTHTOKEN", "value": "tok",
                 "domain": ".bigbasket.com", "path": "/"}]
        r = client.post("/api/auth/connect", json={
            "user_id": "cap-user", "store": "bigbasket",
            "cookies": {"BBAUTHTOKEN": "tok"},
            "local_storage": {},
            "cookies_full": full,
        })
        assert r.status_code == 200 and r.json()["success"] is True
        assert user_store.get_store_cookies_full("cap-user", "bigbasket") == full

    def test_works_without_cookies_full(self, client, clean_db):
        """Existing mobile app (no cookies_full in body) keeps working."""
        r = client.post("/api/auth/connect", json={
            "user_id": "cap-user2", "store": "blinkit",
            "cookies": {"gr_1_accessToken": "x"},
        })
        assert r.status_code == 200 and r.json()["success"] is True
        assert user_store.get_store_cookies_full("cap-user2", "blinkit") == []

    def test_non_list_cookies_full_ignored(self, client, clean_db):
        """A malformed cookies_full (not a list) is coerced to [] rather than
        stored or raising."""
        r = client.post("/api/auth/connect", json={
            "user_id": "cap-user3", "store": "zepto",
            "cookies": {"xsrf": "z"},
            "cookies_full": "not-a-list",
        })
        assert r.status_code == 200 and r.json()["success"] is True
        assert user_store.get_store_cookies_full("cap-user3", "zepto") == []


# ── Legacy-DB migration (the live homeserver sessions.db hits this) ───────────

class TestCookiesFullMigration:
    def test_alter_adds_column_to_legacy_sessions(self):
        """A pre-existing sessions table (no cookies_full) gains the column via
        _migrate(), and existing rows backfill to the '[]' default."""
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE sessions ("
            " user_id TEXT NOT NULL, store TEXT NOT NULL,"
            " cookies TEXT NOT NULL DEFAULT '{}',"
            " local_storage TEXT NOT NULL DEFAULT '{}',"
            " updated_at REAL NOT NULL,"
            " PRIMARY KEY (user_id, store));"
        )
        conn.execute(
            "INSERT INTO sessions (user_id, store, cookies, local_storage, updated_at)"
            " VALUES ('u', 'blinkit', '{}', '{}', ?)",
            (time.time(),),
        )
        conn.commit()

        assert not user_store._col_exists(conn, "sessions", "cookies_full")
        user_store._migrate(conn, is_mysql=False)
        assert user_store._col_exists(conn, "sessions", "cookies_full")

        row = conn.execute(
            "SELECT cookies_full FROM sessions WHERE user_id='u'"
        ).fetchone()
        assert row["cookies_full"] == "[]"
        conn.close()

    def test_migration_idempotent(self):
        """Running _migrate twice must not error (column already present)."""
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE sessions ("
            " user_id TEXT NOT NULL, store TEXT NOT NULL,"
            " cookies TEXT NOT NULL DEFAULT '{}',"
            " local_storage TEXT NOT NULL DEFAULT '{}',"
            " updated_at REAL NOT NULL,"
            " PRIMARY KEY (user_id, store));"
        )
        conn.commit()
        user_store._migrate(conn, is_mysql=False)
        user_store._migrate(conn, is_mysql=False)  # must be a no-op, not raise
        assert user_store._col_exists(conn, "sessions", "cookies_full")
        conn.close()
