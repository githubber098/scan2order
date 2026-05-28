"""
auth.py — User authentication helpers for scan2order.

Password hashing: PBKDF2-SHA256 with 260 000 iterations (OWASP 2023).
Session tokens:   HMAC-SHA256 signed JSON blob, 6-day TTL, URL-safe base64.

Session cookie name: scan2order_session
Secret key source:   SECRET_KEY env var (REQUIRED in production).
                     Falls back to a per-process random key with a loud warning
                     — sessions are then invalidated on every restart.
"""

import base64
import hashlib
import hmac
import json
import os
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

SESSION_TTL       = 6 * 24 * 3600   # 6 days in seconds
COOKIE_NAME       = "scan2order_session"
_ITERATIONS       = 260_000          # PBKDF2 iterations (OWASP 2023 recommendation)


# ── Password hashing ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Return a storable PBKDF2-SHA256 hash string for *password*.

    Format: ``<base64-salt>$<base64-derived-key>``
    Both components are 32 bytes; the separator ``$`` cannot appear in base64.
    """
    salt = secrets.token_bytes(32)
    dk   = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    """Return True iff *password* matches the stored hash from hash_password()."""
    try:
        salt_b64, dk_b64 = stored.split("$", 1)
        salt       = base64.b64decode(salt_b64)
        stored_dk  = base64.b64decode(dk_b64)
        candidate  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
        return hmac.compare_digest(candidate, stored_dk)
    except Exception:
        return False


# ── Session tokens ────────────────────────────────────────────────────────────

def create_session_token(user_id: str) -> str:
    """Create a signed, expiring session token string for *user_id*.

    Structure (before base64):  ``<json-payload>.<hex-hmac>``
    Payload:                    ``{"uid": "...", "exp": <unix-timestamp>}``
    """
    payload = json.dumps(
        {"uid": user_id, "exp": time.time() + SESSION_TTL},
        separators=(",", ":"),
    )
    sig = hmac.new(_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}.{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_session_token(token: str) -> str | None:
    """Verify *token* and return the user_id, or None if invalid / expired."""
    try:
        raw     = base64.urlsafe_b64decode(token.encode()).decode()
        dot     = raw.rfind(".")
        if dot == -1:
            return None
        payload = raw[:dot]
        sig     = raw[dot + 1:]
        expected = hmac.new(
            _SECRET_KEY.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        if data.get("exp", 0) < time.time():
            return None
        return data.get("uid")
    except Exception:
        return None
