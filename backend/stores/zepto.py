"""stores/zepto.py - Zepto httpx store module.

Adapted from scan2order2/automators/zepto.py.
All Playwright dependencies removed. Session sourced from SQLite (or MySQL when
MYSQL_URL is set) via storage.user_store.
Cookie-only auth: xsrf_token, device_id, session_id, store_id expected in cookies.
"""

import json
import time
import uuid
from urllib.parse import unquote

import httpx

from storage.user_store import get_store_session, update_store_cookies
from stores._common import MOBILE_UA as _MOBILE_UA
from stores.zepto_sign import compute_signature

APP_NAME = "zepto"
DISPLAY_NAME = "Zepto"
BASE_URL = "https://www.zeptonow.com"
_API_BASE = "https://bff-gateway.zepto.com"

# Compatible-components string captured from a live session (v15.22.2).
_COMPATIBLE_COMPONENTS = (
    "EXTERNAL_COUPONS,BUNDLE,MULTI_SELLER_ENABLED,ROLLUPS,SCHEDULED_DELIVERY,"
    "HOMEPAGE_V2,NEW_ETA_BANNER,VERTICAL_FEED_PRODUCT_GRID,"
    "AUTOSUGGESTION_PAGE_ENABLED,AUTOSUGGESTION_PIP,AUTOSUGGESTION_AD_PIP,"
    "BOTTOM_NAV_FULL_ICON,COUPON_WIDGET_CART_REVAMP,DELIVERY_UPSELLING_WIDGET,"
    "MARKETPLACE_CATEGORY_GRID,NO_PLATFORM_CHECK_ENABLED_V2,SUPER_SAVER:1,"
    "SUPERSTORE_V1,PROMO_CASH:0,24X7_ENABLED_V1,TABBED_CAROUSEL_V2,HP_V4_FEED,"
    "WIDGET_BASED_ETA,PC_REVAMP_1,NO_COST_EMI_V1,PRE_SEARCH,ITEMISATION_ENABLED,"
    "ZEPTO_PASS:5,BACHAT_FOR_ALL,SAMPLING_UPSELL_CAMPAIGN,DISCOUNTED_ADDONS_ENABLED,"
    "UPSELL_COUPON_SS:0,ENABLE_FLOATING_CART_BUTTON,FASHION_REVAMP,WIDGET_RESTRUCTURE,"
    "MULTITAB_V2,NEW_ROLLUPS_ENABLED,RERANKING_QCL_RELATED_PRODUCTS,PLP_ON_SEARCH,"
    "PAAN_BANNER_WIDGETIZED,ROLLUPS_UOM,DYNAMIC_FILTERS,PHARMA_ENABLED,"
    "AUTOSUGGESTION_RECIPE_PIP,SEARCH_FILTERS_V1,QUERY_DESCRIPTION_WIDGET,"
    "MEDS_WITH_SIMILAR_SALT_WIDGET,SEARCH_RELOOK,FILTER_ATTRIBUTES,LMS_BROWSE,"
    "SEARCH_PRODUCT_GRID_V2,FASHION_REVAMP,L3_UNDERSTANDING_LAYOUT,NEW_BILL_INFO,"
    "RE_PROMISE_ETA_ORDER_SCREEN_ENABLED,SUPERSTORE_V1,"
    "MANUALLY_APPLIED_DELIVERY_FEE_RECEIVABLE,MARKETPLACE_REPLACEMENT,ZEPTO_PASS:5,"
    "CART_REDESIGN_ENABLED,SHIPMENT_WIDGETIZATION_ENABLED,TABBED_CAROUSEL_V2,"
    "24X7_ENABLED_V1,PROMO_CASH:0,HOMEPAGE_V2,SUPER_SAVER:1,"
    "NO_PLATFORM_CHECK_ENABLED_V2,HP_V4_FEED,GIFT_CARD,SCLP_ADD_MONEY,"
    "GIFTING_ENABLED,OFSE,WIDGET_BASED_ETA,PC_REVAMP_1,NEW_ETA_BANNER,"
    "NO_COST_EMI_V1,ITEMISATION_ENABLED,SWAP_AND_SAVE_ON_CART,WIDGET_RESTRUCTURE,"
    "PRICING_CAMPAIGN_ID,BACHAT_FOR_ALL,TABBED_CAROUSEL_V3,CART_LMS:2,"
    "SAMPLING_UPSELL_CAMPAIGN,DISCOUNTED_ADDONS_ENABLED,UPSELL_COUPON_SS:0,"
    "SIZE_EXCHANGE_ENABLED,ENABLE_FLOATING_CART_BUTTON,SAMPLING_V3,HYBRID_CAMPAIGN,"
)


