"""Background worker: capacity retry (DB-driven).

Reuses compliance helpers from app.scheduler and OCI launch path from app.oci_client.
Run: python -m web.backend.worker  (from repo root)

Responsibilities:
- capacity retry with AD × config (downgrade) rotation, per-attempt logging
- worker heartbeat (AppMeta) so the panel can show liveness
- push notifications for capacity results (Telegram / Bark / ServerChan / Webhook / SMTP)

Power schedules, budget alerts and the daily outbound-traffic check were removed in
0.4.36: unused, and they were the only things making Oracle calls without the
operator asking. Capacity retry stays because it only runs while a job exists.
"""

from __future__ import annotations

import logging
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.oci_client import (  # noqa: E402
    is_capacity_error,
    is_capacity_message,
    is_rate_limit_error,
    is_rate_limit_message,
    SessionManager,
)
from app.scheduler import (  # noqa: E402
    MIN_RETRY_INTERVAL_SEC,
    RETRY_JITTER_FRACTION,
    clamp_max_attempts,
    clamp_retry_interval,
    rate_limit_backoff_sec,
)
from web.backend.config import get_settings  # noqa: E402
from web.backend.crypto_util import decrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.meta import (  # noqa: E402
    KEY_WORKER_HEARTBEAT,
    KEY_WORKER_ID,
    set_meta,
)
from web.backend.models import (  # noqa: E402
    CapacityAttempt,
    CapacityJob,
    Tenant,
)
from web.backend.notify import notify_user  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ocibot.worker")

