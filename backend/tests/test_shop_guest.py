"""
test_shop_guest.py — Shop tab + Guest-mode tests.

Covers the features added in v1.2.0:
  - GET /shop and guest GET /        → render (200) without a session
  - POST /api/shop/search            → merged, deduped, sorted cheapest-ppu first
  - POST /api/shop/add               → login-gated (403 for guests), adds when authed
  - POST /api/compare (guest)        → cheapest-only, no price matrix, no history
  - _ppu_label / _guest_strip_entry  → unit helpers

Authenticated requests set the real session cookie (auth.create_session_token)
so the backend's session-based guest enforcement is exercised end-to-end.
"""

import auth
import server
from server import _ppu_label, _guest_strip_entry


def _login(client, user_id):
    """Attach a valid web-session cookie for *user_id* to the test client."""
    client.cookies.set(auth.COOKIE_NAME, auth.create_session_token(user_id))


# ── Page rendering (guest) ────────────────────────────────────────────────────

class TestGuestPages:
    def test_shop_page_renders_for_guest(self, client):
        r = client.get("/shop")
        assert r.status_code == 200
        assert "shop-q" in r.text                 # search box present
        assert "_SERVER_GUEST   = true" in r.text  # guest flag injected

    def test_home_renders_for_guest_no_redirect(self, client):
        # Guests get the Compare page directly (not a redirect to /login).
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 200
        assert "_SERVER_GUEST   = true" in r.text

    def test_cart_page_renders(self, client):
        # The Cart tab must render its own page (empty state), NOT redirect.
        r = client.get("/cart", follow_redirects=False)
        assert r.status_code == 200
        assert "cart-empty" in r.text and "Your cart is empty" in r.text

    def test_tab_order_compare_shop_cart(self, client):
        body = client.get("/shop").text
        i_c = body.find('data-nav="compare"')
        i_s = body.find('data-nav="shop"')
        i_k = body.find('data-nav="cart"')
        assert 0 < i_c < i_s < i_k   # Compare, Shop, Cart in that order

    def test_home_is_not_guest_when_authed(self, client, connected_user_all):
        _login(client, connected_user_all)
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 200
        # A logged-in session must NOT be flagged as a guest.
        assert "_SERVER_GUEST   = false" in r.text


# ── Shop search ────────────────────────────────────────────────────────────────

class TestShopSearch:
    def test_search_merges_and_sorts_by_ppu(self, client, mock_stores, connected_user_all):
        _login(client, connected_user_all)
        r = client.post("/api/shop/search", json={"query": "amul butter"})
        assert r.status_code == 200
        data = r.json()
        assert data["can_add"] is True
        assert data["is_guest"] is False
        prods = data["products"]
        # 2 BB + 1 BL + 2 Z + 1 FM = 6 mocked "amul butter" products
        assert len(prods) == 6
        # Sorted ascending by price-per-unit (cheapest per gram first).
        ppus = [p["price_per_unit"] for p in prods if p["price_per_unit"] is not None]
        assert ppus == sorted(ppus)
        # Cheapest per gram is Zepto 500g @ ₹245 → 0.49/g
        assert prods[0]["price_per_unit"] <= prods[1]["price_per_unit"]
        assert prods[0]["sale_price"] == 245.0

    def test_search_missing_query(self, client, connected_user_all):
        _login(client, connected_user_all)
        r = client.post("/api/shop/search", json={"query": "  "})
        assert r.json()["error"] == "missing query"

    def test_guest_search_without_backing_account_has_no_prices(self, client, mock_stores):
        # No session, GUEST_STORE_USER_ID unset → browse UI but no live prices.
        r = client.post("/api/shop/search", json={"query": "amul butter"})
        data = r.json()
        assert data["is_guest"] is True
        assert data["can_add"] is False
        assert data["products"] == []
        assert "note" in data

    def test_guest_search_with_backing_account_returns_prices_but_no_add(
        self, client, mock_stores, connected_user_all, monkeypatch
    ):
        # Owner opts in: guests borrow this account's sessions for READ-ONLY prices.
        monkeypatch.setenv("GUEST_STORE_USER_ID", connected_user_all)
        r = client.post("/api/shop/search", json={"query": "amul butter"})
        data = r.json()
        assert data["is_guest"] is True
        assert data["can_add"] is False          # still cannot add to cart
        assert len(data["products"]) == 6


# ── Shop add (login-gated) ──────────────────────────────────────────────────────

