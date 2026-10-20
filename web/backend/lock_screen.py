"""登录锁屏：账号密码登录框之前的一道锁屏密码，外加一个蜜罐。

管理员在「用户管理 · 系统更新」→「登录锁屏」里设一个锁屏密码之后：
  * 登录页先显示锁屏，输对锁屏密码才出现账号密码框；
  * 服务端同样把关：``/api/auth/login``、``/api/auth/register`` 要求带着解锁凭证
    （``POST /api/auth/unlock`` 成功后下发的 HttpOnly Cookie），直接调接口的爆破脚本
    拿不到凭证，连账号密码都试不了；
  * 锁屏页上写着「默认密码为 123456」—— 这是诱饵。真正的锁屏密码不允许设成它，所以
    输入 123456 的只会是照着提示来的人或脚本，直接封 IP（ip_ban.py）。

没设锁屏密码时整个功能不生效：刚升级或新装的面板不会把运营者自己挡在外面。
忘了锁屏密码：在服务器上 ``bash scripts/install.sh lock-off``（即
``python -m web.backend.lock_screen off``）关掉锁屏。
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

DECOY_PASSWORD = "123456"
UNLOCK_COOKIE = "ocibot_unlock"
# 解锁之后多久内要完成账号密码（含两步验证）的输入。过期了再输一次锁屏密码。
UNLOCK_TTL_MIN = 30
MIN_LENGTH = 6
MAX_LENGTH = 128
# 登录页拿到这个 detail 就切回锁屏（和 'totp_required' 同一种约定）。
UNLOCK_REQUIRED = "unlock_required"


def _stored_hash(db: Session) -> str:
    from web.backend.meta import KEY_LOCK_PASSWORD_HASH, get_meta

    return get_meta(db, KEY_LOCK_PASSWORD_HASH) or ""


def enabled(db: Session) -> bool:
    return bool(_stored_hash(db))


def password_problem(plain: str) -> str:
    """不能用作锁屏密码的原因；可以用返回空串。"""
    if len(plain) < MIN_LENGTH:
        return f"锁屏密码至少 {MIN_LENGTH} 位"
    if len(plain) > MAX_LENGTH:
        return f"锁屏密码最多 {MAX_LENGTH} 位"
    if plain == DECOY_PASSWORD:
        return f"{DECOY_PASSWORD} 是锁屏页上的诱饵密码（输入它的人会被封 IP），不能用作真正的锁屏密码"
    return ""


def set_password(db: Session, plain: str) -> None:
    from web.backend.auth import hash_password
    from web.backend.meta import KEY_LOCK_PASSWORD_HASH, set_meta

    set_meta(db, KEY_LOCK_PASSWORD_HASH, hash_password(plain))
    db.commit()


def disable(db: Session) -> None:
    from web.backend.meta import KEY_LOCK_PASSWORD_HASH, set_meta

    set_meta(db, KEY_LOCK_PASSWORD_HASH, "")
    db.commit()


def verify(db: Session, plain: str) -> bool:
    from web.backend.auth import verify_password

    stored = _stored_hash(db)
    return bool(stored) and verify_password(plain, stored)


# ---------------------------------------------------------------- 解锁凭证


def _signing_key() -> str:
    """从 JWT 密钥派生出的另一把钥匙：解锁凭证和登录会话用不同的钥匙签，
    一张解锁凭证无论如何也不会被当成登录会话接受。"""
    from web.backend.config import get_settings

    secret = str(get_settings().jwt_secret or "").encode()
    return hmac.new(secret, b"ocibot-unlock-v1", hashlib.sha256).hexdigest()


def _fingerprint(stored_hash: str) -> str:
    """锁屏密码的指纹：改了锁屏密码，之前发出去的解锁凭证全部作废。"""
    return hashlib.sha256(stored_hash.encode()).hexdigest()[:16]


def issue_token(db: Session) -> str:
    import jwt

    payload = {
        "typ": "unlock",
        "fp": _fingerprint(_stored_hash(db)),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=UNLOCK_TTL_MIN),
    }
    return jwt.encode(payload, _signing_key(), algorithm="HS256")


def token_valid(db: Session, token: Optional[str]) -> bool:
    import jwt

    stored = _stored_hash(db)
    if not stored or not token:
        return False
    try:
        payload = jwt.decode(token, _signing_key(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return False
    return payload.get("typ") == "unlock" and payload.get("fp") == _fingerprint(stored)


def request_unlocked(request: Any, db: Session) -> bool:
    """没开锁屏，或者带着有效的解锁凭证。"""
    if not enabled(db):
        return True
    return token_valid(db, request.cookies.get(UNLOCK_COOKIE))


def _cookie_attrs() -> dict[str, Any]:
    from web.backend.config import get_settings

    settings = get_settings()
    samesite = (settings.cookie_samesite or "lax").strip().lower()
    if samesite not in {"lax", "strict", "none"}:
        samesite = "lax"
    return {
        "httponly": True,
        "samesite": samesite,
        "secure": bool(settings.cookie_secure) or samesite == "none",
        # 只有登录 / 注册接口要看它。
        "path": "/api/auth",
    }


def set_unlock_cookie(response: Any, token: str) -> None:
    response.set_cookie(key=UNLOCK_COOKIE, value=token, max_age=UNLOCK_TTL_MIN * 60, **_cookie_attrs())


# ---------------------------------------------------------------- 命令行


def _main(argv: list[str]) -> int:
    """忘了锁屏密码时在服务器上关掉锁屏：

        python -m web.backend.lock_screen off
        python -m web.backend.lock_screen status
    """
    from web.backend.db import SessionLocal, init_db

    init_db()
    cmd = argv[0] if argv else "status"
    with SessionLocal() as db:
        if cmd == "off":
            disable(db)
            print("登录锁屏已关闭。登录后可在「用户管理 · 系统更新」→「登录锁屏」重新设置。")
            return 0
        if cmd == "status":
            print("登录锁屏：" + ("已开启" if enabled(db) else "未开启"))
            return 0
    print(_main.__doc__)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
