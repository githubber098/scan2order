"""backend/stores/bigbasket.py - BigBasket API client.

Stateless httpx-based functions. No Playwright, no browser context.
Cookies come from Redis (passed as plain dicts by the caller).

Public API:
    search_item_api(query, cookies) -> list[dict]
    add_to_cart_api(product_id, query, count, fc_id, cookies) -> dict
    get_location_cookies(city) -> dict   # {"_bb_cid": "1", "_bb_sa_ids": "7427"}
"""

import time
import uuid
from urllib.parse import quote

import httpx

_BASE_URL = "https://www.bigbasket.com"

_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36"
)


# ── Location lookup tables (from scan2order1 location_agent.py) ─

CITY_CID_MAP: dict[str, str] = {
    "bangalore": "1",
    "hyderabad": "3",
    "mumbai": "4",
    "pune": "5",
    "chennai": "6",
    "delhi": "9",
    "mysore": "10",
    "madurai": "11",
    "coimbatore": "12",
    "vijayawada": "13",
    "kolkata": "14",
    "ahmedabad": "15",
    "nashik": "16",
    "lucknow": "17",
    "panipat": "18",
    "vadodara": "19",
    "visakhapatnam": "20",
    "surat": "21",
    "nagpur": "22",
    "patna": "23",
    "indore": "24",
    "chandigarh": "26",
    "jaipur": "27",
    "ludhiana": "30",
    "noida": "31",
    "kochi": "32",
    "kalluru": "34",
    "cuttack": "35",
    "guwahati": "36",
    "hubli": "38",
    "raipur": "42",
    "rajkot": "43",
    "bareilly": "45",
    "allahabad": "46",
    "vikarabad": "47",
    "gudiyatham": "48",
    "tiruvallur": "49",
    "srikakulam": "50",
}

CITY_NHID_FALLBACK: dict[str, str] = {
    "bangalore": "7427",
    "hyderabad": "6",
    "mumbai": "8",
    "pune": "267",
    "chennai": "1681",
    "delhi": "895",
    "mysore": "82",
    "madurai": "97",
    "coimbatore": "99",
    "vijayawada": "103",
    "kolkata": "665",
    "ahmedabad": "109",
    "nashik": "3569",
    "lucknow": "126",
    "panipat": "228",
    "vadodara": "229",
    "visakhapatnam": "299",
    "surat": "300",
    "nagpur": "367",
    "patna": "375",
    "indore": "382",
    "chandigarh": "386",
    "jaipur": "3089",
    "ludhiana": "713",
    "noida": "666",
    "kochi": "1368",
    "kalluru": "6028",
    "cuttack": "5600",
    "guwahati": "3555",
    "hubli": "6874",
    "raipur": "6015",
    "rajkot": "6026",
    "bareilly": "6569",
    "allahabad": "7427",
    "vikarabad": "6877",
    "gudiyatham": "6859",
    "tiruvallur": "4638",
    "srikakulam": "4634",
}


def get_location_cookies(city: str) -> dict:
    """Return BigBasket location cookies for a city name.

    These must be merged into the user's cookie dict for city-aware search.
    Defaults to Bangalore if city not found.
    """
    city = city.lower().strip()
    return {
        "_bb_cid": CITY_CID_MAP.get(city, "1"),
        "_bb_sa_ids": CITY_NHID_FALLBACK.get(city, "7427"),
    }


# ── Internal helpers ────────────────────────────────────────────

def _api_headers(csurftoken: str) -> dict:
    """Headers shared by all BB PWA API calls. x-tracker is fresh per call."""
    return {
        "accept": "*/*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "common-client-static-version": "101",
        "content-type": "application/json",
        "osmos-enabled": "true",
        "user-agent": _MOBILE_UA,
        "x-caller": "bigbasket-pwa",
        "x-channel": "BB-PWA",
        "x-csurftoken": csurftoken,
        "x-entry-context": "bbnow",
        "x-entry-context-id": "10",
        "x-integrated-fc-door-visible": "false",
        "x-requested-with": "XMLHttpRequest",
        "x-tracker": str(uuid.uuid4()),
    }


async def _ensure_csurftoken(client: httpx.AsyncClient) -> str:
    """Get csurftoken cookie, fetching a fresh one via warmup GET if missing.

    httpx.AsyncClient stores Set-Cookie responses in its own jar automatically.
    We initialize the client with user cookies from Redis; if csurftoken is
    absent/expired, this warmup triggers a fresh one from BigBasket's server.
    """
    csurftoken = client.cookies.get("csurftoken") or ""
    if csurftoken:
        return csurftoken

    warmup_t = time.time()
    try:
        await client.get(
            _BASE_URL + "/",
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
                "user-agent": _MOBILE_UA,
            },
            follow_redirects=True,
        )
    except Exception as e:
        print(f"[bigbasket] csurftoken warmup error: {e}")
        return ""

    csurftoken = client.cookies.get("csurftoken") or ""
    warmup_ms = int((time.time() - warmup_t) * 1000)
    if not csurftoken:
        print(f"[bigbasket] csurftoken warmup failed - no Set-Cookie ({warmup_ms}ms)")
        return ""
    print(f"[bigbasket] csurftoken warmup OK ({warmup_ms}ms)")
    return csurftoken


# ── Public API ──────────────────────────────────────────────────

