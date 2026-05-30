"""
user_store.py - Persistent session and link-code storage for scan2order.

Storage backend is chosen at startup by inspecting the MYSQL_URL environment
variable:

  MYSQL_URL not set  → SQLite  (default; file at data/sessions.db)
  MYSQL_URL=mysql://…  → MySQL / MariaDB via PyMySQL

Both backends expose an identical interface to every caller in this module;
the dialect differences (placeholder style, upsert syntax, DDL types) are
confined to this file so nothing else needs to change.

Thread-safety model
───────────────────
• _MySQLDB wraps all connection access in an internal RLock so the shared
  PyMySQL connection is never used from two threads simultaneously.
• user_store._lock (threading.Lock) makes read-modify-write operations in
  update_store_cookies atomic across writer threads — exactly as before.
• SQLite with check_same_thread=False handles concurrent readers natively;
  writers are still serialised by _lock.
"""

import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

_TTL_30D  = 60 * 60 * 24 * 30
_TTL_24H  = 60 * 60 * 24
_OTP_TTL  = 10 * 60   # 10 minutes
_OTP_RATE = 60        # seconds between OTP requests per phone

_MYSQL_URL: str | None = os.environ.get("MYSQL_URL")
# e.g. mysql://scan2order:secretpassword@mysql:3306/scan2order
#      mysql+pymysql://user:pass@host/db   ('+pymysql' suffix is ignored)


# ── MySQL connection wrapper ───────────────────────────────────────────────────

