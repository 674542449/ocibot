"""Bridge web DB tenants to app.oci_client TenantSession."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config_store import TenantConfig
from app.oci_client import (
    ROOT_PASSWORD_TAG,
    InstanceInfo,
    OperationResult,
    SessionManager,
    TenantSession,
    free_tier_tag,
)
from web.backend.crypto_util import decrypt_text
from web.backend.models import Tenant

# Process-wide session cache (API process). Worker keeps its own.
_sessions = SessionManager()


def tenant_row_to_config(row: Tenant) -> TenantConfig:
    pem = decrypt_text(row.private_key_encrypted) if row.private_key_encrypted else ""
    return TenantConfig(
        id=row.id,
        name=row.name,
        user_ocid=row.user_ocid,
        tenancy_ocid=row.tenancy_ocid,
        fingerprint=row.fingerprint,
        region=row.region,
        private_key_pem=pem,
        compartment_ocid=row.compartment_ocid or "",
        description=row.description or "",
        enabled=bool(row.enabled),
        color=row.color or "#3B82F6",
        account_tier=row.account_tier or "",
    )


def get_session_for_row(row: Tenant) -> TenantSession:
    cfg = tenant_row_to_config(row)
    return _sessions.get(cfg)


def drop_session(tenant_id: str) -> None:
    _sessions.drop(tenant_id)


def get_owned_tenant(db: Session, owner_id: str, tenant_id: str) -> Tenant:
    row = db.get(Tenant, tenant_id)
    if row is None or row.owner_id != owner_id:
        raise LookupError("租户不存在")
    return row


def instance_to_dict(info: InstanceInfo, *, tenant_id: str = "", tenant_name: str = "") -> dict[str, Any]:
    return {
        "id": info.id,
        "display_name": info.display_name,
        "lifecycle_state": info.lifecycle_state,
        "shape": info.shape,
        "region": getattr(info, "region", "") or "",
        "availability_domain": info.availability_domain or "",
        "compartment_id": info.compartment_id or "",
        "time_created": info.time_created or "",
        "ocpus": info.ocpus,
        "memory_in_gbs": info.memory_gb,
        "public_ip": info.public_ip or "",
        "private_ip": info.private_ip or "",
        "ipv6_addresses": list(info.ipv6_addresses or []),
        "boot_volume_size_in_gbs": info.boot_volume_gb,
        "free_tier_tag": free_tier_tag(info.shape),
        # Written at launch for password-mode instances (see ROOT_PASSWORD_TAG).
        # It rides along with the instance object, so surfacing it costs no extra
        # OCI call. Plaintext by nature: an OCI freeform tag is readable by anyone
        # with instance-read on the tenancy.
        "root_password": str(
            (getattr(info, "freeform_tags", None) or {}).get(ROOT_PASSWORD_TAG, "") or ""
        ),
        "tenant_id": tenant_id or (info.tenant_id or ""),
        "tenant_name": tenant_name or (info.tenant_name or ""),
    }


def op_result_dict(result: OperationResult) -> dict[str, Any]:
    return {
        "ok": bool(result.ok),
        "message": result.message or "",
        "work_request_id": getattr(result, "work_request_id", "") or "",
    }
