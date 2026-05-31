"""stores/blinkit.py - Blinkit search + cart module.

Search strategy (in order):
  1. POST /v1/layout/search via httpx — fast (~200ms) but requires the
     exact Cloudflare session cookies from a fresh page load; returns
     "location not serviceable" without them. Succeeds when api_auth_key is
     cached AND a fresh Cloudflare session is available.
  2. __NEXT_DATA__ SSR — dead; kept as a stub.
  3. Playwright response interception — reliable (~2.5s). Navigates to the
     search page, intercepts the React app's own successful /v1/layout/search
     response, and caches the derived auth_key for future Strategy 1 attempts.

Cart add via Blinkit's /v2/client/user_cart/ API.
Auth cookie: gr_1_accessToken (stored via user_store.connect_store).
Location: gr_1_lat/gr_1_lon cookies + merchant_id (captured during auth).
"""

import re
import json
import time
from urllib.parse import quote, unquote

import httpx

from storage.user_store import get_store_cookies, update_store_cookies
from stores._common import MOBILE_UA as _MOBILE_UA

APP_NAME = "blinkit"
DISPLAY_NAME = "Blinkit"
BASE_URL = "https://blinkit.com"

# Headers used for the direct JSON search API and for cart operations.
# auth_key is added per-call once we have the token.
_API_HEADERS_BASE = {
    "User-Agent": _MOBILE_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "app_client": "web",
    "web_app_version": "3000",
    "Content-Type": "application/json",
}

