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
    monkeypatch.setenv("OCIBOT_MASTER_KEY", "real-key")
    flags = self_update._compose_env_flags("/root/ocibot")
    joined = " ".join(flags)
    assert "POSTGRES_PASSWORD=" not in joined
    assert "OCIBOT_MASTER_KEY=real-key" in joined
    assert "OCIBOT_HOST_REPO=/root/ocibot" in joined


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
    assert "force-recreate worker" in text
    assert "force-recreate api" in text
    assert "up -d" in text
    assert "deadbeef" in text


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
