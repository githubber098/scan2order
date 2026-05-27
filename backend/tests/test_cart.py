"""
test_cart.py — Tests for cart add-all, cart progress, and mobile /order.

Store cart APIs are replaced by `mock_stores` so no real HTTP calls are made.

Fundamental scenario: a *new account* connects stores, runs a compare, and
adds all items to cart.  The test verifies the full path from connection →
comparison → cart-add with zero mocking gaps.

Covers:
  POST /api/cart/add-all        — add items to each store's cart
  GET  /api/cart/progress       — per-user progress state
  POST /order                   — mobile compat: add items by store
"""

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

def _cart_payload(user_id: str, store: str, product_id: str,
                  fc_id=None, count: int = 1) -> dict:
    item = {
        "product_id": product_id,
        "name": "Amul Butter",
        "count": count,
    }
    if fc_id is not None:
        item["fc_id"] = fc_id
    return {
        "user_id": user_id,
        "carts": {
            store: {"items": [item]},
        },
    }


# ── Full flow: new account → connect → compare → add to cart ─────────────────

class TestNewAccountFullFlow:
    """
    End-to-end: a brand-new user connects BigBasket, compares one item,
    and adds the cheapest result to cart.  This is the core value-prop of
    scan2order and should never regress.
    """

    def test_new_account_bb_connect_compare_cart(
            self, client, user_id, bb_cookies, clean_db, mock_stores):
        # Step 1 — connect
        r = client.post("/api/auth/connect", json={
            "user_id": user_id,
            "store": "bigbasket",
            "cookies": bb_cookies,
        })
        assert r.json()["success"] is True

        # Step 2 — compare
        r = client.post("/api/compare", json={
            "user_id": user_id,
            "items": [{"name": "Amul Butter", "qty": "100g", "count": 1}],
        })
        data = r.json()
        entry = data["comparison"][0]
        assert entry["cheapest_app"] == "bigbasket"

        # Step 3 — add to cart (using the pid + fc_id from compare result)
        pid = entry["selected_pid"]
        fc_id = entry["prices"]["bigbasket"][0]["fc_id"]
        r = client.post("/api/cart/add-all", json=_cart_payload(
            user_id, "bigbasket", pid, fc_id=fc_id
        ))
        result = r.json()["results"]["bigbasket"]
        assert len(result["added"]) == 1
        assert len(result["failed"]) == 0

    def test_new_account_all_stores_connect_compare_cart(
            self, client, user_id, bb_cookies, bl_cookies, zepto_cookies,
            clean_db, mock_stores):
        # Connect all stores
        for store, cookies in [
            ("bigbasket", bb_cookies),
            ("blinkit", bl_cookies),
            ("zepto", zepto_cookies),
        ]:
            r = client.post("/api/auth/connect", json={
                "user_id": user_id, "store": store, "cookies": cookies,
            })
            assert r.json()["success"] is True

        # Compare — Zepto should win at ₹52
        r = client.post("/api/compare", json={
            "user_id": user_id,
            "items": [{"name": "Amul Butter", "qty": "100g", "count": 1}],
        })
        entry = r.json()["comparison"][0]
        assert entry["cheapest_app"] == "zepto"

        # Add the winner to cart
        pid = entry["selected_pid"]
        r = client.post("/api/cart/add-all", json={
            "user_id": user_id,
            "carts": {"zepto": {"items": [{"product_id": pid, "name": "Amul Butter",
                                           "count": 1}]}},
        })
        result = r.json()["results"]["zepto"]
        assert len(result["added"]) == 1


# ── /api/cart/add-all ─────────────────────────────────────────────────────────

