"""Block volume + Object Storage management APIs."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import free_quota
from web.backend import quota_guard
from web.backend.audit import write_audit
from web.backend.auth import get_current_user
from web.backend.db import get_db
from web.backend.models import User
from web.backend.oci_bridge import get_owned_tenant, get_session_for_row, op_result_dict
from web.backend.read_cache import cache_key, get_or_load, invalidate
from web.backend.uploads import read_upload_limited

router = APIRouter(tags=["storage"])

_MAX_UPLOAD = 10 * 1024 * 1024


def _row(db: Session, user_id: str, tenant_id: str):
    try:
        return get_owned_tenant(db, user_id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _guard_storage_delta(
    session,
    row,
    *,
    current_size_gb: float,
    new_size_gb: float,
) -> None:
    # free_only was hardcoded True here, which hard-capped PAID tenants at the
    # Always-Free 200GB with no way to opt out. Derive it from the tier like the
    # launch path does.
    tier = getattr(row, "account_tier", "") or ""
    free_only = quota_guard.free_only_for_tenant(row)
    # 副区: Always Free does not reach it and the snapshot is per-region, so the
    # cap below would measure a paid volume against an allowance it never had.
    if quota_guard.secondary_region_gate(session, row, free_only_mode=free_only):
        return
    # usage_snapshot flags a partial/failed read; treating that as zero usage let
    # a throttled read look like a full free quota.
    snap = quota_guard.usage_snapshot(session, free_only_mode=free_only)
    blocked = quota_guard._blocked_by_incomplete_read(snap, free_only)
    if blocked:
        raise HTTPException(status_code=503, detail=blocked)
    guard = free_quota.validate_block_volume_against_quota(
        current_size_gb=current_size_gb,
        new_size_gb=new_size_gb,
        free_only_mode=free_only,
        account_tier=tier,
        usage=snap,
    )
    if not guard.ok:
        raise HTTPException(status_code=400, detail="；".join(guard.error_messages()) or "超出免费块存储额度")


# ---------------------------------------------------------------------------
# Block volumes
# ---------------------------------------------------------------------------


class BlockVolumeCreate(BaseModel):
    display_name: str = ""
    availability_domain: str
    size_in_gbs: int = Field(ge=50, le=32768)
    vpus_per_gb: int = 10
    compartment_id: str = ""


class BlockVolumeUpdate(BaseModel):
    size_in_gbs: Optional[int] = Field(default=None, ge=50, le=32768)
    vpus_per_gb: Optional[int] = None


class BlockVolumeAttach(BaseModel):
    instance_id: str
    type: str = "PARAVIRTUALIZED"  # PARAVIRTUALIZED | ISCSI


class BlockVolumeDetach(BaseModel):
    attachment_id: str


@router.get("/tenants/{tenant_id}/block-volumes")
def list_block_volumes(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    include_subcompartments: bool = Query(True),
    force: bool = Query(False),
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)

        def _load() -> dict[str, Any]:
            result = session.list_block_volumes(
                include_subcompartments=include_subcompartments,
                include_attachments=True,
            )
            return {
                "ok": bool(result.ok),
                "message": result.message or "",
                "data": result.data if isinstance(result.data, dict) else {},
            }

        key = cache_key(row.id, "block-volumes", include_subcompartments)
        payload, age = get_or_load(key, _load, force=force)
        return {**payload, "cached": age > 0, "cache_age_sec": age}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/block-volumes")
def create_block_volume(
    tenant_id: str,
    body: BlockVolumeCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        _guard_storage_delta(session, row, current_size_gb=0, new_size_gb=float(body.size_in_gbs))
        result = session.create_block_volume(
            compartment_id=(body.compartment_id or "").strip() or session.resolve_compartment(),
            availability_domain=body.availability_domain.strip(),
            size_in_gbs=int(body.size_in_gbs),
            display_name=body.display_name,
            vpus_per_gb=int(body.vpus_per_gb or 10),
        )
        invalidate(tenant_id)
        write_audit(
            db,
            owner_id=user.id,
            action="block_volume.create",
            target=tenant_id,
            detail={"ok": result.ok, "size_in_gbs": body.size_in_gbs, "ad": body.availability_domain},
        )
        data = result.data if isinstance(result.data, dict) else {}
        return {**op_result_dict(result), "data": data}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/tenants/{tenant_id}/block-volumes/{volume_id}")
def delete_block_volume(
    tenant_id: str,
    volume_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.delete_block_volume(volume_id)
        invalidate(tenant_id)
        write_audit(
            db,
            owner_id=user.id,
            action="block_volume.delete",
            target=volume_id,
            detail={"ok": result.ok},
        )
        return op_result_dict(result)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/block-volumes/{volume_id}/update")
def update_block_volume(
    tenant_id: str,
    volume_id: str,
    body: BlockVolumeUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        if body.size_in_gbs is not None:
            # Look up current size from list (best-effort) for quota math.
            cur_size = 0.0
            try:
                listed = session.list_block_volumes(include_subcompartments=True, include_attachments=False)
                for v in (listed.data or {}).get("volumes", []) or []:
                    if v.get("id") == volume_id:
                        cur_size = float(v.get("size_in_gbs") or 0)
                        break
            except Exception:
                cur_size = 0.0
            _guard_storage_delta(session, row, current_size_gb=cur_size, new_size_gb=float(body.size_in_gbs))
        result = session.update_block_volume(
            volume_id,
            size_in_gbs=body.size_in_gbs,
            vpus_per_gb=body.vpus_per_gb,
        )
        invalidate(tenant_id)
        write_audit(
            db,
            owner_id=user.id,
            action="block_volume.update",
            target=volume_id,
            detail={"ok": result.ok, "size_in_gbs": body.size_in_gbs, "vpus_per_gb": body.vpus_per_gb},
        )
        data = result.data if isinstance(result.data, dict) else {}
        return {**op_result_dict(result), "data": data}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/block-volumes/{volume_id}/attach")
def attach_block_volume(
    tenant_id: str,
    volume_id: str,
    body: BlockVolumeAttach,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.attach_volume(
            body.instance_id.strip(),
            volume_id,
            type=(body.type or "PARAVIRTUALIZED"),
        )
        invalidate(tenant_id)
        write_audit(
            db,
            owner_id=user.id,
            action="block_volume.attach",
            target=volume_id,
            detail={"ok": result.ok, "instance_id": body.instance_id, "type": body.type},
        )
        data = result.data if isinstance(result.data, dict) else {}
        return {**op_result_dict(result), "data": data}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/block-volumes/detach")
def detach_block_volume(
    tenant_id: str,
    body: BlockVolumeDetach,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.detach_volume(body.attachment_id.strip())
        invalidate(tenant_id)
        write_audit(
            db,
            owner_id=user.id,
            action="block_volume.detach",
            target=body.attachment_id,
            detail={"ok": result.ok},
        )
        return op_result_dict(result)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/tenants/{tenant_id}/instances/{instance_id}/volume-attachments")
def list_instance_volume_attachments(
    tenant_id: str,
    instance_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.list_volume_attachments(instance_id)
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": result.data if isinstance(result.data, dict) else {},
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Object storage
# ---------------------------------------------------------------------------


class BucketCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    public_access_type: str = "NoPublicAccess"
    compartment_id: str = ""


@router.get("/tenants/{tenant_id}/object-storage/namespace")
def object_namespace(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.get_object_namespace()
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": result.data if isinstance(result.data, dict) else {},
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/tenants/{tenant_id}/object-storage/buckets")
def list_buckets(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.list_buckets()
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": result.data if isinstance(result.data, dict) else {},
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/object-storage/buckets")
def create_bucket(
    tenant_id: str,
    body: BucketCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.create_bucket(
            body.name,
            compartment_id=body.compartment_id,
            public_access_type=body.public_access_type,
        )
        invalidate(tenant_id)
        write_audit(
            db,
            owner_id=user.id,
            action="object_storage.create_bucket",
            target=body.name,
            detail={"ok": result.ok},
        )
        data = result.data if isinstance(result.data, dict) else {}
        return {**op_result_dict(result), "data": data}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/tenants/{tenant_id}/object-storage/buckets/{name}")
def delete_bucket(
    tenant_id: str,
    name: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.delete_bucket(name)
        invalidate(tenant_id)
        write_audit(
            db,
            owner_id=user.id,
            action="object_storage.delete_bucket",
            target=name,
            detail={"ok": result.ok},
        )
        return op_result_dict(result)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/tenants/{tenant_id}/object-storage/buckets/{name}/objects")
def list_objects(
    tenant_id: str,
    name: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    prefix: str = Query(""),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.list_objects(name, prefix=prefix, limit=limit)
        return {
            "ok": bool(result.ok),
            "message": result.message or "",
            "data": result.data if isinstance(result.data, dict) else {},
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/tenants/{tenant_id}/object-storage/buckets/{name}/objects/{object_name:path}")
def delete_object(
    tenant_id: str,
    name: str,
    object_name: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = _row(db, user.id, tenant_id)
    try:
        session = get_session_for_row(row)
        result = session.delete_object(name, object_name)
        invalidate(tenant_id)
        write_audit(
            db,
            owner_id=user.id,
            action="object_storage.delete_object",
            target=f"{name}/{object_name}",
            detail={"ok": result.ok},
        )
        return op_result_dict(result)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/object-storage/buckets/{name}/objects")
def put_object(
    tenant_id: str,
    name: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
    object_name: str = Form(""),
) -> dict[str, Any]:
    """Upload one object.

    Sync handler on purpose: ``session.put_object`` is a blocking OCI call, and
    running it inside an ``async def`` stalled the whole event loop (every other
    request on this worker) for the duration of the upload.
    """
    row = _row(db, user.id, tenant_id)
    # Bounded read: reject an oversized upload instead of materializing it first.
    raw = read_upload_limited(
        file, _MAX_UPLOAD, too_large_detail=f"文件超过 {_MAX_UPLOAD} 字节上限"
    )
    obj_name = (object_name or file.filename or "").strip()
    if not obj_name:
        raise HTTPException(status_code=400, detail="缺少对象名")
    try:
        session = get_session_for_row(row)
        result = session.put_object(
            name,
            obj_name,
            raw,
            content_type=file.content_type or "application/octet-stream",
            max_bytes=_MAX_UPLOAD,
        )
        invalidate(tenant_id)
        write_audit(
            db,
            owner_id=user.id,
            action="object_storage.put_object",
            target=f"{name}/{obj_name}",
            detail={"ok": result.ok, "size": len(raw)},
        )
        data = result.data if isinstance(result.data, dict) else {}
        return {**op_result_dict(result), "data": data}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc
