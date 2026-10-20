"""AppMeta key-value helpers (worker heartbeat, runtime settings)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from web.backend.models import AppMeta

# Well-known keys
KEY_WORKER_HEARTBEAT = "worker_heartbeat"  # ISO timestamp, updated by worker every tick
KEY_WORKER_ID = "worker_heartbeat_worker_id"
KEY_OPEN_REGISTRATION = "allow_open_registration"  # "true"/"false"; overrides env when set
# IP 封禁规则（ip_ban.py），每行一个；没设置过时用 ip_ban 里的默认值。
KEY_BAN_USER_AGENTS = "ip_ban_user_agents"
KEY_BAN_USERNAMES = "ip_ban_usernames"
KEY_BAN_EMPTY_UA = "ip_ban_empty_user_agent"  # "true"/"false"；没设置过时默认开启
# 登录锁屏密码的哈希（lock_screen.py）；空串 / 没设置 = 锁屏未开启。
KEY_LOCK_PASSWORD_HASH = "lock_screen_password_hash"


def get_meta(db: Session, key: str) -> Optional[str]:
    row = db.scalar(select(AppMeta).where(AppMeta.key == key))
    return row.value if row is not None else None


def set_meta(db: Session, key: str, value: str) -> None:
    row = db.scalar(select(AppMeta).where(AppMeta.key == key))
    if row is None:
        row = AppMeta(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.flush()
