# scan2order

Grocery price comparison across BigBasket, Blinkit, and Zepto.
Scan a handwritten list → find the cheapest basket across all three stores → add everything to cart in one tap.

---

## How it works

```
Mobile app (iOS/Android/Web)
  │
  ├─ Login flow: opens BigBasket/Blinkit/Zepto in a WebView
  │              extracts auth cookies → sends to backend → stored in Redis
  │
  ├─ Scan: photo of grocery list → OCR → list of items
  │
  └─ Compare: backend searches all 3 stores in parallel
              returns prices → user sees cheapest highlighted
              taps "Add to cart" → backend calls each store's API

Web UI (browser)
  │
  └─ Enter 8-char link code from mobile app → shares the same Redis session
     Same compare + cart-add workflow, no re-login needed
```

No Playwright. No local browser. Every store call is a direct httpx API request.

---

## Project structure

```
scan2order3/
├── .env.example               ← copy to .env and fill in
├── .gitignore
├── Procfile                   ← Render start command
├── requirements.txt
│
└── backend/
    ├── server.py              ← FastAPI entry point, all routes
    ├── ocr.py                 ← Tesseract OCR (image → grocery list)
    ├── ranker.py              ← price comparison + relevance filtering + Groq fallback
    │
    ├── storage/
    │   └── user_store.py      ← Upstash Redis: per-user cookies, link codes
    │
    ├── stores/
    │   ├── bigbasket.py       ← httpx: listing-svc search, cart-incr-i add
    │   ├── blinkit.py         ← httpx: __NEXT_DATA__ SSR search, v2 cart add
    │   ├── zepto.py           ← httpx: BFF gateway search, bulk-widget-data cart
    │   └── zepto_sign.py      ← SHA-256 signing for Zepto API (no deps)
    │
    └── templates/
        └── index.html         ← web UI (dark theme, link-code auth, compare + cart)
```

