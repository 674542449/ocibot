"""容量雷达：创建实例之前先问 Oracle「这个可用域现在还有没有货」。

用的是官方的 CreateComputeCapacityReport(POST /20160918/computeCapacityReports)。
它**只读**:响应模型连 id 和 lifecycle_state 都没有,ComputeClient 上也只有 create、
没有 get/list/delete —— 没有东西可枚举,也没有东西要清理,更不产生任何计费资源。

但它**不是免费的**。这是一次普通的 Core API POST,和 LaunchInstance 走同一个
per-tenancy 请求速率桶。CLAUDE.md 里那条「请求预算要留给抢机重试循环」对它同样成立,
所以这个模块的每一处设计都在压请求数:进程内缓存、按 AD 并行但每 AD 只发一次、
SDK 重试关掉、路由层限流。

## 为什么它只出结论、不做硬门

容量报告是一个**瞬时快照**,和随后那次 LaunchInstance 之间必然隔着一整个 HTTP 往返,
而 A1 免费容量的窗口以秒计。所以:
  * AVAILABLE 不保证抢得到;
  * OUT_OF_HOST_CAPACITY 也**不保证**抢不到 —— oracle/oci-cli issue #748 记录了一个
    A1.Flex 上结论完全倒置的案例(报告说 AD-3 有货实际开不出来、说 AD-2 无货反而
    开得出来),该 issue 至今未关闭。

再加上 CreateComputeCapacityReport 需要一条和 LaunchInstance **完全不相交**的 IAM
授权(manage compute-capacity-reports),「能创建实例但调不了报告」是常见配置而不是
边缘情况。

把这三件事叠起来,如果让探测失败或「报告说无货」直接阻断创建,就是把 0.4.84/0.4.85
那个故事换一个权限重演一遍 —— 那次是预检比服务端严格,导致付费租户和缺
inspect-compartments 权限的租户从 UI 上**永久**无法创建实例。所以本模块:

  * 从不 raise(照 quota_guard 那边返回 read_incomplete 而不是抛异常的写法);
  * 探测失败一律降级成 status="unknown" + 一句人话,绝不冒充「无货」;
  * 拦不拦由用户在确认框里决定,后端不代劳。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from app.oci_client import (
    RADAR_AVAILABLE,
    RADAR_NOT_SUPPORTED,
    RADAR_OUT_OF_CAPACITY,
    RADAR_UNKNOWN,
)

log = logging.getLogger("ocibot.radar")

# 只支持这一个机型:Always Free 的 ARM 机型,也是唯一一个「抢不到」是常态的。
RADAR_SHAPE = "VM.Standard.A1.Flex"

# 一个 region 最多 3 个 AD,所以 3 个线程就够;每个 AD 恰好一个 HTTP 请求。
_PROBE_WORKERS = 3

# 进程内缓存。60 秒:容量在分钟尺度上变化,而这层缓存的作用是挡住「连点」和
# 「探完看一眼又点一次」,不是让结论过夜。
#
# 刻意**不做** launch_service 那套 app_meta 跨进程二级缓存:那套机制是为
# 「6 个分页读 + 可能建 VCN、几十秒」的 launch-meta 设计的,搬到一个几百毫秒的
# 单发 POST 上不划算,还要背上 _row_key 组键纪律(app_meta.key 是 VARCHAR(64),
# 裸键会在 PostgreSQL 上静默写失败 —— 这个坑本仓踩过两次)。
_CACHE_TTL_SEC = 60.0
_CACHE: dict[tuple, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 512

# 「最优」到「最差」。汇总时取最优的那个。
_RANK = {
    RADAR_AVAILABLE: 3,
    RADAR_OUT_OF_CAPACITY: 2,
    RADAR_NOT_SUPPORTED: 1,
    RADAR_UNKNOWN: 0,
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_key(
    session: Any, tenant_id: str, ad: str, shape: str, configs: list[tuple[float, float]]
) -> tuple:
    """**本功能唯一一处真正的多租户隔离点。**

    容量报告是 Oracle 针对**调用方凭据**给出的答案。键里漏掉 tenant / region /
    compartment 中的任何一个,A 用户的库存结论就会渲染到 B 的面板上。构成照抄
    launch_service.meta_cache_key(tenant_id | region | compartment or tenancy),
    再拼上 AD 和规格 —— 规格必须在键里,因为报告本来就是按 shape config 出的。
    """
    tenant = getattr(session, "tenant", None)
    region = str(getattr(tenant, "region", "") or "")
    comp = str(getattr(tenant, "compartment_ocid", "") or "") or str(
        getattr(tenant, "tenancy_ocid", "") or ""
    )
    return (tenant_id, region, comp, ad, shape, tuple(configs))


def _cache_get(key: tuple) -> Optional[tuple[dict[str, Any], float]]:
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if not hit:
            return None
        stamped, payload = hit
        age = now - stamped
        if age > _CACHE_TTL_SEC:
            _CACHE.pop(key, None)
            return None
        return payload, age


def _cache_put(key: tuple, payload: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            # 无界字典是本仓已经修过的一类 bug(见 rate_limit 的 _evict_expired)。
            # 这里键里含 tenant + 规格,一个用户拖几下滑块就能造出很多键。
            now = time.monotonic()
            for k in [k for k, (t, _) in _CACHE.items() if now - t > _CACHE_TTL_SEC]:
                _CACHE.pop(k, None)
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.clear()
        _CACHE[key] = (time.monotonic(), payload)


def clear_radar_cache() -> None:
    """给测试用,以及租户凭据轮换后调用方主动作废。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def radar_error_hint(message: str, status: Optional[int]) -> str:
    """把一次失败翻译成操作员能照着做的一句话。

    **刻意不走 _format_service_error 的通用 404 分支。** 那条分支会让人去检查
    API Key / Fingerprint / Tenancy OCID 并点「测试连接」—— 而 test_connection 的
    三个探针(list_instances / list_compartments / list_availability_domains)在缺
    manage compute-capacity-reports 时会**全部通过**。把人指向一个刚刚验证过没问题的
    东西,正是那段注释当初为了消除而写的体验。
    """
    if status in (401, 403, 404):
        # 措辞不能把话说死。Oracle 的 NotAuthorizedOrNotFound 是**故意做成歧义**的:
        # 「没权限」和「这东西不存在」返回同一个 404。所以这里只能给出最可能的原因
        # 和排查顺序,不能断言「你没有权限」—— 断错了会让人去改一个本来没问题的策略。
        return (
            "没能读到容量报告,雷达跳过本次探测。**这不代表没有容量**,创建实例不受影响。\n"
            "最可能的原因是缺一条 IAM 授权。CreateComputeCapacityReport 需要一条"
            "**独立于创建实例**的授权,所以「测试连接」通过并不能说明它可用:\n"
            "    Allow group <你的用户组> to manage compute-capacity-reports in tenancy\n"
            "如果你用的是注册账号本人(租户管理员)的 API Key,Administrators 组默认的"
            "「manage all-resources in tenancy」已经包含它,那就不是权限问题 —— "
            "更可能是该区域暂不提供这个接口。"
        )
    if status == 429:
        return "Oracle 正在限流(429),雷达跳过本次探测。稍后再试,或直接尝试创建。"
    if status is not None and 500 <= int(status) < 600:
        return f"Oracle 服务端错误({status}),雷达跳过本次探测。"
    return f"容量探测失败:{message}"


