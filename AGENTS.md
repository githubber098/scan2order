# Codex instructions for scan2order

## Git push workflow — MANDATORY

**Always `git pull --rebase` before every `git push`.** Never skip this step.

If the pull brings in commits that were not there before:
1. Read every new commit message in full (`git log` / `git show`).
2. Re-read every file the incoming commits touched.
3. Verify that your own changes do not silently overwrite or conflict with the collaborator's work — if they do, resolve properly and explain in your commit message what you merged.
4. Only then push.

There is only one branch: **main**. Both collaborators push here directly.

## Commit messages

Whenever code changes are made, **always compose a maximally detailed git commit message**. This project has two collaborators working in separate Codex sessions with no shared context — the commit message is the ONLY way each person's Codex understands what the other did. Write as if the reader has never seen this codebase before and has no access to chat history.

**Include every change, no matter how small.** A one-line CSS tweak, a renamed variable, a removed `console.log`, a tightened guard condition — all of it must appear as an explicit bullet. The collaborator's Codex will use this message to reconstruct full context; missing a "minor" fix means it will be silently reverted next session.

Every commit message must include:

- **Subject line** (≤72 chars): imperative mood, says what changed
- **Body — per-file bullet list**: for EVERY file touched, name it explicitly and describe *what specifically changed inside it* — not just "updated", but: which function/section changed, what the old behaviour was, what the new behaviour is, and why. Micro-changes (e.g. "added `|| []` guard to prevent null-crash on empty shortlist") belong here too.
- **Why**: one or two sentences on the motivation, including alternatives considered and rejected
- **Architecture context**: if the change touches a key design decision (storage backend, API format, auth flow, LLM integration, store module), explain the current approach so a reader with zero prior context understands the system state after this commit
- **Impact / action needed**: anything a collaborator must do after pulling — rebuild Docker image, run a migration, pull a model, update an env var, etc.

Do not commit silently or with a one-liner unless the user explicitly says so.

## Current architecture (keep this section up to date)

