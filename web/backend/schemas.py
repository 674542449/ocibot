"""Pydantic request/response schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from pydantic import BaseModel, BeforeValidator, Field


def _assume_utc(value: Any) -> Any:
    """Tag naive datetimes as UTC.

    SQLite returns timezone-naive values even for DateTime(timezone=True), so these
    serialized without an offset and the SPA rendered them as local time — every
    job/attempt/run timestamp appeared shifted by the viewer's UTC offset. Every
    datetime written by this app is UTC (models use datetime.now(timezone.utc)).
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


UtcDatetime = Annotated[datetime, BeforeValidator(_assume_utc)]


# ---- Auth ----


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    # Bounded like RegisterRequest: the username becomes part of the in-memory
    # rate-limit bucket key, so an unbounded value let unauthenticated requests
    # grow that dict without limit.
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(max_length=1024)
    totp_code: str = Field(default="", max_length=16)  # required when the account has 2FA enabled


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(max_length=1024)
    new_password: str = Field(min_length=8, max_length=128)


class TotpEnableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class TotpDisableRequest(BaseModel):
    password: str = Field(max_length=1024)
    code: str = Field(min_length=6, max_length=8)


class TotpSetupOut(BaseModel):
    secret: str
    otpauth_url: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


class UserOut(BaseModel):
    id: str
    username: str
    is_admin: bool = False
    totp_enabled: bool = False
    created_at: UtcDatetime

    model_config = {"from_attributes": True}


# ---- Tenants ----


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    user_ocid: str = Field(max_length=128)
    tenancy_ocid: str = Field(max_length=128)
    fingerprint: str = Field(max_length=128)
    region: str = Field(default="ap-tokyo-1", max_length=64)
    private_key_pem: str = Field(max_length=32_000)
    compartment_ocid: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=512)
    enabled: bool = True
    color: str = Field(default="#3B82F6", max_length=32)
    budget_monthly_usd: float = 0.0


class TenantPasteImport(BaseModel):
    """Paste ~/.oci/config style text (+ optional PEM in the same blob or separate field)."""

    # Bounded: a real ~/.oci/config plus a PEM is a few KB, and the PEM scan cost
    # grows with input length.
    api_text: str = Field(min_length=1, max_length=64_000, description="OCI config / API key=value text")
    private_key_pem: str = ""
    name: str = ""
    description: str = ""
    compartment_ocid: str = ""
    test_connection: bool = False


class TenantParseResult(BaseModel):
    ok: bool
    message: str = ""
    name: str = ""
    user_ocid: str = ""
    tenancy_ocid: str = ""
    fingerprint: str = ""
    region: str = ""
    compartment_ocid: str = ""
    has_private_key: bool = False
    key_file_hint: str = ""
    warnings: list[str] = Field(default_factory=list)


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    user_ocid: Optional[str] = None
    tenancy_ocid: Optional[str] = None
    fingerprint: Optional[str] = None
    region: Optional[str] = None
    private_key_pem: Optional[str] = None  # omit to keep existing
    compartment_ocid: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    color: Optional[str] = None
    budget_monthly_usd: Optional[float] = None


class TenantOut(BaseModel):
    id: str
    name: str
    user_ocid: str
    tenancy_ocid: str
    fingerprint: str
    region: str
    compartment_ocid: str
    description: str
    enabled: bool
    color: str
    has_private_key: bool
    account_tier: str
    budget_monthly_usd: float = 0.0
    created_at: UtcDatetime
    updated_at: UtcDatetime

    model_config = {"from_attributes": True}


class OciPasswordPolicyOut(BaseModel):
    """Result of reading / mutating Oracle Identity Domain password policies."""

    ok: bool
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class TenantTestResult(BaseModel):
    ok: bool
    message: str


# ---- Instances ----


class InstanceOut(BaseModel):
    id: str
    display_name: str
    lifecycle_state: str
    shape: str
    region: str = ""
    availability_domain: str = ""
    compartment_id: str = ""
    time_created: str = ""
    ocpus: Optional[float] = None
    memory_in_gbs: Optional[float] = None
    public_ip: str = ""
    private_ip: str = ""
    ipv6_addresses: list[str] = Field(default_factory=list)
    boot_volume_size_in_gbs: Optional[int] = None
    free_tier_tag: str = ""
    tenant_id: str = ""
    tenant_name: str = ""


class PowerActionRequest(BaseModel):
    action: str  # START / STOP / SOFTSTOP / RESET / SOFTRESET / ...


