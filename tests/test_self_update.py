"""Tests for admin self-update helpers (no docker required)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from web.backend import self_update


class _FakeDB:
    def __init__(self):
        self.store: dict[str, str] = {}

    def commit(self):
        return None

    def rollback(self):
        return None


def test_get_status_idle(monkeypatch):
    db = _FakeDB()

    def fake_get(db, key):
        return db.store.get(key)

    def fake_set(db, key, value):
        db.store[key] = value

    monkeypatch.setattr(self_update, "get_meta", fake_get)
    monkeypatch.setattr(self_update, "set_meta", fake_set)
    monkeypatch.setattr(self_update, "update_enabled", lambda: True)
    monkeypatch.setattr(
        self_update,
        "local_build_info",
        lambda: {"app_version": "0.1.0", "git_sha": "abc1234", "repo": "674542449/ocibot", "branch": "main"},
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
            "can_apply": True,
        },
    )

    st = self_update.get_status(db)  # type: ignore[arg-type]
    assert st["state"] == "idle"
    assert st["local"]["git_sha"] == "abc1234"
    assert st["capabilities"]["can_apply"] is True


def test_check_for_update_sets_remote(monkeypatch):
    db = _FakeDB()

    def fake_get(db, key):
        return db.store.get(key)

    def fake_set(db, key, value):
        db.store[key] = value

    monkeypatch.setattr(self_update, "get_meta", fake_get)
    monkeypatch.setattr(self_update, "set_meta", fake_set)
    monkeypatch.setattr(self_update, "update_enabled", lambda: True)
    monkeypatch.setattr(
        self_update,
        "local_build_info",
        lambda: {"app_version": "0.1.0", "git_sha": "aaa1111", "repo": "x/y", "branch": "main"},
    )
    monkeypatch.setattr(
        self_update,
        "capabilities",
        lambda: {"enabled": True, "can_apply": False},
    )
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
