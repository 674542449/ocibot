"""Web backend settings (API + worker)."""

from __future__ import annotations

from functools import lru_cache
from typing import ClassVar

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 每个字段都必须写出 alias。pydantic-settings 在没有 alias 时会回落到裸字段名，
    # 于是 CI 镜像或 PaaS 运行时里一个通用的 APP_NAME / DEBUG 就能改掉面板配置。
    # tests/test_app_config_security.py 会遍历所有字段，漏写 alias 直接测试失败。
    app_name: str = Field(default="OCIBot Web", alias="OCIBOT_APP_NAME")

    # MUST be bumped in the same commit as any shipped change, together with a new
    # CHANGELOG.md heading — /api/health is how an operator verifies a deploy
    # actually landed. tests/test_version_bump.py enforces that the two agree.
    #
    # 刻意用 ClassVar 而不是字段：版本号只能来自代码。它曾经是普通字段，于是环境里
    # 随便一个 APP_VERSION 就会让 /api/health 报出与实际运行代码无关的版本 —— 而
    # /api/health 正是操作员确认"更新有没有装上"的唯一手段（README 排障表第一行），
    # test_version_bump.py 也看不见这种偏差。ClassVar 不参与 pydantic 解析，任何环境
    # 变量都改不动它。
    app_version: ClassVar[str] = "0.4.100"

    debug: bool = Field(default=False, alias="OCIBOT_DEBUG")

    # sqlite+pysqlite:////absolute/path.db  or  postgresql+psycopg://user:pass@host/db
    database_url: str = Field(
        default="sqlite+pysqlite:///./web_data/ocibot_web.db",
        alias="DATABASE_URL",
    )

    # Fernet key material — long random string. Required in production.
    master_key: str = Field(default="dev-only-change-me-ocibot-web-master-key", alias="OCIBOT_MASTER_KEY")

    jwt_secret: str = Field(default="dev-only-jwt-secret-change-me", alias="OCIBOT_JWT_SECRET")

    # 同样刻意做成 ClassVar：签名算法不可由环境改写。它进 jwt.encode()，也进
    # jwt.decode(algorithms=[...])，是算法混淆类攻击唯一的着力点；AUDIT.md 第 10 轮
    # 记的"HS256 hardcoded (not env-overridable)"就是后来者会依赖的性质，而当时它其实
    # 能被裸 JWT_ALGORITHM 改掉。没有互操作需求，配置面不该存在。
    jwt_algorithm: ClassVar[str] = "HS256"
    jwt_expire_minutes: int = Field(default=60 * 12, alias="OCIBOT_JWT_EXPIRE_MINUTES")  # 12h default

    # Auth cookie flags. Set OCIBOT_COOKIE_SECURE=1 behind HTTPS so the JWT
    # cookie is only sent over TLS. SameSite=none (cross-site) requires secure.
    cookie_secure: bool = Field(default=False, alias="OCIBOT_COOKIE_SECURE")
    cookie_samesite: str = Field(default="lax", alias="OCIBOT_COOKIE_SAMESITE")  # lax|strict|none

    # Reject state-changing requests whose Origin is not this host (see
    # origin_guard.py). Escape hatch only: a reverse proxy that rewrites Host to
    # the upstream name without sending X-Forwarded-Host would make every POST
    # fail, and an operator locked out of their own panel needs a way back in.
    # Add the public origin to OCIBOT_CORS_ORIGINS rather than leaving this off.
    origin_check: bool = Field(default=True, alias="OCIBOT_ORIGIN_CHECK")

    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080",
        alias="OCIBOT_CORS_ORIGINS",
    )

    # Audit log retention. Failed logins are recorded, and an unauthenticated
    # attacker chooses how many of those happen — so the table needs a ceiling or
    # a credential-stuffing run becomes unbounded disk growth. Both limits apply;
    # 0 disables that one. Pruning runs in the worker's heartbeat (database only,
    # never Oracle), so it also happens with OCIBOT_WORKER_BACKGROUND_OCI=0.
    audit_retention_days: int = Field(default=180, alias="OCIBOT_AUDIT_RETENTION_DAYS")
    audit_max_rows: int = Field(default=50_000, alias="OCIBOT_AUDIT_MAX_ROWS")

    # Worker
    worker_poll_sec: float = Field(default=5.0, alias="OCIBOT_WORKER_POLL_SEC")
    worker_id: str = Field(default="worker-1", alias="OCIBOT_WORKER_ID")

    # Master switch for the worker's background Oracle calls. Since 0.4.36 that is
    # capacity retry alone, and it only runs while a job exists — so leaving this on
    # costs nothing until you create one. Set to 0 to guarantee the panel touches OCI
    # only while somebody is using it.
    #
    # Turning it off does NOT hide capacity retry in the UI — it stops it EXECUTING.
    # The panel says so on the task page and in the sidebar, because a job that
    # silently never fires is worse to diagnose than one that errors.
    worker_background_oci: bool = Field(default=True, alias="OCIBOT_WORKER_BACKGROUND_OCI")

    # PostgreSQL connection pool (ignored for SQLite)
    db_pool_size: int = Field(default=10, alias="OCIBOT_DB_POOL_SIZE")
    db_max_overflow: int = Field(default=20, alias="OCIBOT_DB_MAX_OVERFLOW")
    db_pool_recycle: int = Field(default=1800, alias="OCIBOT_DB_POOL_RECYCLE")

    # API process workers (Docker / production entrypoint). 1 is safest with
    # in-process WebSSH session counters; 2+ improves HTTP throughput.
    api_workers: int = Field(default=2, alias="OCIBOT_API_WORKERS")

    # Secure default: only the FIRST user may self-register (becomes admin).
    # After that, registration is closed unless an admin re-opens it (env here or
    # the DB override at /api/admin/settings, which wins when set).
    allow_open_registration: bool = Field(default=False, alias="OCIBOT_ALLOW_OPEN_REGISTRATION")

    # Trust X-Forwarded-For / X-Real-IP for rate-limit client IP. Enable only when
    # the panel sits behind a reverse proxy that overwrites these headers.
    trust_proxy: bool = Field(default=False, alias="OCIBOT_TRUST_PROXY")

    # 默认 fail closed：内置默认密钥就写在公开仓库里，而主密钥只经一次无盐 SHA-256
    # 就成为 Fernet 密钥 —— 用默认值起面板，等于任何拿到数据库的人都能解出全部 OCI
    # 私钥，默认 JWT 密钥还能让人自己签发管理员会话。此前默认 0，照 docker-compose
    # 的 quick start 一路 up 起来的面板就是这种状态，且 HTTP 层毫无提示。
    # 关成 0 仍是保留的应急出口（见 insecure_secret_error() 给出的提示）。
    require_secure_secrets: bool = Field(default=True, alias="OCIBOT_REQUIRE_SECURE_SECRETS")

    def cors_origin_list(self) -> list[str]:
        """Exact browser origins allowed by CORS.

        A literal "*" is dropped: the auth cookie is sent with credentials, and
        Starlette answers wildcard+credentials by *reflecting* the caller's Origin
        together with Access-Control-Allow-Credentials: true — i.e. any website
        could read this API as the logged-in user. Callers that really want an
        open API must list the origins explicitly.
        """
        origins: list[str] = []
        for raw in self.cors_origins.split(","):
            origin = raw.strip()
            if not origin or origin == "*":
                continue
            origins.append(origin)
        return origins

    def cors_wildcard_requested(self) -> bool:
        return any(o.strip() == "*" for o in self.cors_origins.split(","))

    def weak_secret_reasons(self) -> list[str]:
        """Human-readable reasons the configured secrets are not production-grade."""
        reasons: list[str] = []
        if self.master_key in _INSECURE_DEFAULTS:
            reasons.append("OCIBOT_MASTER_KEY 仍是内置默认值")
        elif len(self.master_key) < _MIN_SECRET_LEN:
            reasons.append(f"OCIBOT_MASTER_KEY 短于 {_MIN_SECRET_LEN} 字符")
        if self.jwt_secret in _INSECURE_DEFAULTS:
            reasons.append("OCIBOT_JWT_SECRET 仍是内置默认值")
        elif len(self.jwt_secret) < _MIN_SECRET_LEN:
            reasons.append(f"OCIBOT_JWT_SECRET 短于 {_MIN_SECRET_LEN} 字符")
        return reasons

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


