"""Auth + user routes (cookie + bearer, rate-limited, TOTP, revocation)."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Any, Optional

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
from web.backend.rate_limit import (
    SlidingWindowLimiter,
    login_ip_limiter,
    login_user_limiter,
    register_ip_limiter,
)
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

# 被限流拦截的登录同样要写一行审计，但限流器判为 False 时是**不**记账的，
# 所以 30 次/5 分钟的 IP 上限只压住了猜密码的速度，对审计写入没有任何上限：
# 90 个未认证请求就是 90 行，而且行数由攻击者决定。审计表本身又是按行数上限
# 裁剪的，于是一次登录洪泛能把真正有用的历史全部挤出去——正好抹掉这次洪泛
# 之外的一切证据。一个 IP 每 5 分钟留一行（与登录限流器同一个窗口）就够回答
# 「这个地址在被暴力试」这个问题了。
_login_blocked_audit_limiter = SlidingWindowLimiter(max_hits=1, window_sec=300)

# 用户名字符集。只放行 ASCII 字母数字加 . _ -，首字符必须是字母或数字。
#
# 之前只有 .strip() 和一个 3–64 的长度检查，于是这些全部能和 admin 同时存在：
#   ad\nmin / admin​（零宽空格）/ аdmin（西里尔 а）/ ａdmin（全角 ａ）
# 五个账号、四种 NFKC 归一后仍互不相同的写法。它们原样落进 audit_logs.target，
# 管理员看审计表时根本分不清哪一行属于哪个账号。登录和查重用的是同一个
# 逐码点比较，所以没有越权，但可读性本身就是审计的全部价值。
_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")
# Cc 控制字符、Cf 格式字符（零宽空格/RTL 覆写）、Zl/Zp 行段分隔符。
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def normalize_username(raw: str) -> str:
    """NFKC-fold and trim a submitted username.

    NFKC is what collapses the fullwidth/compatibility spellings (ａdmin -> admin)
    onto the plain one. It deliberately does NOT remove zero-width characters —
    those are rejected by the charset rule instead, because silently deleting them
    would store a name different from the one the user typed.
    """
    return unicodedata.normalize("NFKC", raw or "").strip()


def username_error(name: str) -> str:
    """Why this username is not allowed, or "" if it is. Registration only."""
    if len(name) < 3:
        return "用户名至少 3 个字符"
    if len(name) > 64:
        return "用户名最多 64 个字符"
    if any(unicodedata.category(ch) in _INVISIBLE_CATEGORIES for ch in name):
        return "用户名不能包含控制字符或零宽字符"
    if not _USERNAME_RE.fullmatch(name):
        return "用户名只能使用字母、数字和 . _ - ，且必须以字母或数字开头"
    return ""


def audit_username(value: str) -> str:
    """Render a submitted username so an audit row is readable and unambiguous.

    Invisible characters become their escape (``admin\\u200b``) instead of
    rendering as a name identical to another account's, and a name that folds
    onto a different one carries that fold with it.
    """
    shown = "".join(
        f"\\u{ord(ch):04x}" if unicodedata.category(ch) in _INVISIBLE_CATEGORIES else ch
        for ch in value
    )
    folded = normalize_username(value)
    if folded and folded != value:
        return f"{shown} (NFKC={folded})"
    return shown


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

    try:
        secret = decrypt_text(user.totp_secret_encrypted or "")
    except ValueError as exc:
        # crypto_util.decrypt_text raises ValueError when OCIBOT_MASTER_KEY no
        # longer matches the stored ciphertext, and main.py registers no handler
        # for it — so **every 2FA account's login** answered a blank 500 with no
        # hint of the cause. The trigger is routine: a rotated master key, an
        # install that ran on the built-in default before a real key was set, or a
        # compose restart where .env failed to load. Returning False instead would
        # be worse: it reads as "your code is wrong", so the operator spends the
        # incident re-syncing their authenticator clock.
        raise HTTPException(
            status_code=500,
            detail=(
                f"{exc}（两步验证密钥）。这通常是 OCIBOT_MASTER_KEY 变了或 .env 未加载："
                "请恢复原来的主密钥；若主密钥确实无法找回，需由管理员在数据库中"
                "清除该账号的 totp_enabled / totp_secret_encrypted 后重新绑定。"
            ),
        ) from exc
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

    username = normalize_username(body.username)
    charset_error = username_error(username)
    if charset_error:
        raise HTTPException(status_code=400, detail=charset_error)

    # Gate BEFORE the existence lookup, otherwise a closed-registration panel still
    # answers "用户名已存在" vs "已关闭开放注册" and becomes an unauthenticated
    # username oracle. The authoritative count for is_admin stays below, inside the
    # advisory lock, so the first-admin race is unaffected.
    if not open_registration_allowed(db) and count_users(db) > 0:
        raise HTTPException(status_code=403, detail="已关闭开放注册，请联系管理员")

    # Case-insensitive on top of the exact unique constraint: "Admin" next to
    # "admin" is the same confusable problem the charset rule is here to stop, and
    # this can only ever refuse a NEW name — no existing account is affected.
    existing = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
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
    # 登录**不**套用注册那套字符集规则，也不改写查询用的名字：老账号可能是在规则
    # 之前建的，用规则去卡登录等于把它们锁在外面。查询照旧逐码点精确匹配。
    username = body.username.strip()
    # Enough to tell a scripted stuffing run from a human mistyping their own
    # password. The submitted password is deliberately NOT recorded: on a failed
    # attempt it is overwhelmingly someone else's real (leaked) credential, or this
    # operator's own password with one character wrong — and this log is served to a
    # browser, stored in Postgres and copied into every backup archive. It also adds
    # nothing: attribution comes from username + IP + outcome.
    agent = (request.headers.get("user-agent") or "").strip()[:200]

    def _audit(
        action: str,
        reason: str,
        owner_id: Optional[str] = None,
        **extra: Any,
    ) -> None:
        write_audit(
            db,
            owner_id=owner_id,
            action=action,
            # Escaped, never verbatim: an invisible character in the submitted name
            # renders as a row indistinguishable from a different account's.
            target=audit_username(username) if username else "(empty)",
            detail={"ip": ip, "reason": reason, "ua": agent, **extra},
        )

    ok_ip, retry_ip = login_ip_limiter.check(f"login-ip:{ip}")
    ok_user, retry_user = login_user_limiter.check(f"login-user:{ip}:{username.lower()}")
    if not ok_ip or not ok_user:
        retry = max(retry_ip, retry_user)
        # A burst tripping the limiter is the clearest single sign of an automated
        # run, and it was the one outcome not recorded at all. One row per IP per
        # window, though — see _login_blocked_audit_limiter: the limiter refuses
        # without recording a hit, so this branch had no ceiling on audit writes
        # and an unauthenticated flood could push the real history past the row cap.
        if _login_blocked_audit_limiter.check(f"login-blocked:{ip}")[0]:
            _audit("auth.login_blocked", "rate_limited", suppress_window_sec=300)
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过多，请 {int(retry) + 1} 秒后重试",
            headers={"Retry-After": str(int(retry) + 1)},
        )

    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        # Fall back to an NFKC comparison, and only then. Exact-first is what keeps
        # every pre-existing account reachable under the name it was created with;
        # this second pass only helps someone whose IME produced a compatibility
        # spelling (ａdmin) of a name that is otherwise plain. Skipped entirely when
        # the input is already canonical, so a normal login never scans the table,
        # and skipped when it is ambiguous — two accounts folding onto one name must
        # not silently pick one.
        folded = normalize_username(body.username)
        if folded and folded != username:
            candidates = [
                row
                for row in db.scalars(select(User)).all()
                if normalize_username(row.username) == folded
            ]
            if len(candidates) == 1:
                user = candidates[0]
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
