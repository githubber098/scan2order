"""backend/server.py - scan2order FastAPI backend.

Architecture:
  - Multi-user: user_id from X-User-ID header on every request.
  - No Playwright: all store access via httpx API calls only.
  - Cloud-ready: stateless except for Upstash Redis (session store).
  - Three stores: BigBasket, Zepto, Blinkit.
  - OCR: Tesseract via pytesseract.

Mobile app (Expo) logs in on device via WebView → extracts cookies →
POST /api/connect/{store}  (with user_id header).

Web users enter a link code from the mobile app → GET /api/link/use/{code}
→ receive user_id → store in sessionStorage → attach to every request.
"""

import asyncio
import os
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Header, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# ── Module imports ──────────────────────────────────────────────

from storage.user_store import (
    get_user_stores,
    connect_store,
    disconnect_store,
    is_store_connected,
    get_store_cookies,
    get_store_local_storage,
    get_store_session,
    get_connected_stores,
    set_user_location,
    get_user_lat_lon,
    generate_link_code,
    resolve_link_code,
    consume_link_code,
)

from stores import bigbasket, zepto, blinkit
from stores.zepto import build_session as zepto_build_session
from ranker import (
    filter_by_query_relevance,
    _compare_one_item,
    _selected_product,
    _build_carts_from_comparison,
    _product_price,
)
from ocr import run_ocr, OCR_AVAILABLE, OCR_ERROR

# ── App setup ───────────────────────────────────────────────────

app = FastAPI(title="scan2order", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_BASE_DIR, "templates"))

# ── Multi-user progress tracking ────────────────────────────────
# Keyed by user_id so concurrent users don't clobber each other.

_compare_progress: dict[str, dict] = {}
_cart_progress: dict[str, dict] = {}


def _get_compare_progress(user_id: str) -> dict:
    return _compare_progress.get(user_id, {
        "running": False, "total": 0, "done": 0, "current": "",
    })


def _get_cart_progress(user_id: str) -> dict:
    return _cart_progress.get(user_id, {
        "running": False, "total": 0, "done": 0, "current": "",
    })


# ── Helpers ─────────────────────────────────────────────────────

def _get_user_id(x_user_id: Optional[str]) -> str:
    """Extract user_id from header, raising 400 if missing."""
    if not x_user_id:
        raise HTTPException(status_code=400, detail="X-User-ID header required")
    return x_user_id.strip()


async def _search_bigbasket(query: str, user_id: str) -> list[dict]:
    cookies = get_store_cookies(user_id, "bigbasket")
    if not cookies.get("BBAUTHTOKEN"):
        return []
    return await bigbasket.search_item_api(query, cookies)


async def _search_zepto(query: str, user_id: str) -> list[dict]:
    sess = get_store_session(user_id, "zepto")
    if not sess.get("connected"):
        return []
    session = zepto_build_session(sess["cookies"], sess["local_storage"])
    return await zepto.search_item_api(query, session)


async def _search_blinkit(query: str, user_id: str) -> list[dict]:
    lat, lon = get_user_lat_lon(user_id)
    if not lat or not lon:
        return []
    cookies = get_store_cookies(user_id, "blinkit")
    return await blinkit.search_item_api(query, lat, lon, cookies or None)


_STORE_SEARCHERS = {
    "bigbasket": _search_bigbasket,
    "zepto": _search_zepto,
    "blinkit": _search_blinkit,
}


async def _search_all_stores(query: str, user_id: str, target_stores: list[str]) -> dict:
    """Search all target stores in parallel. Returns {store: [products]}."""
    results: dict[str, list[dict]] = {}

    async def _one(store: str):
        fn = _STORE_SEARCHERS.get(store)
        if not fn:
            return
        try:
            products = await fn(query, user_id)
            results[store] = products
        except Exception as e:
            print(f"[server] search {store} error: {e}")
            results[store] = []

    await asyncio.gather(*[_one(s) for s in target_stores])
    return results


# ── Web UI ───────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def web_ui(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Status ───────────────────────────────────────────────────────

