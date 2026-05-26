# scan2order

Scan a handwritten grocery list → compare prices across BigBasket, Zepto, and Blinkit → add to cart in one tap.

```
photo of list
      │
      ▼
   Tesseract OCR
      │
      ▼
  item names + qty
      │
      ▼
  search all stores (parallel)
      │
      ▼
  algorithmic ranking (+ optional Groq LLM)
      │
      ▼
  cheapest per item + cart breakdown
      │
      ▼
  "Add all to cart"
```

## Architecture

| Layer | Tech |
|---|---|
| Mobile | React Native + Expo (iOS + Android) |
| Backend | FastAPI on Render |
| Session store | Upstash Redis (multi-user, 30-day TTL) |
| Store access | httpx — no Playwright, no browser |
| OCR | Tesseract via pytesseract + Pillow |
| LLM ranking | Groq `llama-3.3-70b-versatile` (optional) |

### How store login works

The mobile app opens each store's website in an embedded WebView.
After the user logs in normally, the app extracts cookies (and localStorage for Zepto) via `@react-native-cookies/cookies` and posts them to the backend.
The backend stores them in Redis under `user:{user_id}` and uses them for subsequent API calls via httpx.

**No browser automation, no Playwright, no bot detection risk.**

### Web UI

The backend also serves a web UI (`/`).
Enter the 8-character link code shown in the mobile app → the web UI inherits your session and lets you do OCR + compare + cart on desktop.

---

## Repo layout

```
scan2order/
├── backend/
│   ├── server.py            # FastAPI app, all endpoints
│   ├── ocr.py               # Tesseract OCR + image pre-processing
│   ├── ranker.py            # algorithmic ranking + Groq fallback
│   ├── requirements.txt
│   ├── render.yaml          # Render deploy config
│   ├── .env.example
│   ├── storage/
│   │   └── user_store.py    # Upstash Redis helpers
│   ├── stores/
│   │   ├── bigbasket.py     # BigBasket httpx client
│   │   ├── zepto.py         # Zepto httpx client + crypto signing
│   │   ├── blinkit.py       # Blinkit httpx client
│   │   └── zepto_sign.py    # Zepto SHA-256 request signing
│   └── templates/
│       └── index.html       # Web UI (single page, vanilla JS)
└── mobile/
    ├── App.js               # Navigation root
    ├── config.js            # BACKEND_URL + api() helper
    ├── index.js
    ├── app.json             # Expo config
    ├── package.json
    └── screens/
        ├── ConnectScreen.js          # Multi-store hub
        ├── ConnectBigBasketScreen.js # WebView + cookie extraction
        ├── ConnectZeptoScreen.js     # WebView + cookie + localStorage
        ├── ConnectBlinkitScreen.js   # GPS + optional WebView
        ├── HomeScreen.js             # OCR, item list, share code
        └── ResultsScreen.js         # Comparison, swap selections, cart
```

---

## Setup

### 1. Upstash Redis

1. Create a free database at <https://upstash.com>
2. Copy the **REST URL** and **REST Token** from the database console

### 2. Backend — local dev

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env → fill in UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN
# Optionally add GROQ_API_KEY for AI-assisted ranking

uvicorn server:app --reload --port 8000
# → http://localhost:8000
```

**Tesseract** must be installed separately:

| Platform | Command |
|---|---|
| macOS | `brew install tesseract` |
| Ubuntu/Debian | `sudo apt install tesseract-ocr` |
| Windows | Download installer from [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) |

OCR is optional — the app still works without it (manual item entry).

### 3. Backend — deploy to Render

1. Push your repo to GitHub
2. In Render → **New Web Service** → connect your repo
3. Set **Root Directory** to `backend`
4. Render will auto-detect `render.yaml`; confirm the settings
5. Add environment variables in the Render dashboard:
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`
   - `GROQ_API_KEY` (optional)
6. Deploy — note your service URL (e.g. `https://scan2order-backend.onrender.com`)

### 4. Mobile app