class TestShopAdd:
    def test_guest_add_is_blocked_403(self, client, mock_stores):
        r = client.post("/api/shop/add",
                         json={"app": "zepto",
                               "items": [{"product_id": "z-001", "count": 1}]})
        assert r.status_code == 403
        assert r.json()["error"] == "login_required"

    def test_authed_add_succeeds(self, client, mock_stores, connected_user_all):
        _login(client, connected_user_all)
        r = client.post("/api/shop/add",
                        json={"app": "zepto",
                              "items": [{"product_id": "z-001", "count": 2}]})
        assert r.status_code == 200
        result = r.json()["result"]
        assert len(result["added"]) == 1
        assert result["added"][0]["count_added"] == 2
        mock_stores.z_cart.assert_awaited()

    def test_add_unknown_store(self, client, connected_user_all):
        _login(client, connected_user_all)
        r = client.post("/api/shop/add",
                        json={"app": "nosuchstore", "items": [{"product_id": "x123"}]})
        assert "unknown store" in r.json()["error"]

    def test_blinkit_add_sends_full_list_in_one_batch(self, client, mock_stores, connected_user_all):
        # Blinkit /v5/carts replaces the cart, so the whole per-app list must be
        # sent in ONE batch call — never one call per item.
        _login(client, connected_user_all)
        items = [{"product_id": "bl-001", "count": 1},
                 {"product_id": "bl-777", "count": 3}]
        r = client.post("/api/shop/add", json={"app": "blinkit", "items": items})
        assert r.status_code == 200
        assert mock_stores.bl_cart_all.await_count == 1
        sent = mock_stores.bl_cart_all.await_args.args[1]
        assert len(sent) == 2


# ── Trending ───────────────────────────────────────────────────────────────────

class TestShopTrending:
    def test_trending_returns_cards_when_authed(self, client, mock_stores,
                                                connected_user_all, monkeypatch):
        server._trending_cache.clear()
        # Point trending at the one query the store mocks actually match.
        monkeypatch.setattr(server, "_TRENDING_QUERIES", ["amul butter"])
        _login(client, connected_user_all)
        r = client.get("/api/shop/trending")
        assert r.status_code == 200
        data = r.json()
        assert data["is_guest"] is False and data["can_add"] is True
        assert len(data["products"]) >= 1
        assert "price_per_unit" in data["products"][0]

    def test_trending_empty_for_guest_without_backing(self, client, mock_stores):
        server._trending_cache.clear()
        r = client.get("/api/shop/trending")
        data = r.json()
        assert data["is_guest"] is True
        assert data["can_add"] is False
        assert data["products"] == []


# ── Compare guest restriction ────────────────────────────────────────────────────

class TestCompareGuestRestriction:
    def test_guest_compare_returns_cheapest_only(
        self, client, mock_stores, connected_user_all, monkeypatch
    ):
        monkeypatch.setenv("GUEST_STORE_USER_ID", connected_user_all)
        r = client.post("/api/compare", json={"items": [{"name": "amul butter"}]})
        assert r.status_code == 200
        data = r.json()
        assert data["is_guest"] is True
        # Full comparison data must NOT leak to guests.
        assert "carts" not in data
        assert "savings" not in data
        entry = data["comparison"][0]
        assert "prices" not in entry        # per-store matrix stripped
        assert "shortlist" not in entry     # swap list stripped
        assert "cheapest_product" in entry  # only the winning pick exposed

    def test_guest_compare_not_saved_to_history(
        self, client, mock_stores, connected_user_all, monkeypatch
    ):
        from storage import user_store
        monkeypatch.setenv("GUEST_STORE_USER_ID", connected_user_all)
        client.post("/api/compare", json={"items": [{"name": "amul butter"}]})
        # Guests have no account → nothing persisted under the backing user.
        assert user_store.get_history(connected_user_all) == []

    def test_authed_compare_keeps_full_data(self, client, mock_stores, connected_user_all):
        _login(client, connected_user_all)
        r = client.post("/api/compare", json={"items": [{"name": "amul butter"}]})
        data = r.json()
        assert data["is_guest"] is False
        assert "carts" in data
        assert "prices" in data["comparison"][0]


# ── Unit helpers ─────────────────────────────────────────────────────────────────

class TestPpuLabel:
    def test_mass_label(self):
        assert _ppu_label({"sale_price": 50, "unit": "100g"}) == "₹50.0/100g"

    def test_volume_label(self):
        assert _ppu_label({"sale_price": 60, "unit": "1 l"}) == "₹6.0/100ml"

    def test_count_label(self):
        assert _ppu_label({"sale_price": 60, "unit": "6 pcs"}) == "₹10.0/pc"

    def test_no_size_is_empty(self):
        assert _ppu_label({"sale_price": 50, "unit": ""}) == ""


class TestGuestStripEntry:
    def test_strips_prices_and_shortlist(self):
        entry = {
            "item": {"name": "milk"},
            "search_query": "milk",
            "cheapest_app": "zepto",
            "cheapest_price": 25.0,
            "cheapest_effective_price": 25.0,
            "qty_count": 1,
            "selected_pid": "z-1",
            "prices": {"zepto": [{"product_id": "z-1", "name": "Milk 500ml",
                                  "sale_price": 25.0, "unit": "500ml",
                                  "app": "zepto", "app_name": "Zepto"}]},
            "shortlist": [{"product_id": "b-1"}],
        }
        stripped = _guest_strip_entry(entry)
        assert "prices" not in stripped
        assert "shortlist" not in stripped
        assert stripped["cheapest_product"]["name"] == "Milk 500ml"
        assert stripped["cheapest_app"] == "zepto"