async def search_item_api(query: str, cookies: dict) -> list[dict]:
    """Search BigBasket for a query using the listing-svc API.

    Args:
        query: search term (e.g. "tomato 500g")
        cookies: user's BigBasket cookies from Redis (must contain BBAUTHTOKEN)

    Returns:
        List of product dicts, each with:
            name, price, sale_price, unit, image_url, product_id, fc_id
        Returns [] on failure (caller can fall back or skip this store).
    """
    if not cookies.get("BBAUTHTOKEN"):
        print("[bigbasket] search_item_api: no BBAUTHTOKEN - store not connected")
        return []

    print(f"\n[bigbasket] === API SEARCH: '{query}' ===")
    t_start = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
            csurftoken = await _ensure_csurftoken(client)
            if not csurftoken:
                return []

            url = f"{_BASE_URL}/listing-svc/v2/products"
            params = {
                "type": "ps",
                "slug": quote(query),
                "page": "1",
                "bucket_id": "4",
            }
            resp = await client.get(
                url, params=params, headers=_api_headers(csurftoken)
            )

        elapsed_ms = int((time.time() - t_start) * 1000)
        if resp.status_code != 200:
            print(f"[bigbasket] API search HTTP {resp.status_code} ({elapsed_ms}ms)")
            return []

        data = resp.json()
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[bigbasket] API search failed after {elapsed_ms}ms: {e}")
        return []

    products: list[dict] = []
    for tab in (data.get("tabs") or []):
        for p in ((tab.get("product_info") or {}).get("products") or []):
            avail = p.get("availability") or {}
            if avail.get("avail_status") != "001":
                continue

            pricing = (p.get("pricing") or {}).get("discount") or {}
            try:
                sale = float(((pricing.get("prim_price") or {}).get("sp")) or 0)
            except (TypeError, ValueError):
                sale = 0.0
            try:
                mrp = float(pricing.get("mrp") or 0)
            except (TypeError, ValueError):
                mrp = 0.0

            images = p.get("images") or []
            image_url = (images[0] or {}).get("l", "") if images else ""

            # fc_id (fulfillment center) is needed by add_to_cart_api.
            fc_id = ((p.get("visibility") or {}).get("fc_id"))

            products.append({
                "name": (p.get("desc") or "")[:120],
                "price": mrp or sale,
                "sale_price": sale,
                "unit": p.get("w") or "",
                "image_url": image_url,
                "product_id": str(p.get("id") or ""),
                "fc_id": fc_id,
                "app": "bigbasket",
                "app_name": "BigBasket",
            })
            if len(products) >= 8:
                break
        if len(products) >= 8:
            break

    print(f"[bigbasket] === API RESULT: {len(products)} products ===\n")
    return products


async def add_to_cart_api(
    product_id: str,
    query: str = "",
    count: int = 1,
    fc_id: int | None = None,
    cookies: dict | None = None,
) -> dict:
    """Add an item to the BigBasket cart via the cart-incr-i API.

    Args:
        product_id: BigBasket product SKU id
        query: search query string (used as the cart `term` field)
        count: quantity to add
        fc_id: fulfillment center id from search result (required)
        cookies: user's BigBasket cookies from Redis

    Returns:
        {"success": True, "count_added": N}  on success
        {"success": False, "reason": "..."}  on failure
    """
    cookies = cookies or {}
    if not fc_id:
        return {"success": False, "reason": "fc_id missing from product"}
    if not cookies.get("BBAUTHTOKEN"):
        return {"success": False, "reason": "not logged in"}

    print(f"\n[bigbasket] === API ADD: pid={product_id} qty={count} ===")
    t_start = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
            csurftoken = await _ensure_csurftoken(client)
            if not csurftoken:
                return {"success": False, "reason": "csurftoken unavailable"}

            url = f"{_BASE_URL}/mapi/v4.2.0/c-incr-i/"
            body = {
                "prod_id": str(product_id),
                "_bb_client_type": "PWA",
                "qty": count,
                "first_atb": 1,
                "inv_info": {
                    "skus": [
                        {"id": int(product_id), "qty": count, "fc_id": int(fc_id)}
                    ]
                },
                "term": query,
                "term_source": "ps",
            }
            headers = _api_headers(csurftoken)
            headers["origin"] = _BASE_URL
            resp = await client.post(url, json=body, headers=headers)

        elapsed_ms = int((time.time() - t_start) * 1000)
        if resp.status_code != 200:
            print(f"[bigbasket] API add HTTP {resp.status_code} ({elapsed_ms}ms)")
            return {"success": False, "reason": f"HTTP {resp.status_code}"}

        data = resp.json()
        if data.get("status") != "OK":
            reason = data.get("status") or data.get("message") or "unknown error"
            print(f"[bigbasket] API add status={reason} ({elapsed_ms}ms)")
            return {"success": False, "reason": str(reason)}

        tot_qty = (
            ((data.get("response") or {}).get("sku") or {}).get("tot_qty")
        ) or 0
        count_added = min(count, int(tot_qty)) if tot_qty else count
        print(
            f"[bigbasket] API add OK pid={product_id} "
            f"tot_qty={tot_qty} ({elapsed_ms}ms)"
        )
        return {"success": True, "count_added": count_added}

    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[bigbasket] API add failed after {elapsed_ms}ms: {e}")
        return {"success": False, "reason": f"exception: {e}"}
