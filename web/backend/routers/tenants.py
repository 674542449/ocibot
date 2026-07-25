"""Tenant management routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config_store import TenantConfig
from web.backend.auth import get_current_user
from web.backend.crypto_util import encrypt_text
from web.backend.db import get_db
from web.backend.models import Tenant, User
from web.backend.oci_bridge import drop_session, get_owned_tenant, get_session_for_row
from web.backend.schemas import (
    PasswordPolicyOut,
    PasswordPolicyUpdate,
    TenantCreate,
    TenantOut,
    TenantParseResult,
    TenantPasteImport,
    TenantTestResult,
    TenantUpdate,
)
from web.backend.tenant_import import parse_pasted_oci_bundle

router = APIRouter(prefix="/tenants", tags=["tenants"])


def _tenant_config_for_password(row: Tenant) -> TenantConfig:
    """Build a lightweight TenantConfig so we can reuse password-policy helpers."""
    created = ""
    if row.created_at is not None:
        created = row.created_at.isoformat()
    return TenantConfig(
        id=row.id,
        name=row.name or "",
        user_ocid=row.user_ocid or "",
        tenancy_ocid=row.tenancy_ocid or "",
        fingerprint=row.fingerprint or "",
        region=row.region or "",
        private_key_pem="",
        password_changed_at=row.password_changed_at or "",
        password_expiry_days=int(row.password_expiry_days or 0),
        created_at=created,
    )


def _password_policy_fields(row: Tenant) -> dict:
    cfg = _tenant_config_for_password(row)
    expiry = cfg.password_expiry_date()
    status, message = cfg.password_status()
    return {
        "password_changed_at": (row.password_changed_at or "").strip(),
        "password_expiry_days": int(row.password_expiry_days or 0),
        "password_expires_on": expiry.date().isoformat() if expiry else "",
        "password_days_left": cfg.password_days_left(),
        "password_status": status,
        "message": message,
    }


def _normalize_password_changed_at(value: Optional[str]) -> str:
    """Accept YYYY-MM-DD (or longer ISO); empty clears. Raises HTTPException on bad input."""
    if value is None:
        return ""
    raw = value.strip()
    if not raw:
        return ""
    day = raw[:10]
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="密码修改日须为 YYYY-MM-DD") from exc
    return day


def _to_out(row: Tenant) -> TenantOut:
    policy = _password_policy_fields(row)
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
        password_changed_at=policy["password_changed_at"],
        password_expiry_days=policy["password_expiry_days"],
        password_expires_on=policy["password_expires_on"],
        password_days_left=policy["password_days_left"],
        password_status=policy["password_status"],
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
                # Keep tenant but surface failure via 201 + client can re-test;
                # still return created tenant. Message is not part of TenantOut.
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
        password_changed_at=_normalize_password_changed_at(body.password_changed_at or ""),
        password_expiry_days=int(body.password_expiry_days or 0),
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

    if "password_changed_at" in data:
        data["password_changed_at"] = _normalize_password_changed_at(data.get("password_changed_at"))
        # Reset daily notify flag so a new baseline can re-alert when due again.
        row.pwd_expiry_notified_on = ""
    if "password_expiry_days" in data and data["password_expiry_days"] is not None:
        data["password_expiry_days"] = int(data["password_expiry_days"])

    for key, value in data.items():
        if value is not None or key == "password_changed_at":
            setattr(row, key, value if not isinstance(value, str) else value.strip())

    # Validate with effective PEM
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


@router.get("/{tenant_id}/password-policy", response_model=PasswordPolicyOut)
def get_password_policy(
    tenant_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PasswordPolicyOut:
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PasswordPolicyOut(**_password_policy_fields(row))


@router.patch("/{tenant_id}/password-policy", response_model=PasswordPolicyOut)
def update_password_policy(
    tenant_id: str,
    body: PasswordPolicyUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PasswordPolicyOut:
    """Persist password reminder settings (days / last-changed date) on the tenant row."""
    try:
        row = get_owned_tenant(db, user.id, tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    data = body.model_dump(exclude_unset=True)
    changed = False

    if body.mark_changed_today:
        row.password_changed_at = datetime.now(timezone.utc).date().isoformat()
        row.pwd_expiry_notified_on = ""
        changed = True
    elif "password_changed_at" in data:
        row.password_changed_at = _normalize_password_changed_at(data.get("password_changed_at"))
        row.pwd_expiry_notified_on = ""
        changed = True

    if "password_expiry_days" in data and data["password_expiry_days"] is not None:
        row.password_expiry_days = int(data["password_expiry_days"])
        changed = True

    if not changed:
        raise HTTPException(status_code=400, detail="未提供可更新的密码策略字段")

    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return PasswordPolicyOut(**_password_policy_fields(row))


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