def is_session_valid(user_id: str) -> bool:
    sess = _get_zepto_session(user_id)
    has_token = bool(sess.get("xsrf_token") and sess.get("device_id"))
    if has_token and not sess.get("store_id"):
        # Zepto requires a store_id (set from the serviceability cookie when the
        # user saves a delivery address inside the Zepto app or website).
        # Without it the BFF search API returns an empty layout array — searches
        # appear to succeed but return zero products.  This is NOT a login failure
        # so we still return True, but we log a clear warning.
        print(f"[zepto] WARNING: user {user_id[:8]} is authenticated but has no "
              f"store_id — make sure a delivery address is saved in the Zepto app "
              f"(Settings → Address) so the serviceability cookie is set.")
    return has_token


def _hunt_store_id(local_storage: dict, raw_cookies: dict) -> str:
    """Recursively search every stored JSON blob for a 'storeId' value.

    Zepto scatters the active store id across several persisted keys
    (userAddresses, page-layout state, recent-search state, cart state).
    When the serviceability cookie is empty, any of these may still carry
    a usable storeId. Returns the first UUID-shaped storeId found, or "".
    """
    import re
    _UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                          r"[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

    def _search(obj) -> str:
        if isinstance(obj, dict):
            # Prefer an explicit storeId key with a UUID-shaped value
            for key in ("storeId", "store_id", "primaryStoreId"):
                v = obj.get(key)
                if isinstance(v, str) and _UUID_RE.match(v):
                    return v
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


def _get_zepto_session(user_id: str) -> dict:
    """Pull Zepto tokens from the user_store cookies dict (SQLite/MySQL backed).

    Applies same normalization as scan2order2's _get_zepto_session.
    Cookie-only path; localStorage fallback is a TODO if tokens are missing.
    """
    stored = get_store_session(user_id, APP_NAME)
    raw_cookies = stored.get("cookies", {})
    local_storage = stored.get("local_storage", {})

    if not raw_cookies:
        return {}

    def normkey(k: str) -> str:
        return "".join(ch.lower() for ch in k if ch.isalnum())

    bag: dict[str, str] = {}
    for k, v in local_storage.items():
        bag[normkey(k)] = v or ""
    for k, v in raw_cookies.items():
        bag[normkey(k)] = unquote(v) if v and "%" in v else (v or "")

    xsrf_token = bag.get("xsrftoken") or ""
    csrf_secret = bag.get("csrfsecret") or ""
    device_id = bag.get("deviceid") or ""
    session_id = bag.get("sessionid") or ""

    store_id = ""
    store_ids = ""
    store_etas = "{}"
    serviceability = bag.get("serviceability") or ""
    if serviceability:
        try:
            sa = json.loads(serviceability)
            primary = sa.get("primaryStore") or {}
            secondary = sa.get("secondaryStore") or {}
            store_id = primary.get("storeId") or ""
            ids: list[str] = []
            etas: dict = {}
            for s in (primary, secondary):
                sid = s.get("storeId") if s else None
                if sid:
                    ids.append(sid)
                    etas[sid] = s.get("etaInMinutes", -1) if s else -1
            store_ids = ",".join(ids)
            store_etas = json.dumps(etas, separators=(",", ":"))
        except Exception:
            pass

    # Fallback: the serviceability cookie is frequently saved empty
    # (e.g. {"timeSaved": ...}) even when the user has a delivery address.
    # The storeId still lives in OTHER stored blobs — userAddresses, the
    # page-layout state, the search/cart state in localStorage. Recursively
    # hunt for any "storeId" in every stored JSON value as a last resort.
    # Only runs when the primary path found nothing, so it can't regress.
    if not store_id:
        found = _hunt_store_id(local_storage, raw_cookies)
        if found:
            store_id = found
            store_ids = found
            print(f"[zepto] store_id recovered from stored blobs: {store_id}")

    # ── TEMP DIAGNOSTIC: show what's actually in the stored Zepto session so
    # we can find where (if anywhere) a usable storeId lives.
    try:
        print(f"[zepto][DIAG] cookie keys = {sorted(raw_cookies.keys())}")
        print(f"[zepto][DIAG] localStorage keys = {sorted(local_storage.keys())}")
        print(f"[zepto][DIAG] serviceability raw = {serviceability[:400]!r}")
        for _k in ("userAddresses", "user-position", "userPosition",
                   "selectedAddress", "header-store"):
            _v = local_storage.get(_k)
            if _v:
                print(f"[zepto][DIAG] localStorage[{_k}] = {str(_v)[:500]}")
        print(f"[zepto][DIAG] resolved store_id = {store_id!r}")
    except Exception as _e:
        print(f"[zepto][DIAG] dump failed: {_e}")

    return {
        "cookies": raw_cookies,
        "local_storage": local_storage,
        "xsrf_token": xsrf_token,
        "csrf_secret": csrf_secret,
        "device_id": device_id,
        "session_id": session_id,
        "store_id": store_id,
        "store_ids": store_ids,
        "store_etas": store_etas,
    }


