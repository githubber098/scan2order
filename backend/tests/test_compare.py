"""
test_compare.py — Tests for the compare, search, and progress endpoints.

Store search functions are replaced by the `mock_stores` fixture so tests
run offline and fast.

Covers:
  POST /api/compare            — full comparison across connected stores
  POST /api/compare/item       — single-item re-compare
  GET  /api/compare/progress   — per-user progress state
  POST /api/search             — single query across connected stores
  POST /search                 — mobile compat shim
"""

import pytest


# ── /api/compare ──────────────────────────────────────────────────────────────

class TestApiCompare:
    def test_compare_returns_comparison_list(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/api/compare", json={
            "user_id": user_id,
            "items": [{"name": "Amul Butter", "qty": "100g", "count": 1}],
        })
        assert r.status_code == 200
        data = r.json()
        assert "comparison" in data
        assert len(data["comparison"]) == 1

    def test_compare_finds_cheapest_store(
            self, client, connected_user_bb, user_id, mock_stores):
        """BigBasket is the only connected store, so it should be cheapest_app."""
        r = client.post("/api/compare", json={
            "user_id": user_id,
            "items": [{"name": "Amul Butter", "qty": "100g", "count": 1}],
        })
        data = r.json()
        entry = data["comparison"][0]
        assert entry["cheapest_app"] == "bigbasket"
        assert entry["cheapest_price"] == 55.0

    def test_compare_all_stores_picks_zepto(
            self, client, connected_user_all, user_id, mock_stores):
        """Zepto has the lowest price (52.0) for Amul Butter 100g."""
        r = client.post("/api/compare", json={
            "user_id": user_id,
            "items": [{"name": "Amul Butter", "qty": "100g", "count": 1}],
        })
        data = r.json()
        entry = data["comparison"][0]
        # Zepto: 52.0, BB: 55.0, Blinkit: 58.0 → Zepto wins
        assert entry["cheapest_app"] == "zepto"
        assert entry["cheapest_price"] == 52.0

    def test_compare_returns_prices_per_store(
            self, client, connected_user_all, user_id, mock_stores):
        r = client.post("/api/compare", json={
            "user_id": user_id,
            "items": [{"name": "Amul Butter", "qty": "100g", "count": 1}],
        })
        data = r.json()
        prices = data["comparison"][0]["prices"]
        assert "bigbasket" in prices
        assert "blinkit" in prices
        assert "zepto" in prices

    def test_compare_grand_total_matches_carts(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/api/compare", json={
            "user_id": user_id,
            "items": [{"name": "Amul Butter", "qty": "100g", "count": 2}],
        })
        data = r.json()
        carts_total = sum(c["total"] for c in data["carts"].values())
        assert abs(data["grand_total"] - carts_total) < 0.01

    def test_compare_missing_user_id(self, client, clean_db):
        # A request with no session cookie and no body user_id is treated as a
        # guest with no backing store → 401 login_required (not a raw error string).
        r = client.post("/api/compare", json={
            "items": [{"name": "Milk", "qty": "1L"}],
        })
        assert r.status_code == 401
        data = r.json()
        assert "error" in data
        assert data["error"] == "login_required"

    def test_compare_empty_items(self, client, connected_user_bb, user_id, clean_db):
        r = client.post("/api/compare", json={
            "user_id": user_id,
            "items": [],
        })
        data = r.json()
        assert "error" in data

    def test_compare_no_stores_connected(self, client, user_id, clean_db):
        """User with no connected stores gets a descriptive error."""
        r = client.post("/api/compare", json={
            "user_id": user_id,
            "items": [{"name": "Eggs"}],
        })
        data = r.json()
        assert "error" in data
        error_lower = data["error"].lower()
        assert "no stores" in error_lower or "connected" in error_lower

    def test_compare_summary_counts_are_correct(
            self, client, connected_user_all, user_id, mock_stores):
        items = [
            {"name": "Amul Butter", "qty": "100g"},
            {"name": "Amul Butter", "qty": "500g"},
        ]
        r = client.post("/api/compare", json={"user_id": user_id, "items": items})
        data = r.json()
        summary = data["summary"]
        assert summary["total_items"] == 2

    def test_compare_item_not_found_still_returns_entry(
            self, client, connected_user_bb, user_id, mock_stores):
        """Searching for an item with no matches returns an entry with cheapest_app=None."""
        r = client.post("/api/compare", json={
            "user_id": user_id,
            "items": [{"name": "xyzzy_nonexistent_item_999"}],
        })
        data = r.json()
        entry = data["comparison"][0]
        assert entry["cheapest_app"] is None


