"""Extended instance operations: console, firewall, boot volume."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.oci_client import FirewallRuleSpec
from web.backend import quota_guard
from web.backend.audit import iso_utc, write_audit
from web.backend.auth import get_current_user
from web.backend.db import get_db
from web.backend.models import SshHostKey, User
from web.backend.oci_bridge import get_owned_tenant, get_session_for_row, op_result_dict
from web.backend.schemas import PowerActionResult
from web.backend.ssh_hostkey import UNREACHABLE as HOSTKEY_UNREACHABLE
from web.backend.ssh_hostkey import check_instance_host_key, forget_host_key, known_hosts_for

router = APIRouter(tags=["instance-ops"])


class _HostKeyRefused(Exception):
    """Internal signal: host key check failed, details already recorded."""


class ConsoleCreateRequest(BaseModel):
    ssh_public_key: str = Field(min_length=20)


class BootVolumeUpdateRequest(BaseModel):
    size_in_gbs: Optional[int] = None
    vpus_per_gb: Optional[int] = None
    # Optional SSH auto-grow of guest filesystem after OCI size expand (session-only creds).
    auto_grow_fs: bool = False
    ssh_username: str = "ubuntu"
    ssh_private_key_pem: Optional[str] = None
    ssh_password: Optional[str] = None
    ssh_port: int = 22


class FirewallRuleCreate(BaseModel):
    nsg_id: str
    direction: str = "INGRESS"  # INGRESS | EGRESS
    protocol: str = "all"  # all | 6 | 17 | 1
    cidr: str = "0.0.0.0/0"
    port_min: Optional[int] = None
    port_max: Optional[int] = None


class FirewallDeleteRules(BaseModel):
    nsg_id: str
    rule_ids: list[str]


def _row(db: Session, user_id: str, tenant_id: str):
    try:
        return get_owned_tenant(db, user_id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _unpack_console_list(result: Any) -> tuple[list[Any], str]:
    """Split whatever list_console_connections returned into (connections, error).

    That client used to swallow ServiceError and return [], so a throttled or
    unauthorized read was indistinguishable from "this instance has no console
    connection" — and the UI then offered to create one, which deletes every
    existing connection first, so an invisible read failure could tear down the
    session the operator was in the middle of using.

    app/oci_client.py now raises on a failed read, which the caller turns into a
    502. Failures are also reported as OperationResult in much of that module, so
    accept that shape too rather than letting an ok=False object be truthy and pass
    for a list of one. A plain list stays "success" so this route never 502s on a
    client that has not been changed.
    """
    if result is None:
        return [], ""
    # A real bool, because a MagicMock answers hasattr() for anything.
    if isinstance(getattr(result, "ok", None), bool):
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            data = data.get("connections", data.get("items"))
        if bool(result.ok):
            return list(data or []), ""
        return [], str(getattr(result, "message", "") or "") or "读取失败"
    try:
        return list(result), ""
    except TypeError:
        return [], f"无法解析控制台连接返回值（{type(result).__name__}）"


@router.get("/tenants/{tenant_id}/instances/{instance_id}/console")
def list_console(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        raw = session.list_console_connections(instance_id, info.compartment_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"读取控制台连接失败：{exc}") from exc
    conns, error = _unpack_console_list(raw)
    if error:
        # An empty list with ok:true would be read as "none exist"; say it failed.
        raise HTTPException(status_code=502, detail=f"读取控制台连接失败：{error}")
    items = []
    for c in conns:
        items.append(
            {
                "id": getattr(c, "id", "") or "",
                "lifecycle_state": getattr(c, "lifecycle_state", "") or "",
                "serial": getattr(c, "connection_string", "") or "",
                "vnc": getattr(c, "vnc_connection_string", "") or "",
            }
        )
    return {"ok": True, "connections": items}


@router.post("/tenants/{tenant_id}/instances/{instance_id}/console")
def create_console(
    tenant_id: str,
    instance_id: str,
    body: ConsoleCreateRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.create_console_connection(
            instance_id, info.compartment_id, body.ssh_public_key.strip()
        )
        data = result.data if isinstance(result.data, dict) else {}
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": data,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/tenants/{tenant_id}/instances/{instance_id}/console/{connection_id}")
def delete_console(
    tenant_id: str,
    instance_id: str,
    connection_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, str]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.delete_console_connection(connection_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.message or "删除控制台连接失败")
    return {"message": result.message or "已删除控制台连接"}


@router.get("/tenants/{tenant_id}/instances/{instance_id}/firewall")
def get_firewall(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.get_instance_firewall(instance_id, info.compartment_id)
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": result.data if isinstance(result.data, dict) else {},
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/instances/{instance_id}/firewall/rules")
def add_firewall_rule(
    tenant_id: str,
    instance_id: str,
    body: FirewallRuleCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    _ = instance_id
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        direction = body.direction.strip().upper()
        protocol = body.protocol.strip().lower()
        if protocol in {"all", "all protocols", "*"}:
            protocol = "all"
        elif protocol in {"tcp", "6"}:
            protocol = "6"
        elif protocol in {"udp", "17"}:
            protocol = "17"
        elif protocol in {"icmp", "1"}:
            protocol = "1"
        port_min = body.port_min
        port_max = body.port_max if body.port_max is not None else body.port_min
        spec = FirewallRuleSpec(
            direction=direction,
            protocol=protocol,
            cidr=body.cidr.strip(),
            port_min=port_min,
            port_max=port_max,
        )
        try:
            spec.validate()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = session.add_instance_firewall_rule(body.nsg_id, spec)
        return PowerActionResult(**op_result_dict(result))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/instances/{instance_id}/firewall/delete-rules")
def delete_firewall_rules(
    tenant_id: str,
    instance_id: str,
    body: FirewallDeleteRules,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    _ = instance_id
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.delete_nsg_rules(body.nsg_id, body.rule_ids)
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/instances/{instance_id}/firewall/open-all")
def firewall_open_all(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.replace_instance_firewall_with_open_all(instance_id, info.compartment_id)
        write_audit(
            db,
            owner_id=user.id,
            action="firewall.open_all",
            target=instance_id,
            detail={"tenant_id": tenant_id, "ok": result.ok, "message": result.message},
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/tenants/{tenant_id}/boot-volumes")
def list_boot_volumes(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    include_subcompartments: bool = True,
) -> dict[str, Any]:
    """List all boot volumes under the tenant (optionally including subcompartments)."""
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.list_boot_volumes(
            compartment_id=row.compartment_ocid or row.tenancy_ocid,
            include_subcompartments=include_subcompartments,
            include_attachments=True,
        )
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": result.data if isinstance(result.data, dict) else {},
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/tenants/{tenant_id}/instances/{instance_id}/boot-volume")
def boot_volume_info(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.get_boot_volume_info(instance_id, info.compartment_id)
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": result.data if isinstance(result.data, dict) else {},
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/instances/{instance_id}/boot-volume")
def boot_volume_update(
    tenant_id: str,
    instance_id: str,
    body: BootVolumeUpdateRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        from app import free_quota
        from app.fs_grow import truncate_output
        from web.backend.ssh_bridge import (
            grow_filesystem_over_ssh,
            resolve_instance_ssh_target,
            validate_ssh_auth,
        )

        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)

        current_size = 0
        size_changing = body.size_in_gbs is not None
        if size_changing:
            try:
                cur = session.get_boot_volume_info(instance_id, info.compartment_id)
                if cur.ok and isinstance(cur.data, dict):
                    current_size = int(cur.data.get("size_in_gbs") or 0)
            except Exception:
                current_size = 0
            # Same two corrections as the block-volume guard: derive free_only from
            # the tier (True hard-capped paid tenants) and refuse on a partial read
            # instead of reading it as zero usage.
            tier = getattr(row, "account_tier", "") or ""
            # Explicit per-tenant flag, not inferred from account_tier.
            free_only = quota_guard.free_only_for_tenant(row)
            # A 副区 has no free storage allowance to measure against, and the
            # snapshot only counts that region — see secondary_region_gate. It
            # refuses outright if the tenant still has free-only on.
            in_secondary = bool(
                quota_guard.secondary_region_gate(session, row, free_only_mode=free_only)
            )
            if not in_secondary:
                usage = quota_guard.usage_snapshot(session, free_only_mode=free_only)
                blocked = quota_guard._blocked_by_incomplete_read(usage, free_only)
                if blocked:
                    raise HTTPException(status_code=503, detail=blocked)
                guard = free_quota.validate_boot_resize_against_quota(
                    current_size_gb=current_size,
                    new_size_gb=body.size_in_gbs,
                    free_only_mode=free_only,
                    account_tier=tier,
                    usage=usage,
                )
                if not guard.ok:
                    raise HTTPException(
                        status_code=400,
                        detail="；".join(guard.error_messages()) or "超出免费块存储额度",
                    )

        ssh_auth = None
        if body.auto_grow_fs:
            if not size_changing:
                raise HTTPException(status_code=400, detail="自动扩展文件系统仅在扩大引导卷时可用")
            try:
                ssh_auth = validate_ssh_auth(
                    username=body.ssh_username or "ubuntu",
                    private_key_pem=body.ssh_private_key_pem,
                    password=body.ssh_password,
                    port=int(body.ssh_port or 22),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        result = session.resize_boot_volume(
            instance_id,
            info.compartment_id,
            size_in_gbs=body.size_in_gbs,
            vpus_per_gb=body.vpus_per_gb,
            # Bound the wait for an HTTP caller: the library defaults can block for
            # ~31 minutes waiting on volume hydration, holding a threadpool slot and
            # a pooled DB connection the whole time. On timeout resize_boot_volume
            # already returns the "仍在从镜像同步数据（hydrating）…请几分钟后重试"
            # result, so no new UX is needed. The worker keeps the long defaults.
            timeout=60,
            hydration_timeout=120,
        )
        oci_ok = bool(result.ok)
        data: dict[str, Any] = dict(result.data) if isinstance(result.data, dict) else {}
        data["oci_ok"] = oci_ok
        data["fs_ok"] = None
        data["stdout"] = ""
        data["stderr"] = ""
        data["hints"] = []
        message = result.message or ""

        if oci_ok and body.auto_grow_fs and ssh_auth and size_changing:
            try:
                target = resolve_instance_ssh_target(session, instance_id)
                # Verify the host key before the SSH credentials are used, same as
                # WebSSH. A mismatch aborts instead of handing the key/password to
                # whatever answered on that address.
                hostkey = check_instance_host_key(
                    db,
                    owner_id=user.id,
                    tenant_id=tenant_id,
                    instance_id=instance_id,
                    host=target.host,
                    port=int(ssh_auth["port"]),
                )
                if not hostkey.ok:
                    # Reported as a failed FS-grow rather than raising: the OCI
                    # resize above already succeeded, and a 4xx here would hide
                    # that from the caller.
                    data["fs_ok"] = False
                    data["hints"] = [hostkey.message()]
                    reason = (
                        "无法连接 SSH 读取主机密钥"
                        if hostkey.verdict == HOSTKEY_UNREACHABLE
                        else "SSH 主机密钥不匹配"
                    )
                    message = (message or "引导卷已调整") + f"；文件系统扩展已中止：{reason}"
                    raise _HostKeyRefused()
                grow = grow_filesystem_over_ssh(
                    target.host,
                    port=ssh_auth["port"],
                    username=ssh_auth["username"],
                    private_key_pem=ssh_auth.get("private_key_pem"),
                    password=ssh_auth.get("password"),
                    retries=3,
                    retry_delay_sec=8.0,
                    timeout=120.0,
                    known_hosts=known_hosts_for(hostkey.server_key),
                )
                data["fs_ok"] = bool(grow.ok)
                data["stdout"] = truncate_output(grow.stdout)
                data["stderr"] = truncate_output(grow.stderr)
                data["ssh_host"] = grow.host or target.host
                if grow.ok:
                    message = (message or "引导卷已调整") + "；文件系统已扩展"
                else:
                    hints = []
                    if grow.message:
                        hints.append(grow.message)
                    hints.append("可稍后在实例内手动执行: sudo growpart <disk> <part> && sudo resize2fs <dev>")
                    data["hints"] = hints
                    message = (message or "引导卷已调整") + f"；文件系统扩展失败：{grow.message}"
            except _HostKeyRefused:
                pass  # data/message already describe the refusal
            except Exception as exc:  # noqa: BLE001
                data["fs_ok"] = False
                data["hints"] = [
                    str(exc),
                    "请确认实例有公网 IP、22 端口放行，或登录后手动 growpart/resize2fs",
                ]
                message = (message or "引导卷已调整") + f"；文件系统扩展失败：{exc}"

        write_audit(
            db,
            owner_id=user.id,
            action="boot_volume.resize",
            target=instance_id,
            detail={
                "oci_ok": oci_ok,
                "fs_ok": data.get("fs_ok"),
                "auto_grow": bool(body.auto_grow_fs),
                "size_in_gbs": body.size_in_gbs,
                "vpus_per_gb": body.vpus_per_gb,
                "auth_mode": (ssh_auth or {}).get("auth_mode") if ssh_auth else None,
            },
        )
        # Drop any residual credential refs
        ssh_auth = None
        return {
            "ok": oci_ok,
            "message": message,
            "work_request_id": getattr(result, "work_request_id", "") or "",
            "data": data,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# SSH host key (trust on first use)
# ---------------------------------------------------------------------------


@router.get("/tenants/{tenant_id}/instances/{instance_id}/host-key")
def get_host_key(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """The remembered SSH host key fingerprint(s) for this instance."""
    _row(db, user.id, tenant_id)  # ownership
    rows = db.scalars(
        select(SshHostKey).where(
            SshHostKey.owner_id == user.id, SshHostKey.instance_id == instance_id
        )
    ).all()
    return {
        "ok": True,
        "items": [
            {
                "port": r.port,
                "fingerprint": r.fingerprint,
                "key_type": r.key_type,
                "last_host": r.last_host,
                # Offset-less on SQLite otherwise; see iso_utc. "First seen" on a
                # host key is evidence — it must not be shifted by the viewer's zone.
                "created_at": iso_utc(r.created_at),
            }
            for r in rows
        ],
    }


@router.delete("/tenants/{tenant_id}/instances/{instance_id}/host-key")
def reset_host_key(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Forget the remembered host key so the next connection re-learns it.

    Needed after a legitimate rebuild/reinstall, which changes the host key. Only
    ever affects the caller's own record.
    """
    _row(db, user.id, tenant_id)  # ownership
    removed = forget_host_key(db, owner_id=user.id, instance_id=instance_id)
    write_audit(
        db,
        owner_id=user.id,
        action="webssh.hostkey_reset",
        target=instance_id,
        detail={"tenant_id": tenant_id, "removed": removed},
    )
    return {
        "ok": True,
        "removed": removed,
        "message": "已重置主机密钥记录，下次连接会重新记录指纹" if removed else "没有需要重置的记录",
    }