@app.get("/api/status")
async def status():
    """Server health check + feature flags."""
    return {
        "status": "ok",
        "ocr_available": OCR_AVAILABLE,
        "ocr_error": OCR_ERROR if not OCR_AVAILABLE else "",
        "stores": ["bigbasket", "zepto", "blinkit"],
    }


# ── Link code (mobile → web pairing) ────────────────────────────

@app.post("/api/link/generate")
async def link_generate(x_user_id: Optional[str] = Header(None)):
    """Generate a short-lived link code for web UI to use.
    Call from the mobile app after connecting at least one store.
    """
    user_id = _get_user_id(x_user_id)
    code = generate_link_code(user_id)
    return {"code": code, "expires_in_seconds": 900}


@app.post("/api/link/use/{code}")
async def link_use(code: str):
    """Consume a link code and return the user_id.
    Call from the web UI when the user enters their code.
    The code is deleted after use (one-time).
    """
    user_id = consume_link_code(code)
    if not user_id:
        raise HTTPException(status_code=404, detail="Code not found or expired")
    stores = get_connected_stores(user_id)
    return {
        "user_id": user_id,
        "connected_stores": stores,
    }


@app.get("/api/link/peek/{code}")
async def link_peek(code: str):
    """Check if a link code is still valid without consuming it.
    Use this from the mobile app to confirm the web UI scanned it.
    """
    user_id = resolve_link_code(code)
    if not user_id:
        raise HTTPException(status_code=404, detail="Code not found or expired")
    return {"valid": True, "user_id": user_id}


# ── Store connection (called from mobile app) ────────────────────

@app.get("/api/stores")
async def list_stores(x_user_id: Optional[str] = Header(None)):
    """List all stores and their connection status for this user."""
    user_id = _get_user_id(x_user_id)
    all_stores = get_user_stores(user_id)
    lat, lon = get_user_lat_lon(user_id)
    return {
        "stores": {
            "bigbasket": {
                "connected": all_stores.get("bigbasket", {}).get("connected", False),
            },
            "zepto": {
                "connected": all_stores.get("zepto", {}).get("connected", False),
            },
            "blinkit": {
                "connected": all_stores.get("blinkit", {}).get("connected", False),
                "location_set": bool(lat and lon),
                "lat": lat,
                "lon": lon,
            },
        }
    }


