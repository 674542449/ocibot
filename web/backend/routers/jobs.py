"""Capacity retry + schedule job APIs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
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
from web.backend.models import CapacityAttempt, CapacityJob, ScheduleJobRow, ScheduleRun, User
from web.backend.oci_bridge import get_owned_tenant, get_session_for_row
from web.backend.quota_guard import enforce_launch_quota
from web.backend.schemas import (
    CapacityAttemptOut,
    CapacityJobCreate,
    CapacityJobOut,
    MessageOut,
    ScheduleJobCreate,
    ScheduleJobOut,
    ScheduleRunOut,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])


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

    # Compliance: at most one active capacity-retry job per tenant (serialize retries).
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

    try:
        payload = sanitize_launch_payload(body.launch_payload, for_retry=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    # Always Free guard — same rules as the launch wizard.
    try:
        session = get_session_for_row(tenant)
        enforce_launch_quota(
            session,
            account_tier=getattr(tenant, "account_tier", "") or "",
            shape=str(payload.get("shape") or ""),
            ocpus=payload.get("ocpus"),
            memory_in_gbs=payload.get("memory_in_gbs"),
            boot_volume_size_in_gbs=payload.get("boot_volume_size_in_gbs"),
            boot_volume_vpus_per_gb=payload.get("boot_volume_vpus_per_gb") or 10,
            fallback_configs=fallback_configs,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"校验免费额度失败: {exc}") from exc

    interval = clamp_retry_interval(body.interval_sec or DEFAULT_RETRY_INTERVAL_SEC)
    max_attempts = clamp_max_attempts(body.max_attempts or DEFAULT_MAX_ATTEMPTS)
    now = datetime.now(timezone.utc)
    row = CapacityJob(
        owner_id=user.id,
        tenant_id=body.tenant_id,
        name=(body.name or "容量重试").strip(),
        enabled=bool(body.enabled),
        status="idle" if body.enabled else "stopped",
        launch_payload=payload,
        availability_domains=list(body.availability_domains or []),
        fallback_configs=fallback_configs,
        interval_sec=interval,
        max_attempts=max_attempts,
        attempts=0,
        next_run_at=now if body.enabled else None,
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
    row.next_run_at = datetime.now(timezone.utc)
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


@router.get("/schedules", response_model=list[ScheduleJobOut])
def list_schedules(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[ScheduleJobOut]:
    rows = db.scalars(
        select(ScheduleJobRow).where(ScheduleJobRow.owner_id == user.id).order_by(ScheduleJobRow.name)
    ).all()
    return [ScheduleJobOut.model_validate(r) for r in rows]


@router.post("/schedules", response_model=ScheduleJobOut, status_code=201)
def create_schedule(
    body: ScheduleJobCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ScheduleJobOut:
    try:
        get_owned_tenant(db, user.id, body.tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    action = (body.action or "SOFTSTOP").strip().upper()
    if action not in {"START", "STOP", "SOFTSTOP", "RESET", "SOFTRESET"}:
        raise HTTPException(status_code=400, detail=f"不支持的动作: {action}")
    kind = (body.kind or "weekly").strip().lower()
    if kind not in {"weekly", "once"}:
        raise HTTPException(status_code=400, detail="kind 必须为 weekly 或 once")

    tod = (body.time_of_day or "22:00").strip()
    run_at = body.run_at
    if kind == "weekly":
        if len(tod) != 5 or tod[2] != ":":
            raise HTTPException(status_code=400, detail="time_of_day 格式应为 HH:MM")
        if not body.weekdays:
            raise HTTPException(status_code=400, detail="每周任务需要至少选择一个星期")
        run_at = None
    else:
        if run_at is None:
            raise HTTPException(status_code=400, detail="一次性任务需要提供 run_at 时间")
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        if run_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="run_at 必须是将来的时间")

    row = ScheduleJobRow(
        owner_id=user.id,
        tenant_id=body.tenant_id,
        name=body.name.strip(),
        enabled=bool(body.enabled),
        kind=kind,
        time_of_day=tod,
        weekdays=list(body.weekdays or []),
        run_at=run_at,
        action=action,
        instance_ids=list(body.instance_ids or []),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ScheduleJobOut.model_validate(row)


@router.get("/schedules/runs", response_model=list[ScheduleRunOut])
def list_schedule_runs(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    job_id: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
) -> list[ScheduleRunOut]:
    stmt = select(ScheduleRun).where(ScheduleRun.owner_id == user.id)
    if job_id:
        stmt = stmt.where(ScheduleRun.job_id == job_id)
    rows = db.scalars(stmt.order_by(ScheduleRun.created_at.desc()).limit(limit)).all()
    return [ScheduleRunOut.model_validate(r) for r in rows]


@router.delete("/schedules/{job_id}", response_model=MessageOut)
def delete_schedule(
    job_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> MessageOut:
    row = db.get(ScheduleJobRow, job_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    db.delete(row)
    db.commit()
    return MessageOut(message="已删除")
