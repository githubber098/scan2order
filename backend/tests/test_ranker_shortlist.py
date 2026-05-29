"""
test_ranker_shortlist.py — Tests for ranker.build_shortlist() and the
shortlist field that compare_one_item() now returns.

No real HTTP calls.  Store search functions are monkeypatched inline.

Design note: the shortlist is the set of up to 15 ranked alternatives a
client shows alongside the algorithmically-chosen winner so the user can
swap before ordering.  Winner + shortlist[0:2] = 3 always displayed; the
rest are revealed on demand.  Winner is always excluded from the shortlist.

Tier 1: passed relevance + reasonable-size filter (same pipeline as winner).
Tier 2: passed relevance, failed size filter (fallback filler).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ranker import build_shortlist, compare_one_item


# ── helpers ───────────────────────────────────────────────────────────────────

def _p(pid: str, name: str, unit: str, price: float,
       app: str = "bigbasket", app_name: str = "BigBasket") -> dict:
    return {
        "product_id": pid,
        "name": name,
        "unit": unit,
        "sale_price": price,
        "price": price,
        "image_url": "",
        "app": app,
        "app_name": app_name,
    }


# ── build_shortlist — basic exclusion / inclusion ─────────────────────────────

class TestBuildShortlistExclusion:
    def test_excludes_winner_by_pid_and_app(self):
        products = [
            _p("p1", "Tomato 500g", "500g", 30),
            _p("p2", "Tomato 1kg",  "1kg",  55),
        ]
        result = build_shortlist({"bigbasket": products},
                                 "tomato", "500g", "p1", "bigbasket")
        pids = [p["product_id"] for p in result]
        assert "p1" not in pids
        assert "p2" in pids

    def test_same_pid_different_store_not_excluded(self):
        """Winner is (p1, bigbasket); (p1, blinkit) is a different product."""
        products_bb = [_p("p1", "Tomato 500g", "500g", 30)]
        products_bl = [_p("p1", "Tomato 500g", "500g", 32, "blinkit", "Blinkit")]
        result = build_shortlist(
            {"bigbasket": products_bb, "blinkit": products_bl},
            "tomato", "500g", "p1", "bigbasket",
        )
        pids = [p["product_id"] for p in result]
        assert pids.count("p1") == 1          # blinkit's p1 is included

    def test_no_winner_includes_all_candidates(self):
        products = [
            _p("p1", "Tomato 500g", "500g", 30),
            _p("p2", "Tomato 1kg",  "1kg",  55),
        ]
        result = build_shortlist({"bigbasket": products},
                                 "tomato", "500g", None, None)
        pids = {p["product_id"] for p in result}
        assert pids == {"p1", "p2"}

    def test_empty_app_results_returns_empty(self):
        assert build_shortlist({}, "tomato", "500g", None, None) == []

    def test_store_with_empty_product_list_skipped(self):
        result = build_shortlist({"bigbasket": [], "blinkit": []},
                                 "tomato", "500g", None, None)
        assert result == []


# ── build_shortlist — n limit ─────────────────────────────────────────────────

class TestBuildShortlistLimit:
    def test_respects_n_limit(self):
        products = [_p(f"p{i}", f"Tomato {i*100}g", f"{i*100}g", float(i * 5))
                    for i in range(1, 25)]
        result = build_shortlist({"bigbasket": products},
                                 "tomato", "500g", None, None, n=5)
        assert len(result) <= 5

    def test_default_limit_is_15(self):
        products = [_p(f"p{i}", f"Tomato {i*100}g", f"{i*100}g", float(i * 5))
                    for i in range(1, 25)]
        result = build_shortlist({"bigbasket": products},
                                 "tomato", "500g", None, None)
        assert len(result) <= 15

    def test_fewer_than_n_products_returns_all(self):
        products = [
            _p("p1", "Tomato 500g", "500g", 30),
            _p("p2", "Tomato 1kg",  "1kg",  55),
        ]
        result = build_shortlist({"bigbasket": products},
                                 "tomato", "500g", "p1", "bigbasket", n=15)
        assert len(result) == 1   # only p2 left after excluding p1


# ── build_shortlist — tier ordering ──────────────────────────────────────────

class TestBuildShortlistTiers:
    def test_tier1_before_tier2(self):
        """Reasonable-sized products appear before oversized ones."""
        products = [
            _p("p0", "Tomato 500g",  "500g",  28),  # winner — excluded
            _p("p1", "Tomato 500g",  "500g",  30),  # tier 1: exact size
            _p("p2", "Tomato 5000g", "5000g", 100), # tier 2: too large
        ]
        result = build_shortlist({"bigbasket": products},
                                 "tomato", "500g", "p0", "bigbasket")
        assert result[0]["product_id"] == "p1"
        assert result[1]["product_id"] == "p2"

    def test_gen_pid_excluded_from_tier1(self):
        """Products with gen-* IDs are placeholder IDs and must be excluded."""
        products = [
            _p("gen-001", "Tomato 500g", "500g", 20),
            _p("p1",      "Tomato 500g", "500g", 30),
        ]
        result = build_shortlist({"bigbasket": products},
                                 "tomato", "500g", None, None)
        pids = [p["product_id"] for p in result]
        assert "gen-001" not in pids
        assert "p1" in pids

    def test_irrelevant_products_not_in_shortlist(self):
        """Products whose names don't match the query are excluded via relevance filter."""
        products = [
            _p("p1", "Tomato 500g", "500g", 30),
            _p("p2", "Onion 500g",  "500g", 25),  # irrelevant to "tomato" query
        ]
        result = build_shortlist({"bigbasket": products},
                                 "tomato 500g", "500g", None, None)
        pids = [p["product_id"] for p in result]
        assert "p1" in pids
        assert "p2" not in pids


