# scan2order Merge Plan — v3 (Final pre-implementation)

## 0. Summary of All Decisions

| Question | Decision |
|---|---|
| Playwright | Remove entirely |
| Deployment | Render (cloud), Oracle later |
| Users | Multi-user, 100s, Redis per user_id |
| Blinkit | Reverse-engineer + implement httpx API in Phase 1 |
| Ship only when complete | Yes — no stub/coming-soon UI |
| Zepto localStorage | Cookies-only first; add localStorage extraction only if tokens aren't in cookies |
| Web login | Explained below — it's a browser security boundary, not a design choice |
| OCR | Tesseract |
| Ranking | scan2order2 algorithmic (primary) + Groq (opt-in fallback) |

---

## 1. Why Web Login Is Architecturally Different From Mobile

**Where the cookie-paste idea came from:** Browsers enforce a security rule called the Same-Origin Policy. JavaScript on `scan2order.onrender.com` cannot read cookies that belong to `bigbasket.com`, `blinkit.com`, or `zeptonow.com` — period. This is a browser security feature, not a design choice, and cannot be bypassed in a normal web page.

**How scan2order2 bypassed it:** It ran Playwright — a real Chromium browser — as a subprocess on the user's own PC. Playwright isn't a web page; it's a Python process controlling a browser directly. Python can read every cookie in that browser. That's why the "Login" button in scan2order2's web UI works: the Python server opens a browser window on YOUR PC, you log in there, and the server reads the cookies directly. This only works when server and user are on the same machine.

**How scan2order1 bypasses it:** React Native's `WebView` is a native OS component, not a sandboxed browser. The native app can read cookies from any WebView it controls. That's why `CookieManager.getAll()` works in the mobile app. This only works in a native app (iOS/Android), not in a browser.

**What this means for a cloud web app:**
- Mobile (iOS/Android): WebView → automatic cookie extraction → works perfectly
- Browser (web app): can't read BigBasket cookies → needs a different solution

**Proposed solution: Mobile-first linking (the WhatsApp Web model)**

1. User installs the mobile app. Connects BigBasket, Blinkit, Zepto via WebView login. Cookies stored in Redis under their `user_id`.
2. The mobile app shows a "Link web" screen with their `user_id` as a short code or QR code.
3. User opens the web app on their computer, enters the code (or scans QR).
4. Web app now has the same `user_id` → same Redis session → all connected stores work.
5. Web app can now compare, scan, add to cart. No login needed on the web side.

This is exactly how WhatsApp Web, Spotify Connect, and Telegram Web work. The phone is the authenticator; the web is the display. It requires zero technical knowledge — "scan this QR code with your phone" is something any user can do.

For pure web-only users (no phone), this can be extended later with a browser extension that handles cookie extraction. Not in scope for Phase 1.

**Remaining question about web login (Q10 resolved):** The web login for the initial version uses the mobile-linking flow described above. The web UI (`templates/index.html`) will show a "Link with mobile app" option when no `user_id` is stored in the browser's `localStorage`, and will allow entering a code from the mobile app.

---

## 2. Final Directory Structure

```
scan2order/
├── backend/
│   ├── server.py                    # Rebuilt: no Playwright, multi-user, all routes
│   ├── stores/
│   │   ├── __init__.py
│   │   ├── bigbasket.py             # httpx search + cart — cookies from Redis
│   │   ├── blinkit.py              # httpx search + cart — to be implemented via API reverse-eng
│   │   ├── zepto.py                # httpx search + cart + token refresh — session from Redis
│   │   └── zepto_sign.py           # VERBATIM COPY from scan2order2 (pure stdlib SHA-256)
│   ├── ranker.py                    # All compare/sort/filter logic from scan2order2 server.py
│   │                                 # + Groq rank_products from scan2order1 as optional fallback
│   ├── ocr.py                       # Tesseract OCR from scan2order2 server.py
│   ├── storage/
│   │   ├── __init__.py
│   │   └── user_store.py            # Extended from scan2order1: cookies + local_storage per store
│   ├── templates/
│   │   └── index.html               # scan2order2 web UI adapted: Playwright login → QR/code linking
│   ├── requirements.txt
│   └── .env.example
└── mobile/
    ├── config.js                    # NEW: BACKEND URL, sourced from env
    ├── App.js                       # Updated navigation
    ├── screens/
    │   ├── HomeScreen.js            # Updated: multi-store status, config import
    │   ├── ResultsScreen.js         # Updated: multi-store results, new compare API shape
    │   ├── CartScreen.js            # Unchanged
    │   ├── ConnectScreen.js         # Updated: store chooser + "Link web" QR display
    │   ├── ConnectBigBasketScreen.js # WebView login for BigBasket
    │   ├── ConnectBlinkitScreen.js  # WebView login for Blinkit
    │   └── ConnectZeptoScreen.js    # WebView login for Zepto (cookies-first)
    ├── package.json
    ├── app.json
    └── index.js
```

