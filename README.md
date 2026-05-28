# scan2order

Grocery price comparison across BigBasket, Blinkit, and Zepto.
Scan a handwritten list → find the cheapest basket across all three stores → add everything to cart in one tap.

---

## How it works

```
Mobile app (React Native)              Web UI (browser)
  │                                      │
  ├─ Email + password login              ├─ Email + password login
  │  (or via shared 8-char link code)    │  (HMAC-signed session cookie)
  │                                      │
  ├─ Connect a store:                    ├─ Connect Blinkit / Zepto:
  │  opens BigBasket/Blinkit/Zepto in    │  Playwright headless Chromium
  │  a WebView, extracts auth cookies    │  streamed as JPEG screenshots;
  │                                      │  clicks/keystrokes forwarded
  │                                      │  back. Two-phase: login first,
  │                                      │  then save delivery address.
  │                                      │
  │                                      ├─ Connect BigBasket: mobile-only
  │                                      │  (Akamai blocks Playwright by
  │                                      │  TLS fingerprint; use mobile app)
  │                                      │
  └─ Scan / Compare / Order  ────────────┴─ Scan / Compare / Order
        (OCR, then httpx calls to each store's API)
```

Backend is FastAPI + httpx for store API calls + Playwright only for the login relay. Sessions live in SQLite (or MySQL, see below).

---

## Architecture

| Concern | Implementation |
|---|---|
| Web framework | FastAPI + uvicorn |
| User auth | Email + PBKDF2-SHA256 password; HMAC-SHA256 signed session cookie (6-day TTL) |
| Session storage | SQLite at `data/sessions.db` (default), or MySQL/MariaDB if `MYSQL_URL` is set |
| Store API calls | httpx — no Playwright in the request path |
| Browser login relay | Playwright headless Chromium for Blinkit and Zepto only |
| OCR | Tesseract |
| LLM ranking fallback | Local Ollama (default `llama3.2:3b`); only fires when the algorithmic ranker finds no winner |
| Deployment | Docker + docker-compose on a homeserver, optionally fronted by Cloudflare Tunnel |
| CI / auto-deploy | systemd timer that `git pull`s every 30 s and rebuilds on change |

---

## Project structure

```
scan2order3/
├── README.md
├── CLAUDE.md                       ← instructions for Claude collaborators
├── .env.example                    ← copy to .env and fill in
├── Dockerfile
├── docker-compose.yml              ← scan2order + ollama services
├── pytest.ini                      ← root config: testpaths = backend/tests
├── requirements.txt
│
├── backend/
│   ├── server.py                   ← FastAPI app, all routes
│   ├── auth.py                     ← password hashing, session cookies
│   ├── auth_browser.py             ← Playwright relay for Blinkit/Zepto login
│   ├── ocr.py                      ← Tesseract OCR (image → grocery list)
│   ├── ranker.py                   ← price comparison + Ollama LLM fallback
│   ├── render.yaml                 ← optional: deploy to Render instead
│   │
│   ├── storage/
│   │   └── user_store.py           ← SQLite or MySQL backend (one or the other)
│   │
│   ├── stores/
│   │   ├── _common.py              ← shared constants (mobile UA, etc.)
│   │   ├── bigbasket.py            ← httpx: listing-svc + mapi cart
│   │   ├── blinkit.py              ← httpx: v2 JSON search + v2 cart
│   │   ├── zepto.py                ← httpx: BFF gateway, layout-walking parser
│   │   └── zepto_sign.py           ← SHA-256 request signing
│   │
│   ├── templates/
│   │   ├── index.html              ← compare + cart + browser-relay UI
│   │   └── login.html              ← sign-in / sign-up
│   │
│   └── tests/                      ← 238 pytest tests, no network calls
│
└── mobile/                         ← React Native app (Phase 2)
```

---

## Running it

### Docker (recommended)

```bash
cp .env.example .env
# Edit .env to set SECRET_KEY (required for stable sessions across restarts)
docker compose up -d --build
```

This starts:
- `scan2order` on `127.0.0.1:8001` (web UI + API)
- `ollama` as a sidecar for the LLM ranking fallback

The `./data` directory is bind-mounted into the container, so `sessions.db` and `server.log` survive rebuilds.

Open `http://localhost:8001`, create an account, and connect Blinkit / Zepto via the browser relay. For BigBasket, use the mobile app (Akamai blocks Playwright).

