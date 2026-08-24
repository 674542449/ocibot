"""The free-tier check is check-then-act; without a lock two launches double-spend.

`enforce_launch_quota` reports the state at one instant and reserves nothing, and
the window between the verdict and the actual LaunchInstance is wide on purpose:
`prepare_launch_network` (which can create an NSG, even a VCN) runs inside it.
Two tabs, a double submit, or a capacity job racing a manual create therefore both
snapshot "0 used", both pass, and the tenancy ends up with 8 OCPU / 48 GB — twice
the Always-Free A1 allowance. idempotency_key does not help: it dedupes retries of
ONE submission, and two deliberate submissions carry different keys.

`tenant_launch_lock` has to wrap snapshot -> verdict -> launch, so the loser
re-reads the usage after the winner has created its instance (a PROVISIONING
instance is already counted by summarize_instances) and is refused by the caps.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

# The cross-process layer reads Settings (which DB file to sit next to), and
# get_settings() refuses to build with the repo's placeholder secrets.
os.environ.setdefault("OCIBOT_MASTER_KEY", "qlock-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "qlock-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402

from web.backend import quota_guard  # noqa: E402
from web.backend.quota_guard import (  # noqa: E402
    TenantLaunchLockBusy,
    launch_lock_held,
    tenant_launch_lock,
)

_ROOT = Path(__file__).resolve().parents[1]
_TENANTS = (
    "tenant-race-1",
    "tenant-race-2",
    "tenant-parallel-1",
    "tenant-parallel-2",
    "tenant-reentrant",
    "tenant-cross-process",
)


@pytest.fixture(scope="module", autouse=True)
def _clean_lock_files():
    """The SQLite fallback leaves one lock file per tenant beside the database."""
    yield
    for tenant_id in _TENANTS:
        path = quota_guard._lock_file_path(tenant_id)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass
A1 = "VM.Standard.A1.Flex"
FULL_A1 = dict(
    shape=A1,
    ocpus=4,
    memory_in_gbs=24,
    boot_volume_size_in_gbs=47,
    boot_volume_vpus_per_gb=10,
)


class _Result:
    def __init__(self, data: Any):
        self.ok = True
        self.data = data
        self.message = ""


class _Tenancy:
    """One OCI tenancy: the usage read reflects every instance launched so far."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ocpu = 0.0
        self.memory = 0.0
        self.launches = 0

    def get_free_quota_usage(self, free_only_mode: bool = True, **_kw):
        with self._lock:
            cpu, mem = self.ocpu, self.memory
        return _Result(
            {
                "account_tier": "free",
                "usage": {
                    "a1_ocpu": cpu,
                    "a1_memory_gb": mem,
                    "e2_micro_count": 0,
                    "block_storage_gb": 0.0,
                },
                "remaining": {
                    "a1_ocpu": max(0.0, 4.0 - cpu),
                    "a1_memory_gb": max(0.0, 24.0 - mem),
                    "e2_micro_count": 2,
                    "block_storage_gb": 200.0,
                },
                "read_incomplete": False,
            }
        )

    def launch(self, ocpus: float, memory: float) -> None:
        with self._lock:
            self.ocpu += ocpus
            self.memory += memory
            self.launches += 1


def test_two_concurrent_launches_cannot_both_pass_the_free_caps():
    tenant_id = "tenant-race-1"
    tenancy = _Tenancy()
    refused: list[HTTPException] = []
    crashed: list[BaseException] = []
    winner_inside = threading.Event()

    def attempt(first: bool) -> None:
        try:
            with tenant_launch_lock(tenant_id, timeout_sec=20):
                if first:
                    winner_inside.set()
                quota_guard.enforce_launch_quota(
                    tenancy, account_tier="free", free_only_mode=True, **FULL_A1
                )
                # The real gap: prepare_launch_network runs between the verdict and
                # the LaunchInstance call and can take tens of seconds.
                time.sleep(0.3)
                tenancy.launch(4.0, 24.0)
        except HTTPException as exc:
            refused.append(exc)
        except BaseException as exc:  # noqa: BLE001
            crashed.append(exc)

    t1 = threading.Thread(target=attempt, args=(True,), daemon=True)
    t1.start()
    assert winner_inside.wait(10), "first launch never entered the critical section"
    t2 = threading.Thread(target=attempt, args=(False,), daemon=True)
    t2.start()
    t1.join(30)
    t2.join(30)

    assert not crashed, f"unexpected error: {crashed}"
    assert tenancy.launches == 1, f"{tenancy.launches} launches — free caps double-spent"
    assert tenancy.ocpu == 4.0
    assert len(refused) == 1
    # Refused by the caps on a fresh snapshot, not by the lock timing out.
    assert refused[0].status_code == 400
    assert "A1" in str(refused[0].detail)


