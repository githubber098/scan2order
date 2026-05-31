"""ocr.py - Grocery list extraction from images.

Two backends, selected by the OCR_BACKEND env var:

  "auto" (default)  Try the local vision LLM (Gemma 4 via Ollama) first; if it
                    errors or returns nothing, fall back to Tesseract.
  "ollama" / "vlm"  Vision LLM only.
  "tesseract"       Tesseract only (the legacy path).

The vision LLM reads handwriting far better than Tesseract. It reuses the same
Ollama instance and OLLAMA_MODEL used by the ranker, so one model
(default gemma4:e2b) serves both OCR and ranking — nothing extra to run.

Env vars:
  OCR_BACKEND        auto | ollama | tesseract   (default: auto)
  OLLAMA_HOST        e.g. http://ollama:11434     (required for the VLM path)
  OCR_VISION_MODEL   overrides the OCR model only (default: OLLAMA_MODEL or gemma4:e2b)
  OLLAMA_MODEL       shared model name            (default: gemma4:e2b)
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
            or "gemma4:e2b")


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
    # Smaller image = far less image-token prefill = much faster on CPU. 900px
    # keeps a grocery list legible. max_tokens kept tight (a list is short).
    # Both tunable; drop OCR_MAX_DIM toward 768 for more speed at some accuracy.
    max_dim = int(os.getenv("OCR_MAX_DIM", "900") or "900")
    max_tokens = int(os.getenv("OCR_MAX_TOKENS", "256") or "256")
    timeout_s = float(os.getenv("OCR_TIMEOUT", "60") or "60")
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


# ── Dispatcher ───────────────────────────────────────────────────────────────

async def extract_grocery_list(raw_bytes: bytes) -> dict:
    """Extract grocery list items from image bytes.

    Backend chosen by OCR_BACKEND (auto | ollama | tesseract). In "auto" mode
    the vision LLM is tried first and Tesseract is the fallback. Returns
    {"raw_text": str, "items": [str]} or {"error": str, "items": []}.
    """
    backend = os.getenv("OCR_BACKEND", "auto").strip().lower()
    host = os.getenv("OLLAMA_HOST")

    want_vlm = backend in ("auto", "ollama", "vlm")
    if want_vlm and host:
        result = await _extract_vlm(raw_bytes, host)
        if result.get("items"):
            return result
        if backend in ("ollama", "vlm"):
            # VLM-only mode: return its result (possibly an error/empty) as-is.
            return result
        print("[ocr] vlm returned nothing → falling back to Tesseract")
    elif want_vlm and not host:
        if backend in ("ollama", "vlm"):
            return {"error": "OCR_BACKEND=ollama but OLLAMA_HOST is not set", "items": []}
        # auto mode with no Ollama configured → silently use Tesseract.

    # Tesseract path (sync, CPU-bound → offload so we don't block the event loop).
    return await asyncio.to_thread(_extract_tesseract, raw_bytes)
