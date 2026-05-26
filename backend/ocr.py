"""ocr.py - Tesseract OCR for grocery list extraction.

Ported verbatim from scan2order2/server.py OCR section.
Windows path auto-detection preserved.
"""

import io
import os
import platform
import re

OCR_AVAILABLE = False
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
    print(f"[ocr] OCR disabled - {OCR_ERROR}")
except Exception as e:
    OCR_ERROR = (
        f"Tesseract binary not callable ({e}). "
        f"Install from https://github.com/UB-Mannheim/tesseract/wiki"
    )
    print(f"[ocr] OCR disabled - {OCR_ERROR}")


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


def extract_grocery_list(raw_bytes: bytes) -> dict:
    """Extract grocery list items from image bytes via Tesseract.

    Returns {"raw_text": str, "items": [str]} on success,
    or {"error": str, "items": []} on failure.
    """
    if not OCR_AVAILABLE:
        return {"error": OCR_ERROR or "OCR not available", "items": []}

    try:
        img = _preprocess_image(raw_bytes)
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
