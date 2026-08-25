"""Launch wizard helpers — reuse oci_client network/image/shape APIs."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from app.oci_client import (
    BOOT_VPU_PRESETS,
    FREE_TIER_SHAPES,
    LAUNCH_OS_FAMILIES,
    LAUNCH_QUICK_PRESETS,
    OCIClientError,
    TenantSession,
    free_tier_tag,
    generate_root_password,
    sanitize_launch_payload,
)
from app.scheduler import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_INTERVAL_SEC,
    clamp_max_attempts,
    clamp_retry_interval,
)

# tenant_id -> (monotonic_ts, meta)
_META_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_META_TTL = 15 * 60

# Shared second level, so the API's worker processes stop each paying for their
# own cold fetch. Holds ids and display names (compartments, images, shapes,
# VCN/subnet) — no credentials — in the existing app_meta key/value table.
_SHARED_META_PREFIX = "lmeta:"
# A tenancy with many custom images produces a large document. The cap only exists
# so one absurd row cannot dominate the table; it is generously above any realistic
# document (400 images + every shape is a few hundred KB) because falling back to
# "in-process only" silently disables the cross-worker cache, and that is what
# 「加载配置」's polling and the launch request both depend on. A skip is logged.
_SHARED_META_MAX_BYTES = 4 * 1024 * 1024

# Concurrent OCI reads while building the launch metadata. Small on purpose:
# enough to turn a sum of latencies into roughly the slowest one, not so many
# that a single 「加载配置」 becomes a burst against the tenancy's rate limit.
_META_FETCH_WORKERS = 6


# Every shared-cache helper runs in its OWN session, never the request's.
#
# The first version borrowed the caller's `db` and called commit()/rollback() on
# it. That commits whatever else the route had pending, and the rollback in the
# error path discards it — action at a distance with no visible connection to the
# cache. It showed up as an unrelated test losing a User row it had just created.
# Cache bookkeeping is not part of the request's transaction and must not share
# its fate in either direction.
def _cache_session():
    from web.backend.db import SessionLocal

    return SessionLocal()


# Every app_meta row this module writes is keyed through _row_key().
#
# `app_meta.key` is VARCHAR(64). SQLite ignores that width; PostgreSQL — what
# production runs — REJECTS a longer value (StringDataRightTruncation) instead of
# truncating it. The raw cache key is a tenant uuid + region + a tenancy OCID,
# about 127 characters, so with the old plain-text keys EVERY write to both of
# these rows failed on the real database and was swallowed by the handlers below
# (a cache must not break a request). Every test passed because they all run on
# SQLite with a stub tenancy OCID of a dozen characters.
#
# Two shipped features were silently inert because of it:
#   - 0.4.73's cross-worker meta cache — no row was ever stored, so the cold fetch
#     could still happen inside a 创建 request (the intermittent 520).
#   - 0.4.75's polling state — 「加载配置」 wrote "running", the row did not persist,
#     and the poll two seconds later found no status and no result. It read that as
#     "never started" and the page reported 加载未完成，请重试, while the fetch it had
#     just launched was in fact running normally and finished a minute later. That
#     is the first-click error: retrying appeared to work only because by then the
#     earlier run had filled the in-process cache.
#
# The tenant id stays in clear text at the front because clear_launch_meta_cache()
# deletes one tenant's rows with LIKE '<prefix><tenant_id>|%'. Only the part with no
# length bound (region + compartment/tenancy OCID) is hashed.
#
# Keep _KEY_WIDTH equal to models.AppMeta.key's width; a test asserts the composed
# keys fit it, using realistic values instead of the short stubs the other launch
# tests use.
_KEY_WIDTH = 64
_KEY_DIGEST_LEN = 16


def _row_key(prefix: str, cache_key: str) -> str:
    """app_meta key for a cache entry, guaranteed to fit the column."""
    tenant_id, _, tail = cache_key.partition("|")
    digest = hashlib.sha256(tail.encode("utf-8")).hexdigest()[:_KEY_DIGEST_LEN]
    # Derived from the column width rather than assumed: whatever room the prefix,
    # the separator and the digest leave is what the tenant id may use. Tenant ids
    # are a String(36) column and the prefixes here are 6-7 characters, so nothing
    # is actually cut — but a longer prefix cannot quietly overflow the column.
    head = max(0, _KEY_WIDTH - len(prefix) - 1 - _KEY_DIGEST_LEN)
    return f"{prefix}{tenant_id[:head]}|{digest}"


def _load_shared_meta(cache_key: str) -> Optional[tuple[float, dict[str, Any]]]:
    """Return (age_seconds, meta) from the shared cache, or None.

    Never raises: a cache is an optimisation, and a database hiccup here must not
    turn into a failed launch.
    """
    try:
        from sqlalchemy import select

        from web.backend.models import AppMeta

        with _cache_session() as db:
            row = db.scalar(
                select(AppMeta).where(AppMeta.key == _row_key(_SHARED_META_PREFIX, cache_key))
            )
            if row is None or not row.value:
                return None
            raw = row.value
        blob = json.loads(raw)
        # Wall clock, not time.monotonic(): monotonic is only comparable inside
        # one process, so storing it would make entries written by another worker
        # look arbitrarily old or new.
        age = time.time() - float(blob.get("stored_at") or 0)
        if age < 0 or age >= _META_TTL:
            return None
        meta = blob.get("meta")
        if not isinstance(meta, dict) or not meta.get("ads"):
            return None
        return age, meta
    except Exception:  # noqa: BLE001
        return None


def _store_shared_meta(cache_key: str, meta: dict[str, Any]) -> None:
    """Best-effort write of the shared cache entry."""
    log = logging.getLogger("ocibot.launch")
    try:
        from sqlalchemy import select

        from web.backend.models import AppMeta

        payload = json.dumps({"stored_at": time.time(), "meta": meta}, ensure_ascii=False)
        size = len(payload.encode("utf-8"))
        if size > _SHARED_META_MAX_BYTES:
            log.warning(
                "launch meta is %d bytes, past the shared-cache cap — cross-worker "
                "caching is inactive for this tenant",
                size,
            )
            return
        key = _row_key(_SHARED_META_PREFIX, cache_key)
        with _cache_session() as db:
            try:
                row = db.scalar(select(AppMeta).where(AppMeta.key == key))
                if row is None:
                    db.add(AppMeta(key=key, value=payload))
                else:
                    row.value = payload
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                # Logged HERE, not only in the outer handler. A commit that failed
                # inside this block was rolled back and forgotten, so the warning
                # below never fired for the one failure that actually happened in
                # production: the key overflowing app_meta.key's VARCHAR(64) on
                # PostgreSQL (see _row_key). Two releases looked healthy while this
                # cache stored nothing at all.
                log.warning(
                    "launch meta shared cache commit failed (%s: %s) — cross-worker "
                    "caching is inactive for this tenant",
                    exc.__class__.__name__,
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        # Logged, not swallowed. A failure here does not break the request — but
        # it does silently switch the cross-worker cache off, which is the thing
        # that keeps a cold fetch out of the launch request. Without this line the
        # only symptom would be the intermittent timeout quietly coming back, and
        # nothing anywhere would connect it to a serialisation problem.
        log.warning(
            "launch meta shared cache write failed (%s: %s) — cross-worker caching "
            "is inactive for this tenant, cold fetches may run inside a launch",
            exc.__class__.__name__,
            exc,
        )


_META_MAX_ENTRIES = 64


def clear_launch_meta_cache(tenant_id: Optional[str] = None) -> None:
    """Drop cached launch metadata for a tenant (or all of them).

    Clears the shared copy too. Clearing only the in-process dict would be undone
    immediately: the next request reads the stale document back out of the
    database and promotes it again, so a tenant whose region or compartment just
    changed would keep serving the old network and image lists until the TTL
    expired.
    """
    if tenant_id:
        keys = [k for k in _META_CACHE if k.startswith(f"{tenant_id}|")]
        for k in keys:
            _META_CACHE.pop(k, None)
    else:
        _META_CACHE.clear()
    try:
        from sqlalchemy import delete, or_

        from web.backend.models import AppMeta

        # Both rows for this tenant: the cached document AND the refresh status.
        # A stale status row would otherwise be the first thing launch_meta_state
        # reports after the tenant's credentials or region changed.
        #
        # `launch_meta:` / `launch_meta_status:` are the pre-0.4.77 key formats.
        # They only exist on SQLite installs — PostgreSQL rejected them for being
        # longer than app_meta.key (see _row_key) — and nothing reads them now, so
        # they are swept up here rather than left to sit forever.
        suffix = f"{tenant_id}|%" if tenant_id else "%"
        patterns = [
            _SHARED_META_PREFIX + suffix,
            _STATUS_PREFIX + suffix,
            "launch_meta:" + suffix,
            "launch_meta_status:" + suffix,
        ]
        with _cache_session() as db:
            try:
                db.execute(delete(AppMeta).where(or_(*[AppMeta.key.like(p) for p in patterns])))
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# Asynchronous refresh
#
# 「加载配置」 cannot be a single long request. It does six paginated Oracle reads
# and, on a tenancy with no network, creates a VCN and waits for it — how long
# that takes is a property of someone else's account, not something this code can
# bound. Cloudflare's free plan cuts any single request at 100 seconds, so the
# page failed with a gateway error and then worked on the next click, because the
# first attempt kept running server-side and filled the cache.
#
# Parallelising the reads (0.4.74) helped and was still not enough. Chasing the
# ceiling is the wrong game: the fix is to stop having a request that long. The
# browser now asks for a refresh, gets an answer immediately, and polls — the
# same shape the capacity-retry jobs already use.
#
# State lives in the database, not in a module variable, because the POST and the
# poll are served by different worker processes about half the time.
# --------------------------------------------------------------------------

_STATUS_PREFIX = "lmstat:"
# A refresh that has not reported in this long is treated as dead rather than
# leaving the page waiting on a process that has since restarted.
_STATUS_STALE_SEC = 15 * 60

# Threads started in THIS process, so a second click does not launch a second
# fetch. The database row covers the cross-process case.
_REFRESH_LOCK = threading.Lock()
_REFRESHING: set[str] = set()


def meta_cache_key(session: TenantSession, tenant_id: str) -> str:
    tenant = session.tenant
    return f"{tenant_id}|{tenant.region}|{tenant.compartment_ocid or tenant.tenancy_ocid}"


def _read_status(cache_key: str) -> dict[str, Any]:
    try:
        from sqlalchemy import select

        from web.backend.models import AppMeta

        with _cache_session() as db:
            row = db.scalar(
                select(AppMeta).where(AppMeta.key == _row_key(_STATUS_PREFIX, cache_key))
            )
            raw = row.value if row is not None else ""
        if not raw:
            return {}
        blob = json.loads(raw)
        if time.time() - float(blob.get("at") or 0) > _STATUS_STALE_SEC:
            return {}
        return blob if isinstance(blob, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_status(cache_key: str, state: str, error: str = "") -> None:
    log = logging.getLogger("ocibot.launch")
    try:
        from sqlalchemy import delete, select

        from web.backend.models import AppMeta

        key = _row_key(_STATUS_PREFIX, cache_key)
        with _cache_session() as db:
            try:
                if not state:
                    db.execute(delete(AppMeta).where(AppMeta.key == key))
                else:
                    payload = json.dumps(
                        {"state": state, "error": error[:500], "at": time.time()},
                        ensure_ascii=False,
                    )
                    row = db.scalar(select(AppMeta).where(AppMeta.key == key))
                    if row is None:
                        db.add(AppMeta(key=key, value=payload))
                    else:
                        row.value = payload
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                # This used to be `pass`, and that is why the bug below survived two
                # releases unseen: with the row never persisting, the poll two
                # seconds after 「加载配置」 found no status and reported 加载未完成
                # while the fetch ran on happily. A status write that fails IS the
                # page failing, so it has to be audible even though it must not
                # raise into the request.
                log.warning(
                    "launch meta status write failed (%s: %s) — the launch page will "
                    "report an unfinished load even though the refresh is running",
                    exc.__class__.__name__,
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "launch meta status write failed (%s: %s)", exc.__class__.__name__, exc
        )


def peek_launch_meta(session: TenantSession, tenant_id: str) -> Optional[dict[str, Any]]:
    """Cached metadata if it is ready, without ever calling Oracle."""
    cache_key = meta_cache_key(session, tenant_id)
    cached = _META_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < _META_TTL and cached[1].get("ads"):
        meta = dict(cached[1])
        meta["cached"] = True
        meta["cache_age_sec"] = int(time.monotonic() - cached[0])
        return meta
    shared = _load_shared_meta(cache_key)
    if shared is None:
        return None
    age, payload = shared
    _META_CACHE[cache_key] = (time.monotonic() - age, payload)
    meta = dict(payload)
    meta["cached"] = True
    meta["cache_age_sec"] = int(age)
    return meta


def launch_meta_state(session: TenantSession, tenant_id: str) -> dict[str, Any]:
    """What the polling endpoint reports: ready / running / error / idle."""
    cache_key = meta_cache_key(session, tenant_id)
    status = _read_status(cache_key)
    if status.get("state") == "error":
        return {"state": "error", "error": str(status.get("error") or "加载失败")}
    meta = peek_launch_meta(session, tenant_id)
    if meta is not None:
        return {"state": "ready", "meta": meta}
    if status.get("state") == "running":
        return {"state": "running"}
    return {"state": "idle"}


def start_meta_refresh(session: TenantSession, tenant_id: str, *, force: bool = True) -> dict[str, Any]:
    """Kick off a background refresh and return immediately.

    Idempotent: a second click while one is in flight joins the existing run
    rather than starting a competing set of Oracle calls.
    """
    cache_key = meta_cache_key(session, tenant_id)
    if not force:
        meta = peek_launch_meta(session, tenant_id)
        if meta is not None:
            return {"state": "ready", "meta": meta}
    if _read_status(cache_key).get("state") == "running":
        return {"state": "running"}
    with _REFRESH_LOCK:
        if cache_key in _REFRESHING:
            return {"state": "running"}
        _REFRESHING.add(cache_key)
    _write_status(cache_key, "running")

    def _run() -> None:
        try:
            fetch_launch_meta(session, tenant_id=tenant_id, force=True)
            _write_status(cache_key, "")  # cleared: the cache now holds the answer
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("ocibot.launch").warning(
                "launch meta refresh failed for %s: %s", tenant_id, exc
            )
            _write_status(cache_key, "error", str(exc))
        finally:
            with _REFRESH_LOCK:
                _REFRESHING.discard(cache_key)

    threading.Thread(target=_run, name=f"launch-meta-{tenant_id[:8]}", daemon=True).start()
    return {"state": "running"}


def fetch_launch_meta(
    session: TenantSession,
    *,
    tenant_id: str,
    force: bool = False,
) -> dict[str, Any]:
    """Compartments / ADs / images / shapes / network for the launch form.

    A cold call is expensive — it lists images for every OS family and, on a
    tenancy with no network, CREATES a VCN, subnet, gateway and route table,
    waiting for each to become available. A minute or more is normal.

    Results are cached in-process AND in the database. The shared level exists
    because the in-process dict is per PROCESS and the API runs
    OCIBOT_API_WORKERS=2: 加载配置 warmed one worker, and the 创建 request that
    followed had an even chance of landing on the other — where it did the whole
    cold fetch INSIDE the launch request and ran past the proxy's timeout. The
    symptom was an occasional 520 that went away on retry, because by then some
    worker had a warm cache.
    """
    tenant = session.tenant
    # One definition of the key, shared with peek/status (they must agree or a
    # refresh would publish to a row the poll never looks at).
    cache_key = meta_cache_key(session, tenant_id)
    if not force:
        cached = _META_CACHE.get(cache_key)
        if cached and time.monotonic() - cached[0] < _META_TTL and cached[1].get("ads"):
            meta = dict(cached[1])
            meta["cached"] = True
            meta["cache_age_sec"] = int(time.monotonic() - cached[0])
            return meta
        shared = _load_shared_meta(cache_key)
        if shared is not None:
            age, payload = shared
            # Promote into this process so the next hit skips the database too.
            _META_CACHE[cache_key] = (time.monotonic() - age, payload)
            meta = dict(payload)
            meta["cached"] = True
            meta["cache_age_sec"] = int(age)
            return meta

    default_comp = tenant.compartment_ocid or tenant.tenancy_ocid

    # These six reads do not depend on each other, and running them one after
    # another made the wall time their SUM. Each is a paginated round trip to
    # Oracle, and ensure_default_network additionally walks VCNs and subnets per
    # compartment — on a tenancy of any size the total ran past the 100-second
    # ceiling a proxy like Cloudflare puts on one request, which is what made
    # 「加载配置」 fail and then work on the second click (the first attempt kept
    # running server-side and populated the cache).
    #
    # Concurrency, not extra calls: the same requests are made, so this does not
    # spend any more of the tenancy's rate limit. Bounded pool and a shared
    # requests.Session across threads, matching what list_instances_tree and the
    # IP resolver in oci_client already do.
    def _images_for(fam: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        try:
            return fam["id"], session.list_images(
                # default_comp（= compartment_ocid or tenancy_ocid），不是写死租户根。
                # 两个原因：(1) 密钥的 IAM 策略可能只覆盖子 compartment，问根直接
                # 404；(2) list_images 自带一个「子 compartment 结果太少就退回根」
                # 的回退，而那个回退的条件是 `compartment != tenancy_ocid` ——
                # 直接把根传进来，恰好让这条专为此设计的退路失效。
                compartment_id=default_comp,
                operating_system=fam["operating_system"],
                ubuntu_only=(fam["id"] == "ubuntu"),
            )
        except Exception:  # noqa: BLE001
            return fam["id"], []

    def _custom_images() -> list[dict[str, Any]]:
        try:
            return session.list_custom_images(compartment_id=default_comp)
        except Exception:  # noqa: BLE001
            return []

    with ThreadPoolExecutor(max_workers=_META_FETCH_WORKERS) as pool:
        f_comps = pool.submit(session.list_compartments)
        f_ads = pool.submit(session.list_availability_domains, default_comp)
        f_shapes = pool.submit(session.list_shapes, compartment_id=default_comp)
        f_custom = pool.submit(_custom_images)
        f_network = pool.submit(
            session.ensure_default_network, compartment_id=default_comp, create_if_missing=True
        )
        f_families = [pool.submit(_images_for, fam) for fam in LAUNCH_OS_FAMILIES]

        # .result() re-raises, so the calls that used to propagate still do and
        # the ones that were individually caught above still return their empty
        # fallback. Error behaviour is unchanged, only the ordering is.
        images_by_os: dict[str, list[dict[str, Any]]] = {}
        for fut in f_families:
            fam_id, imgs = fut.result()
            images_by_os[fam_id] = imgs
        images_by_os["custom"] = f_custom.result()
        comps = f_comps.result()
        ads = f_ads.result()
        shapes = f_shapes.result()
        network = f_network.result()

    # Dependent fallbacks stay sequential: each only runs when the parallel call
    # above came back empty, so they cost nothing in the normal case.
    images = images_by_os.get("ubuntu") or []
    if not images:
        images = session.list_images(ubuntu_only=True)
        images_by_os["ubuntu"] = images
    if not shapes:
        shapes = session.list_shapes()

    # Prefer free-tier shapes for wizard defaults (same as desktop).
    free_shapes = [s for s in shapes if str(s.get("shape") or "") in FREE_TIER_SHAPES]
    if free_shapes:
        shapes_out = free_shapes
    else:
        shapes_out = shapes

    if not network.ok:
        raise OCIClientError(network.message or "无法准备默认网络（VCN/Subnet）")
    net_data = network.data or {}
    vcns = list(net_data.get("vcns") or [])
    subnets_by_vcn = dict(net_data.get("subnets_by_vcn") or {})
    meta = {
        "compartments": comps,
        "ads": ads,
        "images": images,
        "images_by_os": images_by_os,
        "os_families": LAUNCH_OS_FAMILIES,
        "shapes": shapes_out,
        "all_shapes": shapes,
        "vcns": vcns,
        "subnets_by_vcn": subnets_by_vcn,
        "default_compartment": default_comp,
        "network_note": network.message,
        "network_created": bool(net_data.get("created")),
        "preferred_vcn_id": (net_data.get("vcn") or {}).get("id", ""),
        "preferred_subnet_id": (net_data.get("subnet") or {}).get("id", ""),
        "quick_presets": LAUNCH_QUICK_PRESETS,
        "boot_vpu_presets": [{"value": v, "label": lab} for v, lab in BOOT_VPU_PRESETS],
        "free_tier_shapes": dict(FREE_TIER_SHAPES),
        "defaults": {
            "retry_interval_sec": DEFAULT_RETRY_INTERVAL_SEC,
            "retry_max_attempts": DEFAULT_MAX_ATTEMPTS,
            "display_name": f"instance-{time.strftime('%m%d%H%M')}",
        },
        "cached": False,
        "cache_age_sec": 0,
    }
    # Evict before inserting: this cache holds every tenant's full image/shape/
    # network metadata and previously never released an entry, so RSS grew with
    # every tenant and region ever visited.
    now = time.monotonic()
    for key in [k for k, (ts, _) in _META_CACHE.items() if now - ts >= _META_TTL]:
        _META_CACHE.pop(key, None)
    while len(_META_CACHE) >= _META_MAX_ENTRIES:
        oldest = min(_META_CACHE, key=lambda k: _META_CACHE[k][0])
        _META_CACHE.pop(oldest, None)
    _META_CACHE[cache_key] = (now, dict(meta))
    # Also publish to the shared cache so the OTHER worker process does not have
    # to repeat this — which is the whole reason a launch could take a minute.
    _store_shared_meta(cache_key, meta)
    return meta


def shape_is_flex(shape: str) -> bool:
    """True for *.Flex shapes (fixed shapes like E2.1.Micro must not be resized)."""
    shape_l = str(shape or "").strip().lower()
    if "e2.1.micro" in shape_l or shape_l.endswith(".micro"):
        return False
    return shape_l.endswith(".flex") or ".flex." in shape_l


def normalize_fallback_configs(
    raw_fallbacks: Any,
    *,
    is_flex: bool,
    as_retry: bool,
) -> list[dict[str, float]]:
    """Validate capacity-retry downgrade candidates. Raises ValueError when invalid.

    Shared by the launch wizard and POST /jobs/capacity so both enforce the same
    limits (Flex-only, retry-only, max 5, sane OCPU/memory).
    """
    fallback_configs: list[dict[str, float]] = []
    if not raw_fallbacks:
        return fallback_configs
    if not is_flex:
        raise ValueError("仅 *.Flex 型号支持降级配置")
    if not as_retry:
        raise ValueError("降级配置仅在容量自动重试模式下生效")
    if not isinstance(raw_fallbacks, list) or len(raw_fallbacks) > 5:
        raise ValueError("降级配置最多 5 组")
    for item in raw_fallbacks:
        if not isinstance(item, dict):
            raise ValueError("降级配置格式无效")
        try:
            # OverflowError: float() on a huge JSON integer (e.g. 10**400) raised
            # out of here as an unhandled 500 instead of the intended 400.
            fb_ocpus = float(item.get("ocpus"))
            fb_mem = float(item.get("memory_in_gbs"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("降级配置的 OCPU / 内存必须为数字") from exc
        if not (math.isfinite(fb_ocpus) and math.isfinite(fb_mem)):
            raise ValueError("降级配置的 OCPU / 内存必须为数字")
        if not (0 < fb_ocpus <= 64) or not (1 <= fb_mem <= 1024):
            raise ValueError("降级配置数值超出合理范围")
        fallback_configs.append({"ocpus": fb_ocpus, "memory_in_gbs": fb_mem})
    return fallback_configs


def build_launch_request(
    body: dict[str, Any],
    *,
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Normalize wizard body into launch kwargs + optional capacity-retry options."""
    auth_mode = str(body.get("auth_mode") or "key").strip().lower()
    if auth_mode not in {"key", "password"}:
        raise ValueError("认证方式必须为 key 或 password")

    display_name = str(body.get("display_name") or "").strip() or "instance"
    availability_domain = str(body.get("availability_domain") or "").strip()
    shape = str(body.get("shape") or "").strip()
    image_id = str(body.get("image_id") or "").strip()
    subnet_id = str(body.get("subnet_id") or "").strip()
    compartment_id = str(body.get("compartment_id") or "").strip()

    if meta:
        if not compartment_id:
            compartment_id = str(meta.get("default_compartment") or "")
        if not subnet_id:
            subnet_id = str(meta.get("preferred_subnet_id") or "")
        if not availability_domain:
            ads = meta.get("ads") or []
            if ads:
                availability_domain = ads[0]

    if not all([availability_domain, shape, image_id, subnet_id, compartment_id]):
        raise ValueError("缺少创建参数：availability_domain / shape / image_id / subnet_id / compartment_id")

    ocpus = body.get("ocpus")
    memory = body.get("memory_in_gbs")
    shape_l = shape.lower()
    is_flex = shape_l.endswith(".flex") or ".flex." in shape_l
    is_fixed_micro = "e2.1.micro" in shape_l or shape_l.endswith(".micro")
    if is_fixed_micro:
        is_flex = False
    if not is_flex:
        # Fixed shapes (e.g. VM.Standard.E2.1.Micro) must not send custom shape_config.
        ocpus = None
        memory = None
    else:
        if ocpus is not None and ocpus != "":
            ocpus = float(ocpus)
        else:
            ocpus = None
        if memory is not None and memory != "":
            memory = float(memory)
        else:
            memory = None

    boot_gb = body.get("boot_volume_size_in_gbs")
    if boot_gb in ("", None):
        boot_gb = None
    else:
        boot_gb = int(boot_gb)

    boot_vpu = int(body.get("boot_volume_vpus_per_gb") or 10)
    ssh_key = str(body.get("ssh_public_key") or "").strip()
    root_password = str(body.get("root_password") or "").strip()
    if auth_mode == "password" and not root_password:
        root_password = generate_root_password(16)
    if auth_mode == "key" and not ssh_key:
        raise ValueError("密钥模式需要 SSH 公钥")
    if auth_mode == "password" and len(root_password) < 12:
        raise ValueError("root 密码至少 12 位")

    assign_public_ip = bool(body.get("assign_public_ip", True))
    assign_ipv6_ip = bool(body.get("assign_ipv6_ip", False))
    open_guest_firewall = bool(body.get("open_guest_firewall", True))
    as_retry = bool(body.get("as_retry", False))
    retry_all_ads = bool(body.get("retry_all_ads", False))
    if as_retry and auth_mode != "key":
        raise ValueError("容量自动重试仅支持 root + SSH 公钥模式")

    # Custom first-boot script (cloud-init). Kept OUT of the persisted payload;
    # for retry jobs the caller stores it Fernet-encrypted on the job row.
    custom_user_data = str(body.get("user_data") or "").replace("\r\n", "\n").strip()
    if len(custom_user_data) > 16000:
        raise ValueError("自定义启动脚本过长（上限 16000 字符）")

    # Downgrade candidates for Flex shapes (capacity retry tries these in order
    # after the primary config fails across all ADs).
    fallback_configs = normalize_fallback_configs(
        body.get("fallback_configs") or [], is_flex=is_flex, as_retry=as_retry
    )

    launch_token = str(body.get("launch_token") or uuid.uuid4())
    payload = sanitize_launch_payload(
        {
            "display_name": display_name,
            "compartment_id": compartment_id,
            "availability_domain": availability_domain,
            "shape": shape,
            "image_id": image_id,
            "subnet_id": subnet_id,
            "ssh_public_key": ssh_key if auth_mode == "key" else "",
            "auth_mode": auth_mode,
            "ocpus": ocpus,
            "memory_in_gbs": memory,
            "assign_public_ip": assign_public_ip,
            "assign_ipv6_ip": assign_ipv6_ip,
            "boot_volume_size_in_gbs": boot_gb,
            "boot_volume_vpus_per_gb": boot_vpu,
            "nsg_ids": body.get("nsg_ids") or [],
            "open_guest_firewall": open_guest_firewall,
            "launch_token": launch_token,
        },
        for_retry=as_retry,
    )

    interval = clamp_retry_interval(body.get("retry_interval_sec") or DEFAULT_RETRY_INTERVAL_SEC)
    max_attempts = clamp_max_attempts(body.get("retry_max_attempts") or DEFAULT_MAX_ATTEMPTS)
    ads_for_retry: list[str] = []
    if as_retry and retry_all_ads and meta:
        ads_for_retry = list(meta.get("ads") or [])

    return {
        "payload": payload,
        "root_password": root_password if auth_mode == "password" else "",
        "as_retry": as_retry,
        "retry_interval_sec": interval,
        "retry_max_attempts": max_attempts,
        "availability_domains": ads_for_retry,
        "custom_user_data": custom_user_data,
        "fallback_configs": fallback_configs,
        "free_tier_tag": free_tier_tag(shape),
        "preferred_vcn_id": str((meta or {}).get("preferred_vcn_id") or ""),
        "network_compartment_id": compartment_id,
    }


