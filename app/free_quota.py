"""Always Free quota tracking and launch/resize guards.

Pure helpers + thin TenantSession methods live here so Web and desktop share
the same rules. Network I/O stays on TenantSession.get_free_quota_usage().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Mirror Oracle Always Free caps (tenancy-wide). Keep in sync with oci_client.ALWAYS_FREE_LIMITS.
FREE_A1_OCPU = 4.0
FREE_A1_MEMORY_GB = 24.0
FREE_E2_MICRO_COUNT = 2
FREE_BLOCK_STORAGE_GB = 200.0
FREE_OBJECT_STORAGE_GB = 20.0
# Soft visibility only — ephemeral public IPs on free VMs are free; not a hard block.
FREE_PUBLIC_IP_SOFT = 2
# Outbound data transfer included per month (10 TB, binary units to match the byte
# counter it is compared against). Tracked for visibility/alerting only: the figure
# it is compared with is an upper bound from VCN metrics, not a bill, and egress is
# not knowable at launch time — so it never blocks a create. See
# TenantSession.get_network_egress_usage.
FREE_EGRESS_GB = 10240.0

# Default boot size assumed when the launch form leaves size empty (~Ubuntu image).
DEFAULT_BOOT_GB_ASSUMED = 47

A1_SHAPE = "VM.Standard.A1.Flex"
E2_MICRO_SHAPE = "VM.Standard.E2.1.Micro"

FREE_SHAPES = {
    A1_SHAPE: "免费 ARM",
    E2_MICRO_SHAPE: "免费 AMD",
}


def is_free_shape(shape: str) -> bool:
    return (shape or "").strip() in FREE_SHAPES


def is_a1_shape(shape: str) -> bool:
    s = (shape or "").strip()
    return s == A1_SHAPE or s.endswith(".A1.Flex") or "A1.Flex" in s


def is_e2_micro_shape(shape: str) -> bool:
    s = (shape or "").strip().lower()
    return s == E2_MICRO_SHAPE.lower() or s.endswith(".e2.1.micro") or "e2.1.micro" in s


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def shape_resource_units(shape: str, ocpus: Any = None, memory_in_gbs: Any = None) -> dict[str, float]:
    """Return free-quota units consumed by one instance of this shape."""
    shape = (shape or "").strip()
    if is_e2_micro_shape(shape):
        return {"a1_ocpu": 0.0, "a1_memory_gb": 0.0, "e2_micro_count": 1.0}
    if is_a1_shape(shape):
        cpu = _as_float(ocpus, 0.0)
        mem = _as_float(memory_in_gbs, 0.0)
        # Flex without explicit config: treat as zero rather than inventing 4/24.
        return {"a1_ocpu": max(0.0, cpu), "a1_memory_gb": max(0.0, mem), "e2_micro_count": 0.0}
    return {"a1_ocpu": 0.0, "a1_memory_gb": 0.0, "e2_micro_count": 0.0}


def summarize_instances(instances: list[Any]) -> dict[str, Any]:
    """Aggregate free-tier compute usage from InstanceInfo-like objects or dicts."""
    a1_ocpu = 0.0
    a1_mem = 0.0
    e2_count = 0
    public_ips = 0
    items: list[dict[str, Any]] = []
    for inst in instances or []:
        if isinstance(inst, dict):
            state = str(inst.get("lifecycle_state") or "")
            shape = str(inst.get("shape") or "")
            ocpus = inst.get("ocpus", inst.get("ocpus"))
            memory = inst.get("memory_in_gbs", inst.get("memory_gb"))
            name = str(inst.get("display_name") or inst.get("id") or "")
            public_ip = str(inst.get("public_ip") or "")
            iid = str(inst.get("id") or "")
        else:
            state = str(getattr(inst, "lifecycle_state", "") or "")
            shape = str(getattr(inst, "shape", "") or "")
            ocpus = getattr(inst, "ocpus", None)
            memory = getattr(inst, "memory_gb", None)
            if memory is None:
                memory = getattr(inst, "memory_in_gbs", None)
            name = str(getattr(inst, "display_name", "") or getattr(inst, "id", "") or "")
            public_ip = str(getattr(inst, "public_ip", "") or "")
            iid = str(getattr(inst, "id", "") or "")
        if state.upper() in {"TERMINATED", "TERMINATING"}:
            continue
        units = shape_resource_units(shape, ocpus, memory)
        a1_ocpu += units["a1_ocpu"]
        a1_mem += units["a1_memory_gb"]
        e2_count += int(units["e2_micro_count"])
        if public_ip:
            public_ips += 1
        items.append(
            {
                "id": iid,
                "display_name": name,
                "shape": shape,
                "lifecycle_state": state,
                "ocpus": ocpus,
                "memory_in_gbs": memory,
                "units": units,
            }
        )
    return {
        "a1_ocpu_used": round(a1_ocpu, 4),
        "a1_memory_gb_used": round(a1_mem, 4),
        "e2_micro_count_used": e2_count,
        "public_ip_count": public_ips,
        "instance_count": len(items),
        "instances": items,
    }


def summarize_storage(volumes: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0.0
    boot = 0.0
    block = 0.0
    orphan_boot = 0
    details: list[dict[str, Any]] = []
    for v in volumes or []:
        size = _as_float(v.get("size_in_gbs"), 0.0)
        kind = str(v.get("kind") or v.get("type") or "boot").lower()
        state = str(v.get("lifecycle_state") or "").upper()
        if state in {"TERMINATED", "TERMINATING"}:
            continue
        total += size
        if kind == "block":
            block += size
        else:
            boot += size
            if not v.get("instance_id"):
                orphan_boot += 1
        details.append(
            {
                "id": v.get("id") or "",
                "display_name": v.get("display_name") or "",
                "size_in_gbs": size,
                "kind": kind,
                "instance_id": v.get("instance_id") or "",
                "lifecycle_state": state,
            }
        )
    return {
        "block_storage_gb_used": round(total, 4),
        "boot_volume_gb_used": round(boot, 4),
        "block_volume_gb_used": round(block, 4),
        "orphan_boot_count": orphan_boot,
        "volume_count": len(details),
        "volumes": details,
    }


def summarize_object_storage(buckets: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate approximate object-storage usage from bucket size dicts."""
    total = 0.0
    details: list[dict[str, Any]] = []
    for b in buckets or []:
        size = _as_float(
            b.get("approximate_size_gb", b.get("size_in_gbs", b.get("size_gb"))),
            0.0,
        )
        if size < 0:
            size = 0.0
        total += size
        details.append(
            {
                "name": str(b.get("name") or b.get("id") or ""),
                "namespace": str(b.get("namespace") or ""),
                "compartment_id": str(b.get("compartment_id") or ""),
                "approximate_size_gb": round(size, 4),
                "object_count": _as_int(b.get("object_count"), 0),
            }
        )
    return {
        "object_storage_gb_used": round(total, 4),
        "bucket_count": len(details),
        "object_buckets": details,
    }


