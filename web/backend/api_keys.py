"""API 密钥：给脚本调用「换多出口 IP」用。

为什么不直接用登录的会话：登录响应故意不在 body 里返回令牌（只放 HttpOnly cookie），
会话还会过期、改密码就失效 —— 脚本里只能写死账号密码再模拟登录。密钥则是在面板里
生成一次、随时可删，泄漏了也只能换 IP（routers/open_api.py 是唯一认它的地方，
get_current_user 不认它）。

密钥是 32 字节随机数，库里只存 SHA-256：熵足够高，不需要 bcrypt 那种慢哈希，查找时
按哈希直接命中，也就没有逐字节比较的计时问题。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from web.backend import client_ip
from web.backend.db import get_db
from web.backend.models import ApiKey, User

KEY_PREFIX = "ak_"
MAX_KEYS_PER_USER = 10
# 「最近使用」只是给人看的：一分钟内重复调用不必每次都写库。
_TOUCH_EVERY = timedelta(seconds=60)


@dataclass
class ApiCaller:
    user: User
    key: ApiKey


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def display_prefix(key: str) -> str:
    return key[: len(KEY_PREFIX) + 6]


def _key_from(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return (request.headers.get("x-api-key") or "").strip()


def _aware(dt: datetime) -> datetime:
    # SQLite 读回来的是不带时区的 datetime。
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_api_caller(request: Request, db: Annotated[Session, Depends(get_db)]) -> ApiCaller:
    key = _key_from(request)
    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 API 密钥：请求头加上 Authorization: Bearer <密钥>",
        )
    row = db.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash(key)))
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API 密钥无效或已删除")
    user = db.get(User, row.owner_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")
    if int(row.token_version or 1) != int(user.token_version or 1):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API 密钥已失效（改过密码、开关过两步验证或退出了所有会话），请到「设置」里重新生成",
        )
    now = datetime.now(timezone.utc)
    if row.last_used_at is None or now - _aware(row.last_used_at) > _TOUCH_EVERY:
        row.last_used_at = now
        row.last_used_ip = client_ip.from_request(request)[:64]
        try:
            db.commit()
        except Exception:  # noqa: BLE001 - 记不上「最近使用」不影响这次调用
            db.rollback()
    return ApiCaller(user=user, key=row)


def key_view(row: ApiKey) -> dict:
    def iso(dt):
        return _aware(dt).isoformat() if dt else ""

    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": iso(row.created_at),
        "last_used_at": iso(row.last_used_at),
        "last_used_ip": row.last_used_ip or "",
    }
