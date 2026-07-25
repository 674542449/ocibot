"""Admin: user management + panel settings (first registered user is admin)."""

from __future__ import annotations

import secrets
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from web.backend.audit import write_audit
from web.backend.auth import get_admin_user, hash_password
from web.backend.config import get_settings
from web.backend.db import get_db
from web.backend.meta import KEY_OPEN_REGISTRATION, get_meta, set_meta
from web.backend.models import Tenant, User

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminUserOut(BaseModel):
    id: str
    username: str
    is_active: bool
    is_admin: bool
    totp_enabled: bool
    tenant_count: int
    created_at: str


class AdminUserPatch(BaseModel):
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    # Set a new password for the user (also revokes their sessions).
    new_password: Optional[str] = Field(default=None, min_length=8, max_length=128)


class AdminSettingsOut(BaseModel):
    allow_open_registration: bool
    source: str  # "db" | "env"


class AdminSettingsPatch(BaseModel):
    allow_open_registration: bool


@router.get("/users", response_model=list[AdminUserOut])
def list_users(
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[AdminUserOut]:
    _ = admin
    counts = dict(
        db.execute(
            select(Tenant.owner_id, func.count()).group_by(Tenant.owner_id)
        ).all()
    )
    rows = db.scalars(select(User).order_by(User.created_at)).all()
    return [
        AdminUserOut(
            id=u.id,
            username=u.username,
            is_active=bool(u.is_active),
            is_admin=bool(u.is_admin),
            totp_enabled=bool(u.totp_enabled),
            tenant_count=int(counts.get(u.id, 0)),
            created_at=u.created_at.isoformat() if u.created_at else "",
        )
        for u in rows
    ]


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def patch_user(
    user_id: str,
    body: AdminUserPatch,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AdminUserOut:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == admin.id and body.is_active is False:
        raise HTTPException(status_code=400, detail="不能禁用自己的账号")
    if target.id == admin.id and body.is_admin is False:
        raise HTTPException(status_code=400, detail="不能移除自己的管理员权限")

    changed: list[str] = []
    if body.is_active is not None:
        target.is_active = bool(body.is_active)
        changed.append(f"is_active={target.is_active}")
        if not target.is_active:
            target.token_version = int(target.token_version or 1) + 1
    if body.is_admin is not None:
        target.is_admin = bool(body.is_admin)
        changed.append(f"is_admin={target.is_admin}")
    if body.new_password:
        target.password_hash = hash_password(body.new_password)
        target.token_version = int(target.token_version or 1) + 1
        changed.append("password_reset")
    db.commit()
    db.refresh(target)
    write_audit(
        db,
        owner_id=admin.id,
        action="admin.user_patch",
        target=target.username,
        detail={"changes": changed},
    )
    tenant_count = int(
        db.scalar(select(func.count()).select_from(Tenant).where(Tenant.owner_id == target.id)) or 0
    )
    return AdminUserOut(
        id=target.id,
        username=target.username,
        is_active=bool(target.is_active),
        is_admin=bool(target.is_admin),
        totp_enabled=bool(target.totp_enabled),
        tenant_count=tenant_count,
        created_at=target.created_at.isoformat() if target.created_at else "",
    )


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: str,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Generate a random password for the user and return it once."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    new_password = secrets.token_urlsafe(12)
    target.password_hash = hash_password(new_password)
    target.token_version = int(target.token_version or 1) + 1
    # Losing the authenticator is the common reason for admin resets; clear 2FA
    # so the user can actually log back in, and note it in the response.
    totp_cleared = bool(target.totp_enabled)
    target.totp_enabled = False
    target.totp_secret_encrypted = ""
    db.commit()
    write_audit(
        db,
        owner_id=admin.id,
        action="admin.reset_password",
        target=target.username,
        detail={"totp_cleared": totp_cleared},
    )
    return {
        "username": target.username,
        "new_password": new_password,
        "totp_cleared": totp_cleared,
        "message": "请立即将新密码告知用户；该密码不会再次显示",
    }


@router.get("/settings", response_model=AdminSettingsOut)
def get_admin_settings(
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AdminSettingsOut:
    _ = admin
    stored = get_meta(db, KEY_OPEN_REGISTRATION)
    if stored is not None and stored != "":
        return AdminSettingsOut(
            allow_open_registration=stored.strip().lower() in {"1", "true", "yes"},
            source="db",
        )
    return AdminSettingsOut(
        allow_open_registration=bool(get_settings().allow_open_registration),
        source="env",
    )


@router.put("/settings", response_model=AdminSettingsOut)
def put_admin_settings(
    body: AdminSettingsPatch,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AdminSettingsOut:
    set_meta(db, KEY_OPEN_REGISTRATION, "true" if body.allow_open_registration else "false")
    db.commit()
    write_audit(
        db,
        owner_id=admin.id,
        action="admin.settings",
        target="allow_open_registration",
        detail=str(body.allow_open_registration),
    )
    return AdminSettingsOut(allow_open_registration=body.allow_open_registration, source="db")
