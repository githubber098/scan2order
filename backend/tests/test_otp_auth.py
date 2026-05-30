"""
test_otp_auth.py — phone/email OTP login and second-method linking.

Auth is passwordless: a 6-digit OTP is sent to a phone (SMS) or email. In
tests neither Twilio nor SMTP is configured, so sms/email_sender fall back to
logging the code — we read it straight from the otp_codes table to verify.
"""

import pytest


def _otp_for(db, target: str) -> str | None:
    row = db.execute("SELECT code FROM otp_codes WHERE target=?", (target,)).fetchone()
    return row["code"] if row else None


# ── auth.normalize_* unit tests ───────────────────────────────────────────────

class TestNormalise:
    def test_phone_variants(self):
        from auth import normalize_phone
        assert normalize_phone("9876543210") == "+919876543210"
        assert normalize_phone("09876543210") == "+919876543210"
        assert normalize_phone("+91 98765 43210") == "+919876543210"
        assert normalize_phone("919876543210") == "+919876543210"
        assert normalize_phone("12345") is None
        assert normalize_phone("5876543210") is None  # must start 6-9

    def test_email(self):
        from auth import normalize_email
        assert normalize_email("  User@Example.COM ") == "user@example.com"
        assert normalize_email("nope") is None
        assert normalize_email("a@b") is None  # needs a dotted domain


# ── Phone login ───────────────────────────────────────────────────────────────

class TestPhoneLogin:
    def test_send_otp_stores_under_normalised_number(self, client, clean_db):
        r = client.post("/api/auth/send-otp", json={"channel": "phone", "value": "9876543210"})
        assert r.json()["success"] is True
        assert _otp_for(clean_db, "+919876543210")

    def test_invalid_phone_rejected(self, client, clean_db):
        r = client.post("/api/auth/send-otp", json={"channel": "phone", "value": "123"})
        assert r.json()["success"] is False

    def test_verify_creates_user_sets_cookie(self, client, clean_db):
        client.post("/api/auth/send-otp", json={"channel": "phone", "value": "9876543210"})
        code = _otp_for(clean_db, "+919876543210")
        r = client.post("/api/auth/verify-otp",
                        json={"channel": "phone", "value": "9876543210", "code": code})
        data = r.json()
        assert data["success"] is True and data["user_id"]
        me = client.get("/api/auth/me").json()
        assert me["phone"] == "+919876543210"
        assert me["email"] is None

    def test_wrong_code_rejected(self, client, clean_db):
        client.post("/api/auth/send-otp", json={"channel": "phone", "value": "9876543210"})
        r = client.post("/api/auth/verify-otp",
                        json={"channel": "phone", "value": "9876543210", "code": "000000"})
        assert r.json()["success"] is False

    def test_otp_is_single_use(self, client, clean_db):
        client.post("/api/auth/send-otp", json={"channel": "phone", "value": "9876543210"})
        code = _otp_for(clean_db, "+919876543210")
        client.post("/api/auth/verify-otp",
                    json={"channel": "phone", "value": "9876543210", "code": code})
        r = client.post("/api/auth/verify-otp",
                        json={"channel": "phone", "value": "9876543210", "code": code})
        assert r.json()["success"] is False

    def test_same_phone_returns_same_user(self, client, clean_db, monkeypatch):
        from storage import user_store
        monkeypatch.setattr(user_store, "is_otp_rate_limited", lambda *_: False)

        client.post("/api/auth/send-otp", json={"channel": "phone", "value": "9876543210"})
        c1 = _otp_for(clean_db, "+919876543210")
        uid1 = client.post("/api/auth/verify-otp",
                           json={"channel": "phone", "value": "9876543210", "code": c1}).json()["user_id"]

        client.post("/api/auth/send-otp", json={"channel": "phone", "value": "9876543210"})
        c2 = _otp_for(clean_db, "+919876543210")
        uid2 = client.post("/api/auth/verify-otp",
                           json={"channel": "phone", "value": "9876543210", "code": c2}).json()["user_id"]
        assert uid1 == uid2

    def test_rate_limited_within_60s(self, client, clean_db):
        client.post("/api/auth/send-otp", json={"channel": "phone", "value": "9876543210"})
        r = client.post("/api/auth/send-otp", json={"channel": "phone", "value": "9876543210"})
        assert r.json()["success"] is False

    def test_send_otp_surfaces_provider_error(self, client, clean_db, monkeypatch):
        """A non-None return from sms.send_otp is passed through to the user."""
        import sms
        monkeypatch.setattr(
            sms, "send_otp",
            lambda phone, code: "This number isn't approved for SMS yet.")
        r = client.post("/api/auth/send-otp", json={"channel": "phone", "value": "9876543210"})
        data = r.json()
        assert data["success"] is False
        assert "approved for SMS" in data["error"]


