"""OCI Compute client wrapper for multi-tenant instance operations."""

from __future__ import annotations

import base64
import ipaddress
import re
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# Instance freeform-tag key used to remember the root password set at launch
# (password mode only). Visible to anyone who can read the instance.
ROOT_PASSWORD_TAG = "ocibot_root_password"

# Instance freeform-tag key marking an instance as protected from termination.
# A tag rather than a panel-local column so the flag survives a panel reinstall
# or a database restore, and so it is visible in the Oracle console instead of
# being a fact only this panel knows.
TERMINATE_PROTECT_TAG = "ocibot_protected"

# Bounded parallelism for OCI network calls. The SDK clients wrap a
# requests.Session, which tolerates concurrent GETs; keep pools small so a
# large tenancy does not spawn hundreds of threads.
_IP_RESOLVE_WORKERS = 8
_COMPARTMENT_WORKERS = 5

from app.config_store import TenantConfig

try:
    import oci
    from oci.core import BlockstorageClient, ComputeClient, VirtualNetworkClient
    from oci.identity import IdentityClient
    from oci.limits import LimitsClient
    from oci.monitoring import MonitoringClient
    from oci.exceptions import ServiceError

    try:
        from oci.usage_api import UsageapiClient
    except ImportError:  # pragma: no cover
        UsageapiClient = object  # type: ignore

    try:
        from oci.object_storage import ObjectStorageClient
    except ImportError:  # pragma: no cover
        ObjectStorageClient = object  # type: ignore

    try:
        from oci.compute_instance_agent import ComputeInstanceAgentClient
    except ImportError:  # pragma: no cover
        ComputeInstanceAgentClient = object  # type: ignore

    OCI_AVAILABLE = True
except ImportError:  # pragma: no cover
    oci = None  # type: ignore
    BlockstorageClient = object  # type: ignore
    ComputeClient = object  # type: ignore
    VirtualNetworkClient = object  # type: ignore
    IdentityClient = object  # type: ignore
    LimitsClient = object  # type: ignore
    MonitoringClient = object  # type: ignore
    UsageapiClient = object  # type: ignore
    ObjectStorageClient = object  # type: ignore
    ComputeInstanceAgentClient = object  # type: ignore
    ServiceError = Exception  # type: ignore
    OCI_AVAILABLE = False


def sdk_default_retry_strategy() -> Any:
    """OCI SDK client-level strategy: short exponential backoff for 429 / 5xx / timeouts.

    Used for list/get and ordinary management calls. Not used for LaunchInstance —
    capacity retry owns that loop at the application layer.
    """
    if not OCI_AVAILABLE:
        return None
    return oci.retry.DEFAULT_RETRY_STRATEGY


def sdk_no_retry_strategy() -> Any:
    """Disable SDK-level retries (single attempt). For LaunchInstance compliance path."""
    if not OCI_AVAILABLE:
        return None
    return oci.retry.NoneRetryStrategy()


# Human-friendly action labels (formal OCI-style wording)
POWER_ACTIONS = {
    "START": "开机",
    "STOP": "强制关机",
    "SOFTSTOP": "正常关机",
    "RESET": "强制重启",
    "SOFTRESET": "正常重启",
    "SENDDIAGNOSTICINTERRUPT": "诊断中断",
    "DIAGNOSTICREBOOT": "诊断重启",
    "REBOOTMIGRATE": "重启迁移",
}

# Always Free eligible shapes (mark in UI)
FREE_TIER_SHAPES = {
    "VM.Standard.A1.Flex": "免费 ARM",
    "VM.Standard.E2.1.Micro": "免费 AMD",
}

# Service-Limit name fragments for the shapes above, used to filter the account
# page's reference quota table down to what the panel can actually launch.
# The paid families (E3/E4/E5/standard3) were listed too, but Oracle reports
# non-zero limits for those even on a free account, so they were rows of quota the
# operator has no use for.
FREE_TIER_LIMIT_TAGS = ("a1", "e2-micro")

# Oracle Always Free resource caps (tenancy-wide reference; not region-scoped).
# Source: Oracle Cloud Always Free resources documentation.
ALWAYS_FREE_LIMITS = {
    "a1_ocpu": 4.0,
    "a1_memory_gb": 24.0,
    "e2_micro_count": 2,
    "block_storage_gb": 200.0,
    "object_storage_gb": 20.0,
    # Ephemeral public IPs on free VMs are free; track usage for visibility only.
    "public_ip_soft": 2,
}

BOOT_VPU_PRESETS = [
    (10, "平衡 (10 VPUs/GB)"),
    (20, "较高性能 (20 VPUs/GB)"),
    (30, "超高性能 (30 VPUs/GB) — 可能额外计费"),
    (60, "超高性能 (60 VPUs/GB) — 可能额外计费"),
    (90, "超高性能 (90 VPUs/GB) — 可能额外计费"),
    (120, "超高性能 (120 VPUs/GB) — 可能额外计费"),
]

# One-click free-tier launch presets (shape + boot). Network uses the account default.
LAUNCH_QUICK_PRESETS: list[dict] = [
    {
        "id": "e2_micro_50",
        "label": "免费 AMD · 50G",
        "hint": "VM.Standard.E2.1.Micro · 硬盘 50GB · 性能 120",
        "shape": "VM.Standard.E2.1.Micro",
        "arch": "x86",
        "ocpus": None,
        "memory_in_gbs": None,
        "boot_volume_size_in_gbs": 50,
        "boot_volume_vpus_per_gb": 120,
    },
    {
        "id": "a1_4c24g_100",
        "label": "免费 ARM 4C24G · 100G",
        "hint": "VM.Standard.A1.Flex · 4 OCPU / 24GB · 硬盘 100GB · 性能 120",
        "shape": "VM.Standard.A1.Flex",
        "arch": "arm",
        "ocpus": 4,
        "memory_in_gbs": 24,
        "boot_volume_size_in_gbs": 100,
        "boot_volume_vpus_per_gb": 120,
    },
    {
        "id": "a1_4c24g_200",
        "label": "免费 ARM 4C24G · 200G",
        "hint": "VM.Standard.A1.Flex · 4 OCPU / 24GB · 硬盘 200GB · 性能 120",
        "shape": "VM.Standard.A1.Flex",
        "arch": "arm",
        "ocpus": 4,
        "memory_in_gbs": 24,
        "boot_volume_size_in_gbs": 200,
        "boot_volume_vpus_per_gb": 120,
    },
]

# Platform image OS families offered in the launch wizard. Values are the
# official `operating_system` filter names used by list_images.
LAUNCH_OS_FAMILIES = [
    {"id": "ubuntu", "label": "Ubuntu", "operating_system": "Canonical Ubuntu"},
    {"id": "oracle_linux", "label": "Oracle Linux", "operating_system": "Oracle Linux"},
]

# Default network stack created when a tenancy has no usable VCN/Subnet.
# Keep names professional and free of product branding. Legacy aliases are
# still recognized so existing tenancies continue to reuse the same stack.
DEFAULT_VCN_CIDR = "10.0.0.0/16"
DEFAULT_SUBNET_CIDR = "10.0.0.0/24"
DEFAULT_VCN_NAME = "default-vcn"
DEFAULT_SUBNET_NAME = "public-subnet"
DEFAULT_IGW_NAME = "internet-gateway"
DEFAULT_RT_NAME = "public-route-table"
DEFAULT_SL_NAME = "open-security-list"
DEFAULT_VCN_DNS_LABEL = "defaultvcn"
DEFAULT_INSTANCE_NAME = "instance"
# Historical names created by older builds — never create these again, but
# treat them as the default stack when scanning an existing tenancy.
LEGACY_DEFAULT_VCN_NAMES = frozenset({"ocibot-vcn", "default-vcn"})
LEGACY_DEFAULT_SUBNET_NAMES = frozenset({"ocibot-public-subnet", "public-subnet"})
LEGACY_DEFAULT_IGW_NAMES = frozenset({"ocibot-igw", "internet-gateway"})
LEGACY_DEFAULT_RT_NAMES = frozenset({"ocibot-public-rt", "public-route-table"})
LEGACY_DEFAULT_SL_NAMES = frozenset({"ocibot-open-sl", "open-security-list"})

SAFE_LAUNCH_FIELDS = {
    "display_name",
    "compartment_id",
    "availability_domain",
    "shape",
    "image_id",
    "subnet_id",
    "vcn_id",
    "network_compartment_id",
    "ssh_public_key",
    "auth_mode",
    "ocpus",
    "memory_in_gbs",
    "assign_public_ip",
    "assign_ipv6_ip",
    "boot_volume_size_in_gbs",
    "boot_volume_vpus_per_gb",
    "nsg_ids",
    "managed_nsg_id",
    "launch_token",
    "open_guest_firewall",
}


def sanitize_launch_payload(payload: dict, *, for_retry: bool = False) -> dict:
    """Return a validated launch payload that never contains credentials."""
    if not isinstance(payload, dict):
        raise ValueError("启动参数格式无效")
    forbidden = {"root_password", "password", "password_confirm", "secrets"}
    if forbidden.intersection(payload):
        raise ValueError("启动参数中包含不允许持久化的密码字段")
    if payload.get("user_data_b64"):
        raise ValueError("不允许保存自由 user-data；请重新创建任务")
    clean = {key: payload[key] for key in SAFE_LAUNCH_FIELDS if key in payload}
    auth_mode = str(clean.get("auth_mode") or "key").strip().lower()
    if auth_mode not in {"key", "password"}:
        raise ValueError("认证方式必须为 key 或 password")
    if for_retry and auth_mode != "key":
        raise ValueError("root 密码模式不支持容量自动重试")
    clean["auth_mode"] = auth_mode
    required = ("compartment_id", "availability_domain", "shape", "image_id", "subnet_id")
    missing = [name for name in required if not str(clean.get(name) or "").strip()]
    if missing:
        raise ValueError("缺少启动参数：" + ", ".join(missing))
    if auth_mode == "key":
        key = str(clean.get("ssh_public_key") or "").strip()
        # splitlines() also splits on \r, \x0b, \x0c, \x1c-\x1e, \x85, U+2028 and
        # U+2029 — a bare "\n" check let those through, and cloud-init's YAML
        # treats several of them as line breaks, so the key field could inject
        # extra cloud-config into the persisted launch payload.
        if len(key.splitlines()) > 1:
            raise ValueError("每次只能填写一条 SSH 公钥")
        if not re.match(r"^(ssh-(?:rsa|ed25519)|ecdsa-sha2-[^ ]+)\s+\S+", key):
            raise ValueError("SSH 公钥格式无效")
        clean["ssh_public_key"] = key
    boot_size = clean.get("boot_volume_size_in_gbs")
    if boot_size is not None:
        boot_size = int(boot_size)
        if not 50 <= boot_size <= 32768:
            raise ValueError("Boot Volume 大小必须在 50–32768 GB 之间")
        clean["boot_volume_size_in_gbs"] = boot_size
    vpus = int(clean.get("boot_volume_vpus_per_gb") or 10)
    if vpus not in (10, 20) and not 30 <= vpus <= 120:
        raise ValueError("Boot Volume 性能必须为 10、20 或 30–120 VPUs/GB")
    clean["boot_volume_vpus_per_gb"] = vpus
    clean["assign_public_ip"] = bool(clean.get("assign_public_ip", True))
    clean["assign_ipv6_ip"] = bool(clean.get("assign_ipv6_ip", False))
    clean["open_guest_firewall"] = bool(clean.get("open_guest_firewall", True))
    clean["nsg_ids"] = [str(value) for value in (clean.get("nsg_ids") or []) if value]
    return clean


def _is_lts_version(version: str) -> bool:
    """Ubuntu LTS releases are even-year .04 (e.g. 20.04, 22.04, 24.04)."""
    parts = str(version or "").strip().split(".")
    if len(parts) < 2:
        return False
    try:
        return int(parts[0]) % 2 == 0 and parts[1] == "04"
    except ValueError:
        return False


def _version_key(version: str) -> tuple:
    try:
        return tuple(int(p) for p in str(version).split("."))
    except ValueError:
        return (0,)


def _latest_lts_ubuntu_images(items: list[dict]) -> list[dict]:
    """Keep only the newest image per (LTS version, architecture).

    Input is TIMECREATED-descending, so the first item seen for each
    (version, arch) pair is the newest build. We keep the two most recent LTS
    versions for each of ARM64 / AMD64, i.e. the four canonical choices.
    """
    newest: dict[tuple[str, str], dict] = {}
    for item in items:
        version = str(item.get("operating_system_version", "")).strip()
        if not _is_lts_version(version):
            continue
        arch = item.get("architecture") or (
            "ARM64" if "arm" in str(item.get("display_name", "")).lower() else "AMD64"
        )
        key = (version, arch)
        if key not in newest:  # first == newest (list already DESC)
            newest[key] = item

    if not newest:
        return items  # never hide everything if version parsing failed

    # Two most recent LTS versions overall, newest first.
    versions = sorted({v for v, _ in newest}, key=_version_key, reverse=True)[:2]
    result: list[dict] = []
    for version in versions:
        for arch in ("ARM64", "AMD64"):
            item = newest.get((version, arch))
            if item:
                result.append(item)
    return result


def free_tier_tag(shape: str) -> str:
    return FREE_TIER_SHAPES.get(shape or "", "")


def _clean_retry_token(value: str) -> str:
    """Normalise an idempotency key into something OCI will accept.

    Oracle caps ``opc-retry-token`` at 64 characters and rejects anything outside
    a conservative character set. Sanitising rather than raising is deliberate:
    the token is a safety net, and refusing the whole launch because the client
    sent an odd key would turn a protection into a new failure mode. An
    unusable key degrades to "no token", i.e. exactly the old behaviour.
    """
    cleaned = "".join(ch for ch in (value or "").strip() if ch.isalnum() or ch in "-_")
    return cleaned[:64]


def derive_retry_token(base: str, index: int) -> str:
    """Per-item retry token for a batch launch, collision-free by construction.

    Naively appending "-{index}" is wrong: Oracle caps the token at 64 characters,
    so a base that is already at the limit loses the suffix to truncation and
    every item in the batch ends up with the SAME token. Oracle then creates the
    first instance and replays it for the rest — the page reports N created and
    one exists. The key comes from the client, and the schema permits the full 64,
    so this is reachable without anything looking wrong.

    Room for the suffix is reserved before truncating, so the part that makes the
    tokens distinct is the part that always survives.
    """
    suffix = f"-{int(index)}"
    head = _clean_retry_token(base)[: 64 - len(suffix)]
    return _clean_retry_token(head + suffix)


def generate_root_password(length: int = 16) -> str:
    """Generate a strong random root password suitable for cloud-init and freeform tags.

    - At least 12 characters (default 16)
    - Contains upper, lower, digit, and a safe symbol
    - Avoids ambiguous look-alikes (0/O, 1/l/I) and characters awkward in shells/tags
    """
    length = max(12, int(length or 16))
    # Keep charset freeform-tag and SSH-paste friendly.
    upper = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower = "abcdefghijkmnopqrstuvwxyz"
    digits = "23456789"
    symbols = "!@#%^*-_=+"
    alphabet = upper + lower + digits + symbols
    # Guarantee one of each class, then fill the rest.
    required = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    rest = [secrets.choice(alphabet) for _ in range(length - len(required))]
    chars = required + rest
    # Fisher–Yates with secrets for an unbiased shuffle.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def shape_display_label(shape: str, ocpus=None, memory=None) -> str:
    """Launch-form shape dropdown label: model name only.

    OCPU / memory are chosen in separate fields (or fixed by the shape), so they
    are intentionally omitted from the label. ``ocpus`` / ``memory`` are kept in
    the signature for call-site compatibility and ignored.
    """
    _ = ocpus, memory
    return (shape or "").strip() or "—"


def build_root_cloud_init(
    *,
    auth_mode: str,
    ssh_public_key: str = "",
    root_password: str = "",
    open_guest_firewall: bool = True,
    custom_boot_script: str = "",
) -> str:
    """Build base64 cloud-config for root key or password authentication.

    custom_boot_script: optional user-supplied shell script executed once at
    first boot (after the panel's own SSH/firewall setup). Written via
    write_files so arbitrary content cannot break the YAML structure.
    """
    auth_mode = (auth_mode or "").strip().lower()
    if auth_mode not in {"key", "password"}:
        raise ValueError("认证方式必须为 key 或 password")
    key = (ssh_public_key or "").strip()
    if auth_mode == "key" and not re.match(r"^(ssh-(?:rsa|ed25519)|ecdsa-sha2-[^ ]+)\s+\S+", key):
        raise ValueError("SSH 公钥格式无效")
    if auth_mode == "password":
        if len(root_password or "") < 12 or not root_password.strip():
            raise ValueError("root 密码至少需要 12 个非空字符")
        if "\n" in root_password or "\r" in root_password:
            raise ValueError("root 密码不能包含换行符")
        try:
            from passlib.hash import sha512_crypt
        except ImportError as exc:  # pragma: no cover - dependency error is user-visible
            raise RuntimeError("密码模式需要 passlib，请执行 pip install -r requirements.txt") from exc
        password_hash = sha512_crypt.using(rounds=656000).hash(root_password)
    else:
        password_hash = ""

    permit_root = "prohibit-password" if auth_mode == "key" else "yes"
    password_auth = "no" if auth_mode == "key" else "yes"
    lines = [
        "#cloud-config",
        "disable_root: false",
        f"ssh_pwauth: {'false' if auth_mode == 'key' else 'true'}",
        "users:",
        "  - default",
        "  - name: root",
        f"    lock_passwd: {'true' if auth_mode == 'key' else 'false'}",
    ]
    if auth_mode == "key":
        lines.extend(["    ssh_authorized_keys:", f"      - {key}"])
    else:
        lines.append(f"    passwd: '{password_hash}'")
    # Named 00- so it is read BEFORE Ubuntu's 50-cloud-init.conf /
    # 60-cloudimg-settings.conf drop-ins. sshd uses the FIRST value seen for each
    # keyword, so a later 99- file would lose to the image's "PasswordAuthentication no".
    lines.extend(
        [
            "write_files:",
            "  - path: /etc/ssh/sshd_config.d/00-ocibot-root.conf",
            "    owner: root:root",
            "    permissions: '0644'",
            "    content: |",
            f"      PermitRootLogin {permit_root}",
            f"      PasswordAuthentication {password_auth}",
            "      KbdInteractiveAuthentication no",
        ]
    )
    if auth_mode == "password":
        # The reliable recipe: set root's password directly at runtime with
        # chpasswd -e (the pre-hashed value, never plaintext), force every sshd
        # config file to allow root+password, then restart sshd. This does not
        # depend on cloud-init's user module actually applying the password.
        lines.extend(
            [
                "  - path: /var/lib/ocibot-sshfix.sh",
                "    permissions: '0700'",
                "    content: |",
                "      #!/bin/bash",
                f"      echo 'root:{password_hash}' | chpasswd -e 2>/dev/null || true",
                "      for f in /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf; do",
                '        [ -f "$f" ] || continue',
                "        sed -ri 's/^[#[:space:]]*PasswordAuthentication.*/PasswordAuthentication yes/' \"$f\" 2>/dev/null || true",
                "        sed -ri 's/^[#[:space:]]*PermitRootLogin.*/PermitRootLogin yes/' \"$f\" 2>/dev/null || true",
                "        sed -ri 's/^[#[:space:]]*KbdInteractiveAuthentication.*/KbdInteractiveAuthentication no/' \"$f\" 2>/dev/null || true",
                "      done",
                "      systemctl daemon-reload 2>/dev/null || true",
                "      systemctl restart ssh.socket 2>/dev/null || true",
                "      systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true",
            ]
        )
    # Normalize every character YAML treats as a line break (\r\n, \r, \x85,
    # U+2028, U+2029, ...) — replacing only \r\n and \r left those in place, and
    # a crafted script could then escape the write_files block scalar and inject
    # its own cloud-config keys.
    script = "\n".join((custom_boot_script or "").splitlines()).strip()
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", script):
        raise ValueError("启动脚本包含不支持的控制字符")
    if script:
        script_lines = script.split("\n")
        if not script_lines[0].startswith("#!"):
            script_lines.insert(0, "#!/bin/bash")
        lines.extend(
            [
                "  - path: /var/lib/ocibot-user-script.sh",
                "    owner: root:root",
                "    permissions: '0700'",
                "    content: |",
            ]
        )
        # Indent user content under the YAML block scalar; blank lines stay blank.
        lines.extend((f"      {ln}" if ln.strip() else "") for ln in script_lines)
    lines.append("runcmd:")
    commands = []
    if auth_mode == "password":
        commands.append("bash /var/lib/ocibot-sshfix.sh 2>/dev/null || true")
    commands.append(
        "systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true",
    )
    commands += [
        # Grow the root partition + filesystem so a resized Boot Volume is usable in-OS.
        "growpart /dev/sda 1 2>/dev/null || true",
        "growpart /dev/sda 2 2>/dev/null || true",
        "resize2fs /dev/sda1 2>/dev/null || xfs_growfs / 2>/dev/null || true",
        "resize2fs /dev/sda2 2>/dev/null || true",
    ]
    if open_guest_firewall:
        commands.extend(
            [
                "ufw --force disable 2>/dev/null || true",
                "iptables -P INPUT ACCEPT 2>/dev/null || true",
                "iptables -P FORWARD ACCEPT 2>/dev/null || true",
                "iptables -P OUTPUT ACCEPT 2>/dev/null || true",
                "iptables -F 2>/dev/null || true",
                "ip6tables -P INPUT ACCEPT 2>/dev/null || true",
                "ip6tables -P FORWARD ACCEPT 2>/dev/null || true",
                "ip6tables -P OUTPUT ACCEPT 2>/dev/null || true",
                "ip6tables -F 2>/dev/null || true",
            ]
        )
    if script:
        # Run last so the user script sees final SSH/firewall state; log for debugging.
        commands.append(
            "bash /var/lib/ocibot-user-script.sh > /var/log/ocibot-user-script.log 2>&1 || true"
        )
    lines.extend(f"  - {command}" for command in commands)
    raw = "\n".join(lines) + "\n"
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")

# Which actions are allowed per lifecycle state
STATE_ACTIONS: dict[str, set[str]] = {
    "RUNNING": {"STOP", "SOFTSTOP", "RESET", "SOFTRESET", "SENDDIAGNOSTICINTERRUPT", "DIAGNOSTICREBOOT", "REBOOTMIGRATE"},
    "STOPPED": {"START"},
    "STARTING": set(),
    "STOPPING": set(),
    "PROVISIONING": set(),
    "TERMINATING": set(),
    "TERMINATED": set(),
    "MOVING": set(),
    "CREATING_IMAGE": set(),
}


def _agent_monitoring_disabled(inst: Any) -> Optional[bool]:
    """Is the Oracle Cloud Agent monitoring plugin off for this instance?

    Returns None when the instance carries no agent_config at all, so the UI can
    stay silent rather than assert something it does not know.

    Two independent switches turn metrics off, and reporting only one of them
    would leave the other case looking like a panel fault: the top-level
    ``is_monitoring_disabled``, and ``are_all_plugins_disabled`` which overrides
    everything below it.
    """
    cfg = getattr(inst, "agent_config", None)
    if cfg is None:
        return None
    if bool(getattr(cfg, "are_all_plugins_disabled", False)):
        return True
    return bool(getattr(cfg, "is_monitoring_disabled", False))


@dataclass
class InstanceInfo:
    """Normalized instance view for the UI."""

    id: str
    display_name: str
    lifecycle_state: str
    region: str
    availability_domain: str
    fault_domain: str
    shape: str
    ocpus: Optional[float]
    memory_gb: Optional[float]
    time_created: str
    compartment_id: str
    image_id: str
    freeform_tags: dict
    defined_tags: dict
    tenant_id: str
    tenant_name: str
    private_ip: str = ""
    public_ip: str = ""
    ipv6_addresses: list[str] = field(default_factory=list)
    boot_volume_gb: Optional[int] = None
    boot_vpus_per_gb: Optional[int] = None
    shape_config_raw: dict = field(default_factory=dict)
    # Oracle Cloud Agent 的监控插件是否被禁用。禁用时「监控」页会是空的，而面板
    # 以前一个字都不解释 —— 用户只会当成功能坏了。这个值就在已经拿到的
    # GetInstance 响应里（Instance.agent_config），不需要多调一次 API。
    # None = 该实例没有返回 agent_config（老实例/老镜像），此时不做任何断言。
    monitoring_disabled: Optional[bool] = None
    raw: Any = None

    @property
    def state_color(self) -> str:
        return {
            "RUNNING": "#22C55E",
            "STOPPED": "#94A3B8",
            "STARTING": "#F59E0B",
            "STOPPING": "#F59E0B",
            "PROVISIONING": "#3B82F6",
            "TERMINATING": "#EF4444",
            "TERMINATED": "#64748B",
            "MOVING": "#A855F7",
            "CREATING_IMAGE": "#06B6D4",
        }.get(self.lifecycle_state, "#64748B")

    def allowed_actions(self) -> set[str]:
        return set(STATE_ACTIONS.get(self.lifecycle_state, set()))

    def can(self, action: str) -> bool:
        return action.upper() in self.allowed_actions()

    def shape_summary(self) -> str:
        parts = [self.shape or "—"]
        if self.ocpus is not None:
            parts.append(f"{self.ocpus:g} OCPU")
        if self.memory_gb is not None:
            parts.append(f"{self.memory_gb:g} GB")
        return " · ".join(parts)

    def ocpu_text(self) -> str:
        return f"{self.ocpus:g}" if self.ocpus is not None else "—"

    def memory_text(self) -> str:
        return f"{self.memory_gb:g} G" if self.memory_gb is not None else "—"

    def disk_text(self) -> str:
        return f"{self.boot_volume_gb} G" if self.boot_volume_gb else "—"

    def disk_perf_text(self) -> str:
        if self.boot_vpus_per_gb is None:
            return "—"
        vpu = int(self.boot_vpus_per_gb)
        # Short labels matching BOOT_VPU_PRESETS tiers.
        if vpu <= 10:
            tier = "平衡"
        elif vpu <= 20:
            tier = "较高"
        else:
            tier = "超高"
        return f"{vpu} {tier}"

    def ipv6_text(self) -> str:
        if not self.ipv6_addresses:
            return "—"
        return ", ".join(self.ipv6_addresses)

    def primary_ipv6(self) -> str:
        return self.ipv6_addresses[0] if self.ipv6_addresses else ""

    def ip_summary(self) -> str:
        pub = self.public_ip or "—"
        priv = self.private_ip or "—"
        v6 = self.ipv6_text()
        return f"公网 {pub}  |  私网 {priv}  |  IPv6 {v6}"


@dataclass
class OperationResult:
    ok: bool
    message: str
    work_request_id: str = ""
    data: Any = None


@dataclass
class PrimaryNetworkInfo:
    vnic_id: str = ""
    subnet_id: str = ""
    private_ip_id: str = ""
    private_ipv4: str = ""
    private_ip_compartment_id: str = ""
    public_ip_id: str = ""
    public_ipv4: str = ""
    public_ip_lifetime: str = ""
    ipv6_addresses: list[str] = field(default_factory=list)
    nsg_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FirewallRuleSpec:
    direction: str
    protocol: str
    cidr: str
    port_min: Optional[int] = None
    port_max: Optional[int] = None
    stateless: bool = False
    description: str = ""

    def validate(self) -> None:
        direction = self.direction.upper()
        if direction not in {"INGRESS", "EGRESS"}:
            raise ValueError("方向必须为 INGRESS 或 EGRESS")
        protocol = str(self.protocol).lower()
        if protocol not in {"all", "6", "17", "1", "58"}:
            raise ValueError("协议必须为 All、TCP、UDP、ICMPv4 或 ICMPv6")
        network = ipaddress.ip_network(self.cidr, strict=False)
        if protocol == "1" and network.version != 4:
            raise ValueError("ICMPv4 规则必须使用 IPv4 CIDR")
        if protocol == "58" and network.version != 6:
            raise ValueError("ICMPv6 规则必须使用 IPv6 CIDR")
        if protocol in {"6", "17"} and (self.port_min is not None or self.port_max is not None):
            start = int(self.port_min if self.port_min is not None else self.port_max)
            end = int(self.port_max if self.port_max is not None else start)
            if not 1 <= start <= end <= 65535:
                raise ValueError("端口必须在 1–65535 之间")


class OCIClientError(Exception):
    """Raised for user-visible OCI errors."""


def build_grow_fs_script() -> str:
    """Idempotent, online root-filesystem grow for a resized boot volume.

    Detects the root device/partition, runs growpart, then resize2fs (ext4) or
    xfs_growfs (xfs). Runs as root via the Oracle Cloud Agent (Run Command); no reboot.
    """
    return r"""#!/bin/bash
# OCIBot: grow root filesystem to fill a resized boot volume (online, no reboot).
set -u
ROOT_SRC=$(findmnt -no SOURCE / 2>/dev/null)
FSTYPE=$(findmnt -no FSTYPE / 2>/dev/null)
DEV=$(readlink -f "$ROOT_SRC" 2>/dev/null || echo "$ROOT_SRC")
if [ -z "$DEV" ]; then echo "无法确定根设备"; exit 1; fi
DISK=""; PART=""
if echo "$DEV" | grep -Eq '^/dev/nvme[0-9]+n[0-9]+p[0-9]+$'; then
  DISK=$(echo "$DEV" | sed -E 's/p[0-9]+$//'); PART=$(echo "$DEV" | grep -oE '[0-9]+$')
elif echo "$DEV" | grep -Eq '^/dev/[a-z]+[0-9]+$'; then
  DISK=$(echo "$DEV" | sed -E 's/[0-9]+$//'); PART=$(echo "$DEV" | grep -oE '[0-9]+$')
else
  echo "无法解析磁盘/分区: $DEV"; exit 1
fi
echo "root=$DEV disk=$DISK part=$PART fstype=$FSTYPE"
if ! command -v growpart >/dev/null 2>&1; then
  (command -v apt-get >/dev/null 2>&1 && apt-get update -y && apt-get install -y cloud-guest-utils) \
    || (command -v yum >/dev/null 2>&1 && yum install -y cloud-utils-growpart) \
    || (command -v dnf >/dev/null 2>&1 && dnf install -y cloud-utils-growpart) || true
fi
growpart "$DISK" "$PART" || echo "growpart: 分区已是最大或无需扩展"
if [ "$FSTYPE" = "xfs" ]; then
  xfs_growfs / || xfs_growfs "$DEV"
else
  resize2fs "$DEV"
fi
echo "== 完成 =="
df -h /
"""


