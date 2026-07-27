"""Shared Always-Free quota enforcement for launch / shape / storage mutations."""

from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import HTTPException

from app import free_quota

# An OCI region id: realm-city-index, e.g. ap-tokyo-1 / eu-frankfurt-1.
_REGION_ID = re.compile(r"^[a-z]{2,3}-[a-z]+-\d+$")


def region_pair(session: Any) -> tuple[str, str]:
    """``(session_region, home_region)`` — both "" unless BOTH look like real region ids.

    Deliberately strict: callers use a mismatch to decide that a launch is
    billable, so an unreadable or stubbed value must fall back to "treat as home
    region" rather than block every launch.
    """
    try:
        current = str(getattr(getattr(session, "tenant", None), "region", "") or "").strip().lower()
        home = str(session.home_region() or "").strip().lower()
    except Exception:  # noqa: BLE001
        return "", ""
    if not _REGION_ID.match(current) or not _REGION_ID.match(home):
        return "", ""
    return current, home


def tenant_is_secondary(row: Any) -> bool:
    """True for a 副区 tenant row (one created by 开通副区, linked to a primary).

    Second, independent signal to ``region_pair``: it holds even when the Oracle
    region-subscription read fails, which is the case where the probe alone would
    fall back to "home region" and let the free-cap guard run on a region whose
    usage is not the tenancy's.
    """
    return bool(getattr(row, "parent_tenant_id", "") or "")


def is_secondary_region(session: Any) -> bool:
    """True when this session targets a 副区 rather than the tenancy's home region."""
    current, home = region_pair(session)
    return bool(current and home and current != home)


def enforce_secondary_region(
    session: Any,
    *,
    free_only_mode: bool,
    secondary_hint: bool = False,
    region_hint: str = "",
) -> str:
    """Gate a create in a 副区. Returns a warning to surface, or raises HTTP 400.

    Always Free resources exist **only in the tenancy's home region** — Oracle
    bills everything created in a subscribed secondary region, whatever the shape
    is called. The per-region usage snapshot cannot see that: read from a fresh
    副区 it reports zero A1 usage and would happily wave through a second
    "free" 4 OCPU / 24 GB machine on top of the home region's.

    So the tenant's explicit ``free_only_mode`` flag decides, exactly as it does
    for oversized configurations: on = refuse, off = allow with a billing warning.

    ``secondary_hint`` / ``region_hint`` let a caller add what the DB already
    knows (see ``tenant_is_secondary``) so the verdict does not depend on an OCI
    read succeeding.
    """
    current, home = region_pair(session)
    if not secondary_hint and (not current or current == home):
        return ""
    region_text = current or (region_hint or "").strip() or "副区"
    home_text = home or "主区"
    if free_only_mode:
        raise HTTPException(
            status_code=400,
            detail=(
                f"副区「{region_text}」不在 Always Free 范围内，创建的资源会按量计费。"
                f"（主区为 {home_text}）如确需在副区创建，请先在「租户」页取消该副区租户的"
                "「仅使用免费额度」勾选。"
            ),
        )
    return f"副区「{region_text}」不属于 Always Free（主区 {home_text}），该实例会按量计费"


def free_only_for_tenant(row: Any) -> bool:
    """Whether to hard-enforce the Always-Free caps for this tenant.

    Read from the tenant's explicit ``free_only_mode`` flag (default True) rather
    than inferred from ``account_tier``. Inferring it was wrong: an Oracle account
    that was ever upgraded reports "paid", so a user who only wants free resources
    got a warning instead of a block — e.g. 50GB already used plus a 200GB boot
    volume (250 > 200) sailed through. Deliberate overage is now an explicit opt-out
    per tenant instead of a guess about intent.
    """
    return bool(getattr(row, "free_only_mode", True))


def free_only_for_tier(account_tier: str = "") -> bool:
    """Tier-only fallback for call sites that have no tenant row.

    Prefer free_only_for_tenant(). Only an explicit "paid" opts out here, because an
    unrecognized string (a typo, or a value imported from a backup) must not silently
    disable the caps.
    """
    return (account_tier or "").strip().lower() != "paid"


def usage_snapshot(session: Any, *, free_only_mode: bool = True) -> dict[str, Any]:
    """Public alias — the worker takes its own snapshot to decide whether to defer."""
    return _usage_snapshot(session, free_only_mode=free_only_mode)


def _usage_snapshot(session: Any, *, free_only_mode: bool = True) -> dict[str, Any]:
    """Always-Free usage snapshot, flagged when the underlying reads were partial.

    An exception — or a snapshot the OCI layer marked ``read_incomplete`` — used to
    come back as ``{}``, which the validators read as "nothing in use, full quota
    free". That is the wrong direction for a guard whose whole job is to stop
    accidental Oracle charges, so the flag is preserved for callers to act on.
    """
    try:
        result = session.get_free_quota_usage(free_only_mode=free_only_mode)
        data = result.data if isinstance(result.data, dict) else {}
        if not data:
            return {"read_incomplete": True}
        return data
    except Exception:
        return {"read_incomplete": True}


def _blocked_by_incomplete_read(usage: dict[str, Any], free_only_mode: bool) -> Optional[str]:
    """Reason to refuse, or None. Only hard-capped (non-paid) accounts are blocked."""
    if not free_only_mode or not usage.get("read_incomplete"):
        return None
    return (
        "无法完整读取 Always Free 用量（Oracle API 报错或限流），"
        "为避免超额产生费用已阻止本次操作，请稍后重试"
    )


