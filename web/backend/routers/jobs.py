"""Capacity retry APIs. Power schedules were removed in 0.4.36 (unused)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.oci_client import sanitize_launch_payload
from app.scheduler import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_INTERVAL_SEC,
    clamp_max_attempts,
    clamp_retry_interval,
)
from web.backend.auth import get_current_user
from web.backend.db import get_db
from web.backend.launch_service import normalize_fallback_configs, shape_is_flex
from web.backend.models import CapacityAttempt, CapacityJob, User
from web.backend.oci_bridge import get_owned_tenant, get_session_for_row
from web.backend.quota_guard import (
    enforce_launch_quota,
    enforce_secondary_region,
    free_only_for_tenant,
    tenant_is_secondary,
)
from web.backend.schemas import (
    MAX_AVAILABILITY_DOMAIN,
    MAX_AVAILABILITY_DOMAINS,
    MAX_CAPACITY_JOB_NAME,
    CapacityAttemptOut,
    CapacityJobCreate,
    CapacityJobOut,
    MessageOut,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# 「一个租户只能有一个在跑的任务」那条规则在 enabled=false 时是跳过的（见下面的
# 注释），所以停用的任务行数原本没有任何上限 —— 唯一的天花板是 32MB 的请求体。
# 每一行都拖着一份 launch_payload JSON，而 GET /jobs/capacity 是全量返回的，
# 于是不需要任何 Oracle 调用就能把库和列表接口一起撑爆。
MAX_CAPACITY_JOBS_PER_USER = 100


def _capacity_out(row: CapacityJob) -> CapacityJobOut:
    return CapacityJobOut(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        enabled=bool(row.enabled),
        status=row.status,
        interval_sec=row.interval_sec,
        max_attempts=row.max_attempts,
        attempts=row.attempts,
        last_error=row.last_error or "",
        last_attempt_at=row.last_attempt_at,
        next_run_at=row.next_run_at,
        cooldown_until=row.cooldown_until,
        consecutive_rate_limits=row.consecutive_rate_limits or 0,
        success_instance_id=row.success_instance_id or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
        launch_payload=dict(row.launch_payload or {}),
        fallback_configs=list(row.fallback_configs or []),
        has_user_data=bool(row.user_data_encrypted),
    )


@router.get("/capacity", response_model=list[CapacityJobOut])
def list_capacity_jobs(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[CapacityJobOut]:
    rows = db.scalars(
        select(CapacityJob).where(CapacityJob.owner_id == user.id).order_by(CapacityJob.created_at.desc())
    ).all()
    return [_capacity_out(r) for r in rows]


@router.post("/capacity", response_model=CapacityJobOut, status_code=201)
def create_capacity_job(
    body: CapacityJobCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CapacityJobOut:
    try:
        tenant = get_owned_tenant(db, user.id, body.tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        payload = sanitize_launch_payload(body.launch_payload, for_retry=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 行数上限放在 enforce_launch_quota **之前**：那一步是一整轮租户枚举，会实打
    # 实地花掉 Oracle 的速率预算（抢机循环和它抢的是同一个额度）。任何不需要
    # Oracle 就能判定的拒绝，都不该排在它后面。
    total = (
        db.scalar(
            select(func.count()).select_from(CapacityJob).where(CapacityJob.owner_id == user.id)
        )
        or 0
    )
    if total >= MAX_CAPACITY_JOBS_PER_USER:
        raise HTTPException(
            status_code=409,
            detail=f"容量重试任务数量已达上限（{MAX_CAPACITY_JOBS_PER_USER}），请先删除历史任务",
        )

    # Downgrade candidates must survive to the row: the worker reads
    # job.fallback_configs to rotate configs, so dropping them here silently
    # turned a multi-config retry into a single-config one.
    try:
        fallback_configs = normalize_fallback_configs(
            body.fallback_configs or payload.get("fallback_configs") or [],
            is_flex=shape_is_flex(str(payload.get("shape") or "")),
            as_retry=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Always Free guard — same rules as the launch wizard, including the 副区 gate
    # (free caps are home-region only, so there they are replaced not stacked).
    try:
        session = get_session_for_row(tenant)
        free_only = free_only_for_tenant(tenant)
        secondary = enforce_secondary_region(
            session,
            free_only_mode=free_only,
            secondary_hint=tenant_is_secondary(tenant),
            region_hint=tenant.region or "",
        )
        if not secondary:
            enforce_launch_quota(
                session,
                account_tier=getattr(tenant, "account_tier", "") or "",
                shape=str(payload.get("shape") or ""),
                ocpus=payload.get("ocpus"),
                memory_in_gbs=payload.get("memory_in_gbs"),
                boot_volume_size_in_gbs=payload.get("boot_volume_size_in_gbs"),
                boot_volume_vpus_per_gb=payload.get("boot_volume_vpus_per_gb") or 10,
                fallback_configs=fallback_configs,
                free_only_mode=free_only,
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"校验免费额度失败: {exc}") from exc

    interval = clamp_retry_interval(body.interval_sec or DEFAULT_RETRY_INTERVAL_SEC)
    max_attempts = clamp_max_attempts(body.max_attempts or DEFAULT_MAX_ATTEMPTS)
    now = datetime.now(timezone.utc)
    # schemas.CapacityJobCreate 已经把这两个字段卡在列宽内了；这里再截一刀是因为
    # 下面的 db.commit() 没有（也不该有）异常兜底：在 PostgreSQL 上一次
    # DataError 就是一个没有任何信息的 500，而它发生在 enforce_launch_quota 已经
    # 把租户枚举花出去之后。校验层将来被放宽时，这里仍然只会写出合法宽度。
    name = (body.name or "容量重试").strip()[:MAX_CAPACITY_JOB_NAME] or "容量重试"
    ads = [
        str(a)[:MAX_AVAILABILITY_DOMAIN]
        for a in (body.availability_domains or [])[:MAX_AVAILABILITY_DOMAINS]
    ]
    row = CapacityJob(
        owner_id=user.id,
        tenant_id=body.tenant_id,
        name=name,
        enabled=bool(body.enabled),
        status="idle" if body.enabled else "stopped",
        launch_payload=payload,
        availability_domains=ads,
        fallback_configs=fallback_configs,
        interval_sec=interval,
        max_attempts=max_attempts,
        attempts=0,
        next_run_at=now if body.enabled else None,
    )
    # Re-checked immediately before the INSERT rather than before the quota
    # enumeration above: that left a multi-second window in which two concurrent
    # creates both passed and produced two jobs racing LaunchInstance.
    if body.enabled:
        existing = db.scalar(
            select(CapacityJob)
            .where(
                CapacityJob.tenant_id == body.tenant_id,
                CapacityJob.owner_id == user.id,
                CapacityJob.enabled.is_(True),
                CapacityJob.status.in_(("idle", "running")),
            )
            .limit(1)
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail="该租户已有进行中的容量重试任务，请先在任务中心停止或删除后再新建",
            )

    db.add(row)
    db.commit()
    db.refresh(row)
    return _capacity_out(row)


@router.get("/capacity/{job_id}/attempts", response_model=list[CapacityAttemptOut])
def list_capacity_attempts(
    job_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
) -> list[CapacityAttemptOut]:
    """Attempt log for a job. Poll with after_seq for incremental updates."""
    job = db.get(CapacityJob, job_id)
    if job is None or job.owner_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    rows = db.scalars(
        select(CapacityAttempt)
        .where(CapacityAttempt.job_id == job_id, CapacityAttempt.seq > after_seq)
        .order_by(CapacityAttempt.seq.desc())
        .limit(limit)
    ).all()
    return [CapacityAttemptOut.model_validate(r) for r in reversed(rows)]


@router.post("/capacity/{job_id}/stop", response_model=CapacityJobOut)
def stop_capacity_job(
    job_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CapacityJobOut:
    row = db.get(CapacityJob, job_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    row.enabled = False
    if row.status not in ("success", "failed"):
        row.status = "stopped"
    row.locked_by = None
    row.locked_until = None
    db.commit()
    db.refresh(row)
    return _capacity_out(row)


@router.post("/capacity/{job_id}/resume", response_model=CapacityJobOut)
def resume_capacity_job(
    job_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CapacityJobOut:
    row = db.get(CapacityJob, job_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row.status == "success":
        raise HTTPException(status_code=400, detail="任务已成功，请新建任务")
    # An exhausted job resumed to status=idle is immediately re-failed by the
    # worker's max-attempts check, so 「继续」 reported success and did nothing.
    if int(row.attempts or 0) >= clamp_max_attempts(row.max_attempts):
        raise HTTPException(status_code=400, detail="已达最大重试次数，请新建任务")
    # Same one-active-retry-per-tenant rule the create paths enforce. Resuming
    # skipped it, so stopping job A, creating job B, then resuming A left two
    # active jobs racing LaunchInstance for the same tenant.
    conflict = db.scalar(
        select(CapacityJob)
        .where(
            CapacityJob.tenant_id == row.tenant_id,
            CapacityJob.owner_id == user.id,
            CapacityJob.id != row.id,
            CapacityJob.enabled.is_(True),
            CapacityJob.status.in_(("idle", "running")),
        )
        .limit(1)
    )
    if conflict is not None:
        raise HTTPException(
            status_code=409,
            detail="该租户已有进行中的容量重试任务，请先在任务中心停止或删除后再恢复",
        )
    row.enabled = True
    row.status = "idle"
    # 「继续」不能把间隔下限清零。
    #
    # 这里原来是 next_run_at = now，无条件。worker 唯一的间隔判据就是候选条件里的
    # next_run_at <= now，从来没有人拿 last_attempt_at 比过，所以下一次
    # LaunchInstance 会在一个轮询周期（5s）内发出去。面板上 停止/继续 是同一个格
    # 子里轮换的两个按钮，又没有「立即重试」，所以「停止→继续」就是用户表达「现
    # 在就再试一次」的自然手势 —— 连点几下就是约 12 次/分钟，而
    # app/scheduler.py::MIN_RETRY_INTERVAL_SEC 写死的合规下限是 1 次/60 秒，那个
    # 模块存在的全部理由就是这条线。429 冷却一直是生效的，没有兜底的恰恰是普通
    # 间隔。worker.tick_capacity 里有同样一道地板，这样绕过 API 也无效。
    now = datetime.now(timezone.utc)
    last_attempt = row.last_attempt_at
    if last_attempt is not None and last_attempt.tzinfo is None:
        # SQLite 存的是 naive，直接和 aware 比较会 TypeError。
        last_attempt = last_attempt.replace(tzinfo=timezone.utc)
    if last_attempt is None:
        row.next_run_at = now
    else:
        earliest = last_attempt + timedelta(seconds=clamp_retry_interval(row.interval_sec))
        row.next_run_at = max(now, earliest)
    row.last_error = ""
    db.commit()
    db.refresh(row)
    return _capacity_out(row)


@router.delete("/capacity/{job_id}", response_model=MessageOut)
def delete_capacity_job(
    job_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> MessageOut:
    row = db.get(CapacityJob, job_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    db.delete(row)
    db.commit()
    return MessageOut(message="已删除")


# ---- schedules ----
