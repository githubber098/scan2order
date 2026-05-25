"""backend/stores/zepto.py - Zepto API client.

Stateless httpx-based functions. No Playwright, no browser context.
Cookies and localStorage come from Redis (passed as plain dicts by the caller).

Public API:
    build_session(cookies, local_storage) -> dict
        Normalizes raw cookie/LS dicts into a session dict with all tokens.

    search_item_api(query, session) -> list[dict]
        Search Zepto via the signed bff-gateway API.

    add_all_to_cart_api(items, session, user_id=None) -> dict
        Batch cart-add via bulk-widget-data.
"""

import json
import time
import uuid
from urllib.parse import unquote

import httpx

from stores.zepto_sign import compute_signature

_API_BASE = "https://bff-gateway.zepto.com"
_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36"
)

# Compatible-components string captured from a live session (v15.22.2).
# Sent verbatim on every API call so the gateway knows which feature flags
# this client supports. Not signed - safe to update independently.
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


# ── Session building ────────────────────────────────────────────

def build_session(cookies: dict, local_storage: dict) -> dict:
    """Build a Zepto session dict from raw cookies + localStorage from Redis.

    Cookie names on Zepto are inconsistent (UPPER-DASH, camelCase, snake_case).
    We normalize all keys to lowercase alphanumeric before lookup so
    "XSRF-TOKEN", "xsrfToken", and "xsrf_token" all resolve to "xsrftoken".

    The XSRF-TOKEN cookie value is URL-encoded on the wire (%3A -> :, etc).
    We unquote it because the signing algorithm and the x-xsrf-token header
    both expect the decoded form.

    Returns:
        dict with keys: cookies, local_storage, xsrf_token, csrf_secret,
        device_id, session_id, store_id, store_ids, store_etas.
        Empty string for any token we couldn't find.
    """
    def normkey(k: str) -> str:
        return "".join(ch.lower() for ch in k if ch.isalnum())

    # Build a normalized lookup. Cookies win over localStorage on conflict
    # (cookies are authoritative for these tokens).
    bag: dict[str, str] = {}
    for k, v in local_storage.items():
        bag[normkey(k)] = v or ""
    for k, v in cookies.items():
        bag[normkey(k)] = unquote(v) if v and "%" in v else (v or "")

    xsrf_token = bag.get("xsrftoken") or ""
    csrf_secret = bag.get("csrfsecret") or ""
    device_id = bag.get("deviceid") or ""
    session_id = bag.get("sessionid") or ""

    # Build store_id, store_ids, store_etas from the serviceability cookie.
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

    return {
        "cookies": cookies,
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
    """Human-readable summary of which Zepto tokens were found."""
    return ", ".join(
        f"{k}={'found' if sess.get(k) else 'MISSING'}"
        for k in ("xsrf_token", "csrf_secret", "device_id", "session_id", "store_id")
    )


# ── Internal HTTP helpers ────────────────────────────────────────

def _zepto_api_headers(sess: dict, request_id: str, sig: str, tz_hash: str) -> dict:
    """Common headers for any Zepto API call."""
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
    """Parse all Set-Cookie headers from a response into a name->value dict."""
    out: dict[str, str] = {}
    for key, value in resp.headers.multi_items():
        if key.lower() != "set-cookie":
            continue
        name_value = value.split(";", 1)[0].strip()
        if "=" in name_value:
            name, val = name_value.split("=", 1)
            out[name.strip()] = val.strip()
    return out


async def _zepto_api_post(
    path: str,
    body: dict,
    sess: dict,
    user_id: str | None = None,
) -> dict | None:
    """POST to a Zepto API endpoint with the full signing dance.

    Manages cookies manually via an explicit Cookie request header (not httpx's
    jar) because jar deduplication breaks the 401->refresh->retry flow.

    On 401, refreshes via /api/auth/refresh-auth, then retries with new cookies.
    If user_id is provided, persists refreshed cookies back to Redis.

    Returns parsed JSON on 200, or None on failure.
    """
    if not sess.get("xsrf_token") or not sess.get("device_id"):
        print(f"[zepto] API: missing xsrf_token or device_id. "
              f"Status: {_diag_token_status(sess)}")
        return None

    cookie_jar: dict[str, str] = dict(sess.get("cookies") or {})

    def cookie_header() -> str:
        return "; ".join(f"{k}={v}" for k, v in cookie_jar.items() if v)

    request_id = str(uuid.uuid4())
    body_str = json.dumps(body, separators=(",", ":"))
    sig, tz_hash = compute_signature(
        method="post",
        path_with_query=path,
        request_id=request_id,
        device_id=sess["device_id"],
        xsrf_token=sess["xsrf_token"],
        body=body_str,
    )
    headers = _zepto_api_headers(sess, request_id, sig, tz_hash)
    headers["cookie"] = cookie_header()
    url = _API_BASE + path

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, content=body_str, headers=headers)

            if resp.status_code == 401:
                # accessToken expired - refresh and retry.
                refresh_request_id = str(uuid.uuid4())
                refresh_sig, refresh_tz = compute_signature(
                    method="post",
                    path_with_query="/api/auth/refresh-auth",
                    request_id=refresh_request_id,
                    device_id=sess["device_id"],
                    xsrf_token=sess["xsrf_token"],
                    body="{}",
                )
                refresh_headers = _zepto_api_headers(
                    sess, refresh_request_id, refresh_sig, refresh_tz
                )
                refresh_headers["cookie"] = cookie_header()

                print("[zepto] refreshing accessToken")
                refresh_resp = await client.post(
                    "https://www.zepto.com/api/auth/refresh-auth",
                    content="{}",
                    headers=refresh_headers,
                )
                if refresh_resp.status_code != 200:
                    print(
                        f"[zepto] token refresh HTTP {refresh_resp.status_code}: "
                        f"{refresh_resp.text[:150]}"
                    )
                    return None

                new_cookies = _parse_set_cookies(refresh_resp)
                print(f"[zepto] refresh: got {len(new_cookies)} new cookies: "
                      f"{sorted(new_cookies.keys())}")

                old_token = cookie_jar.get("accessToken", "")
                new_token = new_cookies.get("accessToken")
                if new_token is None:
                    print("[zepto] refresh: response did not set accessToken - "
                          "refreshToken likely expired. Re-login Zepto to fix.")
                    return None
                if not new_token:
                    print("[zepto] refresh: server set accessToken to empty string - "
                          "session rejected. Re-login Zepto to fix.")
                    return None

                cookie_jar.update(new_cookies)
                if cookie_jar.get("accessToken") == old_token:
                    print("[zepto] refresh: accessToken unchanged after merge")
                    return None

                print("[zepto] token refresh OK; retrying")

                # Persist refreshed cookies back to Redis if user_id provided.
                if user_id:
                    try:
                        from storage.user_store import connect_store, get_store_local_storage
                        ls = get_store_local_storage(user_id, "zepto")
                        connect_store(user_id, "zepto", dict(cookie_jar), ls)
                        print("[zepto] refreshed cookies persisted to Redis")
                    except Exception as e:
                        print(f"[zepto] Redis persist failed: {e}")

                # Retry with fresh signature.
                new_request_id = str(uuid.uuid4())
                new_sig, new_tz = compute_signature(
                    method="post",
                    path_with_query=path,
                    request_id=new_request_id,
                    device_id=sess["device_id"],
                    xsrf_token=sess["xsrf_token"],
                    body=body_str,
                )
                new_headers = _zepto_api_headers(sess, new_request_id, new_sig, new_tz)
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


