"""server.py - scan2order merged backend.

Multi-user, cloud-deployable FastAPI server.
No Playwright — all store interactions via httpx.
Sessions stored in Upstash Redis (per-user cookies).

Serves two audiences:
  1. Mobile app (scan2order1 API contract: /scan, /search, /order,
     /connect-account, /account-status/{user_id}, /health)
  2. Web UI (scan2order2 API contract: /api/compare, /api/cart/add-all,
     /api/ocr, /api/search, /api/version, /api/auth/*)
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

import auth_browser
import ocr as ocr_module
import ranker
from storage import user_store
from stores import bigbasket, blinkit, zepto

BASE_DIR = Path(__file__).parent
APP_VERSION = "1.0.0"
_INDEX_HTML = BASE_DIR / "templates" / "index.html"


def _serve_index() -> HTMLResponse:
    if _INDEX_HTML.exists():
        return HTMLResponse(content=_INDEX_HTML.read_text(encoding="utf-8"))
    return HTMLResponse("""<!DOCTYPE html><html><body>
<h1>scan2order</h1><p>Backend running.</p>
<p><a href="/docs">API docs</a></p></body></html>""")


# ── Store display names ─────────────────────────────────────────────────────

_STORE_DISPLAY = {
    "bigbasket": "BigBasket",
    "blinkit": "Blinkit",
    "zepto": "Zepto",
}


# ── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"\n[startup] scan2order v{APP_VERSION} starting")
    print(f"[startup] OCR available: {ocr_module.OCR_AVAILABLE}")
    if not ocr_module.OCR_AVAILABLE:
        print(f"[startup] OCR error: {ocr_module.OCR_ERROR}")
    print("[startup] Ready\n")

    async def _browser_cleanup_loop():
        while True:
            await asyncio.sleep(60)
            await auth_browser.cleanup_expired()

    cleanup_task = asyncio.create_task(_browser_cleanup_loop())
    yield
    cleanup_task.cancel()
    print("[shutdown] Shutting down")


app = FastAPI(title="scan2order", version=APP_VERSION, lifespan=lifespan)

# CORS: allow all origins so the mobile WebView and any web client can connect.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Multi-user progress tracking ────────────────────────────────────────────
# Keyed by user_id so 100s of concurrent compares don't stomp each other.

_compare_progress: dict[str, dict] = {}
_cart_progress: dict[str, dict] = {}


def _new_progress() -> dict:
    return {"running": False, "total": 0, "done": 0, "current": ""}


# ── Store session helpers ────────────────────────────────────────────────────

def _get_available_stores(user_id: str) -> list[str]:
    """Return list of store names that have valid auth for this user."""
    stores = []
    if bigbasket.is_session_valid(user_id):
        stores.append("bigbasket")
    if blinkit.is_session_valid(user_id):
        stores.append("blinkit")
    if zepto.is_session_valid(user_id):
        stores.append("zepto")
    return stores


# ── Utility ──────────────────────────────────────────────────────────────────

def _require_user_id(user_id: str | None) -> str | None:
    """Return a valid user_id or None if missing/empty."""
    if not user_id or not str(user_id).strip():
        return None
    return str(user_id).strip()


# ── Health & version ─────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/version")
async def api_version():
    return {"version": APP_VERSION}


# ── Web UI ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home():
    return _serve_index()


# ── Auth endpoints ────────────────────────────────────────────────────────────

@app.post("/api/auth/connect")
async def api_auth_connect(request: Request):
    """Store cookies for a user's grocery store session.

    Body: {user_id, store, cookies, local_storage?}
    """
    body = await request.json()
    user_id = _require_user_id(body.get("user_id"))
    store = body.get("store", "").lower().strip()
    cookies = body.get("cookies") or {}
    local_storage = body.get("local_storage") or {}

    if not user_id:
        return {"success": False, "error": "missing user_id"}
    if store not in _STORE_DISPLAY:
        return {"success": False, "error": f"unknown store: {store}"}
    if not cookies:
        return {"success": False, "error": "missing cookies"}

    user_store.connect_store(user_id, store, cookies, local_storage)
    print(f"[auth] connected {store} for user {user_id[:8]}... "
          f"({len(cookies)} cookies)")
    return {"success": True, "store": store, "user_id": user_id}


@app.get("/api/auth/status/{user_id}")
async def api_auth_status(user_id: str):
    """Return which stores are connected for a user."""
    uid = _require_user_id(user_id)
    if not uid:
        return {"error": "missing user_id"}
    stores_data = user_store.get_user_stores(uid)
    connected = {
        store: {"connected": True}
        for store in _STORE_DISPLAY
        if stores_data.get(store, {}).get("connected")
    }
    return {"user_id": uid, "connected_stores": connected}


@app.post("/api/auth/link")
async def create_link_code(request: Request):
    """Generate a link code so mobile users can connect the web UI.

    Body: {user_id}
    Returns: {code: "ABCD1234"}
    """
    body = await request.json()
    user_id = _require_user_id(body.get("user_id"))
    if not user_id:
        return {"success": False, "error": "missing user_id"}
    code = user_store.create_link_code(user_id)
    return {"success": True, "code": code}


@app.get("/api/auth/link/{code}")
async def resolve_link_code(code: str):
    """Resolve a link code to user_id + store list (one-time use).

    Returns: {user_id, stores: {bigbasket: bool, ...}}
    """
    user_id = user_store.consume_link_code(code)
    if not user_id:
        return {"success": False, "error": "invalid or expired code"}
    stores_data = user_store.get_user_stores(user_id)
    stores = {
        store: stores_data.get(store, {}).get("connected", False)
        for store in _STORE_DISPLAY
    }
    return {"success": True, "user_id": user_id, "stores": stores}


# ── Browser-based store auth (Playwright) ────────────────────────────────────

@app.post("/api/auth/browser/start/{store}")
async def browser_auth_start(store: str, request: Request):
    """Launch a headless Chromium session for interactive store login.

    Body: {user_id?}  — auto-generated UUID if omitted.
    Returns: {session_id, user_id}

    The client should poll /screenshot for display, forward events via /event,
    and poll /check until {done: true} to know when cookies are saved.
    """
    body = await request.json()
    user_id = _require_user_id(body.get("user_id")) or str(uuid.uuid4())
    try:
        session_id = await auth_browser.start(user_id, store)
    except ValueError as e:
        return {"success": False, "error": str(e)}
    return {"success": True, "session_id": session_id, "user_id": user_id}


@app.get("/api/auth/browser/screenshot/{session_id}")
async def browser_auth_screenshot(session_id: str):
    """Return the current page as a JPEG (quality 65). Refreshed on every call."""
    s = auth_browser.get(session_id)
    if not s:
        return Response(status_code=404)
    jpeg = await s.screenshot_jpeg()
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/auth/browser/event/{session_id}")
async def browser_auth_event(session_id: str, request: Request):
    """Forward a mouse/keyboard/scroll event to the headless browser.

    Body shapes:
      {type: "click",  nx: float, ny: float}   — normalised 0-1 coords
      {type: "type",   text: str}               — printable character(s)
      {type: "key",    key: str}                — Playwright key name (Enter, Tab…)
      {type: "scroll", delta_y: float}
    """
    s = auth_browser.get(session_id)
    if not s:
        return {"success": False, "error": "session not found or expired"}
    body = await request.json()
    ev = body.get("type")
    try:
        if ev == "click":
            await s.click(float(body["nx"]), float(body["ny"]))
        elif ev == "type":
            await s.type_text(str(body["text"]))
        elif ev == "key":
            await s.key_press(str(body["key"]))
        elif ev == "scroll":
            await s.scroll(float(body["delta_y"]))
        else:
            return {"success": False, "error": f"unknown event type: {ev}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    return {"success": True}


@app.get("/api/auth/browser/check/{session_id}")
async def browser_auth_check(session_id: str):
    """Poll for auth completion.

    Returns {done: false} until the store's auth cookie appears, then saves
    the cookies to SQLite, closes the browser, and returns {done: true, user_id, store}.
    """
    s = auth_browser.get(session_id)
    if not s:
        return {"done": False, "error": "session not found or expired"}
    cookies = await s.get_auth_cookies()
    if cookies:
        user_store.connect_store(s.user_id, s.store, cookies)
        print(f"[browser-auth] saved {len(cookies)} cookies for "
              f"{s.store} user {s.user_id[:8]}…")
        await auth_browser.close(session_id)
        return {"done": True, "user_id": s.user_id, "store": s.store}
    s.touch()
    return {"done": False}


@app.delete("/api/auth/browser/session/{session_id}")
async def browser_auth_close(session_id: str):
    """Cancel and close a browser auth session (e.g. user clicked Cancel)."""
    await auth_browser.close(session_id)
    return {"success": True}


# ── Mobile compatibility endpoints ────────────────────────────────────────────
# These match the API contract that scan2order1's mobile app expects exactly.

@app.post("/connect-account")
async def mobile_connect_account(request: Request):
    """Mobile compat: POST /connect-account → /api/auth/connect."""
    body = await request.json()
    user_id = _require_user_id(body.get("user_id"))
    store = body.get("store", "").lower().strip()
    cookies = body.get("cookies") or {}

    if not user_id:
        return {"success": False, "error": "missing user_id"}
    if store not in _STORE_DISPLAY:
        return {"success": False, "error": f"unknown store: {store}"}
    if not cookies:
        return {"success": False, "error": "missing cookies"}

    user_store.connect_store(user_id, store, cookies)
    print(f"[auth] connected {store} for user {user_id[:8]}...")
    return {"success": True}


@app.get("/account-status/{user_id}")
async def mobile_account_status(user_id: str):
    """Mobile compat: GET /account-status/{user_id}

    Returns {bigbasket_connected: bool, blinkit_connected: bool, zepto_connected: bool}.
    """
    uid = _require_user_id(user_id)
    if not uid:
        return {"bigbasket_connected": False, "blinkit_connected": False,
                "zepto_connected": False}
    stores_data = user_store.get_user_stores(uid)
    return {
        "bigbasket_connected": bool(stores_data.get("bigbasket", {}).get("connected")),
        "blinkit_connected": bool(stores_data.get("blinkit", {}).get("connected")),
        "zepto_connected": bool(stores_data.get("zepto", {}).get("connected")),
    }


@app.post("/scan")
async def mobile_scan(image: UploadFile = File(...)):
    """Mobile compat: POST /scan - OCR image → {success, items: [str]}."""
    try:
        raw = await image.read()
        result = ocr_module.extract_grocery_list(raw)
        if "error" in result:
            return {"success": False, "error": result["error"], "items": []}
        return {"success": True, "items": result["items"]}
    except Exception as e:
        print(f"[scan] error: {e}")
        return {"success": False, "error": str(e), "items": []}


@app.post("/search")
async def mobile_search(request: Request):
    """Mobile compat: POST /search - compare items across stores.

    Body: {items: [{name: str}], user_id: str, city?: str (ignored)}
    Returns: {success, products: [{item, name, price, weight, url, store,
              prod_id, fc_id, recommended, reason}]}
    """
    body = await request.json()
    user_id = _require_user_id(body.get("user_id"))
    items = body.get("items", [])

    if not user_id:
        return {"success": False, "error": "missing user_id", "products": []}
    if not items:
        return {"success": False, "error": "missing items", "products": []}

    available = _get_available_stores(user_id)
    if not available:
        return {"success": False,
                "error": "No stores connected. Open the app and log in to BigBasket, Blinkit, or Zepto.",
                "products": []}

    all_products = []
    for item_obj in items:
        item_name = item_obj.get("name", "").strip()
        if not item_name:
            continue

        try:
            entry = await ranker.compare_one_item(
                {"name": item_name}, user_id, available
            )
        except Exception as e:
            print(f"[search] compare_one_item error for '{item_name}': {e}")
            continue

        cheapest_store = entry.get("cheapest_app")
        selected_pid = entry.get("selected_pid")

        for store_name, products in (entry.get("prices") or {}).items():
            store_display = _STORE_DISPLAY.get(store_name, store_name)
            for p in products[:3]:
                pid = p.get("product_id") or ""
                is_recommended = (
                    store_name == cheapest_store and pid == selected_pid
                )
                all_products.append({
                    "item": item_name,
                    "name": p.get("name") or "",
                    "price": float(p.get("sale_price") or p.get("price") or 0),
                    "weight": p.get("unit") or "",
                    "url": "",
                    "store": store_display,
                    "prod_id": pid,
                    "fc_id": p.get("fc_id"),
                    "recommended": is_recommended,
                    "reason": "Best value" if is_recommended else "",
                    # Extra fields for multi-store mobile UI
                    "app": store_name,
                    "image_url": p.get("image_url") or "",
                })

    return {"success": True, "products": all_products}


@app.post("/order")
async def mobile_order(request: Request):
    """Mobile compat: POST /order - add items to cart.

    Body: {user_id, items: [{prod_id, fc_id, qty, app?}]}
    Groups items by store and calls the appropriate add_to_cart API.
    Returns: {success, cart_url, added: [...], failed: [...]}
    """
    body = await request.json()
    user_id = _require_user_id(body.get("user_id"))
    items = body.get("items", [])

    if not user_id:
        return {"success": False, "error": "missing user_id"}
    if not items:
        return {"success": True, "added": [], "failed": [],
                "cart_url": bigbasket.checkout_url()}

    added = []
    failed = []

    for item in items:
        prod_id = str(item.get("prod_id") or "")
        fc_id = item.get("fc_id")
        qty = max(1, int(item.get("qty") or 1))
        store = (item.get("app") or item.get("store") or "bigbasket").lower()

        if not prod_id:
            failed.append({**item, "reason": "missing prod_id"})
            continue

        try:
            if store == "bigbasket":
                result = await bigbasket.add_to_cart_api(
                    user_id, prod_id, count=qty, fc_id=fc_id
                )
                cart_url = bigbasket.checkout_url()
            elif store == "blinkit":
                result = await blinkit.add_to_cart_api(user_id, prod_id, count=qty)
                cart_url = blinkit.checkout_url()
            elif store == "zepto":
                result = await zepto.add_all_to_cart_api(
                    user_id, [{"product_id": prod_id, "count": qty}]
                )
                if result.get("success"):
                    result = {"success": True, "count_added": qty}
                cart_url = zepto.checkout_url()
            else:
                failed.append({**item, "reason": f"unknown store: {store}"})
                continue

            if result.get("success"):
                added.append({**item, "count_added": result.get("count_added", qty)})
            else:
                failed.append({**item, "reason": result.get("reason", "unknown")})
        except Exception as e:
            print(f"[order] error for pid={prod_id} store={store}: {e}")
            failed.append({**item, "reason": str(e)})

    # Default cart URL: first store that has added items
    _store_urls = {"bigbasket": bigbasket.checkout_url(),
                   "blinkit": blinkit.checkout_url(),
                   "zepto": zepto.checkout_url()}
    first_store = (added[0].get("app") or added[0].get("store") or "bigbasket").lower() \
        if added else "bigbasket"

    return {
        "success": len(added) > 0,
        "cart_url": _store_urls.get(first_store, bigbasket.checkout_url()),
        "added": added,
        "failed": failed,
    }


# ── OCR endpoint ──────────────────────────────────────────────────────────────

@app.post("/api/ocr")
async def api_ocr(image: UploadFile = File(...)):
    """Extract grocery list items from an uploaded image via Tesseract."""
    try:
        raw = await image.read()
        result = ocr_module.extract_grocery_list(raw)
        if "error" in result:
            return {"error": result["error"], "items": []}
        return {"raw_text": result.get("raw_text", ""), "items": result["items"]}
    except Exception as e:
        print(f"[api/ocr] error: {e}")
        return {"error": str(e), "items": []}


# ── Search & compare (web UI) ─────────────────────────────────────────────────

@app.post("/api/search")
async def api_search(request: Request):
    """Search ONE item across all connected stores for a user.

    Body: {query, user_id}
    """
    body = await request.json()
    user_id = _require_user_id(body.get("user_id"))
    query = body.get("query", "").strip()

    if not user_id:
        return {"error": "missing user_id"}
    if not query:
        return {"error": "missing query"}

    available = _get_available_stores(user_id)
    if not available:
        return {"error": "No stores connected."}

    from stores import bigbasket as bb, blinkit as bl, zepto as z
    _search_fns = {"bigbasket": bb.search_item_api,
                   "blinkit": bl.search_item_api,
                   "zepto": z.search_item_api}

    results = {}

    async def search_one(store_name: str):
        fn = _search_fns.get(store_name)
        if not fn:
            return
        try:
            products = await fn(user_id, query)
            cheapest = None
            if products:
                cheapest = min(products,
                               key=lambda p: p.get("sale_price") or p.get("price") or float("inf"))
            results[store_name] = {"success": True, "products": products,
                                   "cheapest": cheapest}
        except Exception as e:
            print(f"[api/search][{store_name}] error: {e}")
            results[store_name] = {"success": False, "error": str(e), "products": []}

    await asyncio.gather(*[search_one(s) for s in available])

    overall_cheapest = None
    for r in results.values():
        c = r.get("cheapest")
        if not c:
            continue
        price = c.get("sale_price") or c.get("price") or float("inf")
        oc_price = (overall_cheapest.get("sale_price") or
                    overall_cheapest.get("price") or float("inf")) \
            if overall_cheapest else float("inf")
        if price < oc_price:
            overall_cheapest = c

    return {"query": query, "results": results, "cheapest": overall_cheapest}


@app.post("/api/compare")
async def api_compare(request: Request):
    """Compare a full grocery list across all connected stores.

    Body: {items: [{name, qty?, count?}], user_id}
    """
    body = await request.json()
    user_id = _require_user_id(body.get("user_id"))
    items = body.get("items", [])

    if not user_id:
        return {"error": "missing user_id"}
    if not items:
        return {"error": "missing items"}

    available = _get_available_stores(user_id)
    if not available:
        return {"error": "No stores connected. Link your stores via the mobile app."}

    skipped = [s for s in _STORE_DISPLAY if s not in available]
    print(f"\n[compare] {len(items)} items across {available} "
          f"(skipping: {skipped or 'none'})")

    _compare_progress[user_id] = {
        "running": True, "total": len(items), "done": 0, "current": "",
    }

    try:
        comparison = []
        for i, item in enumerate(items):
            label = f"{item.get('name', '')} {item.get('qty', '')}".strip()
            _compare_progress[user_id]["current"] = label
            _compare_progress[user_id]["done"] = i
            entry = await ranker.compare_one_item(item, user_id, available)
            comparison.append(entry)
        _compare_progress[user_id]["done"] = len(items)

        carts = ranker.build_carts_from_comparison(comparison)
        grand_total = sum(c["total"] for c in carts.values())
        items_found = sum(1 for c in comparison if c["cheapest_app"])

        print(f"[compare] DONE. Found {items_found}/{len(items)}, "
              f"grand total ₹{grand_total:.0f}\n")

        return {
            "comparison": comparison,
            "carts": carts,
            "grand_total": grand_total,
            "skipped_apps": skipped,
            "summary": {
                "total_items": len(items),
                "items_found": items_found,
                "apps_used": len(carts),
            },
        }
    finally:
        _compare_progress[user_id] = _new_progress()


@app.get("/api/compare/progress")
async def api_compare_progress(user_id: str):
    return dict(_compare_progress.get(user_id, _new_progress()))


@app.post("/api/compare/item")
async def api_compare_item(request: Request):
    """Re-compare a single item (e.g. after user tweaks its quantity).

    Body: {item: {name, qty?}, user_id}
    """
    body = await request.json()
    user_id = _require_user_id(body.get("user_id"))
    item = body.get("item")

    if not user_id:
        return {"error": "missing user_id"}
    if not item or not item.get("name"):
        return {"error": "missing item with name"}

    available = _get_available_stores(user_id)
    if not available:
        return {"error": "No stores connected."}

    entry = await ranker.compare_one_item(item, user_id, available)
    return {"entry": entry}


# ── Cart (web UI) ─────────────────────────────────────────────────────────────

@app.post("/api/cart/add-all")
async def api_cart_add_all(request: Request):
    """Add items to carts across stores in parallel.

    Body: {carts: {store_name: {items: [{product_id, search_query, count, ...}]}},
           user_id}
    """
    body = await request.json()
    user_id = _require_user_id(body.get("user_id"))
    carts = body.get("carts", {})

    if not user_id:
        return {"error": "missing user_id"}

    results: dict = {}
    total_items = sum(len(c.get("items", [])) for c in carts.values())
    _cart_progress[user_id] = {
        "running": True, "total": total_items, "done": 0, "current": "",
    }
    done_count = 0

    async def add_for_store(store_name: str, cart_data: dict):
        nonlocal done_count
        app_results: dict = {"added": [], "failed": []}
        results[store_name] = app_results
        all_items = cart_data.get("items", [])

        valid_items = []
        for item in all_items:
            pid = item.get("product_id")
            if not pid or len(str(pid)) < 4 or str(pid).startswith("gen-"):
                app_results["failed"].append(item)
                done_count += 1
                _cart_progress[user_id]["done"] = done_count
                continue
            valid_items.append(item)

        # Zepto: batched API
        if store_name == "zepto" and valid_items:
            _cart_progress[user_id]["current"] = "zepto: (batch add)"
            try:
                batch = await zepto.add_all_to_cart_api(user_id, valid_items)
            except Exception as e:
                print(f"[cart][zepto] batch raised: {e}")
                batch = {"success": False}

            if batch.get("success"):
                item_results = batch.get("items") or []
                for i, item in enumerate(valid_items):
                    ir = item_results[i] if i < len(item_results) else {}
                    try:
                        requested = max(1, min(99, int(item.get("count") or 1)))
                    except (TypeError, ValueError):
                        requested = 1
                    if ir.get("success"):
                        app_results["added"].append(
                            {**item, "count_added": ir.get("count_added", requested),
                             "count_requested": requested})
                    else:
                        app_results["failed"].append(
                            {**item, "failed_reason": ir.get("reason", "")})
                    done_count += 1
                    _cart_progress[user_id]["done"] = done_count
                return

        # Per-item path: BB, Blinkit, or fallback for Zepto
        for item in valid_items:
            _cart_progress[user_id]["current"] = (
                f"{store_name}: {item.get('name', '')}".strip()
            )
            pid = str(item.get("product_id") or "")
            try:
                count = max(1, min(99, int(item.get("count") or 1)))
            except (TypeError, ValueError):
                count = 1

            try:
                if store_name == "bigbasket":
                    fc_id = item.get("fc_id")
                    result = await bigbasket.add_to_cart_api(
                        user_id, pid, count=count, fc_id=fc_id
                    )
                elif store_name == "blinkit":
                    result = await blinkit.add_to_cart_api(user_id, pid, count=count)
                elif store_name == "zepto":
                    r = await zepto.add_all_to_cart_api(
                        user_id, [{**item, "product_id": pid, "count": count}]
                    )
                    result = {"success": r.get("success", False),
                              "count_added": count}
                else:
                    result = {"success": False, "reason": f"unknown store: {store_name}"}

                if result.get("success"):
                    app_results["added"].append(
                        {**item, "count_added": result.get("count_added", count),
                         "count_requested": count})
                else:
                    app_results["failed"].append(
                        {**item, "failed_reason": result.get("reason", "")})
            except Exception as e:
                print(f"[cart][{store_name}] error: {e}")
                app_results["failed"].append({**item, "failed_reason": str(e)})

            done_count += 1
            _cart_progress[user_id]["done"] = done_count

    try:
        await asyncio.gather(*[
            add_for_store(s, cd) for s, cd in carts.items()
        ])
        return {"results": results}
    finally:
        _cart_progress[user_id] = _new_progress()


@app.get("/api/cart/progress")
async def api_cart_progress(user_id: str):
    return dict(_cart_progress.get(user_id, _new_progress()))


# ── App info ──────────────────────────────────────────────────────────────────

@app.get("/api/apps")
async def api_apps():
    return {
        name: {"display_name": display, "base_url": _store_base(name)}
        for name, display in _STORE_DISPLAY.items()
    }


def _store_base(name: str) -> str:
    return {
        "bigbasket": bigbasket.BASE_URL,
        "blinkit": blinkit.BASE_URL,
        "zepto": zepto.BASE_URL,
    }.get(name, "")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