# ── build_shortlist — sort order ──────────────────────────────────────────────

class TestBuildShortlistSortOrder:
    def test_exact_size_beats_wrong_size_in_tier1(self):
        """Within tier 1, qty_distance = 0 ranks before qty_distance > 0."""
        products = [
            _p("p0", "Tomato 500g", "500g", 25),  # winner
            _p("p1", "Tomato 500g", "500g", 30),  # exact size, slightly pricier
            _p("p2", "Tomato 1kg",  "1kg",  45),  # wrong size (dist > 0), cheaper ppu
        ]
        result = build_shortlist({"bigbasket": products},
                                 "tomato", "500g", "p0", "bigbasket")
        assert result[0]["product_id"] == "p1"  # exact size first

    def test_cheaper_ppu_ranks_first_when_same_distance(self):
        """When two products have the same qty_distance, cheaper per-unit wins."""
        products = [
            _p("p0", "Tomato 500g", "500g", 25),  # winner
            _p("p1", "Tomato 500g", "500g", 30),  # ₹30/500g = 0.06/g
            _p("p2", "Tomato 500g", "500g", 35),  # ₹35/500g = 0.07/g — more expensive
        ]
        result = build_shortlist({"bigbasket": products},
                                 "tomato", "500g", "p0", "bigbasket")
        assert result[0]["product_id"] == "p1"
        assert result[1]["product_id"] == "p2"

    def test_multi_store_sorted_correctly(self):
        """Products from multiple stores are ranked together by the same key."""
        products_bb = [_p("bb1", "Tomato 500g", "500g", 35,
                          "bigbasket", "BigBasket")]
        products_bl = [_p("bl1", "Tomato 500g", "500g", 28,
                          "blinkit", "Blinkit")]
        result = build_shortlist(
            {"bigbasket": products_bb, "blinkit": products_bl},
            "tomato", "500g", None, None,
        )
        # bl1 is cheaper per unit (exact same size) → should rank first
        assert result[0]["product_id"] == "bl1"
        assert result[1]["product_id"] == "bb1"


# ── build_shortlist — field preservation ─────────────────────────────────────

class TestBuildShortlistFields:
    def test_preserves_all_product_fields(self):
        product = {
            "product_id": "p1", "name": "Tomato 500g", "unit": "500g",
            "sale_price": 30.0, "price": 35.0, "image_url": "http://img",
            "app": "bigbasket", "app_name": "BigBasket",
        }
        result = build_shortlist({"bigbasket": [product]},
                                 "tomato", "500g", None, None)
        assert len(result) == 1
        r = result[0]
        for k, v in product.items():
            assert r[k] == v

    def test_deduplicates_by_pid_and_app(self):
        """Same product appearing twice (e.g. duplicate from store API) deduped."""
        p = _p("p1", "Tomato 500g", "500g", 30)
        result = build_shortlist({"bigbasket": [p, p]},
                                 "tomato", "500g", None, None)
        assert len(result) == 1


# ── compare_one_item integration: shortlist field ────────────────────────────

