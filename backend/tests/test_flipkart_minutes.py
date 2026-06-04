"""test_flipkart_minutes.py — unit tests for the Flipkart Minutes store module.

No network. Covers:
  - _hunt_pincode  (keyed precise pass, fuzzy address-blob fallback, and the
                    false-positive guards that stop a random 6-digit number from
                    being read as a pincode)
  - _get_fm_session / is_session_valid / session_health (all states)
  - _extract_product  (flat + productInfo.value envelope, reject paths)
  - _parse_response   (nested widgets, envelope, empty, dedupe, 8-item cap)
  - search_item_api   (mocked httpx: no-session, 200+products, 200-empty, 302,
                       401, exception; Rome BFF request construction incl.
                       locationContext pincode)
  - add_all_to_cart_api (mocked httpx: empty, no-session, all-ok, mixed, no-pid)

The live search/cart endpoints are geo-restricted to India and can only be
confirmed from the homeserver; these tests pin the parsing/request logic so a
regression is caught even though the real endpoint shape is verified from logs.
"""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stores import flipkart_minutes as fm


# ── Fake httpx plumbing (mirrors test_groq_rotation.py style) ──────────────────

class _FakeResp:
    def __init__(self, status, json_data=None, text="", headers=None):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeClient:
    """Stand-in for httpx.AsyncClient.

    get_resp:   single _FakeResp returned by .get()
    post_resps: queue of _FakeResp for successive .post() calls (last repeats)
    raise_exc:  if set, every request raises it
    capture:    dict that records the calls for assertions
    """
    def __init__(self, get_resp=None, post_resps=None, raise_exc=None, capture=None):
        self._get_resp = get_resp
        self._post_resps = list(post_resps or [])
        self._raise = raise_exc
        self._cap = capture if capture is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        self._cap["get"] = {"url": url, "params": params or {}, "headers": headers or {}}
        if self._raise:
            raise self._raise
        return self._get_resp

    async def post(self, url, json=None, headers=None, content=None):
        self._cap.setdefault("posts", []).append(
            {"url": url, "json": json, "headers": headers or {}}
        )
        if self._raise:
            raise self._raise
        if self._post_resps:
            return self._post_resps.pop(0) if len(self._post_resps) > 1 else self._post_resps[0]
        return _FakeResp(200, {})


def _install(monkeypatch, **kw):
    cap = {}
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: _FakeClient(capture=cap, **kw))
    return cap


def _connect(user_id, cookies=None, ls=None):
    from storage import user_store
    user_store.connect_store(
        user_id, "flipkart_minutes",
        cookies or {"flid": "fl-" + "x" * 20, "T": "tok", "SN": "1"},
        ls or {},
    )


# ── _hunt_pincode ──────────────────────────────────────────────────────────────

class TestHuntPincode:
    def test_keyed_pincode(self):
        import json
        ls = {"fkUserSelectedAddress": json.dumps({"pinCode": "560034", "city": "BLR"})}
        assert fm._hunt_pincode(ls, {}) == "560034"

    def test_keyed_nested_deep(self):
        import json
        ls = {"state": json.dumps({"user": {"addresses": [{"pincode": "110001"}]}})}
        assert fm._hunt_pincode(ls, {}) == "110001"

    def test_fuzzy_pincode_in_address_string(self):
        import json
        ls = {"addr": json.dumps({"fullAddress": "12 MG Road, Bangalore 560034"})}
        assert fm._hunt_pincode(ls, {}) == "560034"

    def test_fuzzy_raw_cookie_recognised_by_key(self):
        # Raw (non-JSON) cookie value whose KEY signals an address.
        ck = {"deliveryAddress": "12 MG Road, Bengaluru, Karnataka 560001"}
        assert fm._hunt_pincode({}, ck) == "560001"

    def test_guard_order_id_in_non_address_blob(self):
        import json
        # 6-digit number but NOT in an address-like blob → must not match.
        ls = {"cart": json.dumps({"orderId": "785412", "total": 999})}
        assert fm._hunt_pincode(ls, {}) == ""

    def test_guard_invalid_first_digit(self):
        import json
        # 9xxxxx is not a valid Indian PIN (first digit 1-8).
        ls = {"addr": json.dumps({"address": "somewhere 987654"})}
        assert fm._hunt_pincode(ls, {}) == ""

    def test_guard_glued_inside_longer_number(self):
        import json
        ls = {"addr": json.dumps({"address": "phone 9876543210 here"})}
        assert fm._hunt_pincode(ls, {}) == ""

    def test_absent(self):
        import json
        assert fm._hunt_pincode({"x": json.dumps({"y": 1})}, {}) == ""

    def test_keyed_beats_fuzzy(self):
        # When both an exact key and a stray address string exist, the precise
        # keyed value wins.
        import json
        ls = {
            "a": json.dumps({"pinCode": "560034"}),
            "b": json.dumps({"address": "old place 110055"}),
        }
        assert fm._hunt_pincode(ls, {}) == "560034"


