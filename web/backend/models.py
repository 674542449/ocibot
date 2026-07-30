"""ORM models for OCIBot Web."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from web.backend.db import Base


def _uuid() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Bumped on password change / "logout everywhere"; JWTs embed the version.
    token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Fernet-encrypted TOTP secret (pending until totp_enabled=True).
    totp_secret_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    tenants: Mapped[list["Tenant"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    user_ocid: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tenancy_ocid: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    region: Mapped[str] = mapped_column(String(64), nullable=False, default="ap-tokyo-1")
    compartment_ocid: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # Set on a 副区 (secondary-region) row: the id of the primary tenant whose
    # credentials it reuses. Empty on primary rows. A secondary region is modelled
    # as its own tenant row rather than a per-request region override so that every
    # existing per-tenant page (instances, storage, WebSSH, jobs, quota) works there
    # unchanged — an OCI session is bound to exactly one region.
    parent_tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    color: Mapped[str] = mapped_column(String(32), default="#3B82F6")
    private_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Legacy local password-reminder columns (unused by UI/API; kept for DB compatibility).
    password_changed_at: Mapped[str] = mapped_column(String(64), default="")
    password_expiry_days: Mapped[int] = mapped_column(Integer, default=0)
    account_tier: Mapped[str] = mapped_column(String(16), default="")  # paid|free|""
    # Hard-enforce the Always-Free caps for this tenant, regardless of whether Oracle
    # reports the account as paid. Defaults ON: an upgraded/PAYG account that only
    # wants free resources is the common case, and inferring intent from account_tier
    # meant a paid account got a mere warning while exceeding the free tier.
    # Turn it off per tenant to deliberately use billable resources.
    free_only_mode: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Legacy: last local password-expiry notify day (unused).
    pwd_expiry_notified_on: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    owner: Mapped["User"] = relationship(back_populates="tenants")


class CapacityJob(Base):
    """DB-backed capacity retry (LaunchInstance loop)."""

    __tablename__ = "capacity_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="容量重试")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # idle | running | success | stopped | failed
    status: Mapped[str] = mapped_column(String(32), default="idle", index=True)
    launch_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    availability_domains: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # Optional downgrade candidates for Flex shapes: [{"ocpus": 2, "memory_in_gbs": 12}, ...]
    # The primary config from launch_payload is always tried first.
    fallback_configs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # Fernet-encrypted custom cloud-init script (may contain secrets, so it is
    # never stored inside launch_payload). Decrypted only at launch time.
    user_data_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    interval_sec: Mapped[int] = mapped_column(Integer, default=180)
    max_attempts: Mapped[int] = mapped_column(Integer, default=200)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    cooldown_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_rate_limits: Mapped[int] = mapped_column(Integer, default=0)
    success_instance_id: Mapped[str] = mapped_column(String(128), default="")
    locked_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(256), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class CapacityAttempt(Base):
    """Per-attempt log line for a capacity retry job (live log in the UI)."""

    __tablename__ = "capacity_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("capacity_jobs.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    n: Mapped[int] = mapped_column(Integer, default=0)  # attempt number (1-based)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    capacity: Mapped[bool] = mapped_column(Boolean, default=False)  # Out of capacity
    rate_limited: Mapped[bool] = mapped_column(Boolean, default=False)  # 429
    message: Mapped[str] = mapped_column(Text, default="")
    availability_domain: Mapped[str] = mapped_column(String(128), default="")
    config_label: Mapped[str] = mapped_column(String(64), default="")  # e.g. "4C/24G"
    # Monotonic per-job sequence for incremental polling (?after_seq=).
    seq: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class NotificationChannel(Base):
    """Per-user push channel. Secrets (tokens/SMTP password) are Fernet-encrypted."""

    __tablename__ = "notification_channels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # telegram | bark | serverchan | webhook | smtp
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Encrypted JSON dict of channel-specific config (bot_token/chat_id/url/...).
    config_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Event switches. Only capacity results are pushed since 0.4.36; rows created
    # earlier may still list removed event names, which are simply never matched.
    events: Mapped[list[Any]] = mapped_column(JSON, default=lambda: ["capacity"])
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class SshHostKey(Base):
    """Remembered SSH host key fingerprint per instance (trust on first use).

    Keyed by instance OCID rather than IP on purpose: an instance's public IP
    changes routinely (ephemeral IP replacement, stop/start), and keying on the
    address would make every rotation look like an attack. The OS identity — and
    therefore the host key — survives those changes.
    """

    __tablename__ = "ssh_host_keys"
    __table_args__ = (
        UniqueConstraint("owner_id", "instance_id", "port", name="uq_ssh_host_key_target"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[str] = mapped_column(String(36), default="")
    instance_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    port: Mapped[int] = mapped_column(Integer, default=22, nullable=False)
    # "SHA256:..." as reported by asyncssh / ssh-keygen -lf
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    key_type: Mapped[str] = mapped_column(String(64), default="")
    # Last address this key was seen on — informational only, never used for matching.
    last_host: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class AppMeta(Base):
    """Simple key-value for bootstrap flags."""

    __tablename__ = "app_meta"
    __table_args__ = (UniqueConstraint("key", name="uq_app_meta_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, default="")
