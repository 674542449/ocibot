"""Instance list / power / launch / rename / metrics / account."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.oci_client import OCIClientError, POWER_ACTIONS, is_capacity_message, is_rate_limit_message
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
from web.backend.models import CapacityAttempt, CapacityJob, Tenant, User
from web.backend.oci_bridge import (
    get_owned_tenant,
    get_session_for_row,
    instance_to_dict,
    op_result_dict,
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


def _config_label(payload: dict[str, Any]) -> str:
    """Short 'nC/nG' label of the shape config used for an attempt."""
    ocpus = payload.get("ocpus")
    mem = payload.get("memory_in_gbs")
    if ocpus is None and mem is None:
        return ""
    try:
        return f"{float(ocpus):g}C/{float(mem):g}G"
    except (TypeError, ValueError):
        return ""


def _visible_instances(infos: list) -> list:
    """Drop fully terminated instances from list views."""
    return [i for i in infos if str(getattr(i, "lifecycle_state", "") or "").upper() not in _HIDDEN_INSTANCE_STATES]


@router.get("/tenants/{tenant_id}/instances", response_model=list[InstanceOut])
def list_instances(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    include_subcompartments: bool = Query(True),
    resolve_ips: bool = Query(True),
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
    rows = db.scalars(
        select(Tenant).where(Tenant.owner_id == user.id, Tenant.enabled.is_(True)).order_by(Tenant.name)
    ).all()
    out: list[InstanceOut] = []
    errors: list[str] = []
    for row in rows:
        try:
            session = get_session_for_row(row)
            infos = session.list_instances_tree(resolve_ips=resolve_ips)
            if not include_terminated:
                infos = _visible_instances(infos)
            out.extend(
                InstanceOut(**instance_to_dict(i, tenant_id=row.id, tenant_name=row.name)) for i in infos
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{row.name}: {exc}")
    if not out and errors:
        raise HTTPException(status_code=502, detail="; ".join(errors))
    return out


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
        shape = str(getattr(info, "shape", "") or "").lower()
        if "e2.1.micro" in shape or shape.endswith(".micro") or not (
            shape.endswith(".flex") or ".flex." in shape
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Shape {getattr(info, 'shape', '') or '当前型号'} 为固定规格，不允许修改 OCPU / 内存（仅 *.Flex 支持）",
            )
        result = session.update_instance_shape(instance_id, body.ocpus, body.memory_in_gbs)
        return PowerActionResult(**op_result_dict(result))
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

    # Pre-launch: IPv6 + managed NSG (desktop parity)
    try:
        payload = prepare_launch_network(session, payload, meta=meta)
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

    # Capacity retry path: enqueue job, optionally try once immediately.
    if built["as_retry"]:
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

        def _log_attempt(ok: bool, message: str) -> None:
            db.add(
                CapacityAttempt(
                    job_id=job.id,
                    owner_id=user.id,
                    n=1,
                    seq=1,
                    ok=ok,
                    capacity=is_capacity_message(message or ""),
                    rate_limited=is_rate_limit_message(message or ""),
                    message=(message or "")[:2000],
                    availability_domain=str(payload.get("availability_domain") or ""),
                    config_label=_config_label(payload),
                )
            )

        # Immediate first attempt
        try:
            result = session.launch_from_payload(
                payload, root_password="", custom_user_data=custom_user_data
            )
        except Exception as exc:  # noqa: BLE001
            job.attempts = 1
            job.last_attempt_at = now
            job.last_error = str(exc)[:2000]
            job.status = "idle"
            _log_attempt(False, str(exc))
            db.commit()
            return LaunchInstanceResult(
                ok=False,
                message=f"已加入容量重试，首次尝试失败：{exc}",
                capacity_job_id=job.id,
            )

        job.attempts = 1
        job.last_attempt_at = datetime.now(timezone.utc)
        _log_attempt(bool(result.ok), result.message or "")
        if result.ok:
            job.status = "success"
            job.enabled = False
            job.next_run_at = None
            data = result.data if isinstance(result.data, dict) else {}
            job.success_instance_id = str(data.get("instance_id") or "")
            db.commit()
            schedule_post_launch_adjustments(
                session,
                instance_id=job.success_instance_id,
                compartment_id=str(payload.get("compartment_id") or ""),
                boot_vpu=boot_vpu,
            )
            msg = result.message or "创建成功"
            if boot_vpu != 10 and job.success_instance_id:
                msg += f"；引导卷性能 {boot_vpu} VPUs/GB 将在后台自动调整（hydration 完成后）"
            _audit_launch(True, msg, job.success_instance_id, job.id)
            return LaunchInstanceResult(
                ok=True,
                message=msg,
                work_request_id=result.work_request_id or "",
                instance_id=job.success_instance_id,
                capacity_job_id=job.id,
                data=data,
            )

        job.last_error = (result.message or "")[:2000]
        if is_capacity_message(result.message or ""):
            job.status = "idle"
            db.commit()
            _audit_launch(False, result.message or "capacity", "", job.id)
            return LaunchInstanceResult(
                ok=False,
                message=f"容量不足，已加入自动重试（间隔 {job.interval_sec}s）：{result.message}",
                capacity_job_id=job.id,
                data=result.data if isinstance(result.data, dict) else {},
            )
        # permanent fail: try cleanup managed nsg
        if payload.get("managed_nsg_id"):
            try:
                session.delete_managed_nsg(str(payload.get("managed_nsg_id")))
            except Exception:
                pass
        job.status = "failed"
        job.enabled = False
        db.commit()
        return LaunchInstanceResult(
            ok=False,
            message=result.message or "创建失败",
            capacity_job_id=job.id,
            data=result.data if isinstance(result.data, dict) else {},
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