@app.post("/api/connect/{store}")
async def connect_store_endpoint(
    store: str,
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """Connect a store by saving its cookies (and optionally localStorage).

    Body:
        {
          "cookies": {"BBAUTHTOKEN": "...", ...},
          "local_storage": {"device_id": "..."}   // optional, for Zepto
        }

    Called from the mobile app's WebView login screen after the user logs in.
    """
    user_id = _get_user_id(x_user_id)
    if store not in ("bigbasket", "zepto", "blinkit"):
        raise HTTPException(status_code=400, detail=f"Unknown store: {store}")

    body = await request.json()
    cookies = body.get("cookies") or {}
    local_storage = body.get("local_storage") or {}

    if not cookies:
        raise HTTPException(status_code=400, detail="cookies required")

    connect_store(user_id, store, cookies, local_storage)
    print(f"[server] user={user_id} connected {store} "
          f"({len(cookies)} cookies, {len(local_storage)} ls keys)")
    return {"success": True, "store": store, "connected": True}


@app.post("/api/disconnect/{store}")
async def disconnect_store_endpoint(
    store: str,
    x_user_id: Optional[str] = Header(None),
):
    """Disconnect a store by removing its cookies from Redis."""
    user_id = _get_user_id(x_user_id)
    disconnect_store(user_id, store)
    return {"success": True, "store": store, "connected": False}


@app.post("/api/location")
async def set_location(
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """Store user's lat/lon for Blinkit location-aware search.

    Body: {"lat": "12.9942483", "lon": "77.7307156"}
    """
    user_id = _get_user_id(x_user_id)
    body = await request.json()
    lat = str(body.get("lat") or "")
    lon = str(body.get("lon") or "")
    if not lat or not lon:
        raise HTTPException(status_code=400, detail="lat and lon required")
    set_user_location(user_id, lat, lon)
    return {"success": True, "lat": lat, "lon": lon}


# ── OCR ──────────────────────────────────────────────────────────

@app.post("/api/ocr")
async def ocr_grocery_list(
    image: UploadFile = File(...),
    x_user_id: Optional[str] = Header(None),
):
    """Extract grocery list items from an uploaded image via Tesseract.

    Accepts JPEG, PNG, or any image format Pillow understands.
    Returns: {"items": [...], "raw_text": "..."}
    """
    # user_id not strictly needed for OCR but kept for consistency.
    _ = _get_user_id(x_user_id)

    raw = await image.read()
    result = run_ocr(raw)
    return result


# ── Search ───────────────────────────────────────────────────────

@app.post("/api/search")
async def search(
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """Search ONE item across all connected stores in parallel.

    Body: {"query": "tomato", "stores": ["bigbasket", "zepto"]}
    stores is optional; defaults to all connected stores.

    Returns: {
      "query": str,
      "results": {store: {success, products, cheapest}},
      "cheapest": product_dict | null,
      "skipped_stores": [store, ...]
    }
    """
    user_id = _get_user_id(x_user_id)
    body = await request.json()
    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query required")

    connected = get_connected_stores(user_id)
    requested = body.get("stores", connected)
    skipped = [s for s in requested if s not in connected]
    target = [s for s in requested if s in connected]

    if not target:
        return {
            "error": "No connected stores",
            "query": query,
            "results": {},
            "cheapest": None,
            "skipped_stores": skipped,
        }

    store_results = await _search_all_stores(query, user_id, target)

    results: dict = {}
    overall_cheapest = None

    for store, products in store_results.items():
        cheapest = None
        if products:
            cheapest = min(products, key=_product_price)
        results[store] = {
            "success": True,
            "products": products,
            "cheapest": cheapest,
        }
        if cheapest:
            price = _product_price(cheapest)
            if overall_cheapest is None or price < _product_price(overall_cheapest):
                overall_cheapest = cheapest

    return {
        "query": query,
        "results": results,
        "cheapest": overall_cheapest,
        "skipped_stores": skipped,
    }


# ── Compare ──────────────────────────────────────────────────────

@app.post("/api/compare")
async def compare(
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """Compare a full grocery list across all connected stores.

    Body: {
      "items": [{"name": "Tomatoes", "qty": "500g", "count": 2}, ...]
    }

    Returns: {
      "comparison": [...],   // one entry per item
      "carts": {store: {items, total}},
      "grand_total": float,
      "skipped_stores": [...],
      "summary": {total_items, items_found, apps_used}
    }
    """
    user_id = _get_user_id(x_user_id)
    body = await request.json()
    items = body.get("items", [])
    if not items:
        raise HTTPException(status_code=400, detail="items required")

    connected = get_connected_stores(user_id)
    if not connected:
        return {
            "error": "No connected stores. Connect at least one store first.",
            "comparison": [],
            "carts": {},
            "grand_total": 0,
        }

    all_stores = ["bigbasket", "zepto", "blinkit"]
    skipped = [s for s in all_stores if s not in connected]

    print(f"\n[compare] user={user_id}: {len(items)} items across {connected} "
          f"(skipping {skipped or 'none'})")

    _compare_progress[user_id] = {
        "running": True, "total": len(items), "done": 0, "current": "",
    }

    try:
        comparison = []
        # Items run SEQUENTIALLY. Each item parallelizes its store searches
        # internally. Sequential-per-item avoids bot-detection rate limits.
        for i, item in enumerate(items):
            label = f"{item.get('name', '')} {item.get('qty', '')}".strip()
            _compare_progress[user_id]["current"] = label
            _compare_progress[user_id]["done"] = i

            # Search all connected stores in parallel for this item.
            search_query = label
            store_results = await _search_all_stores(search_query, user_id, connected)

            # Sort each store's results cheapest-first.
            sorted_results = {
                store: sorted(prods, key=_product_price)
                for store, prods in store_results.items()
            }

            entry = _compare_one_item(item, sorted_results)
            comparison.append(entry)

        _compare_progress[user_id]["done"] = len(items)

        carts = _build_carts_from_comparison(comparison)
        grand_total = sum(c["total"] for c in carts.values())
        items_found = sum(1 for c in comparison if c["cheapest_app"])

        print(f"[compare] user={user_id}: DONE. Found {items_found}/{len(items)} "
              f"items, grand total ₹{grand_total:.0f}\n")

        return {
            "comparison": comparison,
            "carts": carts,
            "grand_total": grand_total,
            "skipped_stores": skipped,
            "summary": {
                "total_items": len(items),
                "items_found": items_found,
                "apps_used": len(carts),
            },
        }
    finally:
        _compare_progress[user_id]["running"] = False
        _compare_progress[user_id]["current"] = ""


@app.get("/api/compare/progress")
async def compare_progress(x_user_id: Optional[str] = Header(None)):
    """Poll this while a compare is in flight to show a progress bar."""
    user_id = _get_user_id(x_user_id)
    return _get_compare_progress(user_id)


@app.post("/api/compare/item")
async def compare_item(
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """Re-compare a SINGLE item (e.g. after user tweaks quantity).

    Body: {"item": {"name": "Tomatoes", "qty": "500g"}}

    Returns: {"entry": comparison_entry_dict}
    """
    user_id = _get_user_id(x_user_id)
    body = await request.json()
    item = body.get("item")
    if not item or not item.get("name"):
        raise HTTPException(status_code=400, detail="item.name required")

    connected = get_connected_stores(user_id)
    if not connected:
        return {"error": "No connected stores"}

    search_query = f"{item.get('name', '')} {item.get('qty', '')}".strip()
    store_results = await _search_all_stores(search_query, user_id, connected)
    sorted_results = {
        store: sorted(prods, key=_product_price)
        for store, prods in store_results.items()
    }
    entry = _compare_one_item(item, sorted_results)
    return {"entry": entry}


# ── Cart ─────────────────────────────────────────────────────────

@app.post("/api/cart/add-all")
async def add_all_to_carts(
    request: Request,
    x_user_id: Optional[str] = Header(None),
):
    """Add items to carts across all stores in parallel.

    Body: {
      "carts": {
        "bigbasket": {"items": [{product_id, name, count, fc_id, ...}]},
        "zepto":     {"items": [{product_id, name, count, ...}]},
        "blinkit":   {"items": [{product_id, name, count, _cart_item, ...}]}
      }
    }

    Returns: {"results": {store: {added: [...], failed: [...]}}}
    """
    user_id = _get_user_id(x_user_id)
    body = await request.json()
    carts = body.get("carts", {})

    total_items = sum(len(c.get("items", [])) for c in carts.values())
    _cart_progress[user_id] = {
        "running": True, "total": total_items, "done": 0, "current": "",
    }
    done_count = 0
    results: dict = {}

    async def add_bigbasket(cart_data: dict):
        nonlocal done_count
        cookies = get_store_cookies(user_id, "bigbasket")
        app_results: dict = {"added": [], "failed": []}
        results["bigbasket"] = app_results

        for item in cart_data.get("items", []):
            pid = item.get("product_id")
            if not pid or str(pid).startswith("gen-"):
                app_results["failed"].append(item)
                done_count += 1
                _cart_progress[user_id]["done"] = done_count
                continue

            _cart_progress[user_id]["current"] = f"BigBasket: {item.get('name', '')}"
            try:
                count = max(1, min(99, int(item.get("count") or 1)))
            except (TypeError, ValueError):
                count = 1

            result = await bigbasket.add_to_cart_api(
                product_id=str(pid),
                query=item.get("search_query", item.get("name", "")),
                count=count,
                fc_id=item.get("fc_id"),
                cookies=cookies,
            )
            if result.get("success"):
                rec = dict(item)
                rec["count_added"] = result.get("count_added", count)
                rec["count_requested"] = count
                app_results["added"].append(rec)
            else:
                rec = dict(item)
                rec["failed_reason"] = result.get("reason", "")
                app_results["failed"].append(rec)
            done_count += 1
            _cart_progress[user_id]["done"] = done_count

    async def add_zepto(cart_data: dict):
        nonlocal done_count
        sess = get_store_session(user_id, "zepto")
        app_results: dict = {"added": [], "failed": []}
        results["zepto"] = app_results

        if not sess.get("connected"):
            for item in cart_data.get("items", []):
                rec = dict(item)
                rec["failed_reason"] = "not connected"
                app_results["failed"].append(rec)
                done_count += 1
                _cart_progress[user_id]["done"] = done_count
            return

        session = zepto_build_session(sess["cookies"], sess["local_storage"])
        valid_items = []
        for item in cart_data.get("items", []):
            pid = item.get("product_id")
            if not pid or str(pid).startswith("gen-"):
                rec = dict(item)
                rec["failed_reason"] = "invalid product_id"
                app_results["failed"].append(rec)
                done_count += 1
                _cart_progress[user_id]["done"] = done_count
                continue
            valid_items.append(item)

        if not valid_items:
            return

        _cart_progress[user_id]["current"] = "Zepto: (batch add)"
        batch = await zepto.add_all_to_cart_api(valid_items, session, user_id=user_id)

        if batch.get("success"):
            item_results = batch.get("items") or []
            for i, item in enumerate(valid_items):
                ir = item_results[i] if i < len(item_results) else {}
                try:
                    requested = max(1, min(99, int(item.get("count") or 1)))
                except (TypeError, ValueError):
                    requested = 1
                if ir.get("success"):
                    rec = dict(item)
                    rec["count_added"] = ir.get("count_added", requested)
                    rec["count_requested"] = requested
                    app_results["added"].append(rec)
                else:
                    rec = dict(item)
                    rec["failed_reason"] = ir.get("reason", "")
                    app_results["failed"].append(rec)
                done_count += 1
                _cart_progress[user_id]["done"] = done_count
        else:
            for item in valid_items:
                rec = dict(item)
                rec["failed_reason"] = batch.get("reason", "batch failed")
                app_results["failed"].append(rec)
                done_count += 1
                _cart_progress[user_id]["done"] = done_count

    async def add_blinkit(cart_data: dict):
        nonlocal done_count
        cookies = get_store_cookies(user_id, "blinkit")
        lat, lon = get_user_lat_lon(user_id)
        app_results: dict = {"added": [], "failed": []}
        results["blinkit"] = app_results

        for item in cart_data.get("items", []):
            pid = item.get("product_id")
            if not pid or str(pid).startswith("gen-"):
                rec = dict(item)
                rec["failed_reason"] = "invalid product_id"
                app_results["failed"].append(rec)
                done_count += 1
                _cart_progress[user_id]["done"] = done_count
                continue

            _cart_progress[user_id]["current"] = f"Blinkit: {item.get('name', '')}"
            try:
                count = max(1, min(99, int(item.get("count") or 1)))
            except (TypeError, ValueError):
                count = 1

            # Add once, then loop for count > 1 (Blinkit has no bulk-add endpoint).
            success_count = 0
            for _ in range(count):
                result = await blinkit.add_to_cart_api(
                    product_id=str(pid),
                    merchant_id=str(item.get("merchant_id") or ""),
                    cart_item=item.get("_cart_item") or {},
                    cookies=cookies,
                    lat=lat,
                    lon=lon,
                )
                if result.get("success"):
                    success_count += 1
                else:
                    break

            if success_count > 0:
                rec = dict(item)
                rec["count_added"] = success_count
                rec["count_requested"] = count
                app_results["added"].append(rec)
            else:
                rec = dict(item)
                rec["failed_reason"] = result.get("reason", "add failed")
                app_results["failed"].append(rec)
            done_count += 1
            _cart_progress[user_id]["done"] = done_count

    # Run per-store add tasks in parallel.
    _add_tasks = []
    if "bigbasket" in carts:
        _add_tasks.append(add_bigbasket(carts["bigbasket"]))
    if "zepto" in carts:
        _add_tasks.append(add_zepto(carts["zepto"]))
    if "blinkit" in carts:
        _add_tasks.append(add_blinkit(carts["blinkit"]))

    try:
        await asyncio.gather(*_add_tasks)
        return {"results": results}
    finally:
        _cart_progress[user_id]["running"] = False
        _cart_progress[user_id]["current"] = ""


@app.get("/api/cart/progress")
async def cart_progress(x_user_id: Optional[str] = Header(None)):
    """Poll this while add-all is in flight to show a progress bar."""
    user_id = _get_user_id(x_user_id)
    return _get_cart_progress(user_id)


# ── Entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
