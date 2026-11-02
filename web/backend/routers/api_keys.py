"""管理自己的 API 密钥（登录后的网页用）。密钥本身只能调 routers/open_api.py。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from web.backend import api_keys
from web.backend.audit import write_audit
from web.backend.auth import get_current_user, verify_password
from web.backend.db import get_db
from web.backend.models import ApiKey, User
from web.backend.rate_limit import api_key_password_limiter

router = APIRouter(tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    name: str = Field(default="", max_length=64)
    # 再输一次登录密码：只偷到会话的人不能借此造一把长期有效的密钥。
    password: str = Field(min_length=1, max_length=256)


@router.get("/api-keys")
def list_api_keys(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    rows = db.scalars(
        select(ApiKey).where(ApiKey.owner_id == user.id).order_by(ApiKey.created_at.desc())
    ).all()
    current = int(user.token_version or 1)
    return {
        "items": [
            {**api_keys.key_view(r), "valid": int(r.token_version or 1) == current} for r in rows
        ],
        "limit": api_keys.MAX_KEYS_PER_USER,
    }


@router.post("/api-keys")
def create_api_key(
    body: ApiKeyCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    allowed, retry = api_key_password_limiter.check(user.id)
    if not allowed:
        raise HTTPException(status_code=429, detail=f"尝试次数太多，请 {int(retry) + 1} 秒后再试")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=400, detail="密码错误")
    count = int(db.scalar(select(func.count()).select_from(ApiKey).where(ApiKey.owner_id == user.id)) or 0)
    if count >= api_keys.MAX_KEYS_PER_USER:
        raise HTTPException(
            status_code=400, detail=f"最多 {api_keys.MAX_KEYS_PER_USER} 个密钥，请先删掉不用的"
        )
    key = api_keys.new_key()
    name = "".join(ch for ch in body.name.strip() if ch.isprintable())[:64] or "未命名"
    row = ApiKey(
        owner_id=user.id,
        name=name,
        prefix=api_keys.display_prefix(key),
        key_hash=api_keys.key_hash(key),
        token_version=int(user.token_version or 1),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    write_audit(db, owner_id=user.id, action="api_key.create", target=row.prefix, detail={"name": name})
    # 明文只在这里出现这一次。
    return {**api_keys.key_view(row), "valid": True, "key": key}


@router.delete("/api-keys/{key_id}")
def delete_api_key(
    key_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    row = db.get(ApiKey, key_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(status_code=404, detail="密钥不存在")
    prefix, name = row.prefix, row.name
    db.delete(row)
    db.commit()
    write_audit(db, owner_id=user.id, action="api_key.delete", target=prefix, detail={"name": name})
    return {"ok": True}
