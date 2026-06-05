"""ocr.py - Grocery list extraction from images.

Backends, selected by the OCR_BACKEND env var:

  "auto" (default)  Groq cloud vision first if any Groq API key is set (fast, runs
                    off-box so concurrent scans don't lag each other); else the
                    local vision LLM (qwen2.5vl via Ollama); else Tesseract.
                    Falls through on error/empty.
  "groq"            Groq cloud vision only (needs a Groq API key).
  "ollama" / "vlm"  Local vision LLM only (needs OLLAMA_HOST).
  "tesseract"       Tesseract only (the no-network/no-model fallback).

A vision model reads handwriting far better than Tesseract. The local path
reuses the same OLLAMA_MODEL as the ranker (one model serves both); the Groq
path runs Llama 4 Scout on Groq's LPUs (~1-2s, no local CPU cost).

After transcription, an optional context-correction pass (OCR_CONTEXT_CORRECTION,
default on) runs a cheap text LLM over the lines to fix misreads using grocery
knowledge (e.g. 'Green Yogurt' → 'Greek Yogurt') and drop accidental duplicates.

Env vars:
  OCR_BACKEND        auto | groq | ollama | tesseract   (default: auto)
  GROQ_API_KEY       enables the Groq cloud path (preferred in auto mode). May hold
                     SEVERAL keys (comma/space separated) — when one hits its free-tier
                     limit (HTTP 429) the next is used automatically.
  GROQ_API_KEY_1..N  numbered Groq keys, combined with GROQ_API_KEY.
  GROQ_OCR_MODEL     Groq vision model (default: meta-llama/llama-4-scout-17b-16e-instruct)
  GROQ_CORRECTION_MODEL  text model for the correction pass (default: GROQ_OCR_MODEL)
  OCR_CONTEXT_CORRECTION  auto/on (default) | 0/off — toggle the correction pass
  OLLAMA_HOST        e.g. http://ollama:11434           (required for the local VLM path)
  OCR_VISION_MODEL   overrides the local OCR model only (default: OLLAMA_MODEL or qwen2.5vl:3b)
  OLLAMA_MODEL       shared local model name            (default: qwen2.5vl:3b)
"""

import asyncio
import base64
import io
import os
import platform
import re

OCR_AVAILABLE = False   # Tesseract availability (the fallback path)
OCR_ERROR = ""

try:
    import pytesseract
    from PIL import Image, ImageOps

    if platform.system() == "Windows":
        candidates = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
            os.path.expanduser(r"~\AppData\Local\Tesseract-OCR\tesseract.exe"),
        ]
        for path in candidates:
            if os.path.exists(path):
                pytesseract.pytesseract.tesseract_cmd = path
                print(f"[ocr] Tesseract found at: {path}")
                break
        else:
            print("[ocr] Tesseract not found at common Windows paths - relying on PATH.")

    pytesseract.get_tesseract_version()
    OCR_AVAILABLE = True
    print(f"[ocr] Tesseract version: {pytesseract.get_tesseract_version()}")

except ImportError as e:
    OCR_ERROR = f"Python packages missing: {e}. Run: pip install pytesseract Pillow"
    print(f"[ocr] Tesseract disabled - {OCR_ERROR}")
except Exception as e:
    OCR_ERROR = (
        f"Tesseract binary not callable ({e}). "
        f"Install from https://github.com/UB-Mannheim/tesseract/wiki"
    )
    print(f"[ocr] Tesseract disabled - {OCR_ERROR}")


_OCR_SECTION_HEADERS = {
    "shopping list", "grocery list", "list",
    "vegetables", "fruits", "dairy", "meat", "snacks",
    "beverages", "drinks", "produce", "grains", "spices",
    "shopping", "groceries", "items", "pantry", "frozen",
}


def _clean_ocr_line(line: str) -> str:
    line = line.strip()
    while line and not line[0].isalnum():
        line = line[1:].strip()
    while line and not line[-1].isalnum():
        line = line[:-1].strip()
    line = re.sub(r"^\(?[0-9]+[.\)]\s*", "", line).strip()
    line = re.sub(r"^[a-z]\s+(?=[A-Z])", "", line)
    line = re.sub(
        r"([A-Za-z])\s*['\"‘’“”]\s*([A-Za-z])",
        r"\1\2", line,
    )
    return re.sub(r"\s+", " ", line).strip()


def _is_header_line(line: str) -> bool:
    if line.endswith(":"):
        return True
    low = line.lower().rstrip(":.").strip()
    return low in _OCR_SECTION_HEADERS