---

## 3. What Is Preserved From scan2order2

### `server.py` → split into `ranker.py` + `ocr.py` + `server.py`

**→ `backend/ranker.py`** (all comparison logic, preserved exactly):
- `_parse_qty(s)` — "500g" → (500.0, "mass")
- `_product_price(p)` — `sale_price or price`
- `_price_per_unit(p)` — ₹/gram or ₹/ml for smart sorting
- `_qty_distance(query_qty, product)` — how far a product's size is from what user asked for
- `_is_reasonable_size(product, query_qty)` — drop bulk packs >2kg/2L/12pc
- `filter_by_query_relevance(products, query)` — from `base.py`, keeps products matching all non-generic query words
- `_GENERIC_FOOD_WORDS` frozenset
- `_compare_one_item(item, apps, user_id)` — parallel search across connected stores, picks best by qty+ppu+price
- `_selected_product(entry)` — resolve selected_pid in a comparison entry
- `_build_carts_from_comparison(comparison)` — group into {app: {items, total}}
- `rank_with_groq(original_item, products)` — from scan2order1's `ranking_agent.py`, called only if `GROQ_API_KEY` set

**→ `backend/ocr.py`** (all OCR logic, preserved exactly):
- `OCR_AVAILABLE`, `OCR_ERROR` initialization with Windows tesseract path detection
- `OCR_SECTION_HEADERS` set
- `_preprocess_image(raw_bytes)` — grayscale, autocontrast, 2× upscale, EXIF rotate
- `_clean_ocr_line(line)` — strip bullets, numbering, stray OCR artefacts
- `_is_header_line(line)` — skip "Vegetables:", "Shopping List", etc.

**→ `backend/server.py`** (route structure preserved, modified for multi-user + no Playwright):
- `APP_VERSION` constant
- `_logged_in_apps()` → `_connected_stores(user_id)` — checks Redis instead of session files
- Progress dicts keyed by user_id: `_compare_progress[user_id]`, `_cart_progress[user_id]`
- `lifespan` — startup/shutdown (no browser_manager; just Redis health check)
- All `/api/*` routes kept, `user_id` added to body/query for compare+cart
- New `/api/auth/*` routes for mobile login
- Removed: all Playwright routes (`/api/login/*`, `/api/address/*`, `/api/checkout/*`, `/api/cleanup`, `/api/debug/*` with Playwright artifacts)

### `automators/bigbasket.py` → `backend/stores/bigbasket.py`

Preserved exactly (the httpx logic):
- `_MOBILE_UA` string
- `_api_headers(csurftoken)` — all PWA headers (x-caller, x-channel, osmos-enabled, etc.)
- `_ensure_csurftoken(cookies)` — GET bigbasket.com/ warmup, returns updated cookies + token
- `search(query, cookies)` — was `search_item_api()`, cookie source changed from Playwright to param
- `add_to_cart(product_id, count, fc_id, cookies, search_query)` — was `add_to_cart_api()`, same change

Removed: `login()`, `search_item()` (Playwright), `add_to_cart()` (Playwright), `go_to_checkout()`, `_get_session_cookies()` (replaced by caller passing cookies directly), `start_interstitial_watcher()`

### `automators/zepto.py` → `backend/stores/zepto.py`

