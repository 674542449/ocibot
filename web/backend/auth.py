"""JWT auth helpers — Bearer header and/or HttpOnly cookie."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from web.backend.config import get_settings
from web.backend.db import get_db
from web.backend.models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)

COOKIE_NAME = "ocibot_token"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(*, user_id: str, username: str, token_version: int = 1) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub": user_id,
        "username": username,
        "ver": int(token_version),
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def _extract_token(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    if creds is not None and creds.credentials:
        return creds.credentials
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        return cookie
    return None


def get_current_user(
    request: Request,
    creds: Annotated[Optional[HTTPAuthorizationCredentials], Depends(security)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    token = _extract_token(request, creds)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效令牌")
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效或已过期") from exc

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")
    # Token revocation: version bumped on password change / logout-everywhere.
    token_ver = int(payload.get("ver") or 1)
    if token_ver != int(user.token_version or 1):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效，请重新登录")
    return user


def get_admin_user(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    if not bool(user.is_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def count_users(db: Session) -> int:
    from sqlalchemy import func

    return int(db.scalar(select(func.count()).select_from(User)) or 0)


def set_auth_cookie(response, token: str) -> None:
    settings = get_settings()
    max_age = int(settings.jwt_expire_minutes * 60)
    samesite = (settings.cookie_samesite or "lax").strip().lower()
    if samesite not in {"lax", "strict", "none"}:
        samesite = "lax"
    # Browsers reject SameSite=None without Secure; force Secure in that case.
    secure = bool(settings.cookie_secure) or samesite == "none"
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite=samesite,
        secure=secure,
        max_age=max_age,
        path="/",
    )


def clear_auth_cookie(response) -> None:
    settings = get_settings()
    samesite = (settings.cookie_samesite or "lax").strip().lower()
    if samesite not in {"lax", "strict", "none"}:
        samesite = "lax"
    secure = bool(settings.cookie_secure) or samesite == "none"
    # Match the attributes used when setting the cookie so the browser deletes it.
    response.delete_cookie(key=COOKIE_NAME, path="/", samesite=samesite, secure=secure)