_COMPOUND_SPLIT_RE = re.compile(r"\s+[–—\-]\s+|\s+\+\s+")


def _split_compound(line: str) -> list[str]:
    """Split 'Spinach - Carrots' → ['Spinach', 'Carrots'].

    Only splits on surrounded separators ( - or + with spaces on both sides)
    to avoid chopping mid-word hyphens or quantity strings like '500g'.
    """
    parts = _COMPOUND_SPLIT_RE.split(line)
    return [p.strip() for p in parts if p.strip()] if len(parts) > 1 else [line]


def _lines_to_items(text: str) -> list[str]:
    """Clean a block of text (one item per line) into a list of grocery items.

    Also splits compound lines like 'Spinach - Carrots' or 'Pasta + Pasta Sauce'
    into individual items in case the model groups them despite the prompt.
    """
    items = []
    for line in text.split("\n"):
        if not line.strip() or len(line.strip()) < 2:
            continue
        cleaned = _clean_ocr_line(line)
        if not cleaned or len(cleaned) < 2:
            continue
        if _is_header_line(cleaned):
            continue
        for part in _split_compound(cleaned):
            if len(part) >= 2:
                items.append(part)
    return items


# ── Tesseract backend (fallback) ───────────────────────────────────────────────

def _preprocess_image(raw_bytes: bytes):
    img = Image.open(io.BytesIO(raw_bytes))
    transposed = ImageOps.exif_transpose(img)
    if transposed is not None:
        img = transposed
    img = img.convert("L")
    img = ImageOps.autocontrast(img, cutoff=3)
    w, h = img.size
    if max(w, h) < 2000:
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS  # type: ignore[attr-defined]
        img = img.resize((w * 2, h * 2), resample)
    return img


def _extract_tesseract(raw_bytes: bytes) -> dict:
    """Synchronous Tesseract OCR. Run via asyncio.to_thread from the dispatcher."""
    if not OCR_AVAILABLE:
        return {"error": OCR_ERROR or "Tesseract not available", "items": []}
    try:
        img = _preprocess_image(raw_bytes)
        text = pytesseract.image_to_string(img, lang="eng", config="--psm 6")
        items = _lines_to_items(text)
        print(f"[ocr] tesseract: {len(items)} items from {len(text.splitlines())} lines")
        return {"raw_text": text, "items": items}
    except Exception as e:
        print(f"[ocr] tesseract error: {e}")
        return {"error": str(e), "items": []}


# ── Vision-LLM backend (Gemma 4 via Ollama) ─────────────────────────────────────

_VLM_PROMPT = (
    "This image contains a handwritten grocery shopping list. "
    "Your ONLY task is to transcribe what is written — do not interpret, "
    "correct, or substitute words. "
    "Write EVERY item you can see, ONE item per line, in the order written. "
    "Scan the ENTIRE image carefully, including items at the top, bottom, and "
    "edges — do not stop early. "
    "If you cannot read a word clearly, write your best guess rather than "
    "skipping it — a wrong guess is better than a missing item. "
    "NEVER invent or add items that are not physically written on the list. "
    "IMPORTANT: Unit-of-measure words (Pint, Quart, Litre, Ounce, Pound, Dozen) are "
    "NOT grocery items — do not output them as standalone items. "
    "If multiple items appear on one line separated by a dash ( - ) or "
    "plus ( + ) with spaces on both sides, write each as its OWN separate line "
    "(e.g. 'Spinach - Carrots' → two lines; 'Pasta + Pasta Sauce' → two lines). "
    "Keep quantities and units exactly as written (e.g. '2 kg onions', 'mango 500 gm'). "
    "Output ONLY the list items — no numbering, no bullet points, no headings, "
    "no commentary, no blank lines. If there is no list, output nothing."
)


def _ocr_model() -> str:
    return (os.getenv("OCR_VISION_MODEL")
            or os.getenv("OLLAMA_MODEL")
            or "qwen2.5vl:3b")


def _downscale_for_vlm(raw_bytes: bytes, max_dim: int) -> bytes:
    """Shrink the image so the vision model has far fewer pixels to process.

    Vision-LLM latency is dominated by image-token prefill, which scales with
    pixel count. Phone photos are huge (e.g. 3000px), making CPU inference take
    minutes. Downscaling the longest side to ~max_dim keeps handwriting legible
    while cutting prefill dramatically. Re-encoded as JPEG. Falls back to the
    original bytes on any error.
    """
    try:
        import io
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(raw_bytes))
        t = ImageOps.exif_transpose(img)
        if t is not None:
            img = t
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.LANCZOS  # type: ignore[attr-defined]
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), resample)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return buf.getvalue()
    except Exception as e:
        print(f"[ocr] downscale skipped: {e}")
        return raw_bytes


