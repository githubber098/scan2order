"""backend/storage/user_store.py

Per-user session store backed by Upstash Redis.

Schema for each user (key: "user:{user_id}"):
{
  "bigbasket": {
    "connected": true,
    "cookies": {"BBAUTHTOKEN": "...", "csurftoken": "..."},
    "local_storage": {}
  },
  "zepto": {
    "connected": true,
    "cookies": {"XSRF-TOKEN": "...", "accessToken": "..."},
    "local_storage": {"device_id": "...", "session_id": "..."}
  },
  "blinkit": {
    "connected": true,
    "cookies": {"gr_1_deviceId": "...", "city": "bangalore"},
    "local_storage": {}
  }
}

Blinkit location (lat/lon) stored separately:
  key: "location:{user_id}" -> {"lat": "12.99", "lon": "77.73"}
  (30-day TTL, same as session)

Link codes for mobile->web pairing:
  key: "link:{code}" -> user_id
  (15-minute TTL)
"""

import os
import json
import secrets
from upstash_redis import Redis

redis = Redis(
    url=os.getenv("UPSTASH_REDIS_REST_URL"),
    token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
)

_TTL = 60 * 60 * 24 * 30  # 30 days
_LINK_TTL = 60 * 15        # 15 minutes


# ── Core user-store helpers ─────────────────────────────────────

def get_user_stores(user_id: str) -> dict:
    """Get all connected stores for a user."""
    data = redis.get(f"user:{user_id}")
    if not data:
        return {}
    return json.loads(data)


def _save_user_stores(user_id: str, stores: dict) -> None:
    redis.setex(f"user:{user_id}", _TTL, json.dumps(stores))


def connect_store(
    user_id: str,
    store: str,
    cookies: dict,
    local_storage: dict | None = None,
) -> None:
    """Add or update a store connection for a user."""
    current = get_user_stores(user_id)
    current[store] = {
        "connected": True,
        "cookies": cookies,
        "local_storage": local_storage or {},
    }
    _save_user_stores(user_id, current)


def disconnect_store(user_id: str, store: str) -> None:
    """Remove a store connection for a user."""
    current = get_user_stores(user_id)
    if store in current:
        del current[store]
        _save_user_stores(user_id, current)


def is_store_connected(user_id: str, store: str) -> bool:
    """Check if a store is connected for a user."""
    data = get_user_stores(user_id)
    return data.get(store, {}).get("connected", False)


def get_store_cookies(user_id: str, store: str) -> dict:
    """Get cookies dict for a specific store."""
    data = get_user_stores(user_id)
    return data.get(store, {}).get("cookies", {})


def get_store_local_storage(user_id: str, store: str) -> dict:
    """Get localStorage dict for a specific store."""
    data = get_user_stores(user_id)
    return data.get(store, {}).get("local_storage", {})


def get_store_session(user_id: str, store: str) -> dict:
    """Get both cookies and local_storage for a store in one call."""
    data = get_user_stores(user_id)
    entry = data.get(store, {})
    return {
        "connected": entry.get("connected", False),
        "cookies": entry.get("cookies", {}),
        "local_storage": entry.get("local_storage", {}),
    }


def get_connected_stores(user_id: str) -> list[str]:
    """Return list of store names that are connected for a user."""
    data = get_user_stores(user_id)
    return [store for store, info in data.items() if info.get("connected")]


# ── Blinkit location ────────────────────────────────────────────

def set_user_location(user_id: str, lat: str, lon: str) -> None:
    """Store user's lat/lon for Blinkit API calls."""
    redis.setex(
        f"location:{user_id}",
        _TTL,
        json.dumps({"lat": lat, "lon": lon}),
    )


def get_user_lat_lon(user_id: str) -> tuple[str, str]:
    """Return (lat, lon) for user, or ("", "") if not set."""
    data = redis.get(f"location:{user_id}")
    if not data:
        return "", ""
    loc = json.loads(data)
    return loc.get("lat", ""), loc.get("lon", "")


# ── Mobile→Web link codes ───────────────────────────────────────

def generate_link_code(user_id: str) -> str:
    """Generate an 8-char alphanumeric code that maps to user_id.
    Used by the mobile app to link the web UI to its Redis session.
    Overwrites any prior code for this user_id (one active code at a time).
    """
    code = secrets.token_urlsafe(6)[:8].upper()
    redis.setex(f"link:{code}", _LINK_TTL, user_id)
    return code


def resolve_link_code(code: str) -> str:
    """Resolve a link code to a user_id. Returns "" if expired/invalid."""
    user_id = redis.get(f"link:{code.upper()}")
    return user_id or ""


def consume_link_code(code: str) -> str:
    """Resolve and delete a link code in one operation.
    Returns user_id, or "" if invalid. Call this when web UI first connects.
    """
    user_id = resolve_link_code(code)
    if user_id:
        redis.delete(f"link:{code.upper()}")
    return user_id
