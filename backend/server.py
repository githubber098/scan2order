"""server.py - scan2order merged backend.

Multi-user, cloud-deployable FastAPI server.
No Playwright — all store interactions via httpx.
Sessions stored in SQLite (or MySQL via MYSQL_URL env var).

Serves two audiences:
  1. Mobile app (scan2order1 API contract: /scan, /search, /order,
     /connect-account, /account-status/{user_id}, /health)
  2. Web UI (scan2order2 API contract: /api/compare, /api/cart/add-all,
     /api/ocr, /api/search, /api/version, /api/auth/*)

Web UI authentication
─────────────────────
Phone number + SMS OTP.  A 6-digit code is sent via Twilio; on correct
entry a 6-day HMAC-signed session cookie (scan2order_session) is set.
The "/" route reads this cookie and injects the user_id into the HTML.

Mobile auth is unchanged: user_id is sent in the request body.
"""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import auth_browser
import email_sender
import ocr as ocr_module
import ranker
import sms
from storage import user_store
from stores import bigbasket, blinkit, zepto

BASE_DIR = Path(__file__).parent
APP_VERSION = "1.3.0"
_TEMPLATES_DIR = BASE_DIR / "templates"
_STATIC_DIR    = BASE_DIR / "static"
_404_HTML      = BASE_DIR / "templates" / "404.html"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _asset_version() -> str:
    """Cache-busting token for static assets, derived from the newest mtime of
    app.js / theme.css. Changes on every deploy that touches them, so browsers
    fetch the new file instead of a stale cached copy."""
    try:
        mtimes = [
            (_STATIC_DIR / name).stat().st_mtime
            for name in ("app.js", "theme.css")
            if (_STATIC_DIR / name).exists()
        ]
        if mtimes:
            return str(int(max(mtimes)))
    except Exception:
        pass
    return APP_VERSION


# Exposed to every template as {{ asset_v }} so asset URLs can be versioned.
templates.env.globals["asset_v"] = _asset_version()

_SESSION_MAX_AGE = auth.SESSION_TTL   # 6 days


_VALID_THEMES = {"fresh", "night", "aurora", "mono", "light", "brutal"}


def _user_ctx(user_id: str) -> dict:
    """Build the Jinja2 template context for an authenticated user."""
    user = user_store.get_user_by_id(user_id) or {
        "user_id": user_id, "phone": None, "email": None, "name": None, "theme": "fresh",
    }
    # Normalise: theme must be a valid value
    if user.get("theme") not in _VALID_THEMES:
        user["theme"] = "fresh"
    stores_connected = {
        "bigbasket": bigbasket.is_session_valid(user_id),
        "blinkit":   blinkit.is_session_valid(user_id),
        "zepto":     zepto.is_session_valid(user_id),
    }
    return {"user": user, "user_id": user_id, "stores": stores_connected}


def _template_response(request: Request, tpl: str, extra: dict | None = None,
                       headers: dict | None = None, allow_guest: bool = False):
    """Render a Jinja2 template with common user context.

    allow_guest=True lets a logged-out visitor render the page in *guest mode*
    (browse-only). The template receives guest=True, user_id=None and a stub
    user so base.html renders; the frontend then applies feature restrictions
    and the backend enforces them on the API endpoints.
    """
    user_id = _get_session_user(request)
    if not user_id:
        if not allow_guest:
            return RedirectResponse("/login", status_code=302)
        ctx = {
            "user": {"name": None, "theme": "fresh", "phone": None, "email": None},
            "user_id": None,
            "stores": {"bigbasket": False, "blinkit": False, "zepto": False},
            "guest": True,
        }
        if extra:
            ctx.update(extra)
        resp = templates.TemplateResponse(request, tpl, ctx)
        resp.headers.setdefault("Cache-Control", "no-store")
        if headers:
            for k, v in headers.items():
                resp.headers[k] = v
        return resp
    ctx = _user_ctx(user_id)
    ctx["guest"] = False
    if extra:
        ctx.update(extra)
    resp = templates.TemplateResponse(request, tpl, ctx)
    if headers:
        for k, v in headers.items():
            resp.headers[k] = v
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def _set_session_cookie(response: Response, user_id: str) -> None:
    token = auth.create_session_token(user_id)
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,   # set True if HTTPS-only deployment
    )


def _get_session_user(request: Request) -> str | None:
    token = request.cookies.get(auth.COOKIE_NAME)
    if not token:
        return None
    return auth.verify_session_token(token)


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