class _FetchedCursor:
    """Materialised result set that mimics sqlite3 cursor's fetch methods.

    _MySQLDB.execute() fetches all rows under the connection lock and wraps
    them here, so callers can call .fetchone() / .fetchall() after the lock
    is released without risk of interleaving reads on the shared connection.
    """

    __slots__ = ("_rows",)

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _MySQLDB:
    """sqlite3.Connection-compatible wrapper around a single PyMySQL connection.

    Translates ? placeholders → %s automatically in .execute() so all
    SELECT / DELETE queries written once in SQLite style work on MySQL too.
    Only the upsert SQL strings (which differ structurally) are written
    per-dialect as module-level constants below.

    An internal RLock serialises all connection use, making this safe to
    share across threads without an external connection pool.  For very high
    concurrency a pool (e.g. DBUtils.PooledDB) can replace this class.
    """

    def __init__(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        scheme = parsed.scheme.split("+")[0]  # strip '+pymysql', '+mysqlconnector', …
        if scheme not in ("mysql", "mariadb"):
            raise ValueError(
                f"Unsupported scheme '{parsed.scheme}' in MYSQL_URL. "
                "Expected 'mysql://…' or 'mariadb://…'."
            )
        self._kwargs: dict = {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 3306,
            "user": parsed.username or "root",
            "password": parsed.password or "",
            "database": (parsed.path or "/scan2order").lstrip("/") or "scan2order",
            "charset": "utf8mb4",
            "autocommit": False,
        }
        self._rlock = threading.RLock()
        self._raw = None
        self._connect()

    def _connect(self) -> None:
        import pymysql
        import pymysql.cursors
        self._raw = pymysql.connect(
            **self._kwargs,
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _ensure(self) -> None:
        """Reconnect if the server dropped the connection."""
        try:
            self._raw.ping(reconnect=True)
        except Exception:
            self._connect()

    def execute(self, sql: str, params=()):
        """Run *sql* with *params* and return a _FetchedCursor.

        ? placeholders are translated to %s before execution so callers
        can write SQL in SQLite style for all non-upsert queries.
        All rows are fetched under the lock; the cursor is safe to read
        after the lock is released.
        """
        sql = sql.replace("?", "%s")
        with self._rlock:
            self._ensure()
            cur = self._raw.cursor()
            cur.execute(sql, params if params else None)
            rows = cur.fetchall()
        return _FetchedCursor(rows)

    def executescript(self, script: str) -> None:
        """Execute semicolon-delimited DDL statements (schema init only)."""
        with self._rlock:
            self._ensure()
            cur = self._raw.cursor()
            for stmt in script.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            self._raw.commit()

    def commit(self) -> None:
        with self._rlock:
            self._raw.commit()


# ── Schema DDL ─────────────────────────────────────────────────────────────────

_SQLITE_DDL = """
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
    CREATE TABLE IF NOT EXISTS users (
        user_id    TEXT PRIMARY KEY,
        phone      TEXT,
        email      TEXT,
        created_at REAL NOT NULL,
        last_login REAL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
    CREATE TABLE IF NOT EXISTS otp_codes (
        target     TEXT PRIMARY KEY,
        code       TEXT NOT NULL,
        expires_at REAL NOT NULL,
        used       INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    )
"""

# MySQL TEXT columns cannot carry a non-NULL DEFAULT in MySQL < 8.0.13.
_MYSQL_DDL = """
    CREATE TABLE IF NOT EXISTS sessions (
        user_id       VARCHAR(255) NOT NULL,
        store         VARCHAR(64)  NOT NULL,
        cookies       TEXT         NOT NULL,
        local_storage TEXT         NOT NULL,
        updated_at    DOUBLE       NOT NULL,
        PRIMARY KEY (user_id, store)
    );
    CREATE TABLE IF NOT EXISTS link_codes (
        code       VARCHAR(32)  NOT NULL,
        user_id    VARCHAR(255) NOT NULL,
        created_at DOUBLE       NOT NULL,
        PRIMARY KEY (code)
    );
    CREATE TABLE IF NOT EXISTS users (
        user_id    VARCHAR(255) NOT NULL,
        phone      VARCHAR(20)  UNIQUE,
        email      VARCHAR(255) UNIQUE,
        created_at DOUBLE       NOT NULL,
        last_login DOUBLE,
        PRIMARY KEY (user_id)
    );
    CREATE TABLE IF NOT EXISTS otp_codes (
        target     VARCHAR(255) NOT NULL,
        code       CHAR(6)      NOT NULL,
        expires_at DOUBLE       NOT NULL,
        used       TINYINT      NOT NULL DEFAULT 0,
        created_at DOUBLE       NOT NULL,
        PRIMARY KEY (target)
    )
"""

# ── Dialect-specific upsert SQL ────────────────────────────────────────────────
# SELECT / DELETE queries use ? (auto-translated to %s for MySQL by _MySQLDB).
# Only upsert queries differ structurally between the two dialects.

if _MYSQL_URL:
    _SQL_UPSERT_SESSION = """
        INSERT INTO sessions (user_id, store, cookies, local_storage, updated_at)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            cookies       = VALUES(cookies),
            local_storage = VALUES(local_storage),
            updated_at    = VALUES(updated_at)
    """
    _SQL_UPSERT_COOKIES = """
        INSERT INTO sessions (user_id, store, cookies, local_storage, updated_at)
        VALUES (%s, %s, %s, '{}', %s)
        ON DUPLICATE KEY UPDATE
            cookies    = VALUES(cookies),
            updated_at = VALUES(updated_at)
    """
    _SQL_UPSERT_LINK_CODE = (
        "REPLACE INTO link_codes (code, user_id, created_at) VALUES (%s, %s, %s)"
    )
    _SQL_UPSERT_OTP = """
        INSERT INTO otp_codes (target, code, expires_at, used, created_at)
        VALUES (%s, %s, %s, 0, %s)
        ON DUPLICATE KEY UPDATE
            code       = VALUES(code),
            expires_at = VALUES(expires_at),
            used       = 0,
            created_at = VALUES(created_at)
    """
else:
    _SQL_UPSERT_SESSION = """
        INSERT INTO sessions (user_id, store, cookies, local_storage, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, store) DO UPDATE SET
            cookies       = excluded.cookies,
            local_storage = excluded.local_storage,
            updated_at    = excluded.updated_at
    """
    _SQL_UPSERT_COOKIES = """
        INSERT INTO sessions (user_id, store, cookies, local_storage, updated_at)
        VALUES (?, ?, ?, '{}', ?)
        ON CONFLICT(user_id, store) DO UPDATE SET
            cookies    = excluded.cookies,
            updated_at = excluded.updated_at
    """
    _SQL_UPSERT_LINK_CODE = (
        "INSERT OR REPLACE INTO link_codes (code, user_id, created_at) VALUES (?, ?, ?)"
    )
    _SQL_UPSERT_OTP = """
        INSERT INTO otp_codes (target, code, expires_at, used, created_at)
        VALUES (?, ?, ?, 0, ?)
        ON CONFLICT(target) DO UPDATE SET
            code       = excluded.code,
            expires_at = excluded.expires_at,
            used       = 0,
            created_at = excluded.created_at
    """

# ── Schema migration helper ────────────────────────────────────────────────────

def _table_exists(conn, table: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        return True
    except Exception:
        return False


def _col_exists(conn, table: str, col: str) -> bool:
    try:
        conn.execute(f"SELECT {col} FROM {table} LIMIT 1")
        return True
    except Exception:
        return False


def _migrate(conn, is_mysql: bool) -> None:
    """Bring an existing DB up to the current (phone + email) schema.

    Handles every prior shape this project has shipped:
      • email+password users table  → drop (DDL recreates it; old accounts wiped)
      • phone-only users table       → ALTER ADD COLUMN email
      • phone+email users table      → no-op
      • otp_codes keyed by 'phone'   → drop (ephemeral; DDL recreates keyed by 'target')
    Each step is best-effort; a fresh DB (no tables) falls straight through to DDL.
    """
    # users table
    if _table_exists(conn, "users"):
        if not _col_exists(conn, "users", "phone"):
            # Oldest schema (email + password). Safe to wipe — predates real accounts.
            try:
                conn.execute("DROP TABLE IF EXISTS users")
                conn.commit()
                print("[user_store] migrated: dropped legacy email+password users table")
            except Exception:
                pass
        elif not _col_exists(conn, "users", "email"):
            try:
                coltype = "VARCHAR(255)" if is_mysql else "TEXT"
                conn.execute(f"ALTER TABLE users ADD COLUMN email {coltype}")
                if is_mysql:
                    # SQLite gets its unique index from the DDL below; MySQL needs it here.
                    conn.execute("CREATE UNIQUE INDEX idx_users_email ON users(email)")
                conn.commit()
                print("[user_store] migrated: added email column to users")
            except Exception as e:
                print(f"[user_store] email-column migration skipped: {e}")

    # otp_codes table — renamed key column phone → target; ephemeral, safe to drop
    if _table_exists(conn, "otp_codes") and not _col_exists(conn, "otp_codes", "target"):
        try:
            conn.execute("DROP TABLE IF EXISTS otp_codes")
            conn.commit()
        except Exception:
            pass


# ── Connection initialisation ──────────────────────────────────────────────────

if _MYSQL_URL:
    _conn: _MySQLDB | sqlite3.Connection = _MySQLDB(_MYSQL_URL)
    _migrate(_conn, is_mysql=True)
    _conn.executescript(_MYSQL_DDL)
    print(f"[user_store] using MySQL backend: {_MYSQL_URL.split('@')[-1]}")
else:
    _DB_DIR = Path(__file__).resolve().parents[2] / "data"
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(_DB_DIR / "sessions.db"), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _migrate(_conn, is_mysql=False)
    _conn.executescript(_SQLITE_DDL)
    _conn.commit()
    print("[user_store] using SQLite backend")

# Serialises write operations (and the read-modify-write in update_store_cookies).
_lock = threading.Lock()


# ── Public API ─────────────────────────────────────────────────────────────────

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
            _SQL_UPSERT_SESSION,
            (user_id, store, json.dumps(cookies),
             json.dumps(local_storage or {}), time.time()),
        )
        _conn.commit()


def disconnect_store(user_id: str, store: str) -> None:
    with _lock:
        _conn.execute(
            "DELETE FROM sessions WHERE user_id=? AND store=?",
            (user_id, store),
        )
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
    """Merge *new_cookies* into the stored cookie dict for (user_id, store).

    The read and write happen inside _lock so that two concurrent callers
    cannot both read the same stale cookie dict, merge independently, and
    then clobber each other's result.
    """
    cutoff = time.time() - _TTL_30D
    with _lock:
        row = _conn.execute(
            "SELECT cookies FROM sessions WHERE user_id=? AND store=? AND updated_at>?",
            (user_id, store, cutoff),
        ).fetchone()
        existing = json.loads(row["cookies"]) if row else {}
        existing.update(new_cookies)
        _conn.execute(
            _SQL_UPSERT_COOKIES,
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
        _conn.execute(_SQL_UPSERT_LINK_CODE, (code, user_id, time.time()))
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


# ── User account CRUD ─────────────────────────────────────────────────────────

_CONTACT_COL = {"phone": "phone", "email": "email"}


def _col(channel: str) -> str:
    col = _CONTACT_COL.get(channel)
    if not col:
        raise ValueError(f"Unknown contact channel: {channel!r}")
    return col


def get_user_by_contact(channel: str, value: str) -> str | None:
    """Return the user_id whose *channel* (phone|email) equals *value*, or None."""
    col = _col(channel)
    row = _conn.execute(
        f"SELECT user_id FROM users WHERE {col}=?", (value,)
    ).fetchone()
    return row["user_id"] if row else None


def get_or_create_user(channel: str, value: str) -> str:
    """Return the user_id for (channel, value), creating an account if none exists."""
    col = _col(channel)
    with _lock:
        row = _conn.execute(
            f"SELECT user_id FROM users WHERE {col}=?", (value,)
        ).fetchone()
        if row:
            return row["user_id"]
        uid = str(uuid.uuid4())
        _conn.execute(
            f"INSERT INTO users (user_id, {col}, created_at) VALUES (?, ?, ?)",
            (uid, value, time.time()),
        )
        _conn.commit()
        return uid


def attach_contact(user_id: str, channel: str, value: str) -> tuple[bool, str | None]:
    """Attach a verified phone/email to an existing account.

    Returns (True, None) on success, or (False, reason) if the contact is
    already in use by a different account.
    """
    col = _col(channel)
    with _lock:
        owner = _conn.execute(
            f"SELECT user_id FROM users WHERE {col}=?", (value,)
        ).fetchone()
        if owner and owner["user_id"] != user_id:
            return False, "already linked to another account"
        if owner and owner["user_id"] == user_id:
            return True, None  # already attached to this user — idempotent
        _conn.execute(
            f"UPDATE users SET {col}=? WHERE user_id=?", (value, user_id)
        )
        _conn.commit()
        return True, None


def get_user_by_id(user_id: str) -> dict | None:
    row = _conn.execute(
        "SELECT user_id, phone, email FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    if not row:
        return None
    return {"user_id": row["user_id"], "phone": row["phone"], "email": row["email"]}


def update_last_login(user_id: str) -> None:
    with _lock:
        _conn.execute(
            "UPDATE users SET last_login=? WHERE user_id=?",
            (time.time(), user_id),
        )
        _conn.commit()


# ── OTP management ────────────────────────────────────────────────────────────
# Keyed by "target" — a phone (+91…) or an email; the two never collide.

def is_otp_rate_limited(target: str) -> bool:
    """True if an OTP was issued for *target* within the last OTP_RATE seconds."""
    row = _conn.execute(
        "SELECT created_at FROM otp_codes WHERE target=?", (target,)
    ).fetchone()
    if not row:
        return False
    return (time.time() - row["created_at"]) < _OTP_RATE


def save_otp(target: str, code: str) -> None:
    """Upsert a fresh OTP for *target*, replacing any existing entry."""
    with _lock:
        _conn.execute(
            _SQL_UPSERT_OTP,
            (target, code, time.time() + _OTP_TTL, time.time()),
        )
        _conn.commit()


def verify_and_consume_otp(target: str, code: str) -> bool:
    """Return True and mark the OTP used if *code* is valid and unexpired."""
    now = time.time()
    with _lock:
        row = _conn.execute(
            "SELECT code, expires_at, used FROM otp_codes WHERE target=?", (target,)
        ).fetchone()
        if not row:
            return False
        if row["used"] or row["expires_at"] < now:
            return False
        if row["code"] != code:
            return False
        _conn.execute("UPDATE otp_codes SET used=1 WHERE target=?", (target,))
        _conn.commit()
        return True
