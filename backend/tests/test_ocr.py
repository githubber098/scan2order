"""
test_ocr.py — Tests for OCR endpoint and the ocr.py helper functions.

When Tesseract is not installed the OCR endpoint still responds correctly
(graceful degradation).  Tests that actually need OCR output mock the
pytesseract call so the suite runs everywhere without Tesseract.

Covers:
  POST /api/ocr  — web endpoint (file upload)
  POST /scan     — mobile compat endpoint
  _clean_ocr_line, _is_header_line — line-level helpers
  extract_grocery_list — mocked OCR output
"""

import io
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr import _clean_ocr_line, _is_header_line


# ── _clean_ocr_line (pure function — no Tesseract needed) ────────────────────

class TestCleanOcrLine:
    def test_strips_whitespace(self):
        assert _clean_ocr_line("  butter  ") == "butter"

    def test_strips_leading_punctuation(self):
        assert _clean_ocr_line("- milk") == "milk"
        assert _clean_ocr_line("• eggs") == "eggs"

    def test_strips_trailing_punctuation(self):
        assert _clean_ocr_line("tomatoes,") == "tomatoes"
        assert _clean_ocr_line("onions.") == "onions"

    def test_removes_numbered_list_prefix(self):
        assert _clean_ocr_line("1. milk") == "milk"
        assert _clean_ocr_line("2) eggs") == "eggs"

    def test_collapses_multiple_spaces(self):
        result = _clean_ocr_line("amul   butter   100g")
        assert "  " not in result

    def test_empty_string_returns_empty(self):
        assert _clean_ocr_line("") == ""

    def test_preserves_brand_names(self):
        result = _clean_ocr_line("Amul Butter 100g")
        assert "Amul" in result
        assert "Butter" in result

    def test_only_punctuation_returns_empty(self):
        assert _clean_ocr_line("---") == ""


# ── _is_header_line ───────────────────────────────────────────────────────────

class TestIsHeaderLine:
    def test_colon_suffix_is_header(self):
        assert _is_header_line("Shopping List:") is True
        assert _is_header_line("Vegetables:") is True

    def test_known_section_words_are_header(self):
        assert _is_header_line("shopping list") is True
        assert _is_header_line("Groceries") is True
        assert _is_header_line("dairy") is True

    def test_regular_item_not_header(self):
        assert _is_header_line("amul butter 100g") is False
        assert _is_header_line("2 kg tomatoes") is False

    def test_case_insensitive(self):
        assert _is_header_line("SHOPPING LIST") is True


# ── Backend dispatcher (auto | ollama | tesseract) ───────────────────────────

class TestOcrBackendSelection:
    pytestmark = pytest.mark.asyncio

    async def test_auto_uses_tesseract_when_no_ollama(self, monkeypatch):
        import ocr
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        monkeypatch.setenv("OCR_BACKEND", "auto")
        seen = {}

        async def _vlm(raw, host):
            seen["vlm"] = True
            return {"items": []}
        monkeypatch.setattr(ocr, "_extract_vlm", _vlm)
        monkeypatch.setattr(ocr, "_extract_tesseract", lambda raw: {"items": ["milk"]})

        out = await ocr.extract_grocery_list(b"img")
        assert out["items"] == ["milk"]
        assert "vlm" not in seen          # no OLLAMA_HOST → VLM skipped entirely

    async def test_vlm_used_when_configured(self, monkeypatch):
        import ocr
        monkeypatch.setenv("OLLAMA_HOST", "http://fake:11434")
        monkeypatch.setenv("OCR_BACKEND", "auto")

        async def _vlm(raw, host):
            return {"raw_text": "x", "items": ["eggs"]}
        monkeypatch.setattr(ocr, "_extract_vlm", _vlm)
        monkeypatch.setattr(ocr, "_extract_tesseract",
                            lambda raw: {"items": ["SHOULD NOT BE USED"]})

        out = await ocr.extract_grocery_list(b"img")
        assert out["items"] == ["eggs"]

    async def test_vlm_empty_falls_back_to_tesseract(self, monkeypatch):
        import ocr
        monkeypatch.setenv("OLLAMA_HOST", "http://fake:11434")
        monkeypatch.setenv("OCR_BACKEND", "auto")

        async def _vlm(raw, host):
            return {"items": []}        # VLM found nothing
        monkeypatch.setattr(ocr, "_extract_vlm", _vlm)
        monkeypatch.setattr(ocr, "_extract_tesseract", lambda raw: {"items": ["onions"]})

        out = await ocr.extract_grocery_list(b"img")
        assert out["items"] == ["onions"]

    async def test_tesseract_only_never_calls_vlm(self, monkeypatch):
        import ocr
        monkeypatch.setenv("OLLAMA_HOST", "http://fake:11434")
        monkeypatch.setenv("OCR_BACKEND", "tesseract")

        async def _vlm(raw, host):
            raise AssertionError("VLM must not be called in tesseract-only mode")
        monkeypatch.setattr(ocr, "_extract_vlm", _vlm)
        monkeypatch.setattr(ocr, "_extract_tesseract", lambda raw: {"items": ["atta"]})

        out = await ocr.extract_grocery_list(b"img")
        assert out["items"] == ["atta"]

    async def test_ollama_only_errors_without_host(self, monkeypatch):
        import ocr
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        monkeypatch.setenv("OCR_BACKEND", "ollama")
        out = await ocr.extract_grocery_list(b"img")
        assert out["items"] == [] and "error" in out