def prepare_launch_network(
    session: TenantSession,
    payload: dict[str, Any],
    *,
    meta: Optional[dict[str, Any]] = None,
    for_retry: bool = False,
) -> dict[str, Any]:
    """Match desktop pre-launch: optional IPv6 enable + managed open NSG.

    Mutates and returns payload (adds vcn_id / nsg_ids / managed_nsg_id when created).
    Raises ValueError/OCIClientError on hard failures.
    """
    from app.oci_client import OCIClientError

    payload = dict(payload or {})
    subnet_id = str(payload.get("subnet_id") or "").strip()
    compartment_id = str(payload.get("compartment_id") or "").strip()
    network_compartment = str(payload.get("network_compartment_id") or compartment_id).strip()
    vcn_id = str(payload.get("vcn_id") or "").strip()
    if not vcn_id and meta:
        vcn_id = str(meta.get("preferred_vcn_id") or "")
    if not vcn_id and subnet_id:
        # Resolve VCN from subnet list in meta
        if meta:
            for subs in (meta.get("subnets_by_vcn") or {}).values():
                for s in subs or []:
                    if s.get("id") == subnet_id:
                        vcn_id = str(s.get("vcn_id") or "")
                        network_compartment = str(s.get("compartment_id") or network_compartment)
                        break
    if not vcn_id:
        # Fallback: ensure_default_network again
        net = session.ensure_default_network(compartment_id=compartment_id, create_if_missing=True)
        if not net.ok:
            raise OCIClientError(net.message or "无法准备默认网络")
        data = net.data or {}
        vcn_id = str((data.get("vcn") or {}).get("id") or "")
        if not subnet_id:
            subnet_id = str((data.get("subnet") or {}).get("id") or "")
            payload["subnet_id"] = subnet_id
        network_compartment = str((data.get("subnet") or {}).get("compartment_id") or network_compartment)

    payload["vcn_id"] = vcn_id
    payload["network_compartment_id"] = network_compartment

    if payload.get("assign_ipv6_ip"):
        ipv6 = session.ensure_subnet_ipv6(subnet_id, network_compartment or compartment_id)
        if not ipv6.ok:
            raise OCIClientError("IPv6 网络准备失败：" + (ipv6.message or ""))

    if not payload.get("managed_nsg_id") and not payload.get("nsg_ids"):
        token = str(payload.get("launch_token") or uuid.uuid4().hex)
        # open_all 必须跟着表单上那个勾选走。
        #
        # 以前这里无条件建一个 INGRESS all 0.0.0.0/0 的 NSG,而 open_guest_firewall
        # 只被送去 build_root_cloud_init 决定要不要在**客体内**关掉 ufw/iptables ——
        # 云端这一半从来没接过线。于是取消勾选之后,表单 hint 说「不放宽云端安全组」,
        # 实际照样挂上一个全协议全网段入站的 NSG;而 NSG 与 Security List 在 OCI 是
        # 并集,子网上原有的收紧规则会因此形同虚设。
        nsg = session.create_managed_nsg(
            vcn_id=vcn_id,
            compartment_id=network_compartment or compartment_id,
            display_name=str(payload.get("display_name") or "instance"),
            include_ipv6=bool(payload.get("assign_ipv6_ip")),
            launch_token=token,
            # 缺省 True,和上游 build_launch_request(:692)的 `body.get(..., True)` 一致。
            # 用 False 兜底会让**老抢机任务里存下的、还没有这个键的 payload**
            # 被静默收窄成只放 SSH —— 用户的 web 服务某天重试成功后连不上,
            # 而没有任何地方说过规则变了。
            open_all=bool(payload.get("open_guest_firewall", True)),
        )
        if not nsg.ok:
            raise OCIClientError(nsg.message or "创建实例 NSG 失败")
        nsg_id = str((nsg.data or {}).get("nsg_id") or "")
        payload["managed_nsg_id"] = nsg_id
        payload["nsg_ids"] = [nsg_id] if nsg_id else []
        payload["launch_token"] = token

    # Re-sanitize so nsg_ids/vcn fields stay within SAFE_LAUNCH_FIELDS
    from app.oci_client import sanitize_launch_payload

    try:
        # for_retry mirrors the caller's actual mode; deriving it from auth_mode
        # meant a plain key-mode launch was validated under retry-only rules.
        payload = sanitize_launch_payload(payload, for_retry=for_retry)
    except ValueError:
        # keep enriched fields even if retry sanitize is strict
        pass
    return payload


