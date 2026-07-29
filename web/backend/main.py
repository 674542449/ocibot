"""FastAPI application entry."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# Ensure repo root is on sys.path so `app.*` and `web.*` import cleanly
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from web.backend.body_limit import BodySizeLimitMiddleware
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
    storage,
    system,
    tenants,
    webssh,
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


def _warn_insecure_secrets() -> None:
    """Loudly warn about weak/default secrets (not a hard fail unless
    OCIBOT_REQUIRE_SECURE_SECRETS=1, which get_settings() enforces separately)."""
    settings = get_settings()
    reasons = settings.weak_secret_reasons()
    if reasons:
        log.warning(
            "SECURITY: %s. The master key is stretched with a single SHA-256 into the "
            "Fernet key, so a short/guessable value is brute-forceable offline against a "
            "stolen database. Set long random values (>=24 chars) and "
            "OCIBOT_REQUIRE_SECURE_SECRETS=1 before exposing this panel on a network. "
            "Rotating OCIBOT_MASTER_KEY makes existing encrypted private keys undecryptable.",
            "；".join(reasons),
        )
    if settings.cors_wildcard_requested():
        log.warning(
            "SECURITY: OCIBOT_CORS_ORIGINS contains '*', which is ignored. Wildcard CORS "
            "combined with cookie credentials would let any website read this API as the "
            "logged-in user. List the real origins explicitly instead."
        )
    if not settings.cookie_secure:
        log.warning(
            "SECURITY: OCIBOT_COOKIE_SECURE=0 — the session cookie may be sent over plain "
            "HTTP. Terminate TLS in front of the panel and set it to 1."
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    _bootstrap_admin()
    _warn_insecure_secrets()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )
    # Largest request body accepted anywhere. The biggest legitimate payload is a
    # 20MB backup ZIP. Enforced on bytes actually received, not just on a declared
    # Content-Length, so a chunked body cannot stream unbounded data to the disk
    # spool before any route-level check runs.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=32 * 1024 * 1024)
    app.add_middleware(GZipMiddleware, minimum_size=500)
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
    app.include_router(storage.router, prefix="/api")
    app.include_router(webssh.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(backup.router, prefix="/api")
    app.include_router(audit.router, prefix="/api")
    app.include_router(notifications.router, prefix="/api")
    app.include_router(system.router, prefix="/api")
    app.include_router(admin.router, prefix="/api")

    @app.get("/api/health", response_model=HealthOut, tags=["system"])
    def health() -> HealthOut:
        return HealthOut(status="ok", version=settings.app_version, app=settings.app_name)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        # Baseline browser hardening for the SPA + API.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        # HSTS only makes sense once TLS is actually in front of the panel;
        # OCIBOT_COOKIE_SECURE=1 is the operator's "we are on HTTPS" signal.
        if settings.cookie_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        # CSP: SPA is same-origin; allow inline styles from Vue scoped + xterm.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "base-uri 'self'; "
            "frame-ancestors 'none'; "
            "object-src 'none'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; "
            "connect-src 'self' ws: wss:; "
            "form-action 'self'",
        )
        # Caching for the SPA bundle. Vite writes content-hashed filenames under
        # /assets, so those bytes can never change meaning — cache them for a year
        # and skip revalidation entirely. Without this StaticFiles only sends
        # etag/last-modified, so every page load spent one conditional round trip
        # per file just to be told "not modified"; on a high-latency link that is
        # most of the wait before anything renders.
        path = request.url.path
        if path.startswith("/assets/"):
            response.headers.setdefault(
                "Cache-Control", "public, max-age=31536000, immutable"
            )
        elif not path.startswith("/api/"):
            # index.html is NOT hashed and points at the current bundle, so it must
            # be revalidated. Left to heuristic caching it could keep serving the
            # previous deploy's asset names after an update — the "更新后版本不变，
            # 浏览器强刷" line in README's troubleshooting table.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    if _DIST_DIR.is_dir() and (_DIST_DIR / "index.html").is_file():
        assets_dir = _DIST_DIR / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        # Root-level static files from Vite public/ (favicon, logo, etc.)
        @app.get("/favicon.svg", include_in_schema=False)
        def favicon_svg() -> Response:
            path = _DIST_DIR / "favicon.svg"
            if path.is_file():
                return FileResponse(path, media_type="image/svg+xml")
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        @app.get("/logo.svg", include_in_schema=False)
        def logo_svg() -> Response:
            path = _DIST_DIR / "logo.svg"
            if path.is_file():
                return FileResponse(path, media_type="image/svg+xml")
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> Response:
            # Unknown API paths must return JSON 404, not the SPA shell — otherwise
            # the frontend's JSON client receives an HTML 200 for a missing route.
            if full_path.startswith("api"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            # Serve real files (favicon etc.); everything else falls back to the SPA.
            # Reject path traversal even if StaticFiles would.
            if ".." in full_path.split("/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            candidate = (_DIST_DIR / full_path).resolve()
            dist_root = _DIST_DIR.resolve()
            try:
                candidate.relative_to(dist_root)
            except ValueError:
                return FileResponse(dist_root / "index.html")
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist_root / "index.html")

        log.info("serving frontend from %s", _DIST_DIR)

    return app


app = create_app()
