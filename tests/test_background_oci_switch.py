"""Master switch for the worker's background Oracle calls.

Requested by the operator: "turn it all off, stop making requests". The panel
should touch OCI only while somebody is using it.

The important property is that turning it off must be VISIBLE. Capacity retry and
power schedules are implemented BY those background calls, so with the switch off
they do not fail — they simply never fire, and a job stuck at "idle" forever with no
explanation is worse than one that errors.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_bgswitch_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'b.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "bgswitch-master-key-0123456789ab")
os.environ.setdefault("OCIBOT_JWT_SECRET", "bgswitch-jwt-secret-0123456789ab")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend import worker as worker_mod  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.config import Settings  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import User  # noqa: E402


def _phases(background: bool) -> list[str]:
    """The phase list run_forever would build, without entering its loop."""
    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.settings = Settings(OCIBOT_WORKER_BACKGROUND_OCI=background)  # type: ignore[call-arg]
    w.worker_id = "test"
    names: list[str] = []

    def fake_sleep(_seconds: float) -> None:
        raise KeyboardInterrupt  # stop after the first pass

    original_sleep = worker_mod.time.sleep
    original_init = worker_mod.init_db
    original_session = worker_mod.SessionLocal
    worker_mod.time.sleep = fake_sleep  # type: ignore[assignment]
    worker_mod.init_db = lambda: None  # type: ignore[assignment]

    class _NullSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def commit(self):
            return None

    worker_mod.SessionLocal = lambda: _NullSession()  # type: ignore[assignment]
    for name in ("beat", "tick_schedules", "tick_capacity", "tick_daily_checks"):
        setattr(w, name, (lambda n: lambda _db: names.append(n))(name))
    try:
        w.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker_mod.time.sleep = original_sleep  # type: ignore[assignment]
        worker_mod.init_db = original_init  # type: ignore[assignment]
        worker_mod.SessionLocal = original_session  # type: ignore[assignment]
    return names


def test_switch_on_runs_every_phase():
    assert _phases(True) == ["beat", "tick_schedules", "tick_capacity", "tick_daily_checks"]


def test_switch_off_leaves_only_the_heartbeat():
    """Everything except beat calls Oracle; beat writes to the database only, and
    keeping it means the panel still reports the worker as online rather than
    broken."""
    assert _phases(False) == ["beat"]


def test_default_keeps_background_work_enabled():
    """Off by default would silently break capacity retry for every existing
    install that pulls this version."""
    assert Settings().worker_background_oci is True


def test_status_endpoint_reports_the_switch():
    """The UI needs this to explain a job that never runs."""
    init_db()
    name = "bgswitch-user"
    with SessionLocal() as db:
        row = db.query(User).filter(User.username == name).one_or_none()
        if row is None:
            db.add(User(username=name, password_hash=hash_password("supersecret123")))
        else:
            row.password_hash = hash_password("supersecret123")
            row.is_active = True
            row.totp_enabled = False
        db.commit()
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"username": name, "password": "supersecret123"}).status_code
            == 200
        )
        body = c.get("/api/system/status").json()
    assert "background_oci" in body
    assert body["background_oci"] is True