# Static files (theme.css, app.js)
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Serve the branded 404 page for unknown routes; pass other HTTP errors through."""
    if exc.status_code == 404:
        html = (_404_HTML.read_text(encoding="utf-8")
                if _404_HTML.exists() else "<h1>404 Not Found</h1>")
        return HTMLResponse(content=html, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


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


def _guest_store_user() -> str | None:
    """Owner account whose connected store sessions back GUEST (logged-out)
    price searches, set via the GUEST_STORE_USER_ID env var.

    Opt-in and OFF by default: when unset, a logged-out visitor gets the full
    Shop/Compare UI but no live prices (they must log in and connect a store).
    When the owner opts in, guest searches reuse that account's sessions for
    READ-ONLY price lookups only — guests can never add to a cart (that is
    enforced separately on the cart endpoints). Intended for a personal/shared
    deployment where the owner wants visitors to see prices without an account.
    """
    uid = (os.getenv("GUEST_STORE_USER_ID") or "").strip()
    return uid or None


def _ppu_label(product: dict) -> str:
    """Human price-per-unit label, e.g. '₹0.4/100g', '₹0.5/100ml', '₹12/pc'.

    Empty string when the product has no parseable size or price.
    """
    pq = ranker._product_qty(product)
    price = ranker._product_price(product)
    if not pq or price == float("inf") or pq[0] <= 0:
        return ""
    val, kind = pq
    if kind == "mass":
        return f"₹{price / val * 100:.1f}/100g"
    if kind == "volume":
        return f"₹{price / val * 100:.1f}/100ml"
    return f"₹{price / val:.1f}/pc"


def _guest_strip_entry(entry: dict) -> dict:
    """Reduce a compare entry to the cheapest pick only (guest restriction).

    Drops the full per-store price matrix and the swap shortlist, keeping just
    the selected winning product so a logged-out user sees the cheapest option
    without the full cross-store comparison.
    """
    prod = ranker.selected_product(entry) if entry.get("cheapest_app") else {}
    return {
        "item": entry.get("item"),
        "search_query": entry.get("search_query"),
        "cheapest_app": entry.get("cheapest_app"),
        "cheapest_price": entry.get("cheapest_price"),
        "cheapest_effective_price": entry.get("cheapest_effective_price"),
        "qty_count": entry.get("qty_count", 1),
        "cheapest_product": {
            "name": prod.get("name", ""),
            "unit": prod.get("unit", ""),
            "image_url": prod.get("image_url", ""),
            "sale_price": prod.get("sale_price"),
            "price": prod.get("price"),
            "product_id": prod.get("product_id"),
            "app": prod.get("app"),
            "app_name": prod.get("app_name"),
        } if prod else None,
    }


# ── Utility ──────────────────────────────────────────────────────────────────

def _require_user_id(user_id: str | None) -> str | None:
    """Return a valid user_id or None if missing/empty."""
    if not user_id or not str(user_id).strip():
        return None
    return str(user_id).strip()


# ── Health & version ─────────────────────────────────────────────────────────

@app.get("/favicon.ico")
async def favicon():
    """Serve the SVG favicon for the browser's default /favicon.ico request.

    Pages also declare <link rel="icon" href="/static/favicon.svg">, but the
    browser still probes /favicon.ico — answering here avoids 404 noise.
    """
    fav = _STATIC_DIR / "favicon.svg"
    if fav.exists():
        return Response(content=fav.read_bytes(), media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=404)


@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


@app.get("/api/version")
async def api_version():
    return {"version": APP_VERSION}


# ── Web UI ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user_id = _get_session_user(request)
    if user_id:
        # After login, if the user has no name, redirect to onboarding first.
        user = user_store.get_user_by_id(user_id)
        if user and not user.get("name"):
            return RedirectResponse("/onboarding", status_code=302)
    # Logged-out visitors get the Compare page in guest mode (cheapest-only).
    return _template_response(request, "index.html", allow_guest=True)


@app.get("/shop", response_class=HTMLResponse)
async def shop_page(request: Request):
    # Shop is browsable by guests (Add-to-Cart is gated to logged-in users).
    return _template_response(request, "shop.html", allow_guest=True)


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    return _template_response(request, "history.html")


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    return _template_response(request, "profile.html")


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    user_id = _get_session_user(request)
    if not user_id:
        return RedirectResponse("/login", status_code=302)
    ctx = {"user_id": user_id, "user": user_store.get_user_by_id(user_id) or {}}
    return templates.TemplateResponse(request, "onboarding.html", ctx,
                                      headers={"Cache-Control": "no-store"})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _get_session_user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {})


# ── User account endpoints ────────────────────────────────────────────────────
# Passwordless: log in with either a phone (SMS OTP) or an email (email OTP).
# A masked label for logs that never leaks the full contact.

def _mask(channel: str, value: str) -> str:
    if channel == "email":
        return value.split("@")[0][:2] + "***@" + value.split("@")[-1]
    return value[:4] + "****"


def _send_otp_via(channel: str, target: str, code: str) -> str | None:
    """Dispatch an OTP to the right transport for *channel*.

    Returns None on success, or a short human-readable error string on failure.
    """
    if channel == "email":
        return email_sender.send_otp(target, code)
    return sms.send_otp(target, code)


def _otp_dev_mode() -> bool:
    """True when OTP_DEV_MODE is set — echoes OTP codes in API responses so the
    flow is testable on a server with no SMTP/SMS provider. Never set in prod."""
    return os.getenv("OTP_DEV_MODE", "").strip().lower() in ("1", "true", "yes")


def _channel_value(body: dict) -> tuple[str | None, str | None]:
    """Pull (channel, normalised_value) from a request body.

    Accepts an explicit {channel, value}, or legacy {phone} / {email} keys.
    Returns (None, None) if neither a valid channel nor value is present.
    """
    channel = (body.get("channel") or "").strip().lower()
    raw = body.get("value")
    if not channel:
        if body.get("email") is not None:
            channel, raw = "email", body.get("email")
        elif body.get("phone") is not None:
            channel, raw = "phone", body.get("phone")
    if channel not in ("phone", "email"):
        return None, None
    return channel, auth.normalize_contact(channel, raw or "")


@app.post("/api/auth/send-otp")
async def api_send_otp(request: Request):
    """Send a 6-digit OTP to a phone (SMS) or email.

    Body: {channel: "phone"|"email", value} — or legacy {phone} / {email}.
    Rate-limited to one request per 60 seconds per target.
    """
    body = await request.json()
    channel, target = _channel_value(body)
    if not channel:
        return JSONResponse({"success": False, "error": "Specify a phone or email"})
    if not target:
        label = "email address" if channel == "email" else "phone number"
        return JSONResponse({"success": False, "error": f"Invalid {label}"})

    if user_store.is_otp_rate_limited(target):
        return JSONResponse({"success": False,
                             "error": "Please wait 60 seconds before requesting a new code"})

    code = auth.generate_otp()
    user_store.save_otp(target, code)

    err = _send_otp_via(channel, target, code)
    if err:
        return JSONResponse({"success": False, "error": err})

    print(f"[auth] OTP sent via {channel} to {_mask(channel, target)}")
    resp = {"success": True}
    # Dev mode: when no real transport is configured (test instance), echo the
    # code so the flow is testable. Never enabled in production.
    if _otp_dev_mode():
        resp["dev_code"] = code
    return JSONResponse(resp)


@app.post("/api/auth/verify-otp")
async def api_verify_otp(request: Request):
    """Verify a login OTP. Creates the account if new, sets the session cookie.

    Body: {channel, value, code} — or legacy {phone, code} / {email, code}.
    """
    body = await request.json()
    channel, target = _channel_value(body)
    code = str(body.get("code", "")).strip()

    if not channel or not target:
        return JSONResponse({"success": False, "error": "Phone/email and code required"})
    if not code:
        return JSONResponse({"success": False, "error": "Code required"})

    if not user_store.verify_and_consume_otp(target, code):
        return JSONResponse({"success": False, "error": "Invalid or expired code"})

    user_id = user_store.get_or_create_user(channel, target)
    user_store.update_last_login(user_id)

    # Determine redirect: new users (no name) go to onboarding first.
    user = user_store.get_user_by_id(user_id)
    redirect = "/onboarding" if (user and not user.get("name")) else "/"

    resp = JSONResponse({"success": True, "user_id": user_id, "redirect": redirect})
    _set_session_cookie(resp, user_id)
    print(f"[auth] login via {channel} ({_mask(channel, target)}) → {user_id[:8]}…")
    return resp


@app.post("/api/auth/method/send-otp")
async def api_method_send_otp(request: Request):
    """Send an OTP to verify a SECOND contact method for the logged-in user.

    Body: {channel, value}. Requires an authenticated session.
    Rejects contacts already attached to a different account up-front.
    """
    user_id = _get_session_user(request)
    if not user_id:
        return JSONResponse({"success": False, "error": "Not signed in"}, status_code=401)

    body = await request.json()
    channel, target = _channel_value(body)
    if not channel:
        return JSONResponse({"success": False, "error": "Specify a phone or email"})
    if not target:
        label = "email address" if channel == "email" else "phone number"
        return JSONResponse({"success": False, "error": f"Invalid {label}"})

    # If the contact already belongs to ANOTHER account we still send the OTP:
    # receiving it proves the user controls the contact, which authorises
    # merging that account in at the verify step. Only block re-adding a
    # contact already on THIS account.
    owner = user_store.get_user_by_contact(channel, target)
    if owner == user_id:
        return JSONResponse({"success": False,
                             "error": f"That {channel} is already on your account"})

    if user_store.is_otp_rate_limited(target):
        return JSONResponse({"success": False,
                             "error": "Please wait 60 seconds before requesting a new code"})

    code = auth.generate_otp()
    user_store.save_otp(target, code)
    err = _send_otp_via(channel, target, code)
    if err:
        return JSONResponse({"success": False, "error": err})

    print(f"[auth] link OTP via {channel} to {_mask(channel, target)} for {user_id[:8]}…")
    resp = {"success": True}
    if _otp_dev_mode():
        resp["dev_code"] = code
    return JSONResponse(resp)


@app.post("/api/auth/method/verify")
async def api_method_verify(request: Request):
    """Verify the OTP and attach the new contact method to the logged-in user.

    Body: {channel, value, code}. Requires an authenticated session.
    """
    user_id = _get_session_user(request)
    if not user_id:
        return JSONResponse({"success": False, "error": "Not signed in"}, status_code=401)

    body = await request.json()
    channel, target = _channel_value(body)
    code = str(body.get("code", "")).strip()
    if not channel or not target:
        return JSONResponse({"success": False, "error": "Phone/email and code required"})
    if not code:
        return JSONResponse({"success": False, "error": "Code required"})

    if not user_store.verify_and_consume_otp(target, code):
        return JSONResponse({"success": False, "error": "Invalid or expired code"})

    ok, reason = user_store.attach_contact(user_id, channel, target)
    if not ok:
        return JSONResponse({"success": False, "error": reason or "Could not link"})

    print(f"[auth] linked {channel} ({_mask(channel, target)}) to {user_id[:8]}…")
    return JSONResponse({"success": True})


@app.post("/api/auth/logout")
async def api_logout():
    """Clear the session cookie."""
    resp = JSONResponse({"success": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.post("/api/auth/disconnect-all")
async def api_disconnect_all(request: Request):
    """Delete the logged-in user's store connections (keep the account)."""
    user_id = _get_session_user(request)
    if not user_id:
        return JSONResponse({"success": False, "error": "Not signed in"}, status_code=401)
    user_store.delete_user_sessions(user_id)
    print(f"[auth] data cleared (stores disconnected): {user_id[:8]}…")
    return JSONResponse({"success": True})


