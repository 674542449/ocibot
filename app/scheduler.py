"""Local scheduled power tasks and capacity-retry jobs.

Capacity retry is intentionally rate-limited so automated LaunchInstance
calls stay within OCI request-throttling guidance (exponential backoff on
429, conservative fixed interval on OutOfHostCapacity).
"""

from __future__ import annotations

import json
import random
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.config_store import default_data_dir
from app.oci_client import sanitize_launch_payload

# Compliance defaults for capacity retry (LaunchInstance automation).
# OCI documents request throttling and recommends exponential backoff for 429;
# fixed high-frequency polling of LaunchInstance risks TooManyRequests and
# tenancy-level abuse signals. Keep these floors generous and user-visible.
MIN_RETRY_INTERVAL_SEC = 60
DEFAULT_RETRY_INTERVAL_SEC = 180
MAX_RETRY_INTERVAL_SEC = 3600
DEFAULT_MAX_ATTEMPTS = 200
# Hard ceiling even if a user types a huge number; prevents multi-day tight loops
# from being configured as "a few thousand tries".
MAX_MAX_ATTEMPTS = 2000
# Only one active capacity-retry launch at a time per tenant (serialise).
MAX_ACTIVE_RETRIES_PER_TENANT = 1
# Extra delay when OCI returns 429 / TooManyRequests (grows with consecutive hits).
RATE_LIMIT_BASE_BACKOFF_SEC = 60
RATE_LIMIT_MAX_BACKOFF_SEC = 900
# Small jitter fraction so multiple clients do not align on the same second.
RETRY_JITTER_FRACTION = 0.15


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _local_now() -> datetime:
    return datetime.now().astimezone()


def clamp_retry_interval(sec: Any) -> int:
    """Clamp user interval into the compliant range."""
    try:
        value = int(float(sec))
    except (TypeError, ValueError):
        value = DEFAULT_RETRY_INTERVAL_SEC
    if value < MIN_RETRY_INTERVAL_SEC:
        value = MIN_RETRY_INTERVAL_SEC
    if value > MAX_RETRY_INTERVAL_SEC:
        value = MAX_RETRY_INTERVAL_SEC
    return value