# Keep at most this many attempt log rows per capacity job.
MAX_ATTEMPT_ROWS_PER_JOB = 200


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite returns naive datetimes; normalize to aware UTC for comparisons."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class Worker:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.worker_id = self.settings.worker_id
        self.sessions = SessionManager()
        self._busy_tenants: set[str] = set()

    def run_forever(self) -> None:
        init_db()
        log.info("Worker %s started (poll=%ss)", self.worker_id, self.settings.worker_poll_sec)
        # Capacity retry is the only phase that calls Oracle, and only while a job
        # exists. The heartbeat writes to the database only, so it keeps running even
        # with background OCI off — the panel then reports the worker as online
        # rather than as broken.
        background = bool(self.settings.worker_background_oci)
        if not background:
            log.warning(
                "OCIBOT_WORKER_BACKGROUND_OCI=0: capacity retry will NOT run "
                "(no background OCI calls)"
            )
        phases = (("beat", self.beat),) + (
            (("capacity", self.tick_capacity),) if background else ()
        )
        while True:
            for name, phase in phases:
                try:
                    with SessionLocal() as db:
                        phase(db)
                        db.commit()
                except Exception:  # noqa: BLE001
                    log.exception("worker phase '%s' failed", name)
            time.sleep(max(1.0, float(self.settings.worker_poll_sec)))

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------
    def beat(self, db: Session) -> None:
        try:
            set_meta(db, KEY_WORKER_HEARTBEAT, _utcnow().isoformat())
            set_meta(db, KEY_WORKER_ID, self.worker_id)
        except Exception:  # noqa: BLE001
            log.exception("heartbeat write failed")

    def tick_capacity(self, db: Session) -> None:
        now = _utcnow()
        # Release stale locks durably (a crashed worker's lease expires here).
        db.execute(
            update(CapacityJob)
            .where(
                CapacityJob.locked_until.is_not(None),
                CapacityJob.locked_until < now,
            )
            .values(locked_by=None, locked_until=None)
            .execution_options(synchronize_session=False)
        )
        db.commit()

        candidates = db.scalars(
            select(CapacityJob)
            .where(
                CapacityJob.enabled.is_(True),
                CapacityJob.status.in_(("idle", "running")),
                (CapacityJob.next_run_at.is_(None)) | (CapacityJob.next_run_at <= now),
                (CapacityJob.locked_until.is_(None)) | (CapacityJob.locked_until < now),
            )
            .order_by(CapacityJob.next_run_at.nullsfirst())
            .limit(20)
        ).all()

        # Tenant concurrency: skip if another job for the same tenant is locked.
        locked_tenants = {
            j.tenant_id
            for j in db.scalars(
                select(CapacityJob).where(
                    CapacityJob.locked_until.is_not(None),
                    CapacityJob.locked_until >= now,
                )
            ).all()
        }

        for job in candidates:
            if job.tenant_id in locked_tenants or job.tenant_id in self._busy_tenants:
                continue
            if job.attempts >= clamp_max_attempts(job.max_attempts):
                job.enabled = False
                job.status = "failed"
                job.last_error = job.last_error or "已达最大重试次数"
                db.commit()
                self._notify_capacity_end(db, job, reason="max_attempts")
                db.commit()
                continue
            cooldown = _as_utc(job.cooldown_until)
            if cooldown and cooldown > now:
                continue

            # Atomically claim the lease so a second worker cannot take the same job.
            # Committed BEFORE any OCI call, so the lock is durable and visible across
            # processes — per-tenant single-flight, crash-safe.
            claim_at = _utcnow()
            lock_until = claim_at + timedelta(minutes=10)
            claimed = db.execute(
                update(CapacityJob)
                .where(
                    CapacityJob.id == job.id,
                    # Re-assert the candidate conditions inside the claim: the
                    # candidate list was read before the OCI work above, so a user
                    # who pressed "stop" in between still got one more launch.
                    CapacityJob.enabled.is_(True),
                    CapacityJob.status.in_(("idle", "running")),
                    (CapacityJob.locked_until.is_(None)) | (CapacityJob.locked_until < claim_at),
                )
                .values(locked_by=self.worker_id, locked_until=lock_until, status="running")
                .execution_options(synchronize_session=False)
            ).rowcount
            if not claimed:
                continue
            db.commit()
            db.refresh(job)
            locked_tenants.add(job.tenant_id)
            self._busy_tenants.add(job.tenant_id)
            try:
                self._run_capacity_once(db, job)
            finally:
                self._busy_tenants.discard(job.tenant_id)
                job.locked_by = None
                job.locked_until = None
                # Persist this attempt's result + lease release before the next job,
                # so a crash cannot roll back a completed LaunchInstance outcome.
                db.commit()

    @staticmethod
    def _attempt_plan(job: CapacityJob) -> tuple[str, Optional[dict[str, Any]], str]:
        """Pick (availability_domain, config_override, config_label) for this attempt.

        Rotation order: all ADs with the primary config first, then all ADs with
        each fallback config. One LaunchInstance call per attempt (compliance).
        """
        payload = dict(job.launch_payload or {})
        ads = [a for a in (job.availability_domains or []) if a] or [
            str(payload.get("availability_domain") or "")
        ]
        combos: list[Optional[dict[str, Any]]] = [None]
        for fb in job.fallback_configs or []:
            if isinstance(fb, dict) and fb.get("ocpus") and fb.get("memory_in_gbs"):
                combos.append({"ocpus": fb["ocpus"], "memory_in_gbs": fb["memory_in_gbs"]})
        idx = int(job.attempts or 0) % (len(ads) * len(combos))
        ad = ads[idx % len(ads)]
        cfg = combos[idx // len(ads)]
        if cfg is None:
            ocpus = payload.get("ocpus")
            mem = payload.get("memory_in_gbs")
        else:
            ocpus, mem = cfg.get("ocpus"), cfg.get("memory_in_gbs")
        label = ""
        if ocpus is not None and mem is not None:
            try:
                label = f"{float(ocpus):g}C/{float(mem):g}G"
            except (TypeError, ValueError):
                label = ""
        return ad, cfg, label

    def _run_capacity_once(self, db: Session, job: CapacityJob) -> None:
        now = _utcnow()
        tenant = db.get(Tenant, job.tenant_id)
        if tenant is None or not tenant.enabled:
            job.enabled = False
            job.status = "failed"
            job.last_error = "租户不存在或已禁用"
            return
        if tenant.owner_id != job.owner_id:
            job.enabled = False
            job.status = "failed"
            job.last_error = "任务与租户归属不一致"
            return

        interval = clamp_retry_interval(job.interval_sec)
        payload = dict(job.launch_payload or {})
        ad, cfg_override, cfg_label = self._attempt_plan(job)
        if ad:
            payload["availability_domain"] = ad
        if cfg_override is not None:
            payload["ocpus"] = cfg_override.get("ocpus")
            payload["memory_in_gbs"] = cfg_override.get("memory_in_gbs")

        from web.backend.oci_bridge import tenant_row_to_config

        cfg = tenant_row_to_config(tenant)
        session = self.sessions.get(cfg)
        # Ensure managed NSG / IPv6 still present on retry payloads
        try:
            from web.backend.launch_service import prepare_launch_network

            if not payload.get("nsg_ids") and not payload.get("managed_nsg_id"):
                payload = prepare_launch_network(session, payload, meta=None, for_retry=True)
                base_payload = dict(job.launch_payload or {})
                for key in ("nsg_ids", "managed_nsg_id", "vcn_id", "network_compartment_id", "subnet_id", "launch_token"):
                    if payload.get(key):
                        base_payload[key] = payload[key]
                job.launch_payload = base_payload
        except Exception as exc:  # noqa: BLE001
            log.warning("capacity prepare network job=%s: %s", job.id, exc)

        custom_user_data = ""
        if job.user_data_encrypted:
            try:
                custom_user_data = decrypt_text(job.user_data_encrypted)
            except Exception:  # noqa: BLE001
                log.exception("decrypt user_data failed job=%s (continuing without it)", job.id)

        # Quota re-check runs BEFORE the attempt counter moves, so deferring on an
        # unreadable quota does not burn attempts during an Oracle API outage.
        tier = getattr(tenant, "account_tier", "") or ""
        pre_snapshot: Optional[dict[str, Any]] = None
        # A 副区 job has no Always Free allowance to check against — the caps are
        # home-region only and the per-region snapshot would read as empty
        # headroom. The API refused to enqueue it unless the tenant opted into
        # billing, so the free-cap machinery is skipped for the whole attempt.
        secondary_region = False
        try:
            from web.backend.quota_guard import is_secondary_region, tenant_is_secondary

            secondary_region = tenant_is_secondary(tenant) or is_secondary_region(session)
        except Exception as exc:  # noqa: BLE001
            log.warning("capacity region probe job=%s failed: %s", job.id, exc)
        try:
            from web.backend.quota_guard import free_only_for_tenant, usage_snapshot

            if (
                not secondary_region
                and hasattr(session, "get_free_quota_usage")
                and free_only_for_tenant(tenant)
            ):
                snapshot = usage_snapshot(session, free_only_mode=True)
                pre_snapshot = snapshot
                if snapshot.get("read_incomplete"):
                    # Do NOT launch on an undercount — a partial read looks like
                    # free headroom and could create billable overage. Reschedule
                    # instead of failing the job, so a transient blip does not kill
                    # a long-running retry.
                    delay = max(MIN_RETRY_INTERVAL_SEC, interval)
                    job.next_run_at = now + timedelta(seconds=delay)
                    job.status = "idle"
                    job.last_error = "额度读取不完整，已推迟本次尝试"
                    log.warning(
                        "capacity deferred job=%s (quota read incomplete), next_in=%ss",
                        job.id,
                        delay,
                    )
                    return
        except Exception as exc:  # noqa: BLE001
            log.warning("capacity quota pre-read job=%s skipped: %s", job.id, exc)

        job.attempts = int(job.attempts or 0) + 1
        job.last_attempt_at = now
        log.info(
            "capacity attempt %s/%s job=%s tenant=%s ad=%s cfg=%s",
            job.attempts,
            job.max_attempts,
            job.id,
            tenant.name,
            ad[-6:] if ad else "?",
            cfg_label or "primary",
        )

        # Re-check Always Free remaining before each LaunchInstance (usage may
        # have changed since the job was enqueued). Hard block → fail the job
        # instead of burning attempts / creating billable overage.
        try:
            from web.backend.quota_guard import check_launch_quota

            if secondary_region:
                log.info("capacity quota check skipped job=%s (副区，按量计费)", job.id)
            elif not hasattr(session, "get_free_quota_usage"):
                log.warning("capacity quota check skipped job=%s (no get_free_quota_usage)", job.id)
            else:
                guard = check_launch_quota(
                    session,
                    account_tier=tier,
                    shape=str(payload.get("shape") or ""),
                    ocpus=payload.get("ocpus"),
                    memory_in_gbs=payload.get("memory_in_gbs"),
                    boot_volume_size_in_gbs=payload.get("boot_volume_size_in_gbs"),
                    boot_volume_vpus_per_gb=payload.get("boot_volume_vpus_per_gb") or 10,
                    free_only_mode=free_only_for_tenant(tenant),
                    # Reuse the snapshot the deferral decision was made on. Letting
                    # this take its own read meant the pre-check above was not the
                    # deciding one: a second, throttled read returns zeroed usage
                    # (check_launch_quota does not apply the incomplete-read block,
                    # it only builds a GuardResult) and the launch proceeded anyway.
                    # Also halves the OCI enumeration per attempt.
                    usage=pre_snapshot,
                )
                # Ignore pure "spec incomplete" issues here — the launch path will
                # surface those; we only stop on real free-cap exhaustion / non-free shape.
                hard_codes = {
                    "non_free_shape",
                    "a1_over_free_cap",
                    "a1_insufficient",
                    "e2_insufficient",
                    "storage_over_free_cap",
                }
                hard_msgs = [
                    i.message
                    for i in (guard.issues or [])
                    if getattr(i, "severity", "error") == "error"
                    and getattr(i, "code", "") in hard_codes
                ]
                if hard_msgs:
                    msg = "；".join(hard_msgs)
                    self._log_attempt(
                        db, job, ok=False, message=f"额度守卫：{msg}", ad=ad, config_label=cfg_label
                    )
                    job.enabled = False
                    job.status = "failed"
                    job.last_error = f"额度守卫：{msg}"
                    job.next_run_at = None
                    self._notify_capacity_end(db, job, reason=f"额度守卫：{msg}")
                    return
        except Exception as exc:  # noqa: BLE001
            log.warning("capacity quota check job=%s skipped: %s", job.id, exc)

        try:
            result = session.launch_from_payload(payload, custom_user_data=custom_user_data)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            self._log_attempt(db, job, ok=False, message=msg, ad=ad, config_label=cfg_label)
            self._handle_capacity_error(db, job, msg, interval, exc=exc)
            return

        if result.ok:
            job.status = "success"
            job.enabled = False
            job.last_error = ""
            job.consecutive_rate_limits = 0
            job.cooldown_until = None
            job.next_run_at = None
            data = getattr(result, "data", None)
            inst_id = ""
            if isinstance(data, dict):
                inst_id = str(data.get("id") or data.get("instance_id") or "")
            elif data is not None and hasattr(data, "id"):
                inst_id = str(getattr(data, "id") or "")
            if not inst_id and result.message:
                for part in result.message.split():
                    if part.startswith("ocid1.instance."):
                        inst_id = part.strip(" ,.")
                        break
            job.success_instance_id = inst_id
            self._log_attempt(
                db, job, ok=True, message=result.message or "创建成功", ad=ad, config_label=cfg_label
            )
            log.info("capacity SUCCESS job=%s instance=%s", job.id, inst_id or "?")
            # Apply Always-Free boot VPU (fire-and-forget so the worker isn't blocked
            # by hydration). Previously only the API immediate-attempt did this.
            boot_vpu = int(payload.get("boot_volume_vpus_per_gb") or 10)
            if inst_id and boot_vpu != 10:
                try:
                    from web.backend.launch_service import schedule_post_launch_adjustments

                    schedule_post_launch_adjustments(
                        session,
                        instance_id=inst_id,
                        compartment_id=str(payload.get("compartment_id") or ""),
                        boot_vpu=boot_vpu,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("schedule boot vpu failed job=%s", job.id)
            display_name = str(payload.get("display_name") or "instance")
            shape = str(payload.get("shape") or "")
            notify_user(
                db,
                job.owner_id,
                "capacity",
                "🎉 OCIBot 抢机成功",
                (
                    f"任务「{job.name}」第 {job.attempts} 次尝试成功！\n"
                    f"实例：{display_name}\n"
                    f"型号：{shape}" + (f"（{cfg_label}）" if cfg_label else "") + "\n"
                    f"可用域：{ad}\n"
                    f"OCID：{inst_id or '待查询'}\n"
                    "公网 IP 请稍后在面板实例列表查看。"
                ),
            )
            return

        msg = result.message or "Launch failed"
        self._log_attempt(db, job, ok=False, message=msg, ad=ad, config_label=cfg_label)
        self._handle_capacity_error(db, job, msg, interval, exc=None)

    def _log_attempt(
        self,
        db: Session,
        job: CapacityJob,
        *,
        ok: bool,
        message: str,
        ad: str,
        config_label: str,
    ) -> None:
        try:
            db.add(
                CapacityAttempt(
                    job_id=job.id,
                    owner_id=job.owner_id,
                    n=int(job.attempts or 0),
                    seq=int(job.attempts or 0),
                    ok=ok,
                    capacity=is_capacity_message(message or ""),
                    rate_limited=is_rate_limit_message(message or ""),
                    message=(message or "")[:2000],
                    availability_domain=ad or "",
                    config_label=config_label or "",
                )
            )
            # Prune old rows so long-running jobs do not grow unbounded.
            cutoff = int(job.attempts or 0) - MAX_ATTEMPT_ROWS_PER_JOB
            if cutoff > 0:
                db.execute(
                    delete(CapacityAttempt).where(
                        CapacityAttempt.job_id == job.id,
                        CapacityAttempt.seq <= cutoff,
                    )
                )
            db.flush()
        except Exception:  # noqa: BLE001
            log.exception("log attempt failed job=%s", job.id)

    def _handle_capacity_error(
        self,
        db: Session,
        job: CapacityJob,
        msg: str,
        interval: int,
        *,
        exc: Optional[BaseException],
    ) -> None:
        job.last_error = msg[:2000]
        now = _utcnow()
        rate_limited = is_rate_limit_message(msg) or (exc is not None and is_rate_limit_error(exc))
        capacity = is_capacity_message(msg) or (exc is not None and is_capacity_error(exc))

        if rate_limited:
            job.consecutive_rate_limits = int(job.consecutive_rate_limits or 0) + 1
            backoff = rate_limit_backoff_sec(job.consecutive_rate_limits)
            job.cooldown_until = now + timedelta(seconds=backoff)
            job.next_run_at = job.cooldown_until
            job.status = "idle"
            log.warning("capacity 429 job=%s backoff=%ss: %s", job.id, backoff, msg[:200])
            return

        job.consecutive_rate_limits = 0
        job.cooldown_until = None

        if capacity:
            jitter = interval * RETRY_JITTER_FRACTION * random.random()
            delay = max(MIN_RETRY_INTERVAL_SEC, int(interval + jitter))
            job.next_run_at = now + timedelta(seconds=delay)
            job.status = "idle"
            if job.attempts >= clamp_max_attempts(job.max_attempts):
                job.enabled = False
                job.status = "failed"
                job.last_error = f"已达最大次数：{msg}"[:2000]
                self._notify_capacity_end(db, job, reason="max_attempts")
            log.info("capacity OutOfHost job=%s next_in=%ss", job.id, delay)
            return

        # Non-capacity permanent error
        job.enabled = False
        job.status = "failed"
        job.next_run_at = None
        log.error("capacity permanent fail job=%s: %s", job.id, msg[:300])
        self._notify_capacity_end(db, job, reason="permanent")

    def _notify_capacity_end(self, db: Session, job: CapacityJob, *, reason: str) -> None:
        title = "⏹ OCIBot 容量重试已停止"
        if reason == "max_attempts":
            body = (
                f"任务「{job.name}」已达最大重试次数（{job.attempts}/{job.max_attempts}），已自动停止。\n"
                f"最近错误：{(job.last_error or '')[:300]}\n"
                "如需继续，请在任务中心调整后重新启动。"
            )
        else:
            title = "❌ OCIBot 容量重试失败"
            body = (
                f"任务「{job.name}」遇到非容量错误，已停止（第 {job.attempts} 次尝试）。\n"
                f"错误：{(job.last_error or '')[:400]}"
            )
        notify_user(db, job.owner_id, "capacity", title, body)

    # ------------------------------------------------------------------
    # Daily checks: budget + password expiry
    # ------------------------------------------------------------------
def main() -> int:
    Worker().run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
