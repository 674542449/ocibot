"""FastAPI application entry."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Ensure repo root is on sys.path so `app.*` and `web.*` import cleanly
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from web.backend.config import get_settings
from web.backend.db import SessionLocal, init_db
from web.backend.routers import (
    admin,
    audit,
    auth,
    backup,
    instance_ops,
    instances,
    jobs,
    notifications,
    system,
    tenants,
)
from web.backend.schemas import HealthOut

log = logging.getLogger("ocibot.web")

# Built frontend (web/frontend/dist). When present, the API process serves the
# SPA directly — single-service deployment without a separate static host.
_DIST_DIR = _REPO_ROOT / "web" / "frontend" / "dist"


def _bootstrap_admin() -> None:
    """Upgraded installations: if no admin exists, promote the earliest user."""
    from sqlalchemy import select

    from web.backend.models import User

    with SessionLocal() as db:
        has_admin = db.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1))
        if has_admin:
            return
        first = db.scalar(select(User).order_by(User.created_at).limit(1))
        if first is not None:
            first.is_admin = True
            db.commit()
            log.info("bootstrap: promoted first user '%s' to admin", first.username)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    _bootstrap_admin()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router, prefix="/api")
    app.include_router(tenants.router, prefix="/api")
    app.include_router(instances.router, prefix="/api")
    app.include_router(instance_ops.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(backup.router, prefix="/api")
    app.include_router(audit.router, prefix="/api")
    app.include_router(notifications.router, prefix="/api")
    app.include_router(system.router, prefix="/api")
    app.include_router(admin.router, prefix="/api")

    @app.get("/api/health", response_model=HealthOut, tags=["system"])
    def health() -> HealthOut:
        return HealthOut(status="ok", version=settings.app_version, app=settings.app_name)

    if _DIST_DIR.is_dir() and (_DIST_DIR / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=_DIST_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            # Serve real files (favicon etc.); everything else falls back to the SPA.
            candidate = (_DIST_DIR / full_path).resolve()
            if (
                full_path
                and not full_path.startswith("api")
                and candidate.is_file()
                and _DIST_DIR.resolve() in candidate.parents
            ):
                return FileResponse(candidate)
            return FileResponse(_DIST_DIR / "index.html")

        log.info("serving frontend from %s", _DIST_DIR)

    return app


app = create_app()