# Headers for the HTML SSR fallback (no auth_key needed for HTML fetch).
_SSR_HEADERS = {
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


def _parse_api_response(data: dict) -> list[dict]:
    """Parse Blinkit v2 search JSON into the canonical product list format.

    Response shape (as of 2025):
      {"response": {"objects": [{"type": "product", "data": {...}}, ...]}}
    """
    objects = (data.get("response") or {}).get("objects") or []
    products = []
    for obj in objects:
        if obj.get("type") != "product":
            continue
        p = obj.get("data") or {}
        name = p.get("name") or p.get("product_name") or ""
        if not name:
            continue
        if p.get("is_out_of_stock") or p.get("outOfStock"):
            continue
        sale = float(p.get("selling_price") or p.get("offer_price") or p.get("price") or 0)
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
    return products


def _parse_ssr_response(html: str) -> list[dict]:
    """Parse __NEXT_DATA__ from HTML response as SSR fallback.

    Returns [] if the tag is absent, empty, or the product list is not found
    in any of the known pageProps paths.
    """
    data = _extract_next_data(html)
    if not data:
        print(f"[blinkit] SSR: __NEXT_DATA__ tag not found in response")
        return []

    page_props = (data.get("props") or {}).get("pageProps") or {}
    page_keys = list(page_props.keys())[:12]

    product_list = None
    for candidate in [
        (page_props.get("searchResult") or {}).get("products"),
        page_props.get("products"),
        (page_props.get("search") or {}).get("products"),
        (page_props.get("initialData") or {}).get("products"),
        (page_props.get("initialState") or {}).get("products"),
    ]:
        if isinstance(candidate, list) and candidate:
            product_list = candidate
            break

    if not product_list:
        print(f"[blinkit] SSR: __NEXT_DATA__ found but no products. "
              f"pageProps keys: {page_keys}")
        return []

    products = []
    for p in product_list:
        name = p.get("name") or p.get("product_name") or ""
        if not name:
            continue
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
    return products


async def _get_auth_key(access_token: str, cookies: dict) -> str:
    """Exchange gr_1_accessToken cookie for the derived SHA256 API auth key.

    The web app calls GET /v2/accounts/auth_key/ using only the session
    cookie — it does NOT send auth_key as a request header (the key is
    what you get back, not what you send in).
    Falls back to raw access_token on any error.
    """
    # Send base headers WITHOUT auth_key — just let the cookie authenticate.
    # Origin and Referer are required; without them Blinkit returns 400.
    headers = {k: v for k, v in _API_HEADERS_BASE.items() if k != "auth_key"}
    headers["Origin"] = BASE_URL
    headers["Referer"] = f"{BASE_URL}/"
    # Decode cookie values — Playwright URL-encodes them internally but the
    # server expects the original decoded values (e.g. v2:: not v2%3A%3A).
    decoded_cookies = {k: unquote(v) if isinstance(v, str) else v
                       for k, v in cookies.items()}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(
                f"{BASE_URL}/v2/accounts/auth_key/",
                headers=headers,
                cookies=decoded_cookies,
            )
        print(f"[blinkit] _get_auth_key: HTTP {resp.status_code} "
              f"body={resp.text[:120]!r}")
        if resp.status_code == 200:
            key = resp.json().get("auth_key", "")
            if key:
                return key
    except Exception as e:
        print(f"[blinkit] _get_auth_key failed: {e}")
    return access_token


def _parse_layout_search_response(data: dict) -> list[dict]:
    """Parse POST /v1/layout/search response into the canonical product format.

    Each product snippet has the structure:
      {"data": {"identity": {"id": "<product_id>"},
                "name": {"text": "<name>"},
                "variant": {"text": "<unit>"},
                "normal_price": {"text": "₹27"},
                "mrp": {"text": "₹32"},
                "image": {"url": "..."},
                "atc_action": {"add_to_cart": {"cart_item": {
                    "product_id": 530158, "price": 27, "mrp": 32,
                    "unit": "1 kg", "image_url": "..."}}}}}
    """
    snippets = (data.get("response") or {}).get("snippets") or []
    products = []
    for snippet in snippets:
        d = snippet.get("data") or {}
        cart_item = ((d.get("atc_action") or {})
                     .get("add_to_cart", {})
                     .get("cart_item") or {})
        if not cart_item:
            continue
        product_id = str(cart_item.get("product_id") or "")
        if not product_id:
            continue
        name = (cart_item.get("product_name")
                or (d.get("name") or {}).get("text") or "")
        if not name:
            continue
        # Skip disabled/out-of-stock (stepper state = "disabled")
        stepper_title = ((d.get("stepper_data") or {})
                         .get("state", {}).get("title", {}).get("text", ""))
        if stepper_title == "disabled":
            continue
        price = float(cart_item.get("price") or 0)
        mrp = float(cart_item.get("mrp") or price)
        if price <= 0:
            continue
        unit = cart_item.get("unit") or ""
        image_url = cart_item.get("image_url") or ""
        products.append({
            "name": name[:120],
            "price": mrp,
            "sale_price": price,
            "unit": unit,
            "image_url": image_url,
            "product_id": product_id,
            "app": APP_NAME,
            "app_name": DISPLAY_NAME,
        })
        if len(products) >= 8:
            break
    return products


async def search_item_api(user_id: str, query: str) -> list[dict]:
    """Search Blinkit for *query*.

    Strategy 1 (primary): Blinkit's internal v2 JSON API.
      GET /v2/search?search_type=keyword&q={query}&start=0&size=20
      This is the same API the web app calls on every keystroke; it
      returns a clean JSON payload and does not require HTML parsing.

    Strategy 2 (fallback): __NEXT_DATA__ SSR extraction.
      GET /s/?q={query} and parse the embedded Next.js JSON blob.
      Still attempted if Strategy 1 returns no results or fails,
      in case Blinkit changes the v2 API path in a future release.

    Returns [] on complete failure.
    """
    cookies = get_store_cookies(user_id, APP_NAME)
    # Cookie values are stored URL-encoded (as the browser stored them).
    # Do NOT decode them for the cookie jar — send them as-is.
    # Only decode gr_1_accessToken when using it as the auth_key HEADER value.
    access_token = unquote(cookies.get("gr_1_accessToken", ""))
    if not access_token:
        print(f"[blinkit] search_item_api: no gr_1_accessToken for user {user_id[:8]}")
        return []

    print(f"\n[blinkit] === API SEARCH: '{query}' ===")
    t_start = time.time()

    # Location context — web relay saves gr_1_lat/gr_1_lon; mobile saves lat/lng.
    lat = (cookies.get("gr_1_lat") or cookies.get("lat") or cookies.get("dlat") or "")
    lng = (cookies.get("gr_1_lon") or cookies.get("lng") or cookies.get("dlng") or "")
    merchant_id = cookies.get("merchant_id") or cookies.get("gr_1_merchantId") or ""

    print(f"[blinkit] location: lat={lat!r} lng={lng!r} merchant_id={merchant_id!r}")

    # Cloudflare cookies (__cf_bm, _cfuvid) are session-specific and become
    # stale immediately after the auth session ends. Sending stale CF cookies
    # causes Blinkit to reject the request; omit them so CF issues a fresh one.
    _CF_COOKIES = {"__cf_bm", "_cfuvid"}
    httpx_cookies = {k: v for k, v in cookies.items() if k not in _CF_COOKIES}

    # ── Strategy 1: POST /v1/layout/search ───────────────────────────────────
    products: list[dict] = []
    try:
        # Use a cached derived key if Playwright captured one on a previous run.
        # Otherwise derive it fresh from /v2/accounts/auth_key/.
        cached_key = cookies.get("api_auth_key", "")
        if cached_key:
            api_auth_key = cached_key
            print(f"[blinkit] Using cached derived auth key prefix={api_auth_key[:12]!r}")
        else:
            api_auth_key = await _get_auth_key(access_token, httpx_cookies)
        is_derived = (api_auth_key != access_token)
        print(f"[blinkit] auth_key derived={is_derived} "
              f"key_prefix={api_auth_key[:12]!r}")

        # Match exactly what the browser sends: auth_key + lat + Origin + Referer.
        # lon and merchant_id come from cookies; do NOT send them as headers.
        # Referer must be the search page URL, not the site root.
        layout_headers = {
            **_API_HEADERS_BASE,
            "auth_key": api_auth_key,
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/s/?q={quote(query)}",
        }
        if lat:
            layout_headers["lat"] = str(lat)

        # Match the format used by Blinkit's React app (uses previous_search_query,
        # not q, and includes sort / similar_entities / monet_assets).
        layout_body = {
            "previous_search_query": query,
            "applied_filters": None,
            "postback_meta": {},
            "processed_rails": {},
            "monet_assets": [{"name": "ads_vertical_banner", "processed": 0, "total": 0}],
            "similar_entities": None,
            "sort": "",
        }
        print(f"[blinkit] Strategy 1 POST headers keys: "
              f"{[k for k in layout_headers if k not in _API_HEADERS_BASE]}")

        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                f"{BASE_URL}/v1/layout/search",
                json=layout_body,
                headers=layout_headers,
                cookies=httpx_cookies,
            )
        elapsed_ms = int((time.time() - t_start) * 1000)
        if resp.status_code == 200:
            try:
                products = _parse_layout_search_response(resp.json())
                print(f"[blinkit] Strategy 1 (layout/search): {len(products)} products "
                      f"({elapsed_ms}ms)")
            except Exception as e:
                print(f"[blinkit] Strategy 1: parse error: {e} ({elapsed_ms}ms)")
        else:
            print(f"[blinkit] Strategy 1: HTTP {resp.status_code} ({elapsed_ms}ms) "
                  f"body={resp.text[:300]!r}")
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[blinkit] Strategy 1: request failed: {e} ({elapsed_ms}ms)")

    if products:
        print(f"[blinkit] === API RESULT: {len(products)} products "
              f"({int((time.time() - t_start)*1000)}ms) ===\n")
        return products

    # ── Strategy 2: __NEXT_DATA__ SSR fallback ───────────────────────────────
    print(f"[blinkit] Strategy 1 empty → trying SSR fallback")
    ssr_url = f"{BASE_URL}/s/?q={quote(query)}"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(ssr_url, headers=_SSR_HEADERS, cookies=cookies)
        elapsed_ms = int((time.time() - t_start) * 1000)
        if resp.status_code == 200:
            products = _parse_ssr_response(resp.text)
            print(f"[blinkit] Strategy 2 (SSR): {len(products)} products ({elapsed_ms}ms)")
        else:
            print(f"[blinkit] Strategy 2 (SSR): HTTP {resp.status_code} ({elapsed_ms}ms) "
                  f"body={resp.text[:200]!r}")
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[blinkit] Strategy 2 (SSR): request failed: {e} ({elapsed_ms}ms)")

    if products:
        print(f"[blinkit] === API RESULT: {len(products)} products "
              f"({int((time.time() - t_start)*1000)}ms) ===\n")
        return products

    # ── Strategy 3: Playwright DOM scraping ──────────────────────────────────
    # Strategies 1 and 2 both rely on API endpoints that have changed.
    # This strategy loads the real search page in a headless browser with the
    # saved cookies, waits for React to render products, then scrapes the DOM.
    # Also intercepts the network to log the correct search API URL so
    # Strategy 1 can be fixed once we know the real endpoint.
    print(f"[blinkit] SSR fallback empty → trying Playwright strategy")
    try:
        products, pw_auth_key = await _search_playwright(user_id, query, cookies)
        print(f"[blinkit] Strategy 3 (Playwright): {len(products)} products "
              f"({int((time.time() - t_start)*1000)}ms)")
        # Cache the derived auth key so Strategy 1 succeeds on the next call.
        if pw_auth_key:
            update_store_cookies(user_id, APP_NAME, {"api_auth_key": pw_auth_key})
            print(f"[blinkit] Cached derived auth key for future Strategy 1 use")
    except Exception as e:
        print(f"[blinkit] Strategy 3 failed: {e}")

    print(f"[blinkit] === API RESULT: {len(products)} products "
          f"({int((time.time() - t_start)*1000)}ms) ===\n")
    return products