# ---------------------------------------------------------------------------
# Reserved public IPs
# ---------------------------------------------------------------------------


class ReservedIpCreate(BaseModel):
    display_name: str = ""


class ReservedIpAttach(BaseModel):
    public_ip_id: str


@router.get("/tenants/{tenant_id}/reserved-ips")
def list_reserved_ips(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        items = session.list_reserved_public_ips()
        return {"ok": True, "items": items}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/reserved-ips")
def create_reserved_ip(
    tenant_id: str,
    body: ReservedIpCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.create_reserved_public_ip(display_name=body.display_name)
        write_audit(
            db,
            owner_id=user.id,
            action="reserved_ip.create",
            target=str((result.data or {}).get("ip_address") or ""),
            detail={"tenant_id": tenant_id, "ok": result.ok, "message": result.message},
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/tenants/{tenant_id}/reserved-ips/{public_ip_id}")
def delete_reserved_ip(
    tenant_id: str,
    public_ip_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.delete_reserved_public_ip(public_ip_id)
        write_audit(
            db,
            owner_id=user.id,
            action="reserved_ip.delete",
            target=public_ip_id,
            detail={"tenant_id": tenant_id, "ok": result.ok, "message": result.message},
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/instances/{instance_id}/reserved-ip/attach")
def attach_reserved_ip(
    tenant_id: str,
    instance_id: str,
    body: ReservedIpAttach,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.attach_reserved_public_ip(instance_id, info.compartment_id, body.public_ip_id)
        write_audit(
            db,
            owner_id=user.id,
            action="reserved_ip.attach",
            target=instance_id,
            detail={"tenant_id": tenant_id, "ok": result.ok, "message": result.message},
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/reserved-ips/{public_ip_id}/detach")
def detach_reserved_ip(
    tenant_id: str,
    public_ip_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.detach_reserved_public_ip(public_ip_id)
        write_audit(
            db,
            owner_id=user.id,
            action="reserved_ip.detach",
            target=public_ip_id,
            detail={"tenant_id": tenant_id, "ok": result.ok, "message": result.message},
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Boot volume backups + custom images
# ---------------------------------------------------------------------------


class BootBackupCreate(BaseModel):
    boot_volume_id: str
    display_name: str = ""
    backup_type: str = "INCREMENTAL"  # INCREMENTAL | FULL


class CustomImageCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)


@router.get("/tenants/{tenant_id}/boot-volume-backups")
def list_boot_volume_backups(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    boot_volume_id: str = "",
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        items = session.list_boot_volume_backups(boot_volume_id=boot_volume_id or None)
        return {"ok": True, "items": items}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/boot-volume-backups")
def create_boot_volume_backup(
    tenant_id: str,
    body: BootBackupCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _row(db, user.id, tenant_id)
    backup_type = (body.backup_type or "INCREMENTAL").upper()
    if backup_type not in {"INCREMENTAL", "FULL"}:
        raise HTTPException(status_code=400, detail="备份类型必须为 INCREMENTAL 或 FULL")
    try:
        session = get_session_for_row(row)
        result = session.create_boot_volume_backup(
            body.boot_volume_id, display_name=body.display_name, backup_type=backup_type
        )
        write_audit(
            db,
            owner_id=user.id,
            action="boot_backup.create",
            target=body.boot_volume_id,
            detail={"tenant_id": tenant_id, "ok": result.ok, "message": result.message},
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/tenants/{tenant_id}/boot-volume-backups/{backup_id}")
def delete_boot_volume_backup(
    tenant_id: str,
    backup_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.delete_boot_volume_backup(backup_id)
        write_audit(
            db,
            owner_id=user.id,
            action="boot_backup.delete",
            target=backup_id,
            detail={"tenant_id": tenant_id, "ok": result.ok, "message": result.message},
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class BootVolumeRename(BaseModel):
    # 与 tenants.name 一致的下限，上限取 OCI 自己的 255（oci_client 再截一次，
    # 这里只是把明显错误挡在业务层之外）。
    display_name: str = Field(min_length=1, max_length=255)


@router.delete("/tenants/{tenant_id}/boot-volumes/{volume_id}")
def delete_boot_volume(
    tenant_id: str,
    volume_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    """删除一个**未挂载**的引导卷。不可逆，卷上的数据一并消失。

    存在的理由是孤儿卷：在 Oracle 控制台终止实例时「保留引导卷」是默认勾选的，
    留下的卷会一直占着租户 200GB 的 Always Free 块存储额度。面板早就在统计它们
    （免费额度面板里的孤儿卷数量），但一直没有清理的入口。

    是否仍被挂载由 oci_client 侧判断并**读不到就拒绝**——删错的代价是抹掉一台
    在跑的机器的系统盘。
    """
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.delete_boot_volume(volume_id)
        write_audit(
            db,
            owner_id=user.id,
            action="boot_volume.delete",
            target=volume_id,
            detail={
                "tenant_id": tenant_id,
                "ok": result.ok,
                "message": result.message,
                # 释放了多少容量要进审计：这是配额账本上的一笔支出，
                # 事后回溯「200GB 是怎么用光/腾出来的」全靠它。
                "size_in_gbs": (result.data or {}).get("size_in_gbs"),
            },
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/boot-volumes/{volume_id}/rename")
def rename_boot_volume(
    tenant_id: str,
    volume_id: str,
    body: BootVolumeRename,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    """给引导卷改名。孤儿卷全叫「<已终止实例名> (Boot Volume)」，重建几次之后
    列表里就是几行几乎一样的名字，没法判断哪个能删。"""
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.rename_boot_volume(volume_id, body.display_name)
        write_audit(
            db,
            owner_id=user.id,
            action="boot_volume.rename",
            target=volume_id,
            detail={"tenant_id": tenant_id, "ok": result.ok, "name": body.display_name[:255]},
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/instances/{instance_id}/create-image")
def create_custom_image(
    tenant_id: str,
    instance_id: str,
    body: CustomImageCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    # Feature disabled by product decision — keep route for old clients with a clear error.
    _ = (tenant_id, instance_id, body, user, db)
    raise HTTPException(
        status_code=403,
        detail="实例「制作镜像」功能已关闭。如需系统备份，请使用引导卷备份。",
    )


@router.get("/tenants/{tenant_id}/custom-images")
def list_custom_images(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        items = session.list_custom_images()
        return {"ok": True, "items": items}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/tenants/{tenant_id}/custom-images/{image_id}")
def delete_custom_image(
    tenant_id: str,
    image_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.delete_custom_image(image_id)
        write_audit(
            db,
            owner_id=user.id,
            action="image.delete",
            target=image_id,
            detail={"tenant_id": tenant_id, "ok": result.ok, "message": result.message},
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