def _diag_token_status(sess: dict) -> str:
    return ", ".join(
        f"{k}={'found' if sess.get(k) else 'MISSING'}"
        for k in ("xsrf_token", "csrf_secret", "device_id", "session_id", "store_id")
    )


def _zepto_api_headers(sess: dict, request_id: str, sig: str, tz_hash: str) -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "app_sub_platform": "WEB",
        "app_version": "15.22.2",
        "appversion": "15.22.2",
        "auth_from_cookie": "true",
        "auth_revamp_flow": "v2",
        "compatible_components": _COMPATIBLE_COMPONENTS,
        "content-type": "application/json",
        "device_id": sess.get("device_id", ""),
        "deviceid": sess.get("device_id", ""),
        "marketplace_type": "SUPER_SAVER",
        "origin": "https://www.zepto.com",
        "platform": "WEB",
        "priority": "u=1, i",
        "referer": "https://www.zepto.com/",
        "request-signature": sig,
        "request_id": request_id,
        "requestid": request_id,
        "session_id": sess.get("session_id", ""),
        "sessionid": sess.get("session_id", ""),
        "source": "DIRECT",
        "store_etas": sess.get("store_etas", "{}"),
        "store_id": sess.get("store_id", ""),
        "store_ids": sess.get("store_ids", ""),
        "storeid": sess.get("store_id", ""),
        "tenant": "ZEPTO",
        "user-agent": _MOBILE_UA,
        "x-csrf-secret": sess.get("csrf_secret", ""),
        "x-timezone": tz_hash,
        "x-without-bearer": "true",
        "x-xsrf-token": sess.get("xsrf_token", ""),
    }


def _parse_set_cookies(resp: httpx.Response) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in resp.headers.multi_items():
        if key.lower() != "set-cookie":
            continue
        name_value = value.split(";", 1)[0].strip()
        if "=" in name_value:
            name, val = name_value.split("=", 1)
            out[name.strip()] = val.strip()
    return out


