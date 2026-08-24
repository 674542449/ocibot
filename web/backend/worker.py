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
    derive_retry_token,
    is_capacity_error,
    is_capacity_message,
    is_rate_limit_error,
    is_rate_limit_message,
    is_transient_error,
    SessionManager,
)
from app.scheduler import (  # noqa: E402
    MIN_RETRY_INTERVAL_SEC,
    RETRY_JITTER_FRACTION,
    clamp_max_attempts,
    clamp_retry_interval,
    rate_limit_backoff_sec,
)
from web.backend.audit import prune_audit_log  # noqa: E402
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
    _audit_pruned_at: Optional[float] = None

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
        self._prune_audit(db)

    def _prune_audit(self, db: Session) -> None:
        """Enforce audit retention. Database only — no Oracle call, so this runs
        even with OCIBOT_WORKER_BACKGROUND_OCI=0.

        Hourly rather than every poll: the heartbeat fires every few seconds, and
        a COUNT(*) over the audit table that often is pure waste.
        """
        now = time.monotonic()
        last = self._audit_pruned_at
        if last is not None and now - last < 3600.0:
            return
        self._audit_pruned_at = now
        try:
            removed = prune_audit_log(
                db,
                retention_days=int(self.settings.audit_retention_days),
                max_rows=int(self.settings.audit_max_rows),
            )
            if removed:
                log.info("pruned %d audit rows", removed)
        except Exception:  # noqa: BLE001
            log.exception("audit prune failed")

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

            # 间隔下限的服务端兜底 —— 不信任任何写 next_run_at 的路径。
            #
            # 候选条件只看 next_run_at <= now，全文件从来没有拿 last_attempt_at
            # 比过一次。于是任何把 next_run_at 写成「现在」的 API 都能把两次
            # LaunchInstance 压到一个轮询周期（5s）之内：
            # /jobs/capacity/{id}/resume 就是这么写的，而面板上「停止→继续」是
            # 用户表达「立刻再试一次」的唯一手势（两个按钮共用一格，且没有
            # 「立即重试」）。连点几下就是约 12 次/分钟，对着
            # app/scheduler.py::MIN_RETRY_INTERVAL_SEC 的 1 次/60 秒。
            # 429 冷却由上面的 cooldown_until 管，这里管的是普通间隔。
            last_attempt = _as_utc(job.last_attempt_at)
            if last_attempt is not None:
                earliest = last_attempt + timedelta(
                    seconds=clamp_retry_interval(job.interval_sec)
                )
                if earliest > now:
                    # 顺手把 next_run_at 推回合规位置：否则每个轮询周期都要重新
                    # 算一遍，而且面板上显示的「下次尝试」会一直是骗人的。
                    job.next_run_at = earliest
                    db.commit()
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
            handled = True
            try:
                self._run_capacity_once(db, job)
            except Exception as exc:  # noqa: BLE001
                # 一个任务炸掉，绝不能掀翻整个循环。
                #
                # 这里原来只有 try/finally，没有 except。_run_capacity_once 里
                # 建 OCI 会话那两行（tenant_row_to_config / sessions.get）本身
                # 不在任何 try 里，于是一个配置坏掉的租户（比如 fingerprint
                # 格式不合法 —— TenantConfig.validate 放行，oci.config
                # .validate_config 拒绝）抛出的异常会一路逃出 for 循环。后果有三层：
                #
                #   1. 抛点在 attempts += 1 和任何 next_run_at 写入之前，所以这个
                #      任务的 next_run_at 永远不推进。而候选是按
                #      next_run_at.nullsfirst() 排序的，它会在一个周期内变成永久
                #      队头 —— **全站所有用户的抢机任务从此都不再被执行**。
                #   2. attempts 不增，max_attempts 的停止条件永远够不着，每 5 秒
                #      重来一次，无穷无尽。
                #   3. last_error 没写、status 卡在 running，面板显示「运行中」
                #      且没有任何错误，唯一线索是 worker 日志里每 5 秒一条堆栈。
                #
                # 删除任务时 worker 正好在跑同一个任务，也会走到这里：下面 finally
                # 里的 commit 会撞上 StaleDataError（UPDATE 匹配 0 行）。
                handled = False
                log.exception(
                    "capacity job %s raised outside its own error handling", job.id
                )
                self._release_job_after_crash(db, job.id, exc)
            finally:
                self._busy_tenants.discard(job.tenant_id)
                if handled:
                    job.locked_by = None
                    job.locked_until = None
                    # Persist this attempt's result + lease release before the next job,
                    # so a crash cannot roll back a completed LaunchInstance outcome.
                    db.commit()

    def _release_job_after_crash(self, db: Session, job_id: str, exc: BaseException) -> None:
        """Put a job that blew up outside its own handler back into a sane state.

        Runs in a transaction of its own, because the caller's session may already
        be unusable: on PostgreSQL a failed statement aborts the transaction, and
        every later statement then raises ``InFailedSqlTransaction`` /
        ``PendingRollbackError`` — including the lease release. So: roll back
        first, re-read the row by id (it may have been deleted mid-attempt, which
        is one of the ways we get here), then write.

        Advancing ``next_run_at`` and ``attempts`` is the point. Leaving either
        untouched is what turned one broken tenant into a permanent, silent,
        installation-wide stall: the job stayed at the head of the
        ``next_run_at.nullsfirst()`` ordering and never aged out.
        """
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            log.exception("rollback failed while recovering capacity job %s", job_id)
            return
        try:
            row = db.get(CapacityJob, job_id)
            if row is None:
                # Deleted while the worker held it — nothing to release.
                return
            now = _utcnow()
            row.locked_by = None
            row.locked_until = None
            row.status = "idle"
            # Count it: otherwise a permanently broken job never reaches
            # max_attempts and retries until someone notices by hand.
            row.attempts = int(row.attempts or 0) + 1
            row.last_attempt_at = now
            row.next_run_at = now + timedelta(seconds=clamp_retry_interval(row.interval_sec))
            row.last_error = f"任务执行异常：{exc}"[:2000]
            db.commit()
        except Exception:  # noqa: BLE001
            log.exception("failed to release capacity job %s after a crash", job_id)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass

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
            from web.backend.quota_guard import resolve_secondary

            # 以前是 `tenant_is_secondary(tenant) or is_secondary_region(session)` ——
            # DB hint 排在前面，于是一个 region 等于主区的子行会被判成副区，
            # 下面整段免费额度检查被跳过。判定统一收在 resolve_secondary 里。
            secondary_region = resolve_secondary(session, tenant)
        except Exception as exc:  # noqa: BLE001
            log.warning("capacity region probe job=%s failed: %s", job.id, exc)
        try:
            from web.backend.quota_guard import free_only_for_tenant, usage_snapshot

            # 判断依据必须是 hard_free_caps，不是 free_only_for_tenant。
            #
            # 「免费上限是硬阻断还是只警告」的规则是
            # `free_only or tier in {"", "free", "unknown"}`。account_tier 默认就是
            # ""（没人点过「等级查询」的租户都是），所以一个关掉了「仅使用免费额度」
            # 的普通租户满足 hard_free_caps 但不满足 free_only_for_tenant ——
            # 于是这个「读不全就推迟」的兜底整个被跳过，而 check_launch_quota 自己
            # 那次读取失败时会退化成 {"read_incomplete": True}，校验器把它当成
            # 「一点没用」，于是限流路径**比正常路径更宽松**，真的开出一台计费实例。
            #
            # API 路径对同样的配置是 503 拒绝的。这是 AUDIT pass 11 那条「一个共享
            # 判断函数」的第三个调用点，当时没接上。
            from app.free_quota import hard_free_caps

            free_only = free_only_for_tenant(tenant)
            if (
                not secondary_region
                and hasattr(session, "get_free_quota_usage")
                and hard_free_caps(free_only, tier)
            ):
                snapshot = usage_snapshot(session, free_only_mode=free_only)
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
            # free_only_for_tenant 也在这里再导一次：上面那处 import 在自己的
            # try 里，那个 try 失败时这个名字就不存在了，而这里的判断不能跟着消失。
            from web.backend.quota_guard import check_launch_quota, free_only_for_tenant

            if secondary_region and free_only_for_tenant(tenant):
                # 任务入队时这个租户是允许计费的（API 不接受 free_only 的副区任务），
                # 但用户之后可以在租户页把「仅使用免费额度」重新勾上。任务是长期
                # 挂着跑的，这个翻转必须被看见 —— 否则一个明确表示「只要免费」的
                # 用户，会在副区被开出一台按量计费的机器。
                msg = "该副区租户已勾选「仅使用免费额度」，而副区资源一律计费，任务已停止"
                self._log_attempt(db, job, ok=False, message=msg, ad=ad, config_label=cfg_label)
                job.enabled = False
                job.status = "failed"
                job.last_error = msg
                job.next_run_at = None
                self._notify_capacity_end(db, job, reason=f"额度守卫：{msg}")
                return
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

        # opc-retry-token：抢机是**唯一**会重发同一个 LaunchInstance 的路径，
        # 却曾是唯一不带幂等 token 的（浏览器路径 routers/instances.py 一直带）。
        #
        # lease 在调用 OCI 之前就 commit 了，而 attempts+=1 和这次尝试的结果要到
        # finally 才 commit。容器如果在这两者之间重启（面板自带的一键更新、OOM、
        # 宿主机重启都会），事务回滚：attempts 没涨、状态还是 running。等 10 分钟
        # 租约过期被回收后，_attempt_plan 用的还是同一个 attempts，于是以**完全
        # 相同**的 LaunchInstanceDetails 再发一次 —— 多出来一台没人记账的机器，
        # 吃掉 Always Free 额度或者直接产生费用。
        #
        # 用 attempts 做 index 而不是时间戳：同一次尝试的重放会拿到同一个 token，
        # Oracle 侧折叠成重放；而下一次尝试（AD / 配置可能不同）拿到不同的 token，
        # 不会撞上 IdempotentParameterMismatch。
        retry_token = derive_retry_token(
            str(payload.get("launch_token") or job.id), int(job.attempts or 0)
        )
        try:
            result = session.launch_from_payload(
                payload, custom_user_data=custom_user_data, idempotency_key=retry_token
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            self._log_attempt(db, job, ok=False, message=msg, ad=ad, config_label=cfg_label)
            self._handle_capacity_error(db, job, msg, interval, exc=exc, cfg=cfg_override)
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
            # 先落库，再发通知 —— 顺序不能反。
            #
            # notify_user 会做网络 I/O（SMTP 最坏情况几分钟，见 notify.py 的
            # _TOTAL_DEADLINE_SEC 注释）。在它之前，job.status/success_instance_id
            # 和 _log_attempt 的 INSERT 都还只是未提交的写事务：
            #   * SQLite（默认部署）：整库写锁被占住，这段时间内所有登录、审计、
            #     租户编辑全部 `database is locked`；
            #   * PostgreSQL：一个几分钟的 idle-in-transaction 压着 capacity_jobs 的行锁。
            # 更糟的是 notify_user 内部失败会让 PG 事务进入 aborted 状态，finally
            # 里的 commit 随之抛出，**这次抢机成功的结果连同 OCID 一起被回滚**，
            # 而机器已经开出来了。通知是 best-effort 的，本来就不需要待在事务里。
            owner_id = job.owner_id
            job_name, job_attempts = job.name, job.attempts
            db.commit()
            notify_user(
                db,
                owner_id,
                "capacity",
                "🎉 OCIBot 抢机成功",
                (
                    f"任务「{job_name}」第 {job_attempts} 次尝试成功！\n"
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
        self._handle_capacity_error(db, job, msg, interval, exc=None, cfg=cfg_override)

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
        """Write one attempt-log row. Must never damage the caller's transaction.

        写日志失败是可以接受的；把事务打坏不是。这个方法原来只有一个
        ``except: log.exception``，在 PostgreSQL 上远远不够：一条语句失败之后整个
        事务进入 aborted 状态，后面每一条语句都抛 ``InFailedSqlTransaction`` /
        ``PendingRollbackError``。也就是说 _log_attempt 吞掉的那个异常，会以
        ``_handle_capacity_error`` / ``_notify_capacity_end`` 崩掉的形式重新出现，
        而 ``attempts += 1`` 也一起回滚 —— max_attempts 那道上限从此永远够不到，
        租约一过期任务就重新认领、再发一次 LaunchInstance，无限循环。整个文件为
        了合规而维护的尝试次数上限就是这样被绕过的。

        具体的触发器是 ``availability_domains`` 里一个 200 字符的 AD：Oracle 拒
        绝这个 AD（正常），然后这里把它原样写进 ``String(128)``，flush 抛
        ``DataError``。所以两道防线都要有 —— 按列宽截断，**以及** 用 SAVEPOINT
        把这次写入隔离起来，这样任何没预料到的宽度/约束错误都只回滚它自己。
        """
        # 先把 job 自身的改动（attempts、last_attempt_at…）刷进当前事务，再开
        # SAVEPOINT。顺序反过来的话，SAVEPOINT 回滚会连 attempts += 1 一起撤销，
        # 正好是上面描述的那个死循环。
        try:
            db.flush()
        except Exception:  # noqa: BLE001
            log.exception("flush before attempt log failed job=%s", job.id)
            return
        try:
            with db.begin_nested():
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
                        # 按 models.CapacityAttempt 的列宽截断。日志行被截断，
                        # 总好过让一次尝试记不上账。
                        availability_domain=(ad or "")[:128],
                        config_label=(config_label or "")[:64],
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
            # SAVEPOINT 已经回滚，外层事务仍然可用 —— 调用方接着写 last_error /
            # next_run_at / 推送通知都不会受影响。
            log.exception("log attempt failed job=%s", job.id)

    def _handle_capacity_error(
        self,
        db: Session,
        job: CapacityJob,
        msg: str,
        interval: int,
        *,
        exc: Optional[BaseException],
        cfg: Optional[dict[str, Any]] = None,
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

        if is_transient_error(exc, msg):
            # 传输层抖动 / Oracle 5xx 不是「永久错误」。
            #
            # 以前这里只有「容量」和「其他」两档，一次 DNS 抖动、一次读超时、一个
            # 503，都会走到下面把任务 enabled=False 彻底停掉。抢机任务本来就是要挂
            # 一整夜的，第一次网络抖动就死掉，早上看到的是一条「遇到非容量错误」，
            # 而那个错误早已不存在。按容量错误同样的节奏退避重试即可，最大次数的
            # 停止条件仍然生效，不会变成无限重试。
            jitter = interval * RETRY_JITTER_FRACTION * random.random()
            delay = max(MIN_RETRY_INTERVAL_SEC, int(interval + jitter))
            job.next_run_at = now + timedelta(seconds=delay)
            job.status = "idle"
            if job.attempts >= clamp_max_attempts(job.max_attempts):
                job.enabled = False
                job.status = "failed"
                job.last_error = f"已达最大次数：{msg}"[:2000]
                self._notify_capacity_end(db, job, reason="max_attempts")
            log.warning("capacity transient job=%s next_in=%ss: %s", job.id, delay, msg[:200])
            return

        # Non-capacity permanent error
        #
        # 只有备用配置这一档失败时，不该把整个任务判死：AD × 配置的轮换里，
        # 备用配置只占其中几格，主配置和其他 AD 可能完全正常。把这一格从轮换里
        # 摘掉继续跑，比让一个填错的备用规格拖垮整晚的抢机划算。
        dropped = self._drop_failing_fallback(job, cfg)
        if dropped:
            jitter = interval * RETRY_JITTER_FRACTION * random.random()
            delay = max(MIN_RETRY_INTERVAL_SEC, int(interval + jitter))
            job.next_run_at = now + timedelta(seconds=delay)
            job.status = "idle"
            job.last_error = f"备用配置 {dropped} 被 Oracle 拒绝，已从轮换中移除：{msg}"[:2000]
            log.warning("capacity dropped fallback %s job=%s: %s", dropped, job.id, msg[:200])
            return

        job.enabled = False
        job.status = "failed"
        job.next_run_at = None
        log.error("capacity permanent fail job=%s: %s", job.id, msg[:300])
        self._notify_capacity_end(db, job, reason="permanent")

    @staticmethod
    def _drop_failing_fallback(job: CapacityJob, cfg: Optional[dict[str, Any]]) -> str:
        """把这次尝试用的备用配置从轮换里摘掉，返回它的标签；主配置则返回 ""。

        cfg 是 _attempt_plan 这一轮实际选中的那一格，由调用方原样传下来 —— 不在这里
        用 attempts 反推，因为 attempts 在调用 OCI 之前就 +1 过了，反推必然差一格，
        摘错的那一格会是无辜的。

        主配置（cfg is None）那一格不摘：主配置错了就是任务本身填错了，应该停下来
        让操作员改，而不是拿着同样的错参数继续发请求。
        """
        if not isinstance(cfg, dict):
            return ""
        want = (cfg.get("ocpus"), cfg.get("memory_in_gbs"))
        kept = []
        hit = False
        for fb in job.fallback_configs or []:
            if (
                not hit
                and isinstance(fb, dict)
                and (fb.get("ocpus"), fb.get("memory_in_gbs")) == want
            ):
                hit = True
                continue
            kept.append(fb)
        if not hit:
            return ""
        try:
            label = f"{float(want[0]):g}C/{float(want[1]):g}G"
        except (TypeError, ValueError):
            label = "?"
        job.fallback_configs = kept
        return label

    def _notify_capacity_end(self, db: Session, job: CapacityJob, *, reason: str) -> None:
        title = "⏹ OCIBot 容量重试已停止"
        if reason == "max_attempts":
            body = (
                f"任务「{job.name}」已达最大重试次数（{job.attempts}/{job.max_attempts}），已自动停止。\n"
                f"最近错误：{(job.last_error or '')[:300]}\n"
                "如需继续，请在任务中心调整后重新启动。"
            )
        elif reason.startswith("额度守卫："):
            # 这条分支以前不存在，于是额度守卫的停止落进了下面的 else，被标题成
            # 「❌ 容量重试失败 / 遇到非容量错误」—— 把面板自己做出的免费额度拦截
            # 说成是 Oracle 报的错，操作员会去查 API Key 而不是去看额度。
            body = (
                f"任务「{job.name}」被面板的免费额度守卫拦下，已停止（第 {job.attempts} 次尝试）。\n"
                f"{reason[len('额度守卫：'):][:400]}\n"
                "这不是 Oracle 的错误：继续尝试会开出计费实例。请先在「账户」页确认剩余免费额度。"
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
