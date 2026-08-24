"""Shared Always-Free quota enforcement for launch / shape / storage mutations."""

from __future__ import annotations

import errno
import hashlib
import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from app import free_quota

log = logging.getLogger("ocibot.quota")

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


def resolve_secondary(session: Any, row: Any) -> bool:
    """把「这是不是副区」的判定收在一个地方，DB hint 和 OCI 读取的优先级只写一次。

    优先级：一次**成功**的 OCI 读取说了算；读不出来（region_pair 返回 ""）才退回
    DB 里的 parent_tenant_id。反过来写会出事 —— 一个 region 恰好等于主区的子行
    会被当成副区，而副区是整段跳过免费额度检查的，等于在唯一有 Always Free 的
    区域里关掉了守卫。enforce_secondary_region 里有同样的判断，两处必须一致。
    """
    current, home = region_pair(session)
    if current and home:
        return current != home
    return tenant_is_secondary(row)


def secondary_region_gate(session: Any, row: Any, *, free_only_mode: bool) -> str:
    """``enforce_secondary_region`` for callers that hold the tenant row.

    Returns a warning (副区, billing accepted), "" (home region), or raises 400.
    A non-empty return means the Always-Free caps do not apply and the caller must
    skip them rather than stack them: in a 副区 the usage snapshot counts only that
    region, so a free-cap check there compares a paid resource against an allowance
    it does not have — blocking a resize the user is deliberately paying for.
    """
    return enforce_secondary_region(
        session,
        free_only_mode=free_only_mode,
        secondary_hint=tenant_is_secondary(row),
        region_hint=str(getattr(row, "region", "") or ""),
    )


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
    # 一次**成功**的读取说「当前就是主区」时，它必须压过 DB 的 secondary_hint。
    #
    # hint 的职责（见上方 docstring）是在 OCI 读取失败时兜底，而不是推翻一次读成功
    # 的结论。以前 hint 排在前面，于是一个 region 恰好等于主区的「副区」行会被
    # 当成副区：enforce_launch_quota 被整段跳过，免费额度检查在**唯一存在
    # Always Free 的区域**里失效。自相矛盾的报错「副区「ap-tokyo-1」…（主区
    # ap-tokyo-1）」就是这个 bug 的外在表现。
    if current and home and current == home:
        return ""
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


def _blocked_by_incomplete_read(
    usage: dict[str, Any],
    free_only_mode: bool,
    account_tier: Optional[str] = None,
) -> Optional[str]:
    """Reason to refuse, or None. Only hard-capped (non-paid) accounts are blocked.

    「谁是硬上限」必须和真正做判断的 free_quota.validate_* 用**同一个**谓词，
    否则两边会漂移，而且是往危险的方向漂：这里原来只看 free_only_mode，校验器
    却用 ``free_only or tier in {"", "free", "unknown"}``。于是 account_tier="free"
    但取消了「仅使用免费额度」的租户，读得到用量时被硬挡（A1 额度不足 → 400），
    读失败时这里不拦，快照退化成零用量，同样的硬上限反而轻松通过 → 200 + 真的发出
    LaunchInstance。限流路径比正常路径更宽松，等于守卫在最需要它的时候失效。

    ``account_tier=None`` 表示调用方手上没有 tier，只能退回 free_only 单独判断
    （老行为）；有 tier 的调用方一律要传，见 free_quota.hard_free_caps。
    """
    if not usage.get("read_incomplete"):
        return None
    hard = (
        bool(free_only_mode)
        if account_tier is None
        else free_quota.hard_free_caps(bool(free_only_mode), account_tier)
    )
    if not hard:
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
    count: int = 1,
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
        count=count,
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
    count: int = 1,
) -> free_quota.GuardResult:
    """Validate a launch (or capacity-retry primary config). Raises HTTP 400 if blocked.

    ``count`` validates the whole batch at once: the free caps are tenancy-wide
    totals, so checking one instance and then creating several would let the
    batch through at N times the allowance.

    这是一次 check-then-act：本函数只给出**这一刻**的判决，额度不会被预留。
    真正要发 LaunchInstance 的调用方必须把「取快照 → 判决 → LaunchInstance」整段
    包在 ``tenant_launch_lock(tenant_id)`` 里，否则两次并发创建会各自看到同一份
    「已用 0」的快照而双双放行。见文件末尾 tenant_launch_lock 的说明。
    """
    # One snapshot for the primary config and every fallback below.
    effective_free_only = (
        free_only_for_tier(account_tier) if free_only_mode is None else bool(free_only_mode)
    )
    usage = _usage_snapshot(session, free_only_mode=effective_free_only)
    # tier 必须一起传：免费账号即便关掉了 free_only 仍是硬上限，只看 free_only
    # 会让一次读失败把上限整个关掉（见 _blocked_by_incomplete_read）。
    blocked = _blocked_by_incomplete_read(usage, effective_free_only, account_tier)
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
        count=count,
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
            count=count,
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
    blocked = _blocked_by_incomplete_read(usage, bool(free_only_mode), tier)
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


