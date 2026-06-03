"""
test_session_health_history.py — backfill coverage for two features merged
since the test suite was last updated:

  - Per-store session health (all 4 stores: bigbasket/blinkit/zepto/instamart
    session_health + the healthy/reason fields on GET /api/auth/status/{user_id})
  - Comparison history persistence (user_store.save_comparison / get_history +
    the GET /api/history endpoint)
"""

import auth
from stores import bigbasket, blinkit, zepto, instamart


def _login(client, user_id):
    client.cookies.set(auth.COOKIE_NAME, auth.create_session_token(user_id))


# ── Session health (static probes) ───────────────────────────────────────────

class TestSessionHealth:
    # ── BigBasket ─────────────────────────────────────────────────────────────

    def test_bigbasket_healthy_when_token_present(self, clean_db, connected_user_all):
        h = bigbasket.session_health(connected_user_all)
        assert h["ok"] is True
        assert h["reason"] == ""

    def test_bigbasket_unhealthy_when_not_connected(self, clean_db, user_id):
        h = bigbasket.session_health(user_id)
        assert h["ok"] is False
        assert "reconnect" in h["reason"].lower()

    # ── Blinkit ───────────────────────────────────────────────────────────────

    def test_blinkit_unhealthy_without_location(self, clean_db, connected_user_all):
        # bl_cookies fixture has an access token but no gr_1_lat/lon → unhealthy.
        h = blinkit.session_health(connected_user_all)
        assert h["ok"] is False
        assert "location" in h["reason"].lower()

    def test_blinkit_unhealthy_when_not_connected(self, clean_db, user_id):
        h = blinkit.session_health(user_id)
        assert h["ok"] is False
        assert "reconnect" in h["reason"].lower()

    # ── Zepto ─────────────────────────────────────────────────────────────────

    def test_zepto_unhealthy_without_store_id(self, clean_db, connected_user_all):
        # zepto_cookies has tokens but no store_id → unhealthy with a clear reason.
        h = zepto.session_health(connected_user_all)
        assert h["ok"] is False
        assert h["reason"]

    # ── Instamart ─────────────────────────────────────────────────────────────

    def test_instamart_unhealthy_without_store_id(self, clean_db, connected_user_all):
        # im_cookies has tid + deviceId but no store_id → unhealthy.
        h = instamart.session_health(connected_user_all)
        assert h["ok"] is False
        assert h["reason"]

    def test_instamart_unhealthy_when_not_connected(self, clean_db, user_id):
        h = instamart.session_health(user_id)
        assert h["ok"] is False
        assert "reconnect" in h["reason"].lower()

    # ── API endpoint ──────────────────────────────────────────────────────────

    def test_auth_status_exposes_healthy_and_reason(self, client, connected_user_all, user_id):
        data = client.get(f"/api/auth/status/{user_id}").json()
        cs = data["connected_stores"]
        # All 4 connected stores carry the health fields.
        for store in ("bigbasket", "blinkit", "zepto", "instamart"):
            assert "healthy" in cs[store], f"{store} missing 'healthy'"
            assert "reason" in cs[store], f"{store} missing 'reason'"
        # BigBasket (token present, no location required) → healthy.
        assert cs["bigbasket"]["healthy"] is True
        # Blinkit (no location cookies) → unhealthy.
        assert cs["blinkit"]["healthy"] is False
        assert cs["blinkit"]["reason"]
        # Zepto (no store_id) → unhealthy.
        assert cs["zepto"]["healthy"] is False
        # Instamart (no store_id) → unhealthy.
        assert cs["instamart"]["healthy"] is False


# ── Comparison history ────────────────────────────────────────────────────────

class TestHistoryPersistence:
    def test_save_and_read_back(self, clean_db, user_id):
        from storage import user_store
        user_store.save_comparison(
            user_id=user_id,
            query_text="milk 1l\nbread",
            items=[{"name": "milk", "qty": "1l"}, {"name": "bread", "qty": ""}],
            grand_total=88.0,
            savings=12.0,
            stores=["zepto", "blinkit"],
        )
        hist = user_store.get_history(user_id)
        assert len(hist) == 1
        # get_history maps the row to display keys: total / saved / title.
        assert hist[0]["total"] == 88.0
        assert hist[0]["saved"] == 12.0
        assert "milk" in hist[0]["title"].lower()

    def test_history_is_per_user(self, clean_db, user_id):
        from storage import user_store
        user_store.save_comparison(user_id=user_id, query_text="eggs",
                                   items=[{"name": "eggs"}], grand_total=60.0,
                                   savings=0.0, stores=["zepto"])
        assert user_store.get_history("some-other-user") == []

    def test_api_history_requires_auth(self, client):
        r = client.get("/api/history")
        assert r.status_code == 401

    def test_api_history_returns_saved_runs(self, client, clean_db, user_id):
        from storage import user_store
        user_store.save_comparison(user_id=user_id, query_text="rice",
                                   items=[{"name": "rice", "qty": "5kg"}],
                                   grand_total=300.0, savings=40.0, stores=["bigbasket"])
        _login(client, user_id)
        r = client.get("/api/history")
        assert r.status_code == 200
        runs = r.json()
        assert len(runs) == 1
        assert runs[0]["total"] == 300.0