def _rollup(statuses: list[str]) -> str:
    """一组状态取最优。空列表 → unknown(没有结论,不是「没货」)。"""
    if not statuses:
        return RADAR_UNKNOWN
    return max(statuses, key=lambda s: _RANK.get(s, 0))


def _probe_one_ad(
    session: Any,
    tenant_id: str,
    ad: str,
    shape: str,
    configs: list[tuple[float, float]],
) -> dict[str, Any]:
    """探一个 AD。返回的 dict 直接就是响应里 results 的一项。

    从不抛异常:某个 AD 失败不该把另外两个已经拿到的结果一起作废。
    """
    key = _cache_key(session, tenant_id, ad, shape, configs)
    cached = _cache_get(key)
    if cached is not None:
        payload, age = cached
        out = dict(payload)
        out["cached"] = True
        out["cache_age_sec"] = round(age, 1)
        return out

    try:
        result = session.get_capacity_report(ad, shape, configs)
    except Exception as exc:  # noqa: BLE001
        log.warning("capacity radar %s failed: %s", ad, exc)
        return {
            "availability_domain": ad,
            "status": RADAR_UNKNOWN,
            "reason": radar_error_hint(str(exc), None),
            "configs": [],
            "cached": False,
            "cache_age_sec": 0.0,
        }

    if not getattr(result, "ok", False):
        data = getattr(result, "data", None) or {}
        return {
            "availability_domain": ad,
            "status": RADAR_UNKNOWN,
            "reason": radar_error_hint(
                str(getattr(result, "message", "") or ""), data.get("status")
            ),
            "configs": [],
            "cached": False,
            "cache_age_sec": 0.0,
        }

    data = getattr(result, "data", None) or {}
    rows = list(data.get("rows") or [])

    configs_out: list[dict[str, Any]] = []
    for idx, (ocpus, memory) in enumerate(configs):
        mine = [r for r in rows if _same_config(r, ocpus, memory)]
        # Oracle 可能按 FD 逐行返回,也可能只给一行 fault_domain=None 的 AD 级汇总。
        # 两种形状都要能处理 —— 有 FD 的行才进 fault_domains 列表。
        fds = [
            {
                "fault_domain": r["fault_domain"],
                "status": r["status"],
                "available_count": r["available_count"],
            }
            for r in mine
            if r.get("fault_domain")
        ]
        counts = [r["available_count"] for r in mine if r.get("available_count") is not None]
        configs_out.append(
            {
                "ocpus": float(ocpus),
                "memory_in_gbs": float(memory),
                # 第一项是用户表单里那一组;AD 的结论取自它,不是取所有配置里最好的。
                "primary": idx == 0,
                "status": _rollup([r["status"] for r in mine]),
                # 普通租户恒为 None。前端据此渲染「有货(Oracle 未给出数量)」,
                # 而不是「可开 0 台」。
                "available_count": max(counts) if counts else None,
                "fault_domains": fds,
            }
        )

    # AD 的状态 = **主配置**的状态,不是所有配置取最优。
    # 否则「4C24G 无货、1C6G 有货」会渲染成一个绿色的 AD,而用户要的是 4C24G。
    primary = next((c for c in configs_out if c["primary"]), None)
    ad_status = primary["status"] if primary else _rollup([c["status"] for c in configs_out])

    payload = {
        "availability_domain": ad,
        "status": ad_status,
        "reason": "",
        "configs": configs_out,
    }
    _cache_put(key, payload)
    out = dict(payload)
    out["cached"] = False
    out["cache_age_sec"] = 0.0
    return out