# ── Playwright search (Strategy 3) ───────────────────────────────────────────

_PW_SEARCH_SCRIPT = r"""
() => {
    const results = [];
    const cards = document.querySelectorAll('div[role="button"][tabindex="0"][id]');
    cards.forEach(card => {
        const id = card.id || '';
        if (!/^\d+$/.test(id)) return;
        const txt = (card.innerText || '').toLowerCase();
        if (txt.includes('out of stock') || txt.includes('notify me') ||
            txt.includes('sold out')) return;

        const nameEl = card.querySelector('.tw-line-clamp-2');
        const unitEl = card.querySelector('.tw-line-clamp-1');

        let saleEl = null;
        for (const el of card.querySelectorAll('div.tw-font-semibold')) {
            const t = (el.textContent || '').trim();
            if (!t.startsWith('₹')) continue;
            let p = el, struck = false;
            while (p && p !== card) {
                if ((p.className || '').includes('line-through')) { struck=true; break; }
                p = p.parentElement;
            }
            if (!struck) { saleEl = el; break; }
        }
        const mrpEl = card.querySelector('.tw-line-through');
        const px = el => {
            if (!el) return 0;
            return parseFloat((el.textContent||'').replace(/[^\d.]/g,''))||0;
        };
        const sale = px(saleEl), mrp = px(mrpEl)||sale;
        const name = nameEl ? (nameEl.textContent||'').trim() : '';
        const unit = unitEl ? (unitEl.textContent||'').trim() : '';
        if (name && sale > 0) {
            results.push({
                name: name.slice(0,120), price: mrp, sale_price: sale,
                unit, image_url: '', product_id: id,
                app: 'blinkit', app_name: 'Blinkit'
            });
        }
    });
    return results.slice(0, 8);
}
"""


