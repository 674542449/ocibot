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
    app_version: str = "0.1.0"
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

    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080",
        alias="OCIBOT_CORS_ORIGINS",
    )

    # Worker
    worker_poll_sec: float = Field(default=5.0, alias="OCIBOT_WORKER_POLL_SEC")
    worker_id: str = Field(default="worker-1", alias="OCIBOT_WORKER_ID")

    # Bootstrap: if no users exist, first register is allowed without invite.
    # After first user exists, set OCIBOT_ALLOW_OPEN_REGISTRATION=false in production.
    allow_open_registration: bool = Field(default=True, alias="OCIBOT_ALLOW_OPEN_REGISTRATION")

    # Reject requests when still using built-in dev secrets (optional hard fail).
    require_secure_secrets: bool = Field(default=False, alias="OCIBOT_REQUIRE_SECURE_SECRETS")

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


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
        if len(settings.master_key) < 24 or len(settings.jwt_secret) < 24:
            raise RuntimeError("OCIBOT_MASTER_KEY and OCIBOT_JWT_SECRET must be at least 24 characters")
    return settings
