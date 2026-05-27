import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

_DB_DIR = Path(__file__).resolve().parents[2] / "data"
_DB_DIR.mkdir(parents=True, exist_ok=True)

_conn = sqlite3.connect(str(_DB_DIR / "sessions.db"), check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
        user_id       TEXT NOT NULL,
        store         TEXT NOT NULL,
        cookies       TEXT NOT NULL DEFAULT '{}',
        local_storage TEXT NOT NULL DEFAULT '{}',
        updated_at    REAL NOT NULL,
        PRIMARY KEY (user_id, store)
    );
    CREATE TABLE IF NOT EXISTS link_codes (
        code       TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        created_at REAL NOT NULL
    );
""")
_conn.commit()
_lock = threading.Lock()

_TTL_30D = 60 * 60 * 24 * 30
_TTL_24H = 60 * 60 * 24


def get_user_stores(user_id: str) -> dict:
    cutoff = time.time() - _TTL_30D
    rows = _conn.execute(
        "SELECT store, cookies, local_storage FROM sessions WHERE user_id=? AND updated_at>?",
        (user_id, cutoff),
    ).fetchall()
    return {
        r["store"]: {
            "connected": True,
            "cookies": json.loads(r["cookies"]),
            "local_storage": json.loads(r["local_storage"]),
        }
        for r in rows
    }


def connect_store(user_id: str, store: str, cookies: dict,
                  local_storage: dict | None = None) -> None:
    with _lock:
        _conn.execute(
            """INSERT INTO sessions (user_id, store, cookies, local_storage, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, store) DO UPDATE SET
                   cookies=excluded.cookies,
                   local_storage=excluded.local_storage,
                   updated_at=excluded.updated_at""",
            (user_id, store, json.dumps(cookies), json.dumps(local_storage or {}), time.time()),
        )
        _conn.commit()


def disconnect_store(user_id: str, store: str) -> None:
    with _lock:
        _conn.execute("DELETE FROM sessions WHERE user_id=? AND store=?", (user_id, store))
        _conn.commit()


def get_store_cookies(user_id: str, store: str) -> dict:
    cutoff = time.time() - _TTL_30D
    row = _conn.execute(
        "SELECT cookies FROM sessions WHERE user_id=? AND store=? AND updated_at>?",
        (user_id, store, cutoff),
    ).fetchone()
    return json.loads(row["cookies"]) if row else {}


def get_store_local_storage(user_id: str, store: str) -> dict:
    cutoff = time.time() - _TTL_30D
    row = _conn.execute(
        "SELECT local_storage FROM sessions WHERE user_id=? AND store=? AND updated_at>?",
        (user_id, store, cutoff),
    ).fetchone()
    return json.loads(row["local_storage"]) if row else {}


def get_store_session(user_id: str, store: str) -> dict:
    cutoff = time.time() - _TTL_30D
    row = _conn.execute(
        "SELECT cookies, local_storage FROM sessions WHERE user_id=? AND store=? AND updated_at>?",
        (user_id, store, cutoff),
    ).fetchone()
    if not row:
        return {"cookies": {}, "local_storage": {}}
    return {
        "cookies": json.loads(row["cookies"]),
        "local_storage": json.loads(row["local_storage"]),
    }


def update_store_cookies(user_id: str, store: str, new_cookies: dict) -> None:
    existing = get_store_cookies(user_id, store)
    existing.update(new_cookies)
    with _lock:
        _conn.execute(
            """INSERT INTO sessions (user_id, store, cookies, local_storage, updated_at)
               VALUES (?, ?, ?, '{}', ?)
               ON CONFLICT(user_id, store) DO UPDATE SET
                   cookies=excluded.cookies,
                   updated_at=excluded.updated_at""",
            (user_id, store, json.dumps(existing), time.time()),
        )
        _conn.commit()


def is_store_connected(user_id: str, store: str) -> bool:
    cutoff = time.time() - _TTL_30D
    row = _conn.execute(
        "SELECT 1 FROM sessions WHERE user_id=? AND store=? AND updated_at>?",
        (user_id, store, cutoff),
    ).fetchone()
    return row is not None


def create_link_code(user_id: str) -> str:
    code = secrets.token_urlsafe(6)[:8].upper()
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO link_codes (code, user_id, created_at) VALUES (?, ?, ?)",
            (code, user_id, time.time()),
        )
        _conn.commit()
    return code


def get_user_id_by_code(code: str) -> str | None:
    cutoff = time.time() - _TTL_24H
    row = _conn.execute(
        "SELECT user_id FROM link_codes WHERE code=? AND created_at>?",
        (code.upper(), cutoff),
    ).fetchone()
    return row["user_id"] if row else None


def consume_link_code(code: str) -> str | None:
    user_id = get_user_id_by_code(code)
    if user_id:
        with _lock:
            _conn.execute("DELETE FROM link_codes WHERE code=?", (code.upper(),))
            _conn.commit()
    return user_id
