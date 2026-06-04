"""stores/flipkart_minutes.py — Flipkart Minutes (hyperlocal quick-commerce) store module.

Auth model
──────────
Flipkart Minutes shares the main Flipkart account system. After the user logs in
via the Playwright browser relay (www.flipkart.com) and saves a delivery
address, the session is captured as cookies + localStorage.

Key cookies
  flid   — Flipkart user identity (when present; not present in every capture)
  T      — Encrypted session token (present in the June 2026 DevTools captures)
  ULSN   — Auth/user-state JWT-style cookie
  at/rt  — Access/refresh JWT-style cookies used by Flipkart's Rome BFF
  SN/S   — Session-number/session-state cookies

Location
  Unlike the other stores the delivery pincode lives in the stored cookies/
  localStorage under the key the user's browser used. The relay's location_scan
  in auth_browser.py hunts for any "pinCode"/"lat"/"latitude" substring across
  all stored blobs, so the relay closes as soon as an address is set.

Search API
  NOTE: Flipkart's internal BFF API is geo-restricted (India only) and
  undocumented. The June 2026 DevTools captures in fm_search.txt and
  fm_search_2.txt confirm that web search uses the Rome BFF host:

    POST https://2.rome.api.flipkart.com/api/4/page/fetch?cacheFirst=false
         pageUri=/hyperlocal/pr?q={query}&marketplace=HYPERLOCAL
                 &sid=search.flipkart.com&as-show=on
         locationContext.pincode={delivery pincode}

  The captures are cURL request exports only; they do not include response JSON
  bodies. _parse_response therefore remains defensive across Flipkart's nested
  page/widget product shapes instead of claiming a single verified response path.

Cart API
  The DevTools add captures confirm the browse-cart endpoint:

    POST https://2.rome.api.flipkart.com/api/5/cart/browse
         browseContext.listings=[listingId]
         browseCartContext.cartContext[listingId]={productId, quantity, ...}
"""

import json
import re
import time
import uuid
from urllib.parse import quote, unquote

import httpx

from storage.user_store import get_store_session, get_store_cookies
from stores._common import MOBILE_UA as _MOBILE_UA

APP_NAME = "flipkart_minutes"
DISPLAY_NAME = "Flipkart Minutes"
# NOTE: there is NO minutes.flipkart.com host (DNS: non-existent). Flipkart
# Minutes lives on the main www.flipkart.com domain under marketplace=HYPERLOCAL.
# 2.flipkart.com is ALSO non-existent. The live web app calls the Rome API host
# below for page fetches while retaining www.flipkart.com as Origin/Referer.
BASE_URL = "https://www.flipkart.com"
_FK_DOMAIN = "https://www.flipkart.com"
_FK_API_DOMAIN = "https://2.rome.api.flipkart.com"

# Search: confirmed from fm_search.txt / fm_search_2.txt DevTools cURL exports.
_FM_SEARCH_URL = _FK_API_DOMAIN + "/api/4/page/fetch?cacheFirst=false"
_FM_SEARCH_MARKETPLACE = "HYPERLOCAL"
_FM_SEARCH_STORE = "search.flipkart.com"

# Cart endpoint confirmed from fm_add_1.txt / fm_add_2.txt.
_FM_CART_URL = _FK_API_DOMAIN + "/api/5/cart/browse"

