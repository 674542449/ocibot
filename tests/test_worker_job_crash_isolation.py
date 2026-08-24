"""One broken capacity job must not stall every other job in the installation.

The per-job body in ``Worker.tick_capacity`` used to be ``try/finally`` with no
``except``. ``_run_capacity_once`` builds the OCI session outside any handler
(``tenant_row_to_config`` / ``sessions.get``), so a tenant whose stored config
the OCI SDK rejects raises straight out of the ``for job in candidates`` loop.

Three consequences, all of which this file pins:

1. The raise happens before ``attempts += 1`` and before any ``next_run_at``
   write, so the broken job's ``next_run_at`` never advances. Candidates are
   ordered ``next_run_at.nullsfirst()``, so it becomes the permanent head of the
   queue and **no other job — including other users' — is ever attempted again.**
2. ``attempts`` stays 0, so the ``max_attempts`` stop can never fire and the job
   retries every poll forever.
3. ``last_error`` is never written and ``status`` sticks at ``running``, so the
   panel shows 运行中 with no error at all.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("fastapi")

from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.models import CapacityJob, Tenant, User  # noqa: E402
from web.backend.worker import Worker  # noqa: E402

_PAYLOAD = {
    "display_name": "i",
    "compartment_id": "ocid1.compartment.oc1..c",
    "availability_domain": "AD-1",
    "shape": "VM.Standard.A1.Flex",
    "image_id": "ocid1.image.oc1..i",
    "subnet_id": "ocid1.subnet.oc1..s",
    "auth_mode": "key",
    "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfakekeymaterial",
    "ocpus": 1,
    "memory_in_gbs": 6,
    "boot_volume_size_in_gbs": 50,
}


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with SessionLocal() as db:
        db.query(CapacityJob).delete()
        db.query(Tenant).delete()
        db.query(User).delete()
        db.commit()
    yield


def _mk(db, *, name: str) -> tuple[str, str]:
    user = User(id=str(uuid.uuid4()), username=f"u-{name}", password_hash="x")
    tenant = Tenant(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        name=name,
        user_ocid="ocid1.user.oc1.." + "a" * 40,
        tenancy_ocid="ocid1.tenancy.oc1.." + "a" * 44,
        fingerprint="aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
        region="ap-tokyo-1",
        private_key_encrypted="",
    )
    job = CapacityJob(
        id=str(uuid.uuid4()),
        owner_id=user.id,
        tenant_id=tenant.id,
        name=f"job-{name}",
        enabled=True,
        status="idle",
        interval_sec=180,
        max_attempts=100,
        attempts=0,
        launch_payload=dict(_PAYLOAD),
        next_run_at=None,  # nullsfirst -> both are due immediately
    )
    # Insert in dependency order: capacity_jobs has FKs onto both users and
    # tenants, and a single add_all() lets SQLite see the child row first.
    db.add(user)
    db.flush()
    db.add(tenant)
    db.flush()
    db.add(job)
    db.commit()
    return job.id, tenant.id


def test_one_exploding_job_does_not_starve_the_others(monkeypatch):
    """The broken job must be penalised, and the healthy one must still run."""
    with SessionLocal() as db:
        bad_job, bad_tenant = _mk(db, name="bad")
        good_job, good_tenant = _mk(db, name="good")

    worker = Worker()
    ran: list[str] = []
    real = Worker._run_capacity_once

    def fake_run(self, db, job):
        if job.tenant_id == bad_tenant:
            # Exactly what a config the OCI SDK rejects does: raise from the
            # session build, outside any handler inside _run_capacity_once.
            raise ValueError("InvalidConfig {'fingerprint': 'malformed'}")
        ran.append(job.id)

    monkeypatch.setattr(Worker, "_run_capacity_once", fake_run)
    _ = real

    with SessionLocal() as db:
        worker.tick_capacity(db)

    assert good_job in ran, "the healthy job was never attempted — the loop was aborted"

    with SessionLocal() as db:
        bad = db.get(CapacityJob, bad_job)
        # 1. It must age out of the queue head.
        assert bad.next_run_at is not None, "next_run_at never advanced; job stays queue head"
        # 2. It must count against max_attempts so it eventually stops.
        assert bad.attempts == 1, f"attempts not incremented (got {bad.attempts})"
        # 3. It must be visible to the operator, not silently 运行中.
        assert bad.last_error, "last_error empty — the panel would show no error"
        assert bad.status != "running", "status stuck at running"
        # Lease released so it is not wedged behind its own lock.
        assert bad.locked_by is None and bad.locked_until is None


def test_job_deleted_mid_attempt_does_not_abort_the_loop(monkeypatch):
    """Deleting a job while the worker holds it must not take the tick down.

    The lease-release commit in the ``finally`` then updates a row that no longer
    exists; SQLAlchemy raises StaleDataError ("expected to update 1 row(s); 0
    were matched"), which used to escape and discard every remaining candidate.
    """
    with SessionLocal() as db:
        doomed_job, doomed_tenant = _mk(db, name="doomed")
        good_job, _ = _mk(db, name="survivor")

    worker = Worker()
    ran: list[str] = []

    def fake_run(self, db, job):
        if job.tenant_id == doomed_tenant:
            # The user pressed 删除 in the panel while this attempt was in flight.
            db.query(CapacityJob).filter(CapacityJob.id == job.id).delete()
            db.commit()
            raise RuntimeError("row vanished mid-attempt")
        ran.append(job.id)

    monkeypatch.setattr(Worker, "_run_capacity_once", fake_run)

    with SessionLocal() as db:
        worker.tick_capacity(db)  # must not raise

    assert good_job in ran, "the surviving job was never attempted"
    with SessionLocal() as db:
        assert db.get(CapacityJob, doomed_job) is None
