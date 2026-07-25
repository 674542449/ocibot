"""System status: worker heartbeat / liveness."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from web.backend.auth import get_current_user
from web.backend.config import get_settings
from web.backend.db import get_db
from web.backend.meta import KEY_WORKER_HEARTBEAT, KEY_WORKER_ID, get_meta
from web.backend.models import User

router = APIRouter(prefix="/system", tags=["system"])

# Worker is considered offline when the heartbeat is older than this many
# multiples of its poll interval (with a floor for very fast polls).
_STALE_MULTIPLIER = 6
_STALE_FLOOR_SEC = 60


@router.get("/status")
def system_status(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    _ = user
    settings = get_settings()
    raw = get_meta(db, KEY_WORKER_HEARTBEAT) or ""
    worker_id = get_meta(db, KEY_WORKER_ID) or ""
    now = datetime.now(timezone.utc)
    heartbeat_age: float | None = None
    alive = False
    stale_after = max(_STALE_FLOOR_SEC, int(settings.worker_poll_sec * _STALE_MULTIPLIER))
    if raw:
        try:
            hb = datetime.fromisoformat(raw)
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            heartbeat_age = max(0.0, (now - hb).total_seconds())
            alive = heartbeat_age <= stale_after
        except ValueError:
            pass
    import os

    git_sha = (os.environ.get("OCIBOT_GIT_SHA") or "").strip() or "unknown"
    return {
        "worker_alive": alive,
        "worker_id": worker_id,
        "heartbeat_age_sec": heartbeat_age,
        "stale_after_sec": stale_after,
        "server_time": now.isoformat(),
        "app_version": settings.app_version,
        "git_sha": git_sha,
    }
