"""stores/blinkit.py - Blinkit search + cart module.

Search strategy (in order):
  1. POST /v1/layout/search via httpx — fast (~200ms). Needs BOTH lat+lon
     headers, decoded access_token, cached api_auth_key, app_client=
     consumer_web. Returns "location not serviceable" if either coordinate
     is missing.
  2. __NEXT_DATA__ SSR fallback — GET /s/?q={query}, parse embedded Next.js
     JSON. Rarely succeeds (Blinkit stripped most product data from SSR).
  3. Playwright response interception — reliable (~2.5s). Navigates to the
     search page, intercepts the React app's own /v1/layout/search response,
     and caches the derived auth_key for future Strategy 1 attempts.

Cart: POST /v5/carts via httpx (replaces entire cart in one batch).
Playwright fallback if httpx cart fails (sends via fetch() in page context).
Auth cookie: gr_1_accessToken (stored via user_store.connect_store).
Location: gr_1_lat/gr_1_lon cookies + merchant_id (captured during auth).
"""

import re
import json
import time
import uuid
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


def session_health(user_id: str) -> dict:
    """Static health probe (no network call) for the page-load status indicator.

    Returns {"ok": bool, "reason": str}. Blinkit expiry can't be detected
    without an API call, so a present access token is treated as healthy here;
    actual expiry is caught after a compare (the store returns zero products,
    which triggers the stale-session warning in the UI). We do require the
    delivery-location cookies, since without them every search is "location
    not serviceable".
    """
    cookies = get_store_cookies(user_id, APP_NAME)
    if not cookies.get("gr_1_accessToken"):
        return {"ok": False, "reason": "Session expired — reconnect Blinkit."}
    has_loc = (cookies.get("gr_1_lat") or cookies.get("lat")) and \
              (cookies.get("gr_1_lon") or cookies.get("lng"))
    if not has_loc:
        return {"ok": False,
                "reason": "No delivery location — reconnect Blinkit with an address."}
    return {"ok": True, "reason": ""}


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
    captured: dict = {"auth_key": "", "products": []}
    print(f"[blinkit] _search_playwright: starting for query={query!r} "
          f"cookie_count={len(cookies)} "
          f"has_lat={'gr_1_lat' in cookies or 'lat' in cookies} "
          f"has_token={'gr_1_accessToken' in cookies}")
    try:
        ctx = await browser.new_context(
            user_agent=_MOBILE_UA,
            is_mobile=True, has_touch=True,
            locale="en-IN", timezone_id="Asia/Kolkata",
            extra_http_headers={
                "Sec-CH-UA": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "Sec-CH-UA-Mobile": "?1",
                "Sec-CH-UA-Platform": '"Android"',
            },
        )
        # Use dot-prefix domain (.blinkit.com) so cookies apply to all
        # subdomains, matching how a real browser sets them. Without the dot
        # the cookies only apply to the bare "blinkit.com" host and are NOT
        # sent to API subdomains like api.blinkit.com or cdn.blinkit.com.
        # Skip CF cookies (__cf_bm / _cfuvid) — they're device-tied and short-lived;
        # Playwright gets new ones on first request automatically.
        # Skip api_auth_key — it's a local cache value, not a real browser cookie.
        pw_cookies = [
            {"name": k, "value": v, "domain": ".blinkit.com", "path": "/",
             "httpOnly": False, "secure": True, "sameSite": "Lax"}
            for k, v in cookies.items()
            if k not in ("__cf_bm", "_cfuvid", "api_auth_key", "api_auth_key_ts")
        ]
        await ctx.add_cookies(pw_cookies)

        page = await ctx.new_page()

        # Apply same stealth patches as the login relay — Blinkit's WAF checks
        # the same signals during search as during login.
        try:
            from playwright_stealth import stealth_async
            await stealth_async(page)
        except ImportError:
            pass
        await page.add_init_script(_ab._STEALTH_SCRIPT)

        async def on_response(resp):
            try:
                u = resp.url
                if not any(x in u for x in ("/v1/", "/v2/", "/v3/",
                                             "/api/", "/search")):
                    return
                if "json" not in resp.headers.get("content-type", ""):
                    return
                body = await resp.json()
                if "/v2/accounts/auth_key/" in u and body.get("auth_key"):
                    captured["auth_key"] = body["auth_key"]
                    print(f"[blinkit] Playwright captured auth_key "
                          f"prefix={body['auth_key'][:12]!r}")
                # Accept is_success=True OR is_success missing (older API shape)
                if ("/v1/layout/search" in u and resp.status == 200
                        and body.get("is_success") is not False
                        and not captured["products"]):
                    try:
                        parsed = _parse_layout_search_response(body)
                        if parsed:
                            captured["products"] = parsed
                            print(f"[blinkit] Playwright intercepted layout/search: "
                                  f"{len(parsed)} products")
                    except Exception as pe:
                        print(f"[blinkit] Playwright product parse error: {pe}")
            except Exception:
                pass

        page.on("response", on_response)

        print(f"[blinkit] _search_playwright: injected {len(pw_cookies)} cookies "
              f"with domain .blinkit.com; stealth applied")
        goto_url = f"{BASE_URL}/s/?q={quote(query)}"
        print(f"[blinkit] _search_playwright: navigating to {goto_url}")
        await page.goto(goto_url, wait_until="domcontentloaded", timeout=30000)
        print(f"[blinkit] _search_playwright: page loaded, title={await page.title()!r}")

        # Wait up to 8 s for the React app to fire the search API call
        for _ in range(40):
            if captured["products"]:
                break
            await page.wait_for_timeout(200)

        if captured["products"]:
            print(f"[blinkit] _search_playwright: response-interceptor SUCCESS "
                  f"{len(captured['products'])} products")
            return captured["products"], captured["auth_key"]

        # Log what happened — no products captured from response interception
        page_url = page.url
        print(f"[blinkit] _search_playwright: no products from response intercept "
              f"(waited 8s). page_url={page_url!r}")
        try:
            page_text = await page.evaluate(
                "() => document.body ? document.body.innerText.slice(0,200) : 'no body'")
            print(f"[blinkit] _search_playwright: page body snippet: {page_text!r}")
        except Exception as pe:
            print(f"[blinkit] _search_playwright: page body read error: {pe}")

        # DOM scraping fallback — only if the response interceptor found nothing.
        # Wait for product cards to actually render (up to 6 more seconds).
        try:
            await page.wait_for_selector(
                'div[role="button"][tabindex="0"][id]', timeout=6000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        products = await page.evaluate(_PW_SEARCH_SCRIPT)
        print(f"[blinkit] _search_playwright: DOM scrape fallback "
              f"{len(products)} products")
        return [p for p in products if p.get("product_id")], captured["auth_key"]
    finally:
        await browser.close()


async def _cart_post_playwright(user_id: str, cart_items: list[dict]) -> dict:
    """POST a desired cart state to Blinkit's /v5/carts from inside a Playwright
    page (so the request carries fresh Cloudflare cookies).

    /v5/carts is a SYNC endpoint: the posted items array becomes the cart.
    Returns {"success": bool, "reason"?: str}.
    """
    cookies = get_store_cookies(user_id, APP_NAME)
    lat = cookies.get("gr_1_lat") or cookies.get("lat") or ""
    lon = cookies.get("gr_1_lon") or cookies.get("lng") or ""
    device_id = cookies.get("gr_1_deviceId") or ""
    cached_key = cookies.get("api_auth_key") or ""

    import auth_browser as _ab
    pw = await _ab._get_playwright()
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
    captured = {"auth_key": cached_key}
    try:
        ctx = await browser.new_context(
            user_agent=_MOBILE_UA, is_mobile=True, has_touch=True,
            locale="en-IN", timezone_id="Asia/Kolkata",
        )
        await ctx.add_cookies([
            {"name": k, "value": v, "domain": "blinkit.com", "path": "/",
             "httpOnly": False, "secure": True, "sameSite": "Lax"}
            for k, v in cookies.items()
            if k not in ("__cf_bm", "_cfuvid", "api_auth_key")
        ])
        page = await ctx.new_page()

        async def on_response(resp):
            if "/v2/accounts/auth_key/" in resp.url:
                try:
                    b = await resp.json()
                    if b.get("auth_key"):
                        captured["auth_key"] = b["auth_key"]
                except Exception:
                    pass
        page.on("response", on_response)

        await page.goto(f"{BASE_URL}/", wait_until="domcontentloaded", timeout=25000)
        for _ in range(25):
            if captured["auth_key"]:
                break
            await page.wait_for_timeout(200)
        if not captured["auth_key"]:
            return {"success": False, "reason": "could not derive auth_key"}

        result = await page.evaluate(
            """async ({auth_key, lat, lon, device_id, items}) => {
                const r = await fetch('/v5/carts', {
                    method: 'POST', credentials: 'include',
                    headers: {
                        'auth_key': auth_key,
                        'app_client': 'consumer_web',
                        'lat': lat, 'lon': lon,
                        'device_id': device_id,
                        'app_version': '1008010008',
                        'web_app_version': '1008010008',
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({items, promo_codes: ['']}),
                });
                let body = '';
                try { body = (await r.text()).slice(0, 300); } catch (e) {}
                return {status: r.status, body};
            }""",
            {"auth_key": captured["auth_key"], "lat": str(lat), "lon": str(lon),
             "device_id": str(device_id), "items": cart_items},
        )
        if captured["auth_key"] and captured["auth_key"] != cached_key:
            update_store_cookies(user_id, APP_NAME, {"api_auth_key": captured["auth_key"]})

        if result.get("status") == 200:
            return {"success": True}
        return {"success": False,
                "reason": f"HTTP {result.get('status')}: {result.get('body','')[:120]}"}
    finally:
        await browser.close()


async def search_item_api(user_id: str, query: str) -> list[dict]:
    """Search Blinkit for *query*.

    Strategy order (Playwright-primary since httpx is WAF-blocked without
    fresh Cloudflare cookies):

    1. Playwright response interception — most reliable (~2.5s). Navigates to
       the search page inside a real Chromium with saved session cookies; the
       React app fires /v1/layout/search itself and we intercept the response.
       Also captures the derived auth_key for the fast httpx path below.
       Only skipped if a fresh cached auth_key (< 30 min old) is present.

    2. POST /v1/layout/search via httpx — fast (~200ms) but needs a valid
       api_auth_key (derived by Playwright or the /v2/accounts/auth_key/
       endpoint). Used when we have a trusted cached key, falls through to
       Playwright if it returns nothing.

    3. __NEXT_DATA__ SSR extraction — last resort if both above fail.

    Returns [] on complete failure.
    """
    cookies = get_store_cookies(user_id, APP_NAME)
    _CF_COOKIES = {"__cf_bm", "_cfuvid"}
    httpx_cookies = {k: unquote(v) if isinstance(v, str) else v
                     for k, v in cookies.items() if k not in _CF_COOKIES}
    access_token = httpx_cookies.get("gr_1_accessToken", "")
    if not access_token:
        print(f"[blinkit] search_item_api: no gr_1_accessToken for user {user_id[:8]}")
        return []

    print(f"\n[blinkit] === API SEARCH: '{query}' ===")
    t_start = time.time()

    lat = (cookies.get("gr_1_lat") or cookies.get("lat") or cookies.get("dlat") or "")
    lng = (cookies.get("gr_1_lon") or cookies.get("lng") or cookies.get("dlng") or "")
    merchant_id = cookies.get("merchant_id") or cookies.get("gr_1_merchantId") or ""
    api_auth_key = cookies.get("api_auth_key", "")
    # Treat cached auth_key as stale after 30 minutes — Blinkit rotates them.
    auth_key_age = time.time() - float(cookies.get("api_auth_key_ts", 0) or 0)
    auth_key_fresh = bool(api_auth_key and auth_key_age < 1800)

    print(f"[blinkit] location: lat={lat!r} lng={lng!r} merchant_id={merchant_id!r} "
          f"auth_key_fresh={auth_key_fresh}")

    products: list[dict] = []

    # ── Strategy 1: Playwright response interception (PRIMARY) ───────────────
    # Playwright lets the real Chromium make the /v1/layout/search request with
    # its own session cookies + fresh Cloudflare tokens (CF cookies are short-
    # lived and can't be used from server-side httpx). This is the only approach
    # that reliably bypasses Blinkit's WAF.
    # Skip if we have a fresh cached auth_key (< 30 min old) — in that case
    # try the fast httpx path first and only fall to Playwright on failure.
    if not auth_key_fresh:
        print(f"[blinkit] No fresh auth_key → going directly to Playwright")
        try:
            products, pw_auth_key = await _search_playwright(user_id, query, cookies)
            elapsed_ms = int((time.time() - t_start) * 1000)
            print(f"[blinkit] Strategy 1 (Playwright): {len(products)} products "
                  f"({elapsed_ms}ms)")
            if pw_auth_key:
                update_store_cookies(user_id, APP_NAME, {
                    "api_auth_key": pw_auth_key,
                    "api_auth_key_ts": str(int(time.time())),
                })
                api_auth_key = pw_auth_key
                print(f"[blinkit] Cached fresh auth_key prefix={pw_auth_key[:12]!r}")
        except Exception as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            print(f"[blinkit] Strategy 1 (Playwright) failed after {elapsed_ms}ms: {e}")

        if products:
            print(f"[blinkit] === API RESULT: {len(products)} products "
                  f"({int((time.time() - t_start)*1000)}ms) ===\n")
            return products

    # ── Strategy 2: POST /v1/layout/search via httpx (fast path) ─────────────
    # Use the cached auth_key obtained from a previous Playwright run. Blinkit
    # rotates this key so it's only trusted for 30 min. This is a fast ~200ms
    # path used after Strategy 1 has warmed the auth_key cache.
    print(f"[blinkit] {'cached auth_key present, trying' if auth_key_fresh else 'Playwright empty → trying'} httpx")
    try:
        layout_headers = {
            **_API_HEADERS_BASE,
            "app_client": "consumer_web",
            "access_token": access_token,
            "auth_key": api_auth_key or access_token,
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/s/?q={quote(query)}",
        }
        if lat:
            layout_headers["lat"] = str(lat)
        if lng:
            layout_headers["lon"] = str(lng)

        layout_body = {
            "previous_search_query": query,
            "applied_filters": None,
            "postback_meta": {},
            "processed_rails": {},
            "monet_assets": [{"name": "ads_vertical_banner", "processed": 0, "total": 0}],
            "similar_entities": None,
            "sort": "",
        }
        search_url = (f"{BASE_URL}/v1/layout/search"
                      f"?q={quote(query)}&search_type=type_to_search")
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                search_url, json=layout_body,
                headers=layout_headers, cookies=httpx_cookies,
            )
        elapsed_ms = int((time.time() - t_start) * 1000)
        if resp.status_code == 200:
            try:
                products = _parse_layout_search_response(resp.json())
                print(f"[blinkit] Strategy 2 (httpx layout/search): {len(products)} "
                      f"products ({elapsed_ms}ms)")
            except Exception as e:
                print(f"[blinkit] Strategy 2: parse error: {e} ({elapsed_ms}ms)")
        else:
            print(f"[blinkit] Strategy 2 (httpx): HTTP {resp.status_code} ({elapsed_ms}ms) "
                  f"body={resp.text[:200]!r}")
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[blinkit] Strategy 2 (httpx): failed after {elapsed_ms}ms: {e}")

    if products:
        print(f"[blinkit] === API RESULT: {len(products)} products "
              f"({int((time.time() - t_start)*1000)}ms) ===\n")
        return products

    # ── Strategy 3: Playwright (retry if httpx fast-path missed) ─────────────
    # Only runs when auth_key_fresh was True (we skipped Strategy 1) and httpx
    # still returned nothing — auth_key may have expired mid-window.
    if auth_key_fresh:
        print(f"[blinkit] httpx empty (auth_key may have expired) → retrying Playwright")
        try:
            products, pw_auth_key = await _search_playwright(user_id, query, cookies)
            elapsed_ms = int((time.time() - t_start) * 1000)
            print(f"[blinkit] Strategy 3 (Playwright retry): {len(products)} products "
                  f"({elapsed_ms}ms)")
            if pw_auth_key:
                update_store_cookies(user_id, APP_NAME, {
                    "api_auth_key": pw_auth_key,
                    "api_auth_key_ts": str(int(time.time())),
                })
                print(f"[blinkit] Refreshed auth_key from retry Playwright run")
        except Exception as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            print(f"[blinkit] Strategy 3 (Playwright retry) failed after {elapsed_ms}ms: {e}")

    # ── Strategy 4: __NEXT_DATA__ SSR (last resort) ───────────────────────────
    if not products:
        print(f"[blinkit] All Playwright/httpx paths empty → trying SSR last resort")
        ssr_url = f"{BASE_URL}/s/?q={quote(query)}"
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(ssr_url, headers=_SSR_HEADERS, cookies=cookies)
            elapsed_ms = int((time.time() - t_start) * 1000)
            if resp.status_code == 200:
                products = _parse_ssr_response(resp.text)
                print(f"[blinkit] Strategy 4 (SSR): {len(products)} products ({elapsed_ms}ms)")
            else:
                print(f"[blinkit] Strategy 4 (SSR): HTTP {resp.status_code} ({elapsed_ms}ms)")
        except Exception as e:
            elapsed_ms = int((time.time() - t_start) * 1000)
            print(f"[blinkit] Strategy 4 (SSR) failed after {elapsed_ms}ms: {e}")

    print(f"[blinkit] === API RESULT: {len(products)} products "
          f"({int((time.time() - t_start)*1000)}ms) ===\n")
    return products