> **Phase 2 not yet done:** `mobile/` doesn't exist yet. The existing
> scan2order1 mobile app still works — just point `BACKEND` at this server.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.11+ | 3.12 recommended |
| Tesseract OCR | For the `/scan` and `/api/ocr` endpoints |
| Upstash Redis account | Free tier is enough — [upstash.com](https://upstash.com) |
| Groq API key | **Optional** — only needed for LLM-based ranking fallback |

### Install Tesseract

**Windows:** Download from https://github.com/UB-Mannheim/tesseract/wiki  
Install to the default path (`C:\Program Files\Tesseract-OCR\`). The server auto-detects it.

**macOS:** `brew install tesseract`

**Ubuntu/Render:** `apt-get install -y tesseract-ocr`
(Add a `render.yaml` or use the Render dashboard build command.)

---

## Local setup (no Render needed)

### 1. Clone and install

```bash
git clone <your-repo-url>
cd scan2order3
pip install -r requirements.txt
```

### 2. Create your `.env`

```bash
cp .env.example .env
```

Edit `.env`:
```
UPSTASH_REDIS_REST_URL=https://your-instance.upstash.io
UPSTASH_REDIS_REST_TOKEN=your_token_here
```

Get these from [console.upstash.com](https://console.upstash.com) →
create a Redis database → copy the REST URL and token.

### 3. Run the backend

```bash
cd backend
uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

The server starts at:
- **Web UI:** http://localhost:8000
- **API docs:** http://localhost:8000/docs (interactive Swagger — test any endpoint here)

---

## Testing without a mobile app

You can test everything via the browser and `curl`. No mobile app or Render needed.

### Step 1 — Connect a store (simulate what the mobile app does)

The mobile app extracts cookies from a WebView login and sends them here.
You can do the same manually:

1. Open BigBasket in your browser, log in
2. Open DevTools → Application → Cookies → copy all cookies as a JSON object
3. POST them:

```bash
curl -s -X POST http://localhost:8000/api/auth/connect \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test-user-1",
    "store": "bigbasket",
    "cookies": {
      "BBAUTHTOKEN": "your_token_here",
      "csurftoken": "...",
      "_bb_mid": "..."
    }
  }'
```

Key cookies needed per store:

| Store | Required cookie(s) |
|-------|--------------------|
| BigBasket | `BBAUTHTOKEN`, `csurftoken` |
| Blinkit | `gr_1_accessToken` |
| Zepto | `XSRF-TOKEN`, `accessToken`, `deviceId`, `sessionId`, `serviceability` |

### Step 2 — Check connection

```bash
curl http://localhost:8000/api/auth/status/test-user-1
```

### Step 3 — Search

```bash
curl -s -X POST http://localhost:8000/api/compare \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test-user-1",
    "items": [
      {"name": "Tomatoes", "qty": "500g"},
      {"name": "Amul Butter", "qty": "100g"},
      {"name": "Onions", "qty": "1kg"}
    ]
  }' | python -m json.tool
```

### Step 4 — Use the web UI

Open http://localhost:8000 in a browser.

Instead of the link-code flow (which needs the mobile app), you can manually
set `user_id` in browser devtools:

```javascript
localStorage.setItem('user_id', 'test-user-1')
location.reload()
```

Then paste your grocery list and click **Compare prices**.

### Step 5 — Test OCR

```bash
curl -s -X POST http://localhost:8000/api/ocr \
  -F "image=@/path/to/your/grocery-list-photo.jpg" \
  | python -m json.tool
```

### Step 6 — Single item search

```bash
curl -s "http://localhost:8000/api/search" \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test-user-1", "query": "amul butter 100g"}'
```

---

## Testing with the scan2order1 mobile app

The existing mobile app works with this backend as-is.
Only change needed: update `BACKEND` in `HomeScreen.js`:

```javascript
// Find your PC's local IP: run `ipconfig` (Windows) or `ifconfig` (Mac)
const BACKEND = 'http://192.168.1.XXX:8000';   // your local IP
```

Your phone and PC must be on the same Wi-Fi network.

The mobile app will:
- Connect BigBasket via its WebView → cookies sent to your local backend
- `/scan` → OCR on the backend
- `/search` → compare prices
- `/order` → add to BigBasket cart

---

## API reference (quick)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/api/version` | Version string |
| `GET` | `/api/docs` | Interactive Swagger UI |
| `POST` | `/api/auth/connect` | Store cookies for a user |
| `GET` | `/api/auth/status/{user_id}` | Which stores are connected |
| `POST` | `/api/auth/link` | Generate 8-char link code (mobile → web) |
| `GET` | `/api/auth/link/{code}` | Resolve link code → user_id |
| `POST` | `/api/ocr` | OCR image → grocery items |
| `POST` | `/api/compare` | Compare grocery list across all stores |
| `GET` | `/api/compare/progress?user_id=` | Progress while compare runs |
| `POST` | `/api/compare/item` | Re-compare a single item |
| `POST` | `/api/cart/add-all` | Add items to carts |
| `GET` | `/api/cart/progress?user_id=` | Progress while cart-add runs |
| **Mobile compat** | | |
| `POST` | `/connect-account` | Same as `/api/auth/connect` |
| `GET` | `/account-status/{user_id}` | Mobile-shaped status response |
| `POST` | `/scan` | OCR (multipart) → `{success, items}` |
| `POST` | `/search` | Compare → flat product list for mobile UI |
| `POST` | `/order` | Add to cart → `{success, cart_url}` |

---

## Deploying to Render

1. Push this repo to GitHub
2. Create a new **Web Service** on [render.com](https://render.com)
3. Set **Build Command:** `pip install -r requirements.txt`
4. Set **Start Command:** `sh -c 'cd backend && uvicorn server:app --host 0.0.0.0 --port $PORT'`
   (or Render picks it up from the `Procfile` automatically)
5. Add environment variables in the Render dashboard:
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`
   - `GROQ_API_KEY` (optional)
6. Add a build command for Tesseract if you need OCR:
   ```
   apt-get install -y tesseract-ocr && pip install -r requirements.txt
   ```
   (Use a `render.yaml` or set this under **Build & Deploy → Build Command**)

Then update the mobile app's `BACKEND` constant to your Render URL.

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `UPSTASH_REDIS_REST_URL` | Yes | From Upstash console |
| `UPSTASH_REDIS_REST_TOKEN` | Yes | From Upstash console |
| `GROQ_API_KEY` | No | Enables LLM ranking fallback |
| `PORT` | No | Defaults to 8000 |

---

## Known limitations / next steps

- **Blinkit cart API** — the add-to-cart endpoint format was reverse-engineered from known patterns and needs a live Blinkit session to validate. If it fails, check the response code in the logs and adjust `blinkit.py:add_to_cart_api`.
- **Phase 2 (mobile)** — `scan2order3/mobile/` doesn't exist yet. Planned: copy scan2order1/mobile/ and add ConnectBlinkitScreen + ConnectZeptoScreen.
- **Synchronous Redis calls** — `user_store.py` uses the sync `upstash_redis` client inside async handlers. Fine at current scale; switch to `upstash_redis.asyncio` if Redis latency becomes a bottleneck at high concurrency.
