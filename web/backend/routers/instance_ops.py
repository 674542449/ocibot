"""Extended instance operations: console, firewall, boot volume."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.oci_client import FirewallRuleSpec, OCIClientError
from web.backend.audit import write_audit
from web.backend.auth import get_current_user
from web.backend.db import get_db
from web.backend.models import User
from web.backend.oci_bridge import get_owned_tenant, get_session_for_row, op_result_dict
from web.backend.schemas import PowerActionResult

router = APIRouter(tags=["instance-ops"])


class ConsoleCreateRequest(BaseModel):
    ssh_public_key: str = Field(min_length=20)


class BootVolumeUpdateRequest(BaseModel):
    size_in_gbs: Optional[int] = None
    vpus_per_gb: Optional[int] = None


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
        conns = session.list_console_connections(instance_id, info.compartment_id)
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
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
        session.delete_console_connection(connection_id)
        return {"message": "已删除控制台连接"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
) -> PowerActionResult:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.resize_boot_volume(
            instance_id,
            info.compartment_id,
            size_in_gbs=body.size_in_gbs,
            vpus_per_gb=body.vpus_per_gb,
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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


@router.post("/tenants/{tenant_id}/instances/{instance_id}/create-image")
def create_custom_image(
    tenant_id: str,
    instance_id: str,
    body: CustomImageCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PowerActionResult:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        info = session.get_instance(instance_id, resolve_ips=False)
        result = session.create_custom_image(instance_id, info.compartment_id, body.display_name)
        write_audit(
            db,
            owner_id=user.id,
            action="image.create",
            target=instance_id,
            detail={"tenant_id": tenant_id, "ok": result.ok, "message": result.message},
        )
        return PowerActionResult(**op_result_dict(result))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
