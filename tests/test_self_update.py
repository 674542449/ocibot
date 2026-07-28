"""Tests for admin self-update helpers (no docker required)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from web.backend import self_update


class _FakeDB:
    def __init__(self):
        self.store: dict[str, str] = {}

    def commit(self):
        return None

    def rollback(self):
        return None


def _patch_meta(monkeypatch, db: _FakeDB):
    def fake_get(_db, key):
        return db.store.get(key)

    def fake_set(_db, key, value):
        db.store[key] = value

    monkeypatch.setattr(self_update, "get_meta", fake_get)
    monkeypatch.setattr(self_update, "set_meta", fake_set)


def test_get_status_idle(monkeypatch):
    db = _FakeDB()
    _patch_meta(monkeypatch, db)
    monkeypatch.setattr(self_update, "update_enabled", lambda: True)
    monkeypatch.setattr(
        self_update,
        "local_build_info",
        lambda: {
            "app_version": "0.4.5",
            "git_sha": "abc1234",
            "repo": "674542449/ocibot",
            "branch": "main",
        },
    )
    monkeypatch.setattr(
        self_update,
        "capabilities",
        lambda: {
            "enabled": True,
            "host_dir": "/host/ocibot",
            "host_dir_exists": True,
            "compose_file_exists": True,
            "docker_sock": True,
            "docker_bin": True,
            "git_bin": True,
            "docker_daemon": True,
            "host_repo_on_host": "/root/ocibot",
            "compose_via": "container:docker:27-cli",
            "can_apply": True,
        },
    )
    st = self_update.get_status(db)  # type: ignore[arg-type]
    assert st["state"] == "idle"
    assert st["local"]["git_sha"] == "abc1234"
    assert st["capabilities"]["can_apply"] is True


def test_check_for_update_sets_remote(monkeypatch):
    db = _FakeDB()
    _patch_meta(monkeypatch, db)
    monkeypatch.setattr(self_update, "update_enabled", lambda: True)
    monkeypatch.setattr(
        self_update,
        "local_build_info",
        lambda: {"app_version": "0.4.5", "git_sha": "aaa1111", "repo": "x/y", "branch": "main"},
    )
    monkeypatch.setattr(self_update, "capabilities", lambda: {"enabled": True, "can_apply": False})
    monkeypatch.setattr(
        self_update,
        "fetch_remote_head",
        lambda timeout=15.0: {
            "sha": "bbb2222ffffffff",
            "short_sha": "bbb2222",
            "message": "fix stuff",
            "date": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/x/y/commit/bbb",
            "repo": "x/y",
            "branch": "main",
        },
    )
    st = self_update.check_for_update(db)  # type: ignore[arg-type]
    assert st["remote"]["short_sha"] == "bbb2222"
    assert st["update_available"] is True
    raw = json.loads(db.store[self_update.KEY_UPDATE_STATUS])
    assert raw["remote"]["sha"].startswith("bbb2222")


def test_start_update_requires_enabled(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(self_update, "update_enabled", lambda: False)
    with pytest.raises(RuntimeError, match="未启用"):
        self_update.start_update(db, username="admin")  # type: ignore[arg-type]


def test_humanize_build_error_disk():
    tip = self_update._humanize_build_error("ERROR: no space left on device")
    assert "磁盘" in tip


def test_host_repo_prefers_absolute_env(monkeypatch):
    monkeypatch.setenv("OCIBOT_HOST_REPO", "/root/ocibot")
    assert self_update._host_repo_on_host() == "/root/ocibot"


def test_compose_env_flags_skip_empty_secrets(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "")
    monkeypatch.setenv("OCIBOT_MASTER_KEY", "super-secret-master-key-value")
    flags = self_update._compose_env_flags("/root/ocibot")
    joined = " ".join(flags)
    # An empty secret is still skipped: injecting KEY= would override the value
    # the compose file would otherwise default.
    assert "POSTGRES_PASSWORD" not in joined
    # The secret VALUE must not reach the argv. This assertion previously read
    # `"OCIBOT_MASTER_KEY=real-key" in joined` — it encoded the defect: argv is
    # visible in the host process table and _run_cmd logs the command, so the key
    # that decrypts every stored OCI private key was written to the API log on
    # every update. Docker takes `-e KEY` (no value) from the CLI's own
    # environment, which _run_cmd already forwards.
    assert "super-secret-master-key-value" not in joined
    assert flags[flags.index("OCIBOT_MASTER_KEY") - 1] == "-e"
    # Computed, non-secret values are still passed explicitly.
    assert "OCIBOT_HOST_REPO=/root/ocibot" in joined


def test_run_cmd_log_line_redacts_secrets(monkeypatch, caplog):
    """_run_cmd logs what it runs; that line must never carry a secret."""
    import logging

    monkeypatch.setenv("OCIBOT_MASTER_KEY", "super-secret-master-key-value")
    with caplog.at_level(logging.INFO, logger="ocibot.update"):
        self_update._run_cmd(
            ["echo", "OCIBOT_MASTER_KEY=super-secret-master-key-value"], timeout=10
        )
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "super-secret-master-key-value" not in logged
    assert "***OCIBOT_MASTER_KEY***" in logged


def test_admin_visible_log_redacts_secrets(monkeypatch):
    """log_tail is persisted and returned by GET /api/admin/update, and command
    output can echo an interpolated value back."""
    monkeypatch.setenv("OCIBOT_JWT_SECRET", "jwt-secret-value-long-enough")
    tail = self_update._append_log("", "error: OCIBOT_JWT_SECRET=jwt-secret-value-long-enough\n")
    assert "jwt-secret-value-long-enough" not in tail
    assert "***OCIBOT_JWT_SECRET***" in tail


def test_redact_leaves_short_values_alone(monkeypatch):
    """Replacing a 3-character secret would corrupt unrelated output for no gain."""
    monkeypatch.setenv("OCIBOT_MASTER_KEY", "abc")
    assert self_update._redact("abcdef") == "abcdef"


def test_compose_base_args_include_env_file(tmp_path, monkeypatch):
    host = tmp_path / "host"
    (host / "web").mkdir(parents=True)
    (host / "web" / ".env").write_text("POSTGRES_PASSWORD=x\n", encoding="utf-8")
    monkeypatch.setattr(self_update, "_host_dir", lambda: host)
    args = self_update._compose_base_args("/root/ocibot", "ocibot")
    assert "--env-file" in args
    assert "/root/ocibot/web/.env" in args


def test_write_restart_script_contains_recovery(tmp_path):
    host = tmp_path / "repo"
    host.mkdir()
    path = self_update._write_restart_script(host, "/root/ocibot", "ocibot", "deadbeef")
    text = path.read_text(encoding="utf-8")
    assert "docker compose" in text
    assert "build" in text
    assert "up -d" in text
    assert "deadbeef" in text
    assert "OCIBOT_SKIP_GIT=1" in text


def test_recover_stale_running():
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    st = {"state": "running", "started_at": old, "message": "updating"}
    out = self_update._recover_stale_running(st, max_age_sec=60, worker_alive=False)
    assert out["state"] == "error"
    assert out["last_error"] == "stale_running"

    fresh = {"state": "running", "started_at": datetime.now(timezone.utc).isoformat()}
    still = self_update._recover_stale_running(fresh, max_age_sec=3600, worker_alive=False)
    assert still["state"] == "running"

    alive = self_update._recover_stale_running(st, max_age_sec=0, worker_alive=True)
    assert alive["state"] == "running"