class PowerActionResult(BaseModel):
    ok: bool
    message: str
    work_request_id: str = ""


class TerminateRequest(BaseModel):
    preserve_boot_volume: bool = False


class RenameRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)


class LaunchInstanceRequest(BaseModel):
    display_name: str = "instance"
    availability_domain: str = ""
    shape: str
    image_id: str
    subnet_id: str = ""
    compartment_id: str = ""
    auth_mode: str = "key"  # key | password
    ssh_public_key: str = ""
    root_password: str = ""
    ocpus: Optional[float] = None
    memory_in_gbs: Optional[float] = None
    boot_volume_size_in_gbs: Optional[int] = None
    boot_volume_vpus_per_gb: int = 10
    assign_public_ip: bool = True
    assign_ipv6_ip: bool = False
    open_guest_firewall: bool = True
    # Optional first-boot shell script merged into cloud-init (never persisted
    # in plaintext; encrypted on the job row for capacity retries).
    user_data: str = ""
    as_retry: bool = False
    retry_all_ads: bool = False
    retry_interval_sec: int = 180
    retry_max_attempts: int = 200
    # Flex-only downgrade candidates tried after the primary config fails
    # across all ADs: [{"ocpus": 2, "memory_in_gbs": 12}, ...] (max 5).
    fallback_configs: list[dict[str, Any]] = Field(default_factory=list)


class LaunchInstanceResult(BaseModel):
    ok: bool
    message: str
    work_request_id: str = ""
    instance_id: str = ""
    capacity_job_id: str = ""
    root_password: str = ""  # only returned once for password mode
    data: dict[str, Any] = Field(default_factory=dict)


class ShapeConfigRequest(BaseModel):
    ocpus: float
    memory_in_gbs: float


class MetricsRequest(BaseModel):
    hours: int = 3


# ---- Jobs ----


class CapacityJobCreate(BaseModel):
    tenant_id: str
    name: str = "容量重试"
    launch_payload: dict[str, Any]
    availability_domains: list[str] = Field(default_factory=list)
    interval_sec: int = 180
    max_attempts: int = 200
    enabled: bool = True
    # Flex-only downgrade candidates the worker rotates through after the primary
    # config fails across all ADs: [{"ocpus": 2, "memory_in_gbs": 12}, ...] (max 5).
    fallback_configs: list[dict[str, Any]] = Field(default_factory=list)


class CapacityJobOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    enabled: bool
    status: str
    interval_sec: int
    max_attempts: int
    attempts: int
    last_error: str
    last_attempt_at: Optional[UtcDatetime] = None
    next_run_at: Optional[UtcDatetime] = None
    cooldown_until: Optional[UtcDatetime] = None
    consecutive_rate_limits: int
    success_instance_id: str
    created_at: UtcDatetime
    updated_at: UtcDatetime
    launch_payload: dict[str, Any] = Field(default_factory=dict)
    fallback_configs: list[dict[str, Any]] = Field(default_factory=list)
    has_user_data: bool = False

    model_config = {"from_attributes": True}


class CapacityAttemptOut(BaseModel):
    id: str
    job_id: str
    n: int
    seq: int
    ok: bool
    capacity: bool
    rate_limited: bool
    message: str
    availability_domain: str
    config_label: str
    created_at: UtcDatetime

    model_config = {"from_attributes": True}


class ScheduleRunOut(BaseModel):
    id: str
    job_id: str
    job_name: str
    action: str
    ok: bool
    instance_count: int
    success_count: int
    message: str
    created_at: UtcDatetime

    model_config = {"from_attributes": True}


class ScheduleJobCreate(BaseModel):
    tenant_id: str
    name: str = Field(max_length=128)
    kind: str = "weekly"  # weekly | once
    time_of_day: str = "22:00"
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    run_at: Optional[datetime] = None  # required when kind == "once"
    action: str = "SOFTSTOP"
    # Bounded: the worker fires these serially, one OCI call each.
    instance_ids: list[str] = Field(default_factory=list, max_length=200)
    enabled: bool = True


class ScheduleJobOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    enabled: bool
    kind: str = "weekly"
    time_of_day: str
    weekdays: list[Any]
    run_at: Optional[UtcDatetime] = None
    action: str
    instance_ids: list[Any]
    last_run_date: str
    created_at: UtcDatetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    message: str


class HealthOut(BaseModel):
    status: str
    version: str
    app: str
