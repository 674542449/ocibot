"""Compliance guards for capacity-retry (rate limits, floors, no unlimited)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.oci_client import is_rate_limit_error, is_rate_limit_message
from app.scheduler import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_INTERVAL_SEC,
    MAX_ACTIVE_RETRIES_PER_TENANT,
    MAX_MAX_ATTEMPTS,
    MIN_RETRY_INTERVAL_SEC,
    BackgroundRunner,
    CapacityRetryJob,
    JobStore,
    clamp_max_attempts,
    clamp_retry_interval,
    rate_limit_backoff_sec,
)


BASE = {
    "compartment_id": "ocid1.compartment.test",
    "availability_domain": "AD-1",
    "shape": "VM.Standard.A1.Flex",
    "image_id": "ocid1.image.test",
    "subnet_id": "ocid1.subnet.test",
    "auth_mode": "key",
    "ssh_public_key": "ssh-ed25519 AAAATEST user",
}


def test_clamp_interval_floors_aggressive_values():
    assert clamp_retry_interval(1) == MIN_RETRY_INTERVAL_SEC
    assert clamp_retry_interval(5) == MIN_RETRY_INTERVAL_SEC
    assert clamp_retry_interval(DEFAULT_RETRY_INTERVAL_SEC) == DEFAULT_RETRY_INTERVAL_SEC
    assert clamp_retry_interval(99999) == 3600


def test_clamp_max_attempts_rejects_unlimited():
    assert clamp_max_attempts(0) == DEFAULT_MAX_ATTEMPTS
    assert clamp_max_attempts(-3) == DEFAULT_MAX_ATTEMPTS
    assert clamp_max_attempts(50) == 50
    assert clamp_max_attempts(999999) == MAX_MAX_ATTEMPTS


def test_rate_limit_backoff_grows_and_caps():
    d1 = rate_limit_backoff_sec(1)
    d3 = rate_limit_backoff_sec(3)
    d10 = rate_limit_backoff_sec(10)
    assert d1 >= 60
    assert d3 > d1
    assert d10 <= 900 + int(900 * 0.15) + 1


def test_legacy_unlimited_and_subminute_migrated_on_load(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "schedules": [],
                "retries": [
                    {
                        "id": "legacy",
                        "name": "legacy",
                        "tenant_id": "t",
                        "interval_sec": 5,
                        "max_attempts": 0,
                        "launch_payload": BASE,
                        "enabled": True,
                        "status": "running",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = JobStore(tmp_path)
    job = store.get_retry("legacy")
    assert job is not None
    assert job.interval_sec >= MIN_RETRY_INTERVAL_SEC
    assert job.max_attempts >= 1
    assert job.max_attempts == DEFAULT_MAX_ATTEMPTS


def test_upsert_clamps_values(tmp_path):
    store = JobStore(tmp_path)
    job = CapacityRetryJob(
        id="1",
        name="t",
        tenant_id="t",
        launch_payload=BASE,
        interval_sec=3,
        max_attempts=0,
    )
    store.upsert_retry(job)
    saved = store.get_retry("1")
    assert saved.interval_sec >= MIN_RETRY_INTERVAL_SEC
    assert saved.max_attempts >= 1
    raw = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    assert raw["retries"][0]["interval_sec"] >= MIN_RETRY_INTERVAL_SEC
    assert raw["retries"][0]["max_attempts"] >= 1


def test_is_rate_limit_detection():
    exc = SimpleNamespace(status=429, code="TooManyRequests", message="User-rate limit exceeded.")
    assert is_rate_limit_error(exc)
    assert is_rate_limit_message("[429] TooManyRequests User-rate limit exceeded.")
    assert not is_rate_limit_message("Out of host capacity")


def test_runner_respects_cooldown(tmp_path):
    store = JobStore(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    job = CapacityRetryJob(
        id="c1",
        name="cool",
        tenant_id="t",
        launch_payload=BASE,
        interval_sec=MIN_RETRY_INTERVAL_SEC,
        max_attempts=10,
        enabled=True,
        status="running",
        cooldown_until=future,
        attempts=1,
    )
    store.upsert_retry(job)
    ticks: list[str] = []

    runner = BackgroundRunner(
        store,
        on_log=lambda *_: None,
        on_schedule_fire=lambda *_: None,
        on_retry_tick=lambda j: ticks.append(j.id),
    )
    # Pretend last fire was long ago so only cooldown should block.
    runner._last_retry_fire["c1"] = 0
    runner.tick()
    time.sleep(0.05)
    assert ticks == []


def test_runner_serialises_per_tenant(tmp_path):
    store = JobStore(tmp_path)
    for i in range(3):
        store.upsert_retry(
            CapacityRetryJob(
                id=f"j{i}",
                name=f"n{i}",
                tenant_id="same-tenant",
                launch_payload=BASE,
                interval_sec=MIN_RETRY_INTERVAL_SEC,
                max_attempts=10,
                enabled=True,
                status="idle",
            )
        )
    started = []

    def slow_tick(job):
        started.append(job.id)
        time.sleep(0.2)

    runner = BackgroundRunner(
        store,
        on_log=lambda *_: None,
        on_schedule_fire=lambda *_: None,
        on_retry_tick=slow_tick,
    )
    for j in store.list_retries():
        runner._last_retry_fire[j.id] = 0
    runner.tick()
    time.sleep(0.05)
    # Only one in-flight per tenant.
    assert len(started) <= MAX_ACTIVE_RETRIES_PER_TENANT
