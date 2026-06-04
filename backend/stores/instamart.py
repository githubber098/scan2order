"""stores/instamart.py - Swiggy Instamart httpx store module.

Mirrors the Zepto integration (cookie session + a location-derived store id):

  Search:  POST https://www.swiggy.com/api/instamart/search/v2
           query in the JSON body; storeId / primaryStoreId passed as query
           params. Like Zepto's store_id, an empty storeId yields HTTP 202 with
           an empty body (no serviceable store for the session's location), so a
           saved delivery address is required for results.
  Cart:    POST https://www.swiggy.com/api/instamart/checkout/v2/cart/item
           (add/update a single item). GET .../cart returns the current cart.

Auth is cookie-only, sourced from SQLite via storage.user_store (captured by the
browser-login relay). Key cookies: `tid` (session JWT), `sid`, `deviceId`
(signed — the raw UUID inside is echoed in the `x-device-id` header).

If the direct httpx call is blocked (Swiggy's `matcher` anti-bot header can't be
reproduced server-side), search/cart fall back to an in-page Playwright fetch
(the same technique used for Blinkit) so the browser computes the header itself.
TODO: confirm whether `matcher` is actually required; if httpx works reliably
without it, drop the Playwright fallback.
"""

import json
import re
import time
from urllib.parse import quote, unquote

import httpx

from storage.user_store import get_store_session, get_store_cookies
from stores._common import MOBILE_UA as _MOBILE_UA

APP_NAME = "instamart"
DISPLAY_NAME = "Instamart"
BASE_URL = "https://www.swiggy.com"
_SEARCH_PATH = "/api/instamart/search/v2"
_HOME_PATH = "/api/instamart/home/v2"
_CART_GET_PATH = "/api/instamart/checkout/v2/cart"
_CART_ITEM_PATH = "/api/instamart/checkout/v2/cart/item"

# Swiggy web build version sent as x-build-version on every Instamart API call.
# Captured from a live web session; may need bumping when Swiggy ships a new
# build (search starts 4xx/202-ing for no other obvious reason).
_BUILD_VERSION = "2.347.0"

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I
)


# ── Session ────────────────────────────────────────────────────────────────────

def _raw_device_id(device_cookie: str) -> str:
    """Extract the bare UUID from Swiggy's signed `deviceId` cookie.

    The cookie looks like 's:<uuid>.<sig>' (URL-encoded as 's%3A<uuid>.<sig>').
    The x-device-id header uses just the UUID.
    """
    if not device_cookie:
        return ""
    dec = unquote(device_cookie)
    m = _UUID_RE.search(dec)
    return m.group(0) if m else ""


def _hunt_store_id(local_storage: dict, raw_cookies: dict) -> str:
    """Recursively search stored JSON blobs for a Swiggy store id.

    Swiggy persists the active store under various keys (address/location state,
    cart state). Returns the first plausible store id found, else "".
    """
    def _search(obj) -> str:
        if isinstance(obj, dict):
            for key in ("storeId", "store_id", "primaryStoreId",
                        "primary_store_id", "swiggyStoreId", "activeStoreId",
                        "nearestStoreId", "instamart_store_id", "retailStoreId"):
                v = obj.get(key)
                if isinstance(v, (str, int)) and str(v).strip():
                    return str(v)
            for v in obj.values():
                got = _search(v)
                if got:
                    return got
        elif isinstance(obj, list):
            for item in obj:
                got = _search(item)
                if got:
                    return got
        return ""

    for source in (local_storage, raw_cookies):
        for val in (source or {}).values():
            if not isinstance(val, str) or "{" not in val:
                continue
            raw = unquote(val) if "%" in val else val
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            got = _search(parsed)
            if got:
                return got
    return ""