def clamp_max_attempts(value: Any) -> int:
    """Clamp max attempts. 0 is rejected (unlimited loops are not allowed)."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        n = DEFAULT_MAX_ATTEMPTS
    if n < 1:
        n = DEFAULT_MAX_ATTEMPTS
    if n > MAX_MAX_ATTEMPTS:
        n = MAX_MAX_ATTEMPTS
    return n


def rate_limit_backoff_sec(consecutive_rate_limits: int) -> int:
    """Exponential backoff with jitter after consecutive 429 responses."""
    hits = max(1, int(consecutive_rate_limits or 1))
    delay = min(RATE_LIMIT_MAX_BACKOFF_SEC, RATE_LIMIT_BASE_BACKOFF_SEC * (2 ** (hits - 1)))
    jitter = random.uniform(0, delay * RETRY_JITTER_FRACTION)
    return int(delay + jitter)


def sanitize_retry_payload(payload: dict) -> dict:
    """Validate and whitelist a key-auth launch payload for disk persistence."""
    return sanitize_launch_payload(payload, for_retry=True)


def _has_sensitive_launch_material(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return True
    sensitive = {"root_password", "password", "password_confirm", "secrets"}
    return bool(sensitive.intersection(payload) or payload.get("user_data_b64"))


@dataclass
class ScheduleJob:
    """Recurring local schedule: run a power action on matching instances."""

    id: str
    name: str
    tenant_id: str
    enabled: bool = True
    # HH:MM local time
    time_of_day: str = "22:00"
    # 0=Mon .. 6=Sun (Python weekday)
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    action: str = "SOFTSTOP"  # START / SOFTSTOP / STOP / SOFTRESET / RESET
    # empty = all non-terminated instances of tenant
    instance_ids: list[str] = field(default_factory=list)
    last_run_date: str = ""  # YYYY-MM-DD local, prevent double fire
    created_at: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduleJob":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        if "id" not in filtered or not filtered["id"]:
            filtered["id"] = str(uuid.uuid4())
        return cls(**filtered)


@dataclass
class CapacityRetryJob:
    """Retry LaunchInstance until success or max attempts (Out of capacity).

    Compliance notes:
    - interval_sec is floored at MIN_RETRY_INTERVAL_SEC
    - max_attempts must be finite (unlimited is not allowed)
    - cooldown_until defers the next attempt after 429 rate limits
    """

    id: str
    name: str
    tenant_id: str
    enabled: bool = True
    # Serialized LaunchInstance details (plain dict)
    launch_payload: dict = field(default_factory=dict)
    # Optional availability domains to rotate through on each attempt (empty = use payload's AD)
    availability_domains: list = field(default_factory=list)
    interval_sec: int = DEFAULT_RETRY_INTERVAL_SEC
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    attempts: int = 0
    status: str = "idle"  # idle / running / success / stopped / failed
    last_error: str = ""
    last_attempt_at: str = ""
    success_instance_id: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    # ISO timestamp; when set and in the future, the runner will not fire this job.
    cooldown_until: str = ""
    # Consecutive 429 / TooManyRequests hits (reset on non-rate-limit capacity error or success).
    consecutive_rate_limits: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["interval_sec"] = clamp_retry_interval(self.interval_sec)
        data["max_attempts"] = clamp_max_attempts(self.max_attempts)
        try:
            data["launch_payload"] = sanitize_retry_payload(self.launch_payload)
        except ValueError:
            if self.enabled:
                raise
            data["launch_payload"] = {}
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CapacityRetryJob":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        if "id" not in filtered or not filtered["id"]:
            filtered["id"] = str(uuid.uuid4())
        # Migrate legacy aggressive settings (sub-minute interval / unlimited attempts).
        raw_interval = filtered.get("interval_sec", DEFAULT_RETRY_INTERVAL_SEC)
        raw_max = filtered.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
        try:
            legacy_unlimited = int(float(raw_max)) == 0
        except (TypeError, ValueError):
            legacy_unlimited = False
        filtered["interval_sec"] = clamp_retry_interval(raw_interval)
        filtered["max_attempts"] = clamp_max_attempts(raw_max)
        if legacy_unlimited:
            # Preserve progress but attach a finite ceiling going forward.
            filtered["max_attempts"] = DEFAULT_MAX_ATTEMPTS
        try:
            filtered["consecutive_rate_limits"] = max(0, int(filtered.get("consecutive_rate_limits") or 0))
        except (TypeError, ValueError):
            filtered["consecutive_rate_limits"] = 0
        filtered["cooldown_until"] = str(filtered.get("cooldown_until") or "")
        payload = filtered.get("launch_payload") or {}
        compromised = _has_sensitive_launch_material(payload)
        try:
            filtered["launch_payload"] = sanitize_retry_payload(payload)
        except ValueError as exc:
            safe_source = dict(payload) if isinstance(payload, dict) else {}
            for key in ("root_password", "password", "password_confirm", "secrets", "user_data_b64"):
                safe_source.pop(key, None)
            try:
                filtered["launch_payload"] = sanitize_retry_payload(safe_source)
            except ValueError:
                filtered["launch_payload"] = {}
            filtered["enabled"] = False
            filtered["status"] = "failed"
            filtered["last_error"] = f"旧任务已禁用：{exc}，请重新创建"
        if compromised:
            filtered["enabled"] = False
            filtered["status"] = "failed"
            filtered["last_error"] = "旧任务包含不可验证的密码或 user-data，敏感字段已移除，请重新创建"
        return cls(**filtered)


class JobStore:
    """Persist schedule + capacity-retry jobs under app data dir."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "jobs.json"
        self.schedules: dict[str, ScheduleJob] = {}
        self.retries: dict[str, CapacityRetryJob] = {}
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.schedules = {}
            self.retries = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.schedules = {}
            self.retries = {}
            return
        schedules: dict[str, ScheduleJob] = {}
        for j in raw.get("schedules", []):
            try:
                if j.get("id"):
                    job = ScheduleJob.from_dict(j)
                    schedules[job.id] = job
            except Exception:
                continue
        retries: dict[str, CapacityRetryJob] = {}
        migrated = int(raw.get("version") or 1) < 2
        for j in raw.get("retries", []):
            try:
                if j.get("id"):
                    if _has_sensitive_launch_material(j.get("launch_payload")):
                        migrated = True
                    job = CapacityRetryJob.from_dict(j)
                    retries[job.id] = job
            except Exception:
                migrated = True
                continue
        self.schedules = schedules
        self.retries = retries
        if migrated:
            try:
                self.save()
            except (OSError, ValueError):
                # Keep sanitized jobs in memory even if the data directory is temporarily read-only.
                pass

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": 2,
                "schedules": [j.to_dict() for j in self.schedules.values()],
                "retries": [j.to_dict() for j in self.retries.values()],
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            # Path.replace fails on some Windows cases if target locked; fall back
            try:
                tmp.replace(self.path)
            except OSError:
                import os

                os.replace(str(tmp), str(self.path))

    # ---- schedules ----
    def list_schedules(self) -> list[ScheduleJob]:
        with self._lock:
            return sorted(self.schedules.values(), key=lambda j: j.name.lower())

    def upsert_schedule(self, job: ScheduleJob) -> ScheduleJob:
        with self._lock:
            self.schedules[job.id] = job
            self.save()
        return job

    def delete_schedule(self, job_id: str) -> None:
        with self._lock:
            self.schedules.pop(job_id, None)
            self.save()

    # ---- capacity retry ----
    def list_retries(self) -> list[CapacityRetryJob]:
        with self._lock:
            return sorted(self.retries.values(), key=lambda j: j.created_at, reverse=True)

    def upsert_retry(self, job: CapacityRetryJob) -> CapacityRetryJob:
        job.launch_payload = sanitize_retry_payload(job.launch_payload)
        job.interval_sec = clamp_retry_interval(job.interval_sec)
        job.max_attempts = clamp_max_attempts(job.max_attempts)
        try:
            job.consecutive_rate_limits = max(0, int(job.consecutive_rate_limits or 0))
        except (TypeError, ValueError):
            job.consecutive_rate_limits = 0
        job.cooldown_until = str(job.cooldown_until or "")
        with self._lock:
            self.retries[job.id] = job
            self.save()
        return job

    def delete_retry(self, job_id: str) -> None:
        with self._lock:
            self.retries.pop(job_id, None)
            self.save()

    def get_retry(self, job_id: str) -> Optional[CapacityRetryJob]:
        with self._lock:
            return self.retries.get(job_id)