_MIN_SECRET_LEN = 24

_INSECURE_DEFAULTS = {
    "dev-only-change-me-ocibot-web-master-key",
    "dev-only-jwt-secret-change-me",
    "dev-only-change-me-ocibot-web-master-key-please-rotate",
    "dev-only-jwt-secret-change-me-please-rotate",
}


class InsecureSecretsError(RuntimeError):
    """启动被密钥检查拦下。

    是 RuntimeError 的子类，这样既有的 `except RuntimeError` 不会因为换了类型而漏接；
    单独立一个类是为了让 main.py 只捕获这一种失败（其它启动异常仍应原样炸出来）。
    """


def insecure_secret_error(settings: Settings) -> str | None:
    """返回一段可直接照做的错误文本；配置没问题时返回 None。

    为什么要把文案写这么长：这条默认值从 0 改成 1 之后，一台按老文档手工部署、
    一直跑在默认密钥上的面板会在下次重启时起不来。那一刻操作员唯一能看到的东西就是
    这段话，所以它必须自己说清楚 —— 是哪个变量、怎么生成新值、以及怎样先恢复服务。

    中英各写一遍、命令单独占行：这段话会出现在容器日志和 Windows 控制台里，那些地方
    的编码不一定是 UTF-8。真被显示成乱码时，纯 ASCII 的变量名和命令行仍然认得出来。
    """
    if not settings.require_secure_secrets:
        return None

    problems: list[str] = []
    for name, value in (
        ("OCIBOT_MASTER_KEY", settings.master_key),
        ("OCIBOT_JWT_SECRET", settings.jwt_secret),
    ):
        if value in _INSECURE_DEFAULTS:
            problems.append(f"  - {name}: still the built-in default 仍是仓库里公开的内置默认值")
        elif len(value) < _MIN_SECRET_LEN:
            problems.append(
                f"  - {name}: only {len(value)} chars, need >= {_MIN_SECRET_LEN} "
                f"（只有 {len(value)} 个字符，至少需要 {_MIN_SECRET_LEN} 个）"
            )
    if not problems:
        return None

    return "\n".join(
        [
            "OCIBot refuses to start: insecure secrets. OCIBot 拒绝启动：密钥仍是不安全的配置。",
            *problems,
            "",
            "主密钥经一次 SHA-256 派生出 Fernet 密钥，用公开的默认值等于把库里所有 OCI",
            "私钥明文交给任何读到数据库的人；默认 JWT 密钥则允许任何人自行签发管理员会话。",
            "",
            "FIX 修复 — 在 web/.env 里写入两个各不相同的随机值，然后重启：",
            '    echo "OCIBOT_MASTER_KEY=$(openssl rand -hex 48)" >> web/.env',
            '    echo "OCIBOT_JWT_SECRET=$(openssl rand -hex 48)" >> web/.env',
            "    docker compose up -d",
            "（同名旧行要删掉；没有 openssl 时可用 python -c \"import secrets;print(secrets.token_hex(48))\"）",
            "",
            "WARNING 注意：更换 OCIBOT_MASTER_KEY 会让已加密的 OCI 私钥无法解密 —— 需要重新",
            "导入租户，或用改密钥之前导出的备份 ZIP 恢复。",
            "",
            "ESCAPE HATCH 应急出口 — 只想先把面板拉起来、暂不换密钥（跳过本检查，继续使用",
            "不安全的密钥，仅限本机或隔离网络）：",
            "    OCIBOT_REQUIRE_SECURE_SECRETS=0",
        ]
    )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    problem = insecure_secret_error(settings)
    if problem:
        raise InsecureSecretsError(problem)
    return settings