- **Session storage**: SQLite by default (`backend/storage/user_store.py`) — file lives at `/app/data/sessions.db` inside the container, mounted as `./data:/app/data` in docker-compose. Set `MYSQL_URL=mysql://user:pass@host:3306/db` to switch to MySQL/MariaDB instead; the `_MySQLDB` wrapper in `user_store.py` translates all SQL automatically (placeholder style, upsert syntax, DDL types). A commented-out MySQL service is in `docker-compose.yml` for easy opt-in. No external Redis or Upstash dependency.
- **OCR**: `backend/ocr.py`, `extract_grocery_list()` is **async**, backend chosen by `OCR_BACKEND` (default `auto` = Groq → local Ollama VLM → Tesseract; also `groq`, `ollama`, `tesseract`). Preferred path is **Groq cloud** when `GROQ_API_KEY` is set: `_extract_groq()` POSTs the image (base64 data URL) to Groq's OpenAI-compatible `/openai/v1/chat/completions` running Llama 4 Scout (`GROQ_OCR_MODEL`, default `meta-llama/llama-4-scout-17b-16e-instruct`) — ~1-2s, runs off-box so concurrent scans don't fight for the local CPU. Falls back to the local Ollama VLM (`_extract_vlm`), then Tesseract (`asyncio.to_thread`). Image is downscaled first (`_downscale_for_vlm`): Groq path caps at `GROQ_OCR_MAX_DIM` (default 1280); local path at `OCR_MAX_DIM` (default 900 — qwen OOMs at higher res on the 16GB box). History: Tesseract (bad handwriting) → gemma4:e2b (needed 1100px+, slow, OOM'd) → qwen2.5vl:3b (local) → Groq (moved off-box to avoid CPU contention under concurrency). **Multi-key failover**: `GROQ_API_KEY` may hold several comma-separated keys (or `GROQ_API_KEY_2/_3/…`); `_groq_keys()` builds the pool and `_groq_chat()` rotates to the next key on HTTP 429/401/403 (free-tier daily-limit exhaustion), remembering the last-good index (process-local `_groq_key_idx`, wraps around so earlier keys are retried after a daily reset). **Context-correction pass** (`_correct_items`, `OCR_CONTEXT_CORRECTION` default on): after transcription a cheap text-LLM pass (Groq, else Ollama) fixes misreads using grocery context (e.g. "Green Yogurt"→"Greek Yogurt") and drops accidental duplicate lines; falls back to the raw list on error or a degenerate result.
- **LLM ranking (local Ollama)**: `qwen2.5vl:3b` by default (`OLLAMA_MODEL`, reached via `OLLAMA_HOST`) powers the product-ranking fallback (`backend/ranker.py:_ollama_rank`, fires only when the algorithmic ranker finds no winner) and the local OCR fallback. NOT auto-pulled — `docker compose exec ollama ollama pull qwen2.5vl:3b`. With Groq handling OCR, the local model is mostly just the rarely-used ranking fallback.
- **Store API calls**: Four stores — all httpx, no Playwright in search/cart. **BigBasket**: listing-svc search + mapi cart (per-item). **Blinkit**: search `POST /v1/layout/search` (`?q=` param; `lat`+`lon` headers required; `access_token` = URL-decoded `gr_1_accessToken`; cached `api_auth_key`; `app_client: consumer_web`). Cart `POST /v5/carts` (batch-replace, never per-item; requires `app_version`/`rn_bundle_version`/`web_app_version` headers). **Zepto**: BFF search `POST /user-search-service/api/v3/search` (SHA-256 signed); cart `/cfs/api/v1/bulk-widget-data`; needs `store_id` from `serviceability` cookie (absent until delivery address saved → must reconnect; `_hunt_store_id` tries to recover it from stored JSON blobs). **Instamart** (`backend/stores/instamart.py`): search `POST /api/instamart/search/v2` (storeId as query param); cart per-item `POST /api/instamart/checkout/v2/cart/item` wrapped in `add_all_to_cart_api` batch shape; auth via `tid`/`sid`/`deviceId` cookies; `_raw_device_id()` extracts UUID for `x-device-id`; needs storeId (same reconnect requirement as Zepto). All four are registered identically in `server.py`, `ranker.py`, `auth_browser.py`.
- **Store session health**: `/api/auth/status/{user_id}` returns per-store `healthy` + `reason` (via static `blinkit.session_health()` / `zepto.session_health()` / `instamart.session_health()`, no network call). The web UI shows a yellow dot + inline "reconnect" note on page load for a connected-but-broken store (e.g. Zepto/Instamart with no `store_id`), plus a banner after a compare when a connected store returns zero results across all items (likely-expired tokens).
- **Deployment**: Docker + docker-compose on homeserver, exposed via Cloudflare Tunnel (no open inbound ports). Auto-updates from GitHub via systemd timer every 5 min.
- **Auth flow (web)**: Passwordless — phone (Twilio SMS OTP) OR email (SMTP OTP). `backend/auth.py` (normalise + 6-digit OTP + HMAC session cookie), `backend/sms.py` (Twilio), `backend/email_sender.py` (SMTP); all three fall back to printing the OTP to the server log if their provider env vars are unset. `users` table has nullable-unique `phone` + `email` (an account can have one or both); `otp_codes` is keyed by `target` (phone or email). Endpoints: `/api/auth/send-otp` + `/api/auth/verify-otp` (login, `{channel,value[,code]}`), and `/api/auth/method/send-otp` + `/api/auth/method/verify` (link a 2nd method to the logged-in account — verified by OTP, rejects contacts already used elsewhere). After logging in with one method the web UI shows a banner prompting to add+verify the other. `get_or_create_user(channel,value)` auto-creates accounts on first verified login.
- **Auth flow (mobile / store sessions)**: Mobile logs in to each store via WebView, cookies sent to backend → stored in SQLite `sessions` table. Web UI can enter an 8-char link code generated by mobile to share the store session. (Store auth is separate from the user-account auth above.)
- **Shop tab** (`/shop`, `templates/shop.html`): grocery-app-style single-item browse. Search input fires with 350ms debounce on `oninput`; clearing the field reverts to the recommended page without a network call. `POST /api/shop/search` fires concurrent `search_item_api()` across all connected stores, relevance-filters, dedupes by (app, product_id), attaches `price_per_unit` (`ranker._price_per_unit`) + `ppu_label`, sorts cheapest-per-unit first. `POST /api/shop/add` (session-required, else 403 `login_required`) adds via `_add_items_to_store()` — uses `_BATCH_STORES` dict (zepto/blinkit/instamart all via `add_all_to_cart_api`; BB per-item). POPULAR chips = 20 items. Trending cards cached 10 min server-side. Voice helper on search + compare textarea.
- **Compare page** (`/`, `templates/index.html`): item results displayed as thin `.irow` rows — product image thumbnail (from store CDN), item name, store chip + matched product name, effective price, three action buttons: Swap (opens `.swap-sheet` bottom-sheet with all alternatives + product images), Edit qty (re-runs `/api/compare/item` in-place), Delete (removes item, rebuilds carts). Guest mode returns cheapest-only via `_guest_strip_entry`. OCR upload shows a CSS spinner animation while processing.
- **Client-side cart**: localStorage per `user_id`, synced to stores via `/api/shop/add`; header `.cart-badge` count driven by `s2o.shopCartCount()`.
- **Guest mode** (logged-out browsing): `_template_response(allow_guest=True)` lets `/` and `/shop` render without a session (`guest=True`, `user_id=None`, `window._SERVER_GUEST`). Restrictions enforced **server-side**, not just hidden: `/api/compare` for a guest (no session AND no body `user_id`) returns cheapest-only via `_guest_strip_entry()` (no price matrix/shortlist/carts, not saved to history); `/api/shop/add` → 403. Frontend hides History, gates actions behind a login bottom-sheet (`#login-prompt`, `s2o_loginPrompt()`), and `[data-guest-hide]` elements. Optional `GUEST_STORE_USER_ID` env backs guest price lookups with an owner account's store sessions (READ-ONLY; off by default).

## Phase status

- **Phase 1** ✅ — backend complete (`backend/`)
- **Phase 2** ✅ — mobile app present (`mobile/`) — needs BACKEND URL updated to Cloudflare Tunnel URL and end-to-end testing
- **Phase 3** ⏳ — `render.yaml` (for anyone who wants Render instead of homeserver)

## Source directories (read-only)

- `../scan2order1/` — original mobile app + simple backend (reference only)
- `../scan2order2/` — mature backend with Playwright (reference only)

Never modify files under `../scan2order1/` or `../scan2order2/`.