Preserved exactly (the httpx logic):
- `_API_BASE`, `_MOBILE_UA`
- `_COMPATIBLE_COMPONENTS` string (verbatim — large constant, don't touch)
- `_zepto_api_headers(sess, request_id, sig, tz_hash)` — all Zepto headers
- `_parse_set_cookies(resp)` — parse Set-Cookie response headers
- `build_session(cookies, local_storage)` — was `_get_zepto_session()`, takes plain dicts instead of Playwright storage_state
- `_zepto_api_post(path, body, sess)` — sign + POST + 401 token refresh + retry
- `search(query, sess)` — was `search_item_api()`, same structure
- `add_to_cart(items, sess)` — was `add_all_to_cart_api()`, same structure (product-detail enrich + bulk-widget-data)
- `_diag_token_status(sess)` — debug helper kept

Removed: `login()`, `search_item()` (Playwright), `add_to_cart()` (Playwright), `go_to_checkout()`, `_get_zepto_session()` (replaced by `build_session()`), `_persist_cookies_dict()` (replaced by `user_store.update_store_cookies()`)

### `automators/zepto_sign.py` → `backend/stores/zepto_sign.py`
**Verbatim copy. Zero changes. Pure stdlib.**

### `automators/blinkit.py` → `backend/stores/blinkit.py`
**Fully re-implemented** as httpx-only. The Playwright code from scan2order2 provides no reusable httpx logic for Blinkit — it must be discovered via API inspection.

**Plan for Blinkit API implementation:**
- Blinkit's web app is Next.js, served from `blinkit.com`
- Their internal search API endpoint needs to be captured from browser DevTools (Network tab → search for a product → identify the fetch/XHR request)
- Auth token: `gr_1_accessToken` cookie (already identified in scan2order2's blinkit.py)
- Once endpoint is known, implementation follows the same httpx pattern as BigBasket
- The implementation will happen during Phase 1 with the help of a live captured request

### `base.py` `filter_by_query_relevance`
Moved to `ranker.py`. All other `base.py` methods are Playwright-specific and dropped.

### Dropped entirely (Playwright-only):
- `browser_manager.py`
- `automators/base.py` (except `filter_by_query_relevance`)
- Playwright login detection, interstitial handling, page management
- Debug HTML/PNG artifact saving
- `go_to_checkout()` (no headless browser to navigate in)

---

## 4. Multi-User Redis Design

### Data structure per user
```json
{
  "bigbasket": {
    "connected": true,
    "cookies": {
      "BBAUTHTOKEN": "eyJ...",
      "csurftoken": "abc123",
      "_bb_mid": "...",
      "...": "..."
    },
    "local_storage": {}
  },
  "blinkit": {
    "connected": true,
    "cookies": {"gr_1_accessToken": "...", "...": "..."},
    "local_storage": {}
  },
  "zepto": {
    "connected": true,
    "cookies": {
      "XSRF-TOKEN": "...",
      "accessToken": "...",
      "refreshToken": "...",
      "device_id": "...",
      "session_id": "...",
      "serviceability": "{\"primaryStore\":{\"storeId\":\"abc\",\"etaInMinutes\":10},...}",
      "...": "..."
    },
    "local_storage": {}
  }
}
```

### `user_store.py` API
```python
connect_store(user_id, store, cookies: dict, local_storage: dict = {})
get_store_cookies(user_id, store) -> dict
get_store_local_storage(user_id, store) -> dict
get_store_session(user_id, store) -> dict  # {cookies, local_storage, connected}
update_store_cookies(user_id, store, new_cookies: dict)   # merge, not overwrite
get_connected_stores(user_id) -> list[str]
is_store_connected(user_id, store) -> bool
get_user_stores(user_id) -> dict
```

Redis key: `user:{user_id}`, TTL: 30 days (refreshed on every write).

---

## 5. Backend API Endpoints

### Auth
```
POST /api/auth/connect
    Body: {user_id, store, cookies: {}, local_storage: {}}
    Response: {success, store}

GET  /api/auth/status/{user_id}
    Response: {user_id, stores: {bigbasket: {connected}, blinkit: {connected}, zepto: {connected}}}
```

Backward-compat aliases (for old mobile app during transition):
```
POST /connect-account     → delegates to /api/auth/connect
GET  /account-status/{id} → delegates to /api/auth/status/{id}
```

### OCR
```
POST /api/ocr
    Body: multipart/form-data, field: image
    Response: {raw_text, items: [string]}   ← scan2order2 shape

POST /scan
    Body: multipart/form-data, field: file
    Response: {success, items: [{name, quantity: null}], search_items: [string], raw_text, confidence: "medium"}
    ← scan2order1 mobile shape
```

### Search & Compare
```
POST /api/search
    Body: {user_id, query, apps?: [store_names]}
    Response: {query, results: {bigbasket: {products, cheapest}, zepto: {…}}, cheapest, skipped_apps}

POST /api/compare
    Body: {user_id, items: [{name, qty}]}
    Response: {comparison: [...], carts: {…}, grand_total, skipped_apps, summary}

GET  /api/compare/progress?user_id=xxx
    Response: {running, total, done, current}

POST /api/compare/item
    Body: {user_id, item: {name, qty}}
    Response: {entry: {...}}
```

Mobile compat:
```
POST /search
    Body: {user_id, items: [string], original_items: [{name, qty}], city}
    Response: {success, products: [{item, name, price, weight, url, store, prod_id, fc_id, recommended?, reason?}]}
    ← groups by item, translates product schema, optionally runs Groq ranking
```

### Cart
```
POST /api/cart/add-all
    Body: {user_id, carts: {store: {items: [{product_id, fc_id?, count, search_query, name, …}]}}}
    Response: {results: {store: {added: [], failed: []}}}

GET  /api/cart/progress?user_id=xxx
    Response: {running, total, done, current}
```

Mobile compat:
```
POST /order
    Body: {user_id, city, items: [{prod_id, fc_id, qty}]}
    Response: {success, results}
```

### Web app / misc
```
GET  /                    → web UI (HTML)
GET  /api/apps            → {store: {display_name, base_url}}
GET  /api/version         → {version}
GET  /health              → {status: "ok"}
GET  /favicon.ico         → SVG
```

### Removed (Playwright-only)
```
POST /api/login/{app}
GET  /api/login/{app}/poll
POST /api/login/{app}/save
GET  /api/login/{app}/status
POST /api/address/{app}
POST /api/checkout/{app}
POST /api/cleanup
GET  /api/debug/*
GET  /api/_test/search-api/*
```

---

## 6. Store Module Interfaces

### `stores/bigbasket.py`
```python
_MOBILE_UA: str
_API_HEADERS_TEMPLATE: dict  # without csurftoken + x-tracker (added per-call)

async def ensure_csurftoken(cookies: dict) -> tuple[dict, str]:
    """GET bigbasket.com/ to get/refresh csurftoken.
    Returns (updated_cookies, token). Empty token = failure."""

async def search(query: str, cookies: dict) -> list[dict]:
    """httpx GET /listing-svc/v2/products. Returns ≤8 in-stock products.
    Returns [] if BBAUTHTOKEN missing or request fails."""

async def add_to_cart(product_id: str, count: int, fc_id: int,
                      cookies: dict, search_query: str = "") -> dict:
    """httpx POST /mapi/v4.2.0/c-incr-i/.
    Returns {success, count_added} or {success: False, reason}."""
```

### `stores/zepto.py`
```python
_API_BASE: str
_MOBILE_UA: str
_COMPATIBLE_COMPONENTS: str  # verbatim from scan2order2

def build_session(cookies: dict, local_storage: dict) -> dict:
    """Normalize raw cookies + localStorage into the session dict.
    Same logic as _get_zepto_session() but from plain dicts."""

async def search(query: str, sess: dict) -> list[dict]:
    """httpx POST /user-search-service/api/v3/search with SHA-256 signing.
    Returns ≤8 products. Returns [] on missing tokens or failure."""

async def add_to_cart(items: list[dict], sess: dict) -> dict:
    """Product-detail enrich + POST /cfs/api/v1/bulk-widget-data.
    Returns {success, items: [{success, count_added}]}."""
```

### `stores/blinkit.py`
```python
async def search(query: str, cookies: dict) -> list[dict]:
    """httpx call to Blinkit's internal search API.
    Endpoint to be determined via DevTools capture before implementation."""

async def add_to_cart(product_id: str, count: int, cookies: dict) -> dict:
    """httpx call to Blinkit's cart API. Same discovery approach."""
```

### `stores/zepto_sign.py` — verbatim copy from scan2order2

---

## 7. Mobile App Changes

### `mobile/config.js` (new file)
```js
export const BACKEND = process.env.EXPO_PUBLIC_BACKEND_URL
  || 'https://scan2order-backend.onrender.com';
```

### `ConnectScreen.js` — store chooser + web link code
Shows three rows (BigBasket, Blinkit, Zepto) each with green/red status dot and "Connect" button. Below the store list: a "Link web app" section showing the user's short link code (first 8 chars of UUID) and a QR code. User enters this code in the web app to link their session.

### `ConnectBigBasketScreen.js`
- WebView on `https://www.bigbasket.com`
- `onLoadEnd`: `CookieManager.getAll(true)` — when `BBAUTHTOKEN` found, POST to `/api/auth/connect` with `{store: "bigbasket", cookies}`

### `ConnectBlinkitScreen.js`
- WebView on `https://blinkit.com`
- `onLoadEnd`: `CookieManager.getAll(true)` — when `gr_1_accessToken` found, POST to `/api/auth/connect` with `{store: "blinkit", cookies}`

### `ConnectZeptoScreen.js`
- WebView on `https://www.zeptonow.com`
- First attempt: cookies-only. When `XSRF-TOKEN` cookie found → POST `/api/auth/connect` with `{store: "zepto", cookies}`
- If Zepto API calls later fail with "missing tokens" → add localStorage extraction via injected JS:
  ```js
  const INJECT = `(function(){
    try {
      const ls = {};
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        ls[k] = localStorage.getItem(k);
      }
      window.ReactNativeWebView.postMessage(JSON.stringify({type:'ls',data:ls}));
    } catch(e){}
  })(); true;`;
  ```

### `HomeScreen.js`
- Import `BACKEND` from `../config`
- On mount: call `/api/auth/status/{user_id}` → show per-store dots
- Scan → POST `/scan` (unchanged request shape)
- After scan → navigate to ResultsScreen

### `ResultsScreen.js`
- Import `BACKEND` from `../config`
- Instead of `/search`, call `/api/compare` with `{user_id, items: [{name, qty}]}`
- Display adapts for multi-store: show which app each product comes from
- "Add to cart" calls `/api/cart/add-all`

### `App.js` — adds new screen routes (ConnectBigBasket, ConnectBlinkit, ConnectZepto)

---

## 8. Web UI Login Flow (templates/index.html)

On page load:
1. Check `localStorage.getItem('s2o_user_id')` — if present, fetch `/api/auth/status/{user_id}` to show store dots
2. If no `user_id`: show "Get started" section

"Get started" section (replaces scan2order2's "Login" buttons):
- "Mobile app user? Enter your link code:" → text input for 8-char code → fetches `/api/auth/status/{code}` to verify → stores as user_id in localStorage
- "New here? Download the mobile app to connect your grocery accounts" → links to app stores
- Store dots show green once connected stores load

The compare/cart/OCR flows are identical to scan2order2 once the user_id is set. No login buttons, no polling — just the code entry once.

---

## 9. Blinkit API Implementation Plan

Since scan2order2 has no httpx path for Blinkit, implementation requires API discovery:

**Step 1 — Capture a live Blinkit search request:**
- Open `blinkit.com` in Chrome, log in
- Open DevTools → Network tab → filter by Fetch/XHR
- Search for "tomato"
- Find the API request (likely to `api.blinkit.com` or an internal Next.js endpoint)
- Note: URL, method, headers (especially auth headers), request body, response shape

**Step 2 — Implement in `stores/blinkit.py`:**
- Extract the auth mechanism from `gr_1_accessToken` cookie
- Map the response JSON to the standard product dict: `{name, price, sale_price, unit, image_url, product_id}`
- Same pattern as BigBasket's `search_item_api`

**Step 3 — Cart API:**
- Capture a "Add to cart" request from DevTools
- Implement `add_to_cart()` using same auth cookie

This requires a brief offline investigation step at the start of Phase 1. The user (or a team member with a live Blinkit session) needs to capture one search + one cart request and share the details.

---

## 10. Ranking Logic

`backend/ranker.py` combines both ranking systems:

```python
# Primary (always runs, no API cost):
filter_by_query_relevance(products, query) -> list
# → from automators/base.py, all non-generic words must appear in product name

def compare_and_pick(products, query, qty_str) -> (best_product, best_price):
# → _compare_one_item's inner sort logic: qty_distance → price_per_unit → price

# Secondary (runs only on /search mobile endpoint if GROQ_API_KEY set):
async rank_with_groq(original_item, products) -> list
# → from scan2order1/agents/ranking_agent.py; adds recommended + reason fields
```

`/api/compare` (bulk list) → algorithmic only (speed matters: 5-20 items × 3 stores).
`/search` (single item via mobile) → Groq if key set, else algorithmic.

---

## 11. Requirements

```
fastapi==0.115.0
uvicorn==0.30.0
python-multipart==0.0.9
python-dotenv==1.0.0
jinja2==3.1.4
httpx==0.27.0
requests==2.32.3
upstash-redis==1.0.0
pytesseract==0.3.13
Pillow==10.4.0
groq==0.13.0
```

System deps on Render: `tesseract-ocr` via build command.
Dropped vs scan2order1: `anthropic`, `openai`, `huggingface_hub`, `playwright`

---

## 12. Environment Variables

```bash
# Required
UPSTASH_REDIS_REST_URL=https://...
UPSTASH_REDIS_REST_TOKEN=...

# Optional: Groq ranking for /search endpoint
GROQ_API_KEY=sk-...

# Optional: fallback BigBasket cookie for testing without mobile login
BB_COOKIE=...

# Optional: override version string shown in UI
APP_VERSION=1.0.0
```

---

## 13. Deployment

**Backend (Render Web Service):**
- Build: `pip install -r requirements.txt && apt-get install -y tesseract-ocr`
- Start: `uvicorn server:app --host 0.0.0.0 --port $PORT`
- Set env vars in Render dashboard

**Mobile:**
- Expo build for iOS/Android
- `EXPO_PUBLIC_BACKEND_URL=https://your-render-url.onrender.com` in build config

**Web app:**
- Static Expo web build (optional) or same backend serving `templates/index.html`

---

## 14. Phase Sequence

**Phase 1 — Backend** (can begin after plan approval):
1. `mkdir` backend structure
2. `ocr.py` — from scan2order2 server.py
3. `storage/user_store.py` — extended from scan2order1
4. `stores/zepto_sign.py` — verbatim copy
5. `stores/bigbasket.py` — httpx logic from scan2order2, cookie param instead of Playwright
6. `stores/zepto.py` — httpx logic from scan2order2, session from Redis
7. `stores/blinkit.py` — implement via captured API (requires DevTools capture first)
8. `ranker.py` — all comparison logic from scan2order2 + Groq fallback
9. `server.py` — rebuilt: CORS, all routes, multi-user, no Playwright
10. `templates/index.html` — adapt for web linking flow
11. `requirements.txt`, `.env.example`

**Phase 2 — Mobile:**
1. `mobile/config.js`
2. `ConnectBigBasketScreen.js`, `ConnectBlinkitScreen.js`, `ConnectZeptoScreen.js`
3. Update `ConnectScreen.js`, `HomeScreen.js`, `ResultsScreen.js`, `App.js`
4. End-to-end test: mobile login → compare → cart

**Phase 3 — Polish:**
1. `README.md`
2. Render build config verification

---

## 15. One Remaining Question Before Phase 1

**Q11 — Blinkit API capture:**

Implementing Blinkit's httpx search requires a captured network request. Before I start Phase 1 code, do you want to:

- **A**: Provide a captured Blinkit search request (open blinkit.com in Chrome → DevTools → Network → search for anything → share the request URL + headers + response snippet) so I can implement it immediately
- **B**: Skip Blinkit for Phase 1 and add it in Phase 2 once captured — ship with BigBasket + Zepto working first
- **C**: I look up Blinkit's public API documentation (if any exists) and implement based on that

**This is the only open question.** All other decisions are locked in.