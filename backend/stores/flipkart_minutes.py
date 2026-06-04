"""stores/flipkart_minutes.py — Flipkart Minutes (hyperlocal quick-commerce) store module.

Auth model
──────────
Flipkart Minutes shares the main Flipkart account system. After the user logs in
via the Playwright browser relay (www.flipkart.com) and saves a delivery
address, the session is captured as cookies + localStorage.

Key cookies
  flid   — Flipkart user identity (long-lived; login indicator)
  T      — Encrypted session token (shorter-lived; refreshable)
  SN     — Session number
  fn_at  — Authentication token variant (app-level)

Location
  Unlike the other stores the delivery pincode lives in the stored cookies/
  localStorage under the key the user's browser used. The relay's location_scan
  in auth_browser.py hunts for any "pinCode"/"lat"/"latitude" substring across
  all stored blobs, so the relay closes as soon as an address is set.

Search API
  NOTE: Flipkart's internal BFF API is geo-restricted (India only) and
  undocumented. Flipkart Minutes is served from the main www.flipkart.com
  domain under marketplace=HYPERLOCAL (there is NO minutes.flipkart.com host,
  and 2.flipkart.com does not exist either — both fail DNS). The web app's BFF
  is served from www.flipkart.com itself. On the first live deployment the
  response interceptor in auth_browser.py logs all JSON API calls made during
  the relay session; look for "[fm-interceptor]" / "[fm]" lines in server.log
  and update _FM_SEARCH_URL or the request body if they differ.

  Current best-guess:
    POST https://www.flipkart.com/api/4/page/fetch
         {"pageUri": "/search?q={query}&marketplace=HYPERLOCAL", "pageContext": {}}

Cart API
  Best-guess based on Flipkart web cart patterns; will be refined from logs.
  Cart key: `fk_cart_id` (cookie, set on first add or on page load).
"""

import json
import re
import time
from urllib.parse import quote, unquote

import httpx

from storage.user_store import get_store_session, get_store_cookies
from stores._common import MOBILE_UA as _MOBILE_UA

APP_NAME = "flipkart_minutes"
DISPLAY_NAME = "Flipkart Minutes"
# NOTE: there is NO minutes.flipkart.com host (DNS: non-existent). Flipkart
# Minutes lives on the main www.flipkart.com domain under marketplace=HYPERLOCAL.
# 2.flipkart.com is ALSO non-existent — the web app's BFF is served from
# www.flipkart.com itself (relative /api/… paths); the mobile BFF host is
# 1.rome.api.flipkart.com. We use the web BFF since we capture a web session.
BASE_URL = "https://www.flipkart.com"
_FK_DOMAIN = "https://www.flipkart.com"

# Search: Flipkart web BFF "page fetch" — the same endpoint the web search page
# calls. POST with a pageUri body. The exact api version (/api/3 vs /api/4) and
# body shape are best-guess; the relay interceptor logs the real call on the
# first India run — look for "[fm-interceptor]" lines and update if different.
_FM_SEARCH_URL = _FK_DOMAIN + "/api/4/page/fetch"
_FM_SEARCH_MARKETPLACE = "HYPERLOCAL"

# Cart endpoint (best-guess — update from interceptor logs if needed).
_FM_CART_URL = _FK_DOMAIN + "/api/1/cart/add"

# Headers sent by the Flipkart web app on every request.
_WEB_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.6778.135 Mobile Safari/537.36"
)
_CLIENT_HINTS = {
    "Sec-CH-UA": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-CH-UA-Mobile": "?1",
    "Sec-CH-UA-Platform": '"Android"',
}


# ── Session helpers ────────────────────────────────────────────────────────────

def _get_fm_session(user_id: str) -> dict:
    """Pull Flipkart Minutes session from stored cookies + localStorage."""
    stored = get_store_session(user_id, APP_NAME)
    raw_cookies = stored.get("cookies", {}) or {}
    local_storage = stored.get("local_storage", {}) or {}
    if not raw_cookies:
        return {}

    def _norm(k: str) -> str:
        return "".join(ch.lower() for ch in k if ch.isalnum())

    # Build normalised lookup for cookies
    bag: dict[str, str] = {}
    for k, v in raw_cookies.items():
        bag[_norm(k)] = unquote(v) if v and "%" in v else (v or "")
    for k, v in local_storage.items():
        bag[_norm(k)] = v or ""

    flid = bag.get("flid", "")
    t_tok = bag.get("t", "")        # Flipkart "T" session token
    sn = bag.get("sn", "")

    # Try to find a delivery pincode from any stored blob.
    pincode = _hunt_pincode(local_storage, raw_cookies)

    return {
        "cookies": raw_cookies,
        "local_storage": local_storage,
        "flid": flid,
        "t_token": t_tok,
        "sn": sn,
        "pincode": pincode,
    }


