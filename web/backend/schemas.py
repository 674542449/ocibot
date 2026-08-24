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


class LockedTenantRequest(BaseModel):
    """Empty string clears the default."""

    tenant_id: str = Field(default="", max_length=36)


class UserOut(BaseModel):
    id: str
    username: str
    is_admin: bool = False
    totp_enabled: bool = False
    locked_tenant_id: str = ""
    created_at: UtcDatetime

    model_config = {"from_attributes": True}


# ---- Tenants ----


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    user_ocid: str = Field(max_length=128)
    tenancy_ocid: str = Field(max_length=128)
    fingerprint: str = Field(max_length=128)
    # pattern 不是格式洁癖：region 会成为 OCI SDK 拼出的 endpoint 主机名，
    # 带上 "." / "@" / ":" 就能把请求引到任意地址（见
    # app/config_store.py::TenantConfig.validate 的详述）。这里先挡一道，
    # 让它是干净的 422 而不是落到业务层。
    region: str = Field(
        default="ap-tokyo-1", max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$"
    )
    private_key_pem: str = Field(max_length=32_000)
    compartment_ocid: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=512)
    enabled: bool = True
    color: str = Field(default="#3B82F6", max_length=32)
    # Hard-enforce Always-Free caps for this tenant (default on).
    free_only_mode: bool = True


class TenantPasteImport(BaseModel):
    """Paste ~/.oci/config style text (+ optional PEM in the same blob or separate field)."""

    # Bounded: a real ~/.oci/config plus a PEM is a few KB, and the PEM scan cost
    # grows with input length.
    api_text: str = Field(min_length=1, max_length=64_000, description="OCI config / API key=value text")
    private_key_pem: str = Field(default="", max_length=32_000)
    # 同样对齐 models.py 的列宽：这条路径的 name/description/compartment_ocid
    # 会直接进 tenants 表，原来没有上限。
    name: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=512)
    compartment_ocid: str = Field(default="", max_length=128)
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
    # 每个字段的上限必须和 TenantCreate 一致，也必须和 models.py 里那一列的
    # String(n) 一致。原来这里全是裸 Optional[str]：SQLite 不检查 varchar 长度
    # 所以整个测试套件都是绿的，而 PostgreSQL 会在 commit 时抛
    # StringDataRightTruncation —— update_tenant 的 commit 没有包 try，
    # 于是 PATCH 一个 200 字符的 name 就是一个没有任何信息的 500。
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    user_ocid: Optional[str] = Field(default=None, max_length=128)
    tenancy_ocid: Optional[str] = Field(default=None, max_length=128)
    fingerprint: Optional[str] = Field(default=None, max_length=128)
    region: Optional[str] = Field(
        default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$"
    )
    private_key_pem: Optional[str] = Field(default=None, max_length=32_000)  # omit to keep existing
    compartment_ocid: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    enabled: Optional[bool] = None
    color: Optional[str] = Field(default=None, max_length=32)
    free_only_mode: Optional[bool] = None


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
    free_only_mode: bool = True
    # Empty on a primary tenant; the primary's id on a 副区 (secondary region) row.
    parent_tenant_id: str = ""
    region_label: str = ""
    created_at: UtcDatetime
    updated_at: UtcDatetime

    model_config = {"from_attributes": True}


# ---- Regions (副区) ----


class TenantRegionItem(BaseModel):
    region_name: str
    region_key: str = ""
    region_label: str = ""  # localized city name, e.g. 大阪
    is_home_region: bool = False
    status: str = ""
    subscribed: bool = False
    # Id of the panel tenant row that manages this region ("" = not added yet).
    tenant_id: str = ""


class TenantRegionsOut(BaseModel):
    ok: bool
    message: str = ""
    home_region: str = ""
    subscribed: list[TenantRegionItem] = Field(default_factory=list)
    available: list[TenantRegionItem] = Field(default_factory=list)


class RegionSubscribeRequest(BaseModel):
    """Subscribe the tenancy to a region and add a panel row for it.

    ``confirm`` must be true: an OCI region subscription cannot be undone.
    """

    region: str = Field(min_length=2, max_length=64)
    confirm: bool = False
    # Add the linked 副区 tenant row (leave off to only perform the subscription).
    add_tenant: bool = True


class RegionSubscribeResult(BaseModel):
    ok: bool
    message: str
    region_name: str = ""
    already_subscribed: bool = False
    tenant: Optional[TenantOut] = None


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
    # root password recorded on the instance's freeform tags at launch, when the
    # instance was created in password mode. Empty for key-mode instances.
    root_password: str = ""
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


class RootPasswordNoteRequest(BaseModel):
    """New value for the remembered root password (empty clears it).

    No minimum length and no complexity rule on purpose: this records a password
    that already exists on the machine, it does not set one. Rejecting a short
    value here would leave the panel displaying the OLD password, which is worse
    than displaying a weak one — the whole point is that the note stops lying.
    """

    root_password: str = Field(default="", max_length=255)