# ---------------------------------------------------------------------------
# 每租户创建互斥锁：让「取快照 → 判决 → LaunchInstance」不可分割
# ---------------------------------------------------------------------------
#
# 额度守卫是典型的 check-then-act，而中间那段窗口在本项目里**特别宽**：
# routers/instances.py 判决之后才调 prepare_launch_network（可能要新建 NSG 甚至
# VCN，几十秒）。窗口里没有任何互斥，于是：
#   * 两个标签页 / 一次双击各 POST 一次 4 OCPU + 24GB 的 A1，双方都读到「已用
#     0」，双双通过，创建出 8 OCPU / 48GB —— Always Free A1 允许量的两倍；
#   * 抢机 job 正在为租户 T 重试，操作员同时手动为 T 创建，同上。
# idempotency_key 挡不住：它去重的是**同一次提交**的重发，两次有意的提交带的是
# 不同的 key。MAX_ACTIVE_RETRIES_PER_TENANT / 一租户一个活跃 job 也只约束 job 之
# 间，管不到 job 对手动创建、更管不到两次手动创建。
#
# 三层，按同一顺序获取，因此不会互相死锁：
#   1) 同线程重入直接放行（避免自锁）；
#   2) 进程内 threading.Lock（同一个 uvicorn worker 的多个请求线程）；
#   3) 跨进程：PostgreSQL 用 pg_try_advisory_xact_lock（API 默认起
#      OCIBOT_API_WORKERS=2 个进程，worker 还是独立容器，靠数据库才能拉通，
#      参见 routers/auth.py 里首管理员那把 advisory 锁）；SQLite 用数据库文件
#      旁边的 OS 文件锁——SQLite 部署里 API 与 worker 必然共享同一个库文件，
#      所以那个目录必然是两边都看得见的同一个目录。文件锁不可用时（平台/文件
#      系统不支持）退回只有第 2 层，并打一条警告，而不是把创建整个卡死。

# Wait budget for the whole acquire (process lock + cross-process lock). Long
# enough to outlast one launch's network prep, short enough that a queued browser
# request fails with a clear 409 instead of hanging until the proxy times out.
LAUNCH_LOCK_TIMEOUT_SEC = 60.0
_LOCK_POLL_SEC = 0.2
# classid for the two-int advisory form. PostgreSQL keeps (int4,int4) locks in a
# different space from the single-bigint form auth.py uses, so these can never
# collide with 87201401 whatever the tenant hash is.
_LAUNCH_LOCK_CLASSID = 0x4C41554E  # "LAUN"

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()


class TenantLaunchLockBusy(HTTPException):
    """Another create for this tenant is mid-flight — refuse rather than race it.

    HTTPException 子类：HTTP 调用方什么都不用做就得到 409；worker 这类非 HTTP
    调用方可以单独 catch 本类型，把这次尝试推迟掉（不要算作一次 attempt）。
    """

    def __init__(self, tenant_id: str = "") -> None:
        super().__init__(
            status_code=409,
            detail=(
                "该租户已有一次创建正在进行中（另一个标签页或抢机任务），"
                "为避免重复占用免费额度已拒绝本次请求，请稍后重试"
            ),
            headers={"Retry-After": "10"},
        )
        self.tenant_id = str(tenant_id or "")


def _held_keys() -> set[str]:
    keys = getattr(_HELD, "keys", None)
    if keys is None:
        keys = set()
        _HELD.keys = keys
    return keys


def launch_lock_held(tenant_id: str) -> bool:
    """True when THIS thread already holds the launch lock for the tenant."""
    return str(tenant_id or "").strip() in _held_keys()


def _process_lock(key: str) -> threading.Lock:
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _advisory_key(key: str) -> int:
    """Stable signed int4 derived from the tenant id (pg advisory takes int4 pairs)."""
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big", signed=True)


def _sleep_until(deadline: float) -> None:
    time.sleep(max(0.01, min(_LOCK_POLL_SEC, deadline - time.monotonic())))


@contextmanager
def _pg_advisory_lock(key: str, deadline: float) -> Iterator[None]:
    """pg_try_advisory_xact_lock on a dedicated connection, polled until deadline.

    自己开一条连接而不是复用请求的 Session：Session 在这段窗口里会 commit
    （写审计、写 CapacityJob），事务一结束事务级 advisory 锁就没了，而会话级的
    锁又可能在 commit 后被归还到连接池、解锁时落到另一条连接上。事务级 + 专用
    连接的组合还有个好处：进程被杀连接断开，锁自动释放，不会留下死锁。
    """
    from sqlalchemy import text

    from web.backend.db import engine

    stmt = text("SELECT pg_try_advisory_xact_lock(:cls, :key)")
    params = {"cls": _LAUNCH_LOCK_CLASSID, "key": _advisory_key(key)}
    conn = engine.connect()
    try:
        while True:
            if bool(conn.execute(stmt, params).scalar()):
                break
            if time.monotonic() >= deadline:
                raise TenantLaunchLockBusy(key)
            _sleep_until(deadline)
        yield
    finally:
        try:
            conn.rollback()  # 事务结束 = 释放 advisory 锁
        except Exception:  # noqa: BLE001
            pass
        conn.close()