# ── Public API ──────────────────────────────────────────────────

async def search_item_api(query: str, session: dict) -> list[dict]:
    """Search Zepto via the signed bff-gateway API.

    Args:
        query: search term
        session: dict from build_session() containing xsrf_token, device_id, etc.

    Returns:
        List of product dicts, each with:
            name, price, sale_price, unit, image_url, product_id,
            store_product_id, mrp_paise, discount_applicable
        Returns [] on failure.
    """
    if not session.get("xsrf_token"):
        print(f"[zepto] search_item_api: no xsrf_token. "
              f"Status: {_diag_token_status(session)}")
        return []

    print(f"\n[zepto] === API SEARCH: '{query}' ===")
    t_start = time.time()

    body = {
        "query": query,
        "pageNumber": 0,
        "intentId": str(uuid.uuid4()),
        "mode": "AUTOSUGGEST",
        "userSessionId": session.get("session_id", ""),
    }
    data = await _zepto_api_post("/user-search-service/api/v3/search", body, session)
    elapsed_ms = int((time.time() - t_start) * 1000)
    if not data:
        return []

    products: list[dict] = []
    for layout_entry in (data.get("layout") or []):
        items = (
            (((layout_entry.get("data") or {}).get("resolver") or {})
             .get("data") or {}).get("items") or []
        )
        for slot in items:
            pr = slot.get("productResponse") or {}
            if not pr or pr.get("outOfStock"):
                continue

            variant = pr.get("productVariant") or {}
            variant_id = variant.get("id") or ""
            if not variant_id:
                continue

            # Prices in paise - divide by 100.
            sale_paise = pr.get("discountedSellingPrice") or 0
            mrp_paise = pr.get("mrp") or 0
            sale = round(sale_paise / 100, 2) if sale_paise else 0.0
            mrp = round(mrp_paise / 100, 2) if mrp_paise else sale

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

            products.append({
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
                "app": "zepto",
                "app_name": "Zepto",
            })
            if len(products) >= 8:
                break
        if len(products) >= 8:
            break

    print(f"[zepto] === API RESULT: {len(products)} products ({elapsed_ms}ms) ===\n")
    return products


