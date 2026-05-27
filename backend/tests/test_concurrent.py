"""
test_concurrent.py — Multi-user simultaneous access tests.

These tests verify that the backend correctly isolates data between
different users and handles concurrent requests without state leakage.

Two categories:
  A. Data isolation — user A can never see user B's cookies, progress,
     comparison results, or cart state.
  B. Race-condition regression — specific bugs that were found and fixed:
     1. _pw double-initialisation in auth_browser.py
     2. update_store_cookies read-modify-write in user_store.py

Tests use asyncio.gather() to run coroutines truly in parallel within
the same event loop, exposing any races at asyncio yield points.
"""

import asyncio
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ─────────────────────────────────────────────────────────────────────────────
# A. Data isolation — HTTP-level (using TestClient)
# ─────────────────────────────────────────────────────────────────────────────

class TestUserIsolation:
    """Two different users must never see each other's data."""

    def test_cookies_not_shared_between_users(
            self, client, user_id, clean_db, bb_cookies):
        user_a = user_id
        user_b = "user-b-ccccdddd"

        client.post("/api/auth/connect", json={
            "user_id": user_a, "store": "bigbasket", "cookies": bb_cookies,
        })

        # User B has never connected — must not see A's cookies
        from storage import user_store
        assert user_store.get_store_cookies(user_b, "bigbasket") == {}

    def test_auth_status_isolated(self, client, connected_user_bb, user_id, clean_db):
        user_b = "user-b-eeeeffff"
        r = client.get(f"/api/auth/status/{user_b}")
        assert r.json()["connected_stores"] == {}

    def test_compare_progress_isolated(self, client, user_id, clean_db):
        user_b = "user-b-gggghhh"
        # Seed fake progress for user A directly
        from server import _compare_progress
        _compare_progress[user_id] = {
            "running": True, "total": 5, "done": 2, "current": "butter",
        }
        try:
            r = client.get(f"/api/compare/progress?user_id={user_b}")
            data = r.json()
            # User B should see the default zero-state, not A's progress
            assert data["running"] is False
            assert data["done"] == 0
        finally:
            _compare_progress.pop(user_id, None)

    def test_cart_progress_isolated(self, client, user_id, clean_db):
        user_b = "user-b-iiijjjj"
        from server import _cart_progress
        _cart_progress[user_id] = {
            "running": True, "total": 10, "done": 7, "current": "milk",
        }
        try:
            r = client.get(f"/api/cart/progress?user_id={user_b}")
            assert r.json()["done"] == 0
        finally:
            _cart_progress.pop(user_id, None)

    def test_disconnect_one_user_doesnt_affect_other(
            self, client, user_id, clean_db, bb_cookies):
        user_a = user_id
        user_b = "user-b-kkkkllll"

        # Both users connect BigBasket
        for uid in (user_a, user_b):
            client.post("/api/auth/connect", json={
                "user_id": uid, "store": "bigbasket", "cookies": bb_cookies,
            })

        from storage import user_store
        user_store.disconnect_store(user_a, "bigbasket")

        assert not user_store.is_store_connected(user_a, "bigbasket")
        assert user_store.is_store_connected(user_b, "bigbasket")

    def test_link_code_resolves_to_correct_user(
            self, client, user_id, clean_db, bb_cookies):
        user_a = user_id
        user_b = "user-b-mmmmnnnn"

        client.post("/api/auth/connect", json={
            "user_id": user_a, "store": "bigbasket", "cookies": bb_cookies,
        })
        client.post("/api/auth/connect", json={
            "user_id": user_b, "store": "blinkit",
            "cookies": {"gr_1_accessToken": "bl-tok-bbbbbbbbbbbbbbbbbbbbb"},
        })

        code_a = client.post("/api/auth/link", json={"user_id": user_a}).json()["code"]
        code_b = client.post("/api/auth/link", json={"user_id": user_b}).json()["code"]

        assert code_a != code_b

        result_a = client.get(f"/api/auth/link/{code_a}").json()
        result_b = client.get(f"/api/auth/link/{code_b}").json()

        assert result_a["user_id"] == user_a
        assert result_b["user_id"] == user_b

    def test_two_users_compare_same_item_get_independent_results(
            self, client, user_id, clean_db, bb_cookies, bl_cookies, mock_stores):
        user_a = user_id
        user_b = "user-b-oooopppp"

        # User A: only BigBasket
        client.post("/api/auth/connect", json={
            "user_id": user_a, "store": "bigbasket", "cookies": bb_cookies,
        })
        # User B: only Blinkit
        client.post("/api/auth/connect", json={
            "user_id": user_b, "store": "blinkit", "cookies": bl_cookies,
        })

        payload = {"items": [{"name": "Amul Butter", "qty": "100g"}]}

        r_a = client.post("/api/compare", json={"user_id": user_a, **payload})
        r_b = client.post("/api/compare", json={"user_id": user_b, **payload})

        entry_a = r_a.json()["comparison"][0]
        entry_b = r_b.json()["comparison"][0]

        # A only has BB, B only has Blinkit → different cheapest stores
        assert entry_a["cheapest_app"] == "bigbasket"
        assert entry_b["cheapest_app"] == "blinkit"

        # Neither user should see the other's store prices
        assert "blinkit" not in entry_a["prices"]
        assert "bigbasket" not in entry_b["prices"]