def _try_lock_file(handle: Any) -> Optional[bool]:
    """True=acquired, False=someone else holds it, None=no usable lock on this platform.

    区分「被别人占着」和「这个平台/文件系统根本不支持」很重要：把不支持当成占着
    会让每次创建都空转到超时然后 409，等于把功能锁死。
    """
    busy = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}
    try:
        import fcntl
    except ImportError:
        fcntl = None  # type: ignore[assignment]
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            return False if exc.errno in busy else None
    try:
        import msvcrt
    except ImportError:
        return None
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError as exc:
        return False if exc.errno in busy else None


def _unlock_file(handle: Any) -> None:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    except ImportError:
        pass
    except OSError:
        return
    try:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:  # noqa: BLE001
        pass


def _lock_file_path(key: str) -> Optional[Path]:
    """Lock file next to the SQLite database, so every process sharing it agrees.

    刻意不用 /tmp：API 和 worker 若跑在不同容器里，各自的临时目录是分开的，
    锁就各锁各的等于没锁；而 SQLite 部署下两边必须访问同一个数据库文件，
    它所在的目录是唯一保证「同一个、且可写」的位置。
    """
    try:
        import tempfile

        from sqlalchemy.engine.url import make_url

        from web.backend.config import get_settings

        settings = get_settings()
        url = make_url(settings.database_url)
        base: Optional[Path] = None
        # Only a SQLite URL's `database` is a filesystem path — on PostgreSQL it is
        # the database NAME, which would resolve to a lock file in the cwd.
        if settings.is_sqlite and (url.database or "") not in ("", ":memory:") and not str(
            url.database or ""
        ).startswith("file:"):
            base = Path(str(url.database)).expanduser().resolve().parent
        if base is None:
            base = Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        return base / f".ocibot-launch-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}.lock"
    except Exception as exc:  # noqa: BLE001
        log.warning("launch lock file path unavailable (%s); in-process only", exc)
        return None


@contextmanager
def _file_lock(key: str, deadline: float) -> Iterator[None]:
    path = _lock_file_path(key)
    if path is None:
        yield
        return
    try:
        handle = open(path, "a+b")  # noqa: SIM115
    except OSError as exc:
        log.warning("launch lock file %s unusable (%s); in-process only", path, exc)
        yield
        return
    locked = False
    try:
        while True:
            got = _try_lock_file(handle)
            if got is None:
                log.warning("no OS file lock available; launch lock is in-process only")
                break
            if got:
                locked = True
                break
            if time.monotonic() >= deadline:
                raise TenantLaunchLockBusy(key)
            _sleep_until(deadline)
        yield
    finally:
        if locked:
            _unlock_file(handle)
        handle.close()


@contextmanager
def _cross_process_lock(key: str, deadline: float) -> Iterator[None]:
    try:
        from web.backend.config import get_settings

        sqlite = bool(get_settings().is_sqlite)
    except Exception:  # noqa: BLE001
        sqlite = True
    if sqlite:
        with _file_lock(key, deadline):
            yield
    else:
        with _pg_advisory_lock(key, deadline):
            yield


@contextmanager
def tenant_launch_lock(
    tenant_id: str, *, timeout_sec: float = LAUNCH_LOCK_TIMEOUT_SEC
) -> Iterator[None]:
    """Serialize everything that consumes free quota for one tenant.

    调用方必须把**取用量快照 → enforce_launch_quota → LaunchInstance**整段包进来。
    只包判决那一句没有意义：额度是在 LaunchInstance 之后才变的，判决与创建之间
    只要还有窗口，第二个请求就仍会读到旧数字。

    拿不到锁时抛 ``TenantLaunchLockBusy``（HTTP 409），不是静默放行——放行等于回到
    原来的双花。等待成功后第二个请求会**重新取快照**，此时前一台已是 PROVISIONING
    并计入 summarize_instances（只跳过 TERMINATED/TERMINATING），于是被额度正常挡下。
    """
    key = str(tenant_id or "").strip()
    if not key:
        # 没有租户标识就无从互斥；调用方给了空 id 属于 bug，但不该因此拒绝服务。
        yield
        return
    held = _held_keys()
    if key in held:
        yield  # 同线程重入
        return
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    lock = _process_lock(key)
    remaining = deadline - time.monotonic()
    acquired = lock.acquire(timeout=remaining) if remaining > 0 else lock.acquire(blocking=False)
    if not acquired:
        raise TenantLaunchLockBusy(key)
    held.add(key)
    try:
        with _cross_process_lock(key, deadline):
            yield
    finally:
        held.discard(key)
        lock.release()
