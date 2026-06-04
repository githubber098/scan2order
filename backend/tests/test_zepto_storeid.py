"""test_zepto_storeid.py — Zepto delivery-store resolution.

Zepto never persists the resolved delivery storeId to cookies/localStorage (the
serviceability cookie stays {"timeSaved":…} even with a saved address — verified
against a real captured session). The browser relay therefore sniffs the storeId
from Zepto's own API request headers and persists it as the _s2o_store_id cookie.
These tests pin the read-back path in zepto._get_zepto_session so the
"No delivery store" health warning clears and search gets a store_id.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stores import zepto


def _connect(user_id, cookies, ls=None):
    from storage import user_store
    user_store.connect_store(user_id, "zepto", cookies, ls or {})


_BASE = {
    "XSRF-TOKEN": "fake-xsrf-token-abc",
    "deviceId": "fake-device-id-zepto",
    "sessionId": "fake-session-id-zepto",
    "accessToken": "fake-zepto-access-token",
}
_SID = "b4dc8d65-ed2e-4142-81b6-373982b13500"
_SID2 = "0059ff6a-7eb0-477a-a7f5-69256f2c444b"


class TestCapturedStoreId:
    def test_resolves_from_s2o_store_id(self, clean_db, user_id):
        _connect(user_id, {**_BASE, "_s2o_store_id": _SID})
        s = zepto._get_zepto_session(user_id)
        assert s["store_id"] == _SID
        # store_ids falls back to the single id when _s2o_store_ids is absent
        assert s["store_ids"] == _SID

    def test_preserves_store_ids_comma_list(self, clean_db, user_id):
        # Critical: the comma-separated multi-store list must NOT be mangled
        # (an earlier-style normkey() would strip the commas).
        _connect(user_id, {
            **_BASE,
            "_s2o_store_id": _SID,
            "_s2o_store_ids": f"{_SID},{_SID2}",
            "_s2o_store_etas": f'{{"{_SID}":-1,"{_SID2}":-1}}',
        })
        s = zepto._get_zepto_session(user_id)
        assert s["store_id"] == _SID
        assert s["store_ids"] == f"{_SID},{_SID2}"
        assert _SID in s["store_etas"] and _SID2 in s["store_etas"]

    def test_url_encoded_values_decoded(self, clean_db, user_id):
        # store_etas often arrives URL-encoded as a cookie value.
        _connect(user_id, {
            **_BASE,
            "_s2o_store_id": _SID,
            "_s2o_store_etas": "%7B%22" + _SID + "%22%3A-1%7D",
        })
        s = zepto._get_zepto_session(user_id)
        assert s["store_etas"] == '{"' + _SID + '":-1}'

    def test_health_ok_with_captured_store_id(self, clean_db, user_id):
        _connect(user_id, {**_BASE, "_s2o_store_id": _SID})
        h = zepto.session_health(user_id)
        assert h["ok"] is True and h["reason"] == ""

    def test_health_warns_without_any_store_id(self, clean_db, user_id):
        _connect(user_id, dict(_BASE))
        h = zepto.session_health(user_id)
        assert h["ok"] is False and "delivery store" in h["reason"].lower()


class TestPrecedence:
    def test_serviceability_wins_when_present(self, clean_db, user_id):
        # If Zepto DID populate serviceability with a storeId, it is authoritative
        # and the captured _s2o value is not needed.
        import json
        svc = json.dumps({"primaryStore": {"storeId": _SID2}})
        _connect(user_id, {**_BASE, "serviceability": svc, "_s2o_store_id": _SID})
        s = zepto._get_zepto_session(user_id)
        assert s["store_id"] == _SID2   # serviceability, not the captured one

    def test_captured_used_when_serviceability_empty(self, clean_db, user_id):
        import json
        _connect(user_id, {
            **_BASE,
            "serviceability": json.dumps({"timeSaved": 1779107493328}),
            "_s2o_store_id": _SID,
        })
        s = zepto._get_zepto_session(user_id)
        assert s["store_id"] == _SID

    def test_hunt_used_as_last_resort(self, clean_db, user_id):
        # No serviceability storeId, no _s2o cookie, but a blob carries a storeId.
        import json
        ls = {"userAddresses": json.dumps({"state": {"primaryStoreId": _SID}})}
        _connect(user_id, dict(_BASE), ls)
        s = zepto._get_zepto_session(user_id)
        assert s["store_id"] == _SID