async def _extract_vlm(raw_bytes: bytes, host: str) -> dict:
    """OCR via a local vision LLM on Ollama (OpenAI-compatible endpoint).

    Returns {"raw_text", "items"} on success or {"error", "items": []} on failure.
    The image is sent as a base64 data URL; Ollama decodes by content so the
    declared MIME type does not need to match the real format.
    """
    model = _ocr_model()
    # Resolution vs memory/speed. On the 16GB CPU box qwen2.5vl at 1200px needs
    # ~10.1GB (more than is free) and OOMs → Tesseract garbage; 900px needs ~7GB,
    # fits, AND is faster. qwen reads handwriting fine at 900px (gemma4 couldn't).
    # The memory ceiling here is ~1000px, so if a scan returns empty nudge up only
    # slightly. max_tokens 384 covers a ~30-item list without truncating.
    max_dim = int(os.getenv("OCR_MAX_DIM", "900") or "900")
    max_tokens = int(os.getenv("OCR_MAX_TOKENS", "384") or "384")
    timeout_s = float(os.getenv("OCR_TIMEOUT", "90") or "90")
    img_bytes = _downscale_for_vlm(raw_bytes, max_dim)
    b64 = base64.b64encode(img_bytes).decode()
    data_url = f"data:image/jpeg;base64,{b64}"
    print(f"[ocr] vlm sending {len(img_bytes)//1024}KB image (max_dim={max_dim}) to {model}")

    try:
        import httpx
        # Total cap so a runaway generation can't block the Ollama queue for
        # minutes (the frontend can also cancel, which closes this connection
        # and makes Ollama abort the run).
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            resp = await client.post(
                f"{host}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _VLM_PROMPT},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }],
                    "temperature": 0,
                    "max_tokens": max_tokens,
                },
            )
        if resp.status_code != 200:
            print(f"[ocr] vlm HTTP {resp.status_code}: {resp.text[:200]}")
            return {"error": f"VLM HTTP {resp.status_code}", "items": []}

        text = resp.json()["choices"][0]["message"]["content"].strip()
        items = _lines_to_items(text)
        print(f"[ocr] vlm ({model}): {len(items)} items")
        return {"raw_text": text, "items": items}
    except Exception as e:
        print(f"[ocr] vlm error: {e}")
        return {"error": f"VLM error: {e}", "items": []}


# ── Groq cloud backend (Llama 4 vision, OpenAI-compatible) ───────────────────

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Index of the key to try FIRST on the next request. Advances when a key gets
# rate-limited/exhausted (HTTP 429) so we don't keep hitting a dead key, and
# wraps around so an earlier key is retried once a later one fails (covers the
# daily-quota reset without any timer). Process-local; resets on restart.
_groq_key_idx = 0


def _groq_keys() -> list[str]:
    """All configured Groq API keys, in priority order, de-duplicated.

    The user can stack several free-tier keys so scanning keeps working after
    one key hits its daily limit. Supply keys with either or both styles:
      • GROQ_API_KEY="key1,key2,key3"   (comma / whitespace / newline separated)
      • GROQ_API_KEY_1=..., GROQ_API_KEY_2=...  (numbered keys)
    """
    keys: list[str] = []

    def add_from_env(name: str) -> None:
        raw = os.getenv(name, "") or ""
        for part in re.split(r"[,\s]+", raw.strip()):
            if part:
                keys.append(part)

    add_from_env("GROQ_API_KEY")
    add_from_env("GROQ_API_KEY_1")

    i = 2
    while True:
        name = f"GROQ_API_KEY_{i}"
        if not (os.getenv(name, "") or "").strip():
            break
        add_from_env(name)
        i += 1
    seen: set = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


