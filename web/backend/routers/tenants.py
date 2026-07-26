"""Tenant management routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_store import TenantConfig
from web.backend.auth import get_current_user
from web.backend.crypto_util import encrypt_text
from web.backend.db import get_db
from web.backend.launch_service import clear_launch_meta_cache
from web.backend.models import Tenant, User
from web.backend.oci_bridge import drop_session, get_owned_tenant, get_session_for_row
from web.backend.schemas import (
    OciPasswordPolicyOut,
    TenantCreate,
    TenantOut,
    TenantParseResult,
    TenantPasteImport,
    TenantTestResult,
    TenantUpdate,
)
from web.backend.tenant_import import parse_pasted_oci_bundle

router = APIRouter(prefix="/tenants", tags=["tenants"])


def _to_out(row: Tenant) -> TenantOut:
    return TenantOut(
        id=row.id,
        name=row.name,
        user_ocid=row.user_ocid,
        tenancy_ocid=row.tenancy_ocid,
        fingerprint=row.fingerprint,
        region=row.region,
        compartment_ocid=row.compartment_ocid or "",
        description=row.description or "",
        enabled=bool(row.enabled),
        color=row.color or "#3B82F6",
        has_private_key=bool(row.private_key_encrypted),
        account_tier=row.account_tier or "",
        budget_monthly_usd=float(row.budget_monthly_usd or 0.0),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_fields(
    *,
    name: str,
    user_ocid: str,
    tenancy_ocid: str,
    fingerprint: str,
    region: str,
    private_key_pem: str,
    compartment_ocid: str = "",
) -> None:
    cfg = TenantConfig(
        id="validate",
        name=name,
        user_ocid=user_ocid,
        tenancy_ocid=tenancy_ocid,
        fingerprint=fingerprint,
        region=region,
        private_key_pem=private_key_pem,
        compartment_ocid=compartment_ocid or "",
    )
    errors = cfg.validate()
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))


@router.get("", response_model=list[TenantOut])
def list_tenants(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[TenantOut]:
    rows = db.scalars(select(Tenant).where(Tenant.owner_id == user.id).order_by(Tenant.name)).all()
    return [_to_out(r) for r in rows]


@router.post("/parse", response_model=TenantParseResult)
def parse_tenant_paste(
    body: TenantPasteImport,
    user: Annotated[User, Depends(get_current_user)],
) -> TenantParseResult:
    """Parse pasted OCI config text without saving. Does not return private key material."""
    _ = user
    result = parse_pasted_oci_bundle(
        body.api_text,
        private_key_pem=body.private_key_pem,
        name_override=body.name,
        description=body.description,
        compartment_override=body.compartment_ocid,
    )
    return TenantParseResult(
        ok=bool(result.get("ok")),
        message=str(result.get("message") or ""),
        name=str(result.get("name") or ""),
        user_ocid=str(result.get("user_ocid") or ""),
        tenancy_ocid=str(result.get("tenancy_ocid") or ""),
        fingerprint=str(result.get("fingerprint") or ""),
        region=str(result.get("region") or ""),
        compartment_ocid=str(result.get("compartment_ocid") or ""),
        has_private_key=bool(result.get("has_private_key")),
        key_file_hint=str(result.get("key_file_hint") or ""),
        warnings=list(result.get("warnings") or []),
    )


@router.post("/import", response_model=TenantOut, status_code=201)
def import_tenant_from_paste(
    body: TenantPasteImport,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantOut:
    """Paste OCI config (+ PEM) and create a tenant in one step."""
    result = parse_pasted_oci_bundle(
        body.api_text,
        private_key_pem=body.private_key_pem,
        name_override=body.name,
        description=body.description,
        compartment_override=body.compartment_ocid,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message") or "解析失败")

    _validate_fields(
        name=str(result["name"]),
        user_ocid=str(result["user_ocid"]),
        tenancy_ocid=str(result["tenancy_ocid"]),
        fingerprint=str(result["fingerprint"]),
        region=str(result["region"]),
        private_key_pem=str(result["private_key_pem"]),
        compartment_ocid=str(result.get("compartment_ocid") or ""),
    )

    row = Tenant(
        owner_id=user.id,
        name=str(result["name"]).strip(),
        user_ocid=str(result["user_ocid"]).strip(),
        tenancy_ocid=str(result["tenancy_ocid"]).strip(),
        fingerprint=str(result["fingerprint"]).strip(),
        region=str(result["region"]).strip(),
        compartment_ocid=str(result.get("compartment_ocid") or "").strip(),
        description=str(result.get("description") or body.description or "").strip(),
        enabled=True,
        color="#3B82F6",
        private_key_encrypted=encrypt_text(str(result["private_key_pem"]).strip()),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    if body.test_connection:
        try:
            session = get_session_for_row(row)
            test = session.test_connection()
            if not test.ok:
                pass
        except Exception:
            pass

    return _to_out(row)


@router.post("", response_model=TenantOut, status_code=201)
def create_tenant(
    body: TenantCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantOut:
    _validate_fields(
        name=body.name,
        user_ocid=body.user_ocid,
        tenancy_ocid=body.tenancy_ocid,
        fingerprint=body.fingerprint,
        region=body.region,
        private_key_pem=body.private_key_pem,
        compartment_ocid=body.compartment_ocid,
    )
    row = Tenant(
        owner_id=user.id,
        name=body.name.strip(),
        user_ocid=body.user_ocid.strip(),
        tenancy_ocid=body.tenancy_ocid.strip(),
        fingerprint=body.fingerprint.strip(),
        region=body.region.strip(),
        compartment_ocid=(body.compartment_ocid or "").strip(),
        description=(body.description or "").strip(),
        enabled=body.enabled,
        color=body.color or "#3B82F6",
        private_key_encrypted=encrypt_text(body.private_key_pem.strip()),
        budget_monthly_usd=float(body.budget_monthly_usd or 0.0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.get("/{tenant_id}", response_model=TenantOut)
def get_tenant(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantOut:
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _to_out(row)


@router.patch("/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: str,
    body: TenantUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantOut:
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    data = body.model_dump(exclude_unset=True)
    new_pem = data.pop("private_key_pem", None)

    for key, value in data.items():
        if value is not None:
            setattr(row, key, value if not isinstance(value, str) else value.strip())

    from web.backend.crypto_util import decrypt_text

    pem = new_pem.strip() if isinstance(new_pem, str) and new_pem.strip() else decrypt_text(row.private_key_encrypted)
    _validate_fields(
        name=row.name,
        user_ocid=row.user_ocid,
        tenancy_ocid=row.tenancy_ocid,
        fingerprint=row.fingerprint,
        region=row.region,
        private_key_pem=pem,
        compartment_ocid=row.compartment_ocid,
    )
    if isinstance(new_pem, str) and new_pem.strip():
        row.private_key_encrypted = encrypt_text(new_pem.strip())
        drop_session(row.id)

    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    drop_session(row.id)
    return _to_out(row)


@router.get("/{tenant_id}/oci-password-policy", response_model=OciPasswordPolicyOut)
def get_oci_password_policy(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> OciPasswordPolicyOut:
    """Read Oracle Identity Domain password policies (real console force-change settings)."""
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        session = get_session_for_row(row)
        result = session.list_console_password_policies()
        return OciPasswordPolicyOut(
            ok=bool(result.ok),
            message=result.message or "",
            data=result.data if isinstance(result.data, dict) else {},
        )
    except Exception as exc:  # noqa: BLE001
        return OciPasswordPolicyOut(ok=False, message=str(exc), data={})


@router.post("/{tenant_id}/oci-password-policy/disable-expiry", response_model=OciPasswordPolicyOut)
def disable_oci_password_expiry(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> OciPasswordPolicyOut:
    """Call Oracle Identity Domains API to clear passwordExpiresAfter (never expire)."""
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        session = get_session_for_row(row)
        result = session.disable_console_password_expiry()
    except Exception as exc:  # noqa: BLE001
        return OciPasswordPolicyOut(ok=False, message=str(exc), data={})

    return OciPasswordPolicyOut(
        ok=bool(result.ok),
        message=result.message or "",
        data=result.data if isinstance(result.data, dict) else {},
    )


@router.delete("/{tenant_id}")
def delete_tenant(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    drop_session(row.id)
    # The launch-meta cache keyed on this tenant would otherwise be retained until
    # its TTL expired (clear_launch_meta_cache had no callers at all).
    clear_launch_meta_cache(row.id)
    db.delete(row)
    db.commit()
    return {"message": "已删除"}


@router.post("/{tenant_id}/test", response_model=TenantTestResult)
def test_tenant(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TenantTestResult:
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        session = get_session_for_row(row)
        result = session.test_connection()
        return TenantTestResult(ok=bool(result.ok), message=result.message or "")
    except Exception as exc:  # noqa: BLE001
        return TenantTestResult(ok=False, message=str(exc))
