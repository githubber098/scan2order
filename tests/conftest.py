"""Pytest configuration and shared fixtures for scan2order backend tests.

All tests run against an in-memory SQLite DB — nothing touches the real
sessions.db on disk. Playwright and external store HTTP calls are mocked
in test_api.py using unittest.mock.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

# Add the backend directory to sys.path so all backend modules can be
# imported as top-level names (matching how they import each other inside
# the backend package).
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))


# ── DB isolation ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def _patch_db():
    """Replace user_store's module-level SQLite connection with an in-memory DB.

    user_store.py creates _conn at import time, pointing at ./data/sessions.db
    relative to the project root.  Swapping it here means every test sees a
    clean, isolated DB instead of the real one.

    autouse=True at session scope means this runs before any test or TestClient
    is instantiated.  Because Python caches module objects, patching _conn on
    the already-imported user_store module affects every caller that holds a
    reference to user_store (including server.py).
    """
    from storage import user_store  # import triggers real _conn creation

    mem = sqlite3.connect(":memory:", check_same_thread=False)
    mem.row_factory = sqlite3.Row
    mem.execute("PRAGMA journal_mode=WAL")
    mem.executescript("""
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
    mem.commit()

    original = user_store._conn
    user_store._conn = mem
    yield mem
    user_store._conn = original
    mem.close()


# ── App client ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    """Synchronous FastAPI TestClient that exercises the full ASGI stack.

    scope="module" means the app starts up once per test file and shuts down
    after all tests in that file finish — fast without inter-test state leakage
    from the in-memory DB (tests that write data use unique user IDs).
    """
    from fastapi.testclient import TestClient
    from server import app

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