async def _groq_chat(messages: list, keys: list[str], *, model: str,
                     max_tokens: int, timeout: float = 30.0) -> tuple[str | None, str]:
    """POST a chat-completion to Groq, rotating API keys on quota/auth failures.

    Tries keys starting at the last-known-good index and wrapping around; a 429
    (rate-limited / daily quota exhausted) or 401/403 (invalid key) rotates to
    the next key, any other failure stops (another key won't fix a 5xx/network
    error). Returns (content, "") on success or (None, error_message).
    """
    global _groq_key_idx
    n = len(keys)
    if n == 0:
        return None, "no Groq API key configured"
    last_err = ""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for off in range(n):
                idx = (_groq_key_idx + off) % n
                try:
                    resp = await client.post(
                        _GROQ_URL,
                        headers={"Authorization": f"Bearer {keys[idx]}",
                                 "Content-Type": "application/json"},
                        json={"model": model, "messages": messages,
                              "temperature": 0, "max_tokens": max_tokens},
                    )
                except Exception as e:
                    return None, f"Groq request error: {e}"
                if resp.status_code == 200:
                    _groq_key_idx = idx   # remember the working key for next time
                    if off and n > 1:
                        print(f"[ocr] groq: rotated to key #{idx + 1}/{n}")
                    return resp.json()["choices"][0]["message"]["content"].strip(), ""
                if resp.status_code in (429, 401, 403):
                    last_err = f"Groq HTTP {resp.status_code} on key #{idx + 1}/{n}"
                    print(f"[ocr] {last_err} (exhausted/invalid) → rotating to next key")
                    continue
                last_err = f"Groq HTTP {resp.status_code}: {resp.text[:160]}"
                print(f"[ocr] {last_err}")
                return None, last_err
    except Exception as e:
        return None, f"Groq client error: {e}"
    return None, (last_err or "all Groq keys exhausted")