def _hunt_pincode(local_storage: dict, raw_cookies: dict) -> str:
    """Recursively search stored blobs for a delivery pincode (6 digits).

    Two passes, precise → fuzzy:

    Pass 1 (precise): walk every stored JSON blob and return the first 6-digit
        value held by a pincode-named key (pinCode/deliveryPincode/…). This is
        unambiguous, so it runs first.

    Pass 2 (fuzzy fallback): Flipkart often stores the address as a single
        formatted string ("12 MG Road, Bangalore 560034") with no dedicated
        pincode field. So we scan address-like blobs (those containing an
        address marker word) for a standalone Indian PIN token — [1-8]\\d{5}
        not glued to other digits. Guarded by the marker check so a random
        6-digit number in an unrelated blob (order id, timestamp) can't be
        mistaken for a pincode.

    Returns the first hit, or "" if none. The relay's broader location_scan in
    auth_browser.py decides when to CLOSE the session; this stricter probe only
    drives the page-load health indicator, so over-strictness here is at worst a
    spurious "no address" warning, never a wrong pincode sent to the API.
    """
    _PIN_KEYS = frozenset([
        "pinCode", "pin_code", "pincode", "pin", "deliveryPincode",
        "selectedPincode", "locationPincode",
    ])
    # Indian PIN: 6 digits, first 1-8, not part of a longer number.
    _PIN_TOKEN = re.compile(r"(?<!\d)[1-8]\d{5}(?!\d)")
    # An address blob is one mentioning any of these (case-insensitive).
    _ADDR_MARKERS = ("address", "pincode", "pin", "city", "locality",
                     "landmark", "delivery", "addressline", "state")

    def _search_keyed(obj) -> str:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _PIN_KEYS and isinstance(v, (str, int)):
                    s = str(v).strip()
                    if re.fullmatch(r"\d{6}", s):
                        return s
            for v in obj.values():
                r = _search_keyed(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = _search_keyed(item)
                if r:
                    return r
        return ""

    decoded_blobs: list[tuple[str, str]] = []   # (key, decoded_value)
    for source in (local_storage, raw_cookies):
        for key, val in (source or {}).items():
            if not isinstance(val, str) or not val:
                continue
            raw = unquote(val) if "%" in val else val
            decoded_blobs.append((str(key), raw))
            if "{" not in raw:
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            got = _search_keyed(parsed)
            if got:
                return got

    # Pass 2: fuzzy fallback over address-like blobs only. A blob qualifies if
    # EITHER its storage key (e.g. "deliveryAddress") OR its value text mentions
    # an address marker — so a raw address-string cookie with no marker words in
    # the value is still recognised by its key.
    for key, raw in decoded_blobs:
        hay = (key + " " + raw).lower()
        if not any(m in hay for m in _ADDR_MARKERS):
            continue
        m = _PIN_TOKEN.search(raw)
        if m:
            return m.group(0)
    return ""


def is_session_valid(user_id: str) -> bool:
    """True when a Flipkart session has been saved.

    Primary signal: flid cookie (Flipkart user identity, definitive).
    Fallback: any saved cookies at all — this covers the case where Flipkart
    uses a different auth cookie name than 'flid' (or stores auth in
    localStorage). The real cookie name will be confirmed from server.log
    after the first successful connect, then added to the explicit check.
    """
    sess = _get_fm_session(user_id)
    if sess.get("flid"):
        return True
    # Non-empty cookies = session was explicitly saved by the user
    return bool(sess.get("cookies"))


def session_health(user_id: str) -> dict:
    """Static health probe (no network call) for page-load status indicator."""
    sess = _get_fm_session(user_id)
    has_auth = bool(sess.get("flid") or sess.get("cookies"))
    if not has_auth:
        return {"ok": False, "reason": "Session expired — reconnect Flipkart Minutes."}
    if not sess.get("pincode"):
        # Authenticated but no delivery pincode → searches return empty.
        return {
            "ok": False,
            "reason": "No delivery address — reconnect Flipkart Minutes with a saved address.",
        }
    return {"ok": True, "reason": ""}


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _cookie_header(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def _api_headers(sess: dict) -> dict:
    h = {
        "accept": "application/json",
        "accept-language": "en-IN,en;q=0.9",
        "content-type": "application/json",
        "origin": _FK_DOMAIN,
        "referer": BASE_URL + "/",
        "user-agent": _WEB_UA,
        "cookie": _cookie_header(sess.get("cookies") or {}),
        **_CLIENT_HINTS,
    }
    # Pass pincode as a header if we have it — Flipkart uses this to route to
    # the correct hyperlocal dark store.
    pin = sess.get("pincode", "")
    if pin:
        h["x-pincode"] = pin
    return h


# ── Search ─────────────────────────────────────────────────────────────────────

def _extract_product(obj: dict) -> dict | None:
    """Map one Flipkart product dict to the canonical shape.

    Flipkart's BFF page response nests products deep inside widget trees;
    this function is called for any dict that looks like a product listing.
    Accepts two common field-set variants (productInfo.value envelope vs flat).
    """
    if not isinstance(obj, dict):
        return None

    # Variant A: productInfo.value envelope (common in listing widgets)
    if "productInfo" in obj:
        inner = obj.get("productInfo", {})
        if isinstance(inner, dict):
            inner = inner.get("value", inner)
        if not isinstance(inner, dict):
            return None
        obj = inner

    # Variant B: top-level or already-unwrapped product dict
    pid = str(obj.get("id") or obj.get("productId") or obj.get("product_id") or "")
    if not pid:
        return None

    title = (
        obj.get("title") or obj.get("name") or obj.get("productTitle") or ""
    )
    if not title:
        return None

    # Pricing: may be in a nested "pricing" dict or flat fields
    pricing = obj.get("pricing") or {}
    final_p = pricing.get("finalPrice") or {}
    mrp_p = pricing.get("mrp") or {}

    sale = float(final_p.get("value") or obj.get("finalPrice") or
                 obj.get("sale_price") or obj.get("offerPrice") or 0)
    mrp = float(mrp_p.get("value") or obj.get("mrp") or obj.get("price") or sale)
    if sale <= 0 and mrp <= 0:
        return None
    if sale <= 0:
        sale = mrp

    # Unit/size
    unit = (
        obj.get("packSize") or obj.get("packType") or obj.get("unit") or
        obj.get("quantity") or ""
    )

    # Image URL
    images = obj.get("images") or []
    img_url = ""
    if images and isinstance(images[0], dict):
        img_url = images[0].get("url") or ""

    return {
        "name": str(title)[:120],
        "price": round(mrp, 2),
        "sale_price": round(sale, 2),
        "unit": str(unit),
        "image_url": img_url,
        "product_id": pid,
        "store_product_id": pid,
        "app": APP_NAME,
        "app_name": DISPLAY_NAME,
    }


def _parse_response(data) -> list[dict]:
    """Recursively walk a Flipkart BFF JSON response and extract products.

    Flipkart nests products across several different widget/slot shapes. Rather
    than hard-coding one path we BFS every dict that might be a product
    (has 'id'/'productId' AND 'title'/'name' AND 'pricing'/price fields) and
    collect up to 8. This handles minor schema changes across builds.
    """
    products: list[dict] = []
    seen: set[str] = set()

    def _walk(obj, depth=0):
        if depth > 15 or len(products) >= 8:
            return
        if isinstance(obj, dict):
            # Test if this looks like a product
            has_id = bool(obj.get("id") or obj.get("productId")
                          or obj.get("product_id") or "productInfo" in obj)
            has_name = bool(obj.get("title") or obj.get("name")
                            or obj.get("productTitle"))
            has_price = bool(obj.get("pricing") or obj.get("finalPrice")
                             or obj.get("sale_price") or obj.get("offerPrice")
                             or obj.get("mrp") or obj.get("price"))
            if has_id and has_name and has_price:
                p = _extract_product(obj)
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
    """Search Flipkart Minutes. Returns [] on any failure.

    POSTs to Flipkart's web BFF page-fetch endpoint (www.flipkart.com/api/…)
    with a pageUri pointing at the hyperlocal search page. Logs the full
    top-level response structure on the first run (when no products are found)
    so the correct endpoint/parser can be confirmed and refined.

    NOTE: endpoint path + body shape are best-guess and the API is geo-restricted
          to India. Check server.log for "[fm]" / "[fm-interceptor]" lines after
          the first live run and update _FM_SEARCH_URL / the body if needed.
    """
    sess = _get_fm_session(user_id)
    if not sess.get("flid") and not sess.get("cookies"):
        print(f"[fm] search: no session for {user_id[:8]}")
        return []

    print(f"\n[fm] === SEARCH: '{query}' (pincode={sess.get('pincode') or 'MISSING'}) ===")
    t_start = time.time()

    page_uri = (
        f"/search?q={quote(query, safe='')}"
        f"&marketplace={_FM_SEARCH_MARKETPLACE}"
        f"&otracker=search"
    )
    body = {"pageUri": page_uri, "pageContext": {}}
    headers = _api_headers(sess)

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.post(_FM_SEARCH_URL, json=body, headers=headers)

        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[fm] search HTTP {resp.status_code} ({elapsed_ms}ms)")

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception as e:
                print(f"[fm] search: JSON parse error: {e}; body[:300]={resp.text[:300]!r}")
                return []

            products = _parse_response(data)
            print(f"[fm] search: {len(products)} products parsed")

            if not products:
                # Log the shape so the parser can be pinned to the real response.
                top_keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                print(
                    f"[fm] search: 200 but 0 products. top_keys={top_keys} "
                    f"snippet={json.dumps(data)[:500]}"
                )
            return products

        if resp.status_code == 302:
            location = resp.headers.get("location", "")
            print(f"[fm] search: 302 redirect to {location!r} — check endpoint/cookies")
            return []

        if resp.status_code in (401, 403):
            print(f"[fm] search: {resp.status_code} — session may have expired. "
                  f"body={resp.text[:200]!r}")
            return []

        print(f"[fm] search: HTTP {resp.status_code} body={resp.text[:300]!r}")
        return []

    except Exception as e:
        print(f"[fm] search exception: {e}")
        return []


# ── Cart ───────────────────────────────────────────────────────────────────────

async def add_all_to_cart_api(user_id: str, items: list[dict]) -> dict:
    """Add items to Flipkart Minutes cart.

    Uses Flipkart's standard per-item cart-add endpoint. Exposed as a batch
    API (same shape as Zepto/Instamart) for server-side cart routing.

    NOTE: Cart endpoint is best-guess. Check server.log for "[fm]" lines.
    """
    if not items:
        return {"success": True, "items": []}

    sess = _get_fm_session(user_id)
    if not sess.get("flid") and not sess.get("cookies"):
        return {"success": False, "reason": "no session"}

    print(f"\n[fm] === CART ADD: {len(items)} items ===")
    t_start = time.time()

    headers = _api_headers(sess)
    item_results: list[dict] = []
    ok_any = False

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
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
                "item": {
                    "productId": pid,
                    "quantity": qty,
                    "actionType": "ADD",
                    "source": "search",
                }
            }
            try:
                resp = await client.post(
                    _FM_CART_URL, json=body, headers=headers
                )
                if resp.status_code in (200, 201):
                    ok_any = True
                    item_results.append({"success": True, "count_added": qty})
                    print(f"[fm] cart add OK pid={pid} qty={qty}")
                else:
                    print(f"[fm] cart add HTTP {resp.status_code} "
                          f"pid={pid} body={resp.text[:200]!r}")
                    item_results.append({
                        "success": False,
                        "reason": f"HTTP {resp.status_code}",
                    })
            except Exception as e:
                print(f"[fm] cart add exception pid={pid}: {e}")
                item_results.append({"success": False, "reason": str(e)})

    elapsed_ms = int((time.time() - t_start) * 1000)
    added = sum(1 for r in item_results if r.get("success"))
    print(f"[fm] === CART DONE: {added}/{len(items)} ({elapsed_ms}ms) ===\n")
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
    # Flipkart's cart lives at /viewcart (not /cart); scope it to the hyperlocal
    # (Minutes) marketplace so it opens the Minutes basket.
    return BASE_URL + "/viewcart?marketplace=HYPERLOCAL"