# ── /api/compare/item ─────────────────────────────────────────────────────────

class TestApiCompareItem:
    def test_single_item_compare(self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/api/compare/item", json={
            "user_id": user_id,
            "item": {"name": "Amul Butter", "qty": "100g"},
        })
        assert r.status_code == 200
        data = r.json()
        assert "entry" in data
        assert data["entry"]["cheapest_app"] == "bigbasket"

    def test_single_item_missing_user_id(self, client, clean_db):
        r = client.post("/api/compare/item", json={
            "item": {"name": "Milk"},
        })
        assert "error" in r.json()

    def test_single_item_missing_name(self, client, connected_user_bb, user_id, clean_db):
        r = client.post("/api/compare/item", json={
            "user_id": user_id,
            "item": {"qty": "1L"},
        })
        assert "error" in r.json()


# ── /api/compare/progress ─────────────────────────────────────────────────────

class TestCompareProgress:
    def test_progress_for_new_user(self, client, user_id, clean_db):
        r = client.get(f"/api/compare/progress?user_id={user_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["running"] is False
        assert data["total"] == 0
        assert data["done"] == 0

    def test_progress_shape(self, client, user_id, clean_db):
        data = client.get(f"/api/compare/progress?user_id={user_id}").json()
        assert "running" in data
        assert "total" in data
        assert "done" in data
        assert "current" in data


# ── /api/search ───────────────────────────────────────────────────────────────

class TestApiSearch:
    def test_search_returns_results(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/api/search", json={
            "user_id": user_id,
            "query": "Amul Butter 100g",
        })
        assert r.status_code == 200
        data = r.json()
        assert "results" in data
        assert "bigbasket" in data["results"]

    def test_search_returns_cheapest(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/api/search", json={
            "user_id": user_id,
            "query": "Amul Butter 100g",
        })
        data = r.json()
        assert "cheapest" in data
        assert data["cheapest"] is not None

    def test_search_missing_user_id(self, client, clean_db):
        r = client.post("/api/search", json={"query": "milk"})
        assert "error" in r.json()

    def test_search_missing_query(self, client, connected_user_bb, user_id, clean_db):
        r = client.post("/api/search", json={"user_id": user_id, "query": ""})
        assert "error" in r.json()

    def test_search_no_stores_connected(self, client, user_id, clean_db):
        r = client.post("/api/search", json={
            "user_id": user_id,
            "query": "milk",
        })
        assert "error" in r.json()


# ── /search (mobile compat) ───────────────────────────────────────────────────

class TestMobileSearch:
    def test_mobile_search_returns_products(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/search", json={
            "user_id": user_id,
            "items": [{"name": "Amul Butter"}],
        })
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert isinstance(data["products"], list)
        assert len(data["products"]) > 0

    def test_mobile_search_product_shape(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/search", json={
            "user_id": user_id,
            "items": [{"name": "Amul Butter"}],
        })
        products = r.json()["products"]
        for p in products:
            assert "name" in p
            assert "price" in p
            assert "store" in p
            assert "prod_id" in p
            assert "recommended" in p

    def test_mobile_search_marks_recommended(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/search", json={
            "user_id": user_id,
            "items": [{"name": "Amul Butter"}],
        })
        products = r.json()["products"]
        recommended = [p for p in products if p["recommended"]]
        assert len(recommended) >= 1

    def test_mobile_search_missing_user_id(self, client, clean_db):
        r = client.post("/search", json={"items": [{"name": "milk"}]})
        data = r.json()
        assert data["success"] is False

    def test_mobile_search_no_stores(self, client, user_id, clean_db):
        r = client.post("/search", json={
            "user_id": user_id,
            "items": [{"name": "milk"}],
        })
        data = r.json()
        assert data["success"] is False
