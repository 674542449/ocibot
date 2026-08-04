"""Auth + user routes (cookie + bearer, rate-limited, TOTP, revocation)."""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from web.backend.audit import write_audit
from web.backend.auth import (
    clear_auth_cookie,
    count_users,
    create_access_token,
    get_current_user,
    hash_password,
    set_auth_cookie,
    verify_password,
)
from web.backend.config import get_settings
from web.backend.crypto_util import decrypt_text, encrypt_text
from web.backend.db import get_db
from web.backend.meta import KEY_OPEN_REGISTRATION, get_meta
from web.backend.models import Tenant, User
from web.backend.rate_limit import login_ip_limiter, login_user_limiter, register_ip_limiter
from web.backend.schemas import (
    ChangePasswordRequest,
    LockedTenantRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    TotpDisableRequest,
    TotpEnableRequest,
    TotpSetupOut,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Precomputed bcrypt hash used only to equalize login timing when the username
# does not exist (never a valid password for a real account).
_DUMMY_PASSWORD_HASH = hash_password("ocibot-timing-pad-not-a-real-password")


def _client_ip(request: Request) -> str:
    """Client IP used as the rate-limit bucket key.

    Deliberately does NOT parse X-Forwarded-For itself. Taking the leftmost entry
    is wrong whenever the proxy *appends* instead of replacing (nginx's canonical
    ``$proxy_add_x_forwarded_for``, Caddy, cloudflared): the leftmost element is
    then client-supplied, so a forged value produced a fresh bucket per request
    and the login limiter could still be bypassed with OCIBOT_TRUST_PROXY=1.

    ``request.client.host`` is authoritative instead: run.py enables uvicorn's
    ProxyHeadersMiddleware only when OCIBOT_TRUST_PROXY=1, and only for peers in
    OCIBOT_FORWARDED_ALLOW_IPS, and that middleware already walks the header
    right-to-left skipping trusted hops. One parser, one policy.
    """
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def open_registration_allowed(db: Session) -> bool:
    """DB override (admin toggle) wins; falls back to env setting."""
    stored = get_meta(db, KEY_OPEN_REGISTRATION)
    if stored is not None and stored != "":
        return stored.strip().lower() in {"1", "true", "yes"}
    return bool(get_settings().allow_open_registration)


def _issue(response: Response, user: User) -> TokenResponse:
    token = create_access_token(
        user_id=user.id, username=user.username, token_version=int(user.token_version or 1)
    )
    set_auth_cookie(response, token)
    return TokenResponse(access_token=token, username=user.username)


def _verify_totp(user: User, code: str) -> bool:
    import pyotp

    secret = decrypt_text(user.totp_secret_encrypted or "")
    if not secret:
        return False
    return pyotp.TOTP(secret).verify((code or "").strip().replace(" ", ""), valid_window=1)


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    ip = _client_ip(request)
    ok, retry = register_ip_limiter.check(f"reg:{ip}")
    if not ok:
        raise HTTPException(
            status_code=429,
            detail=f"注册过于频繁，请 {int(retry) + 1} 秒后重试",
            headers={"Retry-After": str(int(retry) + 1)},
        )

    username = body.username.strip()
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="用户名至少 3 个字符")

    # Gate BEFORE the existence lookup, otherwise a closed-registration panel still
    # answers "用户名已存在" vs "已关闭开放注册" and becomes an unauthenticated
    # username oracle. The authoritative count for is_admin stays below, inside the
    # advisory lock, so the first-admin race is unaffected.
    if not open_registration_allowed(db) and count_users(db) > 0:
        raise HTTPException(status_code=403, detail="已关闭开放注册，请联系管理员")

    existing = db.scalar(select(User).where(User.username == username))
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")

    # Serialize first-admin bootstrap on PostgreSQL so two concurrent empty-DB
    # registers cannot both become admin. On SQLite this is a no-op and we
    # re-check the count immediately before insert.
    try:
        from sqlalchemy import text

        if not get_settings().is_sqlite:
            db.execute(text("SELECT pg_advisory_xact_lock(87201401)"))
    except Exception:
        # Non-PG or lock unavailable — still re-check count below.
        pass

    user_count = int(db.scalar(select(func.count()).select_from(User)) or 0)
    if user_count > 0 and not open_registration_allowed(db):
        raise HTTPException(status_code=403, detail="已关闭开放注册，请联系管理员")

    user = User(
        username=username,
        password_hash=hash_password(body.password),
        is_admin=(user_count == 0),
    )
    try:
        db.add(user)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=400, detail="用户名已存在或注册冲突，请重试") from exc
    db.refresh(user)

    write_audit(db, owner_id=user.id, action="auth.register", target=user.username, detail=f"ip={ip}")
    return _issue(response, user)


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    ip = _client_ip(request)
    username = body.username.strip()
    # Enough to tell a scripted stuffing run from a human mistyping their own
    # password. The submitted password is deliberately NOT recorded: on a failed
    # attempt it is overwhelmingly someone else's real (leaked) credential, or this
    # operator's own password with one character wrong — and this log is served to a
    # browser, stored in Postgres and copied into every backup archive. It also adds
    # nothing: attribution comes from username + IP + outcome.
    agent = (request.headers.get("user-agent") or "").strip()[:200]

    def _audit(action: str, reason: str, owner_id: Optional[str] = None) -> None:
        write_audit(
            db,
            owner_id=owner_id,
            action=action,
            target=username or "(empty)",
            detail={"ip": ip, "reason": reason, "ua": agent},
        )

    ok_ip, retry_ip = login_ip_limiter.check(f"login-ip:{ip}")
    ok_user, retry_user = login_user_limiter.check(f"login-user:{ip}:{username.lower()}")
    if not ok_ip or not ok_user:
        retry = max(retry_ip, retry_user)
        # A burst tripping the limiter is the clearest single sign of an automated
        # run, and it was the one outcome not recorded at all.
        _audit("auth.login_blocked", "rate_limited")
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过多，请 {int(retry) + 1} 秒后重试",
            headers={"Retry-After": str(int(retry) + 1)},
        )

    user = db.scalar(select(User).where(User.username == username))
    # Always run bcrypt verify to reduce username-enumeration timing skew.
    stored_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(body.password, stored_hash)
    if user is None or not password_ok:
        # no_such_user vs bad_password separates a leaked list being sprayed at
        # invented names from someone working on an account that really exists. The
        # row is attributed to that account when the name matches one, so its owner
        # sees attempts against it without needing to be an admin.
        _audit(
            "auth.login_failed",
            "no_such_user" if user is None else "bad_password",
            owner_id=user.id if user is not None else None,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if not user.is_active:
        _audit("auth.login_disabled", "account_disabled", owner_id=user.id)
        raise HTTPException(status_code=403, detail="账号已禁用")
    if bool(user.totp_enabled):
        code = (body.totp_code or "").strip()
        if not code:
            # Structured marker so the frontend can show the 2FA input.
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="totp_required")
        if not _verify_totp(user, code):
            # The password already verified above, so someone HAS the correct
            # password and only 2FA stopped them. Highest-signal event in this log.
            _audit("auth.totp_failed", "bad_totp_password_was_correct", owner_id=user.id)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="两步验证码错误")
    _audit("auth.login", "ok", owner_id=user.id)
    return _issue(response, user)


