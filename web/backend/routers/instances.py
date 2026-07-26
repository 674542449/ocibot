"""Instance list / power / launch / rename / metrics / account."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.oci_client import OCIClientError, POWER_ACTIONS, is_capacity_message
from web.backend.audit import write_audit
from web.backend.auth import get_current_user
from web.backend.crypto_util import encrypt_text
from web.backend.db import get_db
from web.backend.launch_service import (
    build_launch_request,
    fetch_launch_meta,
    prepare_launch_network,
    schedule_post_launch_adjustments,
)
from web.backend.models import CapacityJob, Tenant, User
from web.backend.oci_bridge import (
    get_owned_tenant,
    get_session_for_row,
    instance_to_dict,
    op_result_dict,
)
from web.backend.quota_guard import (
    check_launch_quota,
    enforce_launch_quota,
    enforce_shape_resize_quota,
    format_guard_warnings,
    free_only_for_tier,
    usage_snapshot,
)
from web.backend.schemas import (
    InstanceOut,
    LaunchInstanceRequest,
    LaunchInstanceResult,
    PowerActionRequest,
    PowerActionResult,
    RenameRequest,
    ShapeConfigRequest,
    TerminateRequest,
)

router = APIRouter(tags=["instances"])


def _tenant_or_404(db: Session, user_id: str, tenant_id: str) -> Tenant:
    try:
        return get_owned_tenant(db, user_id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


_HIDDEN_INSTANCE_STATES = frozenset({"TERMINATED"})


def _visible_instances(infos: list) -> list:
    """Drop fully terminated instances from list views."""
    return [i for i in infos if str(getattr(i, "lifecycle_state", "") or "").upper() not in _HIDDEN_INSTANCE_STATES]


@router.get("/tenants/{tenant_id}/instances", response_model=list[InstanceOut])
def list_instances(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    include_subcompartments: bool = Query(True),
    # Default False: IP resolution multiplies OCI calls; opt in from the UI.
    resolve_ips: bool = Query(False),
    include_terminated: bool = Query(False),
) -> list[InstanceOut]:
    row = _tenant_or_404(db, user.id, tenant_id)
    if not row.enabled:
        raise HTTPException(status_code=400, detail="租户已禁用")
    try:
        session = get_session_for_row(row)
        if include_subcompartments:
            infos = session.list_instances_tree(resolve_ips=resolve_ips)
        else:
            infos = session.list_instances(resolve_ips=resolve_ips)
        if not include_terminated:
            infos = _visible_instances(infos)
        return [
            InstanceOut(**instance_to_dict(i, tenant_id=row.id, tenant_name=row.name))
            for i in infos
        ]
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"列出实例失败: {exc}") from exc


@router.get("/instances", response_model=list[InstanceOut])
def list_all_instances(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    resolve_ips: bool = Query(False),
    include_terminated: bool = Query(False),
) -> list[InstanceOut]:
    """Disabled: multi-tenant fan-out against OCI is too expensive and easy to trigger by accident.

    Use ``GET /tenants/{tenant_id}/instances`` for a single tenant at a time.
    """
    _ = (user, db, resolve_ips, include_terminated)
    raise HTTPException(
        status_code=400,
        detail="已禁用「全部租户聚合」列表，请选择单个租户后再刷新实例，避免多账号同时请求 Oracle API",
    )


@router.get("/tenants/{tenant_id}/instances/{instance_id}", response_model=InstanceOut)
def get_instance(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> InstanceOut:
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=True)
        return InstanceOut(**instance_to_dict(info, tenant_id=row.id, tenant_name=row.name))
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/instances/{instance_id}/power", response_model=PowerActionResult)
def power_action(
    tenant_id: str,
    instance_id: str,
    body: PowerActionRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    action = (body.action or "").strip().upper()
    allowed = set(POWER_ACTIONS) | {
        "START",
        "STOP",
        "SOFTSTOP",
        "RESET",
        "SOFTRESET",
        "SENDDIAGNOSTICINTERRUPT",
        "DIAGNOSTICREBOOT",
        "REBOOTMIGRATE",
    }
    if action not in allowed:
        raise HTTPException(status_code=400, detail=f"不支持的电源操作: {action}")
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.instance_action(instance_id, action)
        write_audit(
            db,
            owner_id=user.id,
            action=f"instance.power.{action}",
            target=instance_id,
            detail={"tenant_id": tenant_id, "ok": result.ok, "message": result.message},
        )
        return PowerActionResult(**op_result_dict(result))
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/instances/{instance_id}/terminate", response_model=PowerActionResult)
def terminate_instance(
    tenant_id: str,
    instance_id: str,
    body: TerminateRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.terminate_instance(
            instance_id,
            preserve_boot_volume=body.preserve_boot_volume,
        )
        write_audit(
            db,
            owner_id=user.id,
            action="instance.terminate",
            target=instance_id,
            detail={
                "tenant_id": tenant_id,
                "preserve_boot_volume": body.preserve_boot_volume,
                "ok": result.ok,
                "message": result.message,
            },
        )
        return PowerActionResult(**op_result_dict(result))
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/instances/{instance_id}/rename", response_model=PowerActionResult)
def rename_instance(
    tenant_id: str,
    instance_id: str,
    body: RenameRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.rename_instance(instance_id, body.display_name.strip())
        return PowerActionResult(**op_result_dict(result))
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/instances/{instance_id}/shape", response_model=PowerActionResult)
def update_shape(
    tenant_id: str,
    instance_id: str,
    body: ShapeConfigRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        # Extra guard: fixed shapes like E2.1.Micro cannot change OCPU/memory
        info = session.get_instance(instance_id, resolve_ips=False)
        shape_raw = str(getattr(info, "shape", "") or "")
        shape = shape_raw.lower()
        if "e2.1.micro" in shape or shape.endswith(".micro") or not (
            shape.endswith(".flex") or ".flex." in shape
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Shape {shape_raw or '当前型号'} 为固定规格，不允许修改 OCPU / 内存（仅 *.Flex 支持）",
            )
        guard = enforce_shape_resize_quota(
            session,
            account_tier=getattr(row, "account_tier", "") or "",
            shape=shape_raw,
            current_ocpus=getattr(info, "ocpus", None),
            current_memory_in_gbs=getattr(info, "memory_gb", None)
            if getattr(info, "memory_gb", None) is not None
            else getattr(info, "memory_in_gbs", None),
            new_ocpus=body.ocpus,
            new_memory_in_gbs=body.memory_in_gbs,
        )
        result = session.update_instance_shape(instance_id, body.ocpus, body.memory_in_gbs)
        out = PowerActionResult(**op_result_dict(result))
        warns = format_guard_warnings(guard)
        if warns and out.ok:
            out.message = (out.message or "已提交规格变更") + "（提醒：" + "；".join(warns) + "）"
        return out
    except HTTPException:
        raise
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/tenants/{tenant_id}/instances/{instance_id}/public-ip/replace",
    response_model=PowerActionResult,
)
def replace_public_ip(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.replace_ephemeral_public_ip(instance_id, info.compartment_id)
        return PowerActionResult(**op_result_dict(result))
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/tenants/{tenant_id}/instances/{instance_id}/ipv6",
    response_model=PowerActionResult,
)
def assign_ipv6(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.assign_public_ipv6(instance_id, info.compartment_id)
        return PowerActionResult(**op_result_dict(result))
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/tenants/{tenant_id}/instances/{instance_id}/metrics")
def instance_metrics(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    hours: int = Query(3, ge=1, le=24),
) -> dict[str, Any]:
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.get_instance_metrics(instance_id, info.compartment_id, hours=hours)
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": result.data if isinstance(result.data, dict) else {"raw": result.data},
        }
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/tenants/{tenant_id}/account")
def account_status(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.get_account_status()
        data = result.data if isinstance(result.data, dict) else {}
        # cache tier on tenant row when known
        tier_code = str(data.get("tier_code") or "")
        if tier_code in {"free", "paid"} and row.account_tier != tier_code:
            row.account_tier = tier_code
            db.commit()
        return {"ok": bool(result.ok), "message": result.message or "", "data": data}
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/tenants/{tenant_id}/usage")
def account_usage(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    days: int = Query(30, ge=1, le=90),
) -> dict[str, Any]:
    """Daily COST summary via OCI Usage API (best-effort)."""
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.get_usage_summary(days=days)
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": result.data if isinstance(result.data, dict) else {},
        }
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc

@router.get("/tenants/{tenant_id}/free-quota")
def free_quota(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    free_only_mode: bool = Query(True),
) -> dict[str, Any]:
    """Always-Free usage gauges (compute + storage) for the dashboard."""
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.get_free_quota_usage(free_only_mode=free_only_mode)
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": result.data if isinstance(result.data, dict) else {},
        }
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"读取免费额度失败: {exc}") from exc


@router.post("/tenants/{tenant_id}/launch-quota-check")
def launch_quota_check(
    tenant_id: str,
    body: LaunchInstanceRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Dry-run the Always-Free guard for a proposed configuration.

    Deliberately reuses check_launch_quota — the same function the launch path
    enforces with — so the panel's pre-submit verdict cannot drift from the
    server's. Returns the guard verdict plus the usage snapshot it was based on,
    so the UI can show both "what you have used" and "what this would need".
    """
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        tier = getattr(row, "account_tier", "") or ""
        free_only = free_only_for_tier(tier)
        usage = usage_snapshot(session, free_only_mode=free_only)
        guard = check_launch_quota(
            session,
            account_tier=tier,
            shape=str(body.shape or ""),
            ocpus=body.ocpus,
            memory_in_gbs=body.memory_in_gbs,
            boot_volume_size_in_gbs=body.boot_volume_size_in_gbs,
            boot_volume_vpus_per_gb=body.boot_volume_vpus_per_gb or 10,
            usage=usage,
        )
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"校验免费额度失败: {exc}") from exc

    out = guard.to_dict()
    out["free_only_mode"] = free_only
    out["account_tier"] = str(usage.get("account_tier") or tier or "")
    # read_incomplete means the numbers below are an undercount; the launch path
    # refuses outright in that case, so tell the UI rather than showing a total
    # that looks like plenty of headroom.
    out["read_incomplete"] = bool(usage.get("read_incomplete"))
    out["limits"] = usage.get("limits") or {}
    out["usage"] = usage.get("usage") or {}
    out["remaining"] = usage.get("remaining") or {}
    out["buckets"] = usage.get("buckets") or {}
    out["overall_status"] = usage.get("overall_status") or ""
    out["summary_lines"] = usage.get("summary_lines") or []
    return out