```bash
cd mobile
npm install

# Point the app at your backend:
# Edit config.js → BACKEND_URL = 'https://your-service.onrender.com'

npx expo start          # scan QR with Expo Go
# or
npx expo run:ios        # Xcode required
npx expo run:android    # Android Studio required
```

---

## API reference

All endpoints (except `GET /` and `GET /api/status`) require the `X-User-ID` header.
The mobile app generates a UUID on first launch and stores it in AsyncStorage.

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/api/status` | Health check + feature flags |
| `POST` | `/api/link/generate` | Generate 8-char code for web UI pairing (15-min TTL) |
| `POST` | `/api/link/use/{code}` | Web: exchange code for `user_id` |
| `GET` | `/api/link/peek/{code}` | Check if a code is still valid |
| `GET` | `/api/stores` | List connected stores + status |
| `POST` | `/api/connect/{store}` | Save store cookies/localStorage to Redis |
| `POST` | `/api/disconnect/{store}` | Remove store session from Redis |
| `POST` | `/api/location` | Save Blinkit GPS coordinates |
| `POST` | `/api/ocr` | Run Tesseract on uploaded image, return item list |
| `POST` | `/api/search` | Search a single query across all connected stores |
| `POST` | `/api/compare` | Full comparison: OCR items → search all stores → rank |
| `GET` | `/api/compare/progress` | SSE-style progress for ongoing compare |
| `POST` | `/api/compare/item` | Compare a single item (used by web UI) |
| `POST` | `/api/cart/add-all` | Add compared items to each store's cart |
| `GET` | `/api/cart/progress` | Progress for ongoing add-to-cart |

### `POST /api/compare` body

```json
{
  "items": [
    {"name": "milk", "qty": "1L", "count": 2},
    {"name": "eggs", "qty": "12", "count": 1}
  ],
  "stores": ["bigbasket", "zepto", "blinkit"]
}
```

### `POST /api/compare` response shape

```json
{
  "comparison": [
    {
      "item": {"name": "milk", "qty": "1L", "count": 2},
      "search_query": "milk 1L",
      "prices": {
        "bigbasket": [...products],
        "zepto": [...products],
        "blinkit": [...products]
      },
      "cheapest_app": "zepto",
      "cheapest_price": 68.0,
      "selected_pid": "pid_zepto_123"
    }
  ],
  "carts": {
    "zepto": {"items": [...], "total": 450.0},
    "bigbasket": {"items": [...], "total": 310.0}
  },
  "grand_total": 760.0,
  "summary": {"total_items": 2, "items_found": 2, "apps_used": 2}
}
```

---

## Ranking algorithm

For each item the ranker:

1. **Filters** products whose names don't contain all non-generic query words (e.g. "amul" must appear if the user wrote "amul butter")
2. **Removes** unreasonably sized variants (e.g. 5 kg pack when user asked for 500 g)
3. **Sorts** by `(qty_distance, price_per_unit, absolute_price)` ascending
4. Picks the cheapest per store, then the cheapest across stores

If `GROQ_API_KEY` is set, items where the top algorithmic pick seems uncertain are re-ranked by `llama-3.3-70b-versatile`.

---

## Store notes

### BigBasket
Requires login. Cookies extracted via WebView on first use.
Key cookie: `BBAUTHTOKEN`.

### Zepto
Requires login. Cookies + localStorage extracted via injected JS in WebView.
Key cookie: `accessToken`. Requests are SHA-256 signed per-call.
Token refresh is handled automatically; refreshed tokens are saved back to Redis.

### Blinkit
**Search works without login** (only GPS coordinates needed).
Cart add requires login cookies (`gr_1_deviceId` etc.).
GPS is captured via `expo-location` on the ConnectBlinkit screen.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `UPSTASH_REDIS_REST_URL` | ✅ | Upstash REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | ✅ | Upstash auth token |
| `GROQ_API_KEY` | ❌ | Enables Groq LLM re-ranking |
| `PORT` | ❌ | HTTP port (Render sets this automatically) |