def build_quota_snapshot(
    *,
    instances: list[Any],
    volumes: list[dict[str, Any]],
    free_only_mode: bool = True,
    account_tier: str = "",
    notes: Optional[list[str]] = None,
    object_usage: Optional[dict[str, Any]] = None,
    egress_usage: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Combine instance + volume (+ optional object / egress) usage into a snapshot.

    ``egress_usage`` is optional because it costs an extra Monitoring query and can
    never block a create: callers on the launch/guard hot path omit it, and the
    bucket is then absent rather than present-and-zero, which would read as
    "10 TB still free" on a page that could not check.
    """
    compute = summarize_instances(instances)
    storage = summarize_storage(volumes)
    object_usage = object_usage or {}
    if "object_storage_gb_used" not in object_usage and object_usage.get("object_buckets") is not None:
        object_usage = summarize_object_storage(list(object_usage.get("object_buckets") or []))
    elif "object_storage_gb_used" not in object_usage and isinstance(object_usage.get("buckets"), list):
        object_usage = summarize_object_storage(list(object_usage.get("buckets") or []))

    a1_ocpu_used = float(compute["a1_ocpu_used"])
    a1_mem_used = float(compute["a1_memory_gb_used"])
    e2_used = int(compute["e2_micro_count_used"])
    disk_used = float(storage["block_storage_gb_used"])
    object_used = _as_float(object_usage.get("object_storage_gb_used"), 0.0)
    public_ips = int(compute["public_ip_count"])
    object_buckets = list(object_usage.get("object_buckets") or [])

    def _bucket(used: float, limit: float, *, soft: bool = False) -> dict[str, Any]:
        remaining = max(0.0, float(limit) - float(used))
        ratio = (float(used) / float(limit)) if limit else 0.0
        if used > limit + 1e-9:
            # Soft caps (public IP) never contribute a hard "over" to overall.
            status = "critical" if soft else "over"
        elif limit and used >= limit - 1e-9:
            # Exactly at the cap. On a *free allowance* that is the intended end
            # state — two of two free micros running is the whole point — so it is
            # reported as "full", not as an alarm. Calling it critical made the
            # launch page show a red verdict for a launch it was about to allow,
            # which read as "the check is wrong".
            status = "full"
        elif ratio >= 0.9:
            status = "critical"
        elif ratio >= 0.7:
            status = "warn"
        else:
            status = "ok"
        return {
            "used": round(float(used), 4),
            "limit": float(limit),
            "remaining": round(remaining, 4),
            "ratio": round(min(ratio, 9.99), 4),
            "status": status,
            "soft": bool(soft),
        }

    buckets = {
        "a1_ocpu": _bucket(a1_ocpu_used, FREE_A1_OCPU),
        "a1_memory_gb": _bucket(a1_mem_used, FREE_A1_MEMORY_GB),
        "e2_micro_count": _bucket(float(e2_used), float(FREE_E2_MICRO_COUNT)),
        "block_storage_gb": _bucket(disk_used, FREE_BLOCK_STORAGE_GB),
        "object_storage_gb": _bucket(object_used, FREE_OBJECT_STORAGE_GB),
        "public_ip_soft": _bucket(float(public_ips), float(FREE_PUBLIC_IP_SOFT), soft=True),
    }
    # Soft: the reading is an upper bound over one region, so it must never push the
    # overall status to "over" and make an unrelated launch look blocked.
    egress_used: Optional[float] = None
    if egress_usage:
        egress_used = _as_float(egress_usage.get("egress_gb"), 0.0)
        buckets["egress_gb"] = _bucket(egress_used, FREE_EGRESS_GB, soft=True)
    # Hard overall ignores soft-only buckets.
    overall = "ok"
    for key, b in buckets.items():
        if b.get("soft"):
            continue
        if b["status"] == "over":
            overall = "over"
            break
        # "full" is deliberately not escalated: a free account that has used its
        # whole allowance is in the expected state, and flagging it made every
        # page render an alarm that no action could clear.
        if b["status"] == "critical" and overall != "over":
            overall = "critical"
        elif b["status"] == "warn" and overall == "ok":
            overall = "warn"

    lines = [
        f"A1 {buckets['a1_ocpu']['remaining']:g}/{FREE_A1_OCPU:g} OCPU 剩余",
        f"A1 内存 {buckets['a1_memory_gb']['remaining']:g}/{FREE_A1_MEMORY_GB:g} GB 剩余",
        f"E2.Micro {int(buckets['e2_micro_count']['remaining'])}/{FREE_E2_MICRO_COUNT} 台剩余",
        f"块存储 {buckets['block_storage_gb']['remaining']:g}/{FREE_BLOCK_STORAGE_GB:g} GB 剩余",
        f"对象存储 {buckets['object_storage_gb']['remaining']:g}/{FREE_OBJECT_STORAGE_GB:g} GB 剩余",
        f"公网 IP {int(buckets['public_ip_soft']['remaining'])}/{FREE_PUBLIC_IP_SOFT}（软追踪）",
    ]
    if egress_used is not None:
        lines.append(
            f"出网流量 本月约 {egress_used:g}/{FREE_EGRESS_GB:g} GB（估算上限）"
        )
    limits = {
        "a1_ocpu": FREE_A1_OCPU,
        "a1_memory_gb": FREE_A1_MEMORY_GB,
        "e2_micro_count": FREE_E2_MICRO_COUNT,
        "block_storage_gb": FREE_BLOCK_STORAGE_GB,
        "object_storage_gb": FREE_OBJECT_STORAGE_GB,
        "public_ip_soft": FREE_PUBLIC_IP_SOFT,
    }
    usage_out: dict[str, Any] = {
        "a1_ocpu": a1_ocpu_used,
        "a1_memory_gb": a1_mem_used,
        "e2_micro_count": e2_used,
        "block_storage_gb": disk_used,
        "boot_volume_gb": storage["boot_volume_gb_used"],
        "block_volume_gb": storage["block_volume_gb_used"],
        "object_storage_gb": object_used,
        "public_ip_count": public_ips,
        "instance_count": compute["instance_count"],
        "orphan_boot_count": storage["orphan_boot_count"],
    }
    remaining_out = {
        "a1_ocpu": buckets["a1_ocpu"]["remaining"],
        "a1_memory_gb": buckets["a1_memory_gb"]["remaining"],
        "e2_micro_count": buckets["e2_micro_count"]["remaining"],
        "block_storage_gb": buckets["block_storage_gb"]["remaining"],
        "object_storage_gb": buckets["object_storage_gb"]["remaining"],
        "public_ip_soft": buckets["public_ip_soft"]["remaining"],
    }
    if egress_used is not None:
        limits["egress_gb"] = FREE_EGRESS_GB
        usage_out["egress_gb"] = egress_used
        usage_out["egress_region"] = str((egress_usage or {}).get("region") or "")
        usage_out["egress_approximate"] = True
        remaining_out["egress_gb"] = buckets["egress_gb"]["remaining"]
    return {
        "free_only_mode": bool(free_only_mode),
        "account_tier": account_tier or "",
        "limits": limits,
        "usage": usage_out,
        "remaining": remaining_out,
        "buckets": buckets,
        "overall_status": overall,
        "summary_lines": lines,
        "instances": compute["instances"],
        "volumes": storage["volumes"],
        "object_buckets": object_buckets,
        "notes": list(notes or []),
    }


@dataclass
class GuardIssue:
    code: str
    message: str
    severity: str = "error"  # error | warn

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class GuardResult:
    ok: bool
    issues: list[GuardIssue] = field(default_factory=list)
    warnings: list[GuardIssue] = field(default_factory=list)
    projected: dict[str, Any] = field(default_factory=dict)

    def error_messages(self) -> list[str]:
        return [i.message for i in self.issues if i.severity == "error"]

    def warning_messages(self) -> list[str]:
        return [i.message for i in self.warnings] + [
            i.message for i in self.issues if i.severity == "warn"
        ]

    def raise_if_blocked(self) -> None:
        errs = self.error_messages()
        if errs:
            raise ValueError("；".join(errs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blocked": not self.ok,
            "errors": self.error_messages(),
            "warnings": self.warning_messages(),
            "issues": [i.to_dict() for i in self.issues],
            "projected": self.projected,
        }


def validate_launch_against_quota(
    *,
    shape: str,
    ocpus: Any = None,
    memory_in_gbs: Any = None,
    boot_volume_size_in_gbs: Any = None,
    boot_volume_vpus_per_gb: Any = 10,
    free_only_mode: bool = True,
    account_tier: str = "",
    usage: Optional[dict[str, Any]] = None,
    enforce_storage_cap: Optional[bool] = None,
    count: int = 1,
) -> GuardResult:
    """Validate a launch request against free-tier caps / free-only mode.

    ``count`` is how many identical instances the caller intends to create. The
    caps apply to the TOTAL, so this must be checked once for the whole batch:
    validating a single instance and then launching four of them would wave
    through four times the free allowance.

    Blocking policy:
    - free_only_mode ON: only free shapes; stay within free remaining for A1/E2/disk.
    - free_only_mode OFF + free account: still block over free remaining (avoid surprise bills).
    - free_only_mode OFF + paid account: allow overage (may bill); still warn.
    """
    usage = usage or {}
    used = usage.get("usage") or usage
    rem = usage.get("remaining") or {}

    a1_used = _as_float(used.get("a1_ocpu"), 0.0)
    a1_mem_used = _as_float(used.get("a1_memory_gb"), 0.0)
    e2_used = _as_int(used.get("e2_micro_count"), 0)
    disk_used = _as_float(used.get("block_storage_gb"), 0.0)

    a1_rem = _as_float(rem.get("a1_ocpu"), max(0.0, FREE_A1_OCPU - a1_used))
    a1_mem_rem = _as_float(rem.get("a1_memory_gb"), max(0.0, FREE_A1_MEMORY_GB - a1_mem_used))
    e2_rem = _as_int(rem.get("e2_micro_count"), max(0, FREE_E2_MICRO_COUNT - e2_used))

    free_only = bool(free_only_mode)
    tier = (account_tier or "").strip().lower()
    # Free accounts always hard-guard free caps even if free_only was toggled off by mistake.
    hard_free_caps = free_only or tier in {"", "free", "unknown"}
    if enforce_storage_cap is None:
        enforce_storage_cap = hard_free_caps

    issues: list[GuardIssue] = []
    warnings: list[GuardIssue] = []
    shape = (shape or "").strip()
    n = max(1, _as_int(count, 1))
    per_unit = shape_resource_units(shape, ocpus, memory_in_gbs)
    # Everything below reasons about the whole batch.
    units = {k: v * n for k, v in per_unit.items()}

    boot = boot_volume_size_in_gbs
    if boot in (None, ""):
        boot_gb = float(DEFAULT_BOOT_GB_ASSUMED)
        boot_assumed = True
    else:
        boot_gb = float(_as_int(boot, DEFAULT_BOOT_GB_ASSUMED))
        boot_assumed = False

    vpu = _as_int(boot_volume_vpus_per_gb, 10)

    if free_only and not is_free_shape(shape):
        issues.append(
            GuardIssue(
                "non_free_shape",
                f"仅免费模式已开启：不允许创建付费 Shape「{shape or '—'}」。请使用 A1.Flex 或 E2.1.Micro。",
            )
        )

    if is_a1_shape(shape):
        need_cpu = units["a1_ocpu"]
        need_mem = units["a1_memory_gb"]
        if need_cpu <= 0 or need_mem <= 0:
            issues.append(
                GuardIssue(
                    "a1_spec_required",
                    "A1.Flex 必须指定 OCPU 与内存（免费上限 4 OCPU / 24 GB）。",
                )
            )
        if need_cpu > FREE_A1_OCPU or need_mem > FREE_A1_MEMORY_GB:
            msg = (
                f"A1 规格 {need_cpu:g} OCPU / {need_mem:g} GB 已超过免费上限 "
                f"{FREE_A1_OCPU:g} / {FREE_A1_MEMORY_GB:g}。"
            )
            if hard_free_caps:
                issues.append(GuardIssue("a1_over_free_cap", msg))
            else:
                warnings.append(GuardIssue("a1_over_free_cap", msg + " 付费账号将产生费用。", "warn"))
        elif need_cpu > a1_rem + 1e-9 or need_mem > a1_mem_rem + 1e-9:
            msg = (
                f"A1 额度不足：需要 {need_cpu:g} OCPU / {need_mem:g} GB，"
                f"剩余 {a1_rem:g} OCPU / {a1_mem_rem:g} GB。"
            )
            if hard_free_caps:
                issues.append(GuardIssue("a1_insufficient", msg))
            else:
                warnings.append(GuardIssue("a1_insufficient", msg + " 超出部分将计费。", "warn"))

    if is_e2_micro_shape(shape):
        if e2_rem < 1:
            msg = f"E2.1.Micro 免费额度已满（{e2_used}/{FREE_E2_MICRO_COUNT}）。"
            if hard_free_caps:
                issues.append(GuardIssue("e2_insufficient", msg))
            else:
                warnings.append(GuardIssue("e2_insufficient", msg + " 继续创建可能计费。", "warn"))

    batch_boot = boot_gb * n
    projected_disk = disk_used + batch_boot
    if projected_disk > FREE_BLOCK_STORAGE_GB + 1e-9:
        assumed = "（未填 Boot 时按约 47GB 估算）" if boot_assumed else ""
        each = f"{n} × {boot_gb:g} GB = " if n > 1 else ""
        msg = (
            f"块存储将超过免费 200GB：当前已用 {disk_used:g} GB，"
            f"本次 Boot {each}{batch_boot:g} GB{assumed}，合计 {projected_disk:g} GB。"
        )
        if enforce_storage_cap:
            issues.append(GuardIssue("storage_over_free_cap", msg))
        else:
            warnings.append(GuardIssue("storage_over_free_cap", msg + " 超出将计费。", "warn"))

    if free_only and vpu > 10:
        warnings.append(
            GuardIssue(
                "high_vpu",
                f"Boot 性能 {vpu} VPUs/GB 高于平衡档(10)。Always Free 账号可能被强制回 10，"
                "部分场景也可能产生费用，请确认。",
                "warn",
            )
        )

    projected = {
        "a1_ocpu_after": round(a1_used + units["a1_ocpu"], 4),
        "a1_memory_gb_after": round(a1_mem_used + units["a1_memory_gb"], 4),
        "e2_micro_count_after": e2_used + int(units["e2_micro_count"]),
        "block_storage_gb_after": round(projected_disk, 4),
        "boot_gb": boot_gb,
        "boot_gb_total": round(batch_boot, 4),
        "boot_assumed": boot_assumed,
        "shape": shape,
        "count": n,
        "units": units,
        "units_per_instance": per_unit,
        "remaining_after": {
            "a1_ocpu": round(max(0.0, FREE_A1_OCPU - (a1_used + units["a1_ocpu"])), 4),
            "a1_memory_gb": round(max(0.0, FREE_A1_MEMORY_GB - (a1_mem_used + units["a1_memory_gb"])), 4),
            "e2_micro_count": max(0, FREE_E2_MICRO_COUNT - (e2_used + int(units["e2_micro_count"]))),
            "block_storage_gb": round(max(0.0, FREE_BLOCK_STORAGE_GB - projected_disk), 4),
        },
    }
    ok = not any(i.severity == "error" for i in issues)
    return GuardResult(ok=ok, issues=issues, warnings=warnings, projected=projected)


def validate_shape_resize_against_quota(
    *,
    shape: str,
    current_ocpus: Any,
    current_memory_in_gbs: Any,
    new_ocpus: Any,
    new_memory_in_gbs: Any,
    free_only_mode: bool = True,
    account_tier: str = "",
    usage: Optional[dict[str, Any]] = None,
) -> GuardResult:
    """Validate Flex OCPU/memory change against free remaining (excluding this instance's current)."""
    usage = usage or {}
    used = usage.get("usage") or usage
    a1_used = _as_float(used.get("a1_ocpu"), 0.0)
    a1_mem_used = _as_float(used.get("a1_memory_gb"), 0.0)
    free_only = bool(free_only_mode)
    tier = (account_tier or "").strip().lower()
    hard = free_only or tier in {"", "free", "unknown"}

    issues: list[GuardIssue] = []
    warnings: list[GuardIssue] = []
    if not is_a1_shape(shape):
        # Non-A1 flex resize is outside free tracking; free-only still blocks non-free shapes.
        if free_only and not is_free_shape(shape):
            issues.append(GuardIssue("non_free_shape", "仅免费模式不允许修改付费规格。"))
        return GuardResult(ok=not issues, issues=issues, warnings=warnings)

    cur_cpu = _as_float(current_ocpus, 0.0)
    cur_mem = _as_float(current_memory_in_gbs, 0.0)
    new_cpu = _as_float(new_ocpus, 0.0)
    new_mem = _as_float(new_memory_in_gbs, 0.0)
    others_cpu = max(0.0, a1_used - cur_cpu)
    others_mem = max(0.0, a1_mem_used - cur_mem)
    after_cpu = others_cpu + new_cpu
    after_mem = others_mem + new_mem

    if new_cpu > FREE_A1_OCPU or new_mem > FREE_A1_MEMORY_GB or after_cpu > FREE_A1_OCPU + 1e-9 or after_mem > FREE_A1_MEMORY_GB + 1e-9:
        msg = (
            f"调整后 A1 将为合计 {after_cpu:g} OCPU / {after_mem:g} GB，"
            f"超过免费上限 {FREE_A1_OCPU:g} / {FREE_A1_MEMORY_GB:g}。"
        )
        if hard:
            issues.append(GuardIssue("a1_resize_over", msg))
        else:
            warnings.append(GuardIssue("a1_resize_over", msg + " 将计费。", "warn"))

    projected = {
        "a1_ocpu_after": round(after_cpu, 4),
        "a1_memory_gb_after": round(after_mem, 4),
    }
    return GuardResult(ok=not issues, issues=issues, warnings=warnings, projected=projected)


def validate_boot_resize_against_quota(
    *,
    current_size_gb: Any,
    new_size_gb: Any,
    free_only_mode: bool = True,
    account_tier: str = "",
    usage: Optional[dict[str, Any]] = None,
) -> GuardResult:
    """Validate boot/block volume size change against the free 200GB storage cap."""
    usage = usage or {}
    used = usage.get("usage") or usage
    disk_used = _as_float(used.get("block_storage_gb"), 0.0)
    cur = _as_float(current_size_gb, 0.0)
    new = _as_float(new_size_gb, 0.0)
    free_only = bool(free_only_mode)
    tier = (account_tier or "").strip().lower()
    hard = free_only or tier in {"", "free", "unknown"}

    issues: list[GuardIssue] = []
    warnings: list[GuardIssue] = []
    if new <= cur:
        return GuardResult(ok=True, issues=issues, warnings=warnings, projected={"block_storage_gb_after": disk_used})

    others = max(0.0, disk_used - cur)
    after = others + new
    if after > FREE_BLOCK_STORAGE_GB + 1e-9:
        msg = (
            f"扩容后块存储约 {after:g} GB，超过免费 200GB（当前合计已用 {disk_used:g} GB）。"
        )
        if hard:
            issues.append(GuardIssue("storage_resize_over", msg))
        else:
            warnings.append(GuardIssue("storage_resize_over", msg + " 超出将计费。", "warn"))
    return GuardResult(
        ok=not issues,
        issues=issues,
        warnings=warnings,
        projected={"block_storage_gb_after": round(after, 4)},
    )


# Alias used by block-volume create/resize APIs (same 200GB Always Free cap).
validate_block_volume_against_quota = validate_boot_resize_against_quota