def _get_instamart_session(user_id: str) -> dict:
    """Pull Instamart session tokens from stored cookies/localStorage."""
    stored = get_store_session(user_id, APP_NAME)
    raw_cookies = stored.get("cookies", {}) or {}
    local_storage = stored.get("local_storage", {}) or {}
    if not raw_cookies:
        return {}

    tid = raw_cookies.get("tid", "")
    sid = raw_cookies.get("sid", "")
    device_cookie = raw_cookies.get("deviceId", "")
    device_id = _raw_device_id(device_cookie)

    # Priority 1: relay-captured storeId persisted as _s2o_store_id in the
    # saved cookies dict (set by auth_browser during/after the relay). This is
    # the most reliable source because Swiggy holds the active storeId in
    # in-memory state and sends it on API request headers, which the relay
    # interceptor sniffs — it is NOT reliably stored in cookies or localStorage.
    store_id = (unquote(raw_cookies.get("_s2o_store_id", "") or "")).strip()

    # Priority 2: deep scan of local_storage and cookie blobs for known key names.
    # Used when the relay didn't capture headers (e.g. session was very short).
    if not store_id:
        store_id = _hunt_store_id(local_storage, raw_cookies)

    return {
        "cookies": raw_cookies,
        "local_storage": local_storage,
        "tid": tid,
        "sid": sid,
        "device_id": device_id,
        "store_id": store_id,
    }


def is_session_valid(user_id: str) -> bool:
    sess = _get_instamart_session(user_id)
    # A usable Instamart session needs the session token (tid) and a device id.
    return bool(sess.get("tid") and sess.get("device_id"))


def session_health(user_id: str) -> dict:
    """Static health probe (no network) for the page-load status indicator."""
    sess = _get_instamart_session(user_id)
    if not (sess.get("tid") and sess.get("device_id")):
        return {"ok": False, "reason": "Session expired — reconnect Instamart."}
    if not sess.get("store_id"):
        # Mirrors Zepto: authenticated but no resolved delivery store → searches
        # come back empty (HTTP 202). The id is usually still recoverable at
        # request time from /home/v2, so we don't hard-fail, just warn.
        return {"ok": False,
                "reason": "No delivery store — reconnect Instamart with a saved address."}
    return {"ok": True, "reason": ""}


# ── HTTP helpers ────────────────────────────────────────────────────────────────