### Without Docker

```bash
pip install -r requirements.txt
playwright install chromium
cd backend
uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

You'll also need Tesseract on PATH (`apt install tesseract-ocr`, `brew install tesseract`, or [the Windows installer](https://github.com/UB-Mannheim/tesseract/wiki)).

If you want the LLM fallback, run Ollama separately and set `OLLAMA_HOST=http://localhost:11434`.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | **Production** | Hex string used to sign session cookies. Without this, all users are logged out on every restart. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `OLLAMA_HOST` | No | Defaults to `http://ollama:11434` in docker-compose. Set to `http://localhost:11434` for local-without-Docker. |
| `OLLAMA_MODEL` | No | Defaults to `llama3.2:3b`. |
| `MYSQL_URL` | No | If set, uses MySQL/MariaDB instead of SQLite. Format: `mysql://user:pass@host:3306/db`. |
| `LOG_API_KEY` | No | Enables `GET /api/logs?key=...&n=300` for remote log access. |
| `PORT` | No | Defaults to 8000. |

---

## Tests

```bash
pip install -r backend/requirements-test.txt
pytest
```

The suite has 238 tests covering auth, the compare pipeline, the cart pipeline, OCR, ranker, the user-store layer, browser-relay wiring, the `/api/logs` endpoint, and 18 multi-user concurrency tests. No real network calls — Playwright and store APIs are mocked.

---

## API surface

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI (redirects to `/login` if no session) |
| `GET` | `/login` | Sign-in / sign-up page |
| `GET` | `/health` | Liveness check |
| `GET` | `/api/version` | Version string |
| `POST` | `/api/auth/signup` | Create account |
| `POST` | `/api/auth/login` | Sign in |
| `POST` | `/api/auth/logout` | Sign out |
| `GET` | `/api/auth/me` | Current user info |
| `POST` | `/api/auth/connect` | Persist cookies for a (user, store) pair |
| `GET` | `/api/auth/status/{user_id}` | Which stores are connected |
| `POST` | `/api/auth/link` | Generate 8-char link code (mobile → web) |
| `GET` | `/api/auth/link/{code}` | Resolve link code |
| `POST` | `/api/auth/browser/start/{store}` | Launch Playwright session (Blinkit/Zepto only) |
| `GET` | `/api/auth/browser/screenshot/{session_id}` | Current page as JPEG |
| `POST` | `/api/auth/browser/event/{session_id}` | Forward mouse/keyboard |
| `GET` | `/api/auth/browser/check/{session_id}` | Poll for login + address completion |
| `DELETE` | `/api/auth/browser/session/{session_id}` | Cancel session |
| `POST` | `/api/ocr` | OCR image → items |
| `POST` | `/api/compare` | Compare a list across all connected stores |
| `GET` | `/api/compare/progress` | Live progress (multi-user safe) |
| `POST` | `/api/cart/add-all` | Add everything to each store's cart |
| `GET` | `/api/cart/progress` | Cart-add progress |
| `GET` | `/api/logs` | Tail server log (requires `LOG_API_KEY`) |

Mobile-app-compat aliases (`/scan`, `/search`, `/order`, `/connect-account`, `/account-status/{user_id}`) are also served.

---

## Deployment options

**Homeserver + Cloudflare Tunnel (current):** `docker compose up -d`, expose via Cloudflare Tunnel — no open inbound ports, auto-deploy from GitHub via systemd timer. See `CLAUDE.md` for architecture details.

**Render:** `backend/render.yaml` is kept for anyone who wants to deploy there instead of self-hosting. Note that Playwright login will be flaky on Render's free tier (memory limits) and that you'll need an external Ollama instance — or to skip the LLM fallback entirely.

---

## Notes

- **BigBasket browser login is mobile-only.** Akamai Bot Manager validates TLS / JA3 fingerprints that Playwright's bundled Chromium cannot match. The mobile WebView is a real OS-level browser and is undetectable.
- **Blinkit and Zepto need a delivery address.** The browser relay waits for both the auth cookie AND the address-related cookie (`merchant_id` for Blinkit, `serviceability` for Zepto) before declaring the session complete — otherwise the BFF search APIs return zero results.
- **Multi-user concurrency.** The Playwright instance is lazily initialised behind an `asyncio.Lock`; cookie updates are serialised by a per-process lock so two simultaneous requests can't corrupt the cookie blob.
