"""stores/bigbasket.py - BigBasket httpx store module.

Adapted from scan2order2/automators/bigbasket.py.
All Playwright dependencies removed. Cookies sourced from SQLite (or MySQL when
MYSQL_URL is set) via storage.user_store.
"""

import time
import uuid
import httpx
from urllib.parse import quote

from storage.user_store import get_store_cookies, update_store_cookies
from stores._common import MOBILE_UA as _MOBILE_UA

APP_NAME = "bigbasket"
DISPLAY_NAME = "BigBasket"
BASE_URL = "https://www.bigbasket.com"


def is_session_valid(user_id: str) -> bool:
    cookies = get_store_cookies(user_id, APP_NAME)
    return bool(cookies.get("BBAUTHTOKEN"))


async def _ensure_csurftoken(client: httpx.AsyncClient, user_id: str) -> str:
    """Ensure the httpx client has a fresh csurftoken cookie.

    If missing, warmup with a GET to bigbasket.com/ which triggers Set-Cookie.
    Persists the new token back via user_store.update_store_cookies. Returns "" on failure.
    """
    csurftoken = client.cookies.get("csurftoken") or ""
    if csurftoken:
        return csurftoken

    t = time.time()
    try:
        await client.get(BASE_URL + "/", headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "user-agent": _MOBILE_UA,
        }, follow_redirects=True)
    except Exception as e:
        print(f"[bigbasket] csurftoken warmup error: {e}")
        return ""

    csurftoken = client.cookies.get("csurftoken") or ""
    elapsed = int((time.time() - t) * 1000)
    if not csurftoken:
        print(f"[bigbasket] csurftoken warmup failed ({elapsed}ms)")
        return ""

    print(f"[bigbasket] csurftoken warmup OK ({elapsed}ms)")
    update_store_cookies(user_id, APP_NAME, {"csurftoken": csurftoken})
    return csurftoken


def _api_headers(csurftoken: str) -> dict:
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


async def search_item_api(user_id: str, query: str) -> list[dict]:
    """Search via BigBasket's listing-svc API.

    Returns the same dict shape as scan2order2 plus fc_id.
    Returns [] on any failure.
    """
    cookies = get_store_cookies(user_id, APP_NAME)
    if not cookies.get("BBAUTHTOKEN"):
        print(f"[bigbasket] search_item_api: no BBAUTHTOKEN for user {user_id[:8]}")
        return []

    print(f"\n[bigbasket] === API SEARCH: '{query}' ===")
    t_start = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
            csurftoken = await _ensure_csurftoken(client, user_id)
            if not csurftoken:
                return []

            url = f"{BASE_URL}/listing-svc/v2/products"
            params = {"type": "ps", "slug": query, "page": "1", "bucket_id": "4"}
            resp = await client.get(url, params=params, headers=_api_headers(csurftoken))

        elapsed_ms = int((time.time() - t_start) * 1000)
        if resp.status_code != 200:
            print(f"[bigbasket] API search HTTP {resp.status_code} ({elapsed_ms}ms)")
            return []
        data = resp.json()
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[bigbasket] API search failed after {elapsed_ms}ms: {e}")
        return []

    products = []
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
            fc_id = ((p.get("visibility") or {}).get("fc_id"))

            products.append({
                "name": (p.get("desc") or "")[:120],
                "price": mrp or sale,
                "sale_price": sale,
                "unit": p.get("w") or "",
                "image_url": image_url,
                "product_id": str(p.get("id") or ""),
                "fc_id": fc_id,
                "app": APP_NAME,
                "app_name": DISPLAY_NAME,
            })
            if len(products) >= 8:
                break
        if len(products) >= 8:
            break

    elapsed_ms = int((time.time() - t_start) * 1000)
    print(f"[bigbasket] === API RESULT: {len(products)} products ({elapsed_ms}ms) ===\n")
    return products


async def add_to_cart_api(user_id: str, product_id: str, count: int = 1,
                          fc_id: int | None = None) -> dict:
    """Add to cart via POST /mapi/v4.2.0/c-incr-i/.

    Returns {"success": True, "count_added": N} or {"success": False, "reason": str}.
    """
    if not fc_id:
        return {"success": False, "reason": "fc_id missing from product"}

    cookies = get_store_cookies(user_id, APP_NAME)
    if not cookies.get("BBAUTHTOKEN"):
        return {"success": False, "reason": "not logged in"}

    print(f"\n[bigbasket] === API ADD: pid={product_id} qty={count} ===")
    t_start = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
            csurftoken = await _ensure_csurftoken(client, user_id)
            if not csurftoken:
                return {"success": False, "reason": "csurftoken unavailable"}

            url = f"{BASE_URL}/mapi/v4.2.0/c-incr-i/"
            body = {
                "prod_id": str(product_id),
                "_bb_client_type": "PWA",
                "qty": count,
                "first_atb": 1,
                "inv_info": {
                    "skus": [{"id": int(product_id), "qty": count, "fc_id": int(fc_id)}]
                },
                "term": "",
                "term_source": "ps",
            }
            headers = _api_headers(csurftoken)
            headers["origin"] = BASE_URL
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

        tot_qty = (((data.get("response") or {}).get("sku") or {}).get("tot_qty")) or 0
        count_added = min(count, int(tot_qty)) if tot_qty else count
        print(f"[bigbasket] API add OK pid={product_id} tot_qty={tot_qty} ({elapsed_ms}ms)")
        return {"success": True, "count_added": count_added}
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[bigbasket] API add failed after {elapsed_ms}ms: {e}")
        return {"success": False, "reason": f"exception: {e}"}


def checkout_url() -> str:
    return f"{BASE_URL}/basket/"
