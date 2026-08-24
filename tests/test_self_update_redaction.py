"""Everything the update path shows an admin is redacted, not just the log.

Pass 9 wired `_redact` into `_append_log` only. `message` and `last_error` were
written straight from `git fetch` / `docker compose` stdout+stderr, so the *same*
failure rendered `***OCIBOT_MASTER_KEY***` in `log_tail` and the raw value in
`last_error` — both of which are persisted in `app_meta` and returned by
`GET /api/admin/update`.

The realistic leak is the git remote: the updater supports a GitHub PAT, and on a
private-repo install the token is commonly baked into the remote URL, so any
network blip makes git print
`fatal: unable to access 'https://ghp_xxxx@github.com/owner/ocibot.git/'`. That
token need not be in the environment at all, so matching known env values is not
sufficient.
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("OCIBOT_MASTER_KEY", "redact-master-key-0123456789abc")
os.environ.setdefault("OCIBOT_JWT_SECRET", "redact-jwt-secret-0123456789abc")

from web.backend import self_update  # noqa: E402

KEY = self_update.KEY_UPDATE_STATUS
TOKEN = "ghp_0123456789abcdefghijABCDEFGHIJ0123"


class _FakeSession:
    def __init__(self, store: dict[str, str]):
        self.store = store

    def commit(self):
        return None

    def rollback(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_meta(monkeypatch, store: dict[str, str]) -> None:
    monkeypatch.setattr(self_update, "get_meta", lambda _db, key: store.get(key))
    monkeypatch.setattr(
        self_update, "set_meta", lambda _db, key, value: store.__setitem__(key, value)
    )


def _row(store: dict[str, str]) -> dict:
    return json.loads(store.get(KEY) or "{}")


def test_check_for_update_failure_does_not_persist_or_return_the_token(monkeypatch):
    monkeypatch.setenv("OCIBOT_GITHUB_TOKEN", TOKEN)
    store: dict[str, str] = {}
    _patch_meta(monkeypatch, store)

    def boom(timeout=15.0):
        raise RuntimeError(
            f"fatal: unable to access 'https://{TOKEN}@github.com/owner/ocibot.git/': 502"
        )

    monkeypatch.setattr(self_update, "fetch_remote_head", boom)

    with pytest.raises(RuntimeError) as excinfo:
        self_update.check_for_update(_FakeSession(store))

    row = _row(store)
    assert TOKEN not in row["last_error"]
    assert TOKEN not in row["message"]
    # The router turns the raised error into the 502 detail — the one copy that
    # does not pass through _write_status.
    assert TOKEN not in str(excinfo.value)


def test_git_fetch_failure_redacts_last_error_and_log_tail_alike(monkeypatch, tmp_path):
    """The asymmetry this pins: log_tail was scrubbed, last_error was not."""
    import web.backend.db as db_module

    monkeypatch.setenv("OCIBOT_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("OCIBOT_HOST_REPO", "/root/ocibot")
    store: dict[str, str] = {}
    monkeypatch.setattr(db_module, "SessionLocal", lambda: _FakeSession(store))
    _patch_meta(monkeypatch, store)
    monkeypatch.setattr(self_update, "_load_dotenv_into_environ", lambda _p: {})
    monkeypatch.setattr(self_update, "_host_dir", lambda: tmp_path)
    monkeypatch.setattr(self_update, "_host_repo_on_host", lambda: "/root/ocibot")
    monkeypatch.setattr(self_update, "_disk_free_gb", lambda path="/": 50.0)
    monkeypatch.setattr(self_update, "_branch", lambda: "main")
    monkeypatch.setattr(
        self_update,
        "capabilities",
        lambda: {"enabled": True, "can_apply": True, "host_repo_on_host": "/root/ocibot"},
    )

    failure = f"fatal: unable to access 'https://{TOKEN}@github.com/owner/ocibot.git/': 502"

    def fake_run(cmd, **kw):
        if "fetch" in cmd:
            return 1, failure
        return 0, "abc1234"

    monkeypatch.setattr(self_update, "_run_cmd", fake_run)

    self_update._apply_job("admin")

    row = _row(store)
    assert row["state"] == "error"
    assert TOKEN not in row["last_error"]
    assert TOKEN not in row["log_tail"]
    assert TOKEN not in row["message"]
    # Still diagnosable: the failure itself survives redaction.
    assert "github.com/owner/ocibot.git" in row["last_error"]


def test_redact_masks_credentials_baked_into_a_url(monkeypatch):
    """The token in the git remote is not necessarily any env var's value."""
    monkeypatch.delenv("OCIBOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    out = self_update._redact(
        "fatal: unable to access 'https://ghp_AAAABBBBCCCCDDDDEEEEFFFF@github.com/o/ocibot.git/'"
    )
    assert "ghp_AAAABBBBCCCCDDDDEEEEFFFF" not in out
    assert "github.com/o/ocibot.git" in out


def test_redact_masks_a_password_inside_a_dsn(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    out = self_update._redact("could not connect to postgresql://ocibot:sup3rs3cretpw@db:5432/x")
    assert "sup3rs3cretpw" not in out
    assert "db:5432/x" in out


def test_redact_leaves_ordinary_output_intact():
    """A regex that eats normal build output would be worse than the leak."""
    line = "Successfully tagged ocibot-api:latest\nhttps://github.com/674542449/ocibot/commit/abc\n"
    assert self_update._redact(line) == line


def test_status_message_is_redacted_at_the_single_write_funnel(monkeypatch):
    """A future save() call site cannot forget: the funnel scrubs, not the caller."""
    monkeypatch.setenv("OCIBOT_MASTER_KEY", "master-key-that-is-long-enough")
    store: dict[str, str] = {}
    _patch_meta(monkeypatch, store)
    self_update._write_status(
        _FakeSession(store),
        {
            "state": "error",
            "message": "boom OCIBOT_MASTER_KEY=master-key-that-is-long-enough",
            "last_error": "boom master-key-that-is-long-enough",
        },
    )
    row = _row(store)
    assert "master-key-that-is-long-enough" not in row["message"]
    assert "master-key-that-is-long-enough" not in row["last_error"]