# ---- 抢机 / 启动参数的长度上限 ----
#
# 每个值都必须和 models.py 里对应的 String(n) 对齐。SQLite 不检查 varchar 宽度，
# 所以少一个上限，整套测试照样是绿的；PostgreSQL 会在 commit 时抛
# DataError / StringDataRightTruncation。
#
# 在抢机这条路径上，溢出的代价远不止「500 而不是 422」：
# POST /tenants/{id}/launch 带 as_retry=true 时顺序是
#   额度校验 → prepare_launch_network（**真的在 Oracle 建出一个 managed NSG**，
#   它把自己的名字截到 100 所以不会失败）→ 写 CapacityJob 行 → db.commit()。
# 那个 commit 在 routers/instances.py 里没有包 try，孤儿 NSG 的清理又只挂在 409
# 那一个分支上。于是一个 122 字符的 display_name（OCI 自己允许 255，这是完全
# 合法的输入）换来的是一个没有任何信息的 500 外加一个永远不会被删掉的 NSG，
# 而且每重试一次就再多一个。
#
# CapacityJob.name = String(128)，routers/instances.py 用
# f"容量重试 · {display_name}" 组装它 —— 前缀本身占掉 7 个字符，所以
# display_name 只剩 121 的余量。前缀写成字面量再取长度，是为了这个数字改前缀时
# 不会悄悄失配。
MAX_CAPACITY_JOB_NAME = 128
_CAPACITY_JOB_NAME_PREFIX = "容量重试 · "
MAX_LAUNCH_DISPLAY_NAME = MAX_CAPACITY_JOB_NAME - len(_CAPACITY_JOB_NAME_PREFIX)  # 121

MAX_OCID = 255
MAX_SHAPE = 64
# CapacityAttempt.availability_domain = String(128)。worker 把这个值原样写进尝试
# 日志，超宽就在 _log_attempt 的 flush 里炸 —— 而那一炸会让整个事务进入
# aborted 状态，attempts 回滚、max_attempts 永远够不着（见 worker._log_attempt）。
MAX_AVAILABILITY_DOMAIN = 128
# 一个 OCI 区域最多 3 个 AD。留余量但绝不能不限：worker 的 _attempt_plan 按
# len(ads) × len(configs) 轮询，而这个列表是整条 JSON 落库的。
MAX_AVAILABILITY_DOMAINS = 8
MAX_SSH_PUBLIC_KEY = 4096
# 和 launch_service.build_launch_request 里的同一个上限对齐，只是提前到 422。
MAX_USER_DATA = 16_000
MAX_NSG_IDS = 8

# launch_payload 是一个原样落库的 JSON 列。sanitize_launch_payload 只白名单了
# 字段**名**，没有给任何一个字段的**值**设长度上限，nsg_ids 更是一个不限长的
# 字符串列表。于是 32MB 的请求体上限成了唯一的天花板：一次 POST 就能写进一条
# 兆级的任务行，而 worker 每次尝试都要把它整条读出来。
_LAUNCH_PAYLOAD_STR_LIMITS: dict[str, int] = {
    "display_name": MAX_LAUNCH_DISPLAY_NAME,
    "compartment_id": MAX_OCID,
    "availability_domain": MAX_AVAILABILITY_DOMAIN,
    "shape": MAX_SHAPE,
    "image_id": MAX_OCID,
    "subnet_id": MAX_OCID,
    "vcn_id": MAX_OCID,
    "network_compartment_id": MAX_OCID,
    "managed_nsg_id": MAX_OCID,
    "ssh_public_key": MAX_SSH_PUBLIC_KEY,
    "auth_mode": 16,
    "launch_token": 64,
}


def _bounded_launch_payload(value: Any) -> Any:
    """Reject an oversized launch payload before it is persisted verbatim."""
    if not isinstance(value, dict):
        # 不是 dict 就交给下游 sanitize_launch_payload 去报它自己的 400，
        # 这里只负责长度。
        return value
    if len(value) > 64:
        raise ValueError("启动参数字段过多")
    for key, limit in _LAUNCH_PAYLOAD_STR_LIMITS.items():
        item = value.get(key)
        if isinstance(item, str) and len(item) > limit:
            raise ValueError(f"启动参数 {key} 过长（上限 {limit} 字符）")
    nsg_ids = value.get("nsg_ids")
    if isinstance(nsg_ids, list):
        if len(nsg_ids) > MAX_NSG_IDS:
            raise ValueError(f"nsg_ids 最多 {MAX_NSG_IDS} 个")
        for item in nsg_ids:
            if isinstance(item, str) and len(item) > MAX_OCID:
                raise ValueError(f"nsg_ids 中的 OCID 过长（上限 {MAX_OCID} 字符）")
    return value