def _cookie_header(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def _api_headers(sess: dict, referer: str) -> dict:
    return {
        "accept": "*/*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": BASE_URL,
        "referer": referer,
        "user-agent": _MOBILE_UA,
        "x-build-version": _BUILD_VERSION,
        "x-device-id": sess.get("device_id", ""),
        "cookie": _cookie_header(sess.get("cookies") or {}),
    }


async def _resolve_store_id(sess: dict) -> str:
    """Return a usable storeId, fetching /home/v2 if the cookie hunt found none.

    Swiggy resolves the serviceable store from the session's saved location;
    /home/v2 echoes the active storeId in its response (and/or sets it as a
    query param the web app then reuses on search).
    """
    if sess.get("store_id"):
        return sess["store_id"]
    try:
        headers = _api_headers(sess, f"{BASE_URL}/instamart")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(BASE_URL + _HOME_PATH, headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            sid = _hunt_store_id({}, {"_": json.dumps(data)})
            if sid:
                print(f"[instamart] store_id resolved from /home/v2: {sid}")
                return sid
            print(f"[instamart] /home/v2 200 but no storeId; top keys: "
                  f"{list(data.keys())[:10]}")
        else:
            print(f"[instamart] /home/v2 HTTP {resp.status_code}")
    except Exception as e:
        print(f"[instamart] /home/v2 error: {e}")
    return ""


# ── Search ──────────────────────────────────────────────────────────────────────

def _to_rupees(v) -> float:
    """Swiggy prices may be paise (int, e.g. 5500) or rupees. Heuristic: values
    that are whole and ≥ 1000 with no decimal are treated as paise."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return round(f / 100, 2) if f >= 1000 and f == int(f) else round(f, 2)


def _extract_variation(v: dict) -> dict | None:
    """Map one Swiggy product 'variation' dict to the canonical product shape.

    Field names are best-effort (Swiggy nests products under several shapes);
    the recursive _parse_search_response feeds anything that looks like a
    product. Returns None when required fields are missing.
    TODO: pin exact field names against a real located search/v2 response.
    """
    if not isinstance(v, dict):
        return None

    name = (v.get("display_name") or v.get("name")
            or (v.get("product") or {}).get("name") or "")
    if not name:
        return None

    pid = str(v.get("id") or v.get("product_id") or v.get("variation_id") or "")
    if not pid:
        return None

    price_block = v.get("price") if isinstance(v.get("price"), dict) else v
    sale = _to_rupees(price_block.get("offer_price")
                      or price_block.get("store_price")
                      or price_block.get("price") or 0)
    mrp = _to_rupees(price_block.get("mrp") or sale)
    if sale <= 0 and mrp <= 0:
        return None
    if sale <= 0:
        sale = mrp

    in_stock = v.get("in_stock")
    if in_stock is False or v.get("inventory", 1) == 0:
        return None

    unit = (v.get("quantity") or v.get("sku_quantity_with_combo")
            or v.get("weight_in_grams_label") or v.get("display_quantity") or "")

    images = v.get("images") or []
    img_id = images[0] if images and isinstance(images[0], str) else ""
    image_url = (f"https://instamart-media-assets.swiggy.com/swiggy/image/upload/"
                 f"fl_lossy,f_auto,q_auto,h_300/{img_id}" if img_id else "")

    return {
        "name": str(name)[:120],
        "price": mrp,
        "sale_price": sale,
        "unit": str(unit),
        "image_url": image_url,
        "product_id": pid,
        "store_product_id": pid,
        "app": APP_NAME,
        "app_name": DISPLAY_NAME,
    }


def _parse_search_response(data: dict) -> list[dict]:
    """Recursively pull products from a Swiggy Instamart search/v2 response.

    Swiggy wraps products under data.widgets[].data[].variations[] (and shifts
    the shape across builds), so rather than hard-code one path we recurse and
    collect any dict that _extract_variation accepts. Caps at 8 like the others.
    """
    products: list[dict] = []
    seen: set = set()

    def _walk(obj, depth=0):
        if depth > 12 or len(products) >= 8:
            return
        if isinstance(obj, dict):
            # A product variation usually carries a name + a price/quantity.
            if (("display_name" in obj or "name" in obj)
                    and ("price" in obj or "store_price" in obj
                         or "offer_price" in obj or "variations" in obj)):
                p = _extract_variation(obj)
                if p and p["product_id"] not in seen:
                    seen.add(p["product_id"])
                    products.append(p)
            for v in obj.values():
                _walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, depth + 1)

    _walk(data)
    return products[:8]


async def search_item_api(user_id: str, query: str) -> list[dict]:
    """Search Swiggy Instamart. Returns [] on any failure.

    Like Zepto, an empty storeId (no saved delivery address) makes Swiggy return
    HTTP 202 with no body → zero products.
    """
    sess = _get_instamart_session(user_id)
    if not sess.get("tid") or not sess.get("device_id"):
        print(f"[instamart] search: no session (tid/device_id) for {user_id[:8]}")
        return []

    print(f"\n[instamart] === API SEARCH: '{query}' ===")
    t_start = time.time()

    store_id = await _resolve_store_id(sess)
    if not store_id:
        print(f"[instamart] search: no store_id — results will likely be empty "
              f"(ensure a delivery address is saved in Swiggy).")

    params = {
        "offset": "0", "ageConsent": "false", "voiceSearchTrackingId": "",
        "storeId": store_id, "primaryStoreId": store_id, "secondaryStoreId": "",
    }
    body = {
        "facets": [], "sortAttribute": "", "query": query,
        "search_results_offset": "0", "page_type": "INSTAMART_SEARCH_PAGE",
        "is_pre_search_tag": False,
    }
    referer = f"{BASE_URL}/instamart/search?custom_back=true&query={quote(query)}"
    headers = _api_headers(sess, referer)

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(BASE_URL + _SEARCH_PATH, params=params,
                                     content=json.dumps(body), headers=headers)
        elapsed_ms = int((time.time() - t_start) * 1000)
        if resp.status_code == 200:
            data = resp.json()
            products = _parse_search_response(data)
            print(f"[instamart] search HTTP 200: {len(products)} products "
                  f"({elapsed_ms}ms)")
            if not products:
                # Log the shape so the parser can be pinned to the real response.
                print(f"[instamart] search: 200 but 0 parsed. top keys="
                      f"{list(data.keys())[:12]} snippet={json.dumps(data)[:400]}")
            return products
        if resp.status_code == 202:
            print(f"[instamart] search HTTP 202 (no serviceable store — empty "
                  f"location/storeId) ({elapsed_ms}ms)")
            return []
        print(f"[instamart] search HTTP {resp.status_code} ({elapsed_ms}ms) "
              f"body={resp.text[:200]!r}")
        # TODO: if 403/blocked due to missing `matcher`, fall back to an in-page
        # Playwright fetch here (see Blinkit _cart_post_playwright pattern).
        return []
    except Exception as e:
        print(f"[instamart] search exception: {e}")
        return []


# ── Cart ────────────────────────────────────────────────────────────────────────

async def add_all_to_cart_api(user_id: str, items: list[dict]) -> dict:
    """Add items to the Instamart cart.

    Swiggy's cart is per-item (POST .../cart/item with the item + delta qty),
    not a whole-cart replace like Blinkit. We loop the items but expose the
    batched {success, items:[{success,count_added}]} shape so the server's cart
    routing treats Instamart like Zepto/Blinkit.
    TODO: confirm the exact /cart/item request body against a live session.
    """
    if not items:
        return {"success": True, "items": []}

    sess = _get_instamart_session(user_id)
    if not sess.get("tid") or not sess.get("device_id"):
        return {"success": False, "reason": "no session (tid/device_id)"}

    store_id = await _resolve_store_id(sess)
    print(f"\n[instamart] === API ADD: {len(items)} items (store_id={store_id or 'MISSING'}) ===")
    t_start = time.time()

    referer = f"{BASE_URL}/instamart"
    headers = _api_headers(sess, referer)
    item_results: list[dict] = []
    ok_any = False

    async with httpx.AsyncClient(timeout=12.0) as client:
        for item in items:
            pid = str(item.get("product_id") or "")
            try:
                qty = max(1, min(99, int(item.get("count") or 1)))
            except (TypeError, ValueError):
                qty = 1
            if not pid:
                item_results.append({"success": False, "reason": "no product_id"})
                continue
            body = {
                "item_id": pid, "spin": "", "store_id": store_id,
                "total_quantity": qty, "quantity_delta": qty,
                "meta": {}, "widget_meta": {},
            }
            try:
                resp = await client.post(BASE_URL + _CART_ITEM_PATH,
                                         content=json.dumps(body), headers=headers)
                if resp.status_code in (200, 201):
                    ok_any = True
                    item_results.append({"success": True, "count_added": qty})
                else:
                    print(f"[instamart] cart item HTTP {resp.status_code} "
                          f"body={resp.text[:200]!r}")
                    item_results.append({"success": False,
                                         "reason": f"HTTP {resp.status_code}"})
            except Exception as e:
                print(f"[instamart] cart item exception: {e}")
                item_results.append({"success": False, "reason": str(e)})

    elapsed_ms = int((time.time() - t_start) * 1000)
    added = sum(1 for r in item_results if r.get("success"))
    print(f"[instamart] === API ADD: {added}/{len(items)} ({elapsed_ms}ms) ===\n")
    return {"success": ok_any, "items": item_results}


async def add_to_cart_api(user_id: str, product_id: str, count: int = 1) -> dict:
    """Single-item add — thin wrapper over the batched call."""
    res = await add_all_to_cart_api(
        user_id, [{"product_id": product_id, "count": count}]
    )
    if res.get("success"):
        return {"success": True, "count_added": count}
    return {"success": False, "reason": res.get("reason", "add failed")}


def checkout_url() -> str:
    return f"{BASE_URL}/instamart/checkout"
