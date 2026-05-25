"""backend/stores/blinkit.py - Blinkit API client.

Stateless httpx-based functions. No Playwright, no browser context.

Blinkit search is UNAUTHENTICATED - auth_key is static, no login needed.
Cart operations do require auth cookies (gr_1_deviceId, city, etc.) from
the mobile app login flow.

Public API:
    search_item_api(query, lat, lon, cookies=None) -> list[dict]
        Search Blinkit products. lat/lon required for location-aware results.

    add_to_cart_api(product_id, merchant_id, cart_item, cookies) -> dict
        Add a single item to cart. cart_item is the full payload from search.
"""

import json
import uuid

import httpx

_SEARCH_URL = "https://blinkit.com/v1/layout/search"

# Static auth_key captured from live session (2026-05). Safe to hardcode -
# it does not change per-user and search is unauthenticated.
_AUTH_KEY = "c761ec3633c22afad934fb17a66385c1c06c5472b4898b866b7306186d0bb477"

_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36"
)

# TODO: Capture the Blinkit cart API endpoint from DevTools.
# The search response embeds the full cart payload in each product's
# data.atc_action.add_to_cart.cart_item. This strongly suggests the cart
# endpoint accepts that payload directly. Best-guess endpoint based on
# Blinkit's API patterns. Update once verified via live DevTools capture.
_CART_URL_GUESS = "https://blinkit.com/v1/cart/add"


def _search_headers(lat: str, lon: str, cookies: dict | None = None) -> dict:
    """Build headers for Blinkit search request."""
    device_id = (cookies or {}).get("gr_1_deviceId", str(uuid.uuid4())[:16])
    return {
        "access_token": "null",
        "app_client": "consumer_web",
        "app_version": "1010101010",
        "auth_key": _AUTH_KEY,
        "content-type": "application/json",
        "device_id": device_id,
        "lat": str(lat),
        "lon": str(lon),
        "rn_bundle_version": "1009003012",
        "session_uuid": str(uuid.uuid4()),
        "user-agent": _MOBILE_UA,
        "web_app_version": "1008010016",
        "accept": "application/json",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "origin": "https://blinkit.com",
        "referer": "https://blinkit.com/",
    }


def _cookie_header(cookies: dict | None) -> str:
    """Build Cookie header string from cookie dict."""
    if not cookies:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


async def search_item_api(
    query: str,
    lat: str,
    lon: str,
    cookies: dict | None = None,
) -> list[dict]:
    """Search Blinkit for products matching query at a given location.

    Args:
        query: search term (e.g. "tomato")
        lat: latitude string (e.g. "12.9942483")
        lon: longitude string (e.g. "77.7307156")
        cookies: optional Blinkit cookies from Redis (for gr_1_deviceId, city, etc.)

    Returns:
        List of product dicts, each with:
            name, unit, price, sale_price, image_url, product_id,
            merchant_id, group_id, _cart_item (full cart payload)
        Returns [] on failure.
    """
    if not lat or not lon:
        print("[blinkit] search_item_api: lat/lon not set - cannot search")
        return []

    print(f"\n[blinkit] === API SEARCH: '{query}' @ ({lat}, {lon}) ===")

    headers = _search_headers(lat, lon, cookies)
    if cookies:
        headers["cookie"] = _cookie_header(cookies)

    params = {"q": query, "search_type": "auto_suggest"}
    body = {}  # POST with empty body

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _SEARCH_URL,
                params=params,
                json=body,
                headers=headers,
            )

        if resp.status_code != 200:
            print(f"[blinkit] API search HTTP {resp.status_code}")
            return []

        data = resp.json()
    except Exception as e:
        print(f"[blinkit] API search exception: {e}")
        return []

    snippets = (data.get("response") or {}).get("snippets") or []
    products: list[dict] = []
    seen_groups: set[str] = set()

    for snippet in snippets:
        if snippet.get("widget_type") != "product_card_snippet_type_2":
            continue

        d = snippet.get("data") or {}

        # Skip unavailable / out-of-stock
        if d.get("is_sold_out"):
            continue
        if d.get("product_state") and d["product_state"] != "available":
            continue

        atc = (d.get("atc_action") or {}).get("add_to_cart") or {}
        cart_item = atc.get("cart_item") or {}
        if not cart_item:
            continue

        # Deduplicate by group_id (same product in different pack sizes shows
        # as separate snippets but should be merged into one result).
        group_id = str(cart_item.get("group_id") or "")
        if group_id and group_id in seen_groups:
            continue
        if group_id:
            seen_groups.add(group_id)

        try:
            mrp = float(cart_item.get("mrp") or 0)
            sale_price = float(cart_item.get("price") or mrp)
        except (TypeError, ValueError):
            mrp, sale_price = 0.0, 0.0

        products.append({
            "name": (d.get("name") or {}).get("text", "")[:120],
            "unit": (d.get("variant") or {}).get("text", ""),
            "price": mrp,
            "sale_price": sale_price,
            "image_url": (d.get("image") or {}).get("url", ""),
            "product_id": str(cart_item.get("product_id") or ""),
            "merchant_id": str(cart_item.get("merchant_id") or ""),
            "group_id": group_id,
            "_cart_item": cart_item,  # full payload for add-to-cart
            "app": "blinkit",
            "app_name": "Blinkit",
        })
        if len(products) >= 8:
            break

    print(f"[blinkit] === API RESULT: {len(products)} products ===\n")
    return products


async def add_to_cart_api(
    product_id: str,
    merchant_id: str,
    cart_item: dict,
    cookies: dict | None = None,
    lat: str = "",
    lon: str = "",
) -> dict:
    """Add a Blinkit product to cart.

    TODO: Capture the real Blinkit cart endpoint from DevTools. The
    _cart_item payload from search is used here directly as the body.
    The endpoint URL below (_CART_URL_GUESS) is a best guess and needs
    to be verified against a live captured request.

    Args:
        product_id: from search result
        merchant_id: from search result
        cart_item: full _cart_item dict from search result
        cookies: Blinkit cookies from Redis
        lat/lon: user's location

    Returns:
        {"success": True, "count_added": 1}  on success
        {"success": False, "reason": "..."}  on failure
    """
    if not cookies:
        return {"success": False, "reason": "Blinkit cookies not connected"}
    if not lat or not lon:
        return {"success": False, "reason": "Blinkit location (lat/lon) not set"}

    print(f"\n[blinkit] === API ADD: pid={product_id} ===")
    print(f"[blinkit] NOTE: Cart endpoint is unverified ({_CART_URL_GUESS}). "
          f"Capture a live DevTools add-to-cart request to verify.")

    headers = _search_headers(lat, lon, cookies)
    if cookies:
        headers["cookie"] = _cookie_header(cookies)

    body = {
        "cart_item": cart_item,
        "quantity": 1,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _CART_URL_GUESS,
                json=body,
                headers=headers,
            )

        if resp.status_code == 200:
            print(f"[blinkit] API add OK pid={product_id}")
            return {"success": True, "count_added": 1}
        else:
            print(f"[blinkit] API add HTTP {resp.status_code}: {resp.text[:200]}")
            return {
                "success": False,
                "reason": (
                    f"HTTP {resp.status_code} - cart endpoint may need updating. "
                    f"Capture a live DevTools add-to-cart request to fix."
                ),
            }
    except Exception as e:
        print(f"[blinkit] API add exception: {e}")
        return {"success": False, "reason": f"exception: {e}"}
