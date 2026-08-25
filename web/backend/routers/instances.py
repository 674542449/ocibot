"""Instance list / power / launch / rename / metrics / account."""

from __future__ import annotations

import logging
from urllib.parse import quote
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.oci_client import (
    OCIClientError,
    POWER_ACTIONS,
    TERMINATE_PROTECT_TAG,
    is_capacity_message,
)
from web.backend.audit import write_audit
from web.backend.auth import get_current_user
from web.backend.crypto_util import encrypt_text
from web.backend.db import get_db
from app.oci_client import derive_retry_token, generate_root_password
from web.backend.launch_service import (
    build_launch_request,
    fetch_launch_meta,
    launch_meta_state,
    peek_launch_meta,
    prepare_launch_network,
    schedule_post_launch_adjustments,
    start_meta_refresh,
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
    enforce_secondary_region,
    enforce_shape_resize_quota,
    format_guard_warnings,
    free_only_for_tenant,
    region_pair,
    tenant_is_secondary,
    tenant_launch_lock,
    usage_snapshot,
)
from web.backend.schemas import (
    CapacityReportRequest,
    InstanceOut,
    LaunchInstanceRequest,
    LaunchInstanceResult,
    PowerActionRequest,
    PowerActionResult,
    RenameRequest,
    RootPasswordNoteRequest,
    ShapeConfigRequest,
    TerminateRequest,
)

router = APIRouter(tags=["instances"])

log = logging.getLogger("ocibot.instances")


class _PhaseTimer:
    """Record how long each phase of a launch takes, and log it once at the end.

    Creating an instance is the panel's longest request: a free-quota read that
    lists instances and volumes across every compartment, then network/NSG
    preparation that WRITES to Oracle, then LaunchInstance itself. Behind
    Cloudflare's free plan there is a hard 100-second ceiling on the whole thing,
    and overruns come back as a 520 with nothing on our side saying which part was
    slow.

    Two fixes have already been aimed at this from inference rather than
    measurement, and neither finished the job. This logs one line per launch —
    always, success or failure — so the next report is data instead of another
    guess about which phase to optimise.
    """

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._last = self._t0
        self._phases: list[tuple[str, float]] = []

    def mark(self, name: str) -> None:
        now = time.monotonic()
        self._phases.append((name, now - self._last))
        self._last = now

    def emit(self, outcome: str) -> None:
        total = time.monotonic() - self._t0
        detail = " ".join(f"{n}={d:.1f}s" for n, d in self._phases)
        # WARNING past 60s: that is the point where the request is close enough to
        # the proxy ceiling that the next slightly slower tenancy will fail.
        level = logging.WARNING if total >= 60 else logging.INFO
        log.log(
            level,
            "launch timing outcome=%s total=%.1fs %s",
            outcome,
            total,
            detail,
        )


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
    response: Response = None,  # type: ignore[assignment]
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
        # 「部分 compartment 读不到」必须传出去。
        #
        # list_instances_tree 只在「只剩一个 compartment、且没扫到任何实例」时才抛；
        # 有两个以上 compartment 而**全部**被拒时，它安静地返回空列表，前端于是渲染
        # 「暂无实例。请先在「租户」添加 API」—— 把一次权限拒绝说成了一个空账号，
        # 还让人去做一件他早就做过的事。部分成功更糟：返回一个看着合理的子集，
        # 没有任何标记说明它不完整。
        #
        # 用响应头而不是改返回结构：这个路由的 response_model 是纯数组，
        # 换成对象会同时打破前端契约和 TS 类型。
        tree_errors = list(getattr(session, "_last_tree_errors", []) or [])
        if tree_errors and response is not None:
            response.headers["X-Ocibot-Partial"] = "1"
            # header 只能装 latin-1，错误里几乎一定有中文，所以百分号编码。
            response.headers["X-Ocibot-Partial-Reason"] = quote(
                f"{len(tree_errors)} 处 compartment 读取失败：{tree_errors[0]}"[:400]
            )
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
        # 终止保护：这是全站唯一不可逆的操作，值得一道独立于 UI 的闸门。
        #
        # 上一轮审计查出过「详情页显示 A 实例、按钮却打 B 实例」的 bug —— 那类
        # 缺陷的共同点是确认框问的和实际执行的不是同一个对象，所以再谨慎的确认
        # 文案也救不了。这里在服务端按实例自己的标签判断，UI 传什么都绕不过去。
        info = session.get_instance(instance_id)
        if str((getattr(info, "freeform_tags", None) or {}).get(TERMINATE_PROTECT_TAG, "")).strip().lower() == "true":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"实例「{getattr(info, 'display_name', '') or instance_id[-12:]}」"
                    "已开启终止保护。请先在详情页解除保护，再执行终止。"
                ),
            )
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
    except HTTPException:
        # 终止保护返回的是 409，不能被下面的兜底改写成 502。
        raise
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