async def _extract_groq(raw_bytes: bytes, keys: list[str]) -> dict:
    """OCR via Groq's hosted vision model, with multi-key failover.

    Runs off-box on Groq's LPUs — fast (~1-2s) and doesn't compete with the
    local CPU, so concurrent scans don't lag each other. When the active key
    runs out of free-tier usage (HTTP 429) it automatically rotates to the next
    configured key (see _groq_keys / _groq_chat).
    Default model: Llama 4 Scout (smaller/faster; plenty for a grocery list).
    """
    model = os.getenv("GROQ_OCR_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    # Groq allows up to 20MB and is fast, so we can afford a higher resolution
    # than the local path (which is RAM-bound). 1280px reads handwriting well.
    img_bytes = _downscale_for_vlm(raw_bytes, int(os.getenv("GROQ_OCR_MAX_DIM", "1280") or "1280"))
    b64 = base64.b64encode(img_bytes).decode()
    data_url = f"data:image/jpeg;base64,{b64}"
    print(f"[ocr] groq sending {len(img_bytes)//1024}KB image to {model} "
          f"({len(keys)} key(s) available)")

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": _VLM_PROMPT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]
    text, err = await _groq_chat(messages, keys, model=model, max_tokens=512)
    if text is None:
        return {"error": f"Groq error: {err}", "items": []}
    items = _lines_to_items(text)
    print(f"[ocr] groq ({model}): {len(items)} items")
    return {"raw_text": text, "items": items}


# ── Context-aware correction pass ─────────────────────────────────────────────

_CORRECTION_PROMPT = (
    "You are cleaning up a grocery shopping list transcribed from a handwritten photo "
    "by an OCR model. Some lines may have reading errors. "
    "Fix clear English-level misreads using grocery knowledge "
    "(e.g. 'Green Yogurt' → 'Greek Yogurt', 'Butterr' → 'Butter'). "
    "IMPORTANT: Indian grocery names written in English (e.g. Palak, Aloo, Tamatar, "
    "Bhindi, Shimla Mirch, Kaddu, Lauki, Arbi, Methi, Gobhi) are VALID grocery items — "
    "do NOT change or remove them, even if they look like misreads. "
    "Keep each line's quantity and unit unchanged (e.g. '500 gm', '1 kg'). "
    "Remove a line ONLY when it is:\n"
    "  1. An exact duplicate of a previous line, OR\n"
    "  2. A bare unit-of-measure word with no product context — words like "
    "'Pint', 'Quart', 'Litre', 'Ounce', 'Pound', 'Dozen' alone are not grocery "
    "items and should be dropped.\n"
    "Do NOT add new items. Do NOT drop distinct valid grocery items. "
    "Return the corrected list, one item per line — no numbering, bullets, or commentary.\n\n"
    "List:\n"
)


def _correction_enabled() -> bool:
    return (os.getenv("OCR_CONTEXT_CORRECTION", "auto").strip().lower()
            not in ("0", "false", "no", "off"))


async def _correct_items(items: list[str], keys: list[str], host: str | None) -> list[str]:
    """Second pass: fix OCR misreads using grocery context + drop accidental dupes.

    Cheap text-only LLM call (Groq preferred with key rotation, else local
    Ollama). Returns the corrected list, or the original list unchanged on any
    error or an implausible result (so correction can only help, never lose data).
    """
    if not items or not _correction_enabled():
        return items
    prompt = _CORRECTION_PROMPT + "\n".join(items)
    messages = [{"role": "user", "content": prompt}]
    corrected_text = None

    if keys:
        model = os.getenv("GROQ_CORRECTION_MODEL") or os.getenv(
            "GROQ_OCR_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        corrected_text, err = await _groq_chat(messages, keys, model=model,
                                               max_tokens=512, timeout=20.0)
        if corrected_text is None:
            print(f"[ocr] correction (groq) skipped: {err}")
    elif host:
        corrected_text = await _ollama_text(messages, host)

    if not corrected_text:
        return items
    corrected = _lines_to_items(corrected_text)
    # Guard against a degenerate result: correction may legitimately drop ONE
    # accidental duplicate, but it must not gut the list or balloon it.
    if not corrected or len(corrected) < max(1, len(items) - max(2, len(items) // 3)) \
            or len(corrected) > len(items) + 1:
        print(f"[ocr] correction rejected ({len(items)}→{len(corrected)} items); keeping raw")
        return items
    if corrected != items:
        print(f"[ocr] correction applied: {len(items)}→{len(corrected)} items")
    return corrected


async def _ollama_text(messages: list, host: str) -> str | None:
    """Text-only completion on the local Ollama model (for the correction pass)."""
    model = _ocr_model()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0)) as client:
            resp = await client.post(
                f"{host}/v1/chat/completions",
                json={"model": model, "messages": messages,
                      "temperature": 0, "max_tokens": 512},
            )
        if resp.status_code != 200:
            print(f"[ocr] correction (ollama) HTTP {resp.status_code}")
            return None
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[ocr] correction (ollama) error: {e}")
        return None


# ── Dispatcher ───────────────────────────────────────────────────────────────

async def extract_grocery_list(raw_bytes: bytes) -> dict:
    """Extract grocery list items from image bytes.

    Backend chosen by OCR_BACKEND (auto | groq | ollama | tesseract):
      • groq      — Groq cloud vision only (needs GROQ_API_KEY).
      • ollama    — local vision LLM only (needs OLLAMA_HOST).
      • tesseract — local Tesseract only.
      • auto      — Groq first if GROQ_API_KEY is set (fast, off-box), then the
                    local vision LLM if OLLAMA_HOST is set, then Tesseract.
    Returns {"raw_text": str, "items": [str]} or {"error": str, "items": []}.
    """
    backend = os.getenv("OCR_BACKEND", "auto").strip().lower()
    groq_keys = _groq_keys()
    host = os.getenv("OLLAMA_HOST")

    async def _finish(result: dict) -> dict:
        """Apply the context-correction pass to a successful transcription."""
        if result.get("items"):
            result["items"] = await _correct_items(result["items"], groq_keys, host)
        return result

    # ── Explicit single-backend modes ───────────────────────────────────────
    if backend == "groq":
        if not groq_keys:
            return {"error": "OCR_BACKEND=groq but GROQ_API_KEY is not set", "items": []}
        return await _finish(await _extract_groq(raw_bytes, groq_keys))
    if backend in ("ollama", "vlm"):
        if not host:
            return {"error": "OCR_BACKEND=ollama but OLLAMA_HOST is not set", "items": []}
        return await _finish(await _extract_vlm(raw_bytes, host))
    if backend == "tesseract":
        return await _finish(await asyncio.to_thread(_extract_tesseract, raw_bytes))

    # ── auto: Groq → local VLM → Tesseract ───────────────────────────────────
    if groq_keys:
        result = await _extract_groq(raw_bytes, groq_keys)
        if result.get("items"):
            return await _finish(result)
        print("[ocr] groq returned nothing → trying next backend")
    if host:
        result = await _extract_vlm(raw_bytes, host)
        if result.get("items"):
            return await _finish(result)
        print("[ocr] vlm returned nothing → falling back to Tesseract")

    # Tesseract path (sync, CPU-bound → offload so we don't block the event loop).
    return await _finish(await asyncio.to_thread(_extract_tesseract, raw_bytes))