def test_busy_tenant_gets_409_instead_of_a_second_snapshot():
    tenant_id = "tenant-race-2"
    inside = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with tenant_launch_lock(tenant_id, timeout_sec=10):
            inside.set()
            release.wait(15)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert inside.wait(10)
        with pytest.raises(TenantLaunchLockBusy) as exc:
            with tenant_launch_lock(tenant_id, timeout_sec=0.3):
                pytest.fail("entered a critical section another thread holds")
        assert exc.value.status_code == 409
        # HTTPException subclass, so a router needs no extra handler for it.
        assert isinstance(exc.value, HTTPException)
    finally:
        release.set()
        holder.join(15)


def test_lock_is_per_tenant_not_global():
    both_in = threading.Barrier(2, timeout=10)
    failures: list[BaseException] = []

    def attempt(tenant_id: str) -> None:
        try:
            with tenant_launch_lock(tenant_id, timeout_sec=10):
                both_in.wait()
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [
        threading.Thread(target=attempt, args=(f"tenant-parallel-{i}",), daemon=True)
        for i in (1, 2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not failures, f"different tenants blocked each other: {failures}"


def test_same_thread_reentry_does_not_deadlock():
    tenant_id = "tenant-reentrant"
    assert launch_lock_held(tenant_id) is False
    with tenant_launch_lock(tenant_id, timeout_sec=5):
        assert launch_lock_held(tenant_id) is True
        with tenant_launch_lock(tenant_id, timeout_sec=0.1):
            assert launch_lock_held(tenant_id) is True
    assert launch_lock_held(tenant_id) is False


def test_blank_tenant_id_does_not_wedge_the_caller():
    with tenant_launch_lock("", timeout_sec=0.1):
        pass


def _file_locking_available() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.lock"
        with open(path, "a+b") as fh:
            return quota_guard._try_lock_file(fh) is True


_CHILD = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from web.backend.quota_guard import tenant_launch_lock
marker = sys.argv[3]
with tenant_launch_lock(sys.argv[2], timeout_sec=5):
    with open(marker, "w") as fh:
        fh.write("locked")
    for _ in range(400):
        if os.path.exists(marker + ".release"):
            break
        time.sleep(0.05)
"""


@pytest.mark.skipif(not _file_locking_available(), reason="no OS file lock here")
def test_sqlite_deployment_serializes_across_processes(tmp_path: Path):
    """SQLite has no advisory locks, and the API runs OCIBOT_API_WORKERS processes.

    An in-process threading.Lock would leave exactly the reported hole open (a
    capacity job in the worker process racing a manual create in the API), so the
    SQLite fallback is an OS file lock beside the database file.
    """
    from web.backend.config import get_settings

    if not get_settings().is_sqlite:
        pytest.skip("PostgreSQL deployment uses the advisory-lock path")

    tenant_id = "tenant-cross-process"
    lock_path = quota_guard._lock_file_path(tenant_id)
    assert lock_path is not None
    # Point the child at a database in the SAME directory, so its lock file path is
    # computed identically without depending on the child's working directory.
    env = dict(os.environ)
    env["DATABASE_URL"] = "sqlite+pysqlite:///" + (lock_path.parent / "probe.db").as_posix()

    marker = tmp_path / "child-locked"
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(_ROOT), tenant_id, str(marker)],
        env=env,
        cwd=str(_ROOT),
    )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            if child.poll() is not None:
                pytest.skip("child process could not take the lock")
            if time.monotonic() > deadline:
                pytest.skip("child process did not report in time")
            time.sleep(0.05)

        with pytest.raises(TenantLaunchLockBusy):
            with tenant_launch_lock(tenant_id, timeout_sec=1.0):
                pytest.fail("acquired a lock another process holds")
    finally:
        (tmp_path / "child-locked.release").write_text("go")
        try:
            child.wait(timeout=30)
        except Exception:  # noqa: BLE001
            child.kill()
