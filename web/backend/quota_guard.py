"""Shared Always-Free quota enforcement for launch / shape / storage mutations."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

from app import free_quota


def free_only_for_tier(account_tier: str = "") -> bool:
    """Paid accounts may overage (with warnings); free/unknown stay hard-capped."""
    tier = (account_tier or "").strip().lower()
    return tier in {"", "free", "unknown"}


def _usage_snapshot(session: Any, *, free_only_mode: bool = True) -> dict[str, Any]:
    try:
        result = session.get_free_quota_usage(free_only_mode=free_only_mode)
        return result.data if isinstance(result.data, dict) else {}
    except Exception:
        # Fail open on read errors would bill users; fail closed for free/unknown.
        return {}


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
) -> free_quota.GuardResult:
    """Return a GuardResult without raising (for worker / soft checks)."""
    tier = (account_tier or "").strip()
    if free_only_mode is None:
        free_only_mode = free_only_for_tier(tier)
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
    guard = check_launch_quota(
        session,
        account_tier=account_tier,
        shape=shape,
        ocpus=ocpus,
        memory_in_gbs=memory_in_gbs,
        boot_volume_size_in_gbs=boot_volume_size_in_gbs,
        boot_volume_vpus_per_gb=boot_volume_vpus_per_gb,
        free_only_mode=free_only_mode,
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
