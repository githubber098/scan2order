"""
auth.py — Authentication helpers for scan2order.

Auth model: phone number + SMS OTP (no passwords).
Session tokens: HMAC-SHA256 signed JSON blob, 6-day TTL, URL-safe base64.

Session cookie name: scan2order_session
Secret key source:   SECRET_KEY env var (REQUIRED in production).
                     Falls back to a per-process random key with a loud warning.
"""

import base64
import hmac
import json
import os
import random
import re
import secrets
import time

_SECRET_KEY: str = os.environ.get("SECRET_KEY", "")
if not _SECRET_KEY:
    _SECRET_KEY = secrets.token_hex(32)
    print(
        "[auth] WARNING: SECRET_KEY env var not set. "
        "A random key is being used — all sessions will be invalidated on restart. "
        "Add SECRET_KEY=<hex-string> to your .env file."
    )

SESSION_TTL = 6 * 24 * 3600   # 6 days
COOKIE_NAME = "scan2order_session"
OTP_TTL     = 10 * 60          # 10 minutes
OTP_RATE    = 60               # seconds between OTP requests per phone


# ── OTP generation ────────────────────────────────────────────────────────────

def generate_otp() -> str:
    """Return a cryptographically random 6-digit code string."""
    return f"{secrets.randbelow(1_000_000):06d}"


# ── Phone normalisation ───────────────────────────────────────────────────────

def normalize_phone(raw: str) -> str | None:
    """Validate and normalise a phone number to E.164 format.

    Accepts Indian numbers (10 digits, with or without +91/0 prefix) and any
    other number already in E.164 (+XXXXXXXXXXX).

    Returns None if the input cannot be parsed as a valid number.
    """
    s = re.sub(r"[\s\-().]+", "", (raw or "").strip())

    if s.startswith("+"):
        # Already in E.164 — accept if 8–15 digits after the +
        if re.fullmatch(r"\+\d{8,15}", s):
            return s
        return None

    # Strip leading zero (common in India: 09876543210)
    if s.startswith("0"):
        s = s[1:]

    # Bare 10-digit Indian mobile number
    if re.fullmatch(r"[6-9]\d{9}", s):
        return f"+91{s}"

    # 12-digit with leading 91 (country code without +)
    if re.fullmatch(r"91[6-9]\d{9}", s):
        return f"+{s}"

    return None


# ── Session tokens ────────────────────────────────────────────────────────────

def create_session_token(user_id: str) -> str:
    """Create a signed, expiring session token for *user_id*.

    Structure: base64url(payload.hex-hmac)
    Payload:   {"uid": "...", "exp": <unix-timestamp>}
    """
    payload = json.dumps(
        {"uid": user_id, "exp": time.time() + SESSION_TTL},
        separators=(",", ":"),
    )
    sig = hmac.new(_SECRET_KEY.encode(), payload.encode(), "sha256").hexdigest()
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def verify_session_token(token: str) -> str | None:
    """Verify *token* and return the user_id, or None if invalid / expired."""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        dot = raw.rfind(".")
        if dot == -1:
            return None
        payload, sig = raw[:dot], raw[dot + 1:]
        expected = hmac.new(_SECRET_KEY.encode(), payload.encode(), "sha256").hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return data.get("uid")
    except Exception:
        return None
