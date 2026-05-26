import os
import json
import secrets
from upstash_redis import Redis

redis = Redis(
    url=os.getenv("UPSTASH_REDIS_REST_URL"),
    token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
)

_TTL_30D = 60 * 60 * 24 * 30
_TTL_24H = 60 * 60 * 24


def get_user_stores(user_id: str) -> dict:
    data = redis.get(f"user:{user_id}")
    if not data:
        return {}
    return json.loads(data) if isinstance(data, str) else data


def connect_store(user_id: str, store: str, cookies: dict,
                  local_storage: dict | None = None) -> None:
    current = get_user_stores(user_id)
    current[store] = {
        "connected": True,
        "cookies": cookies,
        "local_storage": local_storage or {},
    }
    redis.setex(f"user:{user_id}", _TTL_30D, json.dumps(current))


def disconnect_store(user_id: str, store: str) -> None:
    current = get_user_stores(user_id)
    if store in current:
        del current[store]
        redis.setex(f"user:{user_id}", _TTL_30D, json.dumps(current))


def get_store_cookies(user_id: str, store: str) -> dict:
    data = get_user_stores(user_id)
    return data.get(store, {}).get("cookies", {})


def get_store_local_storage(user_id: str, store: str) -> dict:
    data = get_user_stores(user_id)
    return data.get(store, {}).get("local_storage", {})


def get_store_session(user_id: str, store: str) -> dict:
    """Return {cookies, local_storage} for a store."""
    data = get_user_stores(user_id)
    entry = data.get(store, {})
    return {
        "cookies": entry.get("cookies", {}),
        "local_storage": entry.get("local_storage", {}),
    }


def update_store_cookies(user_id: str, store: str, new_cookies: dict) -> None:
    """Merge new_cookies into the stored cookie dict (used for token refresh)."""
    current = get_user_stores(user_id)
    entry = current.setdefault(store, {"connected": True, "cookies": {}, "local_storage": {}})
    entry["cookies"].update(new_cookies)
    redis.setex(f"user:{user_id}", _TTL_30D, json.dumps(current))


def is_store_connected(user_id: str, store: str) -> bool:
    data = get_user_stores(user_id)
    return data.get(store, {}).get("connected", False)


def create_link_code(user_id: str) -> str:
    """Generate an 8-char alphanumeric link code. Mobile → web session sharing."""
    code = secrets.token_urlsafe(6)[:8].upper()
    redis.setex(f"link:{code}", _TTL_24H, user_id)
    return code


def get_user_id_by_code(code: str) -> str | None:
    val = redis.get(f"link:{code.upper()}")
    return val if isinstance(val, str) else None


def consume_link_code(code: str) -> str | None:
    """Resolve and delete a link code (one-time use)."""
    val = redis.get(f"link:{code.upper()}")
    if val:
        redis.delete(f"link:{code.upper()}")
        return val if isinstance(val, str) else None
    return None