def check_launch_quota(
    session: Any,
    *,
    account_tier: str = "",
    shape: str,
    ocpus: Any = None,
    memory_in_gbs: Any = None,
    boot_volume_size_in_gbs: Any = None,
    boot_volume_vpus_per_gb: Any = 10,
    free_only_mode: Optional[bool] = None,
    usage: Optional[dict[str, Any]] = None,
) -> free_quota.GuardResult:
    """Return a GuardResult without raising (for worker / soft checks).

    ``usage`` lets a caller reuse one snapshot across several checks; each
    snapshot is a full tenancy enumeration against the OCI API, so validating a
    primary config plus five fallbacks used to cost six of them.
    """
    tier = (account_tier or "").strip()
    if free_only_mode is None:
        free_only_mode = free_only_for_tier(tier)
    if usage is None:
        usage = _usage_snapshot(session, free_only_mode=bool(free_only_mode))
    tier = str(usage.get("account_tier") or tier or "")
    return free_quota.validate_launch_against_quota(
        shape=shape,
        ocpus=ocpus,
        memory_in_gbs=memory_in_gbs,
        boot_volume_size_in_gbs=boot_volume_size_in_gbs,
        boot_volume_vpus_per_gb=boot_volume_vpus_per_gb,
        free_only_mode=bool(free_only_mode),
        account_tier=tier,
        usage=usage,
    )


def enforce_launch_quota(
    session: Any,
    *,
    account_tier: str = "",
    shape: str,
    ocpus: Any = None,
    memory_in_gbs: Any = None,
    boot_volume_size_in_gbs: Any = None,
    boot_volume_vpus_per_gb: Any = 10,
    free_only_mode: Optional[bool] = None,
    fallback_configs: Optional[list[dict[str, Any]]] = None,
) -> free_quota.GuardResult:
    """Validate a launch (or capacity-retry primary config). Raises HTTP 400 if blocked."""
    # One snapshot for the primary config and every fallback below.
    effective_free_only = (
        free_only_for_tier(account_tier) if free_only_mode is None else bool(free_only_mode)
    )
    usage = _usage_snapshot(session, free_only_mode=effective_free_only)
    blocked = _blocked_by_incomplete_read(usage, effective_free_only)
    if blocked:
        raise HTTPException(status_code=503, detail=blocked)
    guard = check_launch_quota(
        session,
        account_tier=account_tier,
        shape=shape,
        ocpus=ocpus,
        memory_in_gbs=memory_in_gbs,
        boot_volume_size_in_gbs=boot_volume_size_in_gbs,
        boot_volume_vpus_per_gb=boot_volume_vpus_per_gb,
        free_only_mode=free_only_mode,
        usage=usage,
    )
    if not guard.ok:
        raise HTTPException(
            status_code=400,
            detail="；".join(guard.error_messages()) or "超出 Always Free 额度，已阻止创建",
        )
    # Fallback Flex configs must also stay within free caps when free-only applies.
    for fb in fallback_configs or []:
        if not isinstance(fb, dict):
            continue
        fb_guard = check_launch_quota(
            session,
            account_tier=account_tier,
            shape=shape,
            ocpus=fb.get("ocpus", ocpus),
            memory_in_gbs=fb.get("memory_in_gbs", memory_in_gbs),
            boot_volume_size_in_gbs=boot_volume_size_in_gbs,
            boot_volume_vpus_per_gb=boot_volume_vpus_per_gb,
            free_only_mode=free_only_mode,
            usage=usage,
        )
        if not fb_guard.ok:
            raise HTTPException(
                status_code=400,
                detail="降级配置超出免费额度："
                + ("；".join(fb_guard.error_messages()) or "请调整 fallback_configs"),
            )
    return guard


def enforce_shape_resize_quota(
    session: Any,
    *,
    account_tier: str = "",
    shape: str,
    current_ocpus: Any,
    current_memory_in_gbs: Any,
    new_ocpus: Any,
    new_memory_in_gbs: Any,
    free_only_mode: Optional[bool] = None,
) -> free_quota.GuardResult:
    tier = (account_tier or "").strip()
    if free_only_mode is None:
        free_only_mode = free_only_for_tier(tier)
    usage = _usage_snapshot(session, free_only_mode=bool(free_only_mode))
    blocked = _blocked_by_incomplete_read(usage, bool(free_only_mode))
    if blocked:
        raise HTTPException(status_code=503, detail=blocked)
    tier = str(usage.get("account_tier") or tier or "")
    guard = free_quota.validate_shape_resize_against_quota(
        shape=shape,
        current_ocpus=current_ocpus,
        current_memory_in_gbs=current_memory_in_gbs,
        new_ocpus=new_ocpus,
        new_memory_in_gbs=new_memory_in_gbs,
        free_only_mode=bool(free_only_mode),
        account_tier=tier,
        usage=usage,
    )
    if not guard.ok:
        raise HTTPException(
            status_code=400,
            detail="；".join(guard.error_messages()) or "超出 Always Free 额度，已阻止改规格",
        )
    return guard


def format_guard_warnings(guard: Optional[free_quota.GuardResult]) -> list[str]:
    if guard is None:
        return []
    return list(guard.warning_messages() or [])
