"""Background worker: capacity retry + power schedules (DB-driven).

Reuses compliance helpers from app.scheduler and OCI launch path from app.oci_client.
Run: python -m web.backend.worker  (from repo root)

Responsibilities:
- capacity retry with AD × config (downgrade) rotation, per-attempt logging
- weekly + one-shot power schedules with execution history
- worker heartbeat (AppMeta) so the panel can show liveness
- daily checks: budget alerts + OCI login password expiry reminders
- push notifications (Telegram / Bark / ServerChan / Webhook / SMTP)
"""

from __future__ import annotations

import logging
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, select
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
    KEY_DAILY_CHECKS_DATE,
    KEY_WORKER_HEARTBEAT,
    KEY_WORKER_ID,
    get_meta,
    set_meta,
)
from web.backend.models import (  # noqa: E402
    CapacityAttempt,
    CapacityJob,
    ScheduleJobRow,
    ScheduleRun,
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
        while True:
            try:
                with SessionLocal() as db:
                    self.beat(db)
                    self.tick_schedules(db)
                    self.tick_capacity(db)
                    self.tick_daily_checks(db)
                    db.commit()
            except Exception:  # noqa: BLE001
                log.exception("worker tick failed")
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

    # ------------------------------------------------------------------
    # Schedules
    # ------------------------------------------------------------------
    def tick_schedules(self, db: Session) -> None:
        now_local = _local_now()
        today = now_local.strftime("%Y-%m-%d")
        hm = now_local.strftime("%H:%M")
        weekday = now_local.weekday()
        now_utc = _utcnow()

        rows = db.scalars(
            select(ScheduleJobRow).where(ScheduleJobRow.enabled.is_(True))
        ).all()
        for job in rows:
            kind = (job.kind or "weekly").lower()
            if kind == "once":
                run_at = _as_utc(job.run_at)
                if run_at is None or run_at > now_utc:
                    continue
                job.enabled = False  # fire exactly once
                job.last_run_date = today
                db.flush()
            else:
                if weekday not in (job.weekdays or []):
                    continue
                if job.time_of_day != hm:
                    continue
                if job.last_run_date == today:
                    continue
                job.last_run_date = today
                db.flush()
            try:
                self._fire_schedule(db, job)
            except Exception as exc:  # noqa: BLE001
                log.error("schedule %s failed: %s", job.id, exc)

    def _fire_schedule(self, db: Session, job: ScheduleJobRow) -> None:
        tenant = db.get(Tenant, job.tenant_id)
        if tenant is None or not tenant.enabled:
            log.warning("schedule %s: tenant missing/disabled", job.id)
            self._record_schedule_run(db, job, ok=False, total=0, success=0, message="租户不存在或已禁用")
            return
        # Ownership guard: tenant must belong to the schedule owner
        if tenant.owner_id != job.owner_id:
            log.error("schedule %s ownership mismatch tenant=%s", job.id, job.tenant_id)
            job.enabled = False
            return
        from web.backend.oci_bridge import tenant_row_to_config

        cfg = tenant_row_to_config(tenant)
        session = self.sessions.get(cfg)
        action = (job.action or "SOFTSTOP").upper()
        allowed = {"START", "STOP", "SOFTSTOP", "RESET", "SOFTRESET"}
        if action not in allowed:
            log.error("schedule %s invalid action %s", job.id, action)
            self._record_schedule_run(db, job, ok=False, total=0, success=0, message=f"不支持的动作 {action}")
            return
        target_ids = list(job.instance_ids or [])
        if not target_ids:
            infos = session.list_instances_tree(resolve_ips=False)
            target_ids = [i.id for i in infos if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
        log.info("schedule fire %s action=%s count=%s", job.name, action, len(target_ids))
        success = 0
        failures: list[str] = []
        for iid in target_ids:
            try:
                result = session.instance_action(iid, action)
                if getattr(result, "ok", True):
                    success += 1
                else:
                    failures.append(f"{iid[-8:]}: {getattr(result, 'message', '') or '失败'}")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{iid[-8:]}: {exc}")
                log.warning("schedule action %s on %s failed: %s", action, iid, exc)
        ok = not failures
        message = "全部成功" if ok else "; ".join(failures)[:1500]
        self._record_schedule_run(
            db, job, ok=ok, total=len(target_ids), success=success, message=message
        )
        if not ok:
            notify_user(
                db,
                job.owner_id,
                "schedule",
                "OCIBot 定时任务部分失败",
                f"任务「{job.name}」动作 {action}：{success}/{len(target_ids)} 成功。\n{message}",
            )

    def _record_schedule_run(
        self,
        db: Session,
        job: ScheduleJobRow,
        *,
        ok: bool,
        total: int,
        success: int,
        message: str,
    ) -> None:
        try:
            db.add(
                ScheduleRun(
                    job_id=job.id,
                    owner_id=job.owner_id,
                    job_name=job.name or "",
                    action=job.action or "",
                    ok=ok,
                    instance_count=total,
                    success_count=success,
                    message=message[:2000],
                )
            )
            db.flush()
        except Exception:  # noqa: BLE001
            log.exception("record schedule run failed")

    # ------------------------------------------------------------------
    # Capacity retry
    # ------------------------------------------------------------------
    def tick_capacity(self, db: Session) -> None:
        now = _utcnow()
        # Release stale locks
        stale = db.scalars(
            select(CapacityJob).where(
                CapacityJob.locked_until.is_not(None),
                CapacityJob.locked_until < now,
            )
        ).all()
        for j in stale:
            j.locked_by = None
            j.locked_until = None

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

        # Tenant concurrency: skip if another job for same tenant is locked
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
                self._notify_capacity_end(db, job, reason="max_attempts")
                continue
            cooldown = _as_utc(job.cooldown_until)
            if cooldown and cooldown > now:
                continue

            # Claim lock
            lock_until = now + timedelta(minutes=10)
            job.locked_by = self.worker_id
            job.locked_until = lock_until
            job.status = "running"
            db.flush()
            locked_tenants.add(job.tenant_id)
            self._busy_tenants.add(job.tenant_id)
            try:
                self._run_capacity_once(db, job)
            finally:
                self._busy_tenants.discard(job.tenant_id)
                job.locked_by = None
                job.locked_until = None
                db.flush()

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
                payload = prepare_launch_network(session, payload, meta=None)
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
    def tick_daily_checks(self, db: Session) -> None:
        today = _local_now().strftime("%Y-%m-%d")
        try:
            if get_meta(db, KEY_DAILY_CHECKS_DATE) == today:
                return
            set_meta(db, KEY_DAILY_CHECKS_DATE, today)
        except Exception:  # noqa: BLE001
            log.exception("daily-check gate failed")
            return
        log.info("running daily checks (%s)", today)
        tenants = db.scalars(select(Tenant).where(Tenant.enabled.is_(True))).all()
        for tenant in tenants:
            try:
                self._check_tenant_budget(db, tenant)
            except Exception:  # noqa: BLE001
                log.exception("budget check failed tenant=%s", tenant.id)
            try:
                self._check_tenant_password_expiry(db, tenant, today)
            except Exception:  # noqa: BLE001
                log.exception("password expiry check failed tenant=%s", tenant.id)

    def _check_tenant_budget(self, db: Session, tenant: Tenant) -> None:
        budget = float(tenant.budget_monthly_usd or 0.0)
        if budget <= 0:
            return
        month = _local_now().strftime("%Y-%m")
        if tenant.budget_notified_month == month:
            return
        from web.backend.oci_bridge import tenant_row_to_config

        session = self.sessions.get(tenant_row_to_config(tenant))
        days_into_month = max(1, _local_now().day)
        result = session.get_usage_summary(days=days_into_month)
        data = result.data if isinstance(result.data, dict) else {}
        try:
            total = float(data.get("total") or 0.0)
        except (TypeError, ValueError):
            total = 0.0
        if not result.ok or total < budget:
            return
        currency = str(data.get("currency") or "USD")
        tenant.budget_notified_month = month
        db.flush()
        notify_user(
            db,
            tenant.owner_id,
            "budget",
            "💰 OCIBot 预算超额提醒",
            (
                f"租户「{tenant.name}」本月费用约 {total:.2f} {currency}，"
                f"已达到预算 {budget:.2f}。\n"
                "请到「账号用量」页查看服务明细，必要时清理付费资源。"
            ),
        )

    def _check_tenant_password_expiry(self, db: Session, tenant: Tenant, today: str) -> None:
        changed = (tenant.password_changed_at or "").strip()
        expiry_days = int(tenant.password_expiry_days or 0)
        if not changed or expiry_days <= 0:
            return
        if tenant.pwd_expiry_notified_on == today:
            return
        try:
            changed_date = datetime.strptime(changed[:10], "%Y-%m-%d").date()
        except ValueError:
            return
        due = changed_date + timedelta(days=expiry_days)
        days_left = (due - _local_now().date()).days
        if days_left > 7:
            return
        tenant.pwd_expiry_notified_on = today
        db.flush()
        state = f"还有 {days_left} 天到期" if days_left >= 0 else f"已过期 {-days_left} 天"
        notify_user(
            db,
            tenant.owner_id,
            "password_expiry",
            "🔐 OCIBot 甲骨文密码到期提醒",
            (
                f"租户「{tenant.name}」的 Oracle Cloud 登录密码{state}"
                f"（修改于 {changed_date.isoformat()}，有效期 {expiry_days} 天）。\n"
                "请尽快登录 Oracle Cloud 控制台修改密码，并在面板租户设置里更新修改日期。"
            ),
        )


def main() -> int:
    Worker().run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