@router.put("/locked-tenant", response_model=UserOut)
def set_locked_tenant(
    body: LockedTenantRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> UserOut:
    """Set (or clear) the tenant every page opens with, for this account.

    Server-side so the choice follows the operator to another browser or device.
    An empty tenant_id clears it.
    """
    tenant_id = (body.tenant_id or "").strip()
    if tenant_id:
        owned = db.scalar(
            select(Tenant).where(Tenant.id == tenant_id, Tenant.owner_id == user.id)
        )
        # 404, not 403: an id belonging to someone else must not be distinguishable
        # from one that does not exist.
        if owned is None:
            raise HTTPException(status_code=404, detail="租户不存在")
    user.locked_tenant_id = tenant_id
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/logout")
def logout(response: Response, user: Annotated[User, Depends(get_current_user)]) -> dict:
    clear_auth_cookie(response)
    return {"message": "已退出", "username": user.username}


@router.post("/logout-all")
def logout_all(
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Revoke every issued token (all devices) by bumping the token version."""
    user.token_version = int(user.token_version or 1) + 1
    db.commit()
    clear_auth_cookie(response)
    write_audit(db, owner_id=user.id, action="auth.logout_all", target=user.username)
    return {"message": "已在所有设备退出登录"}


@router.post("/change-password", response_model=TokenResponse)
def change_password(
    body: ChangePasswordRequest,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码错误")
    if body.old_password == body.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
    user.password_hash = hash_password(body.new_password)
    # Invalidate all existing sessions, then issue a fresh token for this one.
    user.token_version = int(user.token_version or 1) + 1
    db.commit()
    db.refresh(user)
    write_audit(db, owner_id=user.id, action="auth.change_password", target=user.username)
    return _issue(response, user)


@router.get("/me", response_model=UserOut)
def me(user: Annotated[User, Depends(get_current_user)]) -> User:
    return user


# ---- TOTP two-factor ----


@router.post("/totp/setup", response_model=TotpSetupOut)
def totp_setup(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TotpSetupOut:
    """Generate a pending TOTP secret. Takes effect after /totp/enable verifies a code."""
    import pyotp

    if bool(user.totp_enabled):
        raise HTTPException(status_code=400, detail="两步验证已开启；如需更换请先关闭")
    secret = pyotp.random_base32()
    user.totp_secret_encrypted = encrypt_text(secret)
    db.commit()
    otpauth = pyotp.totp.TOTP(secret).provisioning_uri(
        name=user.username, issuer_name="OCIBot Web"
    )
    return TotpSetupOut(secret=secret, otpauth_url=otpauth)


@router.post("/totp/enable")
def totp_enable(
    body: TotpEnableRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    if bool(user.totp_enabled):
        raise HTTPException(status_code=400, detail="两步验证已开启")
    if not user.totp_secret_encrypted:
        raise HTTPException(status_code=400, detail="请先调用 /auth/totp/setup 生成密钥")
    if not _verify_totp(user, body.code):
        raise HTTPException(status_code=400, detail="验证码错误，请确认认证器时间与密钥正确")
    user.totp_enabled = True
    db.commit()
    write_audit(db, owner_id=user.id, action="auth.totp_enabled", target=user.username)
    return {"message": "两步验证已开启"}


@router.post("/totp/disable")
def totp_disable(
    body: TotpDisableRequest,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    if not bool(user.totp_enabled):
        raise HTTPException(status_code=400, detail="两步验证未开启")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=400, detail="密码错误")
    if not _verify_totp(user, body.code):
        raise HTTPException(status_code=400, detail="两步验证码错误")
    user.totp_enabled = False
    user.totp_secret_encrypted = ""
    db.commit()
    write_audit(db, owner_id=user.id, action="auth.totp_disabled", target=user.username)
    return {"message": "两步验证已关闭"}
