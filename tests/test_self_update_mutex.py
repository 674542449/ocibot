"""The update mutex must stay armed until the host-side helper is really done.

`_apply_job` used to write state="success" *before* launching the container that
performs the build and restart. `docker run -d` returns in seconds, so the worker
thread then exited and cleared `_worker` while `install.sh update` kept running on
the host for another 1-5 minutes. Both mutexes only reject the literal string
"running" — the in-process one and the `SELECT … FOR UPDATE` re-check — and the
SPA also treats anything other than "running" as finished, so it re-enabled the
button. A second apply's first action is an unconditional
`docker rm -f ocibot-self-update-restart`, which SIGKILLs the live
`--privileged --pid=host` helper in the middle of `compose up -d` (db, then api,
then worker) while git-resetting the build context it is reading. Killed between
those steps it can leave the API container destroyed and never recreated: the
panel is unreachable and recovery needs SSH.

These tests pin the behaviour, not the mechanism: the status row must not go
terminal while the helper container is alive, and an apply must be refused while
one is running.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("OCIBOT_MASTER_KEY", "mutex-master-key-0123456789abcd")
os.environ.setdefault("OCIBOT_JWT_SECRET", "mutex-jwt-secret-0123456789abcd")

from web.backend import self_update  # noqa: E402

KEY = self_update.KEY_UPDATE_STATUS
# Spelled out rather than imported: this name is the operator-visible handle on a
# live update (`docker logs -f`), and the tests below drive the real
# `docker inspect` argv, not a module internal.
HELPER = "ocibot-self-update-restart"


class _FakeSession:
    """Stands in for both a request session and the worker's SessionLocal()."""

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


def _caps() -> dict:
    return {
        "enabled": True,
        "can_apply": True,
        "host_repo_on_host": "/root/ocibot",
        "compose_via": "container:docker:27-cli",
    }


def _stub_apply_env(monkeypatch, tmp_path, store: dict[str, str], run_cmd=None) -> None:
    import web.backend.db as db_module

    monkeypatch.setenv("OCIBOT_HOST_REPO", "/root/ocibot")
    monkeypatch.setattr(db_module, "SessionLocal", lambda: _FakeSession(store))
    _patch_meta(monkeypatch, store)
    monkeypatch.setattr(self_update, "_load_dotenv_into_environ", lambda _p: {})
    monkeypatch.setattr(self_update, "_host_dir", lambda: tmp_path)
    monkeypatch.setattr(self_update, "_host_repo_on_host", lambda: "/root/ocibot")
    monkeypatch.setattr(self_update, "_project_name", lambda _host: "ocibot")
    monkeypatch.setattr(self_update, "_disk_free_gb", lambda path="/": 50.0)
    monkeypatch.setattr(self_update, "capabilities", _caps)
    monkeypatch.setattr(self_update, "_branch", lambda: "main")
    # The wait loop sleeps between polls; tests must not.
    monkeypatch.setattr(self_update.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(self_update, "_run_cmd", run_cmd or (lambda cmd, **kw: (0, "abc1234")))


def test_state_stays_running_until_the_helper_container_exits(monkeypatch, tmp_path):
    store: dict[str, str] = {}
    probes: list[tuple[str, str]] = []

    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "inspect"]:
            probes.append((cmd[-1], _row(store).get("state") or ""))
            # Still building for the first two polls, then a clean exit.
            return (0, "running 0\n") if len(probes) < 3 else (0, "exited 0\n")
        return 0, "abc1234"

    _stub_apply_env(monkeypatch, tmp_path, store, run_cmd=fake_run)

    seen: dict[str, str] = {}

    def fake_detach(host, host_repo, project, new_sha):
        seen["state_at_launch"] = _row(store).get("state") or ""
        return 0, "container started"

    monkeypatch.setattr(self_update, "_detach_stack_restart", fake_detach)

    self_update._apply_job("admin")

    # The row must already say "running" when the privileged helper is launched…
    assert seen["state_at_launch"] == "running"
    # …and must keep saying so for as long as that helper is alive.
    assert probes, "the job never looked at the helper container it started"
    assert {name for name, _ in probes} == {HELPER}
    assert [state for _n, state in probes] == ["running", "running", "running"]

    row = _row(store)
    assert row["state"] == "success"
    assert row["applied_sha"] == "abc1234"
    assert row["finished_at"]


def test_a_failed_helper_becomes_error_not_success(monkeypatch, tmp_path):
    store: dict[str, str] = {}

    def run_cmd(cmd, **kw):
        if cmd[:2] == ["docker", "inspect"]:
            return 0, "exited 7\n"
        if cmd[:2] == ["docker", "logs"]:
            return 0, "compose build failed: no space left on device"
        return 0, "abc1234"

    _stub_apply_env(monkeypatch, tmp_path, store, run_cmd=run_cmd)
    monkeypatch.setattr(
        self_update, "_detach_stack_restart", lambda *a, **k: (0, "container started")
    )

    self_update._apply_job("admin")

    row = _row(store)
    assert row["state"] == "error"
    assert "7" in row["last_error"]
    assert "no space left on device" in row["log_tail"]


