"""
test_groq_keys_live.py - Live connectivity check for configured Groq API keys.

The key liveness gate is GET /openai/v1/models, which authenticates the key
without depending on a particular chat/OCR model. After that, the test runs an
advisory 1-token chat probe so model-specific permissions or quota issues are
visible in the output.

Status handling:
  models 200       -> key/org is accepted
  models 401/403   -> key is invalid/revoked or lacks access (test FAILS)
  models 400       -> key/org is rejected (test FAILS)
  chat 200         -> chat probe works
  chat 429         -> valid key but quota exhausted (warn, don't fail)
  chat 400/403     -> model/request rejected after auth passed (warn by default)
  other/network    -> unexpected key-liveness failure (test FAILS)

Optional env vars:
  GROQ_KEY_TEST_MODEL        chat probe model (default: llama-3.1-8b-instant)
  GROQ_KEY_TEST_STRICT_CHAT  set to 1 to fail on chat-probe warnings

Run inside Docker:
    docker compose exec scan2order python -m pytest tests/test_groq_keys_live.py -v -s
"""

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocr

pytestmark = pytest.mark.asyncio

_SEP = "-" * 52
_SEP2 = "=" * 52
_GROQ_MODELS_URL = ocr._GROQ_URL.rsplit("/", 2)[0] + "/models"
_DEFAULT_TEST_MODEL = "llama-3.1-8b-instant"


def _strict_chat() -> bool:
    return os.getenv("GROQ_KEY_TEST_STRICT_CHAT", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _short_reason(body: str) -> str:
    """Extract a short human-readable reason from a Groq error response body."""
    try:
        data = json.loads(body)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            msg = str(err.get("message") or "").strip()
            details = []
            if err.get("type"):
                details.append(f"type={err['type']}")
            if err.get("code"):
                details.append(f"code={err['code']}")
            if details:
                return f"{msg} ({', '.join(details)})" if msg else ", ".join(details)
            if msg:
                return msg
    except Exception:
        pass
    return body[:160].strip()


@pytest.fixture(scope="module")
def groq_keys():
    keys = ocr._groq_keys()
    if not keys:
        pytest.skip("No GROQ_API_KEY configured - skipping live Groq key tests")
    return keys


class TestGroqKeysLive:
    """Live smoke-test: validate each configured key and report chat readiness."""

    async def test_each_key_reachable(self, groq_keys):
        probe_model = os.getenv("GROQ_KEY_TEST_MODEL", _DEFAULT_TEST_MODEL)
        ocr_model = os.getenv("GROQ_OCR_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        strict_chat = _strict_chat()

        print(f"\n{_SEP2}")
        print("  Groq live key test")
        print(f"  Models endpoint : {_GROQ_MODELS_URL}")
        print(f"  Chat probe model: {probe_model}")
        print(f"  OCR model       : {ocr_model}")
        print(f"  Strict chat     : {'yes' if strict_chat else 'no'}")
        print(f"  Keys            : {len(groq_keys)} configured")
        print(f"{_SEP2}\n")

        statuses: list[dict] = []  # {label, suffix, status, reason, ok, warn}

        async with httpx.AsyncClient(timeout=20.0) as client:
            for i, key in enumerate(groq_keys, 1):
                suffix = key[-6:]
                label = f"key {i}"
                print(f"  Testing {label}  (...{suffix}) ... ", end="", flush=True)

                entry = {"label": label, "suffix": suffix}

                try:
                    model_resp = await client.get(
                        _GROQ_MODELS_URL,
                        headers={"Authorization": f"Bearer {key}"},
                    )
                except Exception as exc:
                    entry.update(
                        status="network error",
                        reason=f"models endpoint: {exc}",
                        ok=False,
                        warn=False,
                    )
                    print("x  models network error")
                    statuses.append(entry)
                    continue

                if model_resp.status_code != 200:
                    reason = _short_reason(model_resp.text)
                    entry.update(
                        status="auth rejected",
                        reason=f"models HTTP {model_resp.status_code}: {reason}",
                        ok=False,
                        warn=False,
                    )
                    print(f"x  models HTTP {model_resp.status_code}")
                    statuses.append(entry)
                    continue

                try:
                    chat_resp = await client.post(
                        ocr._GROQ_URL,
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": probe_model,
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_completion_tokens": 1,
                        },
                    )
                except Exception as exc:
                    entry.update(
                        status="chat network warning",
                        reason=f"models ok; chat probe network error: {exc}",
                        ok=not strict_chat,
                        warn=not strict_chat,
                    )
                    print("!  models ok; chat network error")
                    statuses.append(entry)
                    continue

                code = chat_resp.status_code
                body = chat_resp.text

                if code == 200:
                    entry.update(status="working", reason="", ok=True, warn=False)
                    print("ok")
                elif code == 429:
                    reason = _short_reason(body)
                    entry.update(
                        status="quota exhausted",
                        reason=f"models ok; chat HTTP 429: {reason}",
                        ok=True,
                        warn=True,
                    )
                    print("!  models ok; quota exhausted")
                elif code in (400, 403):
                    reason = _short_reason(body)
                    entry.update(
                        status="chat probe rejected",
                        reason=f"models ok; {probe_model} HTTP {code}: {reason}",
                        ok=not strict_chat,
                        warn=not strict_chat,
                    )
                    print(f"!  models ok; chat HTTP {code}")
                elif code == 401:
                    reason = _short_reason(body)
                    entry.update(
                        status="chat auth rejected",
                        reason=f"models ok; chat HTTP 401: {reason}",
                        ok=False,
                        warn=False,
                    )
                    print("x  models ok; chat HTTP 401")
                else:
                    reason = _short_reason(body)
                    entry.update(
                        status="chat error",
                        reason=f"models ok; chat HTTP {code}: {reason}",
                        ok=False,
                        warn=False,
                    )
                    print(f"x  models ok; chat HTTP {code}")

                statuses.append(entry)

        print(f"\n{_SEP}")
        print("  Summary")
        print(_SEP)
        for e in statuses:
            icon = "!" if e["warn"] else ("ok" if e["ok"] else "x")
            reason = f"  ->  {e['reason']}" if e["reason"] else ""
            print(f"  {e['label']}  (...{e['suffix']})  =  {e['status']} {icon}{reason}")
        print(_SEP)

        working = sum(1 for e in statuses if e["ok"] and not e["warn"])
        limited = sum(1 for e in statuses if e["warn"])
        broken = sum(1 for e in statuses if not e["ok"])
        parts = []
        if working:
            parts.append(f"{working} working")
        if limited:
            parts.append(f"{limited} warning")
        if broken:
            parts.append(f"{broken} broken")
        print(f"\n  {' / '.join(parts)}\n")

        bad = [e for e in statuses if not e["ok"]]
        if bad:
            pytest.fail(
                f"{len(bad)} key(s) failed the model-agnostic liveness check:\n"
                + "\n".join(
                    f"  {e['label']} (...{e['suffix']}): {e['reason']}" for e in bad
                )
            )
