"""backend/ocr.py - Tesseract OCR pipeline for grocery list images.

Source: scan2order2/server.py (OCR section).

Ported verbatim; only the module-level setup and exports differ.

Public API:
    OCR_AVAILABLE: bool
    OCR_ERROR: str
    run_ocr(raw_bytes: bytes) -> {"raw_text": str, "items": list[str]}
"""

import io
import os
import re
import sys

# ── Tesseract setup ─────────────────────────────────────────────

OCR_AVAILABLE = False
OCR_ERROR = ""

try:
    import pytesseract
    from PIL import Image, ImageOps

    # Windows: auto-detect common Tesseract install paths.
    if sys.platform == "win32":
        _TESSERACT_PATHS = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            r"C:\Users\{}\AppData\Local\Programs\Tesseract-OCR\tesseract.exe".format(
                os.getenv("USERNAME", "")
            ),
        ]
        for _path in _TESSERACT_PATHS:
            if os.path.exists(_path):
                pytesseract.pytesseract.tesseract_cmd = _path
                break

    # Smoke-test to verify Tesseract binary is reachable.
    pytesseract.get_tesseract_version()
    OCR_AVAILABLE = True
    print("[ocr] Tesseract available")

except ImportError as e:
    OCR_ERROR = f"pytesseract or Pillow not installed: {e}"
    print(f"[ocr] {OCR_ERROR}")
except Exception as e:
    OCR_ERROR = f"Tesseract not found or not working: {e}"
    print(f"[ocr] {OCR_ERROR}")


# ── OCR helpers ─────────────────────────────────────────────────

_OCR_SECTION_HEADERS = {
    "shopping list", "grocery list", "list",
    "vegetables", "fruits", "dairy", "meat", "snacks",
    "beverages", "drinks", "produce", "grains", "spices",
    "shopping", "groceries", "items", "pantry", "frozen",
}


def _clean_ocr_line(line: str) -> str:
    """Clean a single line from Tesseract output."""
    line = line.strip()
    # Strip leading junk (bullets often misread as =, -, _, ~, €, etc.)
    while line and not line[0].isalnum():
        line = line[1:].strip()
    # Strip trailing junk (lines like "Tomatoes =")
    while line and not line[-1].isalnum():
        line = line[:-1].strip()
    # Strip leading "1." / "1)" / "(1)" numbering
    line = re.sub(r"^\(?[0-9]+[.\)]\s*", "", line).strip()
    # Strip a leading single-letter "word" followed by a capital letter
    # (OCR often reads bullet marks as 'a', 'o', 'e', 'c', etc.)
    line = re.sub(r"^[a-z]\s+(?=[A-Z])", "", line)
    # Fix stray quote chars stuck inside words: E "ggplants -> Eggplants
    line = re.sub(
        r"([A-Za-z])\s*['\"‘’“”]\s*([A-Za-z])",
        r"\1\2",
        line,
    )
    return re.sub(r"\s+", " ", line).strip()


def _is_header_line(line: str) -> bool:
    """Detect section headers so we can skip them."""
    if line.endswith(":"):
        return True
    low = line.lower().rstrip(":.").strip()
    return low in _OCR_SECTION_HEADERS


def _preprocess_image(raw_bytes: bytes):
    """Image preprocessing that makes Tesseract dramatically more accurate."""
    img = Image.open(io.BytesIO(raw_bytes))
    # Honor phone EXIF rotation tag so sideways photos work.
    transposed = ImageOps.exif_transpose(img)
    if transposed is not None:
        img = transposed
    img = img.convert("L")             # grayscale
    img = ImageOps.autocontrast(img, cutoff=3)  # stretch brightness range
    # Upscale small images - Tesseract works best around 2000px.
    w, h = img.size
    if max(w, h) < 2000:
        try:
            resample = Image.Resampling.LANCZOS  # Pillow >= 9.1
        except AttributeError:
            resample = Image.LANCZOS  # type: ignore[attr-defined]
        img = img.resize((w * 2, h * 2), resample)
    return img


def run_ocr(raw_bytes: bytes) -> dict:
    """Extract grocery list items from image bytes via Tesseract.

    Args:
        raw_bytes: raw image file content (JPEG, PNG, etc.)

    Returns:
        {"raw_text": str, "items": list[str]}  on success
        {"error": str, "items": []}             if OCR unavailable or failed
    """
    if not OCR_AVAILABLE:
        return {"error": OCR_ERROR or "OCR not available", "items": []}

    try:
        img = _preprocess_image(raw_bytes)
        # --psm 6 = "assume a uniform block of text" (ideal for lists)
        text = pytesseract.image_to_string(img, lang="eng", config="--psm 6")

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

        print(f"[ocr] {len(items)} items from {len(text.split(chr(10)))} lines")
        return {"raw_text": text, "items": items}

    except Exception as e:
        print(f"[ocr] error: {e}")
        return {"error": str(e), "items": []}
