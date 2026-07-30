"""Audit log retention.

Audit pass 10. The login audit added in 0.4.34 records failed attempts, including
attempts against usernames that do not exist — so *unauthenticated* traffic writes
rows to this table and the attacker picks how many. There was no ceiling of any
kind: a credential-stuffing run against an internet-exposed panel grew the table
until the disk filled, which takes Postgres and the whole stack with it.

Pruning lives in the worker heartbeat, which touches the database only — so it
still runs with OCIBOT_WORKER_BACKGROUND_OCI=0, where Oracle is off limits.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_audret_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'r.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "audret-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "audret-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from web.backend.audit import prune_audit_log, write_audit  # noqa: E402
from web.backend.config import Settings  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import AuditLog  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with SessionLocal() as db:
        db.query(AuditLog).delete()
        db.commit()
    yield


def _seed(count: int, *, age_days: float = 0.0) -> None:
    created = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)
    with SessionLocal() as db:
        for i in range(count):
            db.add(
                AuditLog(
                    owner_id=None,
                    action="auth.login_failed",
                    target=f"victim-{i}",
                    detail="{}",
                    # Distinct timestamps so the row-cap boundary is unambiguous.
                    created_at=created + timedelta(microseconds=i),
                )
            )
        db.commit()


def _count() -> int:
    with SessionLocal() as db:
        return db.query(AuditLog).count()


def test_rows_past_the_window_are_deleted():
    _seed(5, age_days=400)
    _seed(3, age_days=1)
    with SessionLocal() as db:
        removed = prune_audit_log(db, retention_days=180, max_rows=0)
    assert removed == 5
    assert _count() == 3


def test_recent_rows_survive():
    _seed(4, age_days=1)
    with SessionLocal() as db:
        assert prune_audit_log(db, retention_days=180, max_rows=0) == 0
    assert _count() == 4


def test_row_cap_bounds_a_burst_inside_the_window():
    """The retention window alone does not stop a flood: every row can be minutes
    old and still fill the disk. This is the limit that actually holds."""
    _seed(50)
    with SessionLocal() as db:
        removed = prune_audit_log(db, retention_days=180, max_rows=20)
    assert removed == 30
    assert _count() == 20


def test_row_cap_keeps_the_newest_rows():
    _seed(30)
    with SessionLocal() as db:
        prune_audit_log(db, retention_days=0, max_rows=10)
    with SessionLocal() as db:
        kept = {r.target for r in db.query(AuditLog).all()}
    assert kept == {f"victim-{i}" for i in range(20, 30)}


def test_zero_disables_each_limit():
    """An operator who wants an unbounded log must be able to say so."""
    _seed(5, age_days=400)
    with SessionLocal() as db:
        assert prune_audit_log(db, retention_days=0, max_rows=0) == 0
    assert _count() == 5


def test_under_the_cap_does_no_work():
    _seed(3)
    with SessionLocal() as db:
        assert prune_audit_log(db, retention_days=0, max_rows=1000) == 0
    assert _count() == 3


def test_empty_table_is_fine():
    with SessionLocal() as db:
        assert prune_audit_log(db, retention_days=180, max_rows=100) == 0


def test_defaults_are_bounded():
    """A default of 0/unlimited would leave every existing install exposed."""
    s = Settings()
    assert s.audit_retention_days > 0
    assert s.audit_max_rows > 0


def test_heartbeat_prunes_without_touching_oracle():
    """The prune must ride on beat, not on a capacity/OCI phase, or it would stop
    happening exactly when the operator turns background Oracle calls off."""
    from web.backend import worker as worker_mod

    _seed(40)
    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.settings = Settings(OCIBOT_AUDIT_MAX_ROWS=10, OCIBOT_WORKER_BACKGROUND_OCI=False)  # type: ignore[call-arg]
    w.worker_id = "prune-test"
    with SessionLocal() as db:
        w.beat(db)
    assert _count() == 10


def test_prune_is_throttled():
    """beat fires every few seconds; a COUNT(*) that often is pure waste."""
    from web.backend import worker as worker_mod

    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.settings = Settings(OCIBOT_AUDIT_MAX_ROWS=10)  # type: ignore[call-arg]
    w.worker_id = "prune-test"
    _seed(40)
    with SessionLocal() as db:
        w.beat(db)
    assert _count() == 10
    # Second beat within the hour must not re-scan; new rows stay until it lapses.
    _seed(40)
    with SessionLocal() as db:
        w.beat(db)
    assert _count() == 50


def test_write_audit_still_works_after_pruning():
    """Regression guard: the prune runs in the worker's session, and a botched
    commit there could poison later writes."""
    _seed(30)
    with SessionLocal() as db:
        prune_audit_log(db, retention_days=0, max_rows=5)
        write_audit(db, owner_id=None, action="auth.login", target="after")
    with SessionLocal() as db:
        assert db.query(AuditLog).filter(AuditLog.target == "after").count() == 1