# ── Session / health ───────────────────────────────────────────────────────────

class TestSession:
    def test_invalid_without_cookies(self, clean_db, user_id):
        assert fm.is_session_valid(user_id) is False
        h = fm.session_health(user_id)
        assert h["ok"] is False and "reconnect" in h["reason"].lower()

    def test_valid_with_flid(self, clean_db, user_id):
        _connect(user_id)
        assert fm.is_session_valid(user_id) is True

    @pytest.mark.parametrize("cookie_name", ["T", "ULSN", "at", "rt"])
    def test_valid_with_captured_auth_cookie_without_flid(
            self, clean_db, user_id, cookie_name):
        _connect(user_id, cookies={
            cookie_name: "fake-" + cookie_name.lower() + "-auth-token-" + "x" * 30,
            "SN": "1",
        })
        assert fm.is_session_valid(user_id) is True

    def test_short_guest_token_without_flid_is_invalid(self, clean_db, user_id):
        _connect(user_id, cookies={"T": "short", "SN": "1"})
        assert fm.is_session_valid(user_id) is False

    def test_health_warns_when_no_pincode(self, clean_db, user_id):
        _connect(user_id)  # flid present, no address
        h = fm.session_health(user_id)
        assert h["ok"] is False and "delivery address" in h["reason"].lower()

    def test_health_ok_with_flid_and_pincode(self, clean_db, user_id):
        import json
        _connect(user_id, ls={"addr": json.dumps({"pinCode": "560034"})})
        h = fm.session_health(user_id)
        assert h["ok"] is True and h["reason"] == ""


class TestGetFmSession:
    def test_extracts_tokens(self, clean_db, user_id):
        _connect(user_id, cookies={"flid": "myflid" + "x" * 14, "T": "mytok", "SN": "7"})
        s = fm._get_fm_session(user_id)
        assert s["flid"].startswith("myflid")
        assert s["t_token"] == "mytok"
        assert s["sn"] == "7"

    def test_url_decodes_values(self, clean_db, user_id):
        _connect(user_id, cookies={"flid": "ab%20cd" + "x" * 14})
        s = fm._get_fm_session(user_id)
        assert "ab cd" in s["flid"]

    def test_empty_when_no_cookies(self, clean_db, user_id):
        assert fm._get_fm_session(user_id) == {}


# ── _extract_product ───────────────────────────────────────────────────────────

class TestExtractProduct:
    def test_flat_shape(self):
        obj = {
            "id": "P1", "title": "Amul Butter 500g",
            "pricing": {"finalPrice": {"value": 260}, "mrp": {"value": 285}},
            "packSize": "500 g", "images": [{"url": "http://img/1"}],
        }
        p = fm._extract_product(obj)
        assert p["product_id"] == "P1"
        assert p["name"] == "Amul Butter 500g"
        assert p["sale_price"] == 260.0 and p["price"] == 285.0
        assert p["unit"] == "500 g"
        assert p["image_url"] == "http://img/1"
        assert p["app"] == "flipkart_minutes" and p["app_name"] == "Flipkart Minutes"

    def test_productinfo_envelope_unwrapped(self):
        obj = {"productInfo": {"value": {
            "id": "P2", "title": "Milk 1L",
            "pricing": {"finalPrice": {"value": 62}},
        }}}
        p = fm._extract_product(obj)
        assert p["product_id"] == "P2" and p["name"] == "Milk 1L"
        assert p["sale_price"] == 62.0

    def test_nested_titles_and_listing_id(self):
        obj = {
            "listingId": "LSTP4",
            "productInfo": {"value": {
                "id": "P4",
                "titles": {"title": "Tomato Local", "subtitle": "500 g"},
                "pricing": {"finalPrice": {"decimalValue": "28.00"},
                            "mrp": {"displayValue": "₹32"}},
                "media": {"images": [{"url": "http://img/4"}]},
            }},
        }
        p = fm._extract_product(obj)
        assert p["product_id"] == "P4"
        assert p["store_product_id"] == "LSTP4"
        assert p["listing_id"] == "LSTP4"
        assert p["unit"] == "500 g"
        assert p["sale_price"] == 28.0 and p["price"] == 32.0
        # media.images[0].url must be extracted
        assert p["image_url"] == "http://img/4"

    def test_image_url_flat_images_list(self):
        """images: [{"url": "..."}] at the top level (no media wrapper)."""
        obj = {
            "id": "P10", "title": "Butter", "mrp": 55,
            "images": [{"url": "http://img/flat"}],
        }
        p = fm._extract_product(obj)
        assert p["image_url"] == "http://img/flat"

    def test_image_url_direct_string(self):
        """imageUrl as a plain string field."""
        obj = {"id": "P11", "title": "Ghee", "mrp": 200, "imageUrl": "http://img/str"}
        p = fm._extract_product(obj)
        assert p["image_url"] == "http://img/str"

    def test_image_url_primary_image(self):
        """primaryImage string field (Flipkart alternate key)."""
        obj = {"id": "P12", "title": "Oil", "mrp": 99, "primaryImage": "http://img/primary"}
        p = fm._extract_product(obj)
        assert p["image_url"] == "http://img/primary"

    def test_image_url_media_primary_image(self):
        """media.primaryImage nested field."""
        obj = {
            "id": "P13", "title": "Dal", "mrp": 120,
            "media": {"primaryImage": "http://img/media_primary"},
        }
        p = fm._extract_product(obj)
        assert p["image_url"] == "http://img/media_primary"

    def test_image_url_missing_returns_empty(self):
        """No image fields → image_url is empty string, not None."""
        obj = {"id": "P14", "title": "Salt", "mrp": 20}
        p = fm._extract_product(obj)
        assert p["image_url"] == ""

    def test_sale_falls_back_to_mrp(self):
        obj = {"id": "P3", "title": "X", "mrp": 50}
        p = fm._extract_product(obj)
        assert p["sale_price"] == 50.0 and p["price"] == 50.0

    def test_none_when_no_id(self):
        assert fm._extract_product({"title": "X", "mrp": 10}) is None

    def test_none_when_no_name(self):
        assert fm._extract_product({"id": "P", "mrp": 10}) is None

    def test_none_when_no_price(self):
        assert fm._extract_product({"id": "P", "title": "X"}) is None

    def test_none_for_non_dict(self):
        assert fm._extract_product("nope") is None