# Headers sent by the Flipkart web app on every request.
_WEB_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Mobile Safari/537.36"
)
_X_USER_AGENT = f"{_WEB_UA} FKUA/msite/0.0.3/msite/Mobile"
_CLIENT_HINTS = {
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"Android"',
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
    at_tok = bag.get("at", "")
    rt_tok = bag.get("rt", "")
    ulsn = bag.get("ulsn", "")

    # Try to find a delivery pincode from any stored blob.
    pincode = _hunt_pincode(local_storage, raw_cookies)

    return {
        "cookies": raw_cookies,
        "local_storage": local_storage,
        "flid": flid,
        "t_token": t_tok,
        "at_token": at_tok,
        "rt_token": rt_tok,
        "ulsn": ulsn,
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
    """True when a Flipkart session has been explicitly connected.

    Primary signal: flid cookie (Flipkart user identity, definitive when the
    browser exposes it). The June 2026 DevTools captures do not include flid,
    but do include JWT-length T / ULSN / at / rt cookies. Treat any one of
    those long auth tokens as a connected session while still rejecting short
    guest/navigation cookie sets captured during a timeout.
    """
    sess = _get_fm_session(user_id)
    if sess.get("flid"):
        return True
    for key in ("t_token", "ulsn", "at_token", "rt_token"):
        val = sess.get(key, "")
        if val and len(val) > 20:
            return True
    return False


def session_health(user_id: str) -> dict:
    """Static health probe (no network call) for page-load status indicator."""
    sess = _get_fm_session(user_id)
    has_auth = is_session_valid(user_id)  # reuse the same logic
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
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": _FK_DOMAIN,
        "Referer": BASE_URL + "/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": _WEB_UA,
        "X-User-Agent": _X_USER_AGENT,
        "flipkart_secure": "true",
        "cookie": _cookie_header(sess.get("cookies") or {}),
        **_CLIENT_HINTS,
    }
    return h


# ── Search ─────────────────────────────────────────────────────────────────────

def _request_id() -> str:
    return uuid.uuid4().hex[:16]


def _search_page_uri(query: str) -> str:
    return (
        f"/hyperlocal/pr?q={quote(query, safe='')}"
        f"&marketplace={_FM_SEARCH_MARKETPLACE}"
        f"&sid={_FM_SEARCH_STORE}"
        f"&as-show=on"
    )


def _location_context(sess: dict) -> dict:
    pin = str(sess.get("pincode") or "").strip()
    if not re.fullmatch(r"\d{6}", pin):
        return {}
    return {"pincode": int(pin), "changed": False}


def _text_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for k in ("text", "value", "title", "subtitle", "name"):
            got = _text_value(value.get(k))
            if got:
                return got
    return ""


def _nested(obj: dict, *path: str):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first_text(*values) -> str:
    for value in values:
        got = _text_value(value)
        if got:
            return got
    return ""


def _num_value(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.replace(",", "")
        m = re.search(r"\d+(?:\.\d+)?", s)
        return float(m.group(0)) if m else 0.0
    if isinstance(value, dict):
        for key in ("value", "decimalValue", "amount", "displayValue", "text"):
            got = _num_value(value.get(key))
            if got:
                return got
    return 0.0


def _image_url(obj: dict) -> str:
    # Scalar candidates: direct string fields, common Flipkart names.
    candidates = [
        obj.get("imageUrl"), obj.get("image_url"), obj.get("image"),
        obj.get("primaryImage"), obj.get("defaultImage"), obj.get("searchImage"),
        _nested(obj, "media", "imageUrl"), _nested(obj, "media", "image"),
        _nested(obj, "media", "primaryImage"), _nested(obj, "media", "defaultImage"),
        _nested(obj, "titles", "image"),
    ]
    # List-of-image-dicts sources: each element may be a string URL or a dict
    # with a "url"/"imageUrl"/"src" key (Flipkart's typical {"url": "...", "ghType": "..."}).
    for source in (
        obj.get("images"),
        _nested(obj, "media", "images"),
        obj.get("imageUrls"),
        _nested(obj, "media", "imageList"),
    ):
        if isinstance(source, list):
            candidates.extend(source)
        elif source:
            candidates.append(source)

    for item in candidates:
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, dict):
            got = _first_text(
                item.get("url"), item.get("imageUrl"),
                item.get("src"), item.get("value"), item.get("image"),
            )
            if got:
                return got
    return ""


def _listing_id(obj: dict, parent: dict) -> str:
    return _first_text(
        obj.get("listingId"), obj.get("listing_id"), obj.get("lid"),
        parent.get("listingId"), parent.get("listing_id"), parent.get("lid"),
        _nested(obj, "listingInfo", "listingId"),
        _nested(obj, "listingInfo", "value", "listingId"),
        _nested(parent, "listingInfo", "listingId"),
        _nested(parent, "listingInfo", "value", "listingId"),
    )


def _extract_product(obj: dict) -> dict | None:
    """Map one Flipkart product dict to the canonical shape.

    Flipkart's BFF page response nests products deep inside widget trees;
    this function is called for any dict that looks like a product listing.
    Accepts productInfo.value envelopes, flat product dicts, and nested
    titles/pricing/media fields seen in Flipkart page widgets.
    """
    if not isinstance(obj, dict):
        return None

    parent = obj

    # Variant A: productInfo.value envelope (common in listing widgets)
    if "productInfo" in obj:
        inner = obj.get("productInfo", {})
        if isinstance(inner, dict):
            inner = inner.get("value", inner)
        if not isinstance(inner, dict):
            return None
        obj = inner

    # Variant B: top-level or already-unwrapped product dict
    pid = _first_text(obj.get("id"), obj.get("productId"),
                      obj.get("product_id"), parent.get("productId"))
    if not pid:
        return None

    title = _first_text(
        obj.get("title"), obj.get("name"), obj.get("productTitle"),
        _nested(obj, "titles", "title"),
        _nested(obj, "titles", "productTitle"),
        _nested(obj, "title", "text"),
    )
    if not title:
        return None

    # Pricing: may be in a nested "pricing" dict or flat fields
    pricing = obj.get("pricing") or {}
    sale = (
        _num_value(pricing.get("finalPrice")) or
        _num_value(pricing.get("sellingPrice")) or
        _num_value(obj.get("finalPrice")) or
        _num_value(obj.get("sale_price")) or
        _num_value(obj.get("offerPrice"))
    )
    mrp = (
        _num_value(pricing.get("mrp")) or
        _num_value(obj.get("mrp")) or
        _num_value(obj.get("price")) or
        sale
    )
    if sale <= 0 and mrp <= 0:
        return None
    if sale <= 0:
        sale = mrp

    # Unit/size
    unit = _first_text(
        obj.get("packSize"), obj.get("packType"), obj.get("unit"),
        obj.get("quantity"), _nested(obj, "titles", "subtitle"),
        _nested(obj, "attributes", "packSize"),
    )

    img_url = _image_url(obj)
    listing_id = _listing_id(obj, parent)

    return {
        "name": str(title)[:120],
        "price": round(mrp, 2),
        "sale_price": round(sale, 2),
        "unit": str(unit),
        "image_url": img_url,
        "product_id": pid,
        "store_product_id": listing_id or pid,
        "listing_id": listing_id,
        "app": APP_NAME,
        "app_name": DISPLAY_NAME,
    }


def _parse_response(data) -> list[dict]:
    """Recursively walk a Flipkart BFF JSON response and extract products.

    Flipkart nests products across several different widget/slot shapes, and
    the available DevTools cURL exports do not include response bodies. Rather
    than hard-coding one unverified path, walk every dict and let
    _extract_product() accept only complete product/listing records.
    """
    products: list[dict] = []
    seen: set[str] = set()

    def _walk(obj, depth=0):
        if depth > 15 or len(products) >= 8:
            return
        if isinstance(obj, dict):
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

    POSTs to the confirmed Rome BFF page-fetch endpoint with the same
    hyperlocal /hyperlocal/pr pageUri shape captured from Flipkart web.
    """
    sess = _get_fm_session(user_id)
    if not is_session_valid(user_id):
        print(f"[fm] search: no valid session for {user_id[:8]}")
        return []

    print(f"\n[fm] === SEARCH: '{query}' (pincode={sess.get('pincode') or 'MISSING'}) ===")
    t_start = time.time()

    page_uri = _search_page_uri(query)
    body = {
        "pageUri": page_uri,
        "pageContext": {
            "trackingContext": {"context": {"eVar51": "config", "eVar61": "search"}},
            "networkSpeed": 10000,
        },
        "requestContext": {
            "type": "BROWSE_PAGE",
            "ssid": _request_id(),
            "sqid": _request_id(),
        },
    }
    loc = _location_context(sess)
    if loc:
        body["locationContext"] = loc
    headers = _api_headers(sess)

    try:
        # 5 s timeout: HYPERLOCAL BFF is fast when it works; if the endpoint or
        # auth is wrong it returns quickly too. Keeping this short prevents FM
        # from blocking the asyncio.gather that also runs Blinkit/Zepto searches.
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
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

    Uses the Rome BFF /api/5/cart/browse endpoint captured from Flipkart web.
    Exposed as a batch API (same shape as Zepto/Instamart) for server-side cart
    routing.
    """
    if not items:
        return {"success": True, "items": []}

    sess = _get_fm_session(user_id)
    if not is_session_valid(user_id):
        return {"success": False, "reason": "no valid session"}

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

            listing_id = str(
                item.get("listing_id") or item.get("listingId") or
                item.get("store_product_id") or pid
            )
            page_uri = item.get("page_uri") or _search_page_uri(
                str(item.get("search_query") or item.get("name") or "")
            )
            body = {
                "browseContext": {
                    "marketplace": _FM_SEARCH_MARKETPLACE,
                    "listings": [listing_id],
                    "store": _FM_SEARCH_STORE,
                },
                "browseCartContext": {
                    "cartContext": {
                        listing_id: {
                            "productId": pid,
                            "quantity": qty,
                            "cashifyDiscountApplied": False,
                            "vulcanDiscountApplied": False,
                        },
                    },
                    "pageType": "SearchPage",
                    "pageUri": page_uri,
                },
            }
            try:
                resp = await client.post(
                    _FM_CART_URL, json=body, headers=headers
                )
                if resp.status_code in (200, 201):
                    ok_any = True
                    item_results.append({"success": True, "count_added": qty})
                    print(f"[fm] cart add OK pid={pid} listing={listing_id} qty={qty}")
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