def test_apply_is_refused_while_a_helper_container_is_still_running(monkeypatch):
    """The row is not the only mutex: the live container is refused on its own.

    Covers the window where the row has been cleared (stale-running recovery, a
    wiped app_meta) while the previous helper is still rewriting the stack.
    """
    store: dict[str, str] = {}
    _patch_meta(monkeypatch, store)
    db = _FakeSession(store)

    monkeypatch.setenv("OCIBOT_HOST_REPO", "/root/ocibot")
    monkeypatch.setattr(self_update, "update_enabled", lambda: True)
    monkeypatch.setattr(self_update, "_load_dotenv_into_environ", lambda _p: {})
    monkeypatch.setattr(self_update, "_host_repo_on_host", lambda: "/root/ocibot")
    monkeypatch.setattr(self_update, "capabilities", _caps)
    monkeypatch.setattr(
        self_update,
        "fetch_remote_head",
        lambda timeout=15.0: {"sha": "b" * 40, "short_sha": "bbbbbbb", "message": "x"},
    )

    def fake_run(cmd, **kw):
        # Emulate the real `docker inspect -f '{{.State.Status}} {{.State.ExitCode}}'`.
        if cmd[:2] == ["docker", "inspect"]:
            return 0, "running 0\n"
        return 0, ""

    monkeypatch.setattr(self_update, "_run_cmd", fake_run)

    with pytest.raises(RuntimeError, match="已有更新任务正在进行"):
        self_update.start_update(db, username="admin")

    assert not (self_update._worker and self_update._worker.is_alive())
    # Nothing was written, so the in-flight update's own bookkeeping is untouched.
    assert store == {}


def test_get_status_keeps_running_while_the_helper_is_alive(monkeypatch):
    """Cross-process / post-restart: the worker thread rarely survives its own
    update, so whoever polls next must read the container, and the stale sweep
    must not declare a slow cold build dead."""
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store = {
        KEY: json.dumps(
            {
                "state": "running",
                "helper_container": HELPER,
                "applied_sha": "abc1234",
                "started_at": two_hours_ago,
                "message": "正在宿主机执行 install.sh update…",
            }
        )
    }
    _patch_meta(monkeypatch, store)
    monkeypatch.setattr(self_update, "capabilities", _caps)
    monkeypatch.setattr(
        self_update,
        "local_build_info",
        lambda: {"app_version": "0.4.x", "git_sha": "old9999", "repo": "x/y", "branch": "main"},
    )
    monkeypatch.setattr(
        self_update,
        "_run_cmd",
        lambda cmd, **kw: (0, "running 0\n") if cmd[:2] == ["docker", "inspect"] else (0, ""),
    )

    st = self_update.get_status(_FakeSession(store))
    assert st["state"] == "running"
    assert _row(store)["state"] == "running"


@pytest.mark.parametrize(
    ("inspect_out", "expected_state"),
    [("exited 0\n", "success"), ("exited 5\n", "error")],
)
def test_get_status_finishes_a_running_row_from_the_container(
    monkeypatch, inspect_out, expected_state
):
    store = {
        KEY: json.dumps(
            {
                "state": "running",
                "helper_container": HELPER,
                "applied_sha": "abc1234",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "log_tail": "",
            }
        )
    }
    _patch_meta(monkeypatch, store)
    monkeypatch.setattr(self_update, "capabilities", _caps)
    monkeypatch.setattr(
        self_update,
        "local_build_info",
        lambda: {"app_version": "0.4.x", "git_sha": "abc1234", "repo": "x/y", "branch": "main"},
    )

    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "inspect"]:
            return 0, inspect_out
        if cmd[:2] == ["docker", "logs"]:
            return 0, "exit 5 detail"
        return 0, ""

    monkeypatch.setattr(self_update, "_run_cmd", fake_run)

    st = self_update.get_status(_FakeSession(store))
    assert st["state"] == expected_state
    assert _row(store)["state"] == expected_state
    if expected_state == "error":
        assert "5" in st["last_error"]


def test_a_fresh_apply_does_not_inherit_the_previous_helper_name(monkeypatch):
    """Otherwise the new run is reconciled against the last run's exit code and
    is reported finished before its own helper has even been launched."""
    store = {
        KEY: json.dumps(
            {
                "state": "success",
                "helper_container": HELPER,
                "applied_sha": "old1111",
                "started_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            }
        )
    }
    _patch_meta(monkeypatch, store)
    db = _FakeSession(store)

    monkeypatch.setenv("OCIBOT_HOST_REPO", "/root/ocibot")
    monkeypatch.setattr(self_update, "_worker", None)
    monkeypatch.setattr(self_update, "update_enabled", lambda: True)
    monkeypatch.setattr(self_update, "_load_dotenv_into_environ", lambda _p: {})
    monkeypatch.setattr(self_update, "_host_repo_on_host", lambda: "/root/ocibot")
    monkeypatch.setattr(self_update, "capabilities", _caps)
    monkeypatch.setattr(self_update, "fetch_remote_head", lambda timeout=15.0: None)
    monkeypatch.setattr(
        self_update,
        "_run_cmd",
        lambda cmd, **kw: (0, "exited 0\n") if cmd[:2] == ["docker", "inspect"] else (0, ""),
    )
    monkeypatch.setattr(
        self_update,
        "local_build_info",
        lambda: {"app_version": "0.4.x", "git_sha": "old1111", "repo": "x/y", "branch": "main"},
    )
    # Do not actually run an update: assert on the row the queueing step wrote.
    started: list[str] = []
    monkeypatch.setattr(self_update, "_apply_job", lambda username: started.append(username))

    self_update.start_update(db, username="admin")
    if self_update._worker is not None:
        self_update._worker.join(timeout=5)

    assert started == ["admin"]
    assert _row(store).get("helper_container") in ("", None)