# Sort key for the invoice list. The service accepts a closed set and the SDK
# rejects anything else client-side; keep this in step with
# oci.osp_gateway.InvoiceServiceClient.list_invoices.
_INVOICE_SORT_BY = "INVOICE_DATE"


class TenantSession:
    """One authenticated OCI session bound to a TenantConfig."""

    def __init__(self, tenant: TenantConfig):
        if not OCI_AVAILABLE:
            raise OCIClientError("未安装 oci SDK，请先执行: pip install -r requirements.txt")
        self.tenant = tenant
        self._key_file: Optional[Path] = None
        self._lock = threading.RLock()
        self._compute: Optional[ComputeClient] = None
        self._network: Optional[VirtualNetworkClient] = None
        self._identity: Optional[IdentityClient] = None
        self._blockstorage: Optional[BlockstorageClient] = None
        self._limits: Optional[LimitsClient] = None
        self._monitoring: Optional[MonitoringClient] = None
        self._usage: Any = None
        self._object_storage: Any = None
        self._object_namespace: str = ""
        self._instance_agent: Any = None
        self._config: dict = {}
        # Per-compartment scan errors from the most recent list_instances_tree call.
        self._last_tree_errors: list[str] = []
        self._build()

    def _build(self) -> None:
        # Keep the decrypted key in memory only. It used to be written to a temp
        # file, which left plaintext OCI API keys in the system temp directory for
        # any session that was not closed cleanly (and chmod 0600 is close to a
        # no-op on Windows). The SDK accepts the PEM directly via key_content.
        try:
            self._key_file = None
            self._config = {
                "user": self.tenant.user_ocid.strip(),
                "fingerprint": self.tenant.fingerprint.strip(),
                "tenancy": self.tenant.tenancy_ocid.strip(),
                "region": self.tenant.region.strip(),
                "key_content": self.tenant.private_key_pem.strip() + "\n",
            }
            # Validate config early
            oci.config.validate_config(self._config)
            # Client-level SDK retry for transient 429 / 5xx / timeouts.
            # LaunchInstance overrides this with NoneRetryStrategy so capacity
            # retry + application 429 cooldown stay the single control plane.
            retry_kw = {"retry_strategy": sdk_default_retry_strategy()}
            self._compute = ComputeClient(self._config, **retry_kw)
            self._network = VirtualNetworkClient(self._config, **retry_kw)
            self._identity = IdentityClient(self._config, **retry_kw)
            self._blockstorage = BlockstorageClient(self._config, **retry_kw)
            self._limits = LimitsClient(self._config, **retry_kw)
            self._monitoring = MonitoringClient(self._config, **retry_kw)
            try:
                self._object_storage = ObjectStorageClient(self._config, **retry_kw)
            except Exception:
                self._object_storage = None
            try:
                self._instance_agent = ComputeInstanceAgentClient(self._config, **retry_kw)
            except Exception:
                self._instance_agent = None
            try:
                # Usage API is often home-region only; prefer home region when known.
                usage_cfg = dict(self._config)
                home = ""
                try:
                    home = self._home_region()
                except Exception:
                    home = ""
                if home:
                    usage_cfg["region"] = home
                self._usage = UsageapiClient(usage_cfg, **retry_kw)
            except Exception:
                self._usage = None
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._key_file and self._key_file.exists():
            try:
                self._key_file.unlink()
            except OSError:
                pass
        self._key_file = None

    def __enter__(self) -> "TenantSession":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @property
    def compute(self) -> ComputeClient:
        assert self._compute is not None
        return self._compute

    @property
    def network(self) -> VirtualNetworkClient:
        assert self._network is not None
        return self._network

    @property
    def identity(self) -> IdentityClient:
        assert self._identity is not None
        return self._identity

    @property
    def blockstorage(self) -> BlockstorageClient:
        assert self._blockstorage is not None
        return self._blockstorage

    @property
    def limits(self) -> LimitsClient:
        assert self._limits is not None
        return self._limits

    @property
    def monitoring(self) -> MonitoringClient:
        assert self._monitoring is not None
        return self._monitoring

    @property
    def usage(self) -> Any:
        return self._usage

    @property
    def object_storage(self) -> Any:
        return self._object_storage

    @property
    def instance_agent(self) -> Any:
        return self._instance_agent

    def resolve_compartment(self) -> str:
        if self.tenant.compartment_ocid.strip():
            return self.tenant.compartment_ocid.strip()
        return self.tenant.tenancy_ocid.strip()

    def test_connection(self) -> OperationResult:
        try:
            compartment = self.resolve_compartment()
            # Lightweight call: get tenancy / list regions or get user
            user = self.identity.get_user(self.tenant.user_ocid.strip()).data
            return OperationResult(
                ok=True,
                message=f"连接成功：{getattr(user, 'description', '') or user.name}",
                data={"user": user.name, "compartment": compartment},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def list_compartments(
        self,
        parent_id: Optional[str] = None,
        subtree: bool = True,
        *,
        strict: bool = False,
    ) -> list[dict[str, str]]:
        """List accessible compartments under tenancy or a parent (including the parent/root).

        ``strict=True`` raises instead of degrading to "just the root". Callers that
        merely populate a picker want the degraded list — a broken enumeration still
        leaves the root usable. Callers that COUNT resources must not: a failed
        ListCompartments makes the subtree look like one compartment, and a count
        taken over it is an undercount that carries no evidence of being one.
        """
        tenancy = self.tenant.tenancy_ocid.strip()
        root = (parent_id or tenancy).strip()
        if root == tenancy:
            items = [{"id": tenancy, "name": "(根) Tenancy", "description": "root"}]
        else:
            items = [{"id": root, "name": "(当前) Compartment", "description": "selected"}]
        try:
            response = oci.pagination.list_call_get_all_results(
                self.identity.list_compartments,
                root,
                compartment_id_in_subtree=bool(subtree),
                access_level="ACCESSIBLE",
            )
            for c in response.data:
                state = getattr(c, "lifecycle_state", "") or ""
                if state in ("DELETED", "DELETING"):
                    continue
                items.append(
                    {
                        "id": c.id,
                        "name": c.name,
                        "description": getattr(c, "description", "") or "",
                    }
                )
        except ServiceError as exc:
            # An IAM policy that grants `manage instance-family in compartment child`
            # without `inspect compartments in tenancy` makes this a PERMANENT 404
            # (NotAuthorizedOrNotFound), not a blip; sustained 429s produce the same
            # thing transiently. Either way the quota readers below would report the
            # root's usage as the whole tenancy's and call it complete.
            if strict:
                raise OCIClientError(_format_service_error(exc)) from exc
        # de-dupe by id
        seen: set[str] = set()
        unique: list[dict[str, str]] = []
        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            unique.append(it)
        return unique

    def list_instances(
        self,
        compartment_id: Optional[str] = None,
        lifecycle_state: Optional[str] = None,
        resolve_ips: bool = True,
    ) -> list[InstanceInfo]:
        compartment = (compartment_id or self.resolve_compartment()).strip()
        kwargs: dict[str, Any] = {"compartment_id": compartment}
        if lifecycle_state:
            kwargs["lifecycle_state"] = lifecycle_state

        try:
            response = oci.pagination.list_call_get_all_results(
                self.compute.list_instances,
                **kwargs,
            )
        except ServiceError as exc:
            raise OCIClientError(_format_service_error(exc)) from exc

        results: list[InstanceInfo] = [self._to_instance_info(inst) for inst in response.data]
        if resolve_ips:
            targets = [i for i in results if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
            self._enrich_instances_parallel(targets, compartment)
        results.sort(key=lambda i: (i.lifecycle_state != "RUNNING", i.display_name.lower()))
        return results

    def _resolve_ips_parallel(self, infos: list["InstanceInfo"], compartment: str) -> None:
        """Fill private/public/IPv6 addresses for many instances concurrently.

        Each instance is resolved against its own compartment so this works for a
        merged multi-compartment list, falling back to ``compartment`` when unknown.
        """
        if not infos:
            return

        def _fill(info: "InstanceInfo") -> None:
            try:
                network = self.resolve_primary_network(info.id, info.compartment_id or compartment)
                info.private_ip = network.private_ipv4
                info.public_ip = network.public_ipv4
                info.ipv6_addresses = network.ipv6_addresses
            except Exception:
                pass

        if len(infos) == 1:
            _fill(infos[0])
            return
        with ThreadPoolExecutor(max_workers=min(_IP_RESOLVE_WORKERS, len(infos))) as pool:
            list(pool.map(_fill, infos))

    def _resolve_boot_volumes_parallel(self, infos: list["InstanceInfo"]) -> None:
        """Fill boot volume size / VPU performance for many instances concurrently."""
        if not infos:
            return

        def _fill(info: "InstanceInfo") -> None:
            try:
                bv_id = self._find_boot_volume_id(
                    info.id,
                    info.compartment_id,
                    info.availability_domain,
                    wait=False,
                )
                if not bv_id:
                    return
                bv = self.blockstorage.get_boot_volume(bv_id).data
                size = int(getattr(bv, "size_in_gbs", 0) or 0)
                vpu = int(getattr(bv, "vpus_per_gb", 10) or 10)
                info.boot_volume_gb = size or None
                info.boot_vpus_per_gb = vpu
            except Exception:
                pass

        if len(infos) == 1:
            _fill(infos[0])
            return
        with ThreadPoolExecutor(max_workers=min(_IP_RESOLVE_WORKERS, len(infos))) as pool:
            list(pool.map(_fill, infos))

    def _enrich_instances_parallel(self, infos: list["InstanceInfo"], compartment: str) -> None:
        """Resolve network addresses and boot-volume specs for listed instances."""
        if not infos:
            return
        # Run both enrichment passes. Each already uses its own bounded pool; for
        # small lists this is sequential enough and keeps error isolation simple.
        self._resolve_ips_parallel(infos, compartment)
        self._resolve_boot_volumes_parallel(infos)

    def enrich_instance(self, info: "InstanceInfo") -> "InstanceInfo":
        """Fill IP + boot-volume fields for one instance (mutates and returns it).

        Used by the UI after a lean list load so bulk refreshes stay cheap.
        """
        if info.lifecycle_state in ("TERMINATED", "TERMINATING"):
            return info
        compartment = (info.compartment_id or self.resolve_compartment()).strip()
        self._enrich_instances_parallel([info], compartment)
        return info

    def get_instance(self, instance_id: str, resolve_ips: bool = True) -> InstanceInfo:
        try:
            inst = self.compute.get_instance(instance_id).data
        except ServiceError as exc:
            raise OCIClientError(_format_service_error(exc)) from exc
        info = self._to_instance_info(inst)
        if resolve_ips:
            self._enrich_instances_parallel([info], info.compartment_id)
        return info

    def instance_action(self, instance_id: str, action: str) -> OperationResult:
        action = action.upper().strip()
        if action not in POWER_ACTIONS:
            return OperationResult(ok=False, message=f"不支持的操作: {action}")
        try:
            # Pre-check state
            current = self.compute.get_instance(instance_id).data
            state = current.lifecycle_state
            allowed = STATE_ACTIONS.get(state, set())
            if action not in allowed:
                return OperationResult(
                    ok=False,
                    message=f"实例当前状态为 {state}，无法执行 {POWER_ACTIONS[action]}",
                )
            resp = self.compute.instance_action(instance_id, action)
            wr = ""
            if hasattr(resp, "headers"):
                wr = resp.headers.get("opc-work-request-id", "") or ""
            return OperationResult(
                ok=True,
                message=f"已提交 {POWER_ACTIONS[action]} 请求",
                work_request_id=wr,
                data=resp.data if hasattr(resp, "data") else None,
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def terminate_instance(
        self,
        instance_id: str,
        preserve_boot_volume: bool = True,
    ) -> OperationResult:
        try:
            resp = self.compute.terminate_instance(
                instance_id,
                preserve_boot_volume=preserve_boot_volume,
            )
            wr = ""
            if hasattr(resp, "headers"):
                wr = resp.headers.get("opc-work-request-id", "") or ""
            return OperationResult(
                ok=True,
                message="已提交终止实例请求"
                + ("（保留 Boot Volume）" if preserve_boot_volume else "（删除 Boot Volume）"),
                work_request_id=wr,
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def set_root_password_note(self, instance_id: str, password: str) -> OperationResult:
        """Update (or clear) the remembered root password on an existing instance.

        The value lives in the instance's freeform tag ``ocibot_root_password``,
        written once at launch. It is a memo — nothing authenticates with it — so
        it silently goes stale the moment the operator changes the password on the
        box over SSH, which is exactly when they need the panel to still be right.

        Two OCI calls, and the first one is not optional: ``UpdateInstanceDetails``
        REPLACES the whole freeform-tag map rather than merging into it, so sending
        only this key would delete every other tag on the instance — including the
        ``ocibot_managed`` marker other features key off. Read, merge, write.

        An empty password removes the tag instead of storing an empty string, so
        the list shows "—" (no password recorded) rather than a blank that looks
        like a rendering fault.
        """
        password = (password or "").strip()
        # Tag values cannot span lines, and a stray newline would corrupt the map
        # rather than fail loudly.
        if "\n" in password or "\r" in password:
            return OperationResult(ok=False, message="密码备注不能包含换行")
        if len(password) > 255:
            return OperationResult(ok=False, message="密码备注过长（上限 255 字符）")
        try:
            current = self.compute.get_instance(instance_id).data
            tags = dict(getattr(current, "freeform_tags", None) or {})
            if password:
                tags[ROOT_PASSWORD_TAG] = password
            else:
                tags.pop(ROOT_PASSWORD_TAG, None)
            details = oci.core.models.UpdateInstanceDetails(freeform_tags=tags)
            self.compute.update_instance(instance_id, details)
            return OperationResult(
                ok=True,
                message="已更新密码备注" if password else "已清除密码备注",
                data={"root_password": password},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def set_instance_protected(self, instance_id: str, protected: bool) -> OperationResult:
        """Mark/unmark an instance as protected from termination.

        Stored as an OCI freeform tag rather than a panel-local column on purpose:
        the flag then survives a panel reinstall or a database restore, and it is
        visible in the Oracle console next to the instance, so it does not become
        a fact only this panel knows.

        Read-merge-write, same as the root-password note above — an
        UpdateInstanceDetails carrying only this key would delete every other tag
        on the instance, including the ``ocibot_managed`` marker other features
        key off.
        """
        try:
            current = self.compute.get_instance(instance_id).data
            tags = dict(getattr(current, "freeform_tags", None) or {})
            if protected:
                tags[TERMINATE_PROTECT_TAG] = "true"
            else:
                tags.pop(TERMINATE_PROTECT_TAG, None)
            self.compute.update_instance(
                instance_id, oci.core.models.UpdateInstanceDetails(freeform_tags=tags)
            )
            return OperationResult(
                ok=True,
                message="已开启终止保护" if protected else "已解除终止保护",
                data={"protected": bool(protected)},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def capture_console_output(
        self,
        instance_id: str,
        *,
        length_bytes: int = 256 * 1024,
        timeout: int = 120,
    ) -> OperationResult:
        """Capture and return the instance's serial console output (boot log).

        This is NOT the interactive console connection the panel already has.
        That one needs an SSH key and a working network path, and it is useless
        for the case that matters most: the machine does not come back after a
        reboot. Editing /etc/fstab or resizing a boot volume can leave a VM
        sitting at an initramfs prompt — OCI still reports RUNNING, SSH times
        out, and until now the panel had no way to see why.

        OCI models this as a resource, not a call: CaptureConsoleHistory creates
        a ConsoleHistory that moves REQUESTED -> SUCCEEDED, and only then can its
        content be read. So this polls, and the wait is bounded.

        Old captures are deleted first. They persist and count against a
        per-instance limit, so a panel that only ever created them would
        eventually start failing with a quota error that names nothing the
        operator recognises.
        """
        instance_id = (instance_id or "").strip()
        if not instance_id:
            return OperationResult(ok=False, message="缺少 instance_id")
        # OCI caps a single read at 2 MiB; keep the default well under it so the
        # response stays something a browser can render.
        length_bytes = max(4096, min(int(length_bytes or 0) or 262144, 2 * 1024 * 1024))
        try:
            inst = self.compute.get_instance(instance_id).data
            compartment_id = getattr(inst, "compartment_id", "") or self.resolve_compartment()

            # Clean up this instance's previous captures before making another.
            try:
                old = oci.pagination.list_call_get_all_results(
                    self.compute.list_console_histories,
                    compartment_id,
                    instance_id=instance_id,
                ).data or []
                for h in old:
                    state = str(getattr(h, "lifecycle_state", "") or "")
                    if state in {"SUCCEEDED", "FAILED"}:
                        try:
                            self.compute.delete_console_history(getattr(h, "id", ""))
                        except Exception:  # noqa: BLE001
                            # Best effort: a stale record we cannot remove must not
                            # stop the operator getting today's log.
                            pass
            except Exception:  # noqa: BLE001
                pass

            created = self.compute.capture_console_history(
                oci.core.models.CaptureConsoleHistoryDetails(instance_id=instance_id)
            ).data
            history_id = getattr(created, "id", "") or ""
            if not history_id:
                return OperationResult(ok=False, message="Oracle 未返回控制台历史 ID")

            deadline = time.monotonic() + max(15, int(timeout))
            state = str(getattr(created, "lifecycle_state", "") or "")
            while state not in {"SUCCEEDED", "FAILED"} and time.monotonic() < deadline:
                time.sleep(2.0)
                try:
                    state = str(
                        getattr(
                            self.compute.get_console_history(history_id).data,
                            "lifecycle_state",
                            "",
                        )
                        or ""
                    )
                except ServiceError:
                    break
            if state == "FAILED":
                return OperationResult(ok=False, message="Oracle 抓取控制台输出失败")
            if state != "SUCCEEDED":
                return OperationResult(
                    ok=False,
                    message=f"抓取控制台输出超时（{timeout}s，状态 {state or '未知'}）。实例刚启动时可能还没有输出，稍后重试。",
                )

            content = self.compute.get_console_history_content(
                history_id, length=length_bytes
            ).data
            text = getattr(content, "value", None)
            if text is None:
                text = content if isinstance(content, str) else str(content or "")
            return OperationResult(
                ok=True,
                message=f"已抓取控制台输出（{len(text)} 字符）",
                data={"content": text, "history_id": history_id},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def rename_instance(self, instance_id: str, display_name: str) -> OperationResult:
        display_name = (display_name or "").strip()
        if not display_name:
            return OperationResult(ok=False, message="名称不能为空")
        try:
            details = oci.core.models.UpdateInstanceDetails(display_name=display_name)
            self.compute.update_instance(instance_id, details)
            return OperationResult(ok=True, message=f"已重命名为「{display_name}」")
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def list_instances_tree(
        self,
        root_compartment_id: Optional[str] = None,
        resolve_ips: bool = True,
        include_subcompartments: bool = True,
    ) -> list[InstanceInfo]:
        """List instances in compartment and optionally its sub-compartments only."""
        root = (root_compartment_id or self.resolve_compartment()).strip()
        # Kept apart from the per-compartment scan errors below: failing to enumerate
        # is not "one compartment could not be read", it is "we never learned the
        # subtree exists and scanned only the root". It still has to reach
        # _last_tree_errors, because that is the only channel get_free_quota_usage
        # reads — without it an undercount is reported as an authoritative zero and
        # the fail-closed quota guard waves the launch through.
        enum_error = ""
        if include_subcompartments:
            try:
                # Only compartments under this root (not the entire tenancy when root is a child)
                compartments = [
                    c["id"] for c in self.list_compartments(parent_id=root, subtree=True, strict=True)
                ]
            except Exception as exc:  # noqa: BLE001
                compartments = [root]
                enum_error = f"子区间枚举失败：{exc}"
            if root not in compartments:
                compartments.insert(0, root)
        else:
            compartments = [root]

        errors: list[str] = []

        def _scan(cid: str) -> list[InstanceInfo]:
            # Defer IP resolution: gather instances across compartments first, then
            # resolve every IP in one bounded pool so pools don't nest and explode.
            return self.list_instances(compartment_id=cid, resolve_ips=False)

        per_compartment: list[list[InstanceInfo]] = []
        if len(compartments) == 1:
            try:
                per_compartment.append(_scan(compartments[0]))
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        else:
            with ThreadPoolExecutor(max_workers=min(_COMPARTMENT_WORKERS, len(compartments))) as pool:
                for cid, future in [(c, pool.submit(_scan, c)) for c in compartments]:
                    try:
                        per_compartment.append(future.result())
                    except Exception as exc:  # noqa: BLE001
                        errors.append(str(exc))

        all_items: list[InstanceInfo] = []
        seen: set[str] = set()
        for items in per_compartment:
            for it in items:
                if it.id in seen:
                    continue
                seen.add(it.id)
                all_items.append(it)

        # Record partial failures so quota accounting can tell "nothing there" apart
        # from "some compartments could not be read". Without this a throttled scan
        # produced an undercount that looked like plenty of free capacity.
        self._last_tree_errors = ([enum_error] if enum_error else []) + list(errors)
        # The raise stays keyed on SCAN errors only. A failed enumeration plus a
        # genuinely empty root is still an empty list for the instances page; it is
        # the quota snapshot, which reads _last_tree_errors, that must treat it as
        # incomplete.
        if not all_items and errors and len(compartments) == 1:
            # Surface the only compartment's error instead of empty silent list
            raise OCIClientError(errors[0])
        if resolve_ips:
            targets = [i for i in all_items if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
            self._enrich_instances_parallel(targets, root)
        all_items.sort(key=lambda i: (i.lifecycle_state != "RUNNING", i.display_name.lower()))
        return all_items

    def list_availability_domains(self) -> list[str]:
        try:
            ads = self.identity.list_availability_domains(self.tenant.tenancy_ocid.strip()).data
            return [a.name for a in ads]
        except ServiceError as exc:
            raise OCIClientError(_format_service_error(exc)) from exc

    def list_images(
        self,
        compartment_id: Optional[str] = None,
        operating_system: Optional[str] = None,
        ubuntu_only: bool = False,
    ) -> list[dict]:
        compartment = (compartment_id or self.resolve_compartment()).strip()
        kwargs: dict[str, Any] = {
            "compartment_id": compartment,
            "lifecycle_state": "AVAILABLE",
            "sort_by": "TIMECREATED",
            "sort_order": "DESC",
        }
        # Prefer official filter when ubuntu_only
        if ubuntu_only and not operating_system:
            kwargs["operating_system"] = "Canonical Ubuntu"
        elif operating_system:
            kwargs["operating_system"] = operating_system
        try:
            resp = oci.pagination.list_call_get_all_results(self.compute.list_images, **kwargs)
        except ServiceError as exc:
            raise OCIClientError(_format_service_error(exc)) from exc

        def _img_item(img: Any) -> dict:
            name = img.display_name or img.id
            os_name = getattr(img, "operating_system", "") or ""
            os_ver = getattr(img, "operating_system_version", "") or ""
            base_image_id = getattr(img, "base_image_id", "") or ""
            img_compartment = getattr(img, "compartment_id", None) or ""
            name_lower = name.lower()
            architecture = "ARM64" if "aarch64" in name_lower or "arm64" in name_lower else "AMD64"
            variant = " · Minimal" if "minimal" in name_lower else ""
            is_custom = bool(img_compartment)
            if is_custom:
                label = f"自定义镜像 · {name}  [{img.id[-8:]}]"
            else:
                os_label = "Ubuntu" if os_name.strip().lower() == "canonical ubuntu" else (os_name or "镜像")
                label = f"{os_label} {os_ver or '未知版本'} · {architecture}{variant}  [{img.id[-8:]}]"
            return {
                "id": img.id,
                "display_name": name,
                "operating_system": os_name,
                "operating_system_version": os_ver,
                "base_image_id": base_image_id,
                "architecture": architecture,
                "is_custom": is_custom,
                "label": label,
            }

        def _is_ubuntu(img_or_dict: Any) -> bool:
            if isinstance(img_or_dict, dict):
                os_name = str(img_or_dict.get("operating_system", ""))
                base_image_id = str(img_or_dict.get("base_image_id", "") or "")
            else:
                os_name = str(getattr(img_or_dict, "operating_system", "") or "")
                base_image_id = str(getattr(img_or_dict, "base_image_id", "") or "")
            return os_name.strip().lower() == "canonical ubuntu" and not base_image_id

        items = [_img_item(img) for img in resp.data]
        # If empty/few, also try tenancy root for platform images
        if len(items) < 5 and compartment != self.tenant.tenancy_ocid.strip():
            try:
                kwargs["compartment_id"] = self.tenant.tenancy_ocid.strip()
                resp2 = oci.pagination.list_call_get_all_results(self.compute.list_images, **kwargs)
                seen = {i["id"] for i in items}
                for img in resp2.data:
                    if img.id in seen:
                        continue
                    items.append(_img_item(img))
            except Exception:
                pass

        if ubuntu_only:
            filtered = [i for i in items if _is_ubuntu(i)]
            # Fallback: OS filter may miss custom naming
            if not filtered:
                try:
                    kwargs.pop("operating_system", None)
                    kwargs["compartment_id"] = self.tenant.tenancy_ocid.strip()
                    resp3 = oci.pagination.list_call_get_all_results(self.compute.list_images, **kwargs)
                    filtered = [_img_item(img) for img in resp3.data if _is_ubuntu(img)]
                except Exception:
                    pass
            # Use the Ubuntu-filtered list; passing `items` here discarded the
            # filter entirely, so non-Ubuntu images leaked into ubuntu_only
            # results. `or items` keeps the never-hide-everything fallback.
            items = _latest_lts_ubuntu_images(filtered or items)

        # Prefer newest Ubuntu versions first (already TIMECREATED DESC)
        return items[:200]

    def list_shapes(
        self,
        compartment_id: Optional[str] = None,
        availability_domain: Optional[str] = None,
        image_id: Optional[str] = None,
    ) -> list[dict]:
        compartment = (compartment_id or self.resolve_compartment()).strip()
        kwargs: dict[str, Any] = {"compartment_id": compartment}
        if availability_domain:
            kwargs["availability_domain"] = availability_domain
        if image_id:
            kwargs["image_id"] = image_id
        try:
            resp = oci.pagination.list_call_get_all_results(self.compute.list_shapes, **kwargs)
        except ServiceError as exc:
            raise OCIClientError(_format_service_error(exc)) from exc
        items = []
        for s in resp.data:
            ocpus = getattr(s, "ocpus", None)
            mem = getattr(s, "memory_in_gbs", None)
            shape_name = s.shape
            ocpu_opts = getattr(s, "ocpu_options", None)
            memory_opts = getattr(s, "memory_options", None)
            items.append(
                {
                    "shape": shape_name,
                    "ocpus": ocpus,
                    "memory_in_gbs": mem,
                    "processor_description": getattr(s, "processor_description", "") or "",
                    "is_free_tier": shape_name in FREE_TIER_SHAPES,
                    "free_tag": free_tier_tag(shape_name),
                    "label": shape_display_label(shape_name, ocpus, mem),
                    "is_flexible": bool(getattr(s, "is_flexible", False)),
                    "min_ocpus": getattr(ocpu_opts, "min", None),
                    "max_ocpus": getattr(ocpu_opts, "max", None),
                    "min_memory_in_gbs": getattr(memory_opts, "min_in_g_bs", None),
                    "max_memory_in_gbs": getattr(memory_opts, "max_in_g_bs", None),
                    "min_gbs_per_ocpu": getattr(memory_opts, "min_per_ocpu_in_gbs", None),
                    "max_gbs_per_ocpu": getattr(memory_opts, "max_per_ocpu_in_gbs", None),
                    "billing_type": getattr(s, "billing_type", "") or "",
                }
            )
        # unique by shape name
        seen = set()
        unique = []
        for it in items:
            if it["shape"] in seen:
                continue
            seen.add(it["shape"])
            unique.append(it)
        # Free tier first, then name
        unique.sort(key=lambda x: (0 if x.get("is_free_tier") else 1, x["shape"]))
        return unique

    def list_vcns(self, compartment_id: Optional[str] = None) -> list[dict]:
        compartment = (compartment_id or self.resolve_compartment()).strip()
        try:
            resp = oci.pagination.list_call_get_all_results(
                self.network.list_vcns,
                compartment_id=compartment,
            )
        except ServiceError as exc:
            raise OCIClientError(_format_service_error(exc)) from exc
        return [
            {
                "id": v.id,
                "display_name": v.display_name,
                "cidr_block": getattr(v, "cidr_block", "") or "",
                "compartment_id": getattr(v, "compartment_id", "") or compartment,
                "ipv6_cidr_blocks": list(getattr(v, "ipv6_cidr_blocks", None) or []),
                "label": f"{v.display_name} ({getattr(v, 'cidr_block', '') or v.id[-8:]})",
            }
            for v in resp.data
            if getattr(v, "lifecycle_state", "") == "AVAILABLE"
        ]

    def list_subnets(self, compartment_id: Optional[str] = None, vcn_id: Optional[str] = None) -> list[dict]:
        compartment = (compartment_id or self.resolve_compartment()).strip()
        kwargs: dict[str, Any] = {"compartment_id": compartment}
        if vcn_id:
            kwargs["vcn_id"] = vcn_id
        try:
            resp = oci.pagination.list_call_get_all_results(self.network.list_subnets, **kwargs)
        except ServiceError as exc:
            raise OCIClientError(_format_service_error(exc)) from exc
        return [
            {
                "id": s.id,
                "display_name": s.display_name,
                "cidr_block": getattr(s, "cidr_block", "") or "",
                "vcn_id": getattr(s, "vcn_id", "") or "",
                "availability_domain": getattr(s, "availability_domain", "") or "",
                "compartment_id": getattr(s, "compartment_id", "") or compartment,
                "prohibit_public_ip_on_vnic": bool(getattr(s, "prohibit_public_ip_on_vnic", False)),
                "prohibit_internet_ingress": bool(getattr(s, "prohibit_internet_ingress", False)),
                "ipv6_cidr_block": getattr(s, "ipv6_cidr_block", "") or "",
                "ipv6_cidr_blocks": list(getattr(s, "ipv6_cidr_blocks", None) or []),
                "ipv6_enabled": bool(
                    getattr(s, "ipv6_cidr_block", "") or getattr(s, "ipv6_cidr_blocks", None)
                ),
                "security_list_ids": list(getattr(s, "security_list_ids", None) or []),
                "label": f"{s.display_name} ({getattr(s, 'cidr_block', '') or s.id[-8:]})",
            }
            for s in resp.data
            if getattr(s, "lifecycle_state", "") == "AVAILABLE"
        ]

    def _subnet_dict(self, s: Any, compartment: str = "") -> dict:
        """Normalize a subnet SDK object (or dict) into the UI/list shape."""
        if isinstance(s, dict):
            return s
        compartment = compartment or getattr(s, "compartment_id", "") or ""
        return {
            "id": s.id,
            "display_name": s.display_name,
            "cidr_block": getattr(s, "cidr_block", "") or "",
            "vcn_id": getattr(s, "vcn_id", "") or "",
            "availability_domain": getattr(s, "availability_domain", "") or "",
            "compartment_id": getattr(s, "compartment_id", "") or compartment,
            "prohibit_public_ip_on_vnic": bool(getattr(s, "prohibit_public_ip_on_vnic", False)),
            "prohibit_internet_ingress": bool(getattr(s, "prohibit_internet_ingress", False)),
            "ipv6_cidr_block": getattr(s, "ipv6_cidr_block", "") or "",
            "ipv6_cidr_blocks": list(getattr(s, "ipv6_cidr_blocks", None) or []),
            "ipv6_enabled": bool(
                getattr(s, "ipv6_cidr_block", "") or getattr(s, "ipv6_cidr_blocks", None)
            ),
            "security_list_ids": list(getattr(s, "security_list_ids", None) or []),
            "label": f"{s.display_name} ({getattr(s, 'cidr_block', '') or s.id[-8:]})",
        }

    def _vcn_dict(self, v: Any, compartment: str = "") -> dict:
        if isinstance(v, dict):
            return v
        compartment = compartment or getattr(v, "compartment_id", "") or ""
        return {
            "id": v.id,
            "display_name": v.display_name,
            "cidr_block": getattr(v, "cidr_block", "") or "",
            "compartment_id": getattr(v, "compartment_id", "") or compartment,
            "ipv6_cidr_blocks": list(getattr(v, "ipv6_cidr_blocks", None) or []),
            "label": f"{v.display_name} ({getattr(v, 'cidr_block', '') or v.id[-8:]})",
        }

    def _wait_network_resource(
        self,
        getter: Callable[[str], Any],
        resource_id: str,
        *,
        ready_states: tuple[str, ...] = ("AVAILABLE",),
        failed_states: tuple[str, ...] = ("TERMINATED", "TERMINATING", "FAULTY"),
        timeout: float = 120,
        interval: float = 2,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = getter(resource_id).data
            state = getattr(last, "lifecycle_state", "") or ""
            if state in ready_states:
                return last
            if state in failed_states:
                raise OCIClientError(f"网络资源 {resource_id[-12:]} 状态异常：{state}")
            time.sleep(interval)
        state = getattr(last, "lifecycle_state", "?") if last is not None else "?"
        raise OCIClientError(f"等待网络资源就绪超时（最后状态 {state}）")

    @staticmethod
    def _vcn_has_ipv6(vcn_obj: Any) -> bool:
        """True when the VCN already has an IPv6 prefix (Oracle GUA / BYOIP / ULA)."""
        if vcn_obj is None:
            return False
        if isinstance(vcn_obj, dict):
            blocks = vcn_obj.get("ipv6_cidr_blocks") or []
            return bool(blocks)
        return bool(getattr(vcn_obj, "ipv6_cidr_blocks", None) or getattr(vcn_obj, "ipv6_cidr_block", None))

    @staticmethod
    def _open_security_list_rules(*, include_ipv6: bool = False) -> tuple[list, list]:
        """Return (ingress, egress) Security List rules that allow all traffic.

        IPv6 ``::/0`` rules are only included when the VCN is IPv6-enabled —
        OCI rejects IPv6 CIDRs on IPv4-only VCNs.
        """
        ingress = [
            oci.core.models.IngressSecurityRule(
                protocol="all",
                source="0.0.0.0/0",
                source_type="CIDR_BLOCK",
                is_stateless=False,
                description="ocibot open all IPv4 ingress",
            ),
        ]
        egress = [
            oci.core.models.EgressSecurityRule(
                protocol="all",
                destination="0.0.0.0/0",
                destination_type="CIDR_BLOCK",
                is_stateless=False,
                description="ocibot open all IPv4 egress",
            ),
        ]
        if include_ipv6:
            ingress.append(
                oci.core.models.IngressSecurityRule(
                    protocol="all",
                    source="::/0",
                    source_type="CIDR_BLOCK",
                    is_stateless=False,
                    description="ocibot open all IPv6 ingress",
                )
            )
            egress.append(
                oci.core.models.EgressSecurityRule(
                    protocol="all",
                    destination="::/0",
                    destination_type="CIDR_BLOCK",
                    is_stateless=False,
                    description="ocibot open all IPv6 egress",
                )
            )
        return ingress, egress

    def _ensure_internet_gateway(self, vcn_id: str, compartment_id: str) -> Any:
        gateways = oci.pagination.list_call_get_all_results(
            self.network.list_internet_gateways, compartment_id, vcn_id=vcn_id
        ).data
        igw = next(
            (g for g in gateways if getattr(g, "lifecycle_state", "") not in ("TERMINATED", "TERMINATING")),
            None,
        )
        if igw is None:
            igw = self.network.create_internet_gateway(
                oci.core.models.CreateInternetGatewayDetails(
                    compartment_id=compartment_id,
                    vcn_id=vcn_id,
                    is_enabled=True,
                    display_name=DEFAULT_IGW_NAME,
                    freeform_tags={"managed_by": "oci-console-helper"},
                )
            ).data
            igw = self._wait_network_resource(self.network.get_internet_gateway, igw.id)
        elif not bool(getattr(igw, "is_enabled", True)):
            self.network.update_internet_gateway(
                igw.id, oci.core.models.UpdateInternetGatewayDetails(is_enabled=True)
            )
            igw = self.network.get_internet_gateway(igw.id).data
        return igw

    def _ensure_public_route_table(
        self,
        vcn_id: str,
        compartment_id: str,
        igw_id: str,
        *,
        include_ipv6: bool = False,
    ) -> Any:
        tables = oci.pagination.list_call_get_all_results(
            self.network.list_route_tables, compartment_id, vcn_id=vcn_id
        ).data
        # Prefer an existing table that already has 0.0.0.0/0 -> IGW.
        # Do NOT require ::/0 here — IPv4-only VCNs cannot carry that rule.
        for table in tables:
            if getattr(table, "lifecycle_state", "") not in ("AVAILABLE", "PROVISIONING"):
                continue
            rules = list(getattr(table, "route_rules", None) or [])
            if any(
                (getattr(r, "destination", "") or "").strip() == "0.0.0.0/0"
                and (getattr(r, "network_entity_id", "") or "") == igw_id
                for r in rules
            ):
                # Optionally top-up ::/0 if this VCN actually supports IPv6.
                if include_ipv6:
                    existing_dests = {(getattr(r, "destination", "") or "").strip() for r in rules}
                    if "::/0" not in existing_dests:
                        rules.append(
                            oci.core.models.RouteRule(
                                destination="::/0",
                                destination_type="CIDR_BLOCK",
                                network_entity_id=igw_id,
                                description="ocibot IPv6 默认路由",
                            )
                        )
                        self.network.update_route_table(
                            table.id, oci.core.models.UpdateRouteTableDetails(route_rules=rules)
                        )
                        return self.network.get_route_table(table.id).data
                return table

        # Prefer our managed table name (incl. legacy) if present, else first available.
        managed = next(
            (
                t
                for t in tables
                if (getattr(t, "display_name", "") or "") in LEGACY_DEFAULT_RT_NAMES
                and getattr(t, "lifecycle_state", "") not in ("TERMINATED", "TERMINATING")
            ),
            None,
        )
        target = managed or next(
            (t for t in tables if getattr(t, "lifecycle_state", "") == "AVAILABLE"),
            None,
        )
        desired = [
            oci.core.models.RouteRule(
                destination="0.0.0.0/0",
                destination_type="CIDR_BLOCK",
                network_entity_id=igw_id,
                description="ocibot IPv4 默认路由",
            ),
        ]
        if include_ipv6:
            desired.append(
                oci.core.models.RouteRule(
                    destination="::/0",
                    destination_type="CIDR_BLOCK",
                    network_entity_id=igw_id,
                    description="ocibot IPv6 默认路由",
                )
            )
        if target is None:
            return self.network.create_route_table(
                oci.core.models.CreateRouteTableDetails(
                    compartment_id=compartment_id,
                    vcn_id=vcn_id,
                    display_name=DEFAULT_RT_NAME,
                    route_rules=desired,
                    freeform_tags={"managed_by": "oci-console-helper"},
                )
            ).data

        rules = list(getattr(target, "route_rules", None) or [])
        existing_dests = {(getattr(r, "destination", "") or "").strip() for r in rules}
        changed = False
        for rule in desired:
            if rule.destination not in existing_dests:
                rules.append(rule)
                changed = True
        if changed:
            self.network.update_route_table(
                target.id, oci.core.models.UpdateRouteTableDetails(route_rules=rules)
            )
            target = self.network.get_route_table(target.id).data
        return target

    def _ensure_open_security_list(
        self,
        vcn_id: str,
        compartment_id: str,
        *,
        include_ipv6: bool = False,
    ) -> Any:
        lists = oci.pagination.list_call_get_all_results(
            self.network.list_security_lists, compartment_id, vcn_id=vcn_id
        ).data
        managed = next(
            (
                sl
                for sl in lists
                if (getattr(sl, "display_name", "") or "") in LEGACY_DEFAULT_SL_NAMES
                and getattr(sl, "lifecycle_state", "") not in ("TERMINATED", "TERMINATING")
            ),
            None,
        )
        if managed is not None:
            return managed
        ingress, egress = self._open_security_list_rules(include_ipv6=include_ipv6)
        return self.network.create_security_list(
            oci.core.models.CreateSecurityListDetails(
                compartment_id=compartment_id,
                vcn_id=vcn_id,
                display_name=DEFAULT_SL_NAME,
                ingress_security_rules=ingress,
                egress_security_rules=egress,
                freeform_tags={"managed_by": "oci-console-helper"},
            )
        ).data

    def _create_public_subnet(
        self,
        *,
        vcn_id: str,
        compartment_id: str,
        cidr_block: str,
        route_table_id: str,
        security_list_id: str,
        display_name: str = DEFAULT_SUBNET_NAME,
    ) -> Any:
        subnet = self.network.create_subnet(
            oci.core.models.CreateSubnetDetails(
                compartment_id=compartment_id,
                vcn_id=vcn_id,
                cidr_block=cidr_block,
                display_name=display_name,
                route_table_id=route_table_id,
                security_list_ids=[security_list_id],
                prohibit_public_ip_on_vnic=False,
                freeform_tags={"ocibot_managed": "true"},
            )
        ).data
        return self._wait_network_resource(self.network.get_subnet, subnet.id)

    def ensure_default_network(
        self,
        *,
        compartment_id: Optional[str] = None,
        create_if_missing: bool = True,
    ) -> OperationResult:
        """Return a usable VCN + public Subnet, creating them when the tenancy has none.

        Preference order for existing resources:
        1. Any AVAILABLE public subnet (``prohibit_public_ip_on_vnic=False``)
        2. Any AVAILABLE subnet
        3. If a VCN exists without subnets and ``create_if_missing``, create a public
           subnet (+ IGW / route / open security list) under that VCN
        4. If nothing exists and ``create_if_missing``, create a full public stack
           (VCN 10.0.0.0/16, public subnet 10.0.0.0/24, IGW, default routes, open SL)
        """
        compartment = (compartment_id or self.resolve_compartment()).strip()
        try:
            # --- scan existing VCNs / subnets in this compartment first, then tenancy root
            scan_comps: list[str] = []
            for cid in (compartment, self.tenant.tenancy_ocid.strip()):
                if cid and cid not in scan_comps:
                    scan_comps.append(cid)

            vcns: list[dict] = []
            subnets_by_vcn: dict[str, list[dict]] = {}
            seen_vcn: set[str] = set()
            for comp in scan_comps:
                for v in self.list_vcns(comp):
                    if v["id"] in seen_vcn:
                        continue
                    seen_vcn.add(v["id"])
                    vcns.append(v)
            # A VCN whose subnet list could not be read at all. Not the same as a VCN
            # with no subnets, and the difference decides whether the create branch
            # below runs — see the guard after the chosen-subnet block.
            unreadable_vcns: list[str] = []
            for vcn in vcns:
                found: list[dict] = []
                read_errors: list[str] = []
                for comp in (vcn.get("compartment_id"), *scan_comps):
                    if not comp:
                        continue
                    try:
                        found = self.list_subnets(compartment_id=comp, vcn_id=vcn["id"])
                    except Exception as exc:  # noqa: BLE001
                        read_errors.append(str(exc))
                        found = []
                    if found:
                        break
                if not found and read_errors:
                    label = vcn.get("display_name") or vcn["id"][-8:]
                    unreadable_vcns.append(f"{label}：{read_errors[0]}")
                subnets_by_vcn[vcn["id"]] = found

            all_subnets = [s for subs in subnets_by_vcn.values() for s in subs]
            public = [s for s in all_subnets if not s.get("prohibit_public_ip_on_vnic")]
            chosen_subnet = (public or all_subnets or [None])[0]
            if chosen_subnet is not None:
                vcn = next((v for v in vcns if v["id"] == chosen_subnet.get("vcn_id")), None)
                if vcn is None and chosen_subnet.get("vcn_id"):
                    try:
                        vcn = self._vcn_dict(
                            self.network.get_vcn(chosen_subnet["vcn_id"]).data,
                            chosen_subnet.get("compartment_id") or compartment,
                        )
                        if vcn["id"] not in seen_vcn:
                            vcns.append(vcn)
                            subnets_by_vcn.setdefault(vcn["id"], []).append(chosen_subnet)
                    except Exception:
                        vcn = {
                            "id": chosen_subnet.get("vcn_id", ""),
                            "display_name": chosen_subnet.get("vcn_id", "")[-8:],
                            "cidr_block": "",
                            "compartment_id": chosen_subnet.get("compartment_id") or compartment,
                            "ipv6_cidr_blocks": [],
                            "label": chosen_subnet.get("vcn_id", "")[-8:],
                        }
                        vcns.append(vcn)
                        subnets_by_vcn.setdefault(vcn["id"], [chosen_subnet])
                return OperationResult(
                    ok=True,
                    message="已使用现有 VCN / Subnet",
                    data={
                        "created": False,
                        "vcn": vcn,
                        "subnet": chosen_subnet,
                        "vcns": vcns,
                        "subnets_by_vcn": subnets_by_vcn,
                    },
                )

            # Reaching here means "no subnet was chosen". If that is because ListSubnets
            # failed rather than because there is none, creating is the wrong move: the
            # create branch builds a whole second public stack (subnet + IGW + route
            # table + open security list) under the EXISTING VCN. On a non-overlapping
            # CIDR it succeeds and the tenancy silently gains a public stack nobody
            # asked for; on an overlapping one it fails with a CIDR message that hides
            # the throttle underneath. Neither is undoable by the operator from here.
            # Typical trigger: launching while a capacity-retry job hammers the same
            # tenancy's rate limit.
            if unreadable_vcns:
                return OperationResult(
                    ok=False,
                    message="子网列表读取失败，无法确认现有网络（"
                    + "；".join(unreadable_vcns[:2])
                    + "），已中止自动创建，请稍后重试",
                    data={"created": False, "vcns": vcns, "subnets_by_vcn": subnets_by_vcn},
                )

            if not create_if_missing:
                return OperationResult(
                    ok=False,
                    message="当前租户没有可用的 VCN / Subnet",
                    data={"created": False, "vcns": vcns, "subnets_by_vcn": subnets_by_vcn},
                )

            # --- create missing pieces
            created_parts: list[str] = []
            if vcns:
                vcn_info = vcns[0]
                vcn_id = vcn_info["id"]
                vcn_comp = vcn_info.get("compartment_id") or compartment
                vcn_obj = self.network.get_vcn(vcn_id).data
                # list_subnets keeps only AVAILABLE rows, so a subnet that is still
                # PROVISIONING (or UPDATING) reads as "this VCN has no subnets" and
                # lands us here seconds after someone else created one. Re-check
                # without the state filter before adding a duplicate stack.
                try:
                    raw_subnets = oci.pagination.list_call_get_all_results(
                        self.network.list_subnets, compartment_id=vcn_comp, vcn_id=vcn_id
                    ).data or []
                except ServiceError as exc:
                    raise OCIClientError(_format_service_error(exc)) from exc
                transient = [
                    str(getattr(s, "lifecycle_state", "") or "")
                    for s in raw_subnets
                    if str(getattr(s, "lifecycle_state", "") or "")
                    not in ("TERMINATED", "TERMINATING")
                ]
                if transient:
                    return OperationResult(
                        ok=False,
                        message=f"现有 VCN 下已有子网正在创建中（状态 {transient[0]}），"
                        "稍后重试即可使用，未重复创建网络",
                        data={"created": False, "vcns": vcns, "subnets_by_vcn": subnets_by_vcn},
                    )
            else:
                vcn_comp = compartment
                vcn_obj = self.network.create_vcn(
                    oci.core.models.CreateVcnDetails(
                        compartment_id=vcn_comp,
                        cidr_blocks=[DEFAULT_VCN_CIDR],
                        display_name=DEFAULT_VCN_NAME,
                        dns_label=DEFAULT_VCN_DNS_LABEL,
                        is_ipv6_enabled=False,
                        freeform_tags={"managed_by": "oci-console-helper"},
                    )
                ).data
                vcn_obj = self._wait_network_resource(self.network.get_vcn, vcn_obj.id)
                vcn_id = vcn_obj.id
                created_parts.append(f"VCN {DEFAULT_VCN_NAME} ({DEFAULT_VCN_CIDR})")

            # IPv4-only VCNs reject ::/0 route / SL rules (InvalidParameter). Only
            # include IPv6 when the VCN already has an IPv6 prefix.
            include_ipv6 = self._vcn_has_ipv6(vcn_obj)

            igw = self._ensure_internet_gateway(vcn_id, vcn_comp)
            if (getattr(igw, "display_name", "") or "") in LEGACY_DEFAULT_IGW_NAMES:
                # Best-effort signal; may already have existed.
                pass
            created_parts.append("Internet Gateway")

            route_table = self._ensure_public_route_table(
                vcn_id, vcn_comp, igw.id, include_ipv6=include_ipv6
            )
            created_parts.append("公网路由表 (0.0.0.0/0)")

            security_list = self._ensure_open_security_list(
                vcn_id, vcn_comp, include_ipv6=include_ipv6
            )
            created_parts.append("开放 Security List")

            # Pick a free /24 under the VCN CIDR when possible.
            vcn_cidr = (
                getattr(vcn_obj, "cidr_block", None)
                or (list(getattr(vcn_obj, "cidr_blocks", None) or []) or [DEFAULT_VCN_CIDR])[0]
            )
            subnet_cidr = DEFAULT_SUBNET_CIDR
            try:
                network = ipaddress.ip_network(vcn_cidr, strict=False)
                # Prefer first /24 of the VCN.
                if network.prefixlen <= 24:
                    subnet_cidr = str(next(network.subnets(new_prefix=24)))
                else:
                    subnet_cidr = str(network)
            except Exception:
                subnet_cidr = DEFAULT_SUBNET_CIDR

            subnet_obj = self._create_public_subnet(
                vcn_id=vcn_id,
                compartment_id=vcn_comp,
                cidr_block=subnet_cidr,
                route_table_id=route_table.id,
                security_list_id=security_list.id,
            )
            created_parts.append(f"公网 Subnet {DEFAULT_SUBNET_NAME} ({subnet_cidr})")

            vcn_info = self._vcn_dict(vcn_obj, vcn_comp)
            subnet_info = self._subnet_dict(subnet_obj, vcn_comp)
            # Refresh list views so the launch wizard dropdowns are complete.
            if vcn_info["id"] not in seen_vcn:
                vcns.append(vcn_info)
            else:
                # Replace placeholder entry if we just created under an existing VCN.
                vcns = [vcn_info if v["id"] == vcn_info["id"] else v for v in vcns]
            subnets_by_vcn[vcn_info["id"]] = [subnet_info]

            return OperationResult(
                ok=True,
                message="已自动创建默认网络：" + "、".join(created_parts),
                data={
                    "created": True,
                    "vcn": vcn_info,
                    "subnet": subnet_info,
                    "vcns": vcns,
                    "subnets_by_vcn": subnets_by_vcn,
                    "igw_id": igw.id,
                    "route_table_id": route_table.id,
                    "security_list_id": security_list.id,
                },
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except OCIClientError as exc:
            return OperationResult(ok=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def launch_instance(
        self,
        *,
        display_name: str,
        compartment_id: str,
        availability_domain: str,
        shape: str,
        image_id: str,
        subnet_id: str,
        ssh_public_key: str = "",
        root_password: str = "",
        auth_mode: str = "key",
        ocpus: Optional[float] = None,
        memory_in_gbs: Optional[float] = None,
        assign_public_ip: bool = True,
        assign_ipv6_ip: bool = False,
        boot_volume_size_in_gbs: Optional[int] = None,
        boot_volume_vpus_per_gb: int = 10,
        nsg_ids: Optional[list[str]] = None,
        open_guest_firewall: bool = True,
        launch_token: str = "",
        custom_user_data: str = "",
        idempotency_key: str = "",
    ) -> OperationResult:
        """Launch a VM with controlled root authentication metadata.

        custom_user_data: optional first-boot shell script; passed in memory only
        and merged into the generated cloud-init (never persisted in payloads).

        idempotency_key: sent as ``opc-retry-token``. Oracle remembers it for 24
        hours and replays the original outcome instead of creating a second
        machine, which is the difference between "the response was lost" and "I
        now own two instances". Without it a lost response — a proxy timeout, a
        dropped connection — leaves the operator no safe way to find out whether
        the launch happened except to look, and looking races the retry.

        Empty means no token, preserving the previous behaviour for callers that
        have not been taught to supply one.
        """
        try:
            payload = sanitize_launch_payload(
                {
                    "display_name": display_name,
                    "compartment_id": compartment_id,
                    "availability_domain": availability_domain,
                    "shape": shape,
                    "image_id": image_id,
                    "subnet_id": subnet_id,
                    "ssh_public_key": ssh_public_key,
                    "auth_mode": auth_mode,
                    "ocpus": ocpus,
                    "memory_in_gbs": memory_in_gbs,
                    "assign_public_ip": assign_public_ip,
                    "assign_ipv6_ip": assign_ipv6_ip,
                    "boot_volume_size_in_gbs": boot_volume_size_in_gbs,
                    "boot_volume_vpus_per_gb": boot_volume_vpus_per_gb,
                    "nsg_ids": nsg_ids or [],
                    "open_guest_firewall": open_guest_firewall,
                    "launch_token": launch_token,
                }
            )
            metadata: dict[str, str] = {}
            if auth_mode == "key":
                metadata["ssh_authorized_keys"] = ssh_public_key.strip()
            metadata["user_data"] = build_root_cloud_init(
                auth_mode=auth_mode,
                ssh_public_key=ssh_public_key,
                root_password=root_password,
                open_guest_firewall=open_guest_firewall,
                custom_boot_script=custom_user_data,
            )
            shape_config = None
            if shape.lower().endswith(".flex"):
                shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=ocpus,
                    memory_in_gbs=memory_in_gbs,
                )
            # Note: Always-Free boot volumes ignore boot_volume_vpus_per_gb at launch
            # (OCI provisions them as Balanced/10). The requested VPU is applied
            # afterward by editing the boot volume — see resize_boot_volume().
            source_details = oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id,
                boot_volume_size_in_gbs=boot_volume_size_in_gbs,
                boot_volume_vpus_per_gb=payload["boot_volume_vpus_per_gb"],
            )
            create_vnic = oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=payload["assign_public_ip"],
                assign_ipv6_ip=payload["assign_ipv6_ip"],
                nsg_ids=payload["nsg_ids"] or None,
            )
            tags = {"ocibot_managed": "true", "ocibot_ssh_user": "root"}
            if launch_token:
                tags["ocibot_launch_token"] = launch_token
            # Password mode: store the chosen root password on the instance freeform
            # tags so the panel can show it later. Anyone with instance-read can see it.
            if auth_mode == "password" and (root_password or "").strip():
                # OCI freeform tag values max 256 chars; our generator stays well under.
                tags[ROOT_PASSWORD_TAG] = (root_password or "").strip()[:256]
            details = oci.core.models.LaunchInstanceDetails(
                display_name=display_name.strip() or None,
                compartment_id=compartment_id,
                availability_domain=availability_domain,
                shape=shape,
                shape_config=shape_config,
                source_details=source_details,
                create_vnic_details=create_vnic,
                metadata=metadata,
                freeform_tags=tags,
            )
            # No SDK retry on launch: OutOfHostCapacity and 429 must surface once
            # so the capacity-retry job can apply its own interval / cooldown.
            #
            # The retry token is a different mechanism and does not conflict with
            # that: it does not retry anything, it only makes a repeat of THIS
            # request return the original result. Oracle scopes it to successful
            # creates, so a genuine OutOfHostCapacity failure still leaves the
            # token free for the next attempt.
            launch_kwargs: dict[str, Any] = {"retry_strategy": sdk_no_retry_strategy()}
            token = _clean_retry_token(idempotency_key)
            if token:
                launch_kwargs["opc_retry_token"] = token
            resp = self.compute.launch_instance(details, **launch_kwargs)
            inst = resp.data
            wr = resp.headers.get("opc-work-request-id", "") if hasattr(resp, "headers") else ""
            return OperationResult(
                ok=True,
                message=f"已提交创建实例：{getattr(inst, 'display_name', display_name)}",
                work_request_id=wr or "",
                data={
                    "instance_id": inst.id,
                    "lifecycle_state": inst.lifecycle_state,
                    "managed_nsg_id": (nsg_ids or [""])[0],
                },
            )
        except ServiceError as exc:
            return OperationResult(
                ok=False,
                message=_format_service_error(exc),
                data={
                    "capacity": is_capacity_error(exc),
                    "rate_limited": is_rate_limit_error(exc),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def launch_from_payload(
        self,
        payload: dict,
        *,
        root_password: str = "",
        custom_user_data: str = "",
        idempotency_key: str = "",
    ) -> OperationResult:
        """Launch from a secret-free payload; password / user-data only in memory."""
        clean = sanitize_launch_payload(payload, for_retry=not bool(root_password))
        return self.launch_instance(
            display_name=clean.get("display_name", DEFAULT_INSTANCE_NAME),
            compartment_id=clean["compartment_id"],
            availability_domain=clean["availability_domain"],
            shape=clean["shape"],
            image_id=clean["image_id"],
            subnet_id=clean["subnet_id"],
            ssh_public_key=clean.get("ssh_public_key", ""),
            root_password=root_password,
            auth_mode=clean.get("auth_mode", "key"),
            ocpus=clean.get("ocpus"),
            memory_in_gbs=clean.get("memory_in_gbs"),
            assign_public_ip=clean["assign_public_ip"],
            assign_ipv6_ip=clean["assign_ipv6_ip"],
            boot_volume_size_in_gbs=clean.get("boot_volume_size_in_gbs"),
            boot_volume_vpus_per_gb=clean["boot_volume_vpus_per_gb"],
            nsg_ids=clean.get("nsg_ids"),
            open_guest_firewall=clean["open_guest_firewall"],
            launch_token=clean.get("launch_token", ""),
            custom_user_data=custom_user_data,
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _to_instance_info(self, inst: Any) -> InstanceInfo:
        ocpus = None
        memory = None
        shape_cfg = {}
        sc = getattr(inst, "shape_config", None)
        if sc is not None:
            ocpus = getattr(sc, "ocpus", None)
            memory = getattr(sc, "memory_in_gbs", None)
            try:
                shape_cfg = oci.util.to_dict(sc)
            except Exception:
                shape_cfg = {}
        # ISO 8601 with the "T" separator, like every other time_created this API
        # returns. Plain str(datetime) yields "2026-01-01 00:00:00+00:00", and a
        # space-separated stamp with an offset is not something Date() is required
        # to parse — Chrome accepts it, Safari does not.
        time_created = self._ts_iso(getattr(inst, "time_created", None))
        return InstanceInfo(
            id=inst.id,
            display_name=inst.display_name or inst.id,
            lifecycle_state=inst.lifecycle_state,
            region=self.tenant.region,
            availability_domain=getattr(inst, "availability_domain", "") or "",
            fault_domain=getattr(inst, "fault_domain", "") or "",
            shape=getattr(inst, "shape", "") or "",
            ocpus=ocpus,
            memory_gb=memory,
            time_created=time_created,
            compartment_id=getattr(inst, "compartment_id", "") or "",
            image_id=getattr(inst, "image_id", "") or "",
            freeform_tags=getattr(inst, "freeform_tags", None) or {},
            defined_tags=getattr(inst, "defined_tags", None) or {},
            tenant_id=self.tenant.id,
            tenant_name=self.tenant.name,
            shape_config_raw=shape_cfg,
            monitoring_disabled=_agent_monitoring_disabled(inst),
            raw=inst,
        )

    def resolve_primary_network(
        self,
        instance_id: str,
        compartment_id: str,
        *,
        include_resource_details: bool = False,
    ) -> PrimaryNetworkInfo:
        """Resolve the primary VNIC, optionally including private/public IP resource OCIDs."""
        attachments = oci.pagination.list_call_get_all_results(
            self.compute.list_vnic_attachments,
            compartment_id=compartment_id,
            instance_id=instance_id,
        ).data
        attachments = sorted(
            attachments,
            key=lambda item: getattr(item, "lifecycle_state", "") != "ATTACHED",
        )
        chosen = None
        for attachment in attachments:
            vnic_id = getattr(attachment, "vnic_id", "") or ""
            if not vnic_id:
                continue
            vnic = self.network.get_vnic(vnic_id).data
            if chosen is None or bool(getattr(vnic, "is_primary", False)):
                chosen = vnic
            if bool(getattr(vnic, "is_primary", False)):
                break
        if chosen is None:
            raise OCIClientError("实例没有可用的主 VNIC")
        info = PrimaryNetworkInfo(
            vnic_id=chosen.id,
            subnet_id=getattr(chosen, "subnet_id", "") or "",
            private_ipv4=getattr(chosen, "private_ip", "") or "",
            public_ipv4=getattr(chosen, "public_ip", "") or "",
            ipv6_addresses=list(getattr(chosen, "ipv6_addresses", None) or []),
            nsg_ids=list(getattr(chosen, "nsg_ids", None) or []),
        )
        if not include_resource_details:
            return info
        private_ips = oci.pagination.list_call_get_all_results(
            self.network.list_private_ips,
            vnic_id=info.vnic_id,
        ).data
        private_ip = next(
            (item for item in private_ips if bool(getattr(item, "is_primary", False))),
            private_ips[0] if private_ips else None,
        )
        if private_ip is None:
            raise OCIClientError("无法解析实例的主 Private IP 资源")
        info.private_ip_id = private_ip.id
        info.private_ipv4 = getattr(private_ip, "ip_address", "") or info.private_ipv4
        info.private_ip_compartment_id = getattr(private_ip, "compartment_id", "") or compartment_id
        try:
            details = oci.core.models.GetPublicIpByPrivateIpIdDetails(private_ip_id=private_ip.id)
            public_ip = self.network.get_public_ip_by_private_ip_id(details).data
            info.public_ip_id = getattr(public_ip, "id", "") or ""
            info.public_ipv4 = getattr(public_ip, "ip_address", "") or info.public_ipv4
            info.public_ip_lifetime = getattr(public_ip, "lifetime", "") or ""
        except ServiceError as exc:
            if getattr(exc, "status", None) != 404:
                raise
        return info

    def _resolve_primary_ips(self, instance_id: str, compartment_id: str) -> tuple[str, str]:
        network = self.resolve_primary_network(instance_id, compartment_id)
        return network.private_ipv4, network.public_ipv4

    @staticmethod
    def _open_all_specs(include_ipv6: bool) -> list[FirewallRuleSpec]:
        specs = [
            FirewallRuleSpec("INGRESS", "all", "0.0.0.0/0", description="ocibot IPv4 全开放入站"),
            FirewallRuleSpec("EGRESS", "all", "0.0.0.0/0", description="ocibot IPv4 全开放出站"),
        ]
        if include_ipv6:
            specs.extend(
                [
                    FirewallRuleSpec("INGRESS", "all", "::/0", description="ocibot IPv6 全开放入站"),
                    FirewallRuleSpec("EGRESS", "all", "::/0", description="ocibot IPv6 全开放出站"),
                ]
            )
        return specs

    @staticmethod
    def _is_ocibot_managed_nsg(tags: Optional[dict] = None) -> bool:
        """Recognize NSGs created by this tool (legacy + current tag schemes)."""
        tags = tags or {}
        managed_by = str(tags.get("managed_by", "")).lower()
        return managed_by in {"oci-console-helper", "true"} or str(
            tags.get("ocibot_managed", "")
        ).lower() == "true"

    @staticmethod
    def _firewall_protocol_label(protocol: str) -> str:
        key = str(protocol or "").strip().lower()
        return {
            "all": "全部协议",
            "6": "TCP",
            "17": "UDP",
            "1": "ICMPv4",
            "58": "ICMPv6",
        }.get(key, f"协议 {protocol}" if protocol else "未知协议")

    @staticmethod
    def _firewall_direction_label(direction: str) -> str:
        key = str(direction or "").strip().upper()
        return {"INGRESS": "入站", "EGRESS": "出站"}.get(key, direction or "未知方向")

    @staticmethod
    def _firewall_rule_model(spec: FirewallRuleSpec) -> Any:
        spec.validate()
        direction = spec.direction.upper()
        protocol = str(spec.protocol).lower()
        # OCI accepts "all" or the IANA protocol number as a string.
        if protocol == "all":
            protocol = "all"
        options: dict[str, Any] = {}
        if protocol in {"6", "17"} and (spec.port_min is not None or spec.port_max is not None):
            start = int(spec.port_min if spec.port_min is not None else spec.port_max)
            end = int(spec.port_max if spec.port_max is not None else start)
            port_range = oci.core.models.PortRange(min=start, max=end)
            option_type = oci.core.models.TcpOptions if protocol == "6" else oci.core.models.UdpOptions
            options["tcp_options" if protocol == "6" else "udp_options"] = option_type(
                destination_port_range=port_range
            )
        network = str(ipaddress.ip_network(spec.cidr, strict=False))
        if direction == "INGRESS":
            options.update(source=network, source_type="CIDR_BLOCK")
        else:
            options.update(destination=network, destination_type="CIDR_BLOCK")
        return oci.core.models.AddSecurityRuleDetails(
            direction=direction,
            protocol=protocol,
            is_stateless=bool(spec.stateless),
            description=(spec.description or "")[:255] or None,
            **options,
        )

    def add_nsg_rules(self, nsg_id: str, specs: list[FirewallRuleSpec]) -> OperationResult:
        try:
            models = [self._firewall_rule_model(spec) for spec in specs]
            for offset in range(0, len(models), 25):
                details = oci.core.models.AddNetworkSecurityGroupSecurityRulesDetails(
                    security_rules=models[offset : offset + 25]
                )
                self.network.add_network_security_group_security_rules(nsg_id, details)
            return OperationResult(
                ok=True,
                message=f"已新增 {len(models)} 条防火墙规则",
                data={"count": len(models)},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def create_managed_nsg(
        self,
        *,
        vcn_id: str,
        compartment_id: str,
        display_name: str,
        include_ipv6: bool = False,
        launch_token: str = "",
    ) -> OperationResult:
        token = launch_token or uuid.uuid4().hex
        safe_name = (display_name or "instance").strip() or "instance"
        details = oci.core.models.CreateNetworkSecurityGroupDetails(
            compartment_id=compartment_id,
            vcn_id=vcn_id,
            display_name=f"nsg-{safe_name}"[:100],
            freeform_tags={
                "managed_by": "oci-console-helper",
                "ocibot_managed": "true",
                "launch_token": token,
            },
        )
        try:
            nsg = self.network.create_network_security_group(details).data
            added = self.add_nsg_rules(nsg.id, self._open_all_specs(include_ipv6))
            if not added.ok:
                try:
                    self.network.delete_network_security_group(nsg.id)
                except Exception:
                    pass
                return added
            return OperationResult(
                ok=True,
                message="已创建实例专属网络安全组，并开放全部入站/出站规则",
                data={"nsg_id": nsg.id, "launch_token": token},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def delete_managed_nsg(self, nsg_id: str) -> OperationResult:
        try:
            nsg = self.network.get_network_security_group(nsg_id).data
            tags = getattr(nsg, "freeform_tags", None) or {}
            if not self._is_ocibot_managed_nsg(tags):
                return OperationResult(ok=False, message="拒绝删除：该网络安全组不是本工具创建的")
            self.network.delete_network_security_group(nsg_id)
            return OperationResult(ok=True, message="已删除未使用的专属网络安全组")
        except ServiceError as exc:
            if getattr(exc, "status", None) == 404:
                return OperationResult(ok=True, message="网络安全组已不存在")
            return OperationResult(ok=False, message=_format_service_error(exc))

    def ensure_instance_nsg(self, instance_id: str, compartment_id: str) -> OperationResult:
        try:
            network = self.resolve_primary_network(instance_id, compartment_id)
            if network.nsg_ids:
                return OperationResult(
                    ok=True,
                    message="实例已有网络安全组（NSG）",
                    data={"nsg_ids": network.nsg_ids},
                )
            subnet = self.network.get_subnet(network.subnet_id).data
            created = self.create_managed_nsg(
                vcn_id=subnet.vcn_id,
                compartment_id=subnet.compartment_id,
                display_name=f"{instance_id[-8:]}-firewall",
                include_ipv6=bool(network.ipv6_addresses),
            )
            if not created.ok:
                return created
            nsg_id = created.data["nsg_id"]
            try:
                self.network.update_vnic(
                    network.vnic_id,
                    oci.core.models.UpdateVnicDetails(nsg_ids=[nsg_id]),
                )
            except Exception:
                self.delete_managed_nsg(nsg_id)
                raise
            return OperationResult(
                ok=True,
                message="已创建并挂载实例专属网络安全组",
                data={"nsg_ids": [nsg_id]},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def get_instance_firewall(self, instance_id: str, compartment_id: str) -> OperationResult:
        try:
            network = self.resolve_primary_network(instance_id, compartment_id)
            groups = []
            for nsg_id in network.nsg_ids:
                nsg = self.network.get_network_security_group(nsg_id).data
                rules = oci.pagination.list_call_get_all_results(
                    self.network.list_network_security_group_security_rules,
                    nsg_id,
                ).data
                tags = getattr(nsg, "freeform_tags", None) or {}
                groups.append(
                    {
                        "id": nsg_id,
                        "display_name": getattr(nsg, "display_name", "") or nsg_id[-8:],
                        "is_managed": self._is_ocibot_managed_nsg(tags),
                        "rules": [self._normalize_firewall_rule(rule) for rule in rules],
                    }
                )
            # Most instances have NO NSG on the VNIC — OCI's default networking puts
            # ingress/egress rules on the SUBNET's security list instead. Reading
            # only nsg_ids therefore showed an empty firewall panel for any instance
            # not launched with this panel's managed NSG. Security lists are reported
            # read-only: the add/delete endpoints operate on NSGs.
            security_lists = self._subnet_security_lists(network.subnet_id)

            parts = []
            if groups:
                parts.append(f"{len(groups)} 个网络安全组（NSG）")
            if security_lists:
                parts.append(f"{len(security_lists)} 个子网安全列表（只读）")
            if parts:
                message = "已加载 " + " · ".join(parts)
            else:
                message = "该实例既未关联 NSG，其子网也没有安全列表规则"

            return OperationResult(
                ok=True,
                message=message,
                data={
                    "groups": groups,
                    "security_lists": security_lists,
                    "subnet_id": network.subnet_id,
                    "has_ipv6": bool(network.ipv6_addresses),
                    "vnic_id": network.vnic_id,
                    "public_ipv4": network.public_ipv4 or "",
                    "private_ipv4": network.private_ipv4 or "",
                    "ipv6_addresses": list(network.ipv6_addresses or []),
                },
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def _subnet_security_lists(self, subnet_id: str) -> list[dict]:
        """Security-list rules for a subnet, normalized like NSG rules (read-only)."""
        if not subnet_id:
            return []
        out: list[dict] = []
        try:
            subnet = self.network.get_subnet(subnet_id).data
        except Exception:  # noqa: BLE001 - best effort; NSGs are still returned
            return out
        for sl_id in list(getattr(subnet, "security_list_ids", None) or []):
            try:
                sl = self.network.get_security_list(sl_id).data
            except Exception:  # noqa: BLE001
                continue
            rules: list[dict] = []
            for rule in list(getattr(sl, "ingress_security_rules", None) or []):
                rules.append(self._normalize_firewall_rule(rule, direction="INGRESS"))
            for rule in list(getattr(sl, "egress_security_rules", None) or []):
                rules.append(self._normalize_firewall_rule(rule, direction="EGRESS"))
            out.append(
                {
                    "id": sl_id,
                    "display_name": getattr(sl, "display_name", "") or sl_id[-8:],
                    "rules": rules,
                }
            )
        return out

    @staticmethod
    def _normalize_firewall_rule(rule: Any, direction: str = "") -> dict:
        options = getattr(rule, "tcp_options", None) or getattr(rule, "udp_options", None)
        port_range = getattr(options, "destination_port_range", None) if options else None
        port = "全部"
        if port_range:
            start, end = getattr(port_range, "min", None), getattr(port_range, "max", None)
            port = str(start) if start == end else f"{start}-{end}"
        # Security-list rules carry no `direction` attribute — it is implied by which
        # list (ingress_security_rules / egress_security_rules) they came from — so
        # the caller passes it in. NSG rules have it on the object.
        direction = direction or (getattr(rule, "direction", "") or "")
        protocol = getattr(rule, "protocol", "") or ""
        return {
            "id": getattr(rule, "id", "") or "",
            "direction": direction,
            "direction_label": TenantSession._firewall_direction_label(direction),
            "protocol": protocol,
            "protocol_label": TenantSession._firewall_protocol_label(protocol),
            "cidr": getattr(rule, "source", None) or getattr(rule, "destination", None) or "",
            "port": port,
            "stateless": bool(getattr(rule, "is_stateless", False)),
            "description": getattr(rule, "description", "") or "",
        }

    def add_instance_firewall_rule(self, nsg_id: str, spec: FirewallRuleSpec) -> OperationResult:
        return self.add_nsg_rules(nsg_id, [spec])

    def delete_nsg_rules(self, nsg_id: str, rule_ids: list[str]) -> OperationResult:
        ids = [value for value in rule_ids if value]
        try:
            for offset in range(0, len(ids), 25):
                details = oci.core.models.RemoveNetworkSecurityGroupSecurityRulesDetails(
                    security_rule_ids=ids[offset : offset + 25]
                )
                self.network.remove_network_security_group_security_rules(nsg_id, details)
            return OperationResult(
                ok=True,
                message=f"已删除 {len(ids)} 条防火墙规则",
                data={"count": len(ids)},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def replace_instance_firewall_with_open_all(
        self,
        instance_id: str,
        compartment_id: str,
    ) -> OperationResult:
        """Clear every attached NSG rule, then open all ports.

        - No attached NSG → create a managed one first
        - Has IPv6 on the primary VNIC → open IPv4 + IPv6
        - No IPv6 → open IPv4 only
        """
        state = self.get_instance_firewall(instance_id, compartment_id)
        if not state.ok:
            return state

        include_ipv6 = bool((state.data or {}).get("has_ipv6"))
        families = "IPv4 + IPv6" if include_ipv6 else "IPv4"
        groups = list((state.data or {}).get("groups") or [])

        # No NSG attached: create a managed open-all NSG and attach it.
        if not groups:
            created = self.ensure_instance_nsg(instance_id, compartment_id)
            if not created.ok:
                return OperationResult(
                    ok=False,
                    message=f"一键开启失败（创建网络安全组）：{created.message}",
                    data={"include_ipv6": include_ipv6},
                )
            # ensure_instance_nsg already writes open-all rules for the current
            # IPv6 state; re-open explicitly so the message is consistent even
            # if the instance gained/lost addresses mid-flight.
            state = self.get_instance_firewall(instance_id, compartment_id)
            if not state.ok:
                return state
            include_ipv6 = bool((state.data or {}).get("has_ipv6"))
            families = "IPv4 + IPv6" if include_ipv6 else "IPv4"
            groups = list((state.data or {}).get("groups") or [])
            if not groups:
                return OperationResult(
                    ok=False,
                    message="已创建网络安全组但未挂载到实例，请刷新后重试",
                )

        results = []
        all_ok = True
        total_removed = 0
        for group in groups:
            ids = [rule["id"] for rule in group["rules"] if rule.get("id")]
            removed = self.delete_nsg_rules(group["id"], ids) if ids else OperationResult(
                ok=True, message="无旧规则", data={"count": 0}
            )
            if removed.ok:
                total_removed += int((removed.data or {}).get("count") or len(ids))
            added = (
                self.add_nsg_rules(group["id"], self._open_all_specs(include_ipv6))
                if removed.ok
                else OperationResult(ok=False, message="删除规则失败，未写入全开放规则")
            )
            all_ok = all_ok and removed.ok and added.ok
            results.append(
                {
                    "nsg_id": group["id"],
                    "removed": removed.message,
                    "added": added.message,
                    "ok": removed.ok and added.ok,
                }
            )

        if all_ok:
            message = (
                f"已一键开启所有端口（{families} 入站/出站，全部协议）"
                f"：清空 {total_removed} 条旧规则，写入 {len(self._open_all_specs(include_ipv6))} 条全开放规则"
            )
        else:
            detail = "；".join(
                f"…{item['nsg_id'][-8:]} 删除={item['removed']} / 写入={item['added']}"
                for item in results
            )
            message = f"一键开启部分失败，请刷新后重试。{detail}"
        return OperationResult(
            ok=all_ok,
            message=message,
            data={
                "results": results,
                "include_ipv6": include_ipv6,
                "families": families,
                "removed": total_removed,
            },
        )

    def get_boot_volume_info(self, instance_id: str, compartment_id: str) -> OperationResult:
        """Return the current boot volume size + performance for an instance.

        Note: OCI exposes provisioned capacity / VPU / hydration state only.
        Guest-OS used/free filesystem space is not available via the API.
        """
        try:
            inst = self.compute.get_instance(instance_id).data
            ad = getattr(inst, "availability_domain", "") or ""
            bv_id = self._find_boot_volume_id(instance_id, compartment_id, ad, wait=False)
            if not bv_id:
                return OperationResult(ok=False, message="未找到实例的引导卷")
            bv = self.blockstorage.get_boot_volume(bv_id).data
            vpu = int(getattr(bv, "vpus_per_gb", 10) or 10)
            if vpu <= 10:
                perf_label = "平衡"
            elif vpu <= 20:
                perf_label = "较高性能"
            else:
                perf_label = "超高性能"
            time_created = getattr(bv, "time_created", None)
            if time_created is not None and hasattr(time_created, "isoformat"):
                time_created_s = time_created.isoformat()
            else:
                time_created_s = str(time_created or "")
            is_hydrated = getattr(bv, "is_hydrated", None)
            return OperationResult(
                ok=True,
                message="已读取引导卷信息",
                data={
                    "boot_volume_id": bv_id,
                    "size_in_gbs": int(getattr(bv, "size_in_gbs", 0) or 0),
                    "vpus_per_gb": vpu,
                    "performance_label": perf_label,
                    "lifecycle_state": getattr(bv, "lifecycle_state", "") or "",
                    "display_name": getattr(bv, "display_name", "") or "",
                    "availability_domain": getattr(bv, "availability_domain", "") or ad,
                    "is_hydrated": None if is_hydrated is None else bool(is_hydrated),
                    "time_created": time_created_s,
                    # Explicit: not available from OCI control plane
                    "usage_note": "OCI 接口只能查看配置容量与性能，无法读取系统内已用/剩余磁盘空间。",
                },
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def list_boot_volumes(
        self,
        *,
        compartment_id: Optional[str] = None,
        include_subcompartments: bool = True,
        include_attachments: bool = True,
    ) -> OperationResult:
        """List boot volumes under a compartment (and optionally its subtree).

        Also maps attached instance OCID/name when possible so the UI can show
        which volume belongs to which VM (or is orphaned).
        """
        root = (compartment_id or self.resolve_compartment()).strip()
        comps: list[str] = [root]
        # A failed enumeration silently shrinks the sweep to the root compartment.
        # get_free_quota_usage reads data["errors"] to set read_incomplete, so this
        # has to end up there or a storage undercount is reported as authoritative.
        enum_error = ""
        if include_subcompartments:
            try:
                comps = [c["id"] for c in self.list_compartments(parent_id=root, subtree=True, strict=True)]
                if root not in comps:
                    comps.insert(0, root)
            except Exception as exc:  # noqa: BLE001
                comps = [root]
                enum_error = f"子区间枚举失败：{exc}"

        # AD list needed by list_boot_volumes API
        try:
            ads = self.list_availability_domains()
        except Exception:
            ads = []
        if not ads:
            # Some tenancies still accept empty AD filter via compartment-only listing
            ads = [""]

        volumes: list[dict] = []
        seen: set[str] = set()
        errors: list[str] = []

        def _perf_label(vpu: int) -> str:
            if vpu <= 10:
                return "平衡"
            if vpu <= 20:
                return "较高性能"
            return "超高性能"

        def _ts(value: Any) -> str:
            if value is None:
                return ""
            if hasattr(value, "isoformat"):
                try:
                    return value.isoformat()
                except Exception:
                    return str(value)
            return str(value)

        for cid in comps:
            for ad in ads:
                try:
                    kwargs: dict[str, Any] = {"compartment_id": cid}
                    if ad:
                        kwargs["availability_domain"] = ad
                    resp = oci.pagination.list_call_get_all_results(
                        self.blockstorage.list_boot_volumes,
                        **kwargs,
                    )
                    for bv in resp.data or []:
                        vid = getattr(bv, "id", "") or ""
                        if not vid or vid in seen:
                            continue
                        state = str(getattr(bv, "lifecycle_state", "") or "")
                        if state in {"TERMINATED", "TERMINATING"}:
                            continue
                        seen.add(vid)
                        vpu = int(getattr(bv, "vpus_per_gb", 10) or 10)
                        is_hydrated = getattr(bv, "is_hydrated", None)
                        volumes.append(
                            {
                                "id": vid,
                                "display_name": getattr(bv, "display_name", "") or vid[-12:],
                                "size_in_gbs": int(getattr(bv, "size_in_gbs", 0) or 0),
                                "vpus_per_gb": vpu,
                                "performance_label": _perf_label(vpu),
                                "lifecycle_state": state,
                                "availability_domain": getattr(bv, "availability_domain", "") or ad,
                                "compartment_id": getattr(bv, "compartment_id", "") or cid,
                                "is_hydrated": None if is_hydrated is None else bool(is_hydrated),
                                "time_created": _ts(getattr(bv, "time_created", None)),
                                "instance_id": "",
                                "instance_name": "",
                                "attachment_state": "",
                            }
                        )
                except ServiceError as exc:
                    errors.append(_format_service_error(exc))
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))

        # Attachment map: boot_volume_id -> instance
        if include_attachments and volumes:
            # Collect ADs/comps from volumes
            by_key: dict[tuple[str, str], list[dict]] = {}
            for v in volumes:
                key = (v.get("availability_domain") or "", v.get("compartment_id") or root)
                by_key.setdefault(key, []).append(v)

            attach_map: dict[str, dict[str, str]] = {}
            for (ad, cid), _group in by_key.items():
                if not ad:
                    continue
                try:
                    atts = oci.pagination.list_call_get_all_results(
                        self.compute.list_boot_volume_attachments,
                        ad,
                        cid,
                    ).data
                except Exception:
                    atts = []
                for att in atts or []:
                    bvid = getattr(att, "boot_volume_id", "") or ""
                    iid = getattr(att, "instance_id", "") or ""
                    if not bvid:
                        continue
                    attach_map[bvid] = {
                        "instance_id": iid,
                        "attachment_state": str(getattr(att, "lifecycle_state", "") or ""),
                    }

            # Resolve instance display names (best-effort, bounded)
            name_cache: dict[str, str] = {}
            for bvid, info in attach_map.items():
                iid = info.get("instance_id") or ""
                if not iid or iid in name_cache:
                    continue
                try:
                    inst = self.compute.get_instance(iid).data
                    name_cache[iid] = str(getattr(inst, "display_name", "") or iid[-12:])
                except Exception:
                    name_cache[iid] = iid[-12:]

            for v in volumes:
                info = attach_map.get(v["id"]) or {}
                iid = info.get("instance_id") or ""
                v["instance_id"] = iid
                v["instance_name"] = name_cache.get(iid, "")
                v["attachment_state"] = info.get("attachment_state") or ""

        volumes.sort(
            key=lambda x: (
                0 if x.get("instance_id") else 1,
                str(x.get("display_name") or "").lower(),
            )
        )
        total_gb = sum(int(v.get("size_in_gbs") or 0) for v in volumes)
        attached = sum(1 for v in volumes if v.get("instance_id"))
        orphaned = len(volumes) - attached
        msg = f"共 {len(volumes)} 个引导卷 · 合计 {total_gb} GB · 已挂载 {attached} · 未挂载 {orphaned}"
        if errors and not volumes:
            return OperationResult(ok=False, message="; ".join(errors[:3]), data={"volumes": [], "summary": {}})
        # Folded in AFTER the "nothing was readable" branch on purpose: an unreadable
        # compartment list must not turn a genuinely empty compartment into a hard
        # failure on the volumes page, but it must still mark the read partial.
        if enum_error:
            errors.append(enum_error)
        if errors:
            msg += f"（部分 compartment 读取失败 {len(errors)} 处）"
        return OperationResult(
            ok=True,
            message=msg,
            data={
                "volumes": volumes,
                "summary": {
                    "count": len(volumes),
                    "total_gb": total_gb,
                    "attached": attached,
                    "orphaned": orphaned,
                },
                "errors": errors[:10],
            },
        )

    def get_free_quota_usage(
        self,
        *,
        free_only_mode: bool = True,
        include_block: bool = True,
        include_egress: bool = False,
    ) -> OperationResult:
        """Aggregate Always-Free usage (compute + storage) for the quota dashboard.

        Reuses app.free_quota.build_quota_snapshot so the same caps/thresholds apply
        everywhere. Returns the snapshot dict in ``.data``.

        ``include_egress`` is off by default: it costs an extra Monitoring query and
        outbound traffic can never block a create, so the launch guard and the worker
        (which take this snapshot on every attempt) skip it. Read-only dashboards
        turn it on.
        """
        from app import free_quota

        notes: list[str] = []
        # Tracks whether any read that feeds a *cap* (compute / block storage) came
        # back incomplete. Deliberately separate from `notes`, which also carries
        # benign informational messages such as the object-storage approximation
        # warning — gating on "any notes" would block legitimate launches.
        read_incomplete = False

        self._last_tree_errors = []
        try:
            instances = self.list_instances_tree(resolve_ips=False)
            if self._last_tree_errors:
                read_incomplete = True
                # Name the first cause: a count alone cannot tell "one compartment
                # was throttled" (retry) apart from "the subtree could not be
                # enumerated at all" (an IAM policy the operator has to fix).
                notes.append(
                    f"部分区间实例读取失败（{len(self._last_tree_errors)} 处）：{self._last_tree_errors[0]}"
                )
        except Exception as exc:  # noqa: BLE001
            instances = []
            read_incomplete = True
            notes.append(f"实例读取失败：{exc}")

        volumes: list[dict[str, Any]] = []
        try:
            bv = self.list_boot_volumes(include_subcompartments=True, include_attachments=True)
            data = bv.data if isinstance(bv.data, dict) else {}
            if not bv.ok or (data.get("errors") or []):
                read_incomplete = True
                notes.append("引导卷读取不完整")
            for v in data.get("volumes", []) or []:
                volumes.append({**v, "kind": "boot"})
        except Exception as exc:  # noqa: BLE001
            read_incomplete = True
            notes.append(f"引导卷读取失败：{exc}")

        if include_block:
            try:
                blk = self.list_block_volumes(include_subcompartments=True, include_attachments=True)
                data = blk.data if isinstance(blk.data, dict) else {}
                if not blk.ok or (data.get("errors") or []):
                    read_incomplete = True
                    notes.append("块存储卷读取不完整")
                for v in data.get("volumes", []) or []:
                    volumes.append({**v, "kind": "block"})
            except Exception as exc:  # noqa: BLE001
                read_incomplete = True
                notes.append(f"块存储卷读取失败：{exc}")

        object_usage: dict[str, Any] = {}
        try:
            est = self.estimate_object_storage_usage()
            if isinstance(est.data, dict):
                object_usage = est.data
                if est.message:
                    notes.append(est.message)
            if not est.ok and est.message:
                notes.append(est.message)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"对象存储读取失败：{exc}")

        egress_usage: dict[str, Any] = {}
        if include_egress:
            try:
                egress = self.get_network_egress_usage()
                if egress.ok and isinstance(egress.data, dict):
                    egress_usage = egress.data
                    if egress.data.get("note"):
                        notes.append(str(egress.data["note"]))
                elif egress.message:
                    # Informational only — an unreadable egress figure must not set
                    # read_incomplete, which would fail-closed on every launch.
                    notes.append(f"出网流量读取失败：{egress.message}")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"出网流量读取失败：{exc}")

        snapshot = free_quota.build_quota_snapshot(
            instances=instances,
            volumes=volumes,
            free_only_mode=free_only_mode,
            account_tier=getattr(self.tenant, "account_tier", "") or "",
            notes=notes,
            object_usage=object_usage,
            egress_usage=egress_usage,
        )
        # Consumed by web.backend.quota_guard to fail closed instead of treating an
        # undercount as free headroom.
        snapshot["read_incomplete"] = bool(read_incomplete)
        return OperationResult(ok=True, message="", data=snapshot)

    def _find_boot_volume_id(self, instance_id: str, compartment_id: str, availability_domain: str, *, wait: bool = True, timeout: int = 150) -> str:
        """Find the boot volume OCID for an instance, optionally waiting for attachment."""
        deadline = time.monotonic() + (timeout if wait else 0)
        while True:
            try:
                attachments = self.compute.list_boot_volume_attachments(
                    availability_domain, compartment_id, instance_id=instance_id
                ).data
            except ServiceError:
                attachments = []
            att = next((a for a in attachments if getattr(a, "boot_volume_id", "")), None)
            if att:
                return att.boot_volume_id
            if time.monotonic() >= deadline:
                return ""
            time.sleep(3)

    def resize_boot_volume(
        self,
        instance_id: str,
        compartment_id: str,
        *,
        size_in_gbs: Optional[int] = None,
        vpus_per_gb: Optional[int] = None,
        wait_for_volume: bool = True,
        timeout: int = 180,
        hydration_timeout: int = 1500,
    ) -> OperationResult:
        """Edit the instance's boot volume size and/or performance (VPUs/GB).

        This is the reliable way to raise boot-volume performance: OCI provisions
        Always-Free boot volumes as Balanced (10) at launch and ignores the
        launch-time VPU, so the value must be applied by updating the volume.

        VPU updates are rejected with [409] "vpus may not be updated while
        hydrating" until the volume finishes copying image data. We wait for
        ``is_hydrated`` and retry 409-hydrating conflicts with backoff.
        """
        if size_in_gbs is None and vpus_per_gb is None:
            return OperationResult(ok=False, message="未指定新的大小或性能")
        if vpus_per_gb is not None and vpus_per_gb not in (10, 20) and not 30 <= int(vpus_per_gb) <= 120:
            return OperationResult(ok=False, message="性能必须为 10、20 或 30–120 VPUs/GB")
        if size_in_gbs is not None and not 50 <= int(size_in_gbs) <= 32768:
            return OperationResult(ok=False, message="引导卷大小必须在 50–32768 GB 之间")
        try:
            inst = self.compute.get_instance(instance_id).data
            ad = getattr(inst, "availability_domain", "") or ""
            bv_id = self._find_boot_volume_id(instance_id, compartment_id, ad, wait=wait_for_volume, timeout=timeout)
            if not bv_id:
                return OperationResult(ok=False, message="未找到实例的引导卷（可能仍在创建中，稍后在详情里重试）")
            # A volume must be AVAILABLE before it accepts an update.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = getattr(self.blockstorage.get_boot_volume(bv_id).data, "lifecycle_state", "")
                if state == "AVAILABLE":
                    break
                if state in ("FAULTY", "TERMINATED", "TERMINATING"):
                    return OperationResult(ok=False, message=f"引导卷状态为 {state}，无法调整")
                time.sleep(3)
            hydration_deadline = time.monotonic() + hydration_timeout
            if vpus_per_gb is not None:
                # Wait for hydration to finish before touching VPUs.
                while time.monotonic() < hydration_deadline:
                    bv = self.blockstorage.get_boot_volume(bv_id).data
                    if bool(getattr(bv, "is_hydrated", True)):
                        break
                    time.sleep(15)
            details = oci.core.models.UpdateBootVolumeDetails()
            if size_in_gbs is not None:
                details.size_in_gbs = int(size_in_gbs)
            if vpus_per_gb is not None:
                details.vpus_per_gb = int(vpus_per_gb)
            while True:
                try:
                    self.blockstorage.update_boot_volume(bv_id, details)
                    break
                except ServiceError as exc:
                    blob = f"{getattr(exc, 'code', '')} {getattr(exc, 'message', '')}".lower()
                    # is_hydrated can flip true slightly before OCI accepts the
                    # update — retry the 409 race instead of failing.
                    if getattr(exc, "status", None) == 409 and "hydrat" in blob and time.monotonic() < hydration_deadline:
                        time.sleep(15)
                        continue
                    if getattr(exc, "status", None) == 409 and "hydrat" in blob:
                        return OperationResult(
                            ok=False,
                            message="引导卷仍在从镜像同步数据（hydrating），等待超时。请几分钟后在「调整引导卷…」里重试。",
                        )
                    raise
            parts = []
            if size_in_gbs is not None:
                parts.append(f"{int(size_in_gbs)} GB")
            if vpus_per_gb is not None:
                parts.append(f"{int(vpus_per_gb)} VPUs/GB")
            return OperationResult(
                ok=True,
                message="引导卷已调整："
                + " · ".join(parts)
                + "（控制面已更新；访客文件系统需 SSH 扩展或手动 growpart/resize2fs）",
                data={"boot_volume_id": bv_id},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def update_instance_shape(self, instance_id: str, ocpus: float, memory_in_gbs: float) -> OperationResult:
        """Change OCPU / memory of a running Flex-shape instance."""
        try:
            inst = self.compute.get_instance(instance_id).data
            shape = getattr(inst, "shape", "") or ""
            if not shape.lower().endswith(".flex"):
                return OperationResult(ok=False, message="仅弹性（Flex）规格支持修改 OCPU / 内存")
            ocpus = float(ocpus)
            memory_in_gbs = float(memory_in_gbs)
            if ocpus <= 0 or memory_in_gbs <= 0:
                return OperationResult(ok=False, message="OCPU 与内存必须大于 0")
            details = oci.core.models.UpdateInstanceDetails(
                shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
                    ocpus=ocpus, memory_in_gbs=memory_in_gbs
                )
            )
            self.compute.update_instance(instance_id, details)
            return OperationResult(
                ok=True,
                message=f"已提交规格变更：{ocpus:g} OCPU / {memory_in_gbs:g} GB（部分变更需重启生效）",
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    @staticmethod
    def _subnet_ipv6_blocks(subnet: Any) -> list[str]:
        blocks: list[str] = []
        single = getattr(subnet, "ipv6_cidr_block", "") or ""
        if single:
            blocks.append(str(single))
        for block in list(getattr(subnet, "ipv6_cidr_blocks", None) or []):
            if block and str(block) not in blocks:
                blocks.append(str(block))
        return blocks

    @staticmethod
    def _vcn_ipv6_blocks(vcn: Any) -> list[str]:
        blocks: list[str] = []
        single = getattr(vcn, "ipv6_cidr_block", "") or ""
        if single:
            blocks.append(str(single))
        for block in list(getattr(vcn, "ipv6_cidr_blocks", None) or []):
            if block and str(block) not in blocks:
                blocks.append(str(block))
        return blocks

    def _wait_vcn_ipv6(self, vcn_id: str, *, timeout: float = 120) -> Any:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.network.get_vcn(vcn_id).data
            if self._vcn_ipv6_blocks(last):
                return last
            state = getattr(last, "lifecycle_state", "") or ""
            if state in ("TERMINATED", "TERMINATING", "FAULTY"):
                raise OCIClientError(f"VCN 状态异常：{state}")
            time.sleep(2)
        raise OCIClientError("等待 VCN 启用 IPv6 前缀超时")

    def _wait_subnet_ipv6(self, subnet_id: str, *, timeout: float = 120) -> Any:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.network.get_subnet(subnet_id).data
            if self._subnet_ipv6_blocks(last):
                return last
            state = getattr(last, "lifecycle_state", "") or ""
            if state in ("TERMINATED", "TERMINATING", "FAULTY"):
                raise OCIClientError(f"Subnet 状态异常：{state}")
            time.sleep(2)
        raise OCIClientError("等待 Subnet 启用 IPv6 前缀超时")

    def _pick_subnet_ipv6_cidr(self, vcn: Any, subnet_id: str, compartment_id: str) -> str:
        """Choose an unused /64 from the VCN Oracle GUA /56 for this subnet."""
        vcn_blocks = self._vcn_ipv6_blocks(vcn)
        if not vcn_blocks:
            raise OCIClientError("VCN 尚未分配 IPv6 前缀")
        parent = ipaddress.IPv6Network(vcn_blocks[0], strict=False)
        if parent.prefixlen > 64:
            raise OCIClientError(f"VCN IPv6 前缀过小，无法划分子网：{parent}")

        used: set[str] = set()
        try:
            siblings = oci.pagination.list_call_get_all_results(
                self.network.list_subnets,
                compartment_id=compartment_id,
                vcn_id=getattr(vcn, "id", "") or "",
            ).data
        except Exception:
            siblings = []
        for sibling in siblings:
            for block in self._subnet_ipv6_blocks(sibling):
                try:
                    used.add(str(ipaddress.IPv6Network(block, strict=False)))
                except Exception:
                    used.add(str(block))

        if parent.prefixlen == 64:
            candidate = str(parent)
            if candidate in used and subnet_id:
                # Already assigned somewhere; allow reuse only if it is this subnet.
                for sibling in siblings:
                    if getattr(sibling, "id", "") == subnet_id and candidate in {
                        str(ipaddress.IPv6Network(b, strict=False)) for b in self._subnet_ipv6_blocks(sibling)
                    }:
                        return candidate
            if candidate not in used:
                return candidate
            raise OCIClientError(f"VCN IPv6 前缀 {parent} 已被占用")

        for subnet_net in parent.subnets(new_prefix=64):
            candidate = str(subnet_net)
            if candidate not in used:
                return candidate
        raise OCIClientError(f"VCN IPv6 前缀 {parent} 下已无可用 /64 子网段")

    def ensure_subnet_ipv6(self, subnet_id: str, compartment_id: str = "") -> OperationResult:
        """Ensure the subnet has an IPv6 prefix and public internet routing."""
        try:
            subnet = self.network.get_subnet(subnet_id).data
            vcn_id = getattr(subnet, "vcn_id", "") or ""
            if not vcn_id:
                return OperationResult(ok=False, message="无法解析子网所属 VCN")
            vcn = self.network.get_vcn(vcn_id).data
            vcn_comp = getattr(vcn, "compartment_id", "") or compartment_id or getattr(subnet, "compartment_id", "")
            subnet_comp = getattr(subnet, "compartment_id", "") or vcn_comp
            parts: list[str] = []
            vcn_created = False
            subnet_created = False

            if not self._vcn_ipv6_blocks(vcn):
                self.network.add_ipv6_vcn_cidr(
                    vcn_id,
                    add_vcn_ipv6_cidr_details=oci.core.models.AddVcnIpv6CidrDetails(
                        is_oracle_gua_allocation_enabled=True
                    ),
                )
                vcn = self._wait_vcn_ipv6(vcn_id)
                vcn_created = True
                parts.append(f"VCN IPv6 {self._vcn_ipv6_blocks(vcn)[0]}")
            elif getattr(vcn, "lifecycle_state", "") != "AVAILABLE":
                vcn = self._wait_vcn_ipv6(vcn_id)

            if not self._subnet_ipv6_blocks(subnet):
                subnet_cidr = self._pick_subnet_ipv6_cidr(vcn, subnet_id, subnet_comp)
                self.network.add_ipv6_subnet_cidr(
                    subnet_id,
                    oci.core.models.AddSubnetIpv6CidrDetails(ipv6_cidr_block=subnet_cidr),
                )
                subnet = self._wait_subnet_ipv6(subnet_id)
                subnet_created = True
                parts.append(f"Subnet IPv6 {subnet_cidr}")
            elif getattr(subnet, "lifecycle_state", "") != "AVAILABLE":
                subnet = self._wait_subnet_ipv6(subnet_id)

            route = self.ensure_ipv6_internet_access(subnet_id, vcn_comp or subnet_comp)
            created = vcn_created or subnet_created
            data = {
                "created": created,
                "vcn_created": vcn_created,
                "subnet_created": subnet_created,
                "subnet_id": subnet_id,
                "vcn_id": vcn_id,
                "ipv6_cidr_blocks": self._subnet_ipv6_blocks(subnet),
                "route_ok": route.ok,
                "route": route.data,
            }
            if not route.ok:
                prefix_note = "已启用 IPv6 前缀，但" if created else "IPv6 前缀已存在，但"
                return OperationResult(
                    ok=False,
                    message=prefix_note + "公网路由配置失败：" + route.message,
                    data=data,
                )
            if (route.data or {}).get("changed"):
                parts.append(route.message)

            if created:
                detail = "、".join(parts) if parts else "已就绪"
                message = "已自动启用 IPv6：" + detail
            else:
                message = "Subnet IPv6 与公网路由已就绪"
            return OperationResult(ok=True, message=message, data=data)
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except OCIClientError as exc:
            return OperationResult(ok=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def remove_public_ipv6(self, instance_id: str, compartment_id: str) -> OperationResult:
        """Delete the IPv6 address(es) on the instance's primary VNIC.

        Scope is deliberately just the VNIC's own addresses. The subnet's /64,
        the VCN's GUA prefix and the ``::/0`` route are SHARED network resources
        that assign_public_ipv6 may have created — other instances in the same
        subnet can be relying on them, so tearing them down here would take those
        machines off IPv6 as a side effect of one instance's change. Undoing the
        network-level enablement is a VCN-level decision, not this button.

        Idempotent: an instance with no IPv6 reports success rather than an
        error, so a double click does not produce a scary message.
        """
        try:
            network = self.resolve_primary_network(instance_id, compartment_id)
            if not network.vnic_id:
                return OperationResult(ok=False, message="找不到实例的主 VNIC")
            existing = oci.pagination.list_call_get_all_results(
                self.network.list_ipv6s, vnic_id=network.vnic_id
            ).data or []
            if not existing:
                return OperationResult(
                    ok=True, message="该实例当前没有 IPv6，无需取消", data={"removed": []}
                )
            removed: list[str] = []
            failures: list[str] = []
            for entry in existing:
                address = str(getattr(entry, "ip_address", "") or "")
                try:
                    self.network.delete_ipv6(entry.id)
                    removed.append(address)
                except ServiceError as exc:
                    failures.append(f"{address}: {_format_service_error(exc)}")
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{address}: {exc}")
            if failures and not removed:
                return OperationResult(
                    ok=False,
                    message="取消 IPv6 失败：" + "；".join(failures),
                    data={"removed": [], "failed": failures},
                )
            message = "已取消 IPv6：" + "、".join(removed)
            if failures:
                # Partial result stated plainly — reporting a clean success while
                # an address is still attached would send the operator away
                # believing the instance is off IPv6 when it is not.
                message += f"；仍有 {len(failures)} 个未能删除：" + "；".join(failures)
            return OperationResult(
                ok=not failures,
                message=message
                + "（子网/VCN 的 IPv6 前缀与路由保持不变，其他实例不受影响）",
                data={"removed": removed, "failed": failures},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def assign_public_ipv6(self, instance_id: str, compartment_id: str) -> OperationResult:
        """Assign a public IPv6 to the primary VNIC and open internet routing.

        If the subnet/VCN has no IPv6 prefix yet, automatically enables an
        Oracle-assigned GUA on the VCN and a /64 on the subnet, then ensures
        Internet Gateway + ``::/0`` so the address is publicly reachable.
        """
        try:
            network = self.resolve_primary_network(instance_id, compartment_id)
            enable = self.ensure_subnet_ipv6(network.subnet_id, compartment_id)
            if not enable.ok:
                return OperationResult(
                    ok=False,
                    message="无法启用 Subnet IPv6：" + enable.message,
                    data=enable.data,
                )
            enable_note = enable.message if enable.data and enable.data.get("created") else ""

            # Re-read VNIC addresses after possible network changes.
            network = self.resolve_primary_network(instance_id, compartment_id)
            if network.ipv6_addresses:
                address = ", ".join(network.ipv6_addresses)
                route = self.ensure_ipv6_internet_access(network.subnet_id, compartment_id)
                suffix = f"；{route.message}" if route.ok else f"；⚠ 公网路由设置失败：{route.message}"
                if enable_note:
                    suffix = f"；{enable_note}" + suffix
                return OperationResult(
                    ok=True,
                    message=f"实例已有 IPv6：{address}{suffix}",
                    data={"ipv6": network.ipv6_addresses, "route_ok": route.ok, "enabled": enable.data},
                )
            details = oci.core.models.CreateIpv6Details(vnic_id=network.vnic_id)
            ipv6 = self.network.create_ipv6(details).data
            address = getattr(ipv6, "ip_address", "") or ""
            route = self.ensure_ipv6_internet_access(network.subnet_id, compartment_id)
            suffix = f"；{route.message}" if route.ok else (
                f"；⚠ 公网路由设置失败，可能仅内网可用：{route.message}"
            )
            if enable_note:
                suffix = f"；{enable_note}" + suffix
            return OperationResult(
                ok=True,
                message=f"已分配公网 IPv6：{address}{suffix}",
                data={"ipv6": address, "route_ok": route.ok, "enabled": enable.data},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def ensure_ipv6_internet_access(self, subnet_id: str, compartment_id: str) -> OperationResult:
        """Ensure a subnet can reach the public internet over IPv6.

        Guarantees the subnet's VCN has an enabled Internet Gateway and that the
        subnet's route table carries a ``::/0`` rule pointing at it. Existing
        rules are preserved. This is what turns an assigned GUA IPv6 from
        intranet-only into publicly reachable.
        """
        try:
            subnet = self.network.get_subnet(subnet_id).data
            vcn_id = getattr(subnet, "vcn_id", "") or ""
            route_table_id = getattr(subnet, "route_table_id", "") or ""
            if not vcn_id or not route_table_id:
                return OperationResult(ok=False, message="无法解析子网的 VCN / 路由表")
            vcn = self.network.get_vcn(vcn_id).data
            vcn_compartment = getattr(vcn, "compartment_id", "") or compartment_id

            # 1) Ensure an enabled Internet Gateway on the VCN.
            gateways = oci.pagination.list_call_get_all_results(
                self.network.list_internet_gateways, vcn_compartment, vcn_id=vcn_id
            ).data
            igw = next(
                (g for g in gateways if getattr(g, "lifecycle_state", "") not in ("TERMINATED", "TERMINATING")),
                None,
            )
            created_igw = False
            enabled_igw = False
            if igw is None:
                igw = self.network.create_internet_gateway(
                    oci.core.models.CreateInternetGatewayDetails(
                        compartment_id=vcn_compartment,
                        vcn_id=vcn_id,
                        is_enabled=True,
                        display_name=DEFAULT_IGW_NAME,
                        freeform_tags={"managed_by": "oci-console-helper"},
                    )
                ).data
                igw = self._wait_network_resource(self.network.get_internet_gateway, igw.id)
                created_igw = True
            elif not bool(getattr(igw, "is_enabled", True)):
                self.network.update_internet_gateway(
                    igw.id, oci.core.models.UpdateInternetGatewayDetails(is_enabled=True)
                )
                igw = self.network.get_internet_gateway(igw.id).data
                enabled_igw = True

            # 2) Ensure the only ::/0 route targets this VCN's Internet Gateway.
            route_table = self.network.get_route_table(route_table_id).data
            rules = list(getattr(route_table, "route_rules", None) or [])
            v6_defaults = [
                r for r in rules if (getattr(r, "destination", "") or "").strip() == "::/0"
            ]
            correct_v6_default = any(
                (getattr(r, "network_entity_id", "") or "") == igw.id for r in v6_defaults
            )
            changed_route = False
            corrected_route = False
            if not correct_v6_default or len(v6_defaults) > 1:
                rules = [
                    r for r in rules if (getattr(r, "destination", "") or "").strip() != "::/0"
                ]
                rules.append(
                    oci.core.models.RouteRule(
                        destination="::/0",
                        destination_type="CIDR_BLOCK",
                        network_entity_id=igw.id,
                        description="ocibot IPv6 默认路由",
                    )
                )
                self.network.update_route_table(
                    route_table_id,
                    oci.core.models.UpdateRouteTableDetails(route_rules=rules),
                )
                changed_route = True
                corrected_route = bool(v6_defaults)

            parts = []
            if created_igw:
                parts.append("已创建 Internet Gateway")
            elif enabled_igw:
                parts.append("已启用 Internet Gateway")
            if changed_route:
                parts.append("已修正 ::/0 默认路由" if corrected_route else "已添加 ::/0 默认路由")
            detail = ("（" + "，".join(parts) + "）") if parts else "（已存在，无需修改）"
            return OperationResult(
                ok=True,
                message="IPv6 公网路由已就绪" + detail,
                data={
                    "igw_id": igw.id,
                    "route_table_id": route_table_id,
                    "changed": bool(parts),
                    "created_igw": created_igw,
                    "enabled_igw": enabled_igw,
                    "changed_route": changed_route,
                    "corrected_route": corrected_route,
                },
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))


    def list_console_connections(self, instance_id: str, compartment_id: str) -> list[Any]:
        """List the instance's live console connections.

        Raises instead of returning ``[]`` on a failed read — same reasoning as
        delete_console_connection below. An empty list is a factual claim ("this
        instance has no console connection") that the UI renders and that
        create_console_connection uses to decide nothing needs cleaning up; a
        throttled or unauthorized read is neither.
        """
        try:
            items = oci.pagination.list_call_get_all_results(
                self.compute.list_instance_console_connections,
                compartment_id,
                instance_id=instance_id,
            ).data
        except ServiceError as exc:
            raise OCIClientError(_format_service_error(exc)) from exc
        return [c for c in items if getattr(c, "lifecycle_state", "") not in ("DELETED", "DELETING")]

    def delete_console_connection(self, console_connection_id: str) -> OperationResult:
        """Delete a console connection.

        Returns a result instead of swallowing the error: the caller reported
        success unconditionally, so a refused delete looked like it worked while the
        entry stayed in the list.
        """
        try:
            self.compute.delete_instance_console_connection(console_connection_id)
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        return OperationResult(ok=True, message="已删除控制台连接")

    def create_console_connection(
        self, instance_id: str, compartment_id: str, ssh_public_key: str
    ) -> OperationResult:
        """Create a serial + VNC console connection, returning the SSH commands to run."""
        key = (ssh_public_key or "").strip()
        if not re.match(r"^(ssh-(?:rsa|ed25519)|ecdsa-sha2-[^ ]+)\s+\S+", key):
            return OperationResult(ok=False, message="需要有效的 SSH 公钥才能创建控制台连接")
        try:
            # A new connection must use our key; remove any stale ones first.
            # Oracle allows exactly one active console connection per instance, so
            # skipping this cleanup because the read failed makes the create below
            # fail with "already exists" — an error that says nothing about the
            # throttle or missing permission that actually caused it.
            try:
                stale = self.list_console_connections(instance_id, compartment_id)
            except OCIClientError as exc:
                return OperationResult(
                    ok=False, message=f"无法确认实例现有的控制台连接，已中止创建：{exc}"
                )
            for existing in stale:
                self.delete_console_connection(existing.id)
            details = oci.core.models.CreateInstanceConsoleConnectionDetails(
                instance_id=instance_id, public_key=key
            )
            conn = self.compute.create_instance_console_connection(details).data
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                conn = self.compute.get_instance_console_connection(conn.id).data
                state = getattr(conn, "lifecycle_state", "")
                if state == "ACTIVE":
                    break
                if state in ("FAILED", "DELETED", "DELETING"):
                    return OperationResult(ok=False, message=f"控制台连接创建失败（状态 {state}）")
                time.sleep(3)
            return OperationResult(
                ok=True,
                message="控制台连接已就绪",
                data={
                    "id": conn.id,
                    "serial": getattr(conn, "connection_string", "") or "",
                    "vnc": getattr(conn, "vnc_connection_string", "") or "",
                },
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def get_instance_metrics(self, instance_id: str, compartment_id: str, hours: int = 3) -> OperationResult:
        """Fetch CPU / memory / network time series from the Monitoring service.

        Network series are **bytes/sec** (MQL ``.rate()`` on cumulative counters).
        CPU / memory are utilization percentages.
        """
        from datetime import datetime, timedelta, timezone

        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=max(1, int(hours)))
        # CpuUtilization / MemoryUtilization: percent (0–100), use mean over 1m.
        # NetworksBytesIn/Out: cumulative byte counters from the compute agent.
        # `.mean()` on counters is meaningless (looks like huge B/s). `.rate()`
        # yields average bytes/sec over each interval — what the UI expects.
        queries = {
            "cpu": 'CpuUtilization[1m]{resourceId = "%s"}.mean()' % instance_id,
            "memory": 'MemoryUtilization[1m]{resourceId = "%s"}.mean()' % instance_id,
            "net_in": 'NetworksBytesIn[1m]{resourceId = "%s"}.rate()' % instance_id,
            "net_out": 'NetworksBytesOut[1m]{resourceId = "%s"}.rate()' % instance_id,
        }
        series: dict[str, list] = {}
        any_data = False
        for key, query in queries.items():
            try:
                details = oci.monitoring.models.SummarizeMetricsDataDetails(
                    namespace="oci_computeagent",
                    query=query,
                    start_time=start,
                    end_time=end,
                )
                resp = self.monitoring.summarize_metrics_data(compartment_id, details).data
                points = []
                if resp:
                    # Multiple series can appear (e.g. multi-VNIC). Sum concurrent
                    # datapoints at the same timestamp so the chart is total traffic.
                    bucket: dict[str, float] = {}
                    order: list = []
                    for metric in resp:
                        for dp in getattr(metric, "aggregated_datapoints", None) or []:
                            ts = getattr(dp, "timestamp", None)
                            val = float(getattr(dp, "value", 0) or 0)
                            if not isinstance(val, float) or val != val:  # NaN
                                continue
                            # Clamp absurd negatives (counter resets) to 0 for rates.
                            if key.startswith("net") and val < 0:
                                val = 0.0
                            key_ts = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                            if key_ts not in bucket:
                                order.append(ts)
                                bucket[key_ts] = 0.0
                            bucket[key_ts] += val
                    for ts in order:
                        key_ts = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                        points.append((ts, bucket[key_ts]))
                    points.sort(key=lambda p: (p[0] is None, p[0]))
                series[key] = points
                any_data = any_data or bool(points)
            except ServiceError:
                series[key] = []
            except Exception:  # noqa: BLE001
                series[key] = []
        return OperationResult(
            ok=True,
            message="已获取监控数据" if any_data else "暂无监控数据（实例需启用计算代理 / 监控插件）",
            data={
                "series": series,
                "hours": hours,
                "has_data": any_data,
                "units": {
                    "cpu": "percent",
                    "memory": "percent",
                    "net_in": "bytes_per_sec",
                    "net_out": "bytes_per_sec",
                },
            },
        )

    def detect_account_tier(self) -> OperationResult:
        """Classify Always Free vs PAYG using the subscription record only.

        Intentionally avoids ``get_tenancy``, Service Limits, and home-region
        resolution — those are extra API calls not needed for the sidebar badge.
        """
        region = (self.tenant.region or "").strip()
        regions = [region] if region else None
        sub_verdict, sub_note, sub_details = self._detect_subscription_tier(regions=regions)
        info = {
            "tier": "未知",
            "tier_code": "unknown",
            "tier_reason": "",
            "subscription": sub_details,
        }
        if sub_verdict == "paid":
            info["tier"] = "已升级 / 付费（PAYG）"
            info["tier_code"] = "paid"
            info["tier_reason"] = f"订阅记录显示为付费账号。依据：{sub_note}"
        elif sub_verdict == "free":
            info["tier"] = "Always Free / 未升级"
            info["tier_code"] = "free"
            info["tier_reason"] = f"订阅记录显示为免费层级。依据：{sub_note}"
        else:
            info["tier"] = "无法确定"
            info["tier_code"] = "unknown"
            info["tier_reason"] = (
                f"未能读取到订阅记录，因此无法判定账号等级。（{sub_note}）"
                "如需准确判定，请给当前用户授予订阅读取权限："
                "Allow group <你的组> to inspect subscriptions in tenancy"
            )
        return OperationResult(ok=True, message="已读取订阅等级", data=info)

    def get_usage_summary(self, days: int = 30) -> OperationResult:
        """Summarize daily COST from Usage API (best-effort; needs usage-report rights).

        Returns daily totals + service breakdown when available. Free accounts often
        have no usage data or lack permission — returns ok with empty series + note.
        """
        from datetime import datetime, timedelta, timezone

        days = max(1, min(int(days or 30), 90))
        if self._usage is None:
            return OperationResult(
                ok=False,
                message="Usage API 客户端不可用（SDK 未安装 usage_api 或初始化失败）",
                # month_to_date is None, not 0: for a cost figure those two mean
                # very different things, and a page that prints 0.00 for a read it
                # never managed to perform is worse than one that prints nothing.
                data={
                    "daily": [],
                    "by_service": [],
                    "total": 0,
                    "currency": "",
                    "days": days,
                    "month_to_date": None,
                },
            )
        now = datetime.now(timezone.utc)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        start = end - timedelta(days=days)
        # Calendar month-to-date is a different question from "the last N days",
        # and the two only coincide by accident. Asking Oracle for one window that
        # covers BOTH keeps this to a single call — spending a second call on it
        # would compete with the capacity retry loop for the same rate limit.
        #
        # The window aggregates below still use `start`, so `total`, `daily` and
        # `by_service` keep meaning exactly what they meant before; only the new
        # month figure reads from the wider range.
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        query_start = min(start, month_start)
        window_day = start.date().isoformat()
        month_day = month_start.date().isoformat()
        tenancy = self.tenant.tenancy_ocid.strip()
        try:
            details = oci.usage_api.models.RequestSummarizedUsagesDetails(
                tenant_id=tenancy,
                time_usage_started=query_start,
                time_usage_ended=end,
                granularity="DAILY",
                query_type="COST",
                group_by=["service", "currency"],
            )
            resp = self._usage.request_summarized_usages(details)
            items = list(getattr(resp, "data", None).items if getattr(resp, "data", None) else []) or list(
                getattr(resp, "data", None) or []
            )
            # Some SDK versions return .data as SummarizedUsageCollection with .items
            if hasattr(resp, "data") and hasattr(resp.data, "items"):
                items = list(resp.data.items or [])
            daily_map: dict[str, float] = {}
            service_map: dict[str, float] = {}
            currency = ""
            total = 0.0
            month_total = 0.0
            for it in items:
                # cost fields vary by query_type
                cost = getattr(it, "computed_amount", None)
                if cost is None:
                    cost = getattr(it, "attributed_cost", None)
                if cost is None:
                    cost = getattr(it, "unit_price", None) or 0
                try:
                    amount = float(cost or 0)
                except (TypeError, ValueError):
                    amount = 0.0
                cur = str(getattr(it, "currency", "") or getattr(it, "currency_code", "") or "")
                if cur and not currency:
                    currency = cur
                svc = str(getattr(it, "service", "") or getattr(it, "service_name", "") or "Other")
                ts = getattr(it, "time_usage_started", None) or getattr(it, "time_usage_ended", None)
                if ts is not None:
                    day = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
                else:
                    day = "unknown"
                # ISO dates compare correctly as strings. "unknown" sorts above any
                # real date, so it is excluded explicitly rather than being counted
                # into whichever bucket the comparison happened to put it in.
                dated = day != "unknown"
                if dated and day >= month_day:
                    month_total += amount
                # Window aggregates: unchanged semantics, so a widened query does
                # not quietly inflate the "最近 N 天" total the page has always shown.
                if dated and day < window_day:
                    continue
                total += amount
                service_map[svc] = service_map.get(svc, 0.0) + amount
                daily_map[day] = daily_map.get(day, 0.0) + amount
            daily = [{"date": d, "amount": round(v, 4)} for d, v in sorted(daily_map.items())]
            by_service = [
                {"service": k, "amount": round(v, 4)}
                for k, v in sorted(service_map.items(), key=lambda kv: kv[1], reverse=True)
            ][:20]
            return OperationResult(
                ok=True,
                message="已获取用量/费用汇总" if daily else "暂无账单数据（免费账号或无 Usage 权限时常见）",
                data={
                    "daily": daily,
                    "by_service": by_service,
                    "total": round(total, 4),
                    "currency": currency or "USD",
                    "days": days,
                    "time_start": start.isoformat(),
                    "time_end": end.isoformat(),
                    # Calendar month to date, in UTC — Oracle bills on UTC, so a
                    # local-midnight boundary would disagree with the invoice.
                    "month_to_date": round(month_total, 4),
                    "month_start": month_start.date().isoformat(),
                },
            )
        except ServiceError as exc:
            return OperationResult(
                ok=False,
                message=_format_service_error(exc),
                # month_to_date is None, not 0: for a cost figure those two mean
                # very different things, and a page that prints 0.00 for a read it
                # never managed to perform is worse than one that prints nothing.
                data={
                    "daily": [],
                    "by_service": [],
                    "total": 0,
                    "currency": "",
                    "days": days,
                    "month_to_date": None,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return OperationResult(
                ok=False,
                message=str(exc),
                # month_to_date is None, not 0: for a cost figure those two mean
                # very different things, and a page that prints 0.00 for a read it
                # never managed to perform is worse than one that prints nothing.
                data={
                    "daily": [],
                    "by_service": [],
                    "total": 0,
                    "currency": "",
                    "days": days,
                    "month_to_date": None,
                },
            )

    def list_invoices(self, limit: int = 24) -> OperationResult:
        """List billing invoices with their payment status (OSP Gateway).

        This is not the Usage API: usage tells you what a month *cost*, invoices
        tell you what Oracle billed and whether it was settled. Only the invoice
        service knows the latter.

        An Always Free / trial tenancy has no subscription and therefore no
        invoices — that returns ok with an empty list and a note, because "no
        bills" is the correct answer for such an account, not a failure.
        """
        from datetime import timezone

        tenancy = (self.tenant.tenancy_ocid or "").strip()
        if not tenancy:
            return OperationResult(ok=False, message="缺少 Tenancy OCID", data={"invoices": []})
        try:
            from oci.osp_gateway import InvoiceServiceClient
        except Exception as exc:  # noqa: BLE001
            return OperationResult(
                ok=False,
                message=f"当前 OCI SDK 不含 osp_gateway 模块（{exc}）",
                data={"invoices": []},
            )

        limit = max(1, min(int(limit or 24), 100))
        try:
            home = self._home_region() or self.tenant.region
        except Exception:
            home = self.tenant.region
        cfg = dict(self._config)
        if home:
            cfg["region"] = home
        try:
            client = InvoiceServiceClient(cfg, retry_strategy=sdk_default_retry_strategy())
            # "INVOICE_DATE" is the billing period this table is ordered by, and it
            # is one of the values the service accepts — the enum is small and
            # closed, so it is asserted rather than assumed. Sending anything else
            # is rejected client-side by the SDK before a request is even made.
            resp = client.list_invoices(
                osp_home_region=home,
                compartment_id=tenancy,
                limit=limit,
                sort_by=_INVOICE_SORT_BY,
                sort_order="DESC",
            )
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            # A tenancy with no subscription answers 404/NotAuthorizedOrNotFound.
            # For a free account that is the expected state, not an error to shout.
            if "NotAuthorizedOrNotFound" in text or "404" in text:
                return OperationResult(
                    ok=True,
                    message=(
                        "未查询到账单。Always Free / 试用账号没有订阅，因此不会产生账单；"
                        "若这是付费账号，请确认当前用户有账单读取权限"
                        "（Allow group <你的组> to read invoices in tenancy）。"
                    ),
                    data={"invoices": [], "unavailable": True},
                )
            return OperationResult(ok=False, message=text, data={"invoices": []})

        data = getattr(resp, "data", None)
        rows = list(getattr(data, "items", None) or (data if isinstance(data, list) else []) or [])

        def _iso(value: Any) -> str:
            if not value:
                return ""
            try:
                return value.astimezone(timezone.utc).isoformat()
            except Exception:
                return str(value)

        def _num(value: Any) -> Optional[float]:
            try:
                return None if value is None else float(value)
            except (TypeError, ValueError):
                return None

        out: list[dict[str, Any]] = []
        for inv in rows:
            status = str(getattr(inv, "invoice_status", "") or "")
            paid = getattr(inv, "is_paid", None)
            # is_paid is authoritative; fall back to the status when absent.
            if paid is None:
                paid = status.upper() == "CLOSED"
            out.append(
                {
                    "invoice_id": str(getattr(inv, "invoice_id", "") or ""),
                    "invoice_number": str(getattr(inv, "invoice_number", "") or ""),
                    "status": status,
                    "is_paid": bool(paid),
                    "is_payment_failed": bool(getattr(inv, "is_payment_failed", False)),
                    "type": str(getattr(inv, "invoice_type", "") or ""),
                    "currency": str(
                        getattr(getattr(inv, "currency", None), "currency_code", "")
                        or getattr(inv, "currency", "")
                        or ""
                    ),
                    "amount": _num(getattr(inv, "invoice_amount", None)),
                    "amount_due": _num(getattr(inv, "invoice_amount_due", None)),
                    "time_invoice": _iso(getattr(inv, "time_invoice", None)),
                    "time_due": _iso(getattr(inv, "time_invoice_due", None)),
                }
            )
        note = "" if out else "该账号下没有账单记录（Always Free / 试用账号不会产生账单）。"
        return OperationResult(
            ok=True,
            message=note or f"已读取 {len(out)} 张账单",
            data={"invoices": out, "unavailable": False},
        )

    def get_network_egress_usage(self) -> OperationResult:
        """Outbound bytes for the current calendar month, from VCN metrics.

        Always Free includes 10 TB/month of outbound data transfer — the one free
        allowance the quota guard never tracked, and a realistic way to be billed
        by surprise (a download box or a proxy reaches it easily).

        Deliberately an UPPER BOUND, not a bill:
          * ``VnicToNetworkBytes`` counts everything leaving the VNIC, including
            intra-VCN and intra-region traffic that Oracle does not charge for;
          * the allowance is tenancy-wide while this query is per region, so a
            tenancy with 副区 sees each region separately.
        Both are stated in the returned note rather than silently rounded away.
        Unlike ``NetworksBytesOut`` in oci_computeagent (a cumulative counter that
        needs ``.rate()``), this metric is a per-interval byte count, so the hourly
        buckets are summed directly — and it needs no agent on the instance.
        """
        from datetime import datetime, timedelta, timezone

        end = datetime.now(timezone.utc)
        start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # Monitoring keeps ~90 days; a calendar month is always within range, but
        # clamp anyway so a clock skew cannot produce a rejected query.
        if (end - start) > timedelta(days=32):
            start = end - timedelta(days=32)
        compartment = self.resolve_compartment()
        try:
            details = oci.monitoring.models.SummarizeMetricsDataDetails(
                namespace="oci_vcn",
                query="VnicToNetworkBytes[1h].sum()",
                start_time=start,
                end_time=end,
            )
            resp = self.monitoring.summarize_metrics_data(
                compartment, details, compartment_id_in_subtree=True
            ).data
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

        total_bytes = 0.0
        for metric in resp or []:
            for dp in getattr(metric, "aggregated_datapoints", None) or []:
                try:
                    value = float(getattr(dp, "value", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if value != value or value < 0:  # NaN / counter reset
                    continue
                total_bytes += value

        egress_gb = total_bytes / (1024**3)
        return OperationResult(
            ok=True,
            message="",
            data={
                "egress_gb": round(egress_gb, 3),
                "region": self.tenant.region.strip(),
                "since": start.isoformat(),
                "until": end.isoformat(),
                "approximate": True,
                "note": (
                    f"出网流量为估算上限（含区域内互通等免费流量），且只统计当前区域 "
                    f"{self.tenant.region.strip()}；10TB/月 免费额度按整个租户计算。"
                ),
            },
        )

    def get_account_status(self) -> OperationResult:
        """Return tenancy info plus an Always-Free / PAYG classification.

        The tier comes from the tenancy's subscription record (payment model /
        subscription tier), which is authoritative and works even for an
        upgraded account that has never been billed. Service Limits are shown
        for reference only — Oracle returns non-zero limits for paid shapes on
        free accounts too, so they must NOT be used to infer PAYG.
        """
        tenancy_id = self.tenant.tenancy_ocid.strip()
        info = {
            "tenancy_name": self.tenant.name or "",
            "home_region": self.tenant.region or "",
            "description": "",
            "tier": "未知",
            "tier_code": "unknown",
            "tier_reason": "",
            "tier_note": "等级依据租户的订阅记录判定；服务配额（Service Limits）在免费账号上也可能非零，不能作为付费依据。",
            "limits": [],
        }
        # Reading the tenancy is best-effort — some users lack inspect-tenancy
        # permission, but that must not block the tier / limits report.
        try:
            tenancy = self.identity.get_tenancy(tenancy_id).data
            info["tenancy_name"] = getattr(tenancy, "name", "") or info["tenancy_name"]
            info["home_region"] = getattr(tenancy, "home_region_key", "") or info["home_region"]
            info["description"] = getattr(tenancy, "description", "") or ""
        except Exception:  # noqa: BLE001
            pass

        # Service limits — informational only (dashboard). Not used for tier.
        try:
            values = oci.pagination.list_call_get_all_results(
                self.limits.list_limit_values, tenancy_id, service_name="compute"
            ).data
            shown = {}
            for v in values:
                name = str(getattr(v, "name", "") or "")
                value = getattr(v, "value", None)
                try:
                    numeric = float(value) if value is not None else 0.0
                except (TypeError, ValueError):
                    numeric = 0.0
                if name.endswith("count"):
                    shown[name] = numeric
            info["limits"] = [
                {"name": n, "value": val}
                for n, val in sorted(shown.items(), key=lambda kv: kv[0])
                if any(tag in n for tag in FREE_TIER_LIMIT_TAGS)
            ][:12]
        except (ServiceError, Exception):  # noqa: BLE001
            info["limits"] = []

        # Tier is decided from the tenancy's subscription record — the
        # authoritative source. Dashboard keeps home-region fallback for reliability.
        sub_verdict, sub_note, sub_details = self._detect_subscription_tier()
        info["subscription"] = sub_details
        if sub_verdict == "paid":
            info["tier"] = "已升级 / 付费（PAYG）"
            info["tier_code"] = "paid"
            info["tier_reason"] = f"订阅记录显示为付费账号，可开启付费性能与更多配额。依据：{sub_note}"
        elif sub_verdict == "free":
            info["tier"] = "Always Free / 未升级"
            info["tier_code"] = "free"
            info["tier_reason"] = f"订阅记录显示为免费层级。依据：{sub_note}"
        else:
            info["tier"] = "无法确定"
            info["tier_code"] = "unknown"
            info["tier_reason"] = (
                f"未能读取到订阅记录，因此无法判定账号等级。（{sub_note}）"
                "如需准确判定，请给当前用户授予订阅读取权限："
                "Allow group <你的组> to inspect subscriptions in tenancy"
            )
        info["tier_note"] = (
            "等级依据租户的订阅记录判定。服务配额仅供参考——免费账号上也可能非零，不能作为付费依据。"
        )
        info["home_region"] = self._home_region() or info["home_region"]
        return OperationResult(ok=True, message="已读取账号信息", data=info)

    def list_console_password_policies(self) -> OperationResult:
        """List Identity Domain password policies (console login password expiry, etc.).

        Free / modern tenancies store the 120-day force-change rule in the
        domain PasswordPolicy resource (`passwordExpiresAfter`), not the local
        OCIBot reminder fields.
        """
        try:
            domains = self._list_identity_domains()
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

        if not domains:
            return OperationResult(
                ok=False,
                message=(
                    "未找到 Identity Domain。当前租户可能仍是经典 IAM，"
                    "或 API 用户缺少 list domains 权限。"
                ),
                data={"domains": [], "policies": []},
            )

        policies: list[dict[str, Any]] = []
        errors: list[str] = []
        for domain in domains:
            try:
                client = self._identity_domains_client(domain["url"])
                items = self._list_domain_password_policies(client)
                for item in items:
                    policies.append({**item, "domain_id": domain["id"], "domain_name": domain["name"], "domain_url": domain["url"]})
            except ServiceError as exc:
                errors.append(f"{domain['name']}: {_format_service_error(exc)}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{domain['name']}: {exc}")

        if not policies and errors:
            return OperationResult(ok=False, message="；".join(errors), data={"domains": domains, "policies": []})
        return OperationResult(
            ok=True,
            message=f"已读取 {len(policies)} 条密码策略（{len(domains)} 个 Domain）",
            data={"domains": domains, "policies": policies, "errors": errors},
        )

    def get_console_password_status(self) -> OperationResult:
        """Real console-password expiry state: the policy AND the actual date.

        ``list_console_password_policies`` answers "how many days does the policy
        say", which is not the same question as "when does MY password expire" —
        and after clicking 关闭强制改密 the operator needs to see the second one to
        know whether it took. The date comes from the user's own passwordState
        (``lastSuccessfulSetDate``) plus the policy that actually applies to them.

        Never raises for a missing piece: a tenancy on classic IAM, or an API user
        without permission to read domain users, still gets the policy half with a
        note saying why the date is absent.
        """
        result = self.list_console_password_policies()
        data = dict(result.data if isinstance(result.data, dict) else {})
        policies = list(data.get("policies") or [])
        notes: list[str] = list(data.get("errors") or [])

        user_info: dict[str, Any] = {"found": False}
        for domain in data.get("domains") or []:
            try:
                found = self._find_domain_user(domain.get("url") or "")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{domain.get('name') or '域'}: 读取用户密码状态失败：{exc}")
                continue
            if found:
                user_info = {**found, "found": True, "domain_name": domain.get("name") or ""}
                break
        if not user_info.get("found") and not notes:
            notes.append("未能在 Identity Domain 中找到当前 API 用户，无法计算真实到期时间")

        data["policies"] = policies
        data["user"] = user_info
        data["effective"] = self._effective_password_expiry(policies, user_info)
        data["errors"] = notes
        return OperationResult(ok=bool(result.ok or policies), message=result.message or "", data=data)

    def _find_domain_user(self, domain_url: str) -> Optional[dict[str, Any]]:
        """Look up this tenant's API user in a domain and return its password state."""
        ocid = self.tenant.user_ocid.strip()
        if not domain_url or not ocid:
            return None
        client = self._identity_domains_client(domain_url)
        # SCIM filter on the OCI-specific `ocid` attribute: the domain's own user id
        # is a SCIM uuid, not the ocid1.user… value the tenant is configured with.
        resp = client.list_users(
            filter=f'ocid eq "{ocid}"',
            attribute_sets=["all"],
            count=1,
        )
        payload = getattr(resp, "data", None)
        resources = getattr(payload, "resources", None) or []
        if not resources:
            return None
        user = resources[0]
        state = getattr(
            user,
            "urn_ietf_params_scim_schemas_oracle_idcs_extension_password_state_user",
            None,
        )
        applicable = getattr(state, "applicable_password_policy", None) if state else None
        return {
            "user_name": str(getattr(user, "user_name", "") or ""),
            "last_set": str(getattr(state, "last_successful_set_date", "") or "") if state else "",
            "expired": bool(getattr(state, "expired", False)) if state else False,
            "cant_expire": bool(getattr(state, "cant_expire", False)) if state else False,
            "applicable_policy_id": str(getattr(applicable, "value", "") or "") if applicable else "",
            "applicable_policy_name": str(getattr(applicable, "display", "") or "") if applicable else "",
        }

    @classmethod
    def _effective_password_expiry(
        cls, policies: list[dict[str, Any]], user: dict[str, Any]
    ) -> dict[str, Any]:
        """Combine policy + user state into the one answer the operator wants."""
        from datetime import datetime, timedelta, timezone

        out: dict[str, Any] = {
            "expires": False,
            "days": None,
            "expires_at": "",
            "days_left": None,
            "policy_name": "",
            "summary": "",
            # Raw per-policy values so the panel can show the actual
            # defaultPasswordPolicy number instead of only a derived verdict.
            "all_policies": [],
        }

        def _as_days(value: Any) -> int:
            """Policy value as a day count; anything unparseable means "no expiry".

            Oracle returns an int here, but this must not be the thing that breaks
            the whole status read — an odd value on one policy would otherwise
            take down the answer for all of them.
            """
            try:
                return int(value) if value not in (None, "") else 0
            except (TypeError, ValueError):
                return 0

        out["all_policies"] = [
            {
                "name": str(p.get("name") or p.get("id") or "?"),
                "days": _as_days(p.get("password_expires_after")),
                "is_default": cls._is_default_password_policy(p),
                "is_template": cls._is_protected_password_policy(p),
            }
            for p in policies
        ]

        chosen: Optional[dict[str, Any]] = None
        wanted = str(user.get("applicable_policy_id") or "")
        if wanted:
            chosen = next((p for p in policies if str(p.get("id") or "") == wanted), None)
        if chosen is None:
            # Console logins are governed by defaultPasswordPolicy — this is the
            # number the operator sees in Oracle's own console, so it is what the
            # panel must report when the user's applicable policy is unknown.
            chosen = next((p for p in policies if cls._is_default_password_policy(p)), None)
        if chosen is None:
            # Still nothing: fall back to the strictest REAL policy, so the answer
            # errs towards "it still expires" rather than reassuring wrongly. The
            # protected system template is excluded — its value is not something
            # the tenancy is actually subject to.
            candidates = [
                p
                for p in policies
                if not cls._is_protected_password_policy(p)
                and _as_days(p.get("password_expires_after")) > 0
            ]
            chosen = min(
                candidates,
                key=lambda p: _as_days(p.get("password_expires_after")),
                default=None,
            )
        if chosen is None and policies:
            real = [p for p in policies if not cls._is_protected_password_policy(p)]
            chosen = (real or policies)[0]
        if chosen is not None:
            out["policy_name"] = str(chosen.get("name") or "")

        days = _as_days(chosen.get("password_expires_after") if chosen else None)

        if user.get("cant_expire"):
            out["summary"] = "永不过期（该用户被标记为密码不会过期）"
            return out
        if days <= 0:
            out["summary"] = "永不过期（策略未设置有效期）"
            return out

        out["expires"] = True
        out["days"] = days
        last_set = str(user.get("last_set") or "")
        if not last_set:
            out["summary"] = f"{days} 天后过期（未能读取上次改密时间，无法算出具体日期）"
            return out
        try:
            text = last_set.replace("Z", "+00:00")
            base = datetime.fromisoformat(text)
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
        except ValueError:
            out["summary"] = f"{days} 天后过期（上次改密时间格式无法解析：{last_set}）"
            return out

        expires_at = base + timedelta(days=days)
        left = (expires_at - datetime.now(timezone.utc)).days
        out["expires_at"] = expires_at.isoformat()
        out["days_left"] = left
        date_text = expires_at.strftime("%Y-%m-%d")
        if user.get("expired") or left < 0:
            out["summary"] = f"已过期（{date_text}）"
        else:
            out["summary"] = f"{date_text} 到期（还有 {left} 天）"
        return out

    # Built-in Identity Domains policies that Oracle marks protected (PATCH → 403).
    # StandardPasswordPolicy is a system template; console logins use Default/defaultPasswordPolicy.
    _PROTECTED_PASSWORD_POLICY_IDS = frozenset(
        {
            "standardpasswordpolicy",
            "standardpasswordpolicyid",
        }
    )
    _PROTECTED_PASSWORD_POLICY_NAMES = frozenset(
        {
            "standardpasswordpolicy",
            "standard password policy",
            "standard",
        }
    )

    # The policy that actually governs console login. The protected
    # StandardPasswordPolicy above is a system template, so reporting ITS number
    # would tell the operator a value they cannot change and that does not match
    # what Oracle's console shows them.
    _DEFAULT_PASSWORD_POLICY_NAMES = frozenset({"defaultpasswordpolicy", "default"})

    @classmethod
    def _is_default_password_policy(cls, pol: dict[str, Any]) -> bool:
        pid = str(pol.get("id") or "").strip().lower().replace(" ", "")
        name = str(pol.get("name") or "").strip().lower().replace(" ", "")
        if name in cls._DEFAULT_PASSWORD_POLICY_NAMES:
            return True
        return "defaultpasswordpolicy" in pid or "defaultpasswordpolicy" in name

    @classmethod
    def _is_protected_password_policy(cls, pol: dict[str, Any]) -> bool:
        pid = str(pol.get("id") or "").strip().lower()
        name = str(pol.get("name") or "").strip().lower()
        if pid in cls._PROTECTED_PASSWORD_POLICY_IDS:
            return True
        if name in cls._PROTECTED_PASSWORD_POLICY_NAMES:
            return True
        # Oracle resource ids often look like StandardPasswordPolicy.
        if "standardpasswordpolicy" in pid.replace(" ", ""):
            return True
        if name.replace(" ", "") == "standardpasswordpolicy":
            return True
        return False

    @staticmethod
    def _is_protected_password_policy_error(exc: BaseException) -> bool:
        """True when Oracle refuses PATCH because the policy is a protected system resource."""
        text = " ".join(
            str(part)
            for part in (
                getattr(exc, "message", ""),
                getattr(exc, "body", ""),
                exc,
            )
            if part
        ).lower()
        return (
            "protected passwordpolicy" in text
            or "protected resource" in text
            or "checkprotectedresource" in text
            or "cannot perform update operation on protected" in text
        )

    @staticmethod
    def _password_policy_sort_key(pol: dict[str, Any]) -> tuple:
        name = str(pol.get("name") or "").strip().lower()
        pid = str(pol.get("id") or "").strip().lower()
        # Prefer the real default policy that free-tier console accounts use.
        if name in {"default", "default password policy", "defaultpasswordpolicy"} or pid in {
            "defaultpasswordpolicy",
            "default",
        }:
            return (0, name or pid)
        if "default" in name or "default" in pid:
            return (1, name or pid)
        return (2, name or pid)

    def disable_console_password_expiry(self) -> OperationResult:
        """Clear Identity Domain ``passwordExpiresAfter`` so console passwords do not expire.

        This talks to Oracle Identity Domains (SCIM PasswordPolicy), not the local
        reminder stored on the OCIBot tenant row. Requires domain admin / password
        policy manage rights on the API user.

        Only mutates editable policies (typically Default/defaultPasswordPolicy).
        Built-in StandardPasswordPolicy is Oracle-protected and is skipped.
        """
        listed = self.list_console_password_policies()
        if not listed.ok:
            return listed
        policies = list((listed.data or {}).get("policies") or [])
        if not policies:
            return OperationResult(
                ok=False,
                message=listed.message or "未找到可修改的密码策略",
                data=listed.data,
            )

        updated: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[str] = []

        policies_sorted = sorted(policies, key=self._password_policy_sort_key)

        for pol in policies_sorted:
            domain_url = str(pol.get("domain_url") or "").strip()
            policy_id = str(pol.get("id") or "").strip()
            label = f"{pol.get('domain_name') or 'domain'}/{pol.get('name') or policy_id}"
            if not domain_url or not policy_id:
                continue
            if self._is_protected_password_policy(pol):
                skipped.append({**pol, "reason": "Oracle 受保护系统策略（不可修改，可忽略）"})
                continue
            expires_after = pol.get("password_expires_after")
            # None / 0 → already never-expire (domain-dependent; treat both as off).
            if expires_after is None or expires_after == 0:
                skipped.append({**pol, "reason": "已是不强制过期"})
                continue
            try:
                client = self._identity_domains_client(domain_url)
                after = self._patch_password_policy_never_expire(client, policy_id)
                updated.append(
                    {
                        "id": policy_id,
                        "name": pol.get("name") or "",
                        "domain_name": pol.get("domain_name") or "",
                        "domain_url": domain_url,
                        "password_expires_after_before": expires_after,
                        "password_expires_after_after": after,
                    }
                )
            except ServiceError as exc:
                if self._is_protected_password_policy_error(exc):
                    # Standard/system templates refuse UPDATE even when listed.
                    skipped.append(
                        {
                            **pol,
                            "reason": "Oracle 受保护系统策略（不可修改，可忽略）",
                            "error": _format_service_error(exc),
                        }
                    )
                else:
                    errors.append(f"{label}: {_format_service_error(exc)}")
            except Exception as exc:  # noqa: BLE001
                if self._is_protected_password_policy_error(exc):
                    skipped.append(
                        {
                            **pol,
                            "reason": "Oracle 受保护系统策略（不可修改，可忽略）",
                            "error": str(exc),
                        }
                    )
                else:
                    errors.append(f"{label}: {exc}")

        if not updated and not skipped and errors:
            return OperationResult(ok=False, message="；".join(errors), data={"updated": [], "errors": errors})

        if updated:
            names = "、".join(
                f"{u.get('domain_name') or 'domain'}/{u.get('name') or u.get('id')}" for u in updated[:5]
            )
            msg = f"已在 Oracle 关闭强制改密：{names}"
            if len(updated) > 5:
                msg += f" 等 {len(updated)} 条"
            # Only mention skips that are useful; don't dump protected-system noise as failure.
            never = [s for s in skipped if "不强制过期" in str(s.get("reason") or "")]
            protected = [s for s in skipped if "受保护" in str(s.get("reason") or "")]
            if never:
                msg += f"；另有 {len(never)} 条本就无需过期"
            if protected:
                msg += f"；已跳过 {len(protected)} 条系统受保护策略（如 StandardPasswordPolicy）"
            if errors:
                msg += f"；其他失败：{'；'.join(errors[:2])}"
            return OperationResult(
                ok=True,
                message=msg,
                data={"updated": updated, "skipped": skipped, "errors": errors},
            )

        if skipped and not errors:
            protected_only = all("受保护" in str(s.get("reason") or "") for s in skipped)
            if protected_only:
                return OperationResult(
                    ok=False,
                    message=(
                        "只找到 Oracle 受保护的系统密码策略（如 StandardPasswordPolicy），"
                        "无法修改。请确认 Domain 中是否存在 Default/defaultPasswordPolicy，"
                        "以及 API 用户是否有管理密码策略权限。"
                    ),
                    data={"updated": [], "skipped": skipped, "errors": []},
                )
            return OperationResult(
                ok=True,
                message=f"Oracle 密码策略已是「不强制过期」或无需修改（{len(skipped)} 条）",
                data={"updated": [], "skipped": skipped, "errors": []},
            )

        return OperationResult(
            ok=False,
            message="；".join(errors) if errors else "未能修改任何密码策略",
            data={"updated": updated, "skipped": skipped, "errors": errors},
        )

    def _list_identity_domains(self) -> list[dict[str, str]]:
        """Return ACTIVE identity domains with a usable domain URL."""
        if not OCI_AVAILABLE:
            raise OCIClientError("未安装 oci SDK")
        tenancy = self.tenant.tenancy_ocid.strip()
        # Domains are tenancy-scoped; list from home-region identity when possible.
        identity = self.identity
        home = self._home_region()
        if home and home != self.tenant.region.strip():
            try:
                identity = IdentityClient(self._config_for_region(home), retry_strategy=sdk_default_retry_strategy())
            except Exception:  # noqa: BLE001
                identity = self.identity

        response = oci.pagination.list_call_get_all_results(
            identity.list_domains,
            compartment_id=tenancy,
            lifecycle_state="ACTIVE",
        )
        items: list[dict[str, str]] = []
        for d in response.data or []:
            url = (
                getattr(d, "url", None)
                or getattr(d, "home_region_url", None)
                or ""
            )
            url = str(url or "").strip().rstrip("/")
            if not url:
                continue
            items.append(
                {
                    "id": str(getattr(d, "id", "") or ""),
                    "name": str(getattr(d, "display_name", "") or getattr(d, "id", "") or ""),
                    "url": url,
                    "type": str(getattr(d, "type", "") or ""),
                    "home_region": str(getattr(d, "home_region", "") or ""),
                }
            )
        # Prefer Default domain first.
        items.sort(key=lambda x: (0 if x.get("type") == "DEFAULT" else 1, x.get("name") or ""))
        return items

    def _identity_domains_client(self, service_endpoint: str) -> Any:
        if not OCI_AVAILABLE:
            raise OCIClientError("未安装 oci SDK")
        try:
            from oci.identity_domains import IdentityDomainsClient
        except ImportError as exc:  # pragma: no cover
            raise OCIClientError("当前 oci SDK 不支持 Identity Domains") from exc
        endpoint = (service_endpoint or "").strip().rstrip("/")
        if not endpoint:
            raise OCIClientError("Identity Domain URL 为空")
        # Domain SCIM endpoint is global to that domain; region in config is still required.
        cfg = self._config_for_region(self._home_region() or self.tenant.region.strip())
        return IdentityDomainsClient(
            cfg,
            service_endpoint=endpoint,
            retry_strategy=sdk_default_retry_strategy(),
        )

    def _list_domain_password_policies(self, client: Any) -> list[dict[str, Any]]:
        """Fetch all password policies from one domain client."""
        items: list[dict[str, Any]] = []
        start_index = 1
        count = 100
        while True:
            resp = client.list_password_policies(
                start_index=start_index,
                count=count,
                attribute_sets=["all"],
            )
            data = getattr(resp, "data", None)
            resources = getattr(data, "resources", None) if data is not None else None
            if resources is None and isinstance(data, list):
                resources = data
            resources = resources or []
            for p in resources:
                items.append(self._password_policy_to_dict(p))
            total = int(getattr(data, "total_results", 0) or 0) if data is not None else 0
            if not resources:
                break
            start_index += len(resources)
            if total and start_index > total:
                break
            if len(resources) < count:
                break
            if start_index > 1000:  # hard safety
                break
        return items

    @staticmethod
    def _password_policy_to_dict(policy: Any) -> dict[str, Any]:
        expires = getattr(policy, "password_expires_after", None)
        warning = getattr(policy, "password_expire_warning", None)
        return {
            "id": str(getattr(policy, "id", "") or ""),
            "ocid": str(getattr(policy, "ocid", "") or ""),
            "name": str(getattr(policy, "name", "") or ""),
            "description": str(getattr(policy, "description", "") or ""),
            "password_expires_after": expires if expires is not None else None,
            "password_expire_warning": warning if warning is not None else None,
            "priority": getattr(policy, "priority", None),
        }

    def _patch_password_policy_never_expire(self, client: Any, password_policy_id: str) -> Any:
        """Clear passwordExpiresAfter (and warning) on a domain password policy."""
        from oci.identity_domains.models import Operations, PatchOp

        # Identity Domains requires uppercase op enums: ADD / REMOVE / REPLACE
        # (lowercase "remove" is rejected with "must be one of ['ADD', ...]").
        ops = [
            Operations(op=Operations.OP_REMOVE, path="passwordExpiresAfter"),
            Operations(op=Operations.OP_REMOVE, path="passwordExpireWarning"),
        ]
        patch = PatchOp(
            schemas=["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            operations=ops,
        )
        try:
            resp = client.patch_password_policy(
                password_policy_id,
                patch_op=patch,
                attribute_sets=["all"],
            )
        except ServiceError as exc:
            # Some domains reject remove; fall back to replace with null.
            status = getattr(exc, "status", None)
            message = str(getattr(exc, "message", "") or exc)
            if status not in (400, 422) and "passwordExpiresAfter" not in message:
                raise
            ops = [
                Operations(op=Operations.OP_REPLACE, path="passwordExpiresAfter", value=None),
                Operations(op=Operations.OP_REPLACE, path="passwordExpireWarning", value=None),
            ]
            patch = PatchOp(
                schemas=["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                operations=ops,
            )
            resp = client.patch_password_policy(
                password_policy_id,
                patch_op=patch,
                attribute_sets=["all"],
            )
        data = getattr(resp, "data", None)
        return getattr(data, "password_expires_after", None) if data is not None else None

    # ------------------------------------------------------------------
    # Region subscriptions (副区 / secondary regions)
    # ------------------------------------------------------------------
    def home_region(self) -> str:
        """Public accessor for the tenancy's home region name.

        Callers outside this module need it to tell a home-region session from a
        secondary-region one — Always Free resources exist only in the home
        region, so anything launched elsewhere is billable.
        """
        return self._home_region()

    def list_subscribed_regions(self) -> OperationResult:
        """Regions this tenancy is already subscribed to (home region first)."""
        try:
            subs = self.identity.list_region_subscriptions(self.tenant.tenancy_ocid.strip()).data or []
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

        home = ""
        regions: list[dict[str, Any]] = []
        for sub in subs:
            name = str(getattr(sub, "region_name", "") or "").strip().lower()
            is_home = bool(getattr(sub, "is_home_region", False))
            if is_home and name:
                home = name
            regions.append(
                {
                    # Key case is preserved exactly as Oracle returned it (they are
                    # uppercase: NRT / KIX / FRA). CreateRegionSubscription resolves
                    # the region BY this key, so a lowercased copy is a different,
                    # non-existent entity — see subscribe_region.
                    "region_name": name,
                    "region_key": str(getattr(sub, "region_key", "") or "").strip(),
                    "status": str(getattr(sub, "status", "") or ""),
                    "is_home_region": is_home,
                }
            )
        regions.sort(key=lambda r: (not r["is_home_region"], r["region_name"]))
        if home:
            # Same answer _home_region() would compute; seed its cache for free.
            self._home_region_name = home
        return OperationResult(
            ok=True,
            message="",
            data={"home_region": home or self.tenant.region.strip(), "regions": regions},
        )

    def list_all_regions(self) -> OperationResult:
        """Every region OCI exposes — the candidate list for 开通副区."""
        try:
            items = self.identity.list_regions().data or []
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))
        regions = [
            {
                # Names are genuinely lowercase (ap-osaka-1); keys are uppercase and
                # must be passed back to Oracle verbatim (see list_subscribed_regions).
                "region_name": str(getattr(r, "name", "") or "").strip().lower(),
                "region_key": str(getattr(r, "key", "") or "").strip(),
            }
            for r in items
        ]
        regions = [r for r in regions if r["region_name"]]
        regions.sort(key=lambda r: r["region_name"])
        return OperationResult(ok=True, message="", data={"regions": regions})

    def subscribe_region(self, region: str) -> OperationResult:
        """Subscribe this tenancy to another region.

        ``region`` accepts a region name (``ap-osaka-1``) or its key (``KIX``,
        matched case-insensitively). Already-subscribed regions return ok with
        ``already=True`` so the caller can treat "subscribe" as idempotent.

        Three Oracle constraints shape this:
          * the call only works against the HOME region endpoint, so it builds a
            dedicated IdentityClient rather than reusing ``self.identity``;
          * ``region_key`` must be the key exactly as Oracle spells it (uppercase).
            A lowercased key resolves to nothing and comes back as
            ``[404] EntityNotFound``, which reads like a permission problem;
          * a subscription cannot be removed once created, which is why the API
            layer above requires an explicit confirmation.

        Each failure names the step it came from: all three calls can answer 404
        and an unattributed message leaves no way to tell them apart.
        """
        wanted = (region or "").strip().lower()
        if not wanted:
            return OperationResult(ok=False, message="请选择要开通的区域")

        def _matches(item: dict[str, Any]) -> bool:
            return wanted in {
                str(item.get("region_name") or "").lower(),
                str(item.get("region_key") or "").lower(),
            }

        subscribed = self.list_subscribed_regions()
        if not subscribed.ok:
            return OperationResult(
                ok=False, message=f"读取已开通区域失败：{subscribed.message or '未知错误'}"
            )
        for item in (subscribed.data or {}).get("regions") or []:
            if _matches(item):
                return OperationResult(
                    ok=True,
                    message=f"该区域已开通：{item.get('region_name')}",
                    data={
                        "region_name": item.get("region_name") or "",
                        "region_key": item.get("region_key") or "",
                        "already": True,
                    },
                )

        catalog = self.list_all_regions()
        if not catalog.ok:
            return OperationResult(
                ok=False, message=f"读取区域清单失败：{catalog.message or '未知错误'}"
            )
        match = next(
            (r for r in (catalog.data or {}).get("regions") or [] if _matches(r)),
            None,
        )
        if match is None:
            return OperationResult(ok=False, message=f"未知区域：{region}")
        if not match.get("region_key"):
            return OperationResult(
                ok=False, message=f"Oracle 未返回「{match['region_name']}」的区域代码，无法开通"
            )

        try:
            identity = IdentityClient(
                self._config_for_region(self._home_region()),
                retry_strategy=sdk_default_retry_strategy(),
            )
            identity.create_region_subscription(
                oci.identity.models.CreateRegionSubscriptionDetails(region_key=match["region_key"]),
                self.tenant.tenancy_ocid.strip(),
            )
        except ServiceError as exc:
            detail = _format_service_error(exc)
            if int(getattr(exc, "status", 0) or 0) in (401, 404):
                # OCI answers 404 for "no permission" as well as "no such thing",
                # and this call needs tenancy-level rights the panel's other calls
                # do not — say so instead of leaving the operator on a bare 404.
                detail += (
                    f"\n提示：提交开通「{match['region_name']}」({match['region_key']}) 被拒绝。"
                    "该接口需要 API 用户具备租户级权限（Administrators 组，或 manage tenancies 策略），"
                    "且账号必须已升级为 PAYG —— 纯 Always Free 账号无法订阅新区域。"
                )
            return OperationResult(ok=False, message=detail)
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=f"提交开通失败：{exc}")
        return OperationResult(
            ok=True,
            message=f"已提交开通「{match['region_name']}」，Oracle 通常需要几分钟才能创建资源",
            data={
                "region_name": match["region_name"],
                "region_key": match["region_key"],
                "already": False,
            },
        )

    def _home_region(self) -> str:
        """Resolve the tenancy's home region name (cached). Falls back to the
        tenant's configured region. Budgets and some Usage API calls only work
        against the home region."""
        cached = getattr(self, "_home_region_name", None)
        if cached:
            return cached
        region = self.tenant.region.strip()
        try:
            subs = self.identity.list_region_subscriptions(self.tenant.tenancy_ocid.strip()).data
            home = next((s for s in subs if getattr(s, "is_home_region", False)), None)
            if home and getattr(home, "region_name", ""):
                region = home.region_name
        except Exception:  # noqa: BLE001
            pass
        self._home_region_name = region
        return region

    def _config_for_region(self, region: str) -> dict:
        cfg = dict(self._config)
        cfg["region"] = (region or "").strip() or self.tenant.region.strip()
        return cfg

    def _account_api_regions(self) -> list[str]:
        """Endpoints to try for tenancy-wide account APIs, best first.

        These APIs answer for the whole tenancy from any region, so when the
        home-region endpoint is unreachable (TLS reset, proxy interception,
        regional outage) the tenant's own region can still answer.
        """
        candidates: list[str] = []
        for region in (self._home_region(), self.tenant.region.strip()):
            region = (region or "").strip()
            if region and region not in candidates:
                candidates.append(region)
        return candidates or [self.tenant.region.strip()]

    @staticmethod
    def _is_network_error(exc: Exception) -> bool:
        """True for transport-level failures (TLS/DNS/proxy), not API errors."""
        blob = str(exc).lower()
        return any(
            key in blob
            for key in (
                "ssl", "max retries", "connection", "connectionpool", "timed out",
                "timeout", "eof occurred", "failed to establish", "name resolution",
            )
        )

    def _detect_subscription_tier(
        self, regions: Optional[list[str]] = None
    ) -> tuple[str, str, dict]:
        """Classify the tenancy from its actual subscription record.

        This is the authoritative signal: an upgraded (PAYG) tenancy reports a
        paid payment model even when it has spent nothing, whereas billed spend
        alone cannot distinguish "upgraded but only using free resources" from
        a genuine Always Free account.

        Returns ``(verdict, note, details)`` with verdict in
        ``{"paid", "free", "unknown"}``.

        ``regions``: optional endpoint list; default is home region then tenant
        region. Sidebar tier probe passes only the tenant region to skip the
        ``list_region_subscriptions`` call inside ``_home_region``.
        """
        details: dict = {}
        try:
            from oci.tenant_manager_control_plane import SubscriptionClient
        except ImportError:
            return "unknown", "SDK 不支持订阅查询", details
        tenancy = self.tenant.tenancy_ocid.strip()
        client = None
        items: list = []
        last_exc: Optional[Exception] = None
        region_list = [r for r in (regions or self._account_api_regions()) if (r or "").strip()]
        if not region_list:
            region_list = [(self.tenant.region or "").strip() or "us-ashburn-1"]
        for region in region_list:
            try:
                client = SubscriptionClient(
                    self._config_for_region(region),
                    retry_strategy=sdk_default_retry_strategy(),
                )
                resp = client.list_subscriptions(compartment_id=tenancy, entity_version="V1")
                items = list(getattr(resp.data, "items", None) or [])
                last_exc = None
                break
            except ServiceError as exc:
                return (
                    "unknown",
                    f"订阅查询失败 [{getattr(exc, 'status', '')}] {getattr(exc, 'code', '') or ''}".strip(),
                    details,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                client = None
                if self._is_network_error(exc):
                    continue  # endpoint unreachable — try the next region
                break
        if last_exc is not None:
            note = "订阅服务网络不可达" if self._is_network_error(last_exc) else f"订阅查询失败：{str(last_exc)[:60]}"
            return "unknown", note, details
        if not items:
            return "unknown", "未返回订阅信息", details

        payment_models: list[str] = []
        tiers: list[str] = []
        promo_active = False
        for item in items:
            model = str(getattr(item, "payment_model", "") or "").strip()
            if model:
                payment_models.append(model)
            sub_id = getattr(item, "id", "") or ""
            if not sub_id:
                continue
            # The full record carries subscription_tier / promotion.
            try:
                full = client.get_subscription(subscription_id=sub_id, entity_version="V1").data
                tier = str(getattr(full, "subscription_tier", "") or "").strip()
                if tier:
                    tiers.append(tier)
                promos = getattr(full, "promotion", None) or []
                if not isinstance(promos, (list, tuple)):
                    promos = [promos]
                for promo in promos:
                    if str(getattr(promo, "status", "") or "").upper() == "ACTIVE":
                        promo_active = True
            except Exception:  # noqa: BLE001
                pass

        details["payment_models"] = payment_models
        details["subscription_tiers"] = tiers
        details["promotion_active"] = promo_active
        blob = " ".join(payment_models + tiers).lower()
        summary = "、".join([v for v in (payment_models + tiers) if v]) or "无"
        if any(k in blob for k in ("pay as you go", "payg", "monthly", "annual", "commit")):
            return "paid", f"订阅付费模式：{summary}", details
        if any(k in blob for k in ("free", "trial", "promo")):
            return "free", f"订阅层级：{summary}", details
        return "unknown", f"订阅信息：{summary}", details

    def replace_ephemeral_public_ip(self, instance_id: str, compartment_id: str) -> OperationResult:
        try:
            network = self.resolve_primary_network(
                instance_id,
                compartment_id,
                include_resource_details=True,
            )
            subnet = self.network.get_subnet(network.subnet_id).data
            if bool(getattr(subnet, "prohibit_public_ip_on_vnic", False)):
                return OperationResult(ok=False, message="该 Subnet 禁止为 VNIC 分配公网 IP")
            if network.public_ip_lifetime.upper() == "RESERVED":
                return OperationResult(ok=False, message="当前绑定的是保留公网 IP，工具不会自动删除或更换")
            old_ip = network.public_ipv4
            if network.public_ip_id:
                self.network.delete_public_ip(network.public_ip_id)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        lookup = oci.core.models.GetPublicIpByPrivateIpIdDetails(
                            private_ip_id=network.private_ip_id
                        )
                        self.network.get_public_ip_by_private_ip_id(lookup)
                        time.sleep(1)
                    except ServiceError as exc:
                        if getattr(exc, "status", None) == 404:
                            break
                        raise
            details = oci.core.models.CreatePublicIpDetails(
                compartment_id=network.private_ip_compartment_id,
                lifetime="EPHEMERAL",
                private_ip_id=network.private_ip_id,
                display_name=f"public-ip-{instance_id[-8:]}",
            )
            try:
                public_ip = self.network.create_public_ip(details).data
            except ServiceError:
                # A timed-out create may still have succeeded; confirm the actual binding first.
                try:
                    lookup = oci.core.models.GetPublicIpByPrivateIpIdDetails(
                        private_ip_id=network.private_ip_id
                    )
                    public_ip = self.network.get_public_ip_by_private_ip_id(lookup).data
                except ServiceError:
                    raise
                # If the lookup returns the address we were replacing, the create
                # did NOT succeed — the old IP is simply still bound (the unbind
                # wait timed out). Reporting ok with the old address claimed a
                # rotation that never happened.
                if (
                    getattr(public_ip, "id", None) == network.public_ip_id
                    or (old_ip and getattr(public_ip, "ip_address", None) == old_ip)
                ):
                    raise
            return OperationResult(
                ok=True,
                message=f"公网 IPv4 已更换：{old_ip or '无'} → {public_ip.ip_address}",
                data={"old_ip": old_ip, "new_ip": public_ip.ip_address, "public_ip_id": public_ip.id},
            )
        except ServiceError as exc:
            return OperationResult(
                ok=False,
                message=_format_service_error(exc) + "；旧地址可能已释放，可再次执行以重新分配",
                data={"stage": "replace", "recovery_possible": True},
            )
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc), data={"recovery_possible": True})

    # ------------------------------------------------------------------
    # Custom images
    # ------------------------------------------------------------------
    def list_custom_images(self, compartment_id: Optional[str] = None) -> list[dict]:
        """List custom images owned by the tenancy compartment (platform images excluded)."""
        compartment = (compartment_id or self.resolve_compartment()).strip()
        try:
            resp = oci.pagination.list_call_get_all_results(
                self.compute.list_images,
                compartment_id=compartment,
                sort_by="TIMECREATED",
                sort_order="DESC",
            )
        except ServiceError as exc:
            raise OCIClientError(_format_service_error(exc)) from exc
        items: list[dict] = []
        for img in resp.data:
            # Custom images carry the owning compartment; platform images do not.
            if not (getattr(img, "compartment_id", None) or ""):
                continue
            state = str(getattr(img, "lifecycle_state", "") or "")
            items.append(
                {
                    "id": img.id,
                    "display_name": img.display_name or img.id,
                    "operating_system": getattr(img, "operating_system", "") or "",
                    "operating_system_version": getattr(img, "operating_system_version", "") or "",
                    "lifecycle_state": state,
                    "size_in_mbs": getattr(img, "size_in_mbs", None),
                    "time_created": str(getattr(img, "time_created", "") or ""),
                    "is_custom": True,
                    "label": f"自定义镜像 · {img.display_name or img.id}  [{img.id[-8:]}]",
                }
            )
        return items[:100]

    def create_custom_image(
        self, instance_id: str, compartment_id: str, display_name: str
    ) -> OperationResult:
        """Create a custom image from an instance (usable for reinstall / clone)."""
        try:
            details = oci.core.models.CreateImageDetails(
                compartment_id=compartment_id,
                instance_id=instance_id,
                display_name=(display_name or "").strip() or None,
            )
            img = self.compute.create_image(details).data
            return OperationResult(
                ok=True,
                message=(
                    f"已提交创建镜像：{img.display_name or img.id}。制作期间实例会短暂进入"
                    " CREATING_IMAGE 状态，完成后可在创建实例向导中选择该镜像。"
                ),
                data={"image_id": img.id, "lifecycle_state": img.lifecycle_state},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def delete_custom_image(self, image_id: str) -> OperationResult:
        try:
            img = self.compute.get_image(image_id).data
            if not (getattr(img, "compartment_id", None) or ""):
                return OperationResult(ok=False, message="仅允许删除自定义镜像")
            self.compute.delete_image(image_id)
            return OperationResult(ok=True, message="已删除自定义镜像")
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    # ------------------------------------------------------------------
    # Reserved public IPs
    # ------------------------------------------------------------------
    def list_reserved_public_ips(self, compartment_id: Optional[str] = None) -> list[dict]:
        """List RESERVED public IPv4 addresses in the compartment (region scope)."""
        compartment = (compartment_id or self.resolve_compartment()).strip()
        try:
            resp = oci.pagination.list_call_get_all_results(
                self.network.list_public_ips,
                scope="REGION",
                compartment_id=compartment,
                lifetime="RESERVED",
            )
        except ServiceError as exc:
            raise OCIClientError(_format_service_error(exc)) from exc
        items: list[dict] = []
        for ip in resp.data:
            items.append(
                {
                    "id": ip.id,
                    "ip_address": getattr(ip, "ip_address", "") or "",
                    "display_name": getattr(ip, "display_name", "") or "",
                    "lifecycle_state": getattr(ip, "lifecycle_state", "") or "",
                    "assigned": bool(getattr(ip, "private_ip_id", None)),
                    "private_ip_id": getattr(ip, "private_ip_id", "") or "",
                    "time_created": str(getattr(ip, "time_created", "") or ""),
                }
            )
        return items

    def create_reserved_public_ip(
        self, compartment_id: Optional[str] = None, display_name: str = ""
    ) -> OperationResult:
        compartment = (compartment_id or self.resolve_compartment()).strip()
        try:
            details = oci.core.models.CreatePublicIpDetails(
                compartment_id=compartment,
                lifetime="RESERVED",
                display_name=(display_name or "").strip() or None,
            )
            ip = self.network.create_public_ip(details).data
            return OperationResult(
                ok=True,
                message=f"已创建保留公网 IP：{ip.ip_address}",
                data={"public_ip_id": ip.id, "ip_address": ip.ip_address},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def delete_reserved_public_ip(self, public_ip_id: str) -> OperationResult:
        try:
            ip = self.network.get_public_ip(public_ip_id).data
            if str(getattr(ip, "lifetime", "") or "").upper() != "RESERVED":
                return OperationResult(ok=False, message="仅允许删除保留（RESERVED）公网 IP")
            if getattr(ip, "private_ip_id", None):
                return OperationResult(ok=False, message="该保留 IP 仍绑定在实例上，请先解绑")
            self.network.delete_public_ip(public_ip_id)
            return OperationResult(ok=True, message=f"已删除保留 IP {getattr(ip, 'ip_address', '')}")
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def attach_reserved_public_ip(
        self, instance_id: str, compartment_id: str, public_ip_id: str
    ) -> OperationResult:
        """Bind a reserved public IP to the instance primary private IP.

        If the private IP currently holds an EPHEMERAL public IP it is deleted
        first (that address is lost). A reserved IP already assigned elsewhere
        is refused — unbind it explicitly first.
        """
        try:
            target = self.network.get_public_ip(public_ip_id).data
            if str(getattr(target, "lifetime", "") or "").upper() != "RESERVED":
                return OperationResult(ok=False, message="所选公网 IP 不是保留（RESERVED）类型")
            if getattr(target, "private_ip_id", None):
                return OperationResult(ok=False, message="该保留 IP 已绑定其他实例，请先解绑")

            network = self.resolve_primary_network(
                instance_id, compartment_id, include_resource_details=True
            )
            if network.public_ip_id and network.public_ip_lifetime.upper() == "RESERVED":
                return OperationResult(
                    ok=False,
                    message="实例当前已绑定保留 IP，请先解绑后再更换",
                )
            if network.public_ip_id:
                # Drop the ephemeral address, then wait for the binding to clear.
                self.network.delete_public_ip(network.public_ip_id)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        lookup = oci.core.models.GetPublicIpByPrivateIpIdDetails(
                            private_ip_id=network.private_ip_id
                        )
                        self.network.get_public_ip_by_private_ip_id(lookup)
                        time.sleep(1)
                    except ServiceError as exc:
                        if getattr(exc, "status", None) == 404:
                            break
                        raise
            update = oci.core.models.UpdatePublicIpDetails(private_ip_id=network.private_ip_id)
            ip = self.network.update_public_ip(public_ip_id, update).data
            return OperationResult(
                ok=True,
                message=f"保留 IP {ip.ip_address} 已绑定到实例（旧临时 IP 已释放）",
                data={"ip_address": ip.ip_address, "public_ip_id": ip.id},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def detach_reserved_public_ip(self, public_ip_id: str) -> OperationResult:
        """Unassign a reserved public IP (the address stays reserved for reuse)."""
        try:
            ip = self.network.get_public_ip(public_ip_id).data
            if str(getattr(ip, "lifetime", "") or "").upper() != "RESERVED":
                return OperationResult(ok=False, message="仅支持解绑保留（RESERVED）公网 IP")
            if not getattr(ip, "private_ip_id", None):
                return OperationResult(ok=True, message="该保留 IP 未绑定任何实例")
            update = oci.core.models.UpdatePublicIpDetails(private_ip_id="")
            self.network.update_public_ip(public_ip_id, update)
            return OperationResult(
                ok=True,
                message=f"保留 IP {getattr(ip, 'ip_address', '')} 已解绑（地址保留，未删除）",
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    # ------------------------------------------------------------------
    # Boot volume backups
    # ------------------------------------------------------------------
    def list_boot_volume_backups(
        self,
        compartment_id: Optional[str] = None,
        boot_volume_id: Optional[str] = None,
    ) -> list[dict]:
        compartment = (compartment_id or self.resolve_compartment()).strip()
        kwargs: dict[str, Any] = {"compartment_id": compartment}
        if boot_volume_id:
            kwargs["boot_volume_id"] = boot_volume_id
        try:
            resp = oci.pagination.list_call_get_all_results(
                self.blockstorage.list_boot_volume_backups, **kwargs
            )
        except ServiceError as exc:
            raise OCIClientError(_format_service_error(exc)) from exc
        items: list[dict] = []
        for b in resp.data:
            state = str(getattr(b, "lifecycle_state", "") or "")
            if state.upper() == "TERMINATED":
                continue
            items.append(
                {
                    "id": b.id,
                    "display_name": getattr(b, "display_name", "") or "",
                    "boot_volume_id": getattr(b, "boot_volume_id", "") or "",
                    "lifecycle_state": state,
                    "type": getattr(b, "type", "") or "",
                    "size_in_gbs": getattr(b, "size_in_gbs", None),
                    "unique_size_in_gbs": getattr(b, "unique_size_in_gbs", None),
                    "time_created": str(getattr(b, "time_created", "") or ""),
                }
            )
        return items

    def create_boot_volume_backup(
        self, boot_volume_id: str, display_name: str = "", backup_type: str = "INCREMENTAL"
    ) -> OperationResult:
        try:
            details = oci.core.models.CreateBootVolumeBackupDetails(
                boot_volume_id=boot_volume_id,
                display_name=(display_name or "").strip() or None,
                type=(backup_type or "INCREMENTAL").upper(),
            )
            b = self.blockstorage.create_boot_volume_backup(details).data
            return OperationResult(
                ok=True,
                message=f"已提交引导卷备份：{b.display_name or b.id}",
                data={"backup_id": b.id, "lifecycle_state": b.lifecycle_state},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def delete_boot_volume_backup(self, backup_id: str) -> OperationResult:
        try:
            self.blockstorage.delete_boot_volume_backup(backup_id)
            return OperationResult(ok=True, message="已删除引导卷备份")
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def _boot_volume_attachments(self, volume_id: str, availability_domain: str, compartment_id: str) -> list:
        """Live (non-detached) attachments for a boot volume.

        Separate from the block-volume path: boot volumes have their own
        attachment API (`list_boot_volume_attachments`), and it takes the AD
        positionally-first, unlike `list_volume_attachments`. Getting that wrong
        is not loud — it raises TypeError, which an `except Exception` around the
        guard would swallow, leaving "still attached?" silently answered "no".
        That exact mistake is recorded in delete_block_volume above.
        """
        atts = self.compute.list_boot_volume_attachments(
            availability_domain, compartment_id, boot_volume_id=volume_id
        ).data or []
        return [
            a
            for a in atts
            if str(getattr(a, "lifecycle_state", "") or "") not in {"DETACHED", "DETACHING", ""}
        ]

    def delete_boot_volume(self, volume_id: str) -> OperationResult:
        """Delete a detached boot volume. Irreversible — the data is gone.

        Exists because terminating an instance with "preserve boot volume" (the
        default in the OCI console) leaves the volume behind, and those orphans
        keep consuming the tenancy's 200 GB Always Free block-storage allowance.
        The panel already counts them (`free_quota.summarize_storage` ->
        `orphan_boot_count`) and shows the number in the quota panel; until now
        there was no way to act on it.

        Refuses an attached volume rather than letting OCI reject it later: the
        API error for that case names neither the volume nor the instance, so the
        operator is left guessing which of several look-alike orphans they hit.
        """
        volume_id = (volume_id or "").strip()
        if not volume_id:
            return OperationResult(ok=False, message="缺少 boot volume id")
        try:
            vol = self.blockstorage.get_boot_volume(volume_id).data
            state = str(getattr(vol, "lifecycle_state", "") or "")
            if state not in {"AVAILABLE", "FAULTY"}:
                return OperationResult(
                    ok=False, message=f"引导卷状态为 {state}，无法删除（需要先卸载或等待操作完成）"
                )
            ad = getattr(vol, "availability_domain", "") or ""
            cid = getattr(vol, "compartment_id", "") or self.resolve_compartment()
            if ad:
                try:
                    live = self._boot_volume_attachments(volume_id, ad, cid)
                except Exception as exc:  # noqa: BLE001
                    # Fail CLOSED. An unreadable attachment list means we cannot
                    # prove the volume is detached, and the cost of being wrong
                    # here is destroying a running machine's disk.
                    return OperationResult(
                        ok=False,
                        message=f"无法确认引导卷是否仍被挂载，已中止删除：{exc}",
                    )
                if live:
                    inst = str(getattr(live[0], "instance_id", "") or "")
                    hint = f"（实例 {inst[-12:]}）" if inst else ""
                    return OperationResult(
                        ok=False, message=f"引导卷仍挂载在实例上{hint}，请先终止或分离该实例"
                    )
            name = str(getattr(vol, "display_name", "") or volume_id[-12:])
            size = int(getattr(vol, "size_in_gbs", 0) or 0)
            self.blockstorage.delete_boot_volume(volume_id)
            return OperationResult(
                ok=True,
                message=f"已删除引导卷「{name}」，释放 {size} GB",
                data={"size_in_gbs": size, "display_name": name},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def rename_boot_volume(self, volume_id: str, display_name: str) -> OperationResult:
        """Set a boot volume's display name.

        Orphans are all created as "<terminated instance name> (Boot Volume)", so
        after a couple of rebuilds the list is several near-identical rows and the
        operator cannot tell which one is safe to delete. Renaming is the cheapest
        way to make that decision recoverable.
        """
        volume_id = (volume_id or "").strip()
        name = (display_name or "").strip()
        if not volume_id:
            return OperationResult(ok=False, message="缺少 boot volume id")
        if not name:
            return OperationResult(ok=False, message="名称不能为空")
        # OCI caps display names at 255; truncate rather than let the API reject
        # the whole call over a detail the operator cannot see.
        name = name[:255]
        try:
            self.blockstorage.update_boot_volume(
                volume_id,
                oci.core.models.UpdateBootVolumeDetails(display_name=name),
            )
            return OperationResult(ok=True, message=f"已重命名为「{name}」")
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    # ------------------------------------------------------------------
    # Block (data) volumes
    # ------------------------------------------------------------------

    @staticmethod
    def _volume_perf_label(vpu: int) -> str:
        if vpu <= 10:
            return "平衡"
        if vpu <= 20:
            return "较高性能"
        return "超高性能"

    @staticmethod
    def _ts_iso(value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:
                return str(value)
        return str(value)

    def list_block_volumes(
        self,
        *,
        compartment_id: Optional[str] = None,
        include_subcompartments: bool = True,
        include_attachments: bool = True,
    ) -> OperationResult:
        """List block (data) volumes under a compartment subtree."""
        root = (compartment_id or self.resolve_compartment()).strip()
        comps: list[str] = [root]
        # Same reason as list_boot_volumes: block storage counts against the same
        # 200GB free cap, so an enumeration failure that is not reported becomes
        # headroom the guard believes in.
        enum_error = ""
        if include_subcompartments:
            try:
                comps = [c["id"] for c in self.list_compartments(parent_id=root, subtree=True, strict=True)]
                if root not in comps:
                    comps.insert(0, root)
            except Exception as exc:  # noqa: BLE001
                comps = [root]
                enum_error = f"子区间枚举失败：{exc}"

        try:
            ads = self.list_availability_domains()
        except Exception:
            ads = []
        if not ads:
            ads = [""]

        volumes: list[dict] = []
        seen: set[str] = set()
        errors: list[str] = []

        for cid in comps:
            for ad in ads:
                try:
                    kwargs: dict[str, Any] = {"compartment_id": cid}
                    if ad:
                        kwargs["availability_domain"] = ad
                    resp = oci.pagination.list_call_get_all_results(
                        self.blockstorage.list_volumes,
                        **kwargs,
                    )
                    for vol in resp.data or []:
                        vid = getattr(vol, "id", "") or ""
                        if not vid or vid in seen:
                            continue
                        state = str(getattr(vol, "lifecycle_state", "") or "")
                        if state in {"TERMINATED", "TERMINATING"}:
                            continue
                        seen.add(vid)
                        vpu = int(getattr(vol, "vpus_per_gb", 10) or 10)
                        volumes.append(
                            {
                                "id": vid,
                                "display_name": getattr(vol, "display_name", "") or vid[-12:],
                                "size_in_gbs": int(getattr(vol, "size_in_gbs", 0) or 0),
                                "vpus_per_gb": vpu,
                                "performance_label": self._volume_perf_label(vpu),
                                "lifecycle_state": state,
                                "availability_domain": getattr(vol, "availability_domain", "") or ad,
                                "compartment_id": getattr(vol, "compartment_id", "") or cid,
                                "time_created": self._ts_iso(getattr(vol, "time_created", None)),
                                "instance_id": "",
                                "instance_name": "",
                                "attachment_id": "",
                                "attachment_state": "",
                                "attachment_type": "",
                                "kind": "block",
                            }
                        )
                except ServiceError as exc:
                    errors.append(_format_service_error(exc))
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))

        if include_attachments and volumes:
            by_key: dict[tuple[str, str], list[dict]] = {}
            for v in volumes:
                key = (v.get("availability_domain") or "", v.get("compartment_id") or root)
                by_key.setdefault(key, []).append(v)

            attach_map: dict[str, dict[str, str]] = {}
            for (ad, cid), _group in by_key.items():
                if not ad:
                    continue
                try:
                    # ComputeClient.list_volume_attachments is
                    # (compartment_id, **kwargs) — only ONE positional, unlike
                    # list_boot_volume_attachments which takes (availability_domain,
                    # compartment_id). Passing the AD positionally raised TypeError
                    # into the bare except below, so attachment data was always
                    # empty and every block volume looked unattached.
                    atts = oci.pagination.list_call_get_all_results(
                        self.compute.list_volume_attachments,
                        cid,
                        availability_domain=ad,
                    ).data
                except Exception:
                    atts = []
                for att in atts or []:
                    vol_id = getattr(att, "volume_id", "") or ""
                    if not vol_id:
                        continue
                    state = str(getattr(att, "lifecycle_state", "") or "")
                    if state in {"DETACHED", "DETACHING"}:
                        continue
                    attach_map[vol_id] = {
                        "instance_id": getattr(att, "instance_id", "") or "",
                        "attachment_id": getattr(att, "id", "") or "",
                        "attachment_state": state,
                        "attachment_type": str(getattr(att, "attachment_type", "") or getattr(type(att), "__name__", "") or ""),
                    }

            name_cache: dict[str, str] = {}
            for _vid, info in attach_map.items():
                iid = info.get("instance_id") or ""
                if not iid or iid in name_cache:
                    continue
                try:
                    inst = self.compute.get_instance(iid).data
                    name_cache[iid] = str(getattr(inst, "display_name", "") or iid[-12:])
                except Exception:
                    name_cache[iid] = iid[-12:]

            for v in volumes:
                info = attach_map.get(v["id"]) or {}
                iid = info.get("instance_id") or ""
                v["instance_id"] = iid
                v["instance_name"] = name_cache.get(iid, "")
                v["attachment_id"] = info.get("attachment_id") or ""
                v["attachment_state"] = info.get("attachment_state") or ""
                v["attachment_type"] = info.get("attachment_type") or ""

        volumes.sort(
            key=lambda x: (
                0 if x.get("instance_id") else 1,
                str(x.get("display_name") or "").lower(),
            )
        )
        total_gb = sum(int(v.get("size_in_gbs") or 0) for v in volumes)
        attached = sum(1 for v in volumes if v.get("instance_id"))
        orphaned = len(volumes) - attached
        msg = f"共 {len(volumes)} 个块卷 · 合计 {total_gb} GB · 已挂载 {attached} · 未挂载 {orphaned}"
        if errors and not volumes:
            return OperationResult(ok=False, message="; ".join(errors[:3]), data={"volumes": [], "summary": {}})
        # See list_boot_volumes: appended after the hard-failure branch so it flags a
        # partial read without failing an empty-but-healthy compartment.
        if enum_error:
            errors.append(enum_error)
        if errors:
            msg += f"（部分 compartment 读取失败 {len(errors)} 处）"
        return OperationResult(
            ok=True,
            message=msg,
            data={
                "volumes": volumes,
                "summary": {
                    "count": len(volumes),
                    "total_gb": total_gb,
                    "attached": attached,
                    "orphaned": orphaned,
                },
                "errors": errors[:10],
            },
        )

    def create_block_volume(
        self,
        *,
        compartment_id: str,
        availability_domain: str,
        size_in_gbs: int,
        display_name: str = "",
        vpus_per_gb: int = 10,
    ) -> OperationResult:
        size_in_gbs = int(size_in_gbs)
        vpus_per_gb = int(vpus_per_gb or 10)
        if not 50 <= size_in_gbs <= 32768:
            return OperationResult(ok=False, message="块卷大小必须在 50–32768 GB 之间")
        if vpus_per_gb not in (10, 20) and not 30 <= vpus_per_gb <= 120:
            return OperationResult(ok=False, message="性能必须为 10、20 或 30–120 VPUs/GB")
        ad = (availability_domain or "").strip()
        if not ad:
            return OperationResult(ok=False, message="必须指定可用域")
        try:
            details = oci.core.models.CreateVolumeDetails(
                compartment_id=(compartment_id or self.resolve_compartment()).strip(),
                availability_domain=ad,
                size_in_gbs=size_in_gbs,
                display_name=(display_name or "").strip() or None,
                vpus_per_gb=vpus_per_gb,
            )
            vol = self.blockstorage.create_volume(details).data
            return OperationResult(
                ok=True,
                message=f"已创建块卷：{getattr(vol, 'display_name', '') or vol.id}",
                data={
                    "id": vol.id,
                    "display_name": getattr(vol, "display_name", "") or "",
                    "size_in_gbs": int(getattr(vol, "size_in_gbs", size_in_gbs) or size_in_gbs),
                    "lifecycle_state": str(getattr(vol, "lifecycle_state", "") or ""),
                    "availability_domain": getattr(vol, "availability_domain", "") or ad,
                },
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def delete_block_volume(self, volume_id: str) -> OperationResult:
        volume_id = (volume_id or "").strip()
        if not volume_id:
            return OperationResult(ok=False, message="缺少 volume_id")
        try:
            vol = self.blockstorage.get_volume(volume_id).data
            state = str(getattr(vol, "lifecycle_state", "") or "")
            if state not in {"AVAILABLE", "FAULTY"}:
                return OperationResult(ok=False, message=f"块卷状态为 {state}，无法删除（请先卸载）")
            # Refuse if still attached
            ad = getattr(vol, "availability_domain", "") or ""
            cid = getattr(vol, "compartment_id", "") or self.resolve_compartment()
            if ad:
                try:
                    # AD is a keyword here (see list_volume_attachments signature);
                    # positionally it raised TypeError, so this "still attached"
                    # guard silently passed and delete was attempted regardless.
                    atts = self.compute.list_volume_attachments(
                        cid, availability_domain=ad, volume_id=volume_id
                    ).data or []
                    live = [
                        a
                        for a in atts
                        if str(getattr(a, "lifecycle_state", "") or "")
                        not in {"DETACHED", "DETACHING", ""}
                    ]
                    if live:
                        return OperationResult(ok=False, message="块卷仍挂载在实例上，请先卸载")
                except Exception:
                    pass
            self.blockstorage.delete_volume(volume_id)
            return OperationResult(ok=True, message="已删除块卷")
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def update_block_volume(
        self,
        volume_id: str,
        *,
        size_in_gbs: Optional[int] = None,
        vpus_per_gb: Optional[int] = None,
    ) -> OperationResult:
        volume_id = (volume_id or "").strip()
        if not volume_id:
            return OperationResult(ok=False, message="缺少 volume_id")
        if size_in_gbs is None and vpus_per_gb is None:
            return OperationResult(ok=False, message="未指定新的大小或性能")
        if vpus_per_gb is not None and vpus_per_gb not in (10, 20) and not 30 <= int(vpus_per_gb) <= 120:
            return OperationResult(ok=False, message="性能必须为 10、20 或 30–120 VPUs/GB")
        if size_in_gbs is not None and not 50 <= int(size_in_gbs) <= 32768:
            return OperationResult(ok=False, message="块卷大小必须在 50–32768 GB 之间")
        try:
            cur = self.blockstorage.get_volume(volume_id).data
            cur_size = int(getattr(cur, "size_in_gbs", 0) or 0)
            if size_in_gbs is not None and int(size_in_gbs) < cur_size:
                return OperationResult(ok=False, message=f"块卷只能扩大（当前 {cur_size} GB）")
            details = oci.core.models.UpdateVolumeDetails()
            if size_in_gbs is not None:
                details.size_in_gbs = int(size_in_gbs)
            if vpus_per_gb is not None:
                details.vpus_per_gb = int(vpus_per_gb)
            self.blockstorage.update_volume(volume_id, details)
            parts = []
            if size_in_gbs is not None:
                parts.append(f"{int(size_in_gbs)} GB")
            if vpus_per_gb is not None:
                parts.append(f"{int(vpus_per_gb)} VPUs/GB")
            return OperationResult(
                ok=True,
                message="块卷已调整：" + " · ".join(parts),
                data={"id": volume_id, "previous_size_in_gbs": cur_size},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def attach_volume(
        self,
        instance_id: str,
        volume_id: str,
        *,
        type: str = "PARAVIRTUALIZED",
        device: Optional[str] = None,
    ) -> OperationResult:
        instance_id = (instance_id or "").strip()
        volume_id = (volume_id or "").strip()
        if not instance_id or not volume_id:
            return OperationResult(ok=False, message="缺少 instance_id 或 volume_id")
        att_type = (type or "PARAVIRTUALIZED").strip().upper()
        if att_type not in {"PARAVIRTUALIZED", "ISCSI"}:
            return OperationResult(ok=False, message="挂载类型必须为 PARAVIRTUALIZED 或 ISCSI")
        try:
            inst = self.compute.get_instance(instance_id).data
            compartment_id = getattr(inst, "compartment_id", "") or self.resolve_compartment()
            if att_type == "ISCSI":
                details = oci.core.models.AttachIScsiVolumeDetails(
                    instance_id=instance_id,
                    volume_id=volume_id,
                    display_name=f"ocibot-{volume_id[-8:]}",
                    device=(device or None),
                )
            else:
                details = oci.core.models.AttachParavirtualizedVolumeDetails(
                    instance_id=instance_id,
                    volume_id=volume_id,
                    display_name=f"ocibot-{volume_id[-8:]}",
                    device=(device or None),
                )
            att = self.compute.attach_volume(details).data
            return OperationResult(
                ok=True,
                message=f"已提交挂载（{att_type}）",
                data={
                    "attachment_id": getattr(att, "id", "") or "",
                    "lifecycle_state": str(getattr(att, "lifecycle_state", "") or ""),
                    "type": att_type,
                    "compartment_id": compartment_id,
                },
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def detach_volume(self, attachment_id: str) -> OperationResult:
        attachment_id = (attachment_id or "").strip()
        if not attachment_id:
            return OperationResult(ok=False, message="缺少 attachment_id")
        try:
            self.compute.detach_volume(attachment_id)
            return OperationResult(ok=True, message="已提交卸载")
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def list_volume_attachments(self, instance_id: str, compartment_id: str = "") -> OperationResult:
        instance_id = (instance_id or "").strip()
        if not instance_id:
            return OperationResult(ok=False, message="缺少 instance_id")
        try:
            inst = self.compute.get_instance(instance_id).data
            ad = getattr(inst, "availability_domain", "") or ""
            cid = (compartment_id or getattr(inst, "compartment_id", "") or self.resolve_compartment()).strip()
            atts = self.compute.list_volume_attachments(
                cid, availability_domain=ad, instance_id=instance_id
            ).data or []
            items = []
            for a in atts:
                state = str(getattr(a, "lifecycle_state", "") or "")
                if state in {"DETACHED"}:
                    continue
                items.append(
                    {
                        "id": getattr(a, "id", "") or "",
                        "volume_id": getattr(a, "volume_id", "") or "",
                        "instance_id": getattr(a, "instance_id", "") or instance_id,
                        "lifecycle_state": state,
                        "attachment_type": str(
                            getattr(a, "attachment_type", "") or getattr(type(a), "__name__", "") or ""
                        ),
                        "device": getattr(a, "device", "") or "",
                        "time_created": self._ts_iso(getattr(a, "time_created", None)),
                    }
                )
            return OperationResult(ok=True, message=f"{len(items)} 个附件", data={"attachments": items})
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    # ------------------------------------------------------------------
    # Object Storage
    # ------------------------------------------------------------------

    def get_object_namespace(self) -> OperationResult:
        if self.object_storage is None:
            return OperationResult(ok=False, message="Object Storage 客户端不可用")
        try:
            if self._object_namespace:
                return OperationResult(ok=True, message="", data={"namespace": self._object_namespace})
            ns = self.object_storage.get_namespace().data
            self._object_namespace = str(ns or "")
            return OperationResult(ok=True, message="", data={"namespace": self._object_namespace})
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def list_buckets(self, compartment_id: str = "") -> OperationResult:
        if self.object_storage is None:
            return OperationResult(ok=False, message="Object Storage 客户端不可用")
        try:
            ns_res = self.get_object_namespace()
            if not ns_res.ok:
                return ns_res
            namespace = (ns_res.data or {}).get("namespace") or ""
            cid = (compartment_id or self.resolve_compartment()).strip()
            resp = oci.pagination.list_call_get_all_results(
                self.object_storage.list_buckets,
                namespace,
                cid,
            )
            items = []
            for b in resp.data or []:
                items.append(
                    {
                        "name": getattr(b, "name", "") or "",
                        "namespace": namespace,
                        "compartment_id": getattr(b, "compartment_id", "") or cid,
                        "time_created": self._ts_iso(getattr(b, "time_created", None)),
                        "public_access_type": str(getattr(b, "public_access_type", "") or ""),
                    }
                )
            items.sort(key=lambda x: str(x.get("name") or "").lower())
            return OperationResult(
                ok=True,
                message=f"{len(items)} 个存储桶",
                data={"namespace": namespace, "buckets": items},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def create_bucket(
        self,
        name: str,
        compartment_id: str = "",
        *,
        public_access_type: str = "NoPublicAccess",
    ) -> OperationResult:
        if self.object_storage is None:
            return OperationResult(ok=False, message="Object Storage 客户端不可用")
        name = (name or "").strip()
        if not name or not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9.\-_]{0,254}$", name):
            return OperationResult(ok=False, message="存储桶名称无效")
        try:
            ns_res = self.get_object_namespace()
            if not ns_res.ok:
                return ns_res
            namespace = (ns_res.data or {}).get("namespace") or ""
            cid = (compartment_id or self.resolve_compartment()).strip()
            access = (public_access_type or "NoPublicAccess").strip()
            if access not in {"NoPublicAccess", "ObjectRead", "ObjectReadWithoutList"}:
                access = "NoPublicAccess"
            details = oci.object_storage.models.CreateBucketDetails(
                name=name,
                compartment_id=cid,
                public_access_type=access,
            )
            b = self.object_storage.create_bucket(namespace, details).data
            return OperationResult(
                ok=True,
                message=f"已创建存储桶：{name}",
                data={"name": getattr(b, "name", name), "namespace": namespace, "compartment_id": cid},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def delete_bucket(self, name: str, namespace: str = "") -> OperationResult:
        if self.object_storage is None:
            return OperationResult(ok=False, message="Object Storage 客户端不可用")
        name = (name or "").strip()
        if not name:
            return OperationResult(ok=False, message="缺少桶名")
        try:
            if not namespace:
                ns_res = self.get_object_namespace()
                if not ns_res.ok:
                    return ns_res
                namespace = (ns_res.data or {}).get("namespace") or ""
            self.object_storage.delete_bucket(namespace, name)
            return OperationResult(ok=True, message=f"已删除存储桶：{name}")
        except ServiceError as exc:
            msg = _format_service_error(exc)
            if "BucketNotEmpty" in msg or "not empty" in msg.lower():
                msg = f"存储桶非空，请先删除对象后再删桶（{msg}）"
            return OperationResult(ok=False, message=msg)
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def list_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        limit: int = 200,
        namespace: str = "",
    ) -> OperationResult:
        if self.object_storage is None:
            return OperationResult(ok=False, message="Object Storage 客户端不可用")
        bucket = (bucket or "").strip()
        if not bucket:
            return OperationResult(ok=False, message="缺少桶名")
        limit = max(1, min(int(limit or 200), 1000))
        try:
            if not namespace:
                ns_res = self.get_object_namespace()
                if not ns_res.ok:
                    return ns_res
                namespace = (ns_res.data or {}).get("namespace") or ""
            # ObjectSummary.size (and the timestamps) are only populated when
            # requested; without `fields` every object came back with size=None,
            # so the object-storage usage gauge always read 0 bytes.
            kwargs: dict[str, Any] = {
                "limit": limit,
                "fields": "name,size,md5,timeCreated,timeModified",
            }
            if prefix:
                kwargs["prefix"] = prefix
            resp = self.object_storage.list_objects(namespace, bucket, **kwargs)
            data = resp.data
            objects = []
            for obj in getattr(data, "objects", None) or []:
                size = int(getattr(obj, "size", 0) or 0)
                objects.append(
                    {
                        "name": getattr(obj, "name", "") or "",
                        "size": size,
                        "size_gb": round(size / (1024**3), 6),
                        "md5": getattr(obj, "md5", "") or "",
                        "time_created": self._ts_iso(getattr(obj, "time_created", None)),
                        "time_modified": self._ts_iso(getattr(obj, "time_modified", None)),
                    }
                )
            next_start = getattr(data, "next_start_with", None) or ""
            return OperationResult(
                ok=True,
                message=f"{len(objects)} 个对象",
                data={
                    "namespace": namespace,
                    "bucket": bucket,
                    "objects": objects,
                    "next_start_with": next_start,
                    "truncated": bool(next_start),
                },
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def delete_object(self, bucket: str, object_name: str, namespace: str = "") -> OperationResult:
        if self.object_storage is None:
            return OperationResult(ok=False, message="Object Storage 客户端不可用")
        bucket = (bucket or "").strip()
        object_name = (object_name or "").strip()
        if not bucket or not object_name:
            return OperationResult(ok=False, message="缺少桶名或对象名")
        try:
            if not namespace:
                ns_res = self.get_object_namespace()
                if not ns_res.ok:
                    return ns_res
                namespace = (ns_res.data or {}).get("namespace") or ""
            self.object_storage.delete_object(namespace, bucket, object_name)
            return OperationResult(ok=True, message=f"已删除对象：{object_name}")
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def put_object(
        self,
        bucket: str,
        object_name: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        namespace: str = "",
        max_bytes: int = 10 * 1024 * 1024,
    ) -> OperationResult:
        if self.object_storage is None:
            return OperationResult(ok=False, message="Object Storage 客户端不可用")
        bucket = (bucket or "").strip()
        object_name = (object_name or "").strip()
        if not bucket or not object_name:
            return OperationResult(ok=False, message="缺少桶名或对象名")
        raw = data if isinstance(data, (bytes, bytearray)) else bytes(data or b"")
        if len(raw) > int(max_bytes):
            return OperationResult(ok=False, message=f"对象超过上限 {int(max_bytes)} 字节")
        try:
            if not namespace:
                ns_res = self.get_object_namespace()
                if not ns_res.ok:
                    return ns_res
                namespace = (ns_res.data or {}).get("namespace") or ""
            self.object_storage.put_object(
                namespace,
                bucket,
                object_name,
                raw,
                content_type=content_type or "application/octet-stream",
            )
            return OperationResult(
                ok=True,
                message=f"已上传：{object_name}（{len(raw)} 字节）",
                data={"name": object_name, "size": len(raw)},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=str(exc))

    def estimate_object_storage_usage(
        self,
        *,
        compartment_id: str = "",
        max_buckets: int = 50,
        max_objects_per_bucket: int = 5000,
        deadline_sec: float = 25.0,
    ) -> OperationResult:
        """Best-effort object storage size estimate for free-quota gauges."""
        if self.object_storage is None:
            return OperationResult(
                ok=True,
                message="Object Storage 客户端不可用，跳过对象用量",
                data={"object_storage_gb_used": 0.0, "object_buckets": [], "bucket_count": 0},
            )
        started = time.monotonic()
        notes: list[str] = []
        try:
            listed = self.list_buckets(compartment_id=compartment_id)
            if not listed.ok:
                return OperationResult(
                    ok=False,
                    message=listed.message or "列出存储桶失败",
                    data={"object_storage_gb_used": 0.0, "object_buckets": [], "bucket_count": 0},
                )
            namespace = (listed.data or {}).get("namespace") or ""
            buckets = list((listed.data or {}).get("buckets") or [])
            if len(buckets) > max_buckets:
                notes.append(f"仅统计前 {max_buckets}/{len(buckets)} 个存储桶")
                buckets = buckets[:max_buckets]

            details: list[dict[str, Any]] = []
            total_bytes = 0
            truncated = False
            for b in buckets:
                if time.monotonic() - started > deadline_sec:
                    truncated = True
                    notes.append("对象存储统计超时，结果为近似值")
                    break
                name = b.get("name") or ""
                size_bytes = 0
                obj_count = 0
                start = None
                pages = 0
                while True:
                    if time.monotonic() - started > deadline_sec:
                        truncated = True
                        break
                    if obj_count >= max_objects_per_bucket:
                        truncated = True
                        break
                    # "size" must be requested explicitly: without `fields` every
                    # ObjectSummary.size is None, so this estimator summed zeros and
                    # the object-storage quota gauge always read 0 GB.
                    kwargs: dict[str, Any] = {
                        "limit": min(1000, max_objects_per_bucket - obj_count),
                        "fields": "name,size",
                    }
                    if start:
                        kwargs["start"] = start
                    try:
                        resp = self.object_storage.list_objects(namespace, name, **kwargs)
                    except Exception as exc:  # noqa: BLE001
                        notes.append(f"桶 {name} 列举失败：{exc}")
                        break
                    data = resp.data
                    for obj in getattr(data, "objects", None) or []:
                        size_bytes += int(getattr(obj, "size", 0) or 0)
                        obj_count += 1
                    start = getattr(data, "next_start_with", None) or None
                    pages += 1
                    if not start:
                        break
                    if pages > 20:
                        truncated = True
                        break
                size_gb = round(size_bytes / (1024**3), 4)
                total_bytes += size_bytes
                details.append(
                    {
                        "name": name,
                        "namespace": namespace,
                        "compartment_id": b.get("compartment_id") or "",
                        "approximate_size_gb": size_gb,
                        "object_count": obj_count,
                    }
                )
            total_gb = round(total_bytes / (1024**3), 4)
            msg = ""
            if notes:
                msg = "；".join(notes)
            if truncated and "近似" not in msg:
                msg = (msg + "；" if msg else "") + "对象存储统计为近似值"
            return OperationResult(
                ok=True,
                message=msg,
                data={
                    "object_storage_gb_used": total_gb,
                    "object_buckets": details,
                    "bucket_count": len(details),
                    "truncated": truncated,
                    "namespace": namespace,
                },
            )
        except ServiceError as exc:
            return OperationResult(
                ok=False,
                message=_format_service_error(exc),
                data={"object_storage_gb_used": 0.0, "object_buckets": [], "bucket_count": 0},
            )
        except Exception as exc:  # noqa: BLE001
            return OperationResult(
                ok=False,
                message=str(exc),
                data={"object_storage_gb_used": 0.0, "object_buckets": [], "bucket_count": 0},
            )


class SessionManager:
    """Cache TenantSession objects keyed by tenant id; rebuild when config changes."""

    def __init__(self) -> None:
        self._sessions: dict[str, TenantSession] = {}
        self._fingerprints: dict[str, str] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _fp(tenant: TenantConfig) -> str:
        return "|".join(
            [
                tenant.user_ocid,
                tenant.tenancy_ocid,
                tenant.fingerprint,
                tenant.region,
                tenant.compartment_ocid,
                hashlib_sha16(tenant.private_key_pem),
            ]
        )

    def get(self, tenant: TenantConfig) -> TenantSession:
        with self._lock:
            fp = self._fp(tenant)
            existing = self._sessions.get(tenant.id)
            if existing and self._fingerprints.get(tenant.id) == fp:
                return existing
            if existing:
                existing.close()
            session = TenantSession(tenant)
            self._sessions[tenant.id] = session
            self._fingerprints[tenant.id] = fp
            return session

    def drop(self, tenant_id: str) -> None:
        with self._lock:
            s = self._sessions.pop(tenant_id, None)
            self._fingerprints.pop(tenant_id, None)
            if s:
                s.close()

    def close_all(self) -> None:
        with self._lock:
            for s in self._sessions.values():
                s.close()
            self._sessions.clear()
            self._fingerprints.clear()


def hashlib_sha16(text: str) -> str:
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def is_capacity_error(exc: Any) -> bool:
    """Detect Out of capacity / InternalError capacity style failures."""
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or exc)
    blob = f"{code} {message}".lower()
    # Avoid matching unrelated "capacity reservation" success paths too broadly:
    if "outofhostcapacity" in blob or "out of host capacity" in blob:
        return True
    if "out of capacity" in blob or "insufficient capacity" in blob:
        return True
    if code == "InternalError" and "capacity" in blob:
        return True
    return False


def is_capacity_message(text: str) -> bool:
    blob = (text or "").lower()
    return any(
        k in blob
        for k in (
            "outofhostcapacity",
            "out of host capacity",
            "out of capacity",
            "insufficient capacity",
        )
    ) or ("internalerror" in blob and "capacity" in blob)


def is_rate_limit_error(exc: Any) -> bool:
    """Detect OCI request throttling (HTTP 429 / TooManyRequests)."""
    status = getattr(exc, "status", None)
    try:
        if int(status) == 429:
            return True
    except (TypeError, ValueError):
        pass
    code = str(getattr(exc, "code", "") or "").lower()
    message = str(getattr(exc, "message", "") or exc).lower()
    blob = f"{code} {message}"
    return (
        "toomanyrequests" in blob
        or "too many requests" in blob
        or "user-rate limit" in blob
        or "rate limit exceeded" in blob
        or "request rate" in blob and "exceed" in blob
    )


def is_rate_limit_message(text: str) -> bool:
    blob = (text or "").lower()
    return any(
        k in blob
        for k in (
            "toomanyrequests",
            "too many requests",
            "user-rate limit",
            "rate limit exceeded",
            "[429]",
        )
    )


def _format_service_error(exc: ServiceError) -> str:
    code = getattr(exc, "code", "") or ""
    status = getattr(exc, "status", "") or ""
    message = getattr(exc, "message", "") or str(exc)
    parts = [p for p in [f"[{status}]", code, message] if p]
    text = " ".join(parts)
    # Friendly hints
    low = text.lower()
    if "notauthorized" in low or "not authenticated" in low or status == 401:
        text += "\n提示：请检查 API Key、Fingerprint、Tenancy/User OCID 是否匹配。"
    elif status == 404:
        text += "\n提示：资源不存在，或当前用户对该 Compartment 无权限。"
    elif is_rate_limit_error(exc) or "too many requests" in low or status == 429:
        text += "\n提示：请求过于频繁（API 限流）。容量重试会自动拉长间隔，请勿缩短重试周期。"
    elif is_capacity_error(exc):
        text += "\n提示：当前可用域容量不足，可启用「容量重试」（默认间隔 ≥60 秒，有限次数）。"
    return text


def run_in_thread(fn: Callable, on_success: Callable, on_error: Callable) -> threading.Thread:
    """Utility: run blocking OCI call off the UI thread."""

    def wrapper() -> None:
        try:
            result = fn()
            on_success(result)
        except Exception as exc:  # noqa: BLE001
            on_error(exc)

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return t
