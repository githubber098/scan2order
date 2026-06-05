"""
test_groq_keys_live.py — Live connectivity check for every configured Groq API key.

Sends a minimal chat-completion request (max_tokens=1) to each key and reports:
  200      → key is working
  429      → valid key but daily quota exhausted (warn, don't fail)
  401/403  → key is invalid or revoked (test FAILS)
  400      → key rejected (org restricted, plan issue, etc.) (test FAILS)
  other    → unexpected error (test FAILS)
  network  → connection error (test FAILS)

Skipped automatically when no GROQ_API_KEY is configured in the environment.

Run inside Docker:
    docker compose exec scan2order python -m pytest tests/test_groq_keys_live.py -v -s
"""

import os
import re
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr

pytestmark = pytest.mark.asyncio

_SEP  = "─" * 52
_SEP2 = "═" * 52


def _short_reason(status: int, body: str) -> str:
    """Extract a short human-readable reason from a Groq error response body."""
    m = re.search(r'"message"\s*:\s*"([^"]{0,120})"', body)
    if m:
        return m.group(1)
    return body[:100].strip()


@pytest.fixture(scope="module")
def groq_keys():
    keys = ocr._groq_keys()
    if not keys:
        pytest.skip("No GROQ_API_KEY configured — skipping live Groq key tests")
    return keys


class TestGroqKeysLive:
    """Live smoke-test: ping each configured Groq key with a 1-token request."""

    async def test_each_key_reachable(self, groq_keys):
        model = os.getenv("GROQ_OCR_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

        # ── Header ────────────────────────────────────────────────────────────
        print(f"\n{_SEP2}")
        print(f"  Groq live key test")
        print(f"  Model   : {model}")
        print(f"  Keys    : {len(groq_keys)} configured")
        print(f"{_SEP2}\n")

        statuses: list[dict] = []   # {label, suffix, status, reason, ok, warn}

        async with httpx.AsyncClient(timeout=20.0) as client:
            for i, key in enumerate(groq_keys, 1):
                suffix = key[-6:]
                label  = f"key {i}"
                print(f"  Testing {label}  (...{suffix}) … ", end="", flush=True)

                entry = {"label": label, "suffix": suffix}

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
                    entry.update(status="network error", reason=str(exc), ok=False, warn=False)
                    print(f"✗  network error")
                    statuses.append(entry)
                    continue

                code = resp.status_code
                body = resp.text

                if code == 200:
                    entry.update(status="working", reason="", ok=True, warn=False)
                    print("✓")
                elif code == 429:
                    reason = _short_reason(code, body)
                    entry.update(status="quota exhausted", reason=reason, ok=True, warn=True)
                    print(f"⚠  quota exhausted")
                elif code in (400, 401, 403):
                    reason = _short_reason(code, body)
                    entry.update(status="not working", reason=reason, ok=False, warn=False)
                    print(f"✗  HTTP {code}")
                else:
                    reason = _short_reason(code, body)
                    entry.update(status="not working", reason=f"HTTP {code} — {reason}", ok=False, warn=False)
                    print(f"✗  HTTP {code}")

                statuses.append(entry)

        # ── Summary table ─────────────────────────────────────────────────────
        print(f"\n{_SEP}")
        print("  Summary")
        print(_SEP)
        for e in statuses:
            icon   = "⚠" if e["warn"] else ("✓" if e["ok"] else "✗")
            status = e["status"]
            suffix = e["suffix"]
            reason = f"  →  {e['reason']}" if e["reason"] else ""
            print(f"  {e['label']}  (...{suffix})  =  {status} {icon}{reason}")
        print(_SEP)

        working  = sum(1 for e in statuses if e["ok"] and not e["warn"])
        limited  = sum(1 for e in statuses if e["warn"])
        broken   = sum(1 for e in statuses if not e["ok"])
        parts = []
        if working: parts.append(f"{working} working")
        if limited: parts.append(f"{limited} quota-limited")
        if broken:  parts.append(f"{broken} broken")
        print(f"\n  {' · '.join(parts)}\n")

        # ── Fail if any key is hard-broken ────────────────────────────────────
        bad = [e for e in statuses if not e["ok"]]
        if bad:
            pytest.fail(
                f"{len(bad)} key(s) are not working:\n"
                + "\n".join(f"  {e['label']} (...{e['suffix']}): {e['reason']}" for e in bad)
            )