class TestSmsErrorMessages:
    def test_unverified_trial_number(self):
        from sms import _friendly_sms_error

        class _E(Exception):
            code = 21608
            msg = "The number +91... is unverified. Trial accounts cannot send..."
        out = _friendly_sms_error(_E())
        assert "trial" in out.lower() and "email" in out.lower()

    def test_generic_error_is_trimmed(self):
        from sms import _friendly_sms_error
        out = _friendly_sms_error(Exception("x" * 500))
        assert out.startswith("Couldn't send the SMS code")
        assert len(out) < 220


# ── Email login ───────────────────────────────────────────────────────────────

class TestEmailLogin:
    def test_send_and_verify_email(self, client, clean_db):
        client.post("/api/auth/send-otp", json={"channel": "email", "value": "User@Example.com"})
        code = _otp_for(clean_db, "user@example.com")   # normalised to lowercase
        assert code
        r = client.post("/api/auth/verify-otp",
                        json={"channel": "email", "value": "user@example.com", "code": code})
        assert r.json()["success"] is True
        me = client.get("/api/auth/me").json()
        assert me["email"] == "user@example.com"
        assert me["phone"] is None

    def test_legacy_email_key_without_channel(self, client, clean_db):
        r = client.post("/api/auth/send-otp", json={"email": "a@b.com"})
        assert r.json()["success"] is True
        assert _otp_for(clean_db, "a@b.com")

    def test_invalid_email_rejected(self, client, clean_db):
        r = client.post("/api/auth/send-otp", json={"channel": "email", "value": "notanemail"})
        assert r.json()["success"] is False


# ── Second-method linking ──────────────────────────────────────────────────────

class TestMethodLinking:
    def _login_phone(self, client, clean_db, phone="9876543210"):
        client.post("/api/auth/send-otp", json={"channel": "phone", "value": phone})
        code = _otp_for(clean_db, "+91" + phone)
        client.post("/api/auth/verify-otp",
                    json={"channel": "phone", "value": phone, "code": code})

    def test_link_requires_auth(self, client, clean_db):
        r = client.post("/api/auth/method/send-otp",
                        json={"channel": "email", "value": "a@b.com"})
        assert r.status_code == 401

    def test_link_email_to_phone_account(self, client, clean_db):
        self._login_phone(client, clean_db)
        r = client.post("/api/auth/method/send-otp",
                        json={"channel": "email", "value": "me@x.com"})
        assert r.json()["success"] is True
        code = _otp_for(clean_db, "me@x.com")
        r2 = client.post("/api/auth/method/verify",
                         json={"channel": "email", "value": "me@x.com", "code": code})
        assert r2.json()["success"] is True
        me = client.get("/api/auth/me").json()
        assert me["phone"] == "+919876543210"
        assert me["email"] == "me@x.com"

    def test_link_merges_contact_owned_by_other(self, client, clean_db):
        """Linking a contact that belongs to another account merges them once
        the OTP (proof of control) is verified."""
        from storage import user_store
        other = user_store.get_or_create_user("email", "taken@x.com")  # separate account
        self._login_phone(client, clean_db)                            # we're the phone account

        r = client.post("/api/auth/method/send-otp",
                        json={"channel": "email", "value": "taken@x.com"})
        assert r.json()["success"] is True                             # no longer rejected
        code = _otp_for(clean_db, "taken@x.com")
        r2 = client.post("/api/auth/method/verify",
                         json={"channel": "email", "value": "taken@x.com", "code": code})
        assert r2.json()["success"] is True

        me = client.get("/api/auth/me").json()
        assert me["phone"] == "+919876543210" and me["email"] == "taken@x.com"
        assert user_store.get_user_by_id(other) is None                # absorbed + deleted

    def test_link_rejects_method_already_on_account(self, client, clean_db):
        self._login_phone(client, clean_db)
        r = client.post("/api/auth/method/send-otp",
                        json={"channel": "phone", "value": "9876543210"})
        assert r.json()["success"] is False

    def test_link_verify_requires_auth(self, client, clean_db):
        r = client.post("/api/auth/method/verify",
                        json={"channel": "email", "value": "a@b.com", "code": "123456"})
        assert r.status_code == 401


# ── Store-layer functions ──────────────────────────────────────────────────────

class TestDeleteAccount:
    def _login_email(self, client, clean_db, email="del@x.com"):
        client.post("/api/auth/send-otp", json={"channel": "email", "value": email})
        code = _otp_for(clean_db, email)
        client.post("/api/auth/verify-otp",
                    json={"channel": "email", "value": email, "code": code})

    def test_delete_requires_auth(self, client, clean_db):
        r = client.post("/api/auth/delete")
        assert r.status_code == 401

    def test_delete_removes_account_and_logs_out(self, client, clean_db):
        self._login_email(client, clean_db)
        assert client.get("/api/auth/me").status_code == 200
        r = client.post("/api/auth/delete")
        assert r.json()["success"] is True
        # cookie cleared → no longer authenticated
        assert client.get("/api/auth/me").status_code == 401

    def test_delete_user_removes_sessions(self, clean_db):
        from storage import user_store
        uid = user_store.get_or_create_user("email", "z@x.com")
        user_store.connect_store(uid, "blinkit", {"gr_1_accessToken": "t"})
        user_store.delete_user(uid)
        assert user_store.get_user_by_id(uid) is None
        assert not user_store.is_store_connected(uid, "blinkit")


