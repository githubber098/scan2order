# Claude instructions for scan2order3

## Commit messages

Whenever code changes are made in this project, **always compose a detailed git commit message** before committing. The message must include:

- **Subject line** (≤72 chars): imperative mood, summarises what changed
- **Body**: bullet list of every file touched and what specifically changed in it
- **Why**: one sentence explaining the motivation or context
- **Impact / notes**: anything that affects runtime behaviour, deployment, or testing (e.g. "Blinkit cart endpoint needs live session validation", "requires re-deploy on Render")

Do not commit silently or with a one-liner unless the user explicitly says so.

## Phase status

- **Phase 1** ✅ — backend complete (`scan2order3/backend/`)
- **Phase 2** ⏳ — mobile wiring (copy `scan2order1/mobile/`, add ConnectBlinkitScreen + ConnectZeptoScreen, update App.js + HomeScreen.js)
- **Phase 3** ⏳ — `render.yaml` + Render deploy docs with Tesseract build command

## Source directories (read-only)

- `../scan2order1/` — original mobile app + simple backend
- `../scan2order2/` — mature backend with Playwright (reference only)

Never modify files under `../scan2order1/` or `../scan2order2/`.

## Architecture reminders

- No Playwright anywhere — all store calls are direct `httpx` requests
- All sessions in Upstash Redis, keyed by `user_id`
- Blinkit cart API (`POST /v2/client/user_cart/`) needs validation against a live session
- `user_store.py` uses the sync upstash-redis client inside async handlers — acceptable at current scale