@app.post("/api/auth/delete")
async def api_delete_account(request: Request):
    """Permanently delete the logged-in user's account and all their data."""
    user_id = _get_session_user(request)
    if not user_id:
        return JSONResponse({"success": False, "error": "Not signed in"}, status_code=401)
    user_store.delete_user(user_id)
    resp = JSONResponse({"success": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    print(f"[auth] account deleted: {user_id[:8]}…")
    return resp


@app.get("/api/auth/me")
async def api_me(request: Request):
    """Return the currently logged-in user, or 401 if not authenticated."""
    user_id = _get_session_user(request)
    if not user_id:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    user = user_store.get_user_by_id(user_id)
    if not user:
        resp = JSONResponse({"error": "user not found"}, status_code=401)
        resp.delete_cookie(auth.COOKIE_NAME)
        return resp
    return {"user_id": user["user_id"], "phone": user["phone"], "email": user["email"],
            "name": user.get("name"), "theme": user.get("theme", "fresh")}


@app.post("/api/auth/name")
async def api_set_name(request: Request):
    """Set or update the user's display name (called from onboarding)."""
    user_id = _get_session_user(request)
    if not user_id:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    user_store.set_user_name(user_id, name)
    return JSONResponse({"success": True})


@app.post("/api/profile/theme")
async def api_set_theme(request: Request):
    """Persist the user's preferred UI theme. Body: {theme: str}. Returns 204."""
    user_id = _get_session_user(request)
    if not user_id:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    body = await request.json()
    theme = body.get("theme", "")
    if theme not in _VALID_THEMES:
        return JSONResponse({"error": f"unknown theme {theme!r}"}, status_code=400)
    user_store.set_user_theme(user_id, theme)
    return Response(status_code=204)


@app.get("/api/history")
async def api_history(request: Request):
    """Return the last 50 comparison runs for the logged-in user."""
    user_id = _get_session_user(request)
    if not user_id:
        return JSONResponse({"error": "not authenticated"}, status_code=401)
    return user_store.get_history(user_id)


# ── Store-session auth endpoints ──────────────────────────────────────────────

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
    _health_fns = {"blinkit": blinkit.session_health, "zepto": zepto.session_health}
    connected = {}
    for store in _STORE_DISPLAY:
        if not stores_data.get(store, {}).get("connected"):
            continue
        entry = {"connected": True, "healthy": True, "reason": ""}
        fn = _health_fns.get(store)
        if fn:
            try:
                h = fn(uid)
                entry["healthy"] = bool(h.get("ok"))
                entry["reason"] = h.get("reason", "")
            except Exception:
                pass
        connected[store] = entry
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
    # Tolerate an empty / missing / malformed body — a bare POST must not 500.
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    user_id = _require_user_id(body.get("user_id")) or str(uuid.uuid4())
    # geolocation forwarded from the user's real browser so store location
    # prompts ("Use my location") resolve correctly inside Playwright.
    geo_raw = body.get("geolocation")
    geolocation = None
    if isinstance(geo_raw, dict):
        try:
            geolocation = {
                "latitude": float(geo_raw["latitude"]),
                "longitude": float(geo_raw["longitude"]),
            }
        except (KeyError, ValueError, TypeError):
            geolocation = None
    try:
        session_id = await auth_browser.start(user_id, store, geolocation)
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


@app.websocket("/api/auth/browser/ws/{session_id}")
async def browser_auth_ws(websocket: WebSocket, session_id: str):
    """Stream the page as live video AND receive input over one WebSocket.

    Frames come from Chrome's CDP screencast (Page.startScreencast): Chrome
    pushes JPEG frames itself, only when the page changes, encoded in the render
    process — far smoother and cheaper than calling page.screenshot() in a loop
    (~30fps vs ~10fps on a CPU box). If CDP isn't available we fall back to the
    screenshot loop. Input events arrive as JSON text frames on the same socket,
    so a click/keystroke skips the HTTP round-trip too.
    """
    await websocket.accept()
    s0 = auth_browser.get(session_id)
    if not s0:
        try:
            await websocket.close()
        except Exception:
            pass
        return

    # Bounded queue with drop-oldest: always show the freshest frame so latency
    # (and therefore stutter) never builds up if the client/tunnel lags.
    frame_q: asyncio.Queue = asyncio.Queue(maxsize=2)

    def _on_frame(jpeg: bytes):
        if frame_q.full():
            try:
                frame_q.get_nowait()
            except Exception:
                pass
        try:
            frame_q.put_nowait(jpeg)
        except Exception:
            pass

    streaming = await s0.start_screencast(_on_frame)

    async def send_frames():
        if streaming:
            while True:
                jpeg = await frame_q.get()
                try:
                    await websocket.send_bytes(jpeg)
                except Exception:
                    break
        else:
            # Fallback: capture loop (works without CDP, slower).
            while True:
                s = auth_browser.get(session_id)
                if not s:
                    break
                try:
                    jpeg = await s.screenshot_jpeg()
                except Exception:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    await websocket.send_bytes(jpeg)
                except Exception:
                    break
                await asyncio.sleep(0.04)

    async def recv_events():
        while True:
            try:
                msg = await websocket.receive_text()
            except Exception:
                break
            try:
                ev = json.loads(msg)
            except Exception:
                continue
            s = auth_browser.get(session_id)
            if not s:
                break
            t = ev.get("type")
            try:
                if t == "click":
                    await s.click(float(ev["nx"]), float(ev["ny"]))
                elif t == "type":
                    await s.type_text(str(ev.get("text", "")))
                elif t == "key":
                    await s.key_press(str(ev.get("key", "")))
                elif t == "scroll":
                    await s.scroll(float(ev.get("delta_y", 0)))
            except Exception:
                pass                        # never let one bad event kill the socket

    send_task = asyncio.create_task(send_frames())
    recv_task = asyncio.create_task(recv_events())
    try:
        await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        send_task.cancel()
        recv_task.cancel()
        try:
            await s0.stop_screencast()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


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

    Phase 1: returns {done: false, message: "Waiting for login…"} until the
             store's primary auth cookie appears.
    Phase 2: returns {done: false, message: "<delivery address hint>"} while
             waiting for location cookies (merchant_id / serviceability) so
             the user knows to set their delivery address before we close.
    Done:    saves all cookies to SQLite, closes the browser, returns
             {done: true, user_id, store}.
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
    message = await s.auth_status_message()
    return {"done": False, "message": message}


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
        result = await ocr_module.extract_grocery_list(raw)
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

    # Blinkit's /v5/carts replaces the whole cart, so all Blinkit items must be
    # sent together. Batch them up front, then skip Blinkit in the per-item loop.
    blinkit_items = [it for it in items
                     if (it.get("app") or it.get("store") or "").lower() == "blinkit"
                     and it.get("prod_id")]
    if blinkit_items:
        try:
            bk = await blinkit.add_all_to_cart_api(user_id, [
                {"product_id": str(it.get("prod_id")), "count": max(1, int(it.get("qty") or 1))}
                for it in blinkit_items
            ])
        except Exception as e:
            print(f"[order][blinkit] batch raised: {e}")
            bk = {"success": False, "reason": str(e)}
        bk_items = bk.get("items") or []
        for i, it in enumerate(blinkit_items):
            qty = max(1, int(it.get("qty") or 1))
            ok = bk.get("success") and (bk_items[i].get("success") if i < len(bk_items) else True)
            if ok:
                added.append({**it, "count_added": qty})
            else:
                failed.append({**it, "reason": bk.get("reason", "blinkit cart add failed")})

    for item in items:
        prod_id = str(item.get("prod_id") or "")
        fc_id = item.get("fc_id")
        qty = max(1, int(item.get("qty") or 1))
        store = (item.get("app") or item.get("store") or "bigbasket").lower()

        if store == "blinkit":
            continue  # already handled in the batch above

        if not prod_id:
            failed.append({**item, "reason": "missing prod_id"})
            continue

        try:
            if store == "bigbasket":
                result = await bigbasket.add_to_cart_api(
                    user_id, prod_id, count=qty, fc_id=fc_id
                )
                cart_url = bigbasket.checkout_url()
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
    """Extract grocery list items from an uploaded image (vision LLM, Tesseract fallback)."""
    try:
        raw = await image.read()
        result = await ocr_module.extract_grocery_list(raw)
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
        return {"error": "No stores connected. Connect Blinkit or Zepto from the Profile page (in the sidebar)."}

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


# ── Shop tab (browse + add a single item) ─────────────────────────────────────

@app.post("/api/shop/search")
async def api_shop_search(request: Request):
    """Search ONE query across all available stores, merged + sorted cheapest
    price-per-unit (per-gram/ml/piece) first. Powers the Shop tab.

    Body: {query, user_id?}. The search account is the logged-in user, or the
    optional guest-backing account (GUEST_STORE_USER_ID) for logged-out
    visitors. Returns can_add=False for guests so the UI gates Add-to-Cart.
    """
    body = await request.json()
    session_uid = _get_session_user(request)
    is_guest = session_uid is None
    # Logged-in user searches their own sessions; a guest borrows the opt-in
    # owner account's sessions for READ-ONLY price lookups (or none if unset).
    search_uid = session_uid or _require_user_id(body.get("user_id")) or _guest_store_user()
    query = (body.get("query") or "").strip()

    if not query:
        return {"error": "missing query", "products": [], "is_guest": is_guest,
                "can_add": not is_guest}
    if not search_uid:
        return {"query": query, "products": [], "is_guest": is_guest,
                "can_add": False,
                "note": "Log in and connect a store to see live prices."}

    available = _get_available_stores(search_uid)
    if not available:
        return {"query": query, "products": [], "is_guest": is_guest,
                "can_add": not is_guest,
                "note": "No stores connected."}

    _search_fns = {"bigbasket": bigbasket.search_item_api,
                   "blinkit": blinkit.search_item_api,
                   "zepto": zepto.search_item_api}
    merged: list[dict] = []

    async def search_one(store_name: str):
        fn = _search_fns.get(store_name)
        if not fn:
            return
        try:
            products = await fn(search_uid, query)
        except Exception as e:
            print(f"[shop/search][{store_name}] error: {e}")
            return
        # Drop off-topic hits; fall back to raw list if the filter nukes all.
        relevant = ranker.filter_by_query_relevance(products, query) or products
        merged.extend(relevant)

    await asyncio.gather(*[search_one(s) for s in available])

    # Dedupe by (app, product_id); attach price-per-unit for sort + display.
    seen: set = set()
    uniq: list[dict] = []
    for p in merged:
        key = (p.get("app"), str(p.get("product_id") or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        ppu = ranker._price_per_unit(p)
        p["price_per_unit"] = None if ppu == float("inf") else round(ppu, 4)
        p["ppu_label"] = _ppu_label(p)
        uniq.append(p)

    # Cheapest price-per-unit first; products with no parseable size go last.
    uniq.sort(key=lambda p: p["price_per_unit"]
              if p.get("price_per_unit") is not None else float("inf"))

    return {"query": query, "products": uniq, "is_guest": is_guest,
            "can_add": not is_guest}


@app.post("/api/shop/add")
async def api_shop_add(request: Request):
    """Add item(s) from the Shop tab to ONE store's real cart.

    Requires a logged-in session — guests get 403 {error:"login_required"} so
    the restriction can't be bypassed by calling the endpoint directly.

    Body: {app, items:[{product_id, count, fc_id?, ...}]}. The frontend sends
    the FULL per-app shop-cart list because Blinkit's /v5/carts replaces the
    whole cart; the tested batch logic handles per-store semantics.
    """
    session_uid = _get_session_user(request)
    if not session_uid:
        return JSONResponse({"error": "login_required",
                             "message": "Log in to add items to your cart."},
                            status_code=403)
    body = await request.json()
    app_name = (body.get("app") or "").strip()
    items = body.get("items")
    if not items and body.get("product_id"):
        items = [body]
    if app_name not in _STORE_DISPLAY:
        return {"error": f"unknown store: {app_name}"}
    if not items:
        return {"error": "no items"}
    if not bigbasket.is_session_valid(session_uid) and app_name == "bigbasket":
        return {"error": f"{app_name} not connected"}

    result = await _add_items_to_store(session_uid, app_name, items)
    return {"result": result, "app": app_name}


async def _add_items_to_store(user_id: str, store_name: str,
                              items: list[dict]) -> dict:
    """Add a list of items to one store's cart. Returns {added, failed}.

    Mirrors the per-store logic in /api/cart/add-all (Zepto/Blinkit batch via
    add_all_to_cart_api; BigBasket per-item) without the progress bookkeeping,
    so the Shop tab can reuse the same tested cart paths.
    """
    app_results: dict = {"added": [], "failed": []}
    valid_items = []
    for item in items:
        pid = item.get("product_id")
        if not pid or len(str(pid)) < 4 or str(pid).startswith("gen-"):
            app_results["failed"].append(item)
            continue
        valid_items.append(item)
    if not valid_items:
        return app_results

    if store_name in ("zepto", "blinkit"):
        store_mod = zepto if store_name == "zepto" else blinkit
        try:
            batch = await store_mod.add_all_to_cart_api(user_id, valid_items)
        except Exception as e:
            print(f"[shop/add][{store_name}] batch raised: {e}")
            batch = {"success": False, "reason": str(e)}
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
            return app_results
        # Batch failed: mark all failed (never per-item retry — Blinkit clobbers).
        reason = batch.get("reason", "cart add failed")
        for item in valid_items:
            app_results["failed"].append({**item, "failed_reason": reason})
        return app_results

    # BigBasket: per-item.
    for item in valid_items:
        pid = str(item.get("product_id") or "")
        try:
            count = max(1, min(99, int(item.get("count") or 1)))
        except (TypeError, ValueError):
            count = 1
        try:
            result = await bigbasket.add_to_cart_api(
                user_id, pid, count=count, fc_id=item.get("fc_id"))
            if result.get("success"):
                app_results["added"].append(
                    {**item, "count_added": result.get("count_added", count),
                     "count_requested": count})
            else:
                app_results["failed"].append(
                    {**item, "failed_reason": result.get("reason", "")})
        except Exception as e:
            print(f"[shop/add][bigbasket] error: {e}")
            app_results["failed"].append({**item, "failed_reason": str(e)})
    return app_results


@app.post("/api/compare")
async def api_compare(request: Request):
    """Compare a full grocery list across all connected stores.

    Body: {items: [{name, qty?, count?}], user_id}

    Guests (no session) get a restricted response: each entry keeps only the
    selected cheapest product (full per-store prices + shortlist are stripped),
    and the comparison is not saved to history. Enforced server-side so it
    can't be bypassed by calling the endpoint directly.
    """
    body = await request.json()
    session_uid = _get_session_user(request)
    body_uid = _require_user_id(body.get("user_id"))
    # Guest = a logged-out web visitor: no session AND no explicit user_id.
    # Mobile / legacy callers pass user_id in the body and get the full result;
    # the guest frontend deliberately sends none, so it gets the stripped one.
    is_guest = session_uid is None and not body_uid
    # Logged-in users compare their own sessions; guests borrow the opt-in
    # owner account (GUEST_STORE_USER_ID) if configured.
    user_id = session_uid or body_uid or _guest_store_user()
    items = body.get("items", [])

    if not user_id:
        return {"error": "missing user_id"}
    if not items:
        return {"error": "missing items"}

    available = _get_available_stores(user_id)
    if not available:
        return {"error": "No stores connected. Connect Blinkit or Zepto from the Profile page (in the sidebar)."}

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

        # Savings = what you'd pay buying every item at its most expensive store
        # minus the actual cross-store basket total (pre-delivery, best effort).
        # Each item's price is multiplied by qty_count so weight multipliers
        # (e.g. 4× 250g for 1kg) are reflected consistently with the basket.
        worst_basket = 0.0
        for entry in comparison:
            if not entry.get("cheapest_app"):
                continue
            n = max(1, int(entry.get("qty_count") or 1))
            max_price = 0.0
            for _store, prods in (entry.get("prices") or {}).items():
                if prods:
                    p = prods[0]
                    unit = float(p.get("sale_price") or p.get("price") or 0)
                    max_price = max(max_price, unit * n)
            worst_basket += max_price
        savings = max(0.0, worst_basket - grand_total)

        print(f"[compare] DONE. Found {items_found}/{len(items)}, "
              f"grand total ₹{grand_total:.0f}, savings ₹{savings:.0f}\n")

        # Persist to history — logged-in users only (guests have no account).
        if not is_guest:
            stores_used = list(carts.keys())
            query_text = "\n".join(
                f"{it.get('name','')} {it.get('qty','')}".strip() for it in items
            )
            try:
                user_store.save_comparison(
                    user_id=user_id,
                    query_text=query_text,
                    items=[{"name": it.get("name",""), "qty": it.get("qty","")} for it in items],
                    grand_total=grand_total,
                    savings=savings,
                    stores=stores_used,
                )
            except Exception as e:
                print(f"[compare] history save failed: {e}")

        if is_guest:
            # Guest restriction: expose ONLY the cheapest pick per item — strip
            # the full per-store price matrix and the swap shortlist so the full
            # comparison stays a logged-in feature. Enforced here, not just in UI.
            return {
                "comparison": [_guest_strip_entry(e) for e in comparison],
                "grand_total": grand_total,
                "skipped_apps": skipped,
                "is_guest": True,
                "summary": {
                    "total_items": len(items),
                    "items_found": items_found,
                    "apps_used": len(carts),
                },
            }

        return {
            "comparison": comparison,
            "carts": carts,
            "grand_total": grand_total,
            "savings": savings,
            "skipped_apps": skipped,
            "is_guest": False,
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
        return {"error": "No stores connected. Connect Blinkit or Zepto from the Profile page (in the sidebar)."}

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

        # Zepto and Blinkit both expose a batched add_all_to_cart_api. Blinkit's
        # /v5/carts is a SYNC endpoint (must send all items at once), so a
        # per-item loop would overwrite each previous add — batching is required,
        # not just an optimisation.
        if store_name in ("zepto", "blinkit") and valid_items:
            _cart_progress[user_id]["current"] = f"{store_name}: (batch add)"
            store_mod = zepto if store_name == "zepto" else blinkit
            try:
                batch = await store_mod.add_all_to_cart_api(user_id, valid_items)
            except Exception as e:
                print(f"[cart][{store_name}] batch raised: {e}")
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

            # Blinkit's /v5/carts is all-or-nothing AND replaces the cart, so on
            # batch failure do NOT retry per item (that would clobber). Mark all
            # failed and stop here. (Zepto may fall through to its per-item path.)
            if store_name == "blinkit":
                reason = batch.get("reason", "cart add failed")
                for item in valid_items:
                    app_results["failed"].append({**item, "failed_reason": reason})
                    done_count += 1
                    _cart_progress[user_id]["done"] = done_count
                return

        # Per-item path: BB, or fallback for Zepto
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


# ── Log viewer ───────────────────────────────────────────────────────────────
# Exposes the tail of /app/data/server.log over HTTP so autonomous loop
# sessions can check real-time output without SSH access.
# Protected by LOG_API_KEY env var (set in .env on the host).

_LOG_FILE = BASE_DIR.parent / "data" / "server.log"
_MAX_LOG_BYTES = 10 * 1024 * 1024  # only read the last 10 MB to stay fast


@app.get("/api/logs")
async def api_logs(key: str = "", n: int = 300):
    """Return the last N lines of the server log.

    Protected: requires ?key=<LOG_API_KEY>.  Returns 403 if the key is wrong
    or LOG_API_KEY is not set.  Returns 404 if the log file doesn't exist yet
    (happens on the very first startup before any output is written).
    """
    api_key = os.getenv("LOG_API_KEY", "")
    if not api_key or key != api_key:
        return Response(status_code=403)
    if not _LOG_FILE.exists():
        return Response(
            status_code=404,
            content="Log file not found — server may not have restarted since this endpoint was added.",
        )
    try:
        # Read only the tail of large files to stay fast
        size = _LOG_FILE.stat().st_size
        if size > _MAX_LOG_BYTES:
            with open(_LOG_FILE, "rb") as fh:
                fh.seek(size - _MAX_LOG_BYTES)
                raw = fh.read().decode("utf-8", errors="replace")
        else:
            raw = _LOG_FILE.read_text(encoding="utf-8", errors="replace")
        lines = raw.splitlines()
        tail = lines[-n:]
        return {
            "total_lines": len(lines),
            "returned": len(tail),
            "log_size_kb": round(size / 1024),
            "lines": tail,
        }
    except Exception as e:
        return {"error": str(e), "lines": []}


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