# ── _parse_response ────────────────────────────────────────────────────────────

class TestParseResponse:
    def test_nested_widget_shape(self):
        data = {"slots": [{"widget": {"data": {"products": [
            {"id": "P1", "title": "Bread", "pricing": {"finalPrice": {"value": 40}}},
        ]}}}]}
        out = fm._parse_response(data)
        assert len(out) == 1 and out[0]["product_id"] == "P1"

    def test_envelope_via_parse(self):
        data = {"products": [{"productInfo": {"value": {
            "id": "P2", "title": "Eggs", "pricing": {"finalPrice": {"value": 75}},
        }}}]}
        out = fm._parse_response(data)
        assert len(out) == 1 and out[0]["name"] == "Eggs"

    def test_empty(self):
        assert fm._parse_response({"slots": []}) == []
        assert fm._parse_response({}) == []

    def test_dedupe_by_product_id(self):
        dup = {"id": "SAME", "title": "Dup", "mrp": 10}
        data = {"a": [dup], "b": [dict(dup)]}
        out = fm._parse_response(data)
        assert len(out) == 1

    def test_caps_at_8(self):
        items = [{"id": str(i), "title": f"I{i}", "mrp": 10} for i in range(20)]
        data = {"products": items}
        assert len(fm._parse_response(data)) == 8


# ── search_item_api (mocked httpx) ─────────────────────────────────────────────

