"""
test_groq_keys_live.py — Live connectivity check for every configured Groq API key.

Sends a minimal chat-completion request (max_tokens=1) to each key and reports:
  200  → key is working
  429  → key is valid but daily quota exhausted (warn, don't fail)
  401/403 → key is invalid or revoked (test FAILS)
  network error → test FAILS

Skipped automatically when no GROQ_API_KEY is configured in the environment,
so it never blocks CI where keys aren't present.

Run manually:
    GROQ_API_KEY="gsk_key1,gsk_key2" pytest backend/tests/test_groq_keys_live.py -v -s
"""

import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def groq_keys():
    keys = ocr._groq_keys()
    if not keys:
        pytest.skip("No GROQ_API_KEY configured — skipping live Groq key tests")
    return keys


class TestGroqKeysLive:
    """Live smoke-test: ping each configured Groq key with a 1-token request."""

    async def test_each_key_reachable(self, groq_keys):
        model = os.getenv(
            "GROQ_OCR_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"
        )
        results: dict[str, str] = {}
        broken: dict[str, str] = {}

        async with httpx.AsyncClient(timeout=20.0) as client:
            for i, key in enumerate(groq_keys, 1):
                label = f"key #{i} (...{key[-6:]})"
                try:
                    resp = await client.post(
                        ocr._GROQ_URL,
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_tokens": 1,
                            "temperature": 0,
                        },
                    )
                except Exception as exc:
                    msg = f"network error: {exc}"
                    results[label] = msg
                    broken[label] = msg
                    continue

                if resp.status_code == 200:
                    results[label] = "✓ working"
                elif resp.status_code == 429:
                    # Valid key but daily quota exhausted — warn only, don't fail.
                    body = resp.text[:120]
                    results[label] = f"⚠ rate-limited / quota exhausted: {body}"
                elif resp.status_code in (401, 403):
                    msg = f"✗ auth error HTTP {resp.status_code} — key invalid or revoked: {resp.text[:120]}"
                    results[label] = msg
                    broken[label] = msg
                else:
                    msg = f"✗ unexpected HTTP {resp.status_code}: {resp.text[:120]}"
                    results[label] = msg
                    broken[label] = msg

        # Always print the full result table so it shows up with -s
        print(f"\n\nGroq key check ({len(groq_keys)} key(s), model={model}):")
        for label, result in results.items():
            print(f"  {label}: {result}")

        if broken:
            pytest.fail(
                f"{len(broken)} key(s) are broken:\n"
                + "\n".join(f"  {k}: {v}" for k, v in broken.items())
            )