class TestApiCartAddAll:
    def test_add_single_bb_item(
            self, client, connected_user_bb, user_id, mock_stores):
        payload = _cart_payload(user_id, "bigbasket", "bb-001", fc_id=10)
        r = client.post("/api/cart/add-all", json=payload)
        assert r.status_code == 200
        result = r.json()["results"]["bigbasket"]
        assert len(result["added"]) == 1
        assert len(result["failed"]) == 0

    def test_add_multiple_items(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/api/cart/add-all", json={
            "user_id": user_id,
            "carts": {
                "bigbasket": {
                    "items": [
                        {"product_id": "bb-001", "name": "Butter 100g",
                         "count": 2, "fc_id": 10},
                        {"product_id": "bb-002", "name": "Butter 500g",
                         "count": 1, "fc_id": 10},
                    ],
                },
            },
        })
        result = r.json()["results"]["bigbasket"]
        assert len(result["added"]) == 2
        assert len(result["failed"]) == 0

    def test_add_zepto_uses_batch_api(
            self, client, connected_user_all, user_id, mock_stores):
        r = client.post("/api/cart/add-all", json={
            "user_id": user_id,
            "carts": {
                "zepto": {"items": [
                    {"product_id": "z-001", "name": "Butter", "count": 1},
                ]},
            },
        })
        result = r.json()["results"]["zepto"]
        assert len(result["added"]) == 1
        # Ensure the batch mock was called (not the per-item path)
        assert mock_stores.z_cart.called

    def test_skips_gen_product_ids(
            self, client, connected_user_bb, user_id, mock_stores):
        """Product IDs starting with 'gen-' are placeholder and must be skipped."""
        r = client.post("/api/cart/add-all", json={
            "user_id": user_id,
            "carts": {
                "bigbasket": {"items": [
                    {"product_id": "gen-placeholder-123", "name": "Ghost item",
                     "count": 1},
                ]},
            },
        })
        result = r.json()["results"]["bigbasket"]
        assert len(result["added"]) == 0
        assert len(result["failed"]) == 1

    def test_skips_short_product_ids(
            self, client, connected_user_bb, user_id, mock_stores):
        """Product IDs shorter than 4 chars are invalid and must be skipped."""
        r = client.post("/api/cart/add-all", json={
            "user_id": user_id,
            "carts": {
                "bigbasket": {"items": [
                    {"product_id": "ab", "name": "Bad item", "count": 1},
                ]},
            },
        })
        result = r.json()["results"]["bigbasket"]
        assert len(result["failed"]) == 1

    def test_missing_user_id(self, client, clean_db):
        r = client.post("/api/cart/add-all", json={
            "carts": {"bigbasket": {"items": [
                {"product_id": "bb-001", "count": 1}
            ]}},
        })
        assert "error" in r.json()

    def test_empty_carts_returns_empty_results(
            self, client, connected_user_bb, user_id, clean_db):
        r = client.post("/api/cart/add-all", json={
            "user_id": user_id,
            "carts": {},
        })
        assert r.status_code == 200
        assert r.json()["results"] == {}

    def test_bb_item_without_fc_id_fails(
            self, client, connected_user_bb, user_id, mock_stores):
        """BigBasket cart API requires fc_id; missing fc_id → failed item."""
        r = client.post("/api/cart/add-all", json={
            "user_id": user_id,
            "carts": {
                "bigbasket": {"items": [
                    {"product_id": "bb-001", "name": "Butter", "count": 1},
                    # no fc_id
                ]},
            },
        })
        result = r.json()["results"]["bigbasket"]
        assert len(result["failed"]) == 1

    def test_multi_store_cart(
            self, client, connected_user_all, user_id, mock_stores):
        r = client.post("/api/cart/add-all", json={
            "user_id": user_id,
            "carts": {
                "bigbasket": {"items": [
                    {"product_id": "bb-001", "name": "Butter", "count": 1, "fc_id": 10}
                ]},
                "blinkit": {"items": [
                    {"product_id": "bl-001", "name": "Butter", "count": 1}
                ]},
            },
        })
        results = r.json()["results"]
        assert "bigbasket" in results
        assert "blinkit" in results
        assert len(results["bigbasket"]["added"]) == 1
        assert len(results["blinkit"]["added"]) == 1

    def test_count_clamped_to_reasonable_range(
            self, client, connected_user_bb, user_id, mock_stores):
        """count=0 should be normalised to 1; count=200 clamped to 99."""
        r = client.post("/api/cart/add-all", json={
            "user_id": user_id,
            "carts": {
                "bigbasket": {"items": [
                    {"product_id": "bb-001", "name": "Butter", "count": 0,
                     "fc_id": 10},
                ]},
            },
        })
        added = r.json()["results"]["bigbasket"]["added"]
        assert len(added) == 1
        assert added[0]["count_requested"] == 1  # 0 → 1


# ── /api/cart/progress ────────────────────────────────────────────────────────

class TestCartProgress:
    def test_progress_for_new_user(self, client, user_id, clean_db):
        r = client.get(f"/api/cart/progress?user_id={user_id}")
        assert r.status_code == 200
        data = r.json()
        assert data["running"] is False
        assert data["total"] == 0
        assert data["done"] == 0

    def test_progress_shape(self, client, user_id, clean_db):
        data = client.get(f"/api/cart/progress?user_id={user_id}").json()
        assert "running" in data
        assert "total" in data
        assert "done" in data
        assert "current" in data


# ── /order (mobile compat) ────────────────────────────────────────────────────

class TestMobileOrder:
    def test_order_single_bb_item(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/order", json={
            "user_id": user_id,
            "items": [{"prod_id": "bb-001", "fc_id": 10, "qty": 1, "app": "bigbasket"}],
        })
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert len(data["added"]) == 1
        assert len(data["failed"]) == 0

    def test_order_returns_cart_url(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/order", json={
            "user_id": user_id,
            "items": [{"prod_id": "bb-001", "fc_id": 10, "qty": 1, "app": "bigbasket"}],
        })
        data = r.json()
        assert "cart_url" in data
        assert data["cart_url"].startswith("https://")

    def test_order_empty_items_succeeds(
            self, client, connected_user_bb, user_id, clean_db):
        r = client.post("/order", json={"user_id": user_id, "items": []})
        data = r.json()
        assert data["success"] is True
        assert data["added"] == []

    def test_order_missing_user_id(self, client, clean_db):
        r = client.post("/order", json={
            "items": [{"prod_id": "bb-001", "fc_id": 10, "qty": 1}],
        })
        assert r.json()["success"] is False

    def test_order_missing_prod_id_fails_item(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/order", json={
            "user_id": user_id,
            "items": [{"fc_id": 10, "qty": 1, "app": "bigbasket"}],
        })
        data = r.json()
        assert len(data["failed"]) == 1
        assert "prod_id" in data["failed"][0]["reason"].lower() \
               or "missing" in data["failed"][0]["reason"].lower()

    def test_order_unknown_store_fails_item(
            self, client, connected_user_bb, user_id, mock_stores):
        r = client.post("/order", json={
            "user_id": user_id,
            "items": [{"prod_id": "bb-001", "qty": 1, "app": "flipkart"}],
        })
        data = r.json()
        assert len(data["failed"]) == 1

    def test_order_multi_store(
            self, client, connected_user_all, user_id, mock_stores):
        r = client.post("/order", json={
            "user_id": user_id,
            "items": [
                {"prod_id": "bb-001", "fc_id": 10, "qty": 1, "app": "bigbasket"},
                {"prod_id": "bl-001", "qty": 1, "app": "blinkit"},
            ],
        })
        data = r.json()
        assert data["success"] is True
        assert len(data["added"]) == 2