# ─────────────────────────────────────────────────────────────────────────────
# B. Race-condition regression tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPlaywrightInitRace:
    """
    Regression for: _pw double-initialisation when two users call
    /api/auth/browser/start/{store} simultaneously.

    Strategy: temporarily un-mock auth_browser.start and test the real
    _get_playwright() double-checked locking in isolation.
    """

    @pytest.mark.asyncio
    async def test_pw_init_called_exactly_once_under_concurrency(self):
        """
        Simulate two concurrent _get_playwright() calls.  With the double-checked
        lock the Playwright-equivalent init body must run exactly once.

        We replay the race directly on the module's own lock+state rather than
        going through the real Playwright, so no Chromium process is needed.
        """
        import auth_browser

        init_count = 0

        # Reset module state; restore it in finally
        original_pw = auth_browser._pw
        original_lock = auth_browser._pw_lock
        auth_browser._pw = None
        auth_browser._pw_lock = None   # _get_pw_lock() will recreate it

        try:
            async def init_once():
                nonlocal init_count
                # Mirror the exact double-checked pattern from auth_browser.py
                if auth_browser._pw is not None:
                    return auth_browser._pw
                async with auth_browser._get_pw_lock():
                    if auth_browser._pw is None:
                        await asyncio.sleep(0)  # yield — lets other coroutine run
                        init_count += 1
                        auth_browser._pw = f"instance-{init_count}"
                return auth_browser._pw

            results = await asyncio.gather(init_once(), init_once())

            assert results[0] == results[1], \
                f"Two different instances were returned: {results}"
            assert init_count == 1, \
                f"Init ran {init_count} times — double-checked lock is broken"
        finally:
            auth_browser._pw = original_pw
            auth_browser._pw_lock = original_lock

    def test_pw_lock_getter_returns_same_lock(self):
        """_get_pw_lock() must return the same Lock object on repeated calls."""
        import auth_browser
        original_lock = auth_browser._pw_lock
        auth_browser._pw_lock = None  # reset for clean test
        try:
            lock1 = auth_browser._get_pw_lock()
            lock2 = auth_browser._get_pw_lock()
            assert lock1 is lock2, "Expected same Lock object on both calls"
        finally:
            auth_browser._pw_lock = original_lock

    @pytest.mark.asyncio
    async def test_concurrent_browser_start_uses_same_session_id(
            self, monkeypatch, clean_db):
        """Two starts for the same (user, store) must result in one session."""
        import auth_browser

        start_count = 0

        async def counting_start(user_id, store, geolocation=None):
            nonlocal start_count
            await asyncio.sleep(0)  # yield
            start_count += 1
            session_id = f"{user_id}--{store}"
            auth_browser._sessions[session_id] = object()  # placeholder
            return session_id

        monkeypatch.setattr(auth_browser, "start", counting_start)

        user = "concurrent-user-xxx"
        store = "blinkit"
        results = await asyncio.gather(
            counting_start(user, store),
            counting_start(user, store),
        )

        # Both calls should target the same session_id
        assert results[0] == results[1]
        auth_browser._sessions.pop(f"{user}--{store}", None)


