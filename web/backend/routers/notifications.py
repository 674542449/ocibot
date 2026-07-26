"""Notification channel settings + test send."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from web.backend.auth import get_current_user
from web.backend.db import get_db
from web.backend.models import NotificationChannel, User
from web.backend.notify import (
    CHANNEL_KINDS,
    EVENT_KEYS,
    config_hint,
    decode_channel_config,
    encode_channel_config,
    send_to_channel,
    validate_channel_config,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


class ChannelCreate(BaseModel):
    kind: str
    name: str = Field(default="", max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)
    events: list[str] = Field(default_factory=lambda: list(EVENT_KEYS))
    enabled: bool = True


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[dict[str, Any]] = None  # omit to keep existing secrets
    events: Optional[list[str]] = None
    enabled: Optional[bool] = None


class ChannelOut(BaseModel):
    id: str
    kind: str
    name: str
    enabled: bool
    events: list[str]
    config_hint: str
    created_at: str


class TestResult(BaseModel):
    ok: bool
    detail: str


def _out(row: NotificationChannel) -> ChannelOut:
    return ChannelOut(
        id=row.id,
        kind=row.kind,
        name=row.name or row.kind,
        enabled=bool(row.enabled),
        events=[e for e in (row.events or []) if e in EVENT_KEYS],
        config_hint=config_hint(row.kind, decode_channel_config(row.config_encrypted)),
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def _owned(db: Session, user_id: str, channel_id: str) -> NotificationChannel:
    row = db.get(NotificationChannel, channel_id)
    if row is None or row.owner_id != user_id:
        raise HTTPException(status_code=404, detail="通知渠道不存在")
    return row


def _clean_events(events: list[str]) -> list[str]:
    # An empty selection means "no events": coercing it back to all three silently
    # re-enabled every notification the user had just switched off. Creation still
    # defaults to all three via ChannelCreate.events' default_factory.
    return [e for e in events if e in EVENT_KEYS]


@router.get("", response_model=list[ChannelOut])
def list_channels(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[ChannelOut]:
    rows = db.scalars(
        select(NotificationChannel)
        .where(NotificationChannel.owner_id == user.id)
        .order_by(NotificationChannel.created_at)
    ).all()
    return [_out(r) for r in rows]


@router.post("", response_model=ChannelOut, status_code=201)
def create_channel(
    body: ChannelCreate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ChannelOut:
    kind = (body.kind or "").strip().lower()
    if kind not in CHANNEL_KINDS:
        raise HTTPException(status_code=400, detail=f"渠道类型必须是 {', '.join(CHANNEL_KINDS)}")
    try:
        validate_channel_config(kind, body.config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = NotificationChannel(
        owner_id=user.id,
        kind=kind,
        name=(body.name or kind).strip()[:64],
        enabled=bool(body.enabled),
        config_encrypted=encode_channel_config(body.config),
        events=_clean_events(body.events),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _out(row)


@router.patch("/{channel_id}", response_model=ChannelOut)
def update_channel(
    channel_id: str,
    body: ChannelUpdate,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ChannelOut:
    row = _owned(db, user.id, channel_id)
    if body.name is not None:
        row.name = body.name.strip()[:64]
    if body.enabled is not None:
        row.enabled = bool(body.enabled)
    if body.events is not None:
        row.events = _clean_events(body.events)
    if body.config is not None:
        try:
            validate_channel_config(row.kind, body.config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        row.config_encrypted = encode_channel_config(body.config)
    db.commit()
    db.refresh(row)
    return _out(row)


@router.delete("/{channel_id}")
def delete_channel(
    channel_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    row = _owned(db, user.id, channel_id)
    db.delete(row)
    db.commit()
    return {"message": "已删除"}


@router.post("/{channel_id}/test", response_model=TestResult)
def test_channel(
    channel_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> TestResult:
    row = _owned(db, user.id, channel_id)
    config = decode_channel_config(row.config_encrypted)
    ok, detail = send_to_channel(
        row.kind,
        config,
        "OCIBot 测试通知",
        f"这是一条测试消息。渠道：{row.name or row.kind}。收到即表示配置正确。",
    )
    return TestResult(ok=ok, detail=detail)