BoundedLaunchPayload = Annotated[dict[str, Any], BeforeValidator(_bounded_launch_payload)]
BoundedAvailabilityDomain = Annotated[str, Field(max_length=MAX_AVAILABILITY_DOMAIN)]


class LaunchInstanceRequest(BaseModel):
    # Stable across retries of the SAME submission, new for a new one. Sent to
    # Oracle as opc-retry-token so a launch whose response was lost is not
    # created a second time. Optional: an older client just gets the old
    # behaviour rather than an error.
    idempotency_key: str = Field(default="", max_length=64)
    # 121 而不是 OCI 允许的 255：as_retry 时这个值要放进 CapacityJob.name
    # String(128)，前缀吃掉 7 个字符（见上面 MAX_LAUNCH_DISPLAY_NAME 的说明）。
    # 想放宽到 255，得先让 routers/instances.py 组装任务名时自己截断。
    display_name: str = Field(default="instance", max_length=MAX_LAUNCH_DISPLAY_NAME)
    availability_domain: str = Field(default="", max_length=MAX_AVAILABILITY_DOMAIN)
    shape: str = Field(max_length=MAX_SHAPE)
    image_id: str = Field(max_length=MAX_OCID)
    subnet_id: str = Field(default="", max_length=MAX_OCID)
    compartment_id: str = Field(default="", max_length=MAX_OCID)
    auth_mode: str = Field(default="key", max_length=16)  # key | password
    ssh_public_key: str = Field(default="", max_length=MAX_SSH_PUBLIC_KEY)
    root_password: str = Field(default="", max_length=255)
    ocpus: Optional[float] = None
    memory_in_gbs: Optional[float] = None
    boot_volume_size_in_gbs: Optional[int] = None
    boot_volume_vpus_per_gb: int = 10
    # How many identical instances to create in one submit. The free caps are
    # tenancy-wide totals, so the guard validates count × this config, not one.
    # Bounded because each one is a separate LaunchInstance call against a rate
    # limit that the capacity-retry loop also competes for.
    count: int = Field(default=1, ge=1, le=8)
    assign_public_ip: bool = True
    assign_ipv6_ip: bool = False
    open_guest_firewall: bool = True
    # Optional first-boot shell script merged into cloud-init (never persisted
    # in plaintext; encrypted on the job row for capacity retries).
    user_data: str = Field(default="", max_length=MAX_USER_DATA)
    as_retry: bool = False
    retry_all_ads: bool = False
    # 上下界不是洁癖：clamp_retry_interval / clamp_max_attempts 用 int(float(x))
    # 归一化，只 catch (TypeError, ValueError)。JSON 允许任意精度整数，
    # float(10**400) 抛的是 OverflowError —— 会穿过 clamp 变成一个裸 500。
    retry_interval_sec: int = Field(default=180, ge=0, le=86_400)
    retry_max_attempts: int = Field(default=200, ge=0, le=1_000_000)
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
    # One entry per instance when count > 1. The scalar fields above stay filled
    # from the FIRST successful instance so existing callers keep working.
    # Each entry: {ok, display_name, instance_id, message, root_password}.
    instances: list[dict[str, Any]] = Field(default_factory=list)
    created_count: int = 0
    requested_count: int = 1


class ShapeConfigRequest(BaseModel):
    ocpus: float
    memory_in_gbs: float


class MetricsRequest(BaseModel):
    hours: int = 3


# ---- Jobs ----


class CapacityJobCreate(BaseModel):
    # tenants.id = String(36)（uuid4）。
    tenant_id: str = Field(max_length=36)
    # CapacityJob.name = String(128)。原来是裸 str：PostgreSQL 上一个 200 字符的
    # name 会让 jobs.py 里那个没有包 try 的 db.commit() 抛 DataError，变成一个
    # 空的 500 —— 而此时 enforce_launch_quota 已经把一整轮租户枚举花在 Oracle 的
    # 速率限制上了。
    name: str = Field(default="容量重试", max_length=MAX_CAPACITY_JOB_NAME)
    launch_payload: BoundedLaunchPayload
    # 列表长度和**元素**长度都要限。元素超宽的后果比行溢出更糟：这个值原样进
    # CapacityAttempt.availability_domain String(128)，worker 的 _log_attempt
    # 一 flush 就把事务打成 aborted，attempts 跟着回滚 —— max_attempts 永远够不
    # 到，任务在租约过期后无限重发 LaunchInstance。
    availability_domains: list[BoundedAvailabilityDomain] = Field(
        default_factory=list, max_length=MAX_AVAILABILITY_DOMAINS
    )
    # 同 LaunchInstanceRequest：挡住 clamp_* 里 int(float(huge)) 的 OverflowError。
    interval_sec: int = Field(default=180, ge=0, le=86_400)
    max_attempts: int = Field(default=200, ge=0, le=1_000_000)
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


class MessageOut(BaseModel):
    message: str


class HealthOut(BaseModel):
    status: str
    version: str
    app: str