class TestMigration:
    """Migrating an existing phone-only DB must make phone nullable so that
    email-only accounts can be created (regression for the NOT NULL crash)."""

    def _conn(self, ddl: str):
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(ddl)
        c.commit()
        return c

    def test_rebuild_phone_only_not_null(self):
        from storage import user_store
        c = self._conn(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, phone TEXT UNIQUE NOT NULL,"
            " created_at REAL NOT NULL, last_login REAL);"
        )
        c.execute("INSERT INTO users (user_id, phone, created_at) VALUES ('u1','+919999999999',1.0)")
        c.commit()
        user_store._migrate(c, is_mysql=False)
        # email-only insert (phone NULL) must now succeed
        c.execute("INSERT INTO users (user_id, email, created_at) VALUES ('u2','x@y.com',2.0)")
        c.commit()
        assert c.execute("SELECT phone FROM users WHERE user_id='u1'").fetchone()["phone"] == "+919999999999"
        row = c.execute("SELECT phone, email FROM users WHERE user_id='u2'").fetchone()
        assert row["phone"] is None and row["email"] == "x@y.com"

    def test_rebuild_partial_migration_state(self):
        # The exact production state: email column was added but phone stayed NOT NULL.
        from storage import user_store
        c = self._conn(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, phone TEXT UNIQUE NOT NULL,"
            " email TEXT, created_at REAL NOT NULL, last_login REAL);"
        )
        c.execute("INSERT INTO users (user_id, phone, created_at) VALUES ('u1','+918888888888',1.0)")
        c.commit()
        user_store._migrate(c, is_mysql=False)
        c.execute("INSERT INTO users (user_id, email, created_at) VALUES ('u2','a@b.com',2.0)")
        c.commit()
        assert c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 2

    def test_noop_on_current_schema(self):
        from storage import user_store
        c = self._conn(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, phone TEXT, email TEXT,"
            " created_at REAL NOT NULL, last_login REAL);"
        )
        c.execute("INSERT INTO users (user_id, phone, created_at) VALUES ('u1','+917777777777',1.0)")
        c.commit()
        user_store._migrate(c, is_mysql=False)   # must not raise or lose data
        assert c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"] == 1


class TestUserStoreContacts:
    def test_get_or_create_idempotent(self, clean_db):
        from storage import user_store
        uid1 = user_store.get_or_create_user("phone", "+919999999999")
        uid2 = user_store.get_or_create_user("phone", "+919999999999")
        assert uid1 == uid2

    def test_attach_contact_success(self, clean_db):
        from storage import user_store
        uid = user_store.get_or_create_user("phone", "+919999999999")
        ok, reason = user_store.attach_contact(uid, "email", "x@y.com")
        assert ok and reason is None
        u = user_store.get_user_by_id(uid)
        assert u["phone"] == "+919999999999" and u["email"] == "x@y.com"

    def test_attach_contact_merges_other_account(self, clean_db):
        from storage import user_store
        other = user_store.get_or_create_user("email", "x@y.com")
        uid = user_store.get_or_create_user("phone", "+919999999999")
        ok, reason = user_store.attach_contact(uid, "email", "x@y.com")
        assert ok is True and reason is None
        u = user_store.get_user_by_id(uid)
        assert u["phone"] == "+919999999999" and u["email"] == "x@y.com"
        assert user_store.get_user_by_id(other) is None  # duplicate absorbed

    def test_merge_moves_store_sessions(self, clean_db):
        from storage import user_store
        other = user_store.get_or_create_user("email", "x@y.com")
        user_store.connect_store(other, "blinkit", {"gr_1_accessToken": "tok"})
        uid = user_store.get_or_create_user("phone", "+919999999999")
        user_store.attach_contact(uid, "email", "x@y.com")
        # the other account's store login moved to the surviving account
        assert user_store.is_store_connected(uid, "blinkit")

    def test_attach_same_contact_idempotent(self, clean_db):
        from storage import user_store
        uid = user_store.get_or_create_user("email", "x@y.com")
        ok, _ = user_store.attach_contact(uid, "email", "x@y.com")
        assert ok is True

    def test_get_user_by_contact(self, clean_db):
        from storage import user_store
        uid = user_store.get_or_create_user("phone", "+918888888888")
        assert user_store.get_user_by_contact("phone", "+918888888888") == uid
        assert user_store.get_user_by_contact("email", "missing@x.com") is None