@router.get("/tenants/{tenant_id}/launch-meta")
def launch_meta(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    force: bool = Query(False),
) -> dict[str, Any]:
    row = _tenant_or_404(db, user.id, tenant_id)
    if not row.enabled:
        raise HTTPException(status_code=400, detail="租户已禁用")
    try:
        session = get_session_for_row(row)
        meta = fetch_launch_meta(session, tenant_id=row.id, force=force)
        return meta
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"加载创建元数据失败: {exc}") from exc


@router.post("/tenants/{tenant_id}/launch", response_model=LaunchInstanceResult)
def launch_instance(
    tenant_id: str,
    body: LaunchInstanceRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> LaunchInstanceResult:
    row = _tenant_or_404(db, user.id, tenant_id)
    if not row.enabled:
        raise HTTPException(status_code=400, detail="租户已禁用")
    try:
        session = get_session_for_row(row)
        meta = fetch_launch_meta(session, tenant_id=row.id, force=False)
        built = build_launch_request(body.model_dump(), meta=meta)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    payload = built["payload"]
    root_password = built["root_password"]
    custom_user_data = str(built.get("custom_user_data") or "")
    boot_vpu = int(payload.get("boot_volume_vpus_per_gb") or 10)

    # Always Free guard BEFORE network/NSG prep so we don't leave orphan resources.
    try:
        launch_guard = enforce_launch_quota(
            session,
            account_tier=getattr(row, "account_tier", "") or "",
            shape=str(payload.get("shape") or ""),
            ocpus=payload.get("ocpus"),
            memory_in_gbs=payload.get("memory_in_gbs"),
            boot_volume_size_in_gbs=payload.get("boot_volume_size_in_gbs"),
            boot_volume_vpus_per_gb=boot_vpu,
            fallback_configs=list(built.get("fallback_configs") or body.fallback_configs or []),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # A quota-read failure must not surface as an unhandled 500 (this branch
        # previously re-raised HTTPException only, so anything else escaped).
        raise HTTPException(status_code=502, detail=f"校验免费额度失败: {exc}") from exc

    # Pre-launch: IPv6 + managed NSG (desktop parity)
    try:
        payload = prepare_launch_network(
            session, payload, meta=meta, for_retry=bool(built["as_retry"])
        )
        built["payload"] = payload
    except (ValueError, OCIClientError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"准备网络/NSG 失败: {exc}") from exc

    def _audit_launch(ok: bool, message: str, instance_id: str = "", job_id: str = "") -> None:
        write_audit(
            db,
            owner_id=user.id,
            action="instance.launch",
            target=instance_id or payload.get("display_name", ""),
            detail={
                "tenant_id": tenant_id,
                "ok": ok,
                "message": message,
                "shape": payload.get("shape"),
                "as_retry": bool(built.get("as_retry")),
                "capacity_job_id": job_id,
            },
        )

    # Capacity retry path: enqueue a job and let the WORKER own every LaunchInstance
    # call. An immediate launch here would race the worker (a second launch for the
    # same tenant) and, on a capacity miss, leave next_run_at=now so the worker fires
    # attempt #2 under the 60s floor. Queue-only keeps retries compliant.
    if built["as_retry"]:
        # Compliance: at most one active capacity-retry job per tenant.
        existing = db.scalar(
            select(CapacityJob)
            .where(
                CapacityJob.tenant_id == row.id,
                CapacityJob.owner_id == user.id,
                CapacityJob.enabled.is_(True),
                CapacityJob.status.in_(("idle", "running")),
            )
            .limit(1)
        )
        if existing is not None:
            if payload.get("managed_nsg_id"):
                try:
                    session.delete_managed_nsg(str(payload.get("managed_nsg_id")))
                except Exception:
                    pass
            raise HTTPException(
                status_code=409,
                detail="该租户已有进行中的容量重试任务，请先在任务中心停止或删除后再新建",
            )
        now = datetime.now(timezone.utc)
        job = CapacityJob(
            owner_id=user.id,
            tenant_id=row.id,
            name=f"容量重试 · {payload.get('display_name') or 'instance'}",
            enabled=True,
            status="idle",
            launch_payload=payload,
            availability_domains=list(built.get("availability_domains") or []),
            fallback_configs=list(built.get("fallback_configs") or []),
            user_data_encrypted=encrypt_text(custom_user_data) if custom_user_data else "",
            interval_sec=int(built["retry_interval_sec"]),
            max_attempts=int(built["retry_max_attempts"]),
            attempts=0,
            next_run_at=now,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        _audit_launch(False, "已加入容量重试队列", "", job.id)
        retry_msg = (
            f"已加入容量重试：后台将每 {job.interval_sec}s 尝试一次"
            f"（最多 {job.max_attempts} 次）。请在「任务中心」查看进度与日志"
            "（需保持 worker 进程运行）。"
        )
        warns = format_guard_warnings(launch_guard)
        if warns:
            retry_msg += " 提醒：" + "；".join(warns)
        return LaunchInstanceResult(
            ok=True,
            message=retry_msg,
            capacity_job_id=job.id,
        )

    # Direct launch
    try:
        result = session.launch_from_payload(
            payload, root_password=root_password, custom_user_data=custom_user_data
        )
    except Exception as exc:  # noqa: BLE001
        if payload.get("managed_nsg_id"):
            try:
                session.delete_managed_nsg(str(payload.get("managed_nsg_id")))
            except Exception:
                pass
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    data = result.data if isinstance(result.data, dict) else {}
    instance_id = str(data.get("instance_id") or "")
    if result.ok:
        schedule_post_launch_adjustments(
            session,
            instance_id=instance_id,
            compartment_id=str(payload.get("compartment_id") or ""),
            boot_vpu=boot_vpu,
        )
        msg = result.message or "创建成功"
        if boot_vpu != 10 and instance_id:
            msg += f"；引导卷性能 {boot_vpu} VPUs/GB 将在后台自动调整（hydration 完成后）"
        warns = format_guard_warnings(launch_guard)
        if warns:
            msg += "；提醒：" + "；".join(warns)
        _audit_launch(True, msg, instance_id)
        return LaunchInstanceResult(
            ok=True,
            message=msg,
            work_request_id=result.work_request_id or "",
            instance_id=instance_id,
            root_password=root_password if root_password else "",
            data=data,
        )

    # fail: cleanup nsg for non-capacity password/key permanent errors
    capacity_failure = bool((data or {}).get("capacity")) or is_capacity_message(result.message or "")
    if payload.get("managed_nsg_id") and (not capacity_failure or payload.get("auth_mode") == "password"):
        try:
            session.delete_managed_nsg(str(payload.get("managed_nsg_id")))
        except Exception:
            pass
    _audit_launch(False, result.message or "创建失败", instance_id)
    return LaunchInstanceResult(
        ok=False,
        message=result.message or "创建失败",
        work_request_id=result.work_request_id or "",
        instance_id=instance_id,
        data=data,
    )