async def add_all_to_cart_api(
    items: list[dict],
    session: dict,
    user_id: str | None = None,
) -> dict:
    """Batch add items to the Zepto cart via /cfs/api/v1/bulk-widget-data.

    The bulk-widget-data endpoint accepts the full desired cart state and
    returns post-ATC widget data. It treats HTTP 200 as success for all items.

    Items missing store_product_id are enriched first via a preflight call
    to /cart-service/api/v1/cart/product-detail.

    Args:
        items: list of product dicts, each must have product_id and count
        session: dict from build_session()
        user_id: passed to _zepto_api_post for Redis token refresh

    Returns:
        {"success": True,  "items": [{success, count_added}, ...]}  on success
        {"success": False, "reason": str}                            on failure
    """
    if not items:
        return {"success": True, "items": []}

    if not session.get("xsrf_token"):
        return {
            "success": False,
            "reason": f"no xsrf_token; {_diag_token_status(session)}",
        }

    print(f"\n[zepto] === API ADD (batch): {len(items)} items ===")
    t_start = time.time()

    # Step 1: Enrich items missing storeProductId via product-detail lookup.
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
            "/cart-service/api/v1/cart/product-detail",
            enrich_body,
            session,
            user_id=user_id,
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
                        "discount_applicable": bool(
                            entry.get("isDiscountApplicable", True)
                        ),
                    }
            for item in needs_enrich:
                pid = str(item["product_id"])
                if pid in enrich_map:
                    item.update(enrich_map[pid])
        else:
            print("[zepto]   product-detail enrich failed - continuing without storeProductId")

    # Step 2: Build cartProducts payload.
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
        "widgets": [
            {"id": 100003339, "pageIdentifier": "GLOBAL", "widgetDisplayType": "FOOTER"}
        ],
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

    data = await _zepto_api_post(
        "/cfs/api/v1/bulk-widget-data", body, session, user_id=user_id
    )
    elapsed_ms = int((time.time() - t_start) * 1000)
    if not data:
        return {"success": False, "reason": "API call failed"}

    # HTTP 200 means all items were accepted at their requested quantities.
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
    print(
        f"[zepto] === API ADD batch: {added}/{len(items)} "
        f"submitted ({elapsed_ms}ms) ===\n"
    )
    return {"success": True, "items": item_results}