@router.post(
    "/tenants/{tenant_id}/instances/{instance_id}/root-password",
    response_model=PowerActionResult,
)
def set_root_password_note(
    tenant_id: str,
    instance_id: str,
    body: RootPasswordNoteRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    """Update the root password remembered against an instance.

    The value is only ever a memo — nothing in the panel authenticates with it —
    but it is written once at launch and then goes stale the moment the operator
    changes the password over SSH, which is precisely when it needs to be right.
    """
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.set_root_password_note(instance_id, body.root_password)
        # Never the value itself — the audit log is readable by every admin. The
        # OUTCOME is recorded, not just the intent: writing "set" for a call
        # Oracle rejected would leave the log asserting a change that never
        # happened, which is worse than having no entry.
        write_audit(
            db,
            owner_id=user.id,
            action="instance.root_password_note",
            target=instance_id,
            detail={
                "tenant_id": tenant_id,
                "action": "set" if (body.root_password or "").strip() else "cleared",
                "ok": bool(result.ok),
                "message": result.message,
            },
        )
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
        free_only = free_only_for_tenant(row)
        # 副区: the free caps do not apply there and the snapshot only counts that
        # region, so measuring a deliberately-paid resize against them would block
        # it with a nonsensical "超过免费上限". The gate refuses instead if the
        # tenant still has free-only on.
        if enforce_secondary_region(
            session,
            free_only_mode=free_only,
            secondary_hint=tenant_is_secondary(row),
            region_hint=row.region or "",
        ):
            result = session.update_instance_shape(instance_id, body.ocpus, body.memory_in_gbs)
            return PowerActionResult(**op_result_dict(result))
        guard = enforce_shape_resize_quota(
            session,
            account_tier=getattr(row, "account_tier", "") or "",
            free_only_mode=free_only,
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


@router.delete(
    "/tenants/{tenant_id}/instances/{instance_id}/ipv6",
    response_model=PowerActionResult,
)
def remove_ipv6(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    """Remove the instance's IPv6 address(es).

    Only the VNIC's own addresses. The subnet /64, the VCN prefix and the ::/0
    route stay — they are shared, and other instances in the subnet may be using
    them.
    """
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.remove_public_ipv6(instance_id, info.compartment_id)
        write_audit(
            db,
            owner_id=user.id,
            action="instance.ipv6.remove",
            target=instance_id,
            detail={
                "tenant_id": tenant_id,
                "ok": result.ok,
                "message": result.message,
                "removed": (result.data or {}).get("removed") if isinstance(result.data, dict) else None,
            },
        )
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


@router.get("/tenants/{tenant_id}/invoices")
def account_invoices(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(24, ge=1, le=100),
) -> dict[str, Any]:
    """Billing invoices and whether each was paid (OSP Gateway).

    Separate from /usage on purpose: usage says what a month cost, an invoice
    says what Oracle billed and whether it was settled. Only this API knows the
    payment state. Read on demand, like every other Oracle call in the panel.
    """
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.list_invoices(limit=limit)
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": result.data if isinstance(result.data, dict) else {},
        }
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"读取账单失败: {exc}") from exc


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
    include_egress: bool = Query(True),
) -> dict[str, Any]:
    """Always-Free usage gauges (compute + storage + outbound traffic) for the dashboard.

    This is the only caller that asks for egress: it is an explicit user refresh,
    whereas the launch guard takes the same snapshot on every submit and must not
    pay for a Monitoring query that cannot change its verdict.
    """
    row = _tenant_or_404(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.get_free_quota_usage(
            free_only_mode=free_only_mode, include_egress=include_egress
        )
        data = dict(result.data if isinstance(result.data, dict) else {})
        # A 副区 has no Always Free allowance of its own; the numbers below are a
        # per-region count and must not be read as free headroom.
        region, home_region = region_pair(session)
        if (region and home_region and region != home_region) or tenant_is_secondary(row):
            data = {
                **data,
                "secondary_region": True,
                "region": region or (row.region or ""),
                "home_region": home_region or "主区",
            }
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": data,
        }
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"读取免费额度失败: {exc}") from exc