def _parse_iso_ts(value: str) -> Optional[float]:
    text = (value or "").strip()
    if not text:
        return None
    try:
        # Support trailing Z and offsets.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


class BackgroundRunner:
    """
    Tick every second from the UI thread via after(), but execute work in threads.
    Callbacks:
      on_log(msg, level)
      on_schedule_fire(job: ScheduleJob)
      on_retry_tick(job: CapacityRetryJob)  -> should attempt once and update job
      on_jobs_changed()
    """

    def __init__(
        self,
        store: JobStore,
        on_log: Callable[[str, str], None],
        on_schedule_fire: Callable[[ScheduleJob], None],
        on_retry_tick: Callable[[CapacityRetryJob], None],
        on_jobs_changed: Optional[Callable[[], None]] = None,
    ):
        self.store = store
        self.on_log = on_log
        self.on_schedule_fire = on_schedule_fire
        self.on_retry_tick = on_retry_tick
        self.on_jobs_changed = on_jobs_changed
        self._busy_schedules: set[str] = set()
        self._busy_retries: set[str] = set()
        self._busy_retry_tenants: set[str] = set()
        self._last_retry_fire: dict[str, float] = {}
        self._lock = threading.Lock()

    def tick(self) -> None:
        """Call from UI thread periodically (e.g. every 1s)."""
        self._check_schedules()
        self._check_retries()

    def _check_schedules(self) -> None:
        now = _local_now()
        today = now.strftime("%Y-%m-%d")
        hm = now.strftime("%H:%M")
        weekday = now.weekday()

        for job in list(self.store.list_schedules()):
            if not job.enabled:
                continue
            if weekday not in (job.weekdays or []):
                continue
            if job.time_of_day != hm:
                continue
            if job.last_run_date == today:
                continue
            if job.id in self._busy_schedules:
                continue

            # mark immediately to avoid double fire within the same minute
            job.last_run_date = today
            self.store.upsert_schedule(job)

            self._busy_schedules.add(job.id)
            self.on_log(f"定时任务触发：{job.name} → {job.action}", "info")

            def run(j=job) -> None:
                try:
                    self.on_schedule_fire(j)
                finally:
                    self._busy_schedules.discard(j.id)
                    if self.on_jobs_changed:
                        self.on_jobs_changed()

            threading.Thread(target=run, daemon=True).start()

    def _check_retries(self) -> None:
        import time

        now_ts = time.time()
        # Count already-busy or in-flight jobs per tenant so we never stampede LaunchInstance.
        active_per_tenant: dict[str, int] = {}
        for jid in self._busy_retries:
            busy = self.store.get_retry(jid)
            if busy:
                active_per_tenant[busy.tenant_id] = active_per_tenant.get(busy.tenant_id, 0) + 1

        for job in list(self.store.list_retries()):
            if not job.enabled or job.status not in ("idle", "running"):
                continue
            max_attempts = clamp_max_attempts(job.max_attempts)
            if job.attempts >= max_attempts:
                continue
            cooldown_ts = _parse_iso_ts(getattr(job, "cooldown_until", "") or "")
            if cooldown_ts is not None and now_ts < cooldown_ts:
                continue
            interval = clamp_retry_interval(job.interval_sec)
            # Mild positive jitter so multi-job / multi-client clocks do not align.
            jitter = interval * RETRY_JITTER_FRACTION * random.random()
            last = self._last_retry_fire.get(job.id, 0)
            if now_ts - last < interval + jitter:
                continue
            if job.id in self._busy_retries:
                continue
            if active_per_tenant.get(job.tenant_id, 0) >= MAX_ACTIVE_RETRIES_PER_TENANT:
                continue
            if job.tenant_id in self._busy_retry_tenants:
                continue

            self._last_retry_fire[job.id] = now_ts
            self._busy_retries.add(job.id)
            self._busy_retry_tenants.add(job.tenant_id)
            active_per_tenant[job.tenant_id] = active_per_tenant.get(job.tenant_id, 0) + 1

            def run(j=job) -> None:
                try:
                    # refresh from store
                    fresh = self.store.get_retry(j.id)
                    if not fresh or not fresh.enabled:
                        return
                    if fresh.status in ("success", "stopped"):
                        return
                    cooldown = _parse_iso_ts(getattr(fresh, "cooldown_until", "") or "")
                    if cooldown is not None and time.time() < cooldown:
                        return
                    if fresh.attempts >= clamp_max_attempts(fresh.max_attempts):
                        return
                    fresh.status = "running"
                    self.store.upsert_retry(fresh)
                    self.on_retry_tick(fresh)
                finally:
                    self._busy_retries.discard(j.id)
                    self._busy_retry_tenants.discard(j.tenant_id)
                    if self.on_jobs_changed:
                        self.on_jobs_changed()

            threading.Thread(target=run, daemon=True).start()
