"""stores/blinkit.py - Blinkit httpx store module.

Search strategy (in order):
  1. Blinkit's internal v2 JSON search API — reliable, no HTML parsing.
     GET /v2/search?search_type=keyword&q={query}&start=0&size=20
     Auth: auth_key header (same token used by the cart API).
  2. __NEXT_DATA__ SSR fallback — used if the v2 API returns no results
     or a non-200 status (e.g. Blinkit changes the endpoint).

Cart add via Blinkit's internal v2 API.
Auth cookie: gr_1_accessToken (stored via user_store.connect_store).
Location cookies (lat, lng, merchant_id) extracted from stored cookies.
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
    access_token = cookies.get("gr_1_accessToken", "")
    if not access_token:
        print(f"[blinkit] search_item_api: no gr_1_accessToken for user {user_id[:8]}")
        return []

    print(f"\n[blinkit] === API SEARCH: '{query}' ===")
    t_start = time.time()

    # ── Strategy 1: direct JSON API ──────────────────────────────────────────
    # All Blinkit v2 endpoints use trailing slashes; /v2/search (no slash)
    # gives "no Route matched with those values" regardless of params.
    v2_url = f"{BASE_URL}/v2/search/"
    # Location context — web relay saves gr_1_lat/gr_1_lon; mobile saves lat/lng.
    lat = (cookies.get("gr_1_lat") or cookies.get("lat")
           or cookies.get("dlat") or "")
    lng = (cookies.get("gr_1_lon") or cookies.get("lng")
           or cookies.get("dlng") or "")
    merchant_id = cookies.get("merchant_id") or cookies.get("gr_1_merchantId") or ""

    print(f"[blinkit] location: lat={lat!r} lng={lng!r} merchant_id={merchant_id!r}")
    print(f"[blinkit] all cookie keys: {sorted(cookies.keys())}")

    # Send lat/lng/merchant_id as BOTH headers AND query params — the v2 API
    # "no Route matched" error suggests a routing constraint may require them
    # as query params rather than headers.
    v2_params: dict = {
        "search_type": "keyword",
        "q": query,
        "start": "0",
        "size": "20",
    }
    if lat:
        v2_params["lat"] = str(lat)
    if lng:
        v2_params["lng"] = str(lng)
    if merchant_id:
        v2_params["merchant_id"] = str(merchant_id)

    api_headers = {**_API_HEADERS_BASE, "auth_key": access_token}
    if lat:
        api_headers["lat"] = str(lat)
    if lng:
        api_headers["lng"] = str(lng)
    if merchant_id:
        api_headers["merchant_id"] = str(merchant_id)

    products: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(v2_url, params=v2_params, headers=api_headers,
                                    cookies=cookies)
        elapsed_ms = int((time.time() - t_start) * 1000)
        if resp.status_code == 200:
            try:
                data = resp.json()
                products = _parse_api_response(data)
                print(f"[blinkit] Strategy 1 (v2 API): {len(products)} products "
                      f"({elapsed_ms}ms)")
            except Exception as e:
                print(f"[blinkit] Strategy 1: JSON parse error: {e} ({elapsed_ms}ms)")
        else:
            print(f"[blinkit] Strategy 1: HTTP {resp.status_code} ({elapsed_ms}ms) "
                  f"body={resp.text[:200]!r}")
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
        products = await _search_playwright(user_id, query, cookies)
        print(f"[blinkit] Strategy 3 (Playwright): {len(products)} products "
              f"({int((time.time() - t_start)*1000)}ms)")
    except Exception as e:
        print(f"[blinkit] Strategy 3 failed: {e}")

    print(f"[blinkit] === API RESULT: {len(products)} products "
          f"({int((time.time() - t_start)*1000)}ms) ===\n")
    return products


# ── Playwright search (Strategy 3) ───────────────────────────────────────────

_PW_SEARCH_SCRIPT = """
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


async def _search_playwright(user_id: str, query: str, cookies: dict) -> list[dict]:
    """Load Blinkit search page in headless Chromium, scrape rendered products.

    Also intercepts JSON API responses so the correct search endpoint URL is
    logged — once identified, Strategy 1 can use it directly.
    """
    import auth_browser as _ab
    pw = await _ab._get_playwright()

    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
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
            if k not in ("__cf_bm", "_cfuvid")
        ]
        await ctx.add_cookies(pw_cookies)

        page = await ctx.new_page()

        # Intercept responses to find the real search API endpoint
        async def on_response(resp):
            try:
                if not any(x in resp.url for x in ("/v1/", "/v2/", "/v3/",
                                                    "/api/", "/search")):
                    return
                if "json" not in resp.headers.get("content-type", ""):
                    return
                body = await resp.json()
                if not isinstance(body, dict):
                    return
                objs = body.get("objects") or body.get("response", {}).get("objects")
                if isinstance(objs, list) and any(
                        o.get("type") == "product" for o in objs):
                    print(f"[blinkit] Playwright found search API: {resp.url}")
                    print(f"[blinkit] Playwright API sample: "
                          f"{str(body)[:300]}")
            except Exception:
                pass

        page.on("response", on_response)

        await page.goto(f"{BASE_URL}/s/?q={quote(query)}",
                        wait_until="domcontentloaded", timeout=25000)
        # Wait for React to hydrate and render product cards
        try:
            await page.wait_for_selector(
                'div[role="button"][tabindex="0"][id]', timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)

        products = await page.evaluate(_PW_SEARCH_SCRIPT)
        return [p for p in products if p.get("product_id")]
    finally:
        await browser.close()


async def add_to_cart_api(user_id: str, product_id: str, count: int = 1) -> dict:
    """Add to Blinkit cart via internal v2 API.

    Uses gr_1_accessToken for auth and lat/lng/merchant_id from stored cookies.
    Returns {"success": True, "count_added": N} or {"success": False, "reason": str}.
    """
    cookies = get_store_cookies(user_id, APP_NAME)
    access_token = cookies.get("gr_1_accessToken", "")
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

    headers = {**_API_HEADERS_BASE, "auth_key": access_token}
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