@router.post("/tenants/{tenant_id}/capacity-report")
def capacity_report(
    tenant_id: str,
    body: CapacityReportRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """容量雷达:探测目标 shape 在各可用域的实时容量。**只读,绝不创建实例。**

    刻意**不在 launch 路由里也拦一道**,和上面那个 launch-quota-check 的取舍正好相反。
    免费额度那道门可以由服务端硬拦,是因为它的判决权威且不可逆:规则是常量、快照是
    事实、放行的代价是真金白银。容量报告两条都不满足 ——

      * 它是一个瞬时快照,和随后那次 LaunchInstance 之间必然有竞态;
      * oracle/oci-cli issue #748 记录过 A1.Flex 上结论完全倒置的案例(报告说有货的
        AD 开不出来、说无货的反而开得出来),该 issue 至今未关闭;
      * CreateComputeCapacityReport 需要一条和 LaunchInstance **完全不相交**的 IAM
        授权(manage compute-capacity-reports),「能创建但调不了报告」是常见配置。

    做成后端硬门就会重演 launch_quota_check 上面那段注释记录的 0.4.84/0.4.85:
    预检比服务端严格 → 缺某项权限的租户从 UI 上**永久**无法创建实例。所以这里只出
    结论,拦不拦由用户在确认框里决定。

    整个响应恒为 HTTP 200,除非租户不存在(404)、租户被禁用(400)、或者撞到面板
    自己的限流(429)。Oracle 侧的失败表现为 status="unknown" + reason,**不是** 5xx ——
    「读不到」和「没有容量」是两件事,混成一件会让人放弃一台其实开得出来的机器。

    也刻意**不进 tenant_launch_lock**:那把锁保护的是「取额度快照 → LaunchInstance」
    这个 check-then-act 窗口,而探测什么都不改。进去只会让探测把创建堵住。
    """
    from web.backend.capacity_radar import RADAR_SHAPE, probe_capacity
    from web.backend.rate_limit import capacity_report_limiter

    row = _tenant_or_404(db, user.id, tenant_id)
    # 被禁用的租户不该继续花 OCI 预算。launch-meta 那三条路由都有这个检查。
    if not row.enabled:
        raise HTTPException(status_code=400, detail="租户已禁用")

    allowed, retry_after = capacity_report_limiter.check(f"caprad:{user.id}:{tenant_id}")
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"容量探测过于频繁,请 {int(retry_after) + 1} 秒后再试。"
                "容量报告和创建实例走的是同一个 Oracle 请求速率桶,"
                "探测太密会挤占抢机重试的预算。"
            ),
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    shape = (body.shape or RADAR_SHAPE).strip() or RADAR_SHAPE
    if shape != RADAR_SHAPE:
        # 只支持 A1.Flex。别的机型要么不是免费的(抢不到不是常态),要么是固定规格
        # (没有 shape config 可探),现在放开只会让人以为面板支持一件它没验证过的事。
        raise HTTPException(
            status_code=400,
            detail=f"容量雷达目前只支持 {RADAR_SHAPE}(收到 {shape})",
        )

    try:
        session = get_session_for_row(row)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # AD 列表:先看缓存,没有就单发一次 list_availability_domains。
    #
    # **绝对不能在这里调 fetch_launch_meta。** 它的冷调用是这个面板最贵的一次操作:
    # 给每个 OS family 列一遍镜像,而且 —— 这才是致命的 ——
    #     f_network = pool.submit(session.ensure_default_network,
    #                             compartment_id=..., create_if_missing=True)
    # 它会在租户里**创建 VCN、子网、网关和路由表**并等它们变可用,它自己的 docstring
    # 写着「A minute or more is normal」。
    #
    # 0.4.88 首版就是这么写的,后果有两层:
    #   1. 用户在雷达页(从没点过「加载配置」,缓存是冷的)点探测,请求要跑一分钟以上,
    #      浏览器/反代先超时 —— 表现就是「探测没结果」;
    #   2. 一个从页面副标题到 CHANGELOG 都写着「只读,绝不创建任何实例」的功能,
    #      会**创建网络资源**。这不是慢,这是把承诺破坏掉了。
    #
    # 现在:peek 命中就白拿(零 Oracle 请求);没命中就单发一次
    # list_availability_domains —— 一次纯读、没有任何写入,正好是我们要的那一样东西。
    # 顺带雷达不再依赖「必须先去创建页点过加载配置」,自己就能用。
    known_ads: list[str] = []
    meta_error = ""
    cached_meta = peek_launch_meta(session, row.id)
    if cached_meta:
        known_ads = [str(a) for a in (cached_meta.get("ads") or []) if a]
    if not known_ads:
        try:
            known_ads = [str(a) for a in (session.list_availability_domains() or []) if a]
        except Exception as exc:  # noqa: BLE001
            meta_error = str(exc)

    wanted = (body.availability_domain or "").strip()
    if wanted:
        if known_ads and wanted not in known_ads:
            raise HTTPException(
                status_code=400,
                detail="未知的可用域。请先在「创建实例」页点「加载配置」,再回来探测。",
            )
        ads = [wanted]
    else:
        ads = known_ads

    if not ads:
        return {
            "ok": False,
            "shape": shape,
            "region": row.region or "",
            "checked_at": "",
            "overall": "unknown",
            "results": [],
            "retry_job_active": False,
            "secondary_region": False,
            "message": (
                "还没有这个租户的可用域列表,无法探测。请先到「创建实例」页点一次"
                "「加载配置」。" + (f"(读取失败:{meta_error})" if meta_error else "")
            ),
        }

    configs: list[tuple[float, float]] = [(float(body.ocpus), float(body.memory_in_gbs))]
    for fb in body.fallback_configs or []:
        pair = (float(fb.ocpus), float(fb.memory_in_gbs))
        if pair not in configs:
            configs.append(pair)

    out = probe_capacity(
        session,
        tenant_id=row.id,
        shape=shape,
        configs=configs,
        availability_domains=ads,
    )
    out["region"] = row.region or ""
    # 有在跑的抢机任务时提醒用户:探测和那个循环共用同一个速率桶。
    out["retry_job_active"] = bool(
        db.scalar(
            select(func.count())
            .select_from(CapacityJob)
            .where(CapacityJob.tenant_id == row.id, CapacityJob.enabled.is_(True))
        )
        or 0
    )
    # 副区没有 Always Free,那边创建出来的一律计费 —— 一个绿色的「有货」很容易被
    # 读成「免费的有货」,所以把这个事实一起带给前端由它挂警告。
    try:
        from web.backend.quota_guard import resolve_secondary

        out["secondary_region"] = bool(resolve_secondary(session, row))
    except Exception:  # noqa: BLE001
        out["secondary_region"] = bool(getattr(row, "parent_tenant_id", "") or "")
    return out


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
        free_only = free_only_for_tenant(row)
        # Mirror the launch path's 副区 gate here so the panel's verdict matches
        # what submitting would actually do (see enforce_secondary_region).
        region, home_region = region_pair(session)
        secondary = bool(region and home_region and region != home_region) or tenant_is_secondary(row)
        if secondary:
            region = region or (row.region or "")
            home_region = home_region or "主区"
            blocked_reason = ""
            try:
                region_note = enforce_secondary_region(
                    session,
                    free_only_mode=free_only,
                    secondary_hint=True,
                    region_hint=row.region or "",
                )
            except HTTPException as exc:
                region_note = ""
                blocked_reason = str(exc.detail)
            return {
                "ok": not blocked_reason,
                "blocked": bool(blocked_reason),
                "errors": [blocked_reason] if blocked_reason else [],
                "warnings": [region_note] if region_note else [],
                "issues": [],
                "projected": {},
                "free_only_mode": free_only,
                "account_tier": tier,
                "read_incomplete": False,
                "secondary_region": True,
                "region": region,
                "home_region": home_region,
                "limits": {},
                "usage": {},
                "remaining": {},
                "buckets": {},
                "overall_status": "",
                # 副区 has no Always Free allowance at all, so there is no usage
                # gauge to show — say so instead of rendering four empty bars.
                "summary_lines": [f"副区 {region} 不适用 Always Free 额度（主区 {home_region}）"],
            }
        usage = usage_snapshot(session, free_only_mode=free_only)
        guard = check_launch_quota(
            session,
            account_tier=tier,
            shape=str(body.shape or ""),
            ocpus=body.ocpus,
            memory_in_gbs=body.memory_in_gbs,
            boot_volume_size_in_gbs=body.boot_volume_size_in_gbs,
            boot_volume_vpus_per_gb=body.boot_volume_vpus_per_gb or 10,
            free_only_mode=free_only,
            usage=usage,
            count=max(1, int(body.count or 1)),
        )
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"校验免费额度失败: {exc}") from exc

    out = guard.to_dict()
    out["free_only_mode"] = free_only
    out["account_tier"] = str(usage.get("account_tier") or tier or "")
    out["secondary_region"] = False
    out["region"] = region
    out["home_region"] = home_region
    # read_incomplete means the numbers below are an undercount; the launch path
    # refuses outright in that case, so tell the UI rather than showing a total
    # that looks like plenty of headroom.
    out["read_incomplete"] = bool(usage.get("read_incomplete"))
    # 预检的结论必须和真正创建时一致。
    #
    # enforce_launch_quota（创建路径）在读取不完整时会直接 503 拒绝，而
    # check_launch_quota 不做这一步 —— 它只算 GuardResult。于是快照退化成
    # {"read_incomplete": True} 时，校验器看到的用量是零，guard.ok 为真，
    # 预检回 blocked=False，「下一步」正常打开确认框，用户点「确认并创建」
    # 才被 503 挡下。本路由的 docstring 写着「面板的预检结论不会和服务端漂移」，
    # 而这正是一处漂移。read_incomplete 虽然也传了出去，但它只渲染在额度预览
    # 面板里，那个看起来更权威的「校验结论」框对此只字不提。
    # 只在**创建路径真的会拒绝**时才拦。
    #
    # 0.4.85 修「预检比服务端宽松」时把这里写成了无条件拦截，于是在另一个方向
    # 造出了同样的漂移：enforce_launch_quota 只在 hard_free_caps(free_only, tier)
    # 为真时才 503，而 (free_only=False, tier="paid") 这一组合是不拦的。结果预检
    # 说「创建会被服务端拒绝」，LaunchView 据此连确认框都不打开 —— 付费租户从 UI
    # 上彻底无法创建，而实际提交是会成功的。
    #
    # 而且这不是偶发：0.4.84 查明缺少 inspect compartments 权限会让 read_incomplete
    # **永久为真**，正是那一类租户会被永久锁死，还附一条假的权限解释。
    # 局部 import：本模块里 `free_quota` 已经是下面那个路由函数的名字，
    # 模块名被它遮住了。
    from app.free_quota import hard_free_caps

    if out["read_incomplete"] and hard_free_caps(free_only, out["account_tier"]):
        reason = (
            "无法完整读取 Always Free 用量（部分 compartment 读取失败），"
            "因此无法判断额度是否够用。创建会被服务端拒绝 —— "
            "请先到租户页点「测试连接」确认权限。"
        )
        out["ok"] = False
        out["blocked"] = True
        out["errors"] = [reason] + list(out.get("errors") or [])
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