async def _zepto_api_post(path: str, body: dict, sess: dict,
                          user_id: str) -> dict | None:
    """POST to a Zepto API endpoint with the full signing dance.

    Manages cookies manually to avoid httpx jar dedup issues on token refresh.
    On 401, refreshes via /api/auth/refresh-auth and retries.
    Persists refreshed cookies back via user_store.update_store_cookies.
    """
    if not sess.get("xsrf_token") or not sess.get("device_id"):
        print(f"[zepto] API: missing xsrf_token or device_id. {_diag_token_status(sess)}")
        return None

    cookie_jar: dict[str, str] = dict(sess.get("cookies") or {})

    def cookie_header() -> str:
        return "; ".join(f"{k}={v}" for k, v in cookie_jar.items() if v)

    request_id = str(uuid.uuid4())
    body_str = json.dumps(body, separators=(",", ":"))
    sig, tz_hash = compute_signature(
        method="post", path_with_query=path, request_id=request_id,
        device_id=sess["device_id"], xsrf_token=sess["xsrf_token"],
        body=body_str,
    )
    headers = _zepto_api_headers(sess, request_id, sig, tz_hash)
    headers["cookie"] = cookie_header()
    url = _API_BASE + path

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, content=body_str, headers=headers)

            if resp.status_code == 401:
                refresh_rid = str(uuid.uuid4())
                refresh_sig, refresh_tz = compute_signature(
                    method="post",
                    path_with_query="/api/auth/refresh-auth",
                    request_id=refresh_rid,
                    device_id=sess["device_id"],
                    xsrf_token=sess["xsrf_token"], body="{}",
                )
                refresh_headers = _zepto_api_headers(sess, refresh_rid, refresh_sig, refresh_tz)
                refresh_headers["cookie"] = cookie_header()

                print(f"[zepto] refreshing accessToken")
                refresh_resp = await client.post(
                    "https://www.zepto.com/api/auth/refresh-auth",
                    content="{}", headers=refresh_headers,
                )
                if refresh_resp.status_code != 200:
                    print(f"[zepto] token refresh HTTP {refresh_resp.status_code}")
                    return None

                new_cookies = _parse_set_cookies(refresh_resp)
                new_token = new_cookies.get("accessToken")
                if not new_token:
                    print(f"[zepto] refresh: no new accessToken issued - re-login required")
                    return None

                cookie_jar.update(new_cookies)
                update_store_cookies(user_id, APP_NAME, new_cookies)
                print(f"[zepto] token refresh OK")

                new_rid = str(uuid.uuid4())
                new_sig, new_tz = compute_signature(
                    method="post", path_with_query=path,
                    request_id=new_rid,
                    device_id=sess["device_id"],
                    xsrf_token=sess["xsrf_token"], body=body_str,
                )
                new_headers = _zepto_api_headers(sess, new_rid, new_sig, new_tz)
                new_headers["cookie"] = cookie_header()
                resp = await client.post(url, content=body_str, headers=new_headers)

            if resp.status_code != 200:
                print(f"[zepto] API HTTP {resp.status_code} on {path}")
                if resp.status_code in (400, 401, 403):
                    print(f"[zepto]   response: {resp.text[:200]}")
                return None

            return resp.json()
    except Exception as e:
        print(f"[zepto] API exception on {path}: {e}")
        return None


def _extract_product(pr: dict) -> dict | None:
    """Extract one canonical product dict from a Zepto productResponse object.

    Returns None if the product is out-of-stock or missing required fields.
    """
    if not pr or pr.get("outOfStock"):
        return None

    variant = pr.get("productVariant") or {}
    variant_id = variant.get("id") or ""
    if not variant_id:
        return None

    sale_paise = pr.get("discountedSellingPrice") or 0
    mrp_paise = pr.get("mrp") or 0
    sale = round(sale_paise / 100, 2) if sale_paise else 0.0
    mrp = round(mrp_paise / 100, 2) if mrp_paise else sale
    if sale <= 0 and mrp <= 0:
        return None

    images = variant.get("images") or []
    image_path = (images[0] or {}).get("path", "") if images else ""
    image_url = (
        f"https://cdn.zeptonow.com/production///tr:w-300,ar-100-100/{image_path}"
        if image_path else ""
    )

    store_product_id = pr.get("id") or ""
    discount_applicable = bool(
        (pr.get("product") or {}).get("discountApplicable", True)
    )

    return {
        "name": ((pr.get("product") or {}).get("name") or "")[:120],
        "price": mrp,
        "sale_price": sale,
        "unit": variant.get("formattedPacksize") or "",
        "image_url": image_url,
        "product_id": variant_id,
        "max_qty": variant.get("maxAllowedQuantity") or 99,
        "store_product_id": store_product_id,
        "mrp_paise": mrp_paise,
        "discount_applicable": discount_applicable,
        "app": APP_NAME,
        "app_name": DISPLAY_NAME,
    }