class TestCompareOneItemShortlist:
    @pytest.mark.asyncio
    async def test_shortlist_key_present(self, monkeypatch):
        from stores import bigbasket

        async def _search(user_id, query):
            return [
                _p("bb-001", "Amul Butter 100g", "100g", 55.0),
                _p("bb-002", "Amul Butter 500g", "500g", 250.0),
            ]

        monkeypatch.setattr(bigbasket, "search_item_api", _search)
        entry = await compare_one_item(
            {"name": "Amul Butter", "qty": "100g"},
            "u", ["bigbasket"],
        )
        assert "shortlist" in entry
        assert isinstance(entry["shortlist"], list)

    @pytest.mark.asyncio
    async def test_winner_not_in_shortlist(self, monkeypatch):
        from stores import bigbasket

        async def _search(user_id, query):
            return [
                _p("bb-001", "Amul Butter 100g", "100g", 55.0),
                _p("bb-002", "Amul Butter 500g", "500g", 250.0),
            ]

        monkeypatch.setattr(bigbasket, "search_item_api", _search)
        entry = await compare_one_item(
            {"name": "Amul Butter", "qty": "100g"},
            "u", ["bigbasket"],
        )
        winner_pid = entry["selected_pid"]
        shortlist_pids = [p["product_id"] for p in entry["shortlist"]]
        if winner_pid:
            assert winner_pid not in shortlist_pids

    @pytest.mark.asyncio
    async def test_shortlist_at_most_15(self, monkeypatch):
        from stores import bigbasket

        async def _search(user_id, query):
            # 24 products, all matching "amul butter" query
            return [
                _p(f"bb-{i:03d}", f"Amul Butter {i * 100}g",
                   f"{i * 100}g", float(i * 10))
                for i in range(1, 25)
            ]

        monkeypatch.setattr(bigbasket, "search_item_api", _search)
        entry = await compare_one_item(
            {"name": "Amul Butter", "qty": "500g"},
            "u", ["bigbasket"],
        )
        assert len(entry["shortlist"]) <= 15

    @pytest.mark.asyncio
    async def test_shortlist_multi_store(self, monkeypatch):
        """Shortlist contains alternatives from all stores, not just one."""
        from stores import bigbasket, blinkit, zepto

        async def _bb(user_id, query):
            return [_p("bb-001", "Amul Butter 100g", "100g", 55.0)]

        async def _bl(user_id, query):
            return [_p("bl-001", "Amul Butter 100g", "100g", 58.0,
                       "blinkit", "Blinkit")]

        async def _z(user_id, query):
            return [_p("z-001", "Amul Butter 100g", "100g", 52.0,
                       "zepto", "Zepto")]

        monkeypatch.setattr(bigbasket, "search_item_api", _bb)
        monkeypatch.setattr(blinkit,   "search_item_api", _bl)
        monkeypatch.setattr(zepto,     "search_item_api", _z)

        entry = await compare_one_item(
            {"name": "Amul Butter", "qty": "100g"},
            "u", ["bigbasket", "blinkit", "zepto"],
        )
        shortlist_apps = {p["app"] for p in entry["shortlist"]}
        # At least one non-winner store must appear in shortlist
        assert len(shortlist_apps) >= 1
        # Total alternatives = 3 products - 1 winner = 2
        assert len(entry["shortlist"]) == 2

    @pytest.mark.asyncio
    async def test_shortlist_empty_when_no_results(self, monkeypatch):
        """No search results → no shortlist."""
        from stores import bigbasket

        async def _search(user_id, query):
            return []

        monkeypatch.setattr(bigbasket, "search_item_api", _search)
        entry = await compare_one_item(
            {"name": "Amul Butter", "qty": "100g"},
            "u", ["bigbasket"],
        )
        assert entry["shortlist"] == []

    @pytest.mark.asyncio
    async def test_shortlist_present_even_when_no_winner(self, monkeypatch):
        """Even if no store wins (e.g. no relevant results), shortlist is a list."""
        from stores import bigbasket

        async def _search(user_id, query):
            # Return products that don't pass relevance for "amul butter" query
            # "Tata Salt" won't match → no winner, empty shortlist
            return [_p("x-001", "Tata Salt 1kg", "1kg", 20.0)]

        monkeypatch.setattr(bigbasket, "search_item_api", _search)
        entry = await compare_one_item(
            {"name": "Amul Butter", "qty": "100g"},
            "u", ["bigbasket"],
        )
        assert isinstance(entry["shortlist"], list)
        assert entry["cheapest_app"] is None
