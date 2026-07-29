"""Web backend settings (API + worker)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "OCIBot Web"
    # MUST be bumped in the same commit as any shipped change, together with a new
    # CHANGELOG.md heading — /api/health is how an operator verifies a deploy
    # actually landed. tests/test_version_bump.py enforces that the two agree.
    app_version: str = "0.4.28"
    debug: bool = False

    # sqlite+pysqlite:////absolute/path.db  or  postgresql+psycopg://user:pass@host/db
    database_url: str = Field(
        default="sqlite+pysqlite:///./web_data/ocibot_web.db",
        alias="DATABASE_URL",
    )

    # Fernet key material — long random string. Required in production.
    master_key: str = Field(default="dev-only-change-me-ocibot-web-master-key", alias="OCIBOT_MASTER_KEY")

    jwt_secret: str = Field(default="dev-only-jwt-secret-change-me", alias="OCIBOT_JWT_SECRET")
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=60 * 12, alias="OCIBOT_JWT_EXPIRE_MINUTES")  # 12h default

    # Auth cookie flags. Set OCIBOT_COOKIE_SECURE=1 behind HTTPS so the JWT
    # cookie is only sent over TLS. SameSite=none (cross-site) requires secure.
    cookie_secure: bool = Field(default=False, alias="OCIBOT_COOKIE_SECURE")
    cookie_samesite: str = Field(default="lax", alias="OCIBOT_COOKIE_SAMESITE")  # lax|strict|none

    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080",
        alias="OCIBOT_CORS_ORIGINS",
    )

    # Worker
    worker_poll_sec: float = Field(default=5.0, alias="OCIBOT_WORKER_POLL_SEC")
    worker_id: str = Field(default="worker-1", alias="OCIBOT_WORKER_ID")

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

    # Reject requests when still using built-in dev secrets (optional hard fail).
    require_secure_secrets: bool = Field(default=False, alias="OCIBOT_REQUIRE_SECURE_SECRETS")

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


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.require_secure_secrets:
        if settings.master_key in _INSECURE_DEFAULTS or settings.jwt_secret in _INSECURE_DEFAULTS:
            raise RuntimeError(
                "OCIBOT_REQUIRE_SECURE_SECRETS=1 but OCIBOT_MASTER_KEY / OCIBOT_JWT_SECRET "
                "still use insecure defaults. Generate long random secrets before starting."
            )
        if len(settings.master_key) < _MIN_SECRET_LEN or len(settings.jwt_secret) < _MIN_SECRET_LEN:
            raise RuntimeError(
                f"OCIBOT_MASTER_KEY and OCIBOT_JWT_SECRET must be at least {_MIN_SECRET_LEN} characters"
            )
    return settings
