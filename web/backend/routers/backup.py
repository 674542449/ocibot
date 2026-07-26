"""Backup / restore encrypted tenant ZIP (web-side)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_store import TenantConfig
from web.backend.auth import get_current_user
from web.backend.crypto_util import decrypt_text, encrypt_text
from web.backend.db import get_db
from web.backend.models import Tenant, User
from web.backend.audit import write_audit
from web.backend.uploads import read_upload_limited

router = APIRouter(prefix="/backup", tags=["backup"])


class RestoreResult(BaseModel):
    imported: int
    tenant_ids: list[str] = Field(default_factory=list)
    message: str = ""


class ExportRequest(BaseModel):
    password: str = Field(min_length=6)


@router.post("/export")
def export_encrypted_zip(
    body: ExportRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> StreamingResponse:
    """Download AES-encrypted ZIP of all tenants (including private keys).

    Password is accepted in the POST body only — never as a query string
    (query strings land in access logs / browser history).
    """
    import pyzipper

    password = (body.password or "").strip()
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="备份密码至少需要 6 位")

    rows = db.scalars(select(Tenant).where(Tenant.owner_id == user.id).order_by(Tenant.name)).all()
    if not rows:
        raise HTTPException(status_code=400, detail="没有可备份的租户")

    tenants_payload: list[dict[str, Any]] = []
    for row in rows:
        pem = decrypt_text(row.private_key_encrypted) if row.private_key_encrypted else ""
        tenants_payload.append(
            {
                "id": row.id,
                "name": row.name,
                "user_ocid": row.user_ocid,
                "tenancy_ocid": row.tenancy_ocid,
                "fingerprint": row.fingerprint,
                "region": row.region,
                "compartment_ocid": row.compartment_ocid or "",
                "description": row.description or "",
                "enabled": bool(row.enabled),
                "color": row.color or "#3B82F6",
                "private_key_pem": pem,
                "password_changed_at": row.password_changed_at or "",
                "password_expiry_days": int(row.password_expiry_days or 0),
                "account_tier": row.account_tier or "",
            }
        )
    content = json.dumps({"version": 1, "tenants": tenants_payload}, ensure_ascii=False, indent=2).encode(
        "utf-8"
    )

    buf = io.BytesIO()
    with pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
        zf.setpassword(password.encode("utf-8"))
        zf.writestr("tenants.json", content)
    buf.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"ocibot-backup-{stamp}.zip"
    write_audit(
        db,
        owner_id=user.id,
        action="backup.export",
        target=filename,
        detail={"tenant_count": len(tenants_payload)},
    )
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", response_model=RestoreResult)
def import_encrypted_zip(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    password: Annotated[str, Form(max_length=512)],
    file: UploadFile = File(...),
) -> RestoreResult:
    """Restore tenants from an AES ZIP.

    Sync handler on purpose: ZIP decryption, JSON parsing and the per-tenant
    re-encryption are all blocking, so FastAPI runs this in its threadpool
    instead of stalling the event loop for every other request.
    """
    import pyzipper

    password = (password or "").strip()
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="备份密码至少需要 6 位")
    # Hard cap: 20 MiB encrypted backup is already huge for tenant JSON. Bounded
    # read so an oversized upload is rejected instead of being buffered whole.
    raw_bytes = read_upload_limited(
        file, 20 * 1024 * 1024, too_large_detail="备份文件过大（上限 20MB）"
    )
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="空文件")

    try:
        with pyzipper.AESZipFile(io.BytesIO(raw_bytes), "r") as zf:
            zf.setpassword(password.encode("utf-8"))
            names = zf.namelist()
            member = "tenants.json" if "tenants.json" in names else (names[0] if names else "")
            if not member:
                raise HTTPException(status_code=400, detail="备份文件为空")
            # Guard zip-bomb: refuse members larger than 5 MiB uncompressed.
            info = zf.getinfo(member)
            if int(getattr(info, "file_size", 0) or 0) > 5 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="备份内容过大（解压后上限 5MB）")
            raw = zf.read(member)
            if len(raw) > 5 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="备份内容过大（解压后上限 5MB）")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="无法解密备份文件，请检查密码是否正确") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="备份内容格式无效") from exc

    items = data.get("tenants", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="备份内容格式无效")
    if len(items) > 200:
        raise HTTPException(status_code=400, detail="单次备份租户过多（上限 200）")
    def _clean_tier(raw: Any) -> str:
        tier = str(raw or "").strip().lower()
        return tier if tier in {"free", "paid"} else ""

    def _expiry_days(raw: Any) -> int:
        """Archive-supplied value clamped to the Integer column's safe range."""
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 120
        return max(0, min(value, 36500))

    imported_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # Never honor attacker-chosen owner/id from the archive.
        item.pop("owner_id", None)
        # Validate via TenantConfig
        try:
            cfg = TenantConfig(
                id=str(uuid4()),
                name=str(item.get("name") or item.get("profile") or "导入租户")[:128],
                user_ocid=str(item.get("user_ocid") or item.get("user") or ""),
                tenancy_ocid=str(item.get("tenancy_ocid") or item.get("tenancy") or ""),
                fingerprint=str(item.get("fingerprint") or ""),
                region=str(item.get("region") or "ap-tokyo-1"),
                private_key_pem=str(item.get("private_key_pem") or item.get("key_content") or ""),
                compartment_ocid=str(item.get("compartment_ocid") or item.get("compartment_id") or ""),
                description=str(item.get("description") or "")[:512],
                enabled=bool(item.get("enabled", True)),
                color=str(item.get("color") or "#3B82F6")[:32],
                password_changed_at=str(item.get("password_changed_at") or "")[:64],
                password_expiry_days=_expiry_days(item.get("password_expiry_days") or 120),
                # Normalize at ingest: an arbitrary string from the archive used to
                # flow into the quota guard, where anything unrecognized turned the
                # Always-Free caps off.
                account_tier=_clean_tier(item.get("account_tier")),
            )
            errors = cfg.validate()
            if errors:
                continue
        except Exception:
            continue

        row = Tenant(
            owner_id=user.id,
            name=cfg.name,
            user_ocid=cfg.user_ocid,
            tenancy_ocid=cfg.tenancy_ocid,
            fingerprint=cfg.fingerprint,
            region=cfg.region,
            compartment_ocid=cfg.compartment_ocid or "",
            description=cfg.description or "",
            enabled=cfg.enabled,
            color=cfg.color or "#3B82F6",
            private_key_encrypted=encrypt_text(cfg.private_key_pem),
            password_changed_at=cfg.password_changed_at or "",
            password_expiry_days=int(cfg.password_expiry_days or 0),
            account_tier=cfg.account_tier or "",
        )
        db.add(row)
        db.flush()
        imported_ids.append(row.id)

    db.commit()
    write_audit(
        db,
        owner_id=user.id,
        action="backup.import",
        target=file.filename or "upload.zip",
        detail={"imported": len(imported_ids)},
    )
    return RestoreResult(
        imported=len(imported_ids),
        tenant_ids=imported_ids,
        message=f"已导入 {len(imported_ids)} 个租户",
    )