@router.post("/tenants/{tenant_id}/launch-meta/refresh")
def launch_meta_refresh(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    force: bool = Query(True),
) -> dict[str, Any]:
    """Start building the launch metadata and return at once.

    The synchronous GET above cannot be made reliably fast: it makes six
    paginated Oracle reads and, on a tenancy with no network, creates a VCN and
    waits for it. How long that takes belongs to the operator's Oracle account,
    not to this code — and Cloudflare cuts any single request at 100 seconds, so
    「加载配置」 returned a gateway error and then worked on the next click,
    because the first attempt finished server-side and filled the cache.

    Returning immediately and letting the browser poll removes the dependency on
    any proxy's ceiling instead of trying to stay under it. A second click while
    one is in flight joins that run rather than starting more Oracle calls.
    """
    row = _tenant_or_404(db, user.id, tenant_id)
    if not row.enabled:
        raise HTTPException(status_code=400, detail="租户已禁用")
    try:
        session = get_session_for_row(row)
        return start_meta_refresh(session, row.id, force=force)
    except OCIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"启动加载失败: {exc}") from exc


@router.get("/tenants/{tenant_id}/launch-meta/status")
def launch_meta_status(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Poll target: ready (with the metadata) / running / error / idle.

    Touches Oracle only through the cache — polling must never itself make API
    calls, or a page left open would quietly spend the tenancy's rate limit.
    """
    row = _tenant_or_404(db, user.id, tenant_id)
    if not row.enabled:
        raise HTTPException(status_code=400, detail="租户已禁用")
    try:
        session = get_session_for_row(row)
        return launch_meta_state(session, row.id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"读取加载状态失败: {exc}") from exc


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
    timer = _PhaseTimer()
    try:
        session = get_session_for_row(row)
        timer.mark("session")
        meta = fetch_launch_meta(session, tenant_id=row.id, force=False)
        timer.mark("meta")
        built = build_launch_request(body.model_dump(), meta=meta)
        timer.mark("build")
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

    count = max(1, int(body.count or 1))
    if count > 1 and built["as_retry"]:
        # Capacity retry is one machine per job by design (one active job per
        # tenant, and the worker owns every LaunchInstance call). Silently
        # creating one would look like the count field was ignored.
        raise HTTPException(
            status_code=400,
            detail="容量重试每次只能抢 1 台。请把数量改回 1，或关闭「加入容量重试」后再批量创建。",
        )

    # Always Free guard BEFORE network/NSG prep so we don't leave orphan resources.
    timer.mark("pre")
    # 从「读用量快照」一直到「最后一次 LaunchInstance」都必须在同一把
    # 每租户的锁里。配额校验只是 check-then-act，它不预留任何东西：两个标签
    # 同时提交 4 OCPU/24GB 的 A1，各自读到「已用 0」，就会双双放行、真的开出
    # 8 OCPU/48GB —— 免费额度的两倍。窗口还特别宽，因为 prepare_launch_network
    # 夹在判定和创建之间，它可能要新建 NSG 甚至 VCN，是几十秒而不是几微秒，
    # 所以它必须留在锁**里面**。
    #
    # 抢机任务和手工创建之间也靠这把锁串行化 —— worker.py 从 0.4.87 起在自己的
    # 「取快照 → LaunchInstance」窗口上取同一把锁（Worker._acquire_launch_lock），
    # 拿不到就推迟本次尝试。在那之前全仓库只有下面这一处 with，两条路径各自读到
    # 同一份「已用 0」的快照就会各开一台，把 Always Free 额度花两遍。
    #
    # 锁内实际发出的 OCI 调用（tests/test_launch_lock_scope.py 逐个钉住，
    # 往里加一次就会红）：home_region（按 session 缓存）→ list_instances_tree
    # → list_boot_volumes → list_block_volumes → list_buckets + 每个桶若干次
    # list_objects → prepare_launch_network → launch_from_payload × count。
    # 其中桶枚举是纯开销：validate_launch_against_quota 只读 A1 / E2 / 块存储四项，
    # object_storage_gb 只喂仪表盘那几根进度条。代价是「1 + 各桶页数之和」次调用
    # （estimate_object_storage_usage: max_buckets=50，每桶最多 5000 个对象、每页
    # limit=min(1000, 剩余)，所以对象多的桶是 5 页就撞上对象数上限退出，那个
    # `pages > 20` 的闸门只有在每页返回不足 1000 条时才轮得到生效），真正兜底的是
    # deadline_sec=25.0 —— 桶多的租户能让这把每租户互斥锁多握近半分钟，同租户的
    # 下一次创建就干等这么久，换不来判决上的任何差别。
    # 去掉它要给 oci_client.get_free_quota_usage 加一个 include_object 开关
    # （它已经有 include_block / include_egress），那是另一个文件的事；**不要**
    # 改成把快照挪到锁外来省这段，那等于把上面说的双花窗口原样放回去。
    with tenant_launch_lock(row.id):
        free_only = free_only_for_tenant(row)
        launch_guard = None
        extra_warnings: list[str] = []
        try:
            # 副区: Always Free only exists in the home region, and the free-usage
            # snapshot is per-region — from a secondary region it reads zero and would
            # wave through a second "free" machine. So the region gate replaces the
            # cap check there instead of running alongside it.
            region_warning = enforce_secondary_region(
                session,
                free_only_mode=free_only,
                secondary_hint=tenant_is_secondary(row),
                region_hint=row.region or "",
            )
            if region_warning:
                extra_warnings.append(region_warning)
            else:
                launch_guard = enforce_launch_quota(
                    session,
                    account_tier=getattr(row, "account_tier", "") or "",
                    shape=str(payload.get("shape") or ""),
                    ocpus=payload.get("ocpus"),
                    memory_in_gbs=payload.get("memory_in_gbs"),
                    boot_volume_size_in_gbs=payload.get("boot_volume_size_in_gbs"),
                    boot_volume_vpus_per_gb=boot_vpu,
                    fallback_configs=list(built.get("fallback_configs") or body.fallback_configs or []),
                    free_only_mode=free_only,
                    count=count,
                )
        except HTTPException:
            timer.emit("quota-refused")
            raise
        except Exception as exc:  # noqa: BLE001
            # A quota-read failure must not surface as an unhandled 500 (this branch
            # previously re-raised HTTPException only, so anything else escaped).
            raise HTTPException(status_code=502, detail=f"校验免费额度失败: {exc}") from exc

        # Pre-launch: IPv6 + managed NSG (desktop parity)
        try:
            timer.mark("quota")
            payload = prepare_launch_network(
                session, payload, meta=meta, for_retry=bool(built["as_retry"])
            )
            built["payload"] = payload
            timer.mark("network")
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
            timer.mark("enqueue")
            timer.emit("queued-retry")
            _audit_launch(False, "已加入容量重试队列", "", job.id)
            retry_msg = (
                f"已加入容量重试：后台将每 {job.interval_sec}s 尝试一次"
                f"（最多 {job.max_attempts} 次）。请在「任务中心」查看进度与日志"
                "（需保持 worker 进程运行）。"
            )
            warns = extra_warnings + format_guard_warnings(launch_guard)
            if warns:
                retry_msg += " 提醒：" + "；".join(warns)
            return LaunchInstanceResult(
                ok=True,
                message=retry_msg,
                capacity_job_id=job.id,
            )

        # Direct launch. One LaunchInstance call per instance — OCI has no batch API.
        base_name = str(payload.get("display_name") or "instance")
        # A password the operator typed is reused for the batch; an auto-generated one
        # is fresh per instance, so a single leak does not hand over every machine.
        # Each lands in that instance's own tag (see ROOT_PASSWORD_TAG), and the
        # instance list already shows it per row.
        user_supplied_password = bool(str(body.root_password or "").strip())
        auth_mode = str(payload.get("auth_mode") or "key")

        created: list[dict[str, Any]] = []
        first_result = None
        failure_message = ""
        failure_capacity = False

        # Supplied by the browser and held constant across retries of the same
        # submission, so a launch whose response was lost (proxy timeout, dropped
        # connection) is not created twice when the operator presses the button
        # again. Empty when the client is older than this feature; that simply means
        # no protection, not an error.
        idempotency_key = str(getattr(body, "idempotency_key", "") or "").strip()

        for index in range(count):
            item_payload = dict(payload)
            if count > 1:
                item_payload["display_name"] = f"{base_name}-{index + 1}"
            if index == 0 or user_supplied_password or auth_mode != "password":
                item_password = root_password
            else:
                item_password = generate_root_password(16)

            # Per-item token, never the bare key. A retry token tells Oracle "this is
            # the same request as before", so sending one identical key for all N
            # items of a batch would make it create the FIRST instance and then hand
            # back that same instance N-1 more times — the page would report five
            # machines created and one would exist.
            #
            # Derived by a helper rather than an f-string: the token is capped at 64
            # characters, so a client-supplied key at that limit lost the suffix to
            # truncation and produced exactly the collision described above.
            item_key = derive_retry_token(idempotency_key, index) if idempotency_key else ""

            try:
                result = session.launch_from_payload(
                    item_payload,
                    root_password=item_password,
                    custom_user_data=custom_user_data,
                    idempotency_key=item_key,
                )
            except Exception as exc:  # noqa: BLE001
                if not created and item_payload.get("managed_nsg_id"):
                    try:
                        session.delete_managed_nsg(str(item_payload.get("managed_nsg_id")))
                    except Exception:
                        pass
                if not created:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                failure_message = str(exc)
                break

            data = result.data if isinstance(result.data, dict) else {}
            instance_id = str(data.get("instance_id") or "")
            if not result.ok:
                failure_message = result.message or "创建失败"
                failure_capacity = bool(data.get("capacity")) or is_capacity_message(failure_message)
                if first_result is None:
                    first_result = result
                break

            if first_result is None:
                first_result = result
            schedule_post_launch_adjustments(
                session,
                instance_id=instance_id,
                compartment_id=str(item_payload.get("compartment_id") or ""),
                boot_vpu=boot_vpu,
            )
            created.append(
                {
                    "ok": True,
                    "display_name": str(item_payload.get("display_name") or ""),
                    "instance_id": instance_id,
                    "work_request_id": result.work_request_id or "",
                    "message": result.message or "创建成功",
                    "root_password": item_password or "",
                }
            )
            _audit_launch(True, result.message or "创建成功", instance_id)
            # Stop the batch at the first failure rather than pushing the remaining
            # calls at a rate limit the capacity-retry loop competes for — a capacity
            # miss means the AD is out, so #3 and #4 would fail the same way.

    warns = extra_warnings + format_guard_warnings(launch_guard)

    if not created:
        # Total failure: clean the managed NSG for permanent (non-capacity) errors.
        if payload.get("managed_nsg_id") and (
            not failure_capacity or payload.get("auth_mode") == "password"
        ):
            try:
                session.delete_managed_nsg(str(payload.get("managed_nsg_id")))
            except Exception:
                pass
        timer.mark("launch")
        timer.emit("failed")
        _audit_launch(False, failure_message or "创建失败", "")
        data = (
            first_result.data
            if first_result is not None and isinstance(first_result.data, dict)
            else {}
        )
        return LaunchInstanceResult(
            ok=False,
            message=failure_message or "创建失败",
            work_request_id=(first_result.work_request_id or "") if first_result else "",
            instance_id=str(data.get("instance_id") or ""),
            data=data,
            instances=[],
            created_count=0,
            requested_count=count,
        )

    timer.mark("launch")
    timer.emit("created" if len(created) == count else "partial")

    head = created[0]
    if count == 1:
        msg = head["message"]
    elif len(created) == count:
        msg = f"已创建 {len(created)} 台：" + "、".join(c["display_name"] for c in created)
    else:
        msg = (
            f"已创建 {len(created)}/{count} 台，第 {len(created) + 1} 台起停止："
            f"{failure_message or '创建失败'}"
        )
    if boot_vpu != 10:
        msg += f"；引导卷性能 {boot_vpu} VPUs/GB 将在后台自动调整（hydration 完成后）"
    if warns:
        msg += "；提醒：" + "；".join(warns)

    return LaunchInstanceResult(
        # Partial success is not "ok": the form must keep the failure visible
        # instead of reporting a clean create for a batch that fell short.
        ok=len(created) == count,
        message=msg,
        work_request_id=head["work_request_id"],
        instance_id=head["instance_id"],
        root_password=head["root_password"],
        data={"instance_id": head["instance_id"], "created_count": len(created)},
        instances=created,
        created_count=len(created),
        requested_count=count,
    )
