"""
test_server_logs.py — Tests for the GET /api/logs endpoint.

The endpoint streams the tail of /app/data/server.log over HTTP,
protected by LOG_API_KEY.  Tests use a temporary file and monkeypatching
so no real log file is needed.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest


GOOD_KEY = "test-log-key-secret-abc123"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def log_file(tmp_path) -> Path:
    """A temporary file with 10 lines that stands in for /app/data/server.log."""
    f = tmp_path / "server.log"
    lines = [f"INFO line {i}: something happened\n" for i in range(10)]
    f.write_text("".join(lines))
    return f


@pytest.fixture()
def client_with_log(client, log_file, monkeypatch):
    """TestClient where _LOG_FILE points at the temp file and LOG_API_KEY is set."""
    import server
    monkeypatch.setattr(server, "_LOG_FILE", log_file)
    monkeypatch.setenv("LOG_API_KEY", GOOD_KEY)
    return client


# ── Auth / access control ─────────────────────────────────────────────────────

class TestLogsAuth:
    def test_no_key_returns_403(self, client, log_file, monkeypatch):
        """Request without ?key returns 403 even when LOG_API_KEY is configured."""
        import server
        monkeypatch.setattr(server, "_LOG_FILE", log_file)
        monkeypatch.setenv("LOG_API_KEY", GOOD_KEY)
        r = client.get("/api/logs")
        assert r.status_code == 403

    def test_wrong_key_returns_403(self, client, log_file, monkeypatch):
        """Wrong key value returns 403."""
        import server
        monkeypatch.setattr(server, "_LOG_FILE", log_file)
        monkeypatch.setenv("LOG_API_KEY", GOOD_KEY)
        r = client.get("/api/logs?key=totally-wrong-key")
        assert r.status_code == 403

    def test_env_var_not_set_returns_403(self, client, log_file, monkeypatch):
        """If LOG_API_KEY is absent in env, every request is rejected."""
        import server
        monkeypatch.setattr(server, "_LOG_FILE", log_file)
        monkeypatch.delenv("LOG_API_KEY", raising=False)
        r = client.get(f"/api/logs?key={GOOD_KEY}")
        assert r.status_code == 403

    def test_correct_key_returns_200(self, client_with_log):
        """Correct key returns 200 JSON."""
        r = client_with_log.get(f"/api/logs?key={GOOD_KEY}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")


# ── File not found ────────────────────────────────────────────────────────────

class TestLogsFileNotFound:
    def test_missing_log_file_returns_404(self, client, tmp_path, monkeypatch):
        """If the log file doesn't exist yet, /api/logs returns 404."""
        import server
        monkeypatch.setattr(server, "_LOG_FILE", tmp_path / "nonexistent.log")
        monkeypatch.setenv("LOG_API_KEY", GOOD_KEY)
        r = client.get(f"/api/logs?key={GOOD_KEY}")
        assert r.status_code == 404


# ── Response content ──────────────────────────────────────────────────────────

class TestLogsContent:
    def test_response_has_required_keys(self, client_with_log):
        """JSON body always contains total_lines, returned, log_size_kb, lines."""
        r = client_with_log.get(f"/api/logs?key={GOOD_KEY}")
        body = r.json()
        for key in ("total_lines", "returned", "log_size_kb", "lines"):
            assert key in body, f"missing key: {key}"

    def test_lines_is_list(self, client_with_log):
        r = client_with_log.get(f"/api/logs?key={GOOD_KEY}")
        assert isinstance(r.json()["lines"], list)

    def test_returned_matches_lines_length(self, client_with_log):
        r = client_with_log.get(f"/api/logs?key={GOOD_KEY}")
        body = r.json()
        assert body["returned"] == len(body["lines"])

    def test_default_returns_all_ten_lines(self, client_with_log):
        """With n defaulting to 300 and only 10 lines in the file, all 10 returned."""
        r = client_with_log.get(f"/api/logs?key={GOOD_KEY}")
        body = r.json()
        assert body["total_lines"] == 10
        assert body["returned"] == 10
        assert len(body["lines"]) == 10

    def test_n_param_limits_returned_lines(self, client_with_log):
        """?n=3 returns only the last 3 lines."""
        r = client_with_log.get(f"/api/logs?key={GOOD_KEY}&n=3")
        body = r.json()
        assert body["returned"] == 3
        assert len(body["lines"]) == 3
        # The last 3 lines should be lines 7, 8, 9
        assert "line 9" in body["lines"][-1]
        assert "line 7" in body["lines"][0]

    def test_lines_contain_log_content(self, client_with_log):
        """Each returned line is a non-empty string from the log."""
        r = client_with_log.get(f"/api/logs?key={GOOD_KEY}&n=5")
        for line in r.json()["lines"]:
            assert isinstance(line, str)
            assert len(line) > 0

    def test_log_size_kb_is_nonnegative(self, client_with_log):
        r = client_with_log.get(f"/api/logs?key={GOOD_KEY}")
        assert r.json()["log_size_kb"] >= 0

    def test_n_larger_than_file_returns_all_lines(self, client_with_log):
        """Requesting more lines than exist returns the full file."""
        r = client_with_log.get(f"/api/logs?key={GOOD_KEY}&n=9999")
        body = r.json()
        assert body["total_lines"] == body["returned"]

    def test_large_file_tail_read(self, client, tmp_path, monkeypatch):
        """When file exceeds _MAX_LOG_BYTES, only the tail is read without error."""
        import server
        large_log = tmp_path / "big.log"
        # Write content larger than the 10 MB cap by patching _MAX_LOG_BYTES
        content = "A" * 100 + "\n"
        large_log.write_text(content * 50)  # 50 lines
        monkeypatch.setattr(server, "_LOG_FILE", large_log)
        monkeypatch.setattr(server, "_MAX_LOG_BYTES", 200)  # tiny cap
        monkeypatch.setenv("LOG_API_KEY", GOOD_KEY)
        r = client.get(f"/api/logs?key={GOOD_KEY}&n=5")
        assert r.status_code == 200
        assert len(r.json()["lines"]) <= 5