def _same_config(row: dict[str, Any], ocpus: float, memory: float) -> bool:
    """把一行响应对回它属于哪一组请求规格。

    Oracle 会把请求里的 instance_shape_config 原样回显,但浮点数经过一轮 JSON
    往返后不保证逐位相等,所以用容差比较而不是 ==。回显缺失(两个字段都是 None)时
    认为它属于**唯一**那组规格 —— 只请求了一组时这是对的;请求了多组却拿不到回显,
    那就无法归属,宁可让它落在第一组也不要凭空丢掉一行。
    """
    r_cpu, r_mem = row.get("ocpus"), row.get("memory_in_gbs")
    if r_cpu is None and r_mem is None:
        return True
    return abs((r_cpu or 0.0) - ocpus) < 1e-6 and abs((r_mem or 0.0) - memory) < 1e-6


def probe_capacity(
    session: Any,
    *,
    tenant_id: str,
    shape: str,
    configs: list[tuple[float, float]],
    availability_domains: list[str],
) -> dict[str, Any]:
    """探测 shape 在给定各 AD 上的容量。**从不抛异常。**

    按 AD 并行(每个 AD 恰好一个 HTTP 请求),某个 AD 失败不影响其它 AD 的结果。
    """
    ads = [a for a in availability_domains if a]
    if not ads:
        return {
            "ok": False,
            "shape": shape,
            "checked_at": _utcnow_iso(),
            "overall": RADAR_UNKNOWN,
            "results": [],
            "message": "没有可探测的可用域,请先在创建页点「加载配置」。",
        }

    if len(ads) == 1:
        results = [_probe_one_ad(session, tenant_id, ads[0], shape, configs)]
    else:
        with ThreadPoolExecutor(max_workers=min(_PROBE_WORKERS, len(ads))) as pool:
            results = list(
                pool.map(
                    lambda ad: _probe_one_ad(session, tenant_id, ad, shape, configs),
                    ads,
                )
            )

    # 排序按传入的 AD 顺序,而不是完成顺序 —— 否则同一次探测每刷新一遍卡片就换个位置。
    order = {ad: i for i, ad in enumerate(ads)}
    results.sort(key=lambda r: order.get(r["availability_domain"], 999))

    return {
        "ok": True,
        "shape": shape,
        "checked_at": _utcnow_iso(),
        "overall": _rollup([r["status"] for r in results]),
        "results": results,
        "message": "",
    }
