"""Admin: user management + panel settings (first registered user is admin)."""

from __future__ import annotations

from app.oci_client import safe_error_text

import secrets
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from web.backend.audit import write_audit
from web.backend.auth import get_admin_user, hash_password
from web.backend.config import get_settings
from web.backend.db import get_db
from web.backend.meta import KEY_OPEN_REGISTRATION, get_meta, set_meta
from web.backend.schemas import UtcDatetime
from web.backend.models import Tenant, User
from web.backend import client_ip, ip_ban, self_update

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminUserOut(BaseModel):
    id: str
    username: str
    is_active: bool
    is_admin: bool
    totp_enabled: bool
    tenant_count: int
    # Was a bare isoformat() of a naive value, so the browser read UTC as local
    # time and 注册时间 appeared shifted by the viewer's offset.
    created_at: Optional[UtcDatetime] = None


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
            created_at=u.created_at,
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
        created_at=target.created_at,
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
    if target.id == admin.id:
        # Mirrors the self-guards in patch_user. Resetting your OWN password here
        # revokes your session and clears your 2FA while returning the only copy of
        # the new password in the response body — a lone admin who loses it has no
        # recovery path. Changing your own password has its own flow.
        raise HTTPException(status_code=400, detail="请在「设置」页修改自己的密码")
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


# ---------------------------------------------------------------------------
# IP 封禁（web/backend/ip_ban.py）
# ---------------------------------------------------------------------------


class IpBanOut(BaseModel):
    ip: str
    reason: str
    detail: str
    created_at: Optional[UtcDatetime] = None


class IpBanRules(BaseModel):
    user_agents: list[str] = Field(default_factory=list, max_length=ip_ban.MAX_RULES)
    usernames: list[str] = Field(default_factory=list, max_length=ip_ban.MAX_RULES)
    # 不传 = 保持原样（老前端不认识这个开关）。
    ban_empty_user_agent: Optional[bool] = None


class IpBanAdd(BaseModel):
    ip: str = Field(min_length=1, max_length=64)


class IpBanRemove(BaseModel):
    ips: list[str] = Field(min_length=1, max_length=1000)


def _viewer(request: Request) -> dict[str, Any]:
    """面板眼里的「你」：用来确认真实 IP 识别对不对（在 Cloudflare / 反代后面尤其要看）。"""
    peer = request.client.host if request.client else ""
    resolved = client_ip.from_request(request)
    return {
        "your_ip": resolved,
        "peer_ip": peer,
        "via_cloudflare": client_ip.is_cloudflare(peer),
        "your_ip_exempt": ip_ban.exempt_reason(resolved),
        "user_agent": (request.headers.get("user-agent") or "")[:200],
    }


@router.get("/ip-bans")
def list_ip_bans(
    request: Request,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    _ = admin
    rows, total = ip_ban.list_bans(db)
    return {
        "bans": [IpBanOut(**r).model_dump(mode="json") for r in rows],
        "total": total,
        "rules": ip_ban.get_rules(db),
        "recommended_user_agents": list(ip_ban.RECOMMENDED_USER_AGENTS),
        "viewer": _viewer(request),
    }


@router.post("/ip-bans")
def add_ip_ban(
    body: IpBanAdd,
    request: Request,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    ip = client_ip.normalize_ip(body.ip)
    # 按封禁键比：IPv6 封的是整个 /64，和你同一个 /64 的地址也会把你自己封掉。
    if ip_ban.ban_key(ip) == ip_ban.ban_key(client_ip.from_request(request)):
        raise HTTPException(status_code=400, detail="这是你当前的 IP（或同一个 IPv6 /64），封了你自己也进不来")
    banned, message = ip_ban.ban_ip(
        db, ip, reason="manual", detail=f"管理员 {admin.username} 手动封禁", actor_id=admin.id
    )
    if not banned:
        raise HTTPException(status_code=400, detail=f"不能封禁 {ip}：{message}")
    return {"ok": True, "ip": ip_ban.ban_key(ip), "message": message}


@router.post("/ip-bans/unban")
def remove_ip_bans(
    body: IpBanRemove,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    removed = ip_ban.unban_ips(db, body.ips, actor_id=admin.id)
    return {"ok": True, "removed": removed, "message": f"已解封 {len(removed)} 个 IP"}


@router.put("/ip-ban-rules")
def put_ip_ban_rules(
    body: IpBanRules,
    request: Request,
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    uas = ip_ban.clean_user_agent_rules(body.user_agents)
    # 自己的浏览器 UA 被某条规则命中的话，保存后的下一个请求就把自己封了（除非 IP 本来免封）。
    # 不管 IP 免不免封，会命中正常浏览器的规则（~mozilla、~chrome）一律拒绝 —— 那会封掉所有访客。
    mine = (request.headers.get("user-agent") or "").strip()
    if ip_ban.exempt_reason(client_ip.from_request(request)):
        mine = ""
    problem = ip_ban.validate_user_agent_rules(uas, own_ua=mine)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    rules = ip_ban.set_rules(
        db,
        user_agents=uas,
        usernames=body.usernames,
        ban_empty_user_agent=body.ban_empty_user_agent,
    )
    write_audit(
        db,
        owner_id=admin.id,
        action="admin.ip_ban_rules",
        target="ip_ban_rules",
        detail=rules,
    )
    return {"ok": True, "rules": rules}


# ---------------------------------------------------------------------------
# In-panel self-update (Docker host)
# ---------------------------------------------------------------------------


@router.get("/update")
def get_update_status(
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    _ = admin
    return self_update.get_status(db)


@router.post("/update/check")
def check_update(
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    _ = admin
    try:
        return self_update.check_for_update(db)
    except Exception as exc:  # noqa: BLE001
        # check_for_update already persisted error state when possible
        raise HTTPException(status_code=502, detail=safe_error_text(exc)) from exc


@router.post("/update/apply")
def apply_update(
    admin: Annotated[User, Depends(get_admin_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    try:
        result = self_update.start_update(db, username=admin.username)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=safe_error_text(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=safe_error_text(exc)) from exc
    write_audit(
        db,
        owner_id=admin.id,
        action="admin.self_update",
        target="apply",
        detail={"repo": result.get("local", {}).get("repo"), "state": result.get("state")},
    )
    return result
