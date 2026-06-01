"""ocr.py - Grocery list extraction from images.

Backends, selected by the OCR_BACKEND env var:

  "auto" (default)  Groq cloud vision first if GROQ_API_KEY is set (fast, runs
                    off-box so concurrent scans don't lag each other); else the
                    local vision LLM (qwen2.5vl via Ollama); else Tesseract.
                    Falls through on error/empty.
  "groq"            Groq cloud vision only (needs GROQ_API_KEY).
  "ollama" / "vlm"  Local vision LLM only (needs OLLAMA_HOST).
  "tesseract"       Tesseract only (the no-network/no-model fallback).

A vision model reads handwriting far better than Tesseract. The local path
reuses the same OLLAMA_MODEL as the ranker (one model serves both); the Groq
path runs Llama 4 Scout on Groq's LPUs (~1-2s, no local CPU cost).

Env vars:
  OCR_BACKEND        auto | groq | ollama | tesseract   (default: auto)
  GROQ_API_KEY       enables the Groq cloud path (preferred in auto mode)
  GROQ_OCR_MODEL     Groq vision model (default: meta-llama/llama-4-scout-17b-16e-instruct)
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


def _lines_to_items(text: str) -> list[str]:
    """Clean a block of text (one item per line) into a list of grocery items."""
    items = []
    for line in text.split("\n"):
        if not line.strip() or len(line.strip()) < 2:
            continue
        cleaned = _clean_ocr_line(line)
        if not cleaned or len(cleaned) < 2:
            continue
        if _is_header_line(cleaned):
            continue
        items.append(cleaned)
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
    "This image is a grocery shopping list, possibly handwritten. "
    "Transcribe every item, one per line, exactly as written. "
    "Keep quantities and units if present (e.g. '2 kg onions', 'Amul butter 100g'). "
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


async def _extract_groq(raw_bytes: bytes, api_key: str) -> dict:
    """OCR via Groq's hosted vision model (OpenAI-compatible endpoint).

    Runs off-box on Groq's LPUs — fast (~1-2s) and doesn't compete with the
    local CPU, so concurrent scans don't lag each other. Same image-message
    shape as the Ollama path; only the URL, auth header, and model differ.
    Default model: Llama 4 Scout (smaller/faster; plenty for a grocery list).
    """
    model = os.getenv("GROQ_OCR_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
    # Groq allows up to 20MB and is fast, so we can afford a higher resolution
    # than the local path (which is RAM-bound). 1280px reads handwriting well.
    img_bytes = _downscale_for_vlm(raw_bytes, int(os.getenv("GROQ_OCR_MAX_DIM", "1280") or "1280"))
    b64 = base64.b64encode(img_bytes).decode()
    data_url = f"data:image/jpeg;base64,{b64}"
    print(f"[ocr] groq sending {len(img_bytes)//1024}KB image to {model}")

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
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
                    "max_tokens": 512,
                },
            )
        if resp.status_code != 200:
            print(f"[ocr] groq HTTP {resp.status_code}: {resp.text[:200]}")
            return {"error": f"Groq HTTP {resp.status_code}", "items": []}
        text = resp.json()["choices"][0]["message"]["content"].strip()
        items = _lines_to_items(text)
        print(f"[ocr] groq ({model}): {len(items)} items")
        return {"raw_text": text, "items": items}
    except Exception as e:
        print(f"[ocr] groq error: {e}")
        return {"error": f"Groq error: {e}", "items": []}


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
    groq_key = os.getenv("GROQ_API_KEY")
    host = os.getenv("OLLAMA_HOST")

    # ── Explicit single-backend modes ───────────────────────────────────────
    if backend == "groq":
        if not groq_key:
            return {"error": "OCR_BACKEND=groq but GROQ_API_KEY is not set", "items": []}
        return await _extract_groq(raw_bytes, groq_key)
    if backend in ("ollama", "vlm"):
        if not host:
            return {"error": "OCR_BACKEND=ollama but OLLAMA_HOST is not set", "items": []}
        return await _extract_vlm(raw_bytes, host)
    if backend == "tesseract":
        return await asyncio.to_thread(_extract_tesseract, raw_bytes)

    # ── auto: Groq → local VLM → Tesseract ───────────────────────────────────
    if groq_key:
        result = await _extract_groq(raw_bytes, groq_key)
        if result.get("items"):
            return result
        print("[ocr] groq returned nothing → trying next backend")
    if host:
        result = await _extract_vlm(raw_bytes, host)
        if result.get("items"):
            return result
        print("[ocr] vlm returned nothing → falling back to Tesseract")

    # Tesseract path (sync, CPU-bound → offload so we don't block the event loop).
    return await asyncio.to_thread(_extract_tesseract, raw_bytes)