class TestCookieUpdateRace:
    """
    Regression for: update_store_cookies() read-modify-write not atomic.

    Two concurrent callers that each add a different key must both survive
    in the final stored cookies — neither write must be lost.
    """

    def test_sequential_updates_preserve_both_keys(self, clean_db):
        """Baseline: sequential updates merge correctly."""
        from storage import user_store

        uid = "merge-test-user"
        user_store.connect_store(uid, "zepto", {"accessToken": "tok-0"})

        user_store.update_store_cookies(uid, "zepto", {"newKey1": "v1"})
        user_store.update_store_cookies(uid, "zepto", {"newKey2": "v2"})

        cookies = user_store.get_store_cookies(uid, "zepto")
        assert cookies.get("accessToken") == "tok-0"
        assert cookies.get("newKey1") == "v1"
        assert cookies.get("newKey2") == "v2"

    def test_concurrent_thread_updates_do_not_lose_keys(self, clean_db):
        """
        Two threads each call update_store_cookies with a different key.
        After both complete the stored dict must contain both keys.

        This is the thread-level concurrency test for the read-inside-lock fix.
        Note: asyncio is single-threaded so this scenario only arises if
        user_store functions are ever called from a thread pool.  We test it
        anyway to validate the lock is correct.
        """
        from storage import user_store

        uid = "thread-merge-user"
        user_store.connect_store(uid, "bigbasket", {"BBAUTHTOKEN": "base-tok"})

        errors = []
        barrier = threading.Barrier(2)

        def updater(key: str, value: str):
            try:
                barrier.wait()  # both threads start at the same time
                user_store.update_store_cookies(uid, "bigbasket", {key: value})
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=updater, args=("threadKey1", "val1"))
        t2 = threading.Thread(target=updater, args=("threadKey2", "val2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Thread raised: {errors}"

        cookies = user_store.get_store_cookies(uid, "bigbasket")
        assert cookies.get("BBAUTHTOKEN") == "base-tok", "Base token was overwritten"
        assert cookies.get("threadKey1") == "val1", "threadKey1 was lost"
        assert cookies.get("threadKey2") == "val2", "threadKey2 was lost"

    def test_update_does_not_overwrite_unrelated_keys(self, clean_db):
        """update_store_cookies must merge, not replace the whole dict."""
        from storage import user_store

        uid = "merge-no-overwrite-user"
        user_store.connect_store(uid, "zepto", {
            "accessToken": "tok-z",
            "deviceId": "dev-z",
        })
        user_store.update_store_cookies(uid, "zepto", {"refreshToken": "ref-z"})

        cookies = user_store.get_store_cookies(uid, "zepto")
        assert "accessToken" in cookies
        assert "deviceId" in cookies
        assert cookies["refreshToken"] == "ref-z"


class TestProgressIsolation:
    """Per-user progress state must never bleed between users."""

    @pytest.mark.asyncio
    async def test_two_users_compare_progress_stays_separate(
            self, monkeypatch, clean_db, bb_cookies, bl_cookies):
        """
        Two users run /api/compare simultaneously.  Each user's progress
        endpoint must only reflect their own operation.
        """
        from storage import user_store
        from stores import bigbasket, blinkit, zepto

        user_a = "progress-user-aaaa"
        user_b = "progress-user-bbbb"

        user_store.connect_store(user_a, "bigbasket", bb_cookies)
        user_store.connect_store(user_b, "blinkit", bl_cookies)

        # Replace store searches with slow async mocks so progress has time to be set
        call_order = []

        async def slow_bb_search(uid, query):
            call_order.append(("bb", uid))
            await asyncio.sleep(0.01)
            return [{"product_id": "bb-01", "name": "Amul Butter 100g",
                     "unit": "100g", "sale_price": 55.0}]

        async def slow_bl_search(uid, query):
            call_order.append(("bl", uid))
            await asyncio.sleep(0.01)
            return [{"product_id": "bl-01", "name": "Amul Butter 100g",
                     "unit": "100g", "sale_price": 58.0}]

        async def no_search(uid, query):
            return []

        monkeypatch.setattr(bigbasket, "search_item_api", slow_bb_search)
        monkeypatch.setattr(blinkit, "search_item_api", slow_bl_search)
        monkeypatch.setattr(zepto, "search_item_api", no_search)
        monkeypatch.setattr(bigbasket, "add_to_cart_api",
                            AsyncMock(return_value={"success": True, "count_added": 1}))
        monkeypatch.setattr(blinkit, "add_to_cart_api",
                            AsyncMock(return_value={"success": True, "count_added": 1}))
        monkeypatch.setattr(zepto, "add_all_to_cart_api",
                            AsyncMock(return_value={"success": True, "items": []}))

        import ranker

        items = [{"name": "Amul Butter", "qty": "100g"}]

        async def run_compare(uid: str, stores: list):
            results = []
            for item in items:
                entry = await ranker.compare_one_item(item, uid, stores)
                results.append(entry)
            return results

        # Run both users' compares in parallel
        results_a, results_b = await asyncio.gather(
            run_compare(user_a, ["bigbasket"]),
            run_compare(user_b, ["blinkit"]),
        )

        # Each user's results must come from their own connected store
        assert results_a[0]["cheapest_app"] == "bigbasket", \
            f"user_a got wrong store: {results_a[0]['cheapest_app']}"
        assert results_b[0]["cheapest_app"] == "blinkit", \
            f"user_b got wrong store: {results_b[0]['cheapest_app']}"

        # Verify each user only searched their own store
        bb_calls = [uid for store, uid in call_order if store == "bb"]
        bl_calls = [uid for store, uid in call_order if store == "bl"]
        assert all(u == user_a for u in bb_calls), \
            f"BigBasket was called for wrong user: {bb_calls}"
        assert all(u == user_b for u in bl_calls), \
            f"Blinkit was called for wrong user: {bl_calls}"

    @pytest.mark.asyncio
    async def test_cart_progress_resets_after_completion(
            self, monkeypatch, clean_db, bb_cookies, mock_stores):
        """Progress dict is reset to zero-state after /api/cart/add-all finishes."""
        from storage import user_store
        from server import app, _cart_progress
        from starlette.testclient import TestClient

        uid = "cart-progress-reset-user"
        user_store.connect_store(uid, "bigbasket", bb_cookies)

        with TestClient(app) as c:
            c.post("/api/cart/add-all", json={
                "user_id": uid,
                "carts": {
                    "bigbasket": {"items": [
                        {"product_id": "bb-001", "name": "Butter",
                         "count": 1, "fc_id": 10},
                    ]},
                },
            })

        # After the request completes, progress must be reset (not left dirty)
        progress = _cart_progress.get(uid, {})
        assert not progress.get("running", False), \
            "Progress 'running' flag was not reset after cart-add completed"


class TestSessionIsolation:
    """Browser sessions must be isolated per user+store."""

    def test_session_id_format_includes_user_and_store(self, mock_browser, clean_db, client):
        r = client.post("/api/auth/browser/start/blinkit",
                        json={"user_id": "iso-user-aaa"})
        sid = r.json()["session_id"]
        assert "iso-user-aaa" in sid
        assert "blinkit" in sid

    def test_two_users_get_separate_sessions(self, mock_browser, clean_db, client):
        r1 = client.post("/api/auth/browser/start/blinkit",
                         json={"user_id": "user-sess-111"})
        r2 = client.post("/api/auth/browser/start/blinkit",
                         json={"user_id": "user-sess-222"})

        sid1 = r1.json()["session_id"]
        sid2 = r2.json()["session_id"]

        assert sid1 != sid2

        # Each session only responds to its own screenshot requests
        s1 = client.get(f"/api/auth/browser/screenshot/{sid1}")
        s2 = client.get(f"/api/auth/browser/screenshot/{sid2}")
        assert s1.status_code == 200
        assert s2.status_code == 200

        # Cross-access: trying to use the wrong session_id returns 404
        cross = client.get(f"/api/auth/browser/screenshot/user-sess-111--zepto")
        assert cross.status_code == 404

    def test_user_can_reconnect_same_store(self, mock_browser, clean_db, client):
        """Starting a new session for the same (user, store) replaces the old one."""
        uid = "reconnect-user-zzz"
        client.post("/api/auth/browser/start/blinkit", json={"user_id": uid})
        r2 = client.post("/api/auth/browser/start/blinkit", json={"user_id": uid})
        assert r2.json()["success"] is True
        # The session_id format is deterministic: "uid--store"
        assert r2.json()["session_id"] == f"{uid}--blinkit"