async def add_all_to_cart_api(user_id: str, items: list[dict]) -> dict:
    """Add ALL items to the Blinkit cart in ONE request to POST /v5/carts.

    Blinkit's web cart is synced wholesale: the client sends its entire cart
    as {"items":[{"product_id","quantity"}...],"promo_codes":[""]} and the
    server replaces the cart with it. (The old per-item /v2/client/user_cart/
    endpoint was removed — it now 404s with Kong "no Route matched".)

    Because /v5/carts REPLACES the cart, every Blinkit item must go in a single
    call — never loop this per item, or each call wipes the previous one.

    Returns {"success": bool, "items": [{"success", "count_added"}...]} aligned
    with the input order, or {"success": False, "reason": str}.
    """
    if not items:
        return {"success": True, "items": []}

    cookies = get_store_cookies(user_id, APP_NAME)
    _CF_COOKIES = {"__cf_bm", "_cfuvid"}
    httpx_cookies = {k: unquote(v) if isinstance(v, str) else v
                     for k, v in cookies.items() if k not in _CF_COOKIES}
    access_token = httpx_cookies.get("gr_1_accessToken", "")
    if not access_token:
        return {"success": False, "reason": "not logged in (no gr_1_accessToken)"}

    lat = cookies.get("gr_1_lat") or cookies.get("lat") or cookies.get("dlat") or ""
    lng = cookies.get("gr_1_lon") or cookies.get("lng") or cookies.get("dlng") or ""
    device_id = (cookies.get("gr_1_deviceId") or cookies.get("device_id")
                 or cookies.get("deviceId") or "")
    api_auth_key = cookies.get("api_auth_key") or access_token

    # Build the cart payload (product_id as string + integer quantity), keeping
    # the per-item order so the caller can map results back.
    cart_items, order = [], []
    for it in items:
        pid = str(it.get("product_id") or "")
        if not pid:
            continue
        try:
            qty = max(1, min(99, int(it.get("count") or it.get("quantity") or 1)))
        except (TypeError, ValueError):
            qty = 1
        cart_items.append({"product_id": pid, "quantity": qty})
        order.append(qty)
    if not cart_items:
        return {"success": False, "reason": "no valid items"}

    print(f"\n[blinkit] === API ADD (batch /v5/carts): {len(cart_items)} items ===")
    t_start = time.time()

    headers = {
        **_API_HEADERS_BASE,
        "app_client": "consumer_web",
        "access_token": access_token,
        "auth_key": api_auth_key,
        "platform": "mobile_web",
        "qd_sdk_request": "true",
        "x-age-consent-granted": "false",
        # The cart upstream (assembly-consumer/v4/carts) hard-validates these
        # version headers — without app_version it 400s with
        # "Key: 'writeCartDataHeaders.AppVersion'". Values captured from a live
        # blinkit.com web session; update if Blinkit bumps its web build.
        "app_version": "52434333",
        "rn_bundle_version": "1009003012",
        "web_app_version": "1008010016",
        "session_uuid": str(uuid.uuid4()),
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/cart",
    }
    if lat:
        headers["lat"] = str(lat)
    if lng:
        headers["lon"] = str(lng)
    if device_id:
        headers["device_id"] = str(device_id)

    body = {"items": cart_items, "promo_codes": [""]}

    httpx_ok = False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{BASE_URL}/v5/carts", json=body,
                                     headers=headers, cookies=httpx_cookies)
        elapsed_ms = int((time.time() - t_start) * 1000)
        if resp.status_code == 200:
            print(f"[blinkit] /v5/carts OK: {len(cart_items)} items ({elapsed_ms}ms)")
            httpx_ok = True
        else:
            print(f"[blinkit] /v5/carts HTTP {resp.status_code} ({elapsed_ms}ms) "
                  f"body={resp.text[:200]!r} → trying Playwright fallback")
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[blinkit] /v5/carts failed after {elapsed_ms}ms: {e} → trying Playwright fallback")

    if httpx_ok:
        return {"success": True,
                "items": [{"success": True, "count_added": q} for q in order]}

    # Playwright fallback: fetch() inside a page context carries fresh CF cookies.
    try:
        pw_cart_items = [{"product_id": ci["product_id"], "quantity": ci["quantity"]}
                         for ci in cart_items]
        res = await _cart_post_playwright(user_id, pw_cart_items)
        elapsed_ms = int((time.time() - t_start) * 1000)
        ok = res.get("success", False)
        print(f"[blinkit] Playwright cart: {'OK' if ok else 'FAIL'} "
              f"({len(pw_cart_items)} items, {elapsed_ms}ms) {res.get('reason', '')}")
        return {"success": ok,
                "items": [{"success": ok, "count_added": q if ok else 0} for q in order],
                **({"reason": res.get("reason")} if not ok else {})}
    except Exception as e:
        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[blinkit] Playwright cart fallback failed after {elapsed_ms}ms: {e}")
        return {"success": False, "reason": f"all methods failed: {e}"}


async def add_to_cart_api(user_id: str, product_id: str, count: int = 1) -> dict:
    """Single-item convenience wrapper around the batch /v5/carts call.

    NOTE: /v5/carts REPLACES the whole cart, so calling this in a loop will
    leave only the last item. Callers adding multiple Blinkit items must use
    add_all_to_cart_api() with the full list instead.
    """
    r = await add_all_to_cart_api(user_id, [{"product_id": product_id, "count": count}])
    if r.get("success"):
        return {"success": True, "count_added": count}
    return {"success": False, "reason": r.get("reason", "unknown")}


def checkout_url() -> str:
    return f"{BASE_URL}/cart"