def _walk_layout(layout: list) -> list[dict]:
    """Walk Zepto's layout array and extract product cards.

    Tries the known deep path first:
      layout[].data.resolver.data.items[].productResponse

    If that yields nothing, falls back to a recursive search for any dict
    that has a "productResponse" or "productVariant" key — this handles
    minor structural changes in the BFF API response.
    """
    products: list[dict] = []

    # ── Primary path (verified against API v3 responses) ─────────────────────
    for entry in layout:
        items = (
            (((entry.get("data") or {}).get("resolver") or {}).get("data") or {})
            .get("items") or []
        )
        for slot in items:
            p = _extract_product(slot.get("productResponse") or {})
            if p:
                products.append(p)
                if len(products) >= 8:
                    return products

    if products:
        return products

    # ── Fallback: recursive BFS for "productResponse" keys ───────────────────
    def _bfs(obj, depth=0):
        if depth > 8 or len(products) >= 8:
            return
        if isinstance(obj, dict):
            if "productResponse" in obj:
                p = _extract_product(obj["productResponse"] or {})
                if p:
                    products.append(p)
            for v in obj.values():
                _bfs(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _bfs(item, depth + 1)

    _bfs(layout)
    return products


async def search_item_api(user_id: str, query: str) -> list[dict]:
    """Search via Zepto's BFF API. Returns [] on any failure.

    Common reason for empty results even when authenticated:
      store_id is missing (no delivery address saved in Zepto app).
      The BFF API returns an empty layout when store_id is not supplied.
      Fix: open Zepto app → Settings → Address and save a delivery address.
    """
    sess = _get_zepto_session(user_id)
    if not sess.get("xsrf_token"):
        print(f"[zepto] search_item_api: no xsrf_token. {_diag_token_status(sess)}")
        return []

    if not sess.get("store_id"):
        print(f"[zepto] search_item_api: store_id is empty — search will likely "
              f"return no results. Ensure a delivery address is saved in Zepto app.")

    print(f"\n[zepto] === API SEARCH: '{query}' (store_id={sess.get('store_id') or 'MISSING'}) ===")
    t_start = time.time()

    body = {
        "query": query,
        "pageNumber": 0,
        "intentId": str(uuid.uuid4()),
        "mode": "AUTOSUGGEST",
        "userSessionId": sess.get("session_id", ""),
    }
    data = await _zepto_api_post(
        "/user-search-service/api/v3/search", body, sess, user_id
    )
    elapsed_ms = int((time.time() - t_start) * 1000)
    if not data:
        return []

    layout = data.get("layout") or []
    if not layout:
        # Log the top-level keys so we can diagnose structure changes.
        top_keys = list(data.keys())[:10]
        print(f"[zepto] search: API returned no layout. Top-level keys: {top_keys}")
        return []

    products = _walk_layout(layout)

    if not products:
        # Log layout entry types to help diagnose structure changes.
        entry_types = [e.get("type") or e.get("widgetType") or "?"
                       for e in layout[:5]]
        print(f"[zepto] search: layout has {len(layout)} entries but no products extracted. "
              f"Entry types (first 5): {entry_types}")

    print(f"[zepto] === API RESULT: {len(products)} products ({elapsed_ms}ms) ===\n")
    return products


async def add_all_to_cart_api(user_id: str, items: list[dict]) -> dict:
    """Batched cart-add via /cfs/api/v1/bulk-widget-data.

    Enriches missing store_product_ids via product-detail preflight.
    Returns {success: True, items: [{success, count_added}]} or {success: False, reason: str}.
    """
    if not items:
        return {"success": True, "items": []}

    sess = _get_zepto_session(user_id)
    if not sess.get("xsrf_token"):
        return {"success": False,
                "reason": f"no xsrf_token; {_diag_token_status(sess)}"}

    print(f"\n[zepto] === API ADD (batch): {len(items)} items ===")
    t_start = time.time()

    needs_enrich = [
        i for i in items
        if not i.get("store_product_id") and i.get("product_id")
    ]
    if needs_enrich:
        enrich_body = {
            "cartProducts": [
                {"productVariantId": str(i["product_id"]), "quantity": 1}
                for i in needs_enrich
            ]
        }
        enrich_data = await _zepto_api_post(
            "/cart-service/api/v1/cart/product-detail", enrich_body, sess, user_id
        )
        if enrich_data:
            enrich_map: dict[str, dict] = {}
            for entry in (enrich_data.get("cartProductResponse") or []):
                vid = (
                    (entry.get("productVariant") or {}).get("id")
                    or entry.get("productVariantId") or ""
                )
                if vid:
                    enrich_map[vid] = {
                        "store_product_id": entry.get("id") or "",
                        "mrp_paise": entry.get("mrp") or 0,
                        "discount_applicable": bool(entry.get("isDiscountApplicable", True)),
                    }
            for item in needs_enrich:
                pid = str(item["product_id"])
                if pid in enrich_map:
                    item.update(enrich_map[pid])

    cart_products = []
    last_variant_id = ""
    for item in items:
        pid = str(item.get("product_id") or "")
        if not pid:
            continue
        try:
            qty = max(1, min(99, int(item.get("count") or 1)))
        except (TypeError, ValueError):
            qty = 1

        store_pid = item.get("store_product_id") or ""
        mrp_paise = item.get("mrp_paise") or 0
        if not mrp_paise:
            try:
                mrp_paise = int(round(float(item.get("price") or 0) * 100))
            except (TypeError, ValueError):
                mrp_paise = 0
        discount_applicable = bool(item.get("discount_applicable", True))

        cp: dict = {
            "productVariantId": pid,
            "quantity": qty,
            "isDiscountApplicable": discount_applicable,
            "isAddedToGiftBag": False,
        }
        if store_pid:
            cp["storeProductId"] = store_pid
        if mrp_paise:
            cp["mrp"] = mrp_paise
        cart_products.append(cp)
        last_variant_id = pid

    if not cart_products:
        return {"success": False, "reason": "no valid items"}

    body = {
        "widgets": [{"id": 100003339, "pageIdentifier": "GLOBAL",
                     "widgetDisplayType": "FOOTER"}],
        "cartProducts": cart_products,
        "enrichCall": False,
        "showAllOffers": False,
        "campaignIds": [],
        "couponIds": [],
        "alreadyShownNudgeThresholdsV2": [],
        "currentUpsellState": {"id": "free_delivery_upsell_id", "status": "LOCKED"},
        "availedCampaignProducts": [],
        "removedCampaigns": [],
        "cartId": "",
        "pageContext": {"queryTerm": "", "pageType": "search", "productGridSize": 3},
        "atcProductVariantId": last_variant_id,
    }

    data = await _zepto_api_post("/cfs/api/v1/bulk-widget-data", body, sess, user_id)
    elapsed_ms = int((time.time() - t_start) * 1000)
    if not data:
        return {"success": False, "reason": "API call failed"}

    item_results = []
    for item in items:
        pid = str(item.get("product_id") or "")
        if not pid:
            item_results.append({"success": False, "reason": "no product_id"})
            continue
        try:
            qty = max(1, min(99, int(item.get("count") or 1)))
        except (TypeError, ValueError):
            qty = 1
        item_results.append({"success": True, "count_added": qty})

    added = sum(1 for r in item_results if r.get("success"))
    print(f"[zepto] === API ADD batch: {added}/{len(items)} submitted ({elapsed_ms}ms) ===\n")
    return {"success": True, "items": item_results}


def checkout_url() -> str:
    return "https://www.zepto.com/?cart=open"