async def _search_playwright(
    user_id: str, query: str, cookies: dict
) -> tuple[list[dict], str]:
    """Load Blinkit search page in headless Chromium, scrape rendered products.

    Returns (products, derived_auth_key). derived_auth_key is the SHA256 key
    captured from the /v2/accounts/auth_key/ response; empty string if not seen.
    """
    import auth_browser as _ab
    pw = await _ab._get_playwright()

    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
    lat = cookies.get("gr_1_lat") or cookies.get("lat") or cookies.get("dlat") or ""
    captured: dict = {"auth_key": "", "products": []}
    try:
        ctx = await browser.new_context(
            user_agent=_MOBILE_UA,
            is_mobile=True, has_touch=True,
            locale="en-IN", timezone_id="Asia/Kolkata",
        )
        pw_cookies = [
            {"name": k, "value": v, "domain": "blinkit.com", "path": "/",
             "httpOnly": False, "secure": True, "sameSite": "Lax"}
            for k, v in cookies.items()
            if k not in ("__cf_bm", "_cfuvid", "api_auth_key")
        ]
        await ctx.add_cookies(pw_cookies)

        page = await ctx.new_page()

        def on_request(req):
            pass  # No request logging needed now that the strategy is stable

        async def on_response(resp):
            try:
                u = resp.url
                if not any(x in u for x in ("/v1/", "/v2/", "/v3/",
                                             "/api/", "/search")):
                    return
                if "json" not in resp.headers.get("content-type", ""):
                    return
                body = await resp.json()
                # Capture derived auth key for caching
                if "/v2/accounts/auth_key/" in u and body.get("auth_key"):
                    captured["auth_key"] = body["auth_key"]
                # Capture the first successful layout/search response directly.
                # The React app's own request succeeds where our standalone
                # httpx/fetch calls do not; intercepting the response avoids
                # replicating the exact session state the React app has.
                if ("/v1/layout/search" in u and resp.status == 200
                        and body.get("is_success") and not captured["products"]):
                    try:
                        captured["products"] = _parse_layout_search_response(body)
                        print(f"[blinkit] Playwright captured "
                              f"{len(captured['products'])} products from response")
                    except Exception as pe:
                        print(f"[blinkit] Playwright product parse error: {pe}")
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        await page.goto(f"{BASE_URL}/s/?q={quote(query)}",
                        wait_until="domcontentloaded", timeout=25000)

        # Wait for the React app's layout/search response to be intercepted.
        # It arrives within ~2s of DOMContentLoaded; no DOM scraping needed.
        for _ in range(30):
            if captured["products"]:
                break
            await page.wait_for_timeout(200)

        if captured["products"]:
            return captured["products"], captured["auth_key"]

        # Fall back to DOM scraping if the response wasn't intercepted.
        try:
            await page.wait_for_selector(
                'div[role="button"][tabindex="0"][id]', timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        products = await page.evaluate(_PW_SEARCH_SCRIPT)
        return [p for p in products if p.get("product_id")], captured["auth_key"]
    finally:
        await browser.close()


async def add_to_cart_api(user_id: str, product_id: str, count: int = 1) -> dict:
    """Add to Blinkit cart via internal v2 API.

    Uses gr_1_accessToken for auth and lat/lng/merchant_id from stored cookies.
    Returns {"success": True, "count_added": N} or {"success": False, "reason": str}.
    """
    cookies = get_store_cookies(user_id, APP_NAME)
    access_token = unquote(cookies.get("gr_1_accessToken", ""))
    if not access_token:
        return {"success": False, "reason": "not logged in (no gr_1_accessToken)"}

    # Location context — web relay saves gr_1_lat/gr_1_lon; mobile saves lat/lng.
    lat = (cookies.get("gr_1_lat") or cookies.get("lat")
           or cookies.get("dlat") or cookies.get("delivery_lat") or "")
    lng = (cookies.get("gr_1_lon") or cookies.get("lng")
           or cookies.get("dlng") or cookies.get("delivery_lng") or "")
    merchant_id = cookies.get("merchant_id") or cookies.get("gr_1_merchantId") or ""

    print(f"\n[blinkit] === API ADD: pid={product_id} qty={count} ===")
    t_start = time.time()

    _CF_COOKIES = {"__cf_bm", "_cfuvid"}
    httpx_cookies = {k: v for k, v in cookies.items() if k not in _CF_COOKIES}
    # Prefer cached derived auth key; fall back to raw token.
    api_auth_key = cookies.get("api_auth_key") or access_token
    headers = {**_API_HEADERS_BASE, "auth_key": api_auth_key}
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
