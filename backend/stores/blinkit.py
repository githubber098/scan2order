"""stores/blinkit.py - Blinkit httpx store module.

Search via __NEXT_DATA__ SSR extraction (same strategy as scan2order2/automators/blinkit.py
Strategy 1, implemented as a pure httpx GET instead of Playwright page.evaluate).

Cart add via Blinkit's internal v2 API. Auth cookie: gr_1_accessToken.
Location cookies (lat, lng, merchant_id) extracted from stored cookies.

NOTE: The cart API endpoint and request format were reverse-engineered from
known Blinkit API patterns. Test against a real session before relying on it.
"""

import re
import json
import time
from urllib.parse import quote

import httpx

from storage.user_store import get_store_cookies

APP_NAME = "blinkit"
DISPLAY_NAME = "Blinkit"
BASE_URL = "https://blinkit.com"

_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36"
)

_SEARCH_HEADERS = {
    "User-Agent": _MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def is_session_valid(user_id: str) -> bool:
    cookies = get_store_cookies(user_id, APP_NAME)
    return bool(cookies.get("gr_1_accessToken"))


def _extract_next_data(html: str) -> dict | None:
    """Extract and parse the __NEXT_DATA__ JSON from Blinkit's SSR HTML."""
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


async def search_item_api(user_id: str, query: str) -> list[dict]:
    """Search via Blinkit SSR __NEXT_DATA__ parsing.

    GET https://blinkit.com/s/?q={query} with auth cookies,
    then extract the product list from the embedded Next.js JSON.
    Returns [] on any failure.
    """
    cookies = get_store_cookies(user_id, APP_NAME)
    if not cookies.get("gr_1_accessToken"):
        print(f"[blinkit] search_item_api: no gr_1_accessToken for user {user_id[:8]}")
        return []

    url = f"{BASE_URL}/s/?q={quote(query)}"
    print(f"\n[blinkit] === API SEARCH: '{query}' ===")
    print(f"[blinkit] URL: {url}")
    t_start = time.time()

    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True
        ) as client:
            resp = await client.get(url, headers=_SEARCH_HEADERS, cookies=cookies)
        elapsed_ms = int((time.time() - t_start) * 1000)

        if resp.status_code != 200:
            print(f"[blinkit] API search HTTP {resp.status_code} ({elapsed_ms}ms)")
            return []
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[blinkit] API search failed after {elapsed_ms}ms: {e}")
        return []

    data = _extract_next_data(resp.text)
    if not data:
        print(f"[blinkit] __NEXT_DATA__ not found in response ({elapsed_ms}ms)")
        return []

    page_props = (data.get("props") or {}).get("pageProps") or {}

    # Same candidate paths as scan2order2 Strategy 1
    product_list = None
    for candidate in [
        (page_props.get("searchResult") or {}).get("products"),
        page_props.get("products"),
        (page_props.get("search") or {}).get("products"),
        (page_props.get("initialData") or {}).get("products"),
    ]:
        if isinstance(candidate, list) and candidate:
            product_list = candidate
            break

    if not product_list:
        page_keys = list(page_props.keys())[:10]
        print(f"[blinkit] no products in __NEXT_DATA__. pageProps keys: {page_keys}")
        return []

    products = []
    for p in product_list:
        name = p.get("name") or p.get("product_name") or ""
        if not name:
            continue

        # Skip OOS
        if p.get("is_out_of_stock") or p.get("outOfStock"):
            continue

        sale = float(p.get("offer_price") or p.get("selling_price") or p.get("price") or 0)
        mrp = float(p.get("mrp") or p.get("price") or sale)
        if sale <= 0:
            continue

        product_id = str(p.get("id") or p.get("product_id") or p.get("group_id") or "")
        if not product_id:
            continue

        products.append({
            "name": name[:120],
            "price": mrp,
            "sale_price": sale,
            "unit": p.get("unit") or p.get("quantity") or "",
            "image_url": p.get("image_url") or p.get("thumbnail") or "",
            "product_id": product_id,
            "app": APP_NAME,
            "app_name": DISPLAY_NAME,
        })
        if len(products) >= 8:
            break

    elapsed_ms = int((time.time() - t_start) * 1000)
    print(f"[blinkit] === API RESULT: {len(products)} products ({elapsed_ms}ms) ===\n")
    return products


async def add_to_cart_api(user_id: str, product_id: str, count: int = 1) -> dict:
    """Add to Blinkit cart via internal v2 API.

    Uses gr_1_accessToken for auth and lat/lng/merchant_id from stored cookies.
    Returns {"success": True, "count_added": N} or {"success": False, "reason": str}.
    """
    cookies = get_store_cookies(user_id, APP_NAME)
    access_token = cookies.get("gr_1_accessToken", "")
    if not access_token:
        return {"success": False, "reason": "not logged in (no gr_1_accessToken)"}

    # Location context from cookies - set when user saves delivery address in app.
    lat = cookies.get("lat") or cookies.get("dlat") or cookies.get("delivery_lat") or ""
    lng = cookies.get("lng") or cookies.get("dlng") or cookies.get("delivery_lng") or ""
    merchant_id = cookies.get("merchant_id") or cookies.get("gr_1_merchantId") or ""

    print(f"\n[blinkit] === API ADD: pid={product_id} qty={count} ===")
    t_start = time.time()

    headers = {
        "User-Agent": _MOBILE_UA,
        "auth_key": access_token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "app_client": "web",
        "web_app_version": "3000",
    }
    if lat:
        headers["lat"] = str(lat)
    if lng:
        headers["lng"] = str(lng)
    if merchant_id:
        headers["merchant_id"] = str(merchant_id)

    body = {
        "items": [{"product_id": int(product_id), "quantity": count}],
        "order_type": "blinkIt",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BASE_URL}/v2/client/user_cart/",
                json=body, headers=headers,
            )
        elapsed_ms = int((time.time() - t_start) * 1000)

        if resp.status_code != 200:
            print(f"[blinkit] API add HTTP {resp.status_code} ({elapsed_ms}ms)")
            return {"success": False, "reason": f"HTTP {resp.status_code}"}

        print(f"[blinkit] API add OK pid={product_id} ({elapsed_ms}ms)")
        return {"success": True, "count_added": count}
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[blinkit] API add failed after {elapsed_ms}ms: {e}")
        return {"success": False, "reason": f"exception: {e}"}


def checkout_url() -> str:
    return f"{BASE_URL}/cart"
