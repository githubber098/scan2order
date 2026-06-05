"""
test_groq_rotation.py — multi-key Groq failover + OCR context-correction.

  - _groq_keys()  parses comma/numbered keys, de-duped, in order
  - _groq_chat()  rotates to the next key on HTTP 429 / 401 / 403 and
                  key-specific restricted-org 400s, remembers the working key,
                  and errors only when all keys are exhausted or a non-key
                  failure occurs
  - _correct_items()  fixes misreads (Green Yogurt → Greek Yogurt) + drops
                  accidental duplicates, and falls back to the raw list on a
                  degenerate or failed correction
"""

import httpx
import pytest

import ocr


# ── Fake httpx plumbing (no real network) ─────────────────────────────────────

class _FakeResp:
    def __init__(self, status, content="", text=""):
        self.status_code = status
        self._content = content
        self.text = text

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    """Stands in for httpx.AsyncClient; routes .post() to a handler callback."""
    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        return self._handler(headers or {}, json or {})


def _install_fake_httpx(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(handler))


# ── _groq_keys parsing ─────────────────────────────────────────────────────────

def _clear_numbered_groq_keys(monkeypatch):
    for i in range(1, 8):
        monkeypatch.delenv(f"GROQ_API_KEY_{i}", raising=False)


class TestGroqKeys:
    def test_comma_separated(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "k1, k2 ,k3")
        _clear_numbered_groq_keys(monkeypatch)
        assert ocr._groq_keys() == ["k1", "k2", "k3"]

    def test_numbered_keys_appended(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "k1")
        monkeypatch.delenv("GROQ_API_KEY_1", raising=False)
        monkeypatch.setenv("GROQ_API_KEY_2", "k2")
        monkeypatch.setenv("GROQ_API_KEY_3", "k3")
        monkeypatch.delenv("GROQ_API_KEY_4", raising=False)
        assert ocr._groq_keys() == ["k1", "k2", "k3"]

    def test_numbered_keys_can_start_at_one_without_base(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEY_1", "k1")
        monkeypatch.setenv("GROQ_API_KEY_2", "k2")
        monkeypatch.delenv("GROQ_API_KEY_3", raising=False)
        assert ocr._groq_keys() == ["k1", "k2"]

    def test_dedup_preserves_order(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "k1,k2,k1")
        monkeypatch.setenv("GROQ_API_KEY_1", "k1")
        monkeypatch.delenv("GROQ_API_KEY_2", raising=False)
        assert ocr._groq_keys() == ["k1", "k2"]

    def test_empty(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        _clear_numbered_groq_keys(monkeypatch)
        assert ocr._groq_keys() == []


# ── _groq_chat key rotation ──────────────────────────────────────────────────

class TestGroqRotation:
    pytestmark = pytest.mark.asyncio

    async def test_rotates_past_exhausted_key(self, monkeypatch):
        ocr._groq_key_idx = 0
        calls = []

        def handler(headers, body):
            auth = headers.get("Authorization", "")
            calls.append(auth)
            if auth == "Bearer k1":
                return _FakeResp(429, text="rate limit exceeded")   # exhausted
            return _FakeResp(200, content="ok")
        _install_fake_httpx(monkeypatch, handler)

        text, err = await ocr._groq_chat([{"role": "user", "content": "hi"}],
                                         ["k1", "k2"], model="m", max_tokens=10)
        assert text == "ok" and err == ""
        assert calls == ["Bearer k1", "Bearer k2"]   # tried k1, rotated to k2
        assert ocr._groq_key_idx == 1                 # remembers the working key

    async def test_remembered_key_used_first_next_time(self, monkeypatch):
        ocr._groq_key_idx = 1            # pretend k2 is the known-good key
        seen = []

        def handler(headers, body):
            seen.append(headers.get("Authorization", ""))
            return _FakeResp(200, content="x")
        _install_fake_httpx(monkeypatch, handler)

        await ocr._groq_chat([{"role": "user", "content": "hi"}],
                             ["k1", "k2"], model="m", max_tokens=10)
        assert seen[0] == "Bearer k2"     # started at the remembered index

    async def test_all_keys_exhausted_returns_error(self, monkeypatch):
        ocr._groq_key_idx = 0

        def handler(headers, body):
            return _FakeResp(429, text="rate limit")
        _install_fake_httpx(monkeypatch, handler)

        text, err = await ocr._groq_chat([{"role": "user", "content": "hi"}],
                                         ["k1", "k2"], model="m", max_tokens=10)
        assert text is None
        assert "429" in err or "exhausted" in err.lower()

    async def test_restricted_org_400_rotates_to_next_key(self, monkeypatch):
        ocr._groq_key_idx = 0
        calls = []

        def handler(headers, body):
            auth = headers.get("Authorization", "")
            calls.append(auth)
            if auth == "Bearer k1":
                return _FakeResp(
                    400,
                    text='{"error":{"message":"Organization has been restricted."}}',
                )
            return _FakeResp(200, content="ok")
        _install_fake_httpx(monkeypatch, handler)

        text, err = await ocr._groq_chat([{"role": "user", "content": "hi"}],
                                         ["k1", "k2"], model="m", max_tokens=10)
        assert text == "ok" and err == ""
        assert calls == ["Bearer k1", "Bearer k2"]
        assert ocr._groq_key_idx == 1

    async def test_generic_400_does_not_rotate(self, monkeypatch):
        ocr._groq_key_idx = 0
        calls = []

        def handler(headers, body):
            calls.append(headers.get("Authorization"))
            return _FakeResp(400, text="bad request")
        _install_fake_httpx(monkeypatch, handler)

        text, err = await ocr._groq_chat([{"role": "user", "content": "hi"}],
                                         ["k1", "k2"], model="m", max_tokens=10)
        assert text is None
        assert "400" in err
        assert len(calls) == 1

    async def test_non_quota_error_does_not_rotate(self, monkeypatch):
        ocr._groq_key_idx = 0
        calls = []

        def handler(headers, body):
            calls.append(headers.get("Authorization"))
            return _FakeResp(500, text="server error")
        _install_fake_httpx(monkeypatch, handler)

        text, err = await ocr._groq_chat([{"role": "user", "content": "hi"}],
                                         ["k1", "k2"], model="m", max_tokens=10)
        assert text is None
        assert len(calls) == 1            # 5xx is not key-specific → no rotation

    async def test_extract_groq_uses_rotation(self, monkeypatch):
        # End-to-end: first key 429s, second returns a list → items parsed.
        ocr._groq_key_idx = 0

        def handler(headers, body):
            if headers.get("Authorization") == "Bearer k1":
                return _FakeResp(429, text="rate limit")
            return _FakeResp(200, content="milk\neggs")
        _install_fake_httpx(monkeypatch, handler)

        out = await ocr._extract_groq(b"img", ["k1", "k2"])
        assert out["items"] == ["milk", "eggs"]


# ── Context correction ────────────────────────────────────────────────────────

class TestContextCorrection:
    pytestmark = pytest.mark.asyncio

    async def test_fixes_misread_and_drops_duplicate(self, monkeypatch):
        monkeypatch.setenv("OCR_CONTEXT_CORRECTION", "auto")
        raw = ["mango 500 gm", "Dates 250 gm", "Tomato 1 kg",
               "Green Yogurt", "Green Yogurt", "The whole Truth whey isolate 1 kg"]
        corrected = ("mango 500 gm\nDates 250 gm\nTomato 1 kg\n"
                     "Greek Yogurt\nThe Whole Truth Whey Isolate 1 kg")

        async def fake_chat(messages, keys, **kw):
            return corrected, ""
        monkeypatch.setattr(ocr, "_groq_chat", fake_chat)

        out = await ocr._correct_items(raw, ["k1"], None)
        assert "Greek Yogurt" in out
        assert "Green Yogurt" not in out
        assert out.count("Greek Yogurt") == 1          # duplicate collapsed

    async def test_disabled_returns_raw(self, monkeypatch):
        monkeypatch.setenv("OCR_CONTEXT_CORRECTION", "0")

        async def fake_chat(*a, **k):
            raise AssertionError("correction must not run when disabled")
        monkeypatch.setattr(ocr, "_groq_chat", fake_chat)

        raw = ["Green Yogurt"]
        assert await ocr._correct_items(raw, ["k1"], None) == raw

    async def test_degenerate_correction_rejected(self, monkeypatch):
        monkeypatch.setenv("OCR_CONTEXT_CORRECTION", "auto")
        raw = ["milk", "eggs", "bread", "butter", "rice", "atta"]

        async def fake_chat(messages, keys, **kw):
            return "milk", ""        # correction gutted the list → reject
        monkeypatch.setattr(ocr, "_groq_chat", fake_chat)

        assert await ocr._correct_items(raw, ["k1"], None) == raw

    async def test_no_keys_no_host_returns_raw(self, monkeypatch):
        monkeypatch.setenv("OCR_CONTEXT_CORRECTION", "auto")
        raw = ["Green Yogurt"]
        assert await ocr._correct_items(raw, [], None) == raw

    async def test_failed_correction_falls_back_to_raw(self, monkeypatch):
        monkeypatch.setenv("OCR_CONTEXT_CORRECTION", "auto")

        async def fake_chat(messages, keys, **kw):
            return None, "all keys exhausted"
        monkeypatch.setattr(ocr, "_groq_chat", fake_chat)

        raw = ["mango 500 gm", "Greek Yogurt"]
        assert await ocr._correct_items(raw, ["k1"], None) == raw
