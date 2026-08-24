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

from web.backend.config import InsecureSecretsError, get_settings

log = logging.getLogger("ocibot.web")

# 密钥预检必须跑在 db / routers 之前：db.py 在 import 时就建引擎并调用
# get_settings()，所以配置不合格时异常会从一条 import 语句里冒出来，操作员看到的是
# 一段与原因无关的 traceback。而 run.py 以 workers=2 起 uvicorn，启动异常发生在子
# 进程里，父进程会不停把它拉起来 —— 变成滚屏的重启循环。这里先把失败接住，改为启动
# 一个"只会解释原因"的应用（见 _fail_closed_app）。
_STARTUP_BLOCK: str | None = None
try:
    get_settings()
except InsecureSecretsError as exc:  # 只接这一种；其它启动错误仍应原样抛出
    _STARTUP_BLOCK = str(exc)

if _STARTUP_BLOCK is None:
    from web.backend.body_limit import BodySizeLimitMiddleware
    from web.backend.origin_guard import UNSAFE_METHODS, origin_allowed
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
    """Loudly warn about weak/default secrets.

    自 OCIBOT_REQUIRE_SECURE_SECRETS 默认改为 1 起，还能走到这里只有一种情况：操作员
    显式写了 =0 把检查关掉了。所以这条警告不再是"你可能忘了配"，而是"你正在明知故犯"，
    措辞按后者写 —— 面板每次启动都得再说一遍。
    """
    settings = get_settings()
    reasons = settings.weak_secret_reasons()
    if reasons:
        log.warning(
            "SECURITY: %s（OCIBOT_REQUIRE_SECURE_SECRETS=0 让启动检查被跳过了）。"
            "The master key is stretched with a single SHA-256 into the "
            "Fernet key, so a default/short value means anyone who reads the database can "
            "decrypt every stored OCI private key, and the default JWT secret lets anyone "
            "mint an admin session. Set long random values (>=24 chars, openssl rand -hex 48) "
            "and remove OCIBOT_REQUIRE_SECURE_SECRETS=0 before exposing this panel on a network. "
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


def _fail_closed_app(message: str) -> FastAPI:
    """密钥不合格时启动的替身应用：进程活着，但除了这段说明什么都不提供。

    比让进程崩掉更有用的原因：崩溃发生在 uvicorn 的工作子进程里，父进程会无限重启，
    操作员翻日志翻到的是同一段 traceback 反复出现；而这里进程稳定存活，日志里只出现
    一次原因，`curl /api/health`（以及容器 healthcheck）也会直接把修复步骤打回来。
    仍然是 fail closed：数据库、鉴权、加密一个都没初始化，没有挂载任何业务路由，
    任何请求只可能拿到 503。
    """
    log.error("%s", message)
    app = FastAPI(
        title="OCIBot（配置错误）",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.api_route(
        "/{_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    def blocked(_path: str) -> Response:
        return JSONResponse(
            {"status": "config_error", "detail": message},
            status_code=503,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    return app


def create_app() -> FastAPI:
    if _STARTUP_BLOCK is not None:
        return _fail_closed_app(_STARTUP_BLOCK)

    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        # 交互式文档默认关闭。/openapi.json 未鉴权就返回全部路由与请求/响应 schema，
        # 等于在登录之前把 admin、自更新这些入口的地图交出去。开发时用 OCIBOT_DEBUG=1
        # 打开，免得调接口时无从下手。
        openapi_url="/openapi.json" if settings.debug else None,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
    )
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

    # Registered before security_headers so it ends up *inside* it: a rejected
    # request still gets the hardening headers on its 403.
    @app.middleware("http")
    async def csrf_origin_guard(request, call_next):
        """Reject cross-site state-changing requests.

        The auth cookie alone used to authorize these. See origin_guard.py for why
        SameSite is not enough on its own.
        """
        if settings.origin_check and request.method in UNSAFE_METHODS:
            origin = request.headers.get("origin") or ""
            if not origin_allowed(
                origin,
                host=request.headers.get("host") or "",
                forwarded_host=request.headers.get("x-forwarded-host") or "",
            ):
                log.warning(
                    "rejected cross-site %s %s: origin=%r host=%r x-forwarded-host=%r",
                    request.method,
                    request.url.path,
                    origin,
                    request.headers.get("host"),
                    request.headers.get("x-forwarded-host"),
                )
                # Say what to do about it: if a reverse proxy rewrote Host this
                # looks like the whole panel breaking, and the fix is not guessable.
                return JSONResponse(
                    {
                        "detail": (
                            "跨站请求被拒绝：请求的 Origin 与本站不一致。若这是反向代理配置"
                            "问题，请把面板的公开地址（如 https://panel.example.com）加入 "
                            "OCIBOT_CORS_ORIGINS；应急可设 OCIBOT_ORIGIN_CHECK=0。"
                        )
                    },
                    status_code=403,
                )
        return await call_next(request)

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
            # 只允许同源连接。裸 ws:/wss: 曾经在这里，语义是"任意主机的 WebSocket"，
            # 于是这条策略里最值钱的那道外发防线（default-src 'self' 挡住的数据外传）
            # 被自己打开了一个口子：一段注入脚本或一个被投毒的前端依赖，就能把 DOM、
            # 会话状态和 WebSSH 终端输出源源不断送去 wss://attacker.example。
            # CSP3 里 'self' 本来就覆盖同源的 ws://wss://（现代浏览器均已实现），
            # 前端 client.ts::wsUrl() 也只用 location.host 拼同源地址，所以 WebSSH 不受影响。
            "connect-src 'self'; "
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

    # Largest request body accepted anywhere. The biggest legitimate payload is a
    # 20MB backup ZIP. Enforced on bytes actually received, not just on a declared
    # Content-Length, so a chunked body cannot stream unbounded data to the disk
    # spool before any route-level check runs.
    #
    # 位置很重要，必须是最后一个 add_middleware：add_middleware 是往队首插入，
    # 最后注册的那个才在最外层。这个上限要包住其他所有中间件，否则将来任何一个会读
    # 请求体的中间件（请求日志、HMAC 校验、WAF 垫片）都会落在上限外面，先把整个 body
    # 读进内存/磁盘之后才轮到这里数字节 —— 上限就形同虚设。
    # 新增中间件请加在这一行【之前】。
    #
    # 代价是 413 不再经过上面的 security_headers，所以那几个必要的响应头改由
    # body_limit._send_413 自己带上（AUDIT 第 10 轮核对过 413 带安全头，别让它退化）。
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=32 * 1024 * 1024)

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