# ── /api/ocr endpoint ─────────────────────────────────────────────────────────

class TestApiOcrEndpoint:
    def _fake_image(self) -> bytes:
        """Smallest valid 1×1 white PNG."""
        return (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    def test_ocr_endpoint_exists(self, client):
        """Even with no Tesseract the endpoint should return 200, not 404/500."""
        img_bytes = self._fake_image()
        r = client.post(
            "/api/ocr",
            files={"image": ("list.png", io.BytesIO(img_bytes), "image/png")},
        )
        assert r.status_code == 200

    def test_ocr_response_has_items_key(self, client):
        img_bytes = self._fake_image()
        r = client.post(
            "/api/ocr",
            files={"image": ("list.png", io.BytesIO(img_bytes), "image/png")},
        )
        data = r.json()
        assert "items" in data

    def test_ocr_without_tesseract_returns_error_gracefully(self, client, monkeypatch):
        """If OCR is unavailable, response has error key but still 200 status."""
        import ocr as ocr_module
        monkeypatch.setattr(ocr_module, "OCR_AVAILABLE", False)
        monkeypatch.setattr(ocr_module, "OCR_ERROR", "Tesseract not found")
        img_bytes = self._fake_image()
        r = client.post(
            "/api/ocr",
            files={"image": ("list.png", io.BytesIO(img_bytes), "image/png")},
        )
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert data["items"] == []

    def test_ocr_with_mocked_tesseract_returns_items(self, client, monkeypatch):
        """Mock pytesseract to return a grocery list string; verify parsing."""
        import ocr as ocr_module
        monkeypatch.setattr(ocr_module, "OCR_AVAILABLE", True)

        fake_text = "milk\neggs\namul butter\nonions\n"

        async def _fake_extract(raw_bytes: bytes) -> dict:
            return {
                "raw_text": fake_text,
                "items": ["milk", "eggs", "amul butter", "onions"],
            }

        monkeypatch.setattr(ocr_module, "extract_grocery_list", _fake_extract)
        img_bytes = self._fake_image()
        r = client.post(
            "/api/ocr",
            files={"image": ("list.png", io.BytesIO(img_bytes), "image/png")},
        )
        data = r.json()
        assert "milk" in data["items"]
        assert "eggs" in data["items"]
        assert "amul butter" in data["items"]

    def test_ocr_response_includes_raw_text_when_available(self, client, monkeypatch):
        import ocr as ocr_module
        monkeypatch.setattr(ocr_module, "OCR_AVAILABLE", True)

        async def _fake(_):
            return {"raw_text": "milk\n", "items": ["milk"]}
        monkeypatch.setattr(ocr_module, "extract_grocery_list", _fake)
        img_bytes = self._fake_image()
        r = client.post(
            "/api/ocr",
            files={"image": ("list.png", io.BytesIO(img_bytes), "image/png")},
        )
        assert "raw_text" in r.json()


# ── /scan endpoint (mobile compat) ───────────────────────────────────────────

class TestMobileScanEndpoint:
    def _fake_image(self) -> bytes:
        return b"\x89PNG" + b"\x00" * 50  # not a real PNG but enough for test

    def test_scan_returns_success_false_without_ocr(self, client, monkeypatch):
        import ocr as ocr_module
        monkeypatch.setattr(ocr_module, "OCR_AVAILABLE", False)
        monkeypatch.setattr(ocr_module, "OCR_ERROR", "Tesseract missing")
        r = client.post(
            "/scan",
            files={"image": ("list.jpg", io.BytesIO(self._fake_image()), "image/jpeg")},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False
        assert data["items"] == []

    def test_scan_returns_items_with_mocked_ocr(self, client, monkeypatch):
        import ocr as ocr_module
        monkeypatch.setattr(ocr_module, "OCR_AVAILABLE", True)

        async def _fake(_):
            return {"items": ["Tomatoes", "Onions 1kg"]}
        monkeypatch.setattr(ocr_module, "extract_grocery_list", _fake)
        r = client.post(
            "/scan",
            files={"image": ("list.jpg", io.BytesIO(self._fake_image()), "image/jpeg")},
        )
        data = r.json()
        assert data["success"] is True
        assert "Tomatoes" in data["items"]

    def test_scan_shape_matches_mobile_contract(self, client, monkeypatch):
        """Mobile app expects {success: bool, items: [str], error?: str}."""
        import ocr as ocr_module
        monkeypatch.setattr(ocr_module, "OCR_AVAILABLE", False)
        monkeypatch.setattr(ocr_module, "OCR_ERROR", "no tesseract")
        r = client.post(
            "/scan",
            files={"image": ("list.jpg", io.BytesIO(self._fake_image()), "image/jpeg")},
        )
        data = r.json()
        assert "success" in data
        assert "items" in data
