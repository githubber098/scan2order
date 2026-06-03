"""test_instamart.py — pure-function unit tests for the Swiggy Instamart store.

No network: covers device-id extraction, store-id hunting, session validity /
health, price normalisation, and the recursive search-response parser. The live
search/cart paths are exercised manually against a real session.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stores import instamart


class TestRawDeviceId:
    def test_extracts_uuid_from_signed_cookie(self):
        raw = "s%3Abc911281-56ca-4040-bd6d-625d161dd141.RItLH1M567s6BnGXKT"
        assert instamart._raw_device_id(raw) == "bc911281-56ca-4040-bd6d-625d161dd141"

    def test_plain_uuid_passthrough(self):
        u = "bc911281-56ca-4040-bd6d-625d161dd141"
        assert instamart._raw_device_id(u) == u

    def test_empty(self):
        assert instamart._raw_device_id("") == ""


class TestHuntStoreId:
    def test_finds_storeId_in_localstorage_json(self):
        ls = {"someState": '{"location":{"storeId":"1391286","city":"BLR"}}'}
        assert instamart._hunt_store_id(ls, {}) == "1391286"

    def test_finds_primaryStoreId_in_cookie_json(self):
        ck = {"loc": '{"primaryStoreId": 42}'}
        assert instamart._hunt_store_id({}, ck) == "42"

    def test_none_when_absent(self):
        assert instamart._hunt_store_id({"x": '{"y":1}'}, {"a": "b"}) == ""


class TestSession:
    def test_invalid_without_cookies(self, clean_db, user_id):
        assert instamart.is_session_valid(user_id) is False
        h = instamart.session_health(user_id)
        assert h["ok"] is False and "reconnect" in h["reason"].lower()

    def test_valid_with_tid_and_device(self, clean_db, user_id):
        from storage import user_store
        user_store.connect_store(user_id, "instamart", {
            "tid": "jwt.tok.en",
            "deviceId": "s%3Abc911281-56ca-4040-bd6d-625d161dd141.sig",
        })
        assert instamart.is_session_valid(user_id) is True

    def test_health_warns_when_no_store_id(self, clean_db, user_id):
        from storage import user_store
        user_store.connect_store(user_id, "instamart", {
            "tid": "jwt.tok.en",
            "deviceId": "s%3Abc911281-56ca-4040-bd6d-625d161dd141.sig",
        })
        h = instamart.session_health(user_id)
        assert h["ok"] is False and "delivery store" in h["reason"].lower()


class TestPriceNormalise:
    def test_paise_to_rupees(self):
        # 5500 (whole, >=1000) → treated as paise → 55.0
        assert instamart._to_rupees(5500) == 55.0

    def test_rupees_passthrough(self):
        assert instamart._to_rupees(55.0) == 55.0
        assert instamart._to_rupees(999) == 999.0  # below the paise threshold


class TestParseSearchResponse:
    def test_extracts_nested_variation(self):
        data = {"data": {"widgets": [{"data": [{
            "display_name": "Amul Gold Milk",
            "id": "ABC123",
            "price": {"offer_price": 8300, "mrp": 8500},
            "quantity": "1 L",
            "images": ["instamart/img1"],
        }]}]}}
        out = instamart._parse_search_response(data)
        assert len(out) == 1
        p = out[0]
        assert p["name"] == "Amul Gold Milk"
        assert p["product_id"] == "ABC123"
        assert p["sale_price"] == 83.0 and p["price"] == 85.0
        assert p["unit"] == "1 L"
        assert p["app"] == "instamart" and p["app_name"] == "Instamart"
        assert p["image_url"].endswith("instamart/img1")

    def test_empty_when_no_products(self):
        assert instamart._parse_search_response({"data": {"widgets": []}}) == []

    def test_caps_at_8(self):
        items = [{"display_name": f"Item {i}", "id": str(i),
                  "price": {"offer_price": 1000}} for i in range(20)]
        data = {"data": {"items": items}}
        assert len(instamart._parse_search_response(data)) == 8