def post_launch_adjustments(
    session: TenantSession,
    *,
    instance_id: str,
    compartment_id: str,
    boot_vpu: int,
) -> list[str]:
    """Apply Always-Free boot VPU after launch (best-effort). Returns log messages.

    WARNING: resize_boot_volume may wait a long time for hydration. Prefer
    schedule_post_launch_adjustments() from HTTP handlers so the API can return
    immediately after LaunchInstance succeeds.
    """
    notes: list[str] = []
    if not instance_id:
        return notes
    vpu = int(boot_vpu or 10)
    if vpu != 10:
        try:
            # Cap wait so a stuck volume cannot block a worker forever.
            result = session.resize_boot_volume(
                instance_id,
                compartment_id,
                vpus_per_gb=vpu,
                wait_for_volume=True,
                timeout=180,
                hydration_timeout=600,
            )
            notes.append(result.message or f"引导卷性能调整：{vpu}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"引导卷性能调整失败：{exc}")
    return notes


def schedule_post_launch_adjustments(
    session: TenantSession,
    *,
    instance_id: str,
    compartment_id: str,
    boot_vpu: int,
) -> None:
    """Fire-and-forget VPU adjust so the HTTP launch response is not blocked."""
    import logging
    import threading

    log = logging.getLogger("ocibot.launch")
    vpu = int(boot_vpu or 10)
    if not instance_id or vpu == 10:
        return

    def _run() -> None:
        try:
            notes = post_launch_adjustments(
                session,
                instance_id=instance_id,
                compartment_id=compartment_id,
                boot_vpu=vpu,
            )
            for note in notes:
                log.info("post-launch %s: %s", instance_id, note)
        except Exception:  # noqa: BLE001
            log.exception("post-launch adjustment failed for %s", instance_id)

    threading.Thread(target=_run, name=f"boot-vpu-{instance_id[-8:]}", daemon=True).start()
