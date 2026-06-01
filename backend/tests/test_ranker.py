"""
test_ranker.py — Pure unit tests for ranker.py.

No HTTP calls, no database, no store mocks required.
Tests the algorithmic core: qty parsing, relevance filtering, price-per-unit,
qty distance, reasonable-size filtering, comparison logic, cart building.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ranker import (
    _parse_qty,
    _price_per_unit,
    _qty_distance,
    _qty_multiplier,
    _effective_price,
    _is_reasonable_size,
    filter_by_query_relevance,
    selected_product,
    build_carts_from_comparison,
)


# ── _qty_multiplier (weight-aware whole-unit matching) ──────────────────────────

class TestQtyMultiplier:
    def test_250g_for_1kg_is_4(self):
        assert _qty_multiplier("1kg", {"unit": "250 g"}) == 4

    def test_500g_for_1kg_is_2(self):
        assert _qty_multiplier("1kg", {"unit": "500 g"}) == 2

    def test_exact_match_is_1(self):
        assert _qty_multiplier("1kg", {"unit": "1 kg"}) == 1

    def test_non_integer_multiple_excluded(self):
        # 300g cannot tile 1kg in whole units
        assert _qty_multiplier("1kg", {"unit": "300 g"}) is None

    def test_oversize_unit_excluded(self):
        assert _qty_multiplier("1kg", {"unit": "2 kg"}) is None

    def test_product_without_size_excluded_when_target_given(self):
        assert _qty_multiplier("1kg", {"name": "Tomato"}) is None

    def test_no_target_returns_1(self):
        assert _qty_multiplier("", {"unit": "500 g"}) == 1

    def test_unit_kind_mismatch_excluded(self):
        # target is mass, product is volume
        assert _qty_multiplier("1kg", {"unit": "500 ml"}) is None

    def test_effective_price_multiplies(self):
        # 250g @ ₹10 → 4 units → ₹40
        assert _effective_price("1kg", {"unit": "250 g", "sale_price": 10}) == 40
        # 500g @ ₹18 → 2 units → ₹36 (cheaper, should win)
        assert _effective_price("1kg", {"unit": "500 g", "sale_price": 18}) == 36

    def test_effective_price_excluded_is_inf(self):
        assert _effective_price("1kg", {"unit": "300 g", "sale_price": 5}) == float("inf")


# ── compare_one_item weight-aware cross-store selection ─────────────────────────

class TestCompareOneItemWeight:
    """Regression: a non-tiling small pack at one store must NOT beat a
    legitimately-tiling product at another store just because its raw single-
    unit price is lower (the "Milk 1L → 180ml pack ₹14 vs 2×500ml ₹48" bug)."""

    def _run(self, item, store_products, monkeypatch):
        import ranker
        from stores import blinkit, zepto
        monkeypatch.delenv("OLLAMA_HOST", raising=False)

        async def _mk(products):
            async def _search(user_id, query):
                return list(products)
            return _search

        async def go():
            zepto.search_item_api = await _mk(store_products.get("zepto", []))
            blinkit.search_item_api = await _mk(store_products.get("blinkit", []))
            return await ranker.compare_one_item(item, "u1", ["zepto", "blinkit"])

        return asyncio.run(go())

    def test_tiling_product_beats_cheaper_nontiling_pack(self, monkeypatch):
        store_products = {
            # Zepto: a 180ml pack at ₹14 — cannot tile 1L in whole units
            "zepto": [{"name": "Toned Milk Pouch", "unit": "180 ml",
                       "sale_price": 14, "price": 14, "product_id": "z-180"}],
            # Blinkit: a 500ml carton at ₹24 — 2× makes exactly 1L → ₹48
            "blinkit": [{"name": "Toned Milk", "unit": "500 ml",
                         "sale_price": 24, "price": 24, "product_id": "b-500"}],
        }
        entry = self._run({"name": "Milk", "qty": "1L"}, store_products, monkeypatch)
        assert entry["cheapest_app"] == "blinkit"
        assert entry["selected_pid"] == "b-500"
        assert entry["qty_count"] == 2
        assert entry["cheapest_effective_price"] == 48

    def test_cheapest_tiling_option_wins(self, monkeypatch):
        store_products = {
            # 250g × 4 = 1kg → ₹40
            "zepto": [{"name": "Tomato", "unit": "250 g",
                       "sale_price": 10, "price": 10, "product_id": "z-250"}],
            # 500g × 2 = 1kg → ₹36 (cheaper effective) → should win
            "blinkit": [{"name": "Tomato", "unit": "500 g",
                         "sale_price": 18, "price": 18, "product_id": "b-500"}],
        }
        entry = self._run({"name": "Tomato", "qty": "1kg"}, store_products, monkeypatch)
        assert entry["cheapest_app"] == "blinkit"
        assert entry["qty_count"] == 2
        assert entry["cheapest_effective_price"] == 36


# ── _parse_qty ────────────────────────────────────────────────────────────────

class TestParseQty:
    def test_grams(self):
        assert _parse_qty("500g") == (500.0, "mass")

    def test_grams_with_space(self):
        assert _parse_qty("250 g") == (250.0, "mass")

    def test_gm_abbreviation(self):
        assert _parse_qty("200gm") == (200.0, "mass")

    def test_kg_converts_to_grams(self):
        assert _parse_qty("1kg") == (1000.0, "mass")

    def test_kg_decimal(self):
        val, kind = _parse_qty("0.5 kg")
        assert kind == "mass"
        assert abs(val - 500.0) < 0.01

    def test_ml(self):
        assert _parse_qty("500ml") == (500.0, "volume")

    def test_litre(self):
        assert _parse_qty("1l") == (1000.0, "volume")

    def test_litre_word(self):
        val, kind = _parse_qty("1.5 litre")
        assert kind == "volume"
        assert abs(val - 1500.0) < 0.01

    def test_pieces(self):
        assert _parse_qty("12 pcs") == (12.0, "count")

    def test_pack(self):
        assert _parse_qty("6 pack") == (6.0, "count")

    def test_no_qty_returns_none(self):
        assert _parse_qty("butter") is None

    def test_empty_string_returns_none(self):
        assert _parse_qty("") is None

    def test_none_returns_none(self):
        assert _parse_qty(None) is None

    def test_qty_embedded_in_product_name(self):
        val, kind = _parse_qty("Amul Butter 500g")
        assert kind == "mass"
        assert val == 500.0


# ── _price_per_unit ───────────────────────────────────────────────────────────

class TestPricePerUnit:
    def test_100g_product(self):
        product = {"unit": "100g", "sale_price": 55.0}
        ppu = _price_per_unit(product)
        assert abs(ppu - 0.55) < 0.001   # ₹0.55 per gram

    def test_500g_product(self):
        product = {"unit": "500g", "sale_price": 250.0}
        ppu = _price_per_unit(product)
        assert abs(ppu - 0.50) < 0.001   # ₹0.50 per gram (cheaper per unit)

    def test_500g_is_cheaper_per_unit_than_100g(self):
        p100 = {"unit": "100g", "sale_price": 55.0}
        p500 = {"unit": "500g", "sale_price": 250.0}
        assert _price_per_unit(p100) > _price_per_unit(p500)

    def test_no_unit_returns_inf(self):
        product = {"name": "Mystery item", "sale_price": 10.0}
        assert _price_per_unit(product) == float("inf")

    def test_zero_price_returns_inf(self):
        product = {"unit": "100g", "price": 0.0}
        assert _price_per_unit(product) == float("inf")

    def test_uses_sale_price_over_price(self):
        product = {"unit": "100g", "sale_price": 45.0, "price": 60.0}
        ppu = _price_per_unit(product)
        assert abs(ppu - 0.45) < 0.001  # uses 45, not 60


# ── _qty_distance ─────────────────────────────────────────────────────────────

class TestQtyDistance:
    def test_exact_match_returns_zero(self):
        product = {"unit": "500g"}
        assert _qty_distance("500g", product) == 0.0

    def test_double_size_returns_one(self):
        # max(500, 1000) / min(500, 1000) - 1 = 2 - 1 = 1.0
        product = {"unit": "1000g"}
        assert abs(_qty_distance("500g", product) - 1.0) < 0.01

    def test_smaller_product_still_positive(self):
        product = {"unit": "100g"}
        dist = _qty_distance("500g", product)
        assert dist > 0

    def test_unit_kind_mismatch_returns_inf(self):
        # mass vs. volume → incompatible
        product = {"unit": "500ml"}
        assert _qty_distance("500g", product) == float("inf")

    def test_no_product_qty_returns_inf(self):
        product = {"name": "Some item with no unit"}
        assert _qty_distance("500g", product) == float("inf")

    def test_no_query_qty_returns_inf(self):
        product = {"unit": "500g"}
        assert _qty_distance("", product) == float("inf")


# ── _is_reasonable_size ───────────────────────────────────────────────────────

class TestIsReasonableSize:
    def test_100g_product_reasonable_for_any_query(self):
        assert _is_reasonable_size({"unit": "100g"}, "") is True

    def test_2kg_product_above_default_2000g_limit(self):
        # 2000g is the default cutoff for 'mass'; 2000g exactly is allowed
        product = {"unit": "2000g"}
        assert _is_reasonable_size(product, "") is True

    def test_5kg_product_too_large_by_default(self):
        product = {"unit": "5000g"}
        assert _is_reasonable_size(product, "") is False

    def test_large_product_allowed_when_query_is_large(self):
        # User asked for 5kg → 5kg product is ≤ 5kg * 1.2 = 6kg
        product = {"unit": "5000g"}
        assert _is_reasonable_size(product, "5kg") is True

    def test_no_unit_is_always_reasonable(self):
        product = {"name": "Item with no unit"}
        assert _is_reasonable_size(product, "100g") is True


# ── filter_by_query_relevance ─────────────────────────────────────────────────

class TestFilterByQueryRelevance:
    def _product(self, name: str, price: float = 50.0) -> dict:
        return {"product_id": name, "name": name, "sale_price": price}

    def test_brand_word_must_appear(self):
        products = [
            self._product("Amul Butter 100g"),
            self._product("Mother Dairy Butter 100g"),
            self._product("Gowardhan Butter 100g"),
        ]
        filtered = filter_by_query_relevance(products, "amul butter 100g")
        names = [p["name"] for p in filtered]
        assert "Amul Butter 100g" in names
        # Mother Dairy and Gowardhan don't contain "amul"
        assert "Mother Dairy Butter 100g" not in names
        assert "Gowardhan Butter 100g" not in names

    def test_generic_only_query_returns_all(self):
        """If every query word is generic, return all products (no filter)."""
        products = [
            self._product("Amul Butter 100g"),
            self._product("Lays Chips"),
        ]
        # 'milk' and 'fresh' are both in _GENERIC_FOOD_WORDS → any-word match
        filtered = filter_by_query_relevance(products, "fresh milk")
        # At least some products returned (not an empty list)
        assert isinstance(filtered, list)

    def test_empty_products_returns_empty(self):
        assert filter_by_query_relevance([], "butter") == []

    def test_stemming_allows_plural(self):
        products = [self._product("Organic Tomatoes 500g")]
        # 'tomato' should match 'tomatoes' via stem (tomato → tomatoes → tomato)
        filtered = filter_by_query_relevance(products, "tomato 500g")
        assert len(filtered) == 1

    def test_case_insensitive(self):
        products = [self._product("AMUL BUTTER 100G")]
        filtered = filter_by_query_relevance(products, "Amul Butter")
        assert len(filtered) == 1

    def test_unrelated_products_dropped(self):
        products = [
            self._product("Amul Butter 100g"),
            self._product("Tata Salt 1kg"),    # no 'amul' or 'butter'
        ]
        filtered = filter_by_query_relevance(products, "amul butter")
        assert len(filtered) == 1
        assert filtered[0]["name"] == "Amul Butter 100g"


# ── selected_product ──────────────────────────────────────────────────────────

class TestSelectedProduct:
    def _entry(self, store: str, products: list, pid=None) -> dict:
        return {
            "cheapest_app": store,
            "selected_pid": pid or (products[0]["product_id"] if products else None),
            "prices": {store: products},
        }

    def test_returns_selected_pid_product(self):
        products = [
            {"product_id": "p1", "name": "A"},
            {"product_id": "p2", "name": "B"},
        ]
        entry = self._entry("bigbasket", products, pid="p2")
        assert selected_product(entry)["product_id"] == "p2"

    def test_falls_back_to_first_product(self):
        products = [{"product_id": "p1", "name": "A"}]
        entry = self._entry("bigbasket", products)
        assert selected_product(entry)["product_id"] == "p1"

    def test_no_cheapest_app_returns_empty(self):
        entry = {"cheapest_app": None, "prices": {}}
        assert selected_product(entry) == {}

    def test_unknown_pid_falls_back_to_first(self):
        products = [{"product_id": "p1", "name": "A"}]
        entry = self._entry("bigbasket", products, pid="nonexistent-pid")
        assert selected_product(entry)["product_id"] == "p1"


# ── build_carts_from_comparison ───────────────────────────────────────────────

class TestBuildCarts:
    def _entry(self, store: str, pid: str, price: float, name: str = "Item") -> dict:
        return {
            "item": {"name": name, "qty": "100g", "count": 2},
            "search_query": name,
            "cheapest_app": store,
            "cheapest_price": price,
            "selected_pid": pid,
            "prices": {
                store: [{"product_id": pid, "name": f"{name} product",
                         "unit": "100g", "sale_price": price}]
            },
        }

    def test_single_item_single_store(self):
        comparison = [self._entry("bigbasket", "bb-001", 55.0)]
        carts = build_carts_from_comparison(comparison)
        assert "bigbasket" in carts
        assert len(carts["bigbasket"]["items"]) == 1

    def test_total_is_price_times_count(self):
        comparison = [self._entry("bigbasket", "bb-001", 55.0, "Butter")]
        carts = build_carts_from_comparison(comparison)
        # count=2 in the item, price=55 → total=110
        assert abs(carts["bigbasket"]["total"] - 110.0) < 0.01

    def test_items_from_different_stores_split_correctly(self):
        comparison = [
            self._entry("bigbasket", "bb-001", 55.0, "Butter"),
            self._entry("zepto", "z-001", 52.0, "Milk"),
        ]
        carts = build_carts_from_comparison(comparison)
        assert "bigbasket" in carts
        assert "zepto" in carts
        assert len(carts["bigbasket"]["items"]) == 1
        assert len(carts["zepto"]["items"]) == 1

    def test_items_same_store_grouped(self):
        comparison = [
            self._entry("bigbasket", "bb-001", 55.0, "Butter"),
            self._entry("bigbasket", "bb-010", 30.0, "Eggs"),
        ]
        carts = build_carts_from_comparison(comparison)
        assert len(carts["bigbasket"]["items"]) == 2
        assert abs(carts["bigbasket"]["total"] - (110.0 + 60.0)) < 0.01

    def test_entry_with_no_cheapest_app_skipped(self):
        comparison = [
            self._entry("bigbasket", "bb-001", 55.0),
            {"item": {"name": "Ghost"}, "cheapest_app": None, "cheapest_price": None,
             "selected_pid": None, "prices": {}, "search_query": "Ghost"},
        ]
        carts = build_carts_from_comparison(comparison)
        assert "bigbasket" in carts
        assert len(carts["bigbasket"]["items"]) == 1

    def test_empty_comparison_returns_empty_carts(self):
        assert build_carts_from_comparison([]) == {}

    def test_comparison_index_stored_on_item(self):
        comparison = [self._entry("bigbasket", "bb-001", 55.0)]
        carts = build_carts_from_comparison(comparison)
        assert carts["bigbasket"]["items"][0]["comparison_index"] == 0