class TestSearchApi:
    async def test_no_session_returns_empty_no_network(self, clean_db, user_id, monkeypatch):
        # If httpx is touched at all the test would fail to set up a resp; assert [].
        cap = _install(monkeypatch, post_resps=[_FakeResp(200, {})])
        out = await fm.search_item_api(user_id, "milk")
        assert out == []
        assert "posts" not in cap   # never made a network call

    async def test_200_with_products(self, clean_db, user_id, monkeypatch):
        import json
        _connect(user_id, ls={"addr": json.dumps({"pinCode": "560034"})})
        body = {"products": [
            {"id": "P1", "title": "Amul Butter 100g",
             "pricing": {"finalPrice": {"value": 57}, "mrp": {"value": 60}},
             "packSize": "100 g"},
        ]}
        cap = _install(monkeypatch, post_resps=[_FakeResp(200, body)])
        out = await fm.search_item_api(user_id, "amul butter")
        assert len(out) == 1
        assert out[0]["product_id"] == "P1" and out[0]["sale_price"] == 57.0
        # Request construction: POST to the confirmed Rome BFF endpoint, with
        # marketplace/search store in pageUri and pincode in locationContext.
        post = cap["posts"][0]
        assert post["url"] == fm._FM_SEARCH_URL
        assert post["headers"].get("flipkart_secure") == "true"
        assert "FKUA/msite" in post["headers"].get("X-User-Agent", "")
        assert "x-pincode" not in post["headers"]
        assert post["json"]["locationContext"] == {"pincode": 560034, "changed": False}
        assert "marketplace=HYPERLOCAL" in post["json"]["pageUri"]
        assert "sid=search.flipkart.com" in post["json"]["pageUri"]
        assert post["json"]["pageUri"].startswith("/hyperlocal/pr?")
        assert "amul%20butter" in post["json"]["pageUri"] or "amul+butter" in post["json"]["pageUri"]

    async def test_200_empty_body(self, clean_db, user_id, monkeypatch):
        _connect(user_id)
        _install(monkeypatch, post_resps=[_FakeResp(200, {"slots": []})])
        assert await fm.search_item_api(user_id, "milk") == []

    async def test_302_redirect(self, clean_db, user_id, monkeypatch):
        _connect(user_id)
        _install(monkeypatch, post_resps=[_FakeResp(
            302, None, headers={"location": "https://login"})])
        assert await fm.search_item_api(user_id, "milk") == []

    async def test_401_unauthorised(self, clean_db, user_id, monkeypatch):
        _connect(user_id)
        _install(monkeypatch, post_resps=[_FakeResp(401, None, text="unauth")])
        assert await fm.search_item_api(user_id, "milk") == []

    async def test_exception_is_swallowed(self, clean_db, user_id, monkeypatch):
        _connect(user_id)
        _install(monkeypatch, raise_exc=httpx.ConnectError("boom"))
        assert await fm.search_item_api(user_id, "milk") == []


# ── add_all_to_cart_api (mocked httpx) ─────────────────────────────────────────

class TestCartApi:
    async def test_empty_items(self, clean_db, user_id):
        res = await fm.add_all_to_cart_api(user_id, [])
        assert res == {"success": True, "items": []}

    async def test_no_session(self, clean_db, user_id, monkeypatch):
        cap = _install(monkeypatch, post_resps=[_FakeResp(200, {})])
        res = await fm.add_all_to_cart_api(user_id, [{"product_id": "P1", "count": 1}])
        assert res["success"] is False
        assert "posts" not in cap   # never hit the network without a session

    async def test_all_success(self, clean_db, user_id, monkeypatch):
        _connect(user_id)
        cap = _install(monkeypatch, post_resps=[_FakeResp(200, {})])
        res = await fm.add_all_to_cart_api(
            user_id,
            [{"product_id": "P1", "listing_id": "LSTP1", "count": 2},
             {"product_id": "P2", "count": 1}],
        )
        assert res["success"] is True
        assert [i["success"] for i in res["items"]] == [True, True]
        assert res["items"][0]["count_added"] == 2
        assert len(cap["posts"]) == 2
        post = cap["posts"][0]
        assert post["url"] == fm._FM_CART_URL
        assert post["json"]["browseContext"]["listings"] == ["LSTP1"]
        cart_ctx = post["json"]["browseCartContext"]["cartContext"]["LSTP1"]
        assert cart_ctx["productId"] == "P1"
        assert cart_ctx["quantity"] == 2

    async def test_mixed_success_and_failure(self, clean_db, user_id, monkeypatch):
        _connect(user_id)
        _install(monkeypatch, post_resps=[_FakeResp(200, {}), _FakeResp(500, None, text="err")])
        res = await fm.add_all_to_cart_api(
            user_id,
            [{"product_id": "P1", "count": 1}, {"product_id": "P2", "count": 1}],
        )
        assert res["success"] is True            # ok_any
        assert res["items"][0]["success"] is True
        assert res["items"][1]["success"] is False

    async def test_missing_product_id_marked_failed(self, clean_db, user_id, monkeypatch):
        _connect(user_id)
        cap = _install(monkeypatch, post_resps=[_FakeResp(200, {})])
        res = await fm.add_all_to_cart_api(
            user_id,
            [{"product_id": "", "count": 1}, {"product_id": "P2", "count": 1}],
        )
        assert res["items"][0]["success"] is False
        assert res["items"][1]["success"] is True
        # Only the valid item should have triggered a POST.
        assert len(cap.get("posts", [])) == 1

    async def test_single_item_wrapper(self, clean_db, user_id, monkeypatch):
        _connect(user_id)
        _install(monkeypatch, post_resps=[_FakeResp(200, {})])
        res = await fm.add_to_cart_api(user_id, "P1", count=3)
        assert res["success"] is True and res["count_added"] == 3


# ── checkout_url ───────────────────────────────────────────────────────────────

def test_checkout_url():
    url = fm.checkout_url()
    assert url.startswith("https://www.flipkart.com/")
    assert "viewcart" in url
