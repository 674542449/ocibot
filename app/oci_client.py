"""OCI Compute client wrapper for multi-tenant instance operations."""

from __future__ import annotations

import base64
import ipaddress
import logging
import re
import secrets
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# 每一次 OCI 失败都在这里留一条服务端记录。
#
# 在此之前 app/oci_client.py **一行日志都没有** —— 120 多个 SDK 调用点,任何一次
# 失败只要没冒泡到界面就彻底消失。用户报「刷新实例详情 404」时,那条错误唯一存在
# 的地方是他浏览器里的红框,他只能手动复制粘贴给我;运维事后想查「昨晚 3 点是不是
# 也炸过」——查不到。opc-request-id 是找 Oracle 开工单**唯一**认的东西,而它只存在
# 于那一次响应里。
#
# 只记 _err_facts() 那组事实(状态码/错误码/操作名/端点主机名/request-id),不记
# OCID、不记密钥、不记签名头 —— 理由见 _endpoint_host 的注释。
_OCI_LOG = logging.getLogger("ocibot.oci")

# Instance freeform-tag key used to remember the root password set at launch
# (password mode only). Visible to anyone who can read the instance.
ROOT_PASSWORD_TAG = "ocibot_root_password"

# Instance freeform-tag key marking an instance as protected from termination.
# A tag rather than a panel-local column so the flag survives a panel reinstall
# or a database restore, and so it is visible in the Oracle console instead of
# being a fact only this panel knows.
TERMINATE_PROTECT_TAG = "ocibot_protected"


def _format_probe_report(
    user_name: str,
    display_name: str,
    region: str,
    compartment: str,
    results: list[dict[str, Any]],
) -> str:
    """把探针结果渲染成「事实 + 排序候选」,**不下结论**。

    这一段以前是一句断言(「这是 IAM 策略范围的问题,不是密钥的问题」)。Oracle 用
    同一个 404 `NotAuthorizedOrNotFound` 同时表示「没有权限」「资源不存在」和
    「不在这个区域」,面板没有任何办法替它区分 —— 硬要下结论,就一定会有一部分
    用户被指向一个本来没问题的地方。这里改成把观测到的事实原样摆出来,再按本次
    证据给出排查**顺序**。
    """
    lines: list[str] = []
    for r in results:
        if r["ok"]:
            lines.append(f"  ✓ {r['label']}（{r['service']}，{r['elapsed_ms']}ms）")
            continue
        head = f"  ✗ {r['label']}（{r['service']}"
        if r.get("operation"):
            head += f" · {r['operation']}"
        head += "）"
        tail = f"[{r.get('status') or r.get('type')}] {r.get('code') or ''}".strip()
        bits = [f"{head}：{tail}", f"耗时 {r['elapsed_ms']}ms"]
        if r.get("host"):
            bits.append(f"端点 {r['host']}")
        if r.get("request_id"):
            bits.append(f"opc-request-id: {r['request_id']}")
        if r.get("retried_ok") is True:
            bits.append("★ 1.5 秒后重读同一个接口 —— 成功了")
        elif r.get("retried_ok") is False:
            bits.append("1.5 秒后重读同一个接口 —— 仍然失败")
        lines.append("\n      ".join(bits))

    by_label = {r["label"]: r for r in results}
    blocking = [r for r in results if r["required"] and not r["ok"]]
    head = (
        f"凭据有效（用户 {user_name}，签名校验已通过）。"
        f"区域 {region or '(未配置)'}，"
        f"Compartment …{compartment[-16:] if compartment else '(未配置，按 Tenancy 根处理)'}"
    )
    if not blocking:
        note = ""
        if any(not r["ok"] for r in results):
            note = "\n\n注：上面有非必需的探针没通过，不影响列实例等主要功能，但值得留意。"
        return f"连接成功：{display_name}\n{head}\n\n" + "\n".join(lines) + note

    # 按本次实际观测到的形态排候选,而不是给一句放之四海的建议。
    #
    # 全部用纯文本,**不要** markdown 星号 —— 这段会直接进面板的错误条,
    # 星号会原样显示出来(同 _format_service_error 里那条既有的注释)。
    transient = any(r.get("retried_ok") is True for r in results)
    hints: list[str] = []
    if transient:
        hints.append(
            "上面标了「重读成功」—— 那次 404 是瞬时的，与 IAM 策略无关。"
            "隔几分钟再试即可；如果反复出现，把 opc-request-id 提给 Oracle 支持。"
        )
    shapes, instances = by_label.get("列出规格"), by_label.get("列出实例")
    # 重读成功时不再提策略：既然同一个调用一秒半后就通了，策略解释已经出局，
    # 再列出来只会让人去改一条本来没问题的策略 —— 那正是这次要修掉的毛病。
    if not transient and shapes and instances and shapes["ok"] and not instances["ok"]:
        hints.append(
            "「列出规格」通过而「列出实例」失败 —— 两个调用走同一个服务、同一个 "
            "Compartment，差别只在 verb。ListInstances 需要的是 read，不是 inspect："
            "\n       Allow group <你的用户组> to read instance-family in compartment <名称>"
            "\n     只写 inspect instance-family 是很常见的一个坑。"
        )
    compute_fail = [r for r in results if r["service"] == "Compute" and not r["ok"]]
    identity_ok = [r for r in results if r["service"] == "Identity" and r["ok"]]
    if not transient and len(compute_fail) == 2 and len(identity_ok) == 2:
        hints.append(
            "两个 Compute 探针都失败、两个 Identity 探针都通过 —— 差异落在服务端点上"
            "（Compute 走 iaas.*，Identity 走 identity.*），不是落在 Compartment 上。"
            "常见于副区刚订阅不久、或该区域的授权数据尚未就绪。"
            "先确认这一行是不是副区、订阅多久了。"
        )
    if any(int(r.get("status") or 0) == 429 for r in results):
        hints.append("出现 [429] —— 那是 Oracle 限流，不是权限问题。隔几分钟再点。")
    if any(str(r.get("type") or "").startswith("CircuitBreaker") for r in results):
        hints.append(
            "出现熔断器错误 —— 这是 SDK 客户端侧的保护（连续失败后暂停发请求，约 30 秒自愈），"
            "与权限无关。"
        )
    hints.append(
        "本次只探了这一个 Compartment，没有向下展开子 Compartment。"
        "如果资源其实在子 Compartment 里，把租户配置里的 Compartment OCID 改成它所在的那个。"
    )

    return (
        f"{head}\n\n" + "\n".join(lines) + "\n\n"
        "Oracle 用同一个 404「NotAuthorizedOrNotFound」同时表示「没有权限」「资源不存在」"
        "和「不在这个区域」，面板无法替它区分。按本次观测到的证据，建议按这个顺序排查：\n"
        + "\n".join(f"  {i}) {h}" for i, h in enumerate(hints, 1))
    )


_ENDPOINT_HOST_RE = re.compile(r"https?://([^/\s?]+)")


def _public_ip_busy(ip: Any) -> bool:
    """这个公网 IP 是不是「已绑定 或 正在绑定」。

    **不能只看 private_ip_id。** PublicIp 的文档写着这个字段在绑定进行中时也是
    null（而且它本身已被标记 deprecated）。于是一个处于 ASSIGNING 的保留 IP 会被
    读成「未绑定」：界面给出「删除」按钮、服务端守卫也放行，delete_public_ip 就打在
    一个正在绑定的 IP 上。按 lifecycle_state 判断才是可靠的。
    """
    state = str(getattr(ip, "lifecycle_state", "") or "").upper()
    if state in {"ASSIGNING", "ASSIGNED"}:
        return True
    return bool(getattr(ip, "assigned_entity_id", None) or getattr(ip, "private_ip_id", None))


def _vpus_or_default(raw: Any, default: int = 10) -> int:
    """读 vpus_per_gb。**0 是合法值**（Lower Cost 档），不能被 `or` 吃掉。

    原来四处都写成 `int(getattr(v, "vpus_per_gb", 10) or 10)` —— Python 里
    `0 or 10` 等于 10，于是一块真正配成 0 VPUs/GB 的卷会被显示成 10 并标成
    「平衡」。这是把一个**更便宜、更慢**的档次显示成标准档，方向和用户的实际
    配置正好相反。只有 None（字段真的没返回）才该退回默认值。
    """
    if raw is None:
        return int(default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


# OCID 长这样：ocid1.<类型>.<realm>.<区域可省>.<一长串>
_OCID_RE = re.compile(r"\bocid1\.([a-z0-9]+)\.[a-z0-9-]*\.[a-z0-9-]*\.?([a-z0-9]{6,})", re.I)


def _scrub_ocids(text: str) -> str:
    """把任意文本里的完整 OCID 截成 `ocid1.<类型>…<末 8 位>`。

    面板到处只显示 `compartment[-16:]` 是有意的（见 _endpoint_host）,但那条纪律
    只覆盖了走 _format_service_error 的路径。裸 `str(exc)` 绕过它 —— 而 SDK 的
    CircuitBreakerError.__str__ 正好是这种形状：

        'Circuit "%s" OPEN until %s (%d failures, %d sec remaining) (last_failure: %r)'

    那个 `%r` 是被熔断的 ServiceError 的 repr,而 ServiceError.__init__ 把
    request_endpoint 整条塞进了 args —— 里面带着**未脱敏的完整 compartment OCID**
    （对 2.182.0 实测确认）。它不是 ServiceError 的子类,所以各处 `except
    ServiceError` 都接不住,最后落到 `except Exception -> str(exc)`,原样进前端。
    """
    if not text:
        return text
    return _OCID_RE.sub(lambda m: f"ocid1.{m.group(1)}…{m.group(2)[-8:]}", text)


def safe_error_text(exc: BaseException) -> str:
    """任何异常 → 可以安全显示给用户的一句话。

    ``str(exc)`` 的最后一道闸门：熔断器异常换成一句说人话的解释，其余一律脱敏。
    """
    if _is_circuit_breaker_error(exc):
        remaining = ""
        try:
            breaker = getattr(exc, "_circuit_breaker", None)
            secs = int(round(float(getattr(breaker, "open_remaining", 0) or 0)))
            if secs > 0:
                remaining = f"约 {secs} 秒后自动恢复。"
        except Exception:  # noqa: BLE001
            remaining = ""
        return (
            "OCI SDK 客户端熔断器已打开：此前连续多次调用失败，SDK 暂停了对该服务的请求。"
            f"{remaining or '约 30 秒后自动恢复。'}"
            "这与 API Key、IAM 策略、Compartment 配置都无关，稍后重试即可。"
        )
    return _scrub_ocids(str(exc))


def _endpoint_host(exc: Any) -> str:
    """从 ServiceError 里只取**主机名**,绝不取原文。

    `ServiceError.request_endpoint` 的实际内容长这样:

        GET https://iaas.us-phoenix-1.oraclecloud.com/20160918/instances
            ?compartmentId=ocid1.compartment.oc1..aaaa<完整 OCID>&limit=1

    SDK 自己的 `redact_sensitive_string_for_logs` 只脱敏凭据头,**不脱敏
    compartmentId**(对 2.182.0 实测过,OCID 原样输出)。原样打印等于把完整
    compartment OCID 送进前端和日志 —— 而面板到处只显示 `compartment[-16:]`
    正是为了防这件事。

    只留 host 反而保住了诊断价值最高的那一段:`iaas.*` 是 Core 服务,
    `identity.*` 是 Identity 服务。本次故障里「哪些通过、哪些失败」正好
    沿着这条服务边界切开。
    """
    m = _ENDPOINT_HOST_RE.search(str(getattr(exc, "request_endpoint", "") or ""))
    return m.group(1) if m else ""


def _err_facts(exc: BaseException) -> dict[str, Any]:
    """把一次失败压成一组**可以安全外发**的事实。绝不含 OCID / 密钥 / 签名头。

    这些字段本来就在 ServiceError 上,以前全被丢掉了 —— 诊断代码只留了
    `code or status`,于是「限流」「瞬时故障」「真的没权限」三种完全不同的情况
    在面板上长得一模一样,只能靠一句写死的断言去填空。opc-request-id 更是找
    Oracle 支持时**唯一**有用的东西。

    注意不要改用 `oci.base_client.is_http_log_enabled` 去补这些信息:那个函数
    整个函数体就是 `HTTPConnection.debuglevel = 1`,会把 OCI 请求签名的
    Authorization 头打到 stdout。凭据永不落日志是本仓的常设约束。
    """
    return {
        "type": type(exc).__name__,
        "status": int(getattr(exc, "status", 0) or 0),
        "code": str(getattr(exc, "code", "") or ""),
        # opc-request-id
        "request_id": str(getattr(exc, "request_id", "") or ""),
        "operation": str(getattr(exc, "operation_name", "") or ""),
        "host": _endpoint_host(exc),
    }


# 模糊 404 的复读间隔（秒）。两次,不是八次。
#
# Oracle 的 API 错误表把 404 NotAuthorizedOrNotFound 的 Retry 列明确标成 "No."，
# 所以这里刻意**不**给 SDK 挂 404 重试策略 —— 那会让每一个真的没权限的调用都白白
# 慢上八倍。但「按 OCID 直读一台实例」是幂等 GET,在注定要弹红框的那一刻多读一两次
# 的性质完全不同:**复读成功,这个 404 按定义就是瞬时的**,「权限问题」这个解释当场
# 出局;复读仍然失败,那就是持续问题,而且这一位证据本身就值得说给用户听。
#
# 一次(0.4.96 的做法)对用户报的现象不够:他描述的是「多点几次就好了」,手动点三五次
# 跨度是好几秒。1.2 + 3.0 覆盖约 4.2 秒的窗口,而且**只在失败路径上**付这个代价 ——
# 严格少于他现在手动点三次刷新的请求量。
_REREAD_DELAYS: tuple[float, ...] = (1.2, 3.0)


def _is_ambiguous_404(exc: BaseException) -> bool:
    """这次失败是不是 Oracle 那个「没权限 **或** 不存在」的模糊 404。

    同时认结构化字段和文本:上层有些地方拿到的已经是 _format_service_error()
    拼过的字符串包在 OCIClientError 里,没有 status/code 属性了。
    """
    if int(getattr(exc, "status", 0) or 0) == 404:
        return True
    blob = f"{getattr(exc, 'code', '') or ''} {exc}".lower()
    return "notauthorizedornotfound" in blob or "[404]" in blob


def read_with_404_evidence(call: Callable[[], Any]) -> tuple[bool, Any, Optional[BaseException], int]:
    """跑 ``call()``；**只在模糊 404 上**复读，最多 len(_REREAD_DELAYS) 次。

    返回 ``(成功?, 值, 最后一次异常, 实际复读次数)``。

    第一次就成功时复读次数为 0，一个额外请求都不发 —— 成功路径零增量。
    任何**非** 404 的失败立刻返回,不复读:限流(429)有 SDK 自己的退避,
    5xx 同理,401 是真的凭据问题,重读只是白等。
    """
    last: Optional[BaseException] = None
    for attempt in range(len(_REREAD_DELAYS) + 1):
        if attempt:
            time.sleep(_REREAD_DELAYS[attempt - 1])
        try:
            return True, call(), None, attempt
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not _is_ambiguous_404(exc):
                return False, None, exc, attempt
    return False, None, last, len(_REREAD_DELAYS)


def transient_404_note(first_error: str, rereads: int) -> str:
    """首读失败、复读成功时说给用户听的那句话。

    必须说出来,不能让它悄悄成功:用户当前的困惑正是「我权限是满的,为什么有时候
    报错有时候不报」。默默修好等于让这个困惑一直挂着。
    """
    return (
        f"首次读取失败、{rereads} 次重读后成功 —— 本次是 Oracle 侧的瞬时故障，"
        f"与你的 IAM 策略、密钥、Compartment 配置都无关，无需改动任何配置。"
        f"\n（首次错误：{first_error[:300]}）"
    )


def persistent_404_note(rereads: int) -> str:
    """复读也没救回来时补的那句 —— 重试**不能**变成一次沉默的加时。

    只陈述做过什么，**不下**「所以这是持续故障」的结论。Oracle 的授权最终一致性
    窗口可以长达几分钟（Go SDK 把这个窗口设成 240 秒），而这里总共只等了几秒 ——
    据此断言「这不是瞬时故障」就是又一次替 Oracle 下它没给我们依据的结论,
    正是这一整轮要修掉的毛病。想等满 240 秒也不行:那要把一个 HTTP 请求挂四分钟。
    """
    if not rereads:
        return ""
    window = " + ".join(f"{d:g}s" for d in _REREAD_DELAYS[:rereads])
    return (
        f"（已自动重读 {rereads} 次、间隔 {window}，仍然失败。"
        f"注意这不足以排除瞬时故障：Oracle 的授权最终一致性窗口可达数分钟，"
        f"而这里只等了约 {sum(_REREAD_DELAYS[:rereads]):g} 秒。"
        f"若隔几分钟重试即恢复，那就是 Oracle 侧的问题，不必改动任何配置。）"
    )

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

    **每次新建一个**,不再返回 `oci.retry.DEFAULT_RETRY_STRATEGY` 那个模块级单例。
    SDK 在每次调用前会往策略对象上写 `add_circuit_breaker_callback(...)`(实现就是
    `self.circuit_breaker_callback = callback`)——返回单例等于让所有租户、所有
    client、所有线程往同一个可变对象上写。当前写入值恒为 None,所以还观察不到症状,
    但这正是本仓一直在清的那类跨租户共享可变状态。

    参数照抄 DEFAULT_RETRY_STRATEGY 的实际取值(8 次 / 600 秒 / 429 + 任意 5xx),
    行为不变。
    """
    if not OCI_AVAILABLE:
        return None
    builder = oci.retry.RetryStrategyBuilder(
        max_attempts_check=True,
        max_attempts=8,
        total_elapsed_time_check=True,
        total_elapsed_time_seconds=600,
        retry_max_wait_between_calls_seconds=30,
        retry_base_sleep_time_seconds=1,
        service_error_check=True,
        # 传一份**全新的 dict**。RetryStrategyBuilder 的 add_service_error_check
        # 重载会就地改写模块级全局 RETRYABLE_STATUSES_AND_CODES，那是 DEFAULT
        # 策略内部持有的同一个对象（见 tests/test_instance_list_404.py 的那条测试）。
        service_error_retry_config={429: [], 409: ["IncorrectState"]},
        service_error_retry_on_any_5xx=True,
        backoff_type=oci.retry.BACKOFF_FULL_JITTER_EQUAL_ON_THROTTLE_VALUE,
    )
    strategy = builder.get_retry_strategy()
    _disarm_circuit_breaker_retry(strategy)
    return strategy


def _disarm_circuit_breaker_retry(strategy: Any) -> None:
    """让熔断器异常**立刻**失败，而不是被当成「可重试」再退避八次。

    SDK 自己的 TimeoutConnectionAndServiceErrorRetryChecker.should_retry 里有这么
    一条：

        elif isinstance(exception, CircuitBreakerError):
            threading.Thread(target=kwargs['circuit_breaker_callback'], ...).start()
            return True

    也就是说熔断器一打开，调用方线程并不会马上拿到错误 —— 它会按 1.4/2.9/4.1/8.3/
    16.9/30/30 秒退避重试**八次**，而熔断器的 recovery_timeout 是 30 秒。于是一次
    「本该立刻失败」的调用把一个线程占住三十几秒到一分钟。FastAPI 的同步路由跑在
    anyio 那个默认 40 个线程的池里,一个租户的 429 风暴打开共享熔断器之后,这些调用
    会把线程池坐满,面板整体像卡死 —— 而熔断本来的意义恰恰是**快速失败**。
    """
    try:
        checkers = getattr(getattr(strategy, "checkers", None), "checkers", None) or []
        for checker in checkers:
            inner = getattr(checker, "should_retry", None)
            if inner is None:
                continue

            def _wrap(original: Callable) -> Callable:
                # 签名照抄 SDK 的 should_retry(self, exception=None, response=None,
                # **kwargs) —— `response` 必须原样透传，吞掉它会让基于响应体的重试
                # 判断失效（SDK 目前是全关键字调用，但不要指望这一点不变）。
                def _should_retry(
                    exception: Any = None, response: Any = None, **kwargs: Any
                ) -> bool:
                    if _is_circuit_breaker_error(exception):
                        return False
                    return bool(original(exception=exception, response=response, **kwargs))

                return _should_retry

            checker.should_retry = _wrap(inner)
    except Exception:  # noqa: BLE001
        # 装不上就算了 —— 退回 SDK 原行为，绝不能因为这个优化让 client 建不出来。
        pass


def cb_kwargs(service: str, tenant_id: str) -> dict:
    """每租户 × 每服务一个熔断器的 client kwargs（理由见 TenantSession._build）。

    是模块级函数而不是方法：它只需要一个租户 id，不需要整个会话。`_build` 之外还有
    四处按需重建 client 的地方（账单走主区的 Identity / InvoiceService / UsageApi /
    Subscription / IdentityDomains），它们同样不能落回那个全进程共用的 DEFAULT 熔断器。
    """
    kw: dict = {"retry_strategy": sdk_default_retry_strategy()}
    if not OCI_AVAILABLE:
        return kw
    try:
        kw["circuit_breaker_strategy"] = oci.circuit_breaker.CircuitBreakerStrategy(
            failure_threshold=10,
            recovery_timeout=30,
            name=f"ocibot-{service}-{tenant_id}",
        )
    except Exception:  # noqa: BLE001
        pass
    return kw


def _is_circuit_breaker_error(exc: Any) -> bool:
    """CircuitBreakerError **不是** ServiceError 的子类，所以到处都接不住它。"""
    try:
        from circuitbreaker import CircuitBreakerError

        return isinstance(exc, CircuitBreakerError)
    except Exception:  # noqa: BLE001
        return type(exc).__name__ == "CircuitBreakerError"


def sdk_bounded_paged_retry_strategy() -> Any:
    """给 `oci.pagination.list_call_get_all_results` 用的**收敛**重试策略。

    分页助手自己**硬编码**又套了一层 DEFAULT_RETRY_STRATEGY，和 client 层那个相乘：
    最坏 8 x 8 = 64 次真实调用、~600 秒卡在一个 HTTP 请求里 —— 正在跟抢机重试循环
    抢同一个 per-tenancy 限流额度（CLAUDE.md）。收敛到 3 次 / 20 秒。

    两条不能改的：
      * `service_error_retry_config` 是**替换**语义，不合并 —— 409/429 必须自己
        抄回来，漏写就把它们的重试一起丢掉；
      * **不含 404** —— Oracle 的 API 错误表把 404 NotAuthorizedOrNotFound 的
        Retry 列标成 "No."。
    另外绝不能用 `RetryStrategyBuilder.add_service_error_check()` 那个重载：
    它会**原地改写模块级全局** RETRYABLE_STATUSES_AND_CODES，而那正是
    DEFAULT_RETRY_STRATEGY 内部持有的同一个对象，会污染整个进程、所有租户。
    """
    if not OCI_AVAILABLE:
        return None
    strategy = oci.retry.RetryStrategyBuilder(
        max_attempts_check=True,
        max_attempts=3,
        total_elapsed_time_check=True,
        total_elapsed_time_seconds=20,
        retry_base_sleep_time_seconds=1,
        retry_max_wait_between_calls_seconds=4,
        service_error_check=True,
        service_error_retry_on_any_5xx=True,
        service_error_retry_config={
            409: ["IncorrectState", "LockConflict"],
            429: [],
        },
    ).get_retry_strategy()
    # 分页路径同样不能把熔断器异常当成「可重试」再退避（见 _disarm_circuit_breaker_retry）。
    _disarm_circuit_breaker_retry(strategy)
    return strategy


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
# Always Free（不能计费的租户）的上限。升级号的 A1 额度不同 ——
# 权威判断在 app/free_quota.py 的 a1_caps()，那里按账号类型分；这份镜像只作展示用。
ALWAYS_FREE_LIMITS = {
    "a1_ocpu": 2.0,
    "a1_memory_gb": 12.0,
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

# One-click launch presets (shape + boot). Network uses the account default.
#
# **按账号类型生成，不是一张写死的表。** 免费号的 A1 额度是 2 OCPU / 12 GB，
# 升级（PAYG）号是 4 / 24（见 free_quota.a1_caps）—— 一张静态表必然对其中一边是错的：
#   * 写 4C24G：免费号照着点会开出超一倍的配置，然后被 Oracle 收走一台
#     （这就是 0.4.102 修的那个故障）；
#   * 写 2C12G：升级号被无端砍掉一半，而那本来就是他的额度。
#
# VPU 一律 10（平衡档）。同一个文件里 BOOT_VPU_PRESETS 把 >20 标成「可能额外计费」，
# free_quota 也会为 vpu>10 挂告警 —— 一个标着「免费」的预设却默认选一个自己都在
# 警告的档位，是自相矛盾。想要更高性能仍可在向导里手动选。
def launch_quick_presets(account_tier: str = "") -> list[dict]:
    """这个账号该看到的一键预设。"""
    from app.free_quota import a1_caps

    cap_cpu, cap_mem = a1_caps(account_tier)
    cpu = int(cap_cpu)
    mem = int(cap_mem)
    half_cpu = max(1, cpu // 2)
    half_mem = max(1, mem // 2)
    # 免费号那 2/12 是硬上限；升级号的 4/24 是免费额度，超出按量计费，
    # 所以文案上不把升级号那档说成「免费」——「额度内」才是准确的。
    tag = "免费" if cap_cpu <= 2 else "额度内"
    return [
        {
            "id": "e2_micro_50",
            "label": "免费 AMD · 50G",
            "hint": "VM.Standard.E2.1.Micro · 硬盘 50GB",
            "shape": "VM.Standard.E2.1.Micro",
            "arch": "x86",
            "ocpus": None,
            "memory_in_gbs": None,
            "boot_volume_size_in_gbs": 50,
            "boot_volume_vpus_per_gb": 10,
        },
        {
            "id": f"a1_{cpu}c{mem}g_100",
            "label": f"{tag} ARM {cpu}C{mem}G · 100G",
            "hint": f"VM.Standard.A1.Flex · {cpu} OCPU / {mem}GB · 硬盘 100GB（用满额度）",
            "shape": "VM.Standard.A1.Flex",
            "arch": "arm",
            "ocpus": cpu,
            "memory_in_gbs": mem,
            "boot_volume_size_in_gbs": 100,
            "boot_volume_vpus_per_gb": 10,
        },
        {
            "id": f"a1_{cpu}c{mem}g_200",
            "label": f"{tag} ARM {cpu}C{mem}G · 200G",
            "hint": f"VM.Standard.A1.Flex · {cpu} OCPU / {mem}GB · 硬盘 200GB（用满额度）",
            "shape": "VM.Standard.A1.Flex",
            "arch": "arm",
            "ocpus": cpu,
            "memory_in_gbs": mem,
            "boot_volume_size_in_gbs": 200,
            "boot_volume_vpus_per_gb": 10,
        },
        # 想要两台机器就得对半分 —— A1 额度是**合计**的。
        {
            "id": f"a1_{half_cpu}c{half_mem}g_100",
            "label": f"{tag} ARM {half_cpu}C{half_mem}G · 100G（可开两台）",
            "hint": (
                f"VM.Standard.A1.Flex · {half_cpu} OCPU / {half_mem}GB · 硬盘 100GB · "
                "开两台正好用满额度"
            ),
            "shape": "VM.Standard.A1.Flex",
            "arch": "arm",
            "ocpus": half_cpu,
            "memory_in_gbs": half_mem,
            "boot_volume_size_in_gbs": 100,
            "boot_volume_vpus_per_gb": 10,
        },
    ]


# 兼容旧引用：不带 tier 就是免费号那份（最保守的一份）。
LAUNCH_QUICK_PRESETS: list[dict] = launch_quick_presets("")

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
    # 创建路径刻意**不**开放 0（Lower Cost）：前端下拉里也没有这一档，而创建时
    # 选错性能是要重建机器才能改回来的。已存在的卷读回 0 时不再被改写成 10
    # （见 _vpus_or_default），调整路径也接受 0 —— 只有「新建」这一步保持保守。
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
        # 引号包起来，不能裸着写。
        #
        # 上面的正则只锚定了开头（类型 + base64 块），公钥尾部的注释是完全自由的
        # 文本。裸标量 `- ssh-rsa AAAA... foo: bar` 在 YAML 里会被解析成一个映射，
        # `- ssh-rsa AAAA... #x` 里的注释会被截掉 —— 两种情况都会让整份 cloud-config
        # 解析失败或者装错，结果是一台**没有任何 SSH 公钥、连不上去**的机器，
        # 而创建流程一路显示成功。
        if "\n" in key or "\r" in key:
            raise ValueError("SSH 公钥不能包含换行符")
        lines.extend(["    ssh_authorized_keys:", "      - '" + key.replace("'", "''") + "'"])
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
    # 「首读被拒(404)、复读成功」时填这一句，路由渲染成 X-Ocibot-Reread 响应头。
    #
    # 挂在**这里**而不是 TenantSession 上是有意的：TenantSession 是进程级缓存的
    # （web/backend/oci_bridge.py 的 SessionManager），同一个租户的并发请求拿到的
    # 是同一个对象。写在 session 上，A 请求的「本次是瞬时故障」会被 B 请求覆盖，
    # 或者更糟 —— B 把 A 的提示挂在一个根本没出错的页面上。
    # _last_tree_errors 就是这么写的，那是一个已知的旧坑，不要照抄。
    read_note: str = ""
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

    def _cb_kwargs(self, service: str) -> dict:
        """本会话的 client kwargs —— 见模块级 cb_kwargs()。"""
        return cb_kwargs(service, str(getattr(self.tenant, "id", "") or ""))

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

            # 熔断器必须**每租户 × 每服务**一个。
            #
            # BaseClient 是这样取熔断器的：
            #     circuit_breaker = CircuitBreakerMonitor.get(strategy.name)
            # 而 DEFAULT_CIRCUIT_BREAKER_STRATEGY.name 是**模块导入时生成的一个固定
            # uuid**。于是所有兜底用 DEFAULT 的 client，无论属于哪个租户、哪个服务，
            # 按 name 查出来的都是**同一个**熔断器实例。
            #
            # 0.4.96 只给 Identity 换了独立熔断器，依据是一句「IdentityClient 是唯一
            # 会兜底装上 DEFAULT 的 client」—— 那句话是错的。对 2.182.0 逐个读
            # __init__ 源码实测的结果是：
            #     ComputeClient                → GLOBAL（实测值为 None，即没有熔断器）
            #     VirtualNetwork / Blockstorage / ComputeManagement / Identity /
            #     Monitoring / Limits / Quotas / ObjectStorage / Usageapi /
            #     ComputeInstanceAgent          → 全部兜底 DEFAULT
            # 也就是说：除 Compute 之外的九个 client，跨租户**并且跨服务**共用一个
            # 熔断器。任意租户在任意一个服务上攒够 10 次 429/5xx，其余所有租户的
            # 网络、块存储、监控、对象存储调用会一起被熔断 30 秒。
            #
            # 而 CircuitBreakerError **不是** ServiceError 的子类（实测 issubclass
            # 为 False），所以它会穿透各处 `except ServiceError`；在 IP 解析、引导卷
            # 读取这类 `except Exception: pass` 的地方则被静默吞掉，表现为字段莫名其妙
            # 变空。保留熔断保护本身，只去掉这个交叉污染。
            #
            # Compute **不要**加：它本来就没有熔断器，加上等于凭空引入一个新的失败
            # 模式，而实例列表和实例详情正好全压在它身上。
            _cb_kw = self._cb_kwargs

            self._compute = ComputeClient(self._config, **retry_kw)
            self._network = VirtualNetworkClient(self._config, **_cb_kw("network"))
            self._identity = IdentityClient(self._config, **_cb_kw("identity"))
            self._blockstorage = BlockstorageClient(self._config, **_cb_kw("blockstorage"))
            self._limits = LimitsClient(self._config, **_cb_kw("limits"))
            self._monitoring = MonitoringClient(self._config, **_cb_kw("monitoring"))
            try:
                self._object_storage = ObjectStorageClient(self._config, **_cb_kw("objectstorage"))
            except Exception:
                self._object_storage = None
            try:
                self._instance_agent = ComputeInstanceAgentClient(
                    self._config, **_cb_kw("instanceagent")
                )
            except Exception:
                self._instance_agent = None
            try:
                # Usage API is often home-region only; prefer home region when known.
                # 先按租户自己的区域建，**不要**在这里解析主区。
                #
                # _home_region() 会真的发一次 list_region_subscriptions —— 放在
                # _build 里意味着**每建一个 TenantSession 就多打一次 Identity 调用**，
                # 而 SessionManager.get 是在一把进程级锁里构造 session 的，
                # 那次网络调用会把同一个 worker 里所有租户的 OCI 请求一起堵住。
                # 真正需要主区的只有账单接口，那时再按需重建（见 usage property）。
                self._usage = UsageapiClient(dict(self._config), **_cb_kw("usage"))
                self._usage_region_pinned = False
            except Exception:
                self._usage = None
                self._usage_region_pinned = True
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
        """账单/用量客户端。第一次被访问时才解析主区并按需换端点。

        惰性化的理由见 _build：主区解析是一次真实的 Identity 调用，放在构造里
        会让每个 session 的冷建都多一次网络往返，而构造是在进程级锁里做的。
        """
        if self._usage is not None and not getattr(self, "_usage_region_pinned", True):
            self._usage_region_pinned = True
            try:
                home = self.home_region_confirmed()
                if home and home != self.tenant.region.strip():
                    self._usage = UsageapiClient(
                        self._config_for_region(home),
                        **cb_kwargs("usage", str(getattr(self.tenant, "id", "") or "")),
                    )
            except Exception:  # noqa: BLE001
                # 换不成就用租户自己区域的那个 —— 账单接口是租户级的，
                # 打错区域最多是拿不到数据，不该让整个 property 抛。
                pass
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
        """Verify the credentials **and** that they can actually read what the panel needs.

        It used to call `get_user()` only — and `get_user` succeeds with virtually
        any valid key regardless of IAM policy. So a tenant whose policy does not
        cover the configured compartment got 连接成功 here and then
        `NotAuthorizedOrNotFound` on every real page, with the panel advising them
        to check the very key this test had just validated. Circular and unactionable.

        The extra probes are cheap reads against the compartment the panel will
        actually use. Each is reported separately: "which of these can I not read"
        is precisely the fact that turns an opaque 404 into a policy the operator
        can go fix.
        """
        compartment = self.resolve_compartment()
        region = self.tenant.region.strip()
        # GetUser 失败**不等于**凭据不行。
        #
        # 策略参考里 GetUser 需要 USER_INSPECT，而这是一条和面板其余功能完全不相干的
        # 权限 —— 一把只被授予了 compute/网络权限的密钥，其余功能全都正常，却会在
        # 这里早退，整份诊断报告一个探针都没跑就结束了。
        # 只有 401（NotAuthenticated：签名/指纹/私钥对不上）才是真的凭据问题。
        user = None
        user_error = ""
        try:
            user = self.identity.get_user(self.tenant.user_ocid.strip()).data
        except ServiceError as exc:
            status = int(getattr(exc, "status", 0) or 0)
            if status == 401:
                return OperationResult(ok=False, message=_format_service_error(exc))
            user_error = _format_service_error(exc)
        except Exception as exc:  # noqa: BLE001
            user_error = str(exc)

        # 这里以前写着「Credentials are good past this point; anything below is a
        # policy/scope problem」—— 那句话是错的,而且是本次故障的根源。
        #
        # get_user 成功之后,下面的探针仍然可能因为限流(429)、Oracle 服务端错误
        # (5xx)、网络超时、DNS 抖动、熔断器打开而失败,这些全都不是策略问题。
        # 旧代码把它们一律讲成「这是 IAM 策略范围的问题,不是密钥的问题」,并让操作员
        # 去改一条本来没问题的策略。用户实际遇到的正是这个:满权限的账号,时好时坏,
        # 每次都被告知去改 IAM。
        #
        # 探针表也重排过。旧表是 {ListInstances, ListCompartments, ListAvailabilityDomains},
        # 看着像测三样东西,实际上:
        #   * ListCompartments 传的是 access_level="ACCESSIBLE" —— 那是个**过滤器**,
        #     SDK 文档明说它只返回你有 INSPECT 权限的那些,零权限时返回空页 + 200,
        #     结构上**不可能**因为缺权限而失败。这一票是废票。
        #   * 它和 ListAvailabilityDomains 需要的是**同一个**权限 COMPARTMENT_INSPECT。
        # 于是真实的探针集合是 {INSTANCE_READ, COMPARTMENT_INSPECT, 废票},
        # 「2 通过 1 失败」根本推不出「compartment 读不了」。
        #
        # 新表把两条轴分开:同一个服务的不同 verb、以及不同服务的同一件事。
        #   ListInstances -> INSTANCE_READ      ListShapes -> INSTANCE_INSPECT
        #   GetCompartment / ListAvailabilityDomains -> COMPARTMENT_INSPECT
        # 「列出规格通过、列出实例失败」就直接指向「策略只给了 inspect 没给 read」——
        # 这是个很常见的写法错误(ListInstances 要的是 read,不是 inspect)。
        #
        # 探针用**收敛**的重试策略,不用 client 默认那套(8 次 / 600 秒上限)。默认策略
        # 比前端 axios 的超时和常见反代的 100 秒都长,所以一次真被限流的「测试连接」
        # 根本不会返回文案 —— 它会在浏览器里超时,而后端还在继续烧请求预算。收敛之后
        # [429] 才有机会被显示出来,而那正是本次要区分的东西之一。
        probe_kw: dict[str, Any] = {}
        try:
            probe_kw["retry_strategy"] = (
                oci.retry.RetryStrategyBuilder(
                    max_attempts_check=True,
                    max_attempts=2,
                    total_elapsed_time_check=True,
                    total_elapsed_time_seconds=20,
                    retry_max_wait_between_calls_seconds=5,
                    service_error_check=True,
                    # 参数名必须是 service_error_retry_config。
                    #
                    # 0.4.90 这里写的是 `retry_on_service_error_codes=[429]` ——
                    # 那不是 RetryStrategyBuilder 的参数，被 kwargs.get 静默吞掉，
                    # 于是当时那句「只对 429 重试」的注释是假的：实际生效的是默认的
                    # {-1: [], 409: [...], 429: []} 外加 retry_any_5xx=True。
                    # 收敛的部分（2 次 / 20 秒）当时是生效的，所以行为影响不大，
                    # 但照着那个形状抄的人会得到一个和注释不符的策略。
                    # 这个 dict 是**替换**语义，不合并 —— 要保留的项必须自己写全。
                    service_error_retry_config={429: []},
                    service_error_retry_on_any_5xx=True,
                ).get_retry_strategy()
            )
        except Exception:  # noqa: BLE001
            probe_kw = {}

        # 把 GetUser 也列成一个**非必需**探针 —— 它读不到不影响面板任何功能，
        # 但操作员该知道这件事，以及需要的话补 `inspect users in tenancy`。
        # 绝不能设成 required=True：那会把这类租户从「假失败」变成「真失败」。
        probes: list[tuple[str, str, str, bool, Callable[[], Any]]] = [
            ("列出实例", "Compute", "read instance-family", True,
             lambda: self.compute.list_instances(compartment, limit=1, **probe_kw)),
            ("列出规格", "Compute", "inspect instance-family", False,
             lambda: self.compute.list_shapes(compartment, limit=1, **probe_kw)),
            ("读取 Compartment", "Identity", "inspect compartments", False,
             lambda: self.identity.get_compartment(compartment, **probe_kw)),
            ("列出可用域", "Identity", "inspect compartments", False,
             lambda: self.identity.list_availability_domains(compartment, **probe_kw)),
        ]

        results: list[dict[str, Any]] = []
        if user_error:
            results.append(
                {
                    "label": "读取用户", "service": "Identity",
                    "verb": "inspect users", "required": False,
                    "ok": False, "elapsed_ms": 0, "retried_ok": None,
                    "type": "ServiceError", "status": 0, "code": "",
                    "request_id": "", "operation": "get_user", "host": "",
                }
            )
        for label, svc, verb, required, call in probes:
            started = time.monotonic()
            try:
                call()
                results.append(
                    {
                        "label": label, "service": svc, "verb": verb, "required": required,
                        "ok": True, "elapsed_ms": int((time.monotonic() - started) * 1000),
                    }
                )
                continue
            except Exception as exc:  # noqa: BLE001
                facts = _err_facts(exc)
            elapsed_ms = int((time.monotonic() - started) * 1000)

            # 只对 404 复读一次,而且只在这条「用户主动点击的诊断」路径上。
            #
            # 这一位信息的价值极高:复读成功,那个 404 按定义就是**瞬时的**,
            # 「策略问题」这个解释当场出局 —— 这正是用户「有时候好有时候坏」需要的答案。
            #
            # 为什么只对 404:404 不是限流信号,SDK 本身也从不重试它,补一读不可能给
            # 限流火上浇油;而 429/5xx 一律不复读,SDK 刚刚已经替我们退避重试过,
            # 再来一次纯粹是抢抢机重试循环的请求预算(CLAUDE.md 记的 0.4.21 回退
            # 理由就是这条约束)。List/Get 是幂等的,不建资源、不消耗 retry token。
            retried_ok: Optional[bool] = None
            if facts["status"] == 404:
                time.sleep(1.5)
                try:
                    call()
                    retried_ok = True
                except Exception:  # noqa: BLE001
                    retried_ok = False
            results.append(
                {
                    "label": label, "service": svc, "verb": verb, "required": required,
                    "ok": False, "elapsed_ms": elapsed_ms, "retried_ok": retried_ok, **facts,
                }
            )

        # 拿不到用户信息也要把报告出完 —— 用面板里配的租户名或 user OCID 尾段占位。
        user_label = (
            getattr(user, "name", "") if user is not None else ""
        ) or (self.tenant.name or "").strip() or f"…{self.tenant.user_ocid.strip()[-12:]}"
        name = (
            (getattr(user, "description", "") or getattr(user, "name", ""))
            if user is not None
            else user_label
        )
        # 成败只看**必需**的那一条(列出实例)。其余作为诊断上下文报出来。
        # 旧逻辑是「任一探针失败即失败」,而其中一个探针结构上不可能失败、另一个和
        # 第三个测的是同一件事 —— 那种判定既不敏感也不特异。
        blocking = [r for r in results if r["required"] and not r["ok"]]
        return OperationResult(
            ok=not blocking,
            message=_format_probe_report(user_label, name, region, compartment, results),
            data={
                "user": user_label,
                "compartment": compartment,
                "region": region,
                "probes": results,
                # 兼容旧字段:曾经有调用方读它。
                "failures": [f"{r['label']}：{r.get('code') or r.get('status')}" for r in results if not r["ok"]],
            },
        )

    # Oracle: "Maximum nested compartment hierarchy levels: 6"
    # （Content/Identity/compartments/Working_with_Compartments.htm 的 Limits 一节）
    # root 自己可能已经在第 1~5 层，所以从 root 往下再走 6 层是严格的上界。
    _MAX_COMPARTMENT_DEPTH = 6
    # 请求预算硬闸。超了就当「没读全」上报，绝不当成读全了 —— 这正是本 bug 的教训。
    _MAX_COMPARTMENT_CALLS = 64

    def _compartment_children(self, cid: str, *, in_subtree: bool = False) -> list[Any]:
        """列出一个 compartment 的子项。

        `compartment_id_in_subtree` **只有在租户根上才有意义**，所以默认整个不传。
        """
        kwargs: dict[str, Any] = {
            # ACCESSIBLE 是**结果过滤器**：只返回调用方有 INSPECT 权限的那些。
            # 不要换成 ANY —— 文档里 "permissions are not checked" 说的是过滤器
            # 不生效，不是「不需要权限」；换成 ANY 等于要求调用方在整个请求范围上
            # 拿到授权，而权限受限的租户正是枚举会失败的那批人。
            "access_level": "ACCESSIBLE",
            # 分页层自己**硬编码**又套了一层 DEFAULT_RETRY_STRATEGY，和 client 层
            # 那个相乘：最坏 8 × 8 = 64 次真实调用、~600 秒卡在一个 HTTP 请求里 ——
            # 在跟抢机重试循环抢同一个 per-tenancy 限流额度（CLAUDE.md）。
            # 收敛到 3 次 / 20 秒。service_error_retry_config 是**替换**语义，
            # 429/409 必须自己抄回来；也**不含 404** —— Oracle 的错误表把
            # 404 NotAuthorizedOrNotFound 的 Retry 列标成 "No."。
            "retry_strategy": sdk_bounded_paged_retry_strategy(),
        }
        if in_subtree:
            kwargs["compartment_id_in_subtree"] = True
        resp = oci.pagination.list_call_get_all_results(
            self.identity.list_compartments, cid, **kwargs
        )
        return list(resp.data or [])

    def _walk_compartment_subtree(self, root: str) -> tuple[list[Any], bool]:
        """逐层 BFS 出 root 的整棵子树。返回 (compartments, truncated)。

        为什么不能一次问完：ListCompartments 的文档说得很死 ——

            "With the exception of the tenancy (root compartment), the
             ListCompartments operation returns only the first-level child
             compartments in the parent compartment specified in compartmentId.
             The list does not include any subcompartments of the child
             compartments (grandchildren)."
            ":param bool compartment_id_in_subtree: Default is false. Can only be
             set to true when performing ListCompartments on the tenancy
             (root compartment)."

        对非根传 `compartment_id_in_subtree=True` 是**静默忽略**（调用成功、HTTP 200、
        只回一层），不是报错。于是孙层里的实例/卷一个都不在列表里，而
        `_last_tree_errors` 是空的、`read_incomplete` 是 False —— 配额守卫拿着一份
        少算的用量，认为额度还有富余，放行一台**计费**机器。
        这正是「读漏了却看起来像读全了」，比「读不到」危险得多。

        为什么不改成「从租户根 + subtree=True 列全，再客户端筛」：那要
        `inspect compartments in tenancy` 权限，而会把 compartment_ocid 指向子
        compartment 的租户，恰恰是策略被限制在那一层的那批人 —— 对他们会 404，
        等于把静默漏数换成更狠的静默漏数。

        环：OCI 的 compartment 是树（每个只有一个父指针），但我没有找到一句官方
        文档保证「不能移到自己的后代下」。所以不依赖无环 —— `seen` 集合加深度上限
        让任何环结构都必然终止。
        """
        out: list[Any] = []
        seen: set[str] = {root}
        frontier = [root]
        calls = 0
        depth = 0
        while frontier and depth < self._MAX_COMPARTMENT_DEPTH:
            nxt: list[str] = []
            for cid in frontier:
                if calls >= self._MAX_COMPARTMENT_CALLS:
                    return out, True
                calls += 1
                # 异常**不吞** —— 让它冒到 list_compartments 的 except，
                # 那里会写 _last_enum_facts 并按 strict 决定要不要 raise。
                for c in self._compartment_children(cid):
                    state = str(getattr(c, "lifecycle_state", "") or "")
                    # DELETING/DELETED 跳过且不下钻：文档要求 compartment 必须先清空
                    # 所有资源（含子 compartment）才能删，所以它们里面没有可计数的
                    # 东西；删除失败还会回到 ACTIVE，下次枚举自然重新收进来。
                    # 注意 CREATING / INACTIVE **不能**跳过 —— 它们可以持有资源。
                    if state in ("DELETED", "DELETING"):
                        continue
                    cid_child = getattr(c, "id", "") or ""
                    if not cid_child or cid_child in seen:
                        continue
                    seen.add(cid_child)
                    out.append(c)
                    nxt.append(cid_child)
            frontier = nxt
            depth += 1
        # frontier 非空 = 撞到深度上限，还有没走的层。
        return out, bool(frontier)

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
        truncated = False
        try:
            if not subtree:
                raw = self._compartment_children(root)
            elif root == tenancy or root.startswith("ocid1.tenancy."):
                # 只有在**租户根**上，compartment_id_in_subtree 才真正生效。
                raw = self._compartment_children(root, in_subtree=True)
            else:
                # 非根：必须自己逐层走。见 _walk_compartment_subtree 的注释。
                raw, truncated = self._walk_compartment_subtree(root)
            for c in raw:
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
            if truncated:
                # 「走不完」必须和「读不到」走同一条通道 —— 本 bug 的教训就是
                # 「读漏了但看起来像读全了」，绝不能再造一个同形的洞。
                self._last_enum_facts = {
                    "type": "SubtreeTruncated",
                    "status": 0,
                    "code": "SubtreeTruncated",
                    "request_id": "",
                    "operation": "list_compartments",
                    "host": "",
                }
                if strict:
                    raise OCIClientError(
                        "子 Compartment 层级过深或数量过多，本次没有遍历完整。"
                        "为避免把「没读全」当成「读全了」，这里主动报错 —— "
                        "请把租户配置里的 Compartment OCID 指向更靠近资源的那一层。"
                    )
        except OCIClientError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 必须是 except Exception，不能只接 ServiceError。
            #
            # IdentityClient 默认带熔断器，而它抛的 CircuitBreakerError **不是**
            # ServiceError（实测 issubclass 为 False）。只接 ServiceError 的话，
            # 熔断打开时这个异常会直接穿透出去 —— 连 strict=False 那条
            # 「退化成只有根」的承诺都保不住。连接超时同理。
            #
            # 至于 404 本身：一条只给了 `manage instance-family in compartment child`
            # 而没给 `inspect compartments in tenancy` 的策略会让这里**永久** 404；
            # 持续 429 也会短暂产生同样的结果。两种情况下，下面的配额读取都会把根的
            # 用量当成整个租户的、并且认为读全了。
            #
            # 把结构化事实留下来，让上层能说清楚到底是哪一种（_err_facts 已经声明
            # 这些字段可安全外发、不含 OCID）。
            self._last_enum_facts = _err_facts(exc)
            if strict:
                detail = (
                    _format_service_error(exc)
                    if isinstance(exc, ServiceError)
                    else f"{type(exc).__name__}: {exc}"
                )
                raise OCIClientError(detail) from exc
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
                # 分页层自己又套了一层 DEFAULT_RETRY_STRATEGY，和 client 层那个
                # 相乘（最坏 8x8=64 次调用 / ~600 秒）。收敛，把预算还给抢机循环。
                retry_strategy=sdk_bounded_paged_retry_strategy(),
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
                vpu = _vpus_or_default(getattr(bv, "vpus_per_gb", None))
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

    def get_instance(
        self,
        instance_id: str,
        resolve_ips: bool = True,
        reread_on_404: bool = False,
    ) -> InstanceInfo:
        """按 OCID 直读一台实例。

        实例详情页那个「刷新」按钮**唯一**能弹出红色错误框的调用就是这个 —— 页内
        其它 loader(监控/控制台/防火墙/保留 IP/引导卷)在前端各自 catch,只写自己那
        一块的提示。所以用户报的「详情页点刷新报 404」一定出自这里,而这里以前既不
        复读、也不说明是哪个调用失败的。

        ``reread_on_404`` **默认关**,只有那一次「失败会直接进红框」的读才打开。
        因为同一次刷新里这个方法会被调好几遍(详情路由一次,监控路由为了拿
        compartment_id 又一次,引导卷页还有更多),全都复读的话一次失败的刷新要打
        三倍请求、多等十几秒 —— 而其中只有一次的错误是用户看得见的,其余几次的
        异常在前端就被各自的 catch 吃掉了。给看不见的失败付重试代价没有意义。
        """
        reader = lambda: self.compute.get_instance(instance_id).data  # noqa: E731
        if reread_on_404:
            ok, inst, exc, rereads = read_with_404_evidence(reader)
        else:
            rereads = 0
            try:
                ok, inst, exc = True, reader(), None
            except Exception as _exc:  # noqa: BLE001
                ok, inst, exc = False, None, _exc
        if not ok:
            detail = (
                _format_service_error(exc)
                if isinstance(exc, ServiceError)
                else str(exc or "读取实例失败")
            )
            note = persistent_404_note(rereads)
            raise OCIClientError(f"{detail}\n{note}" if note else detail) from exc
        info = self._to_instance_info(inst)
        if rereads:
            # 记在返回值上,不是记在 session 上 —— 见 InstanceInfo.read_note 的注释。
            info.read_note = transient_404_note("按 OCID 读取实例被拒（404）", rereads)
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
                    retry_strategy=sdk_bounded_paged_retry_strategy(),
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

            # 一次把快照读全，然后在**本地**留尾部。
            #
            # 以前直接把 length_bytes(默认 256 KB)传给 Oracle，拿到的是快照的
            # **开头** —— 而 CaptureConsoleHistory 抓的是「up to a megabyte」，
            # 所以最坏情况是只看到前 25%。机器为什么起不来（panic、
            # 停在 initramfs、磁盘挂不上）恰恰写在**最后**那几十行里，
            # 也就是被丢掉的那部分。而且截断了也不给任何提示。
            # 读满 1 MB 和读 256 KB 是同一次请求，不多花调用。
            _WIRE_LEN = 1024 * 1024
            content = self.compute.get_console_history_content(
                history_id, length=_WIRE_LEN
            ).data
            # `.data` 是 **bytes**：这个调用在 SDK 里声明的是
            # response_type="bytes"（oci/core/compute_client.py），base_client
            # 直接返回 response.content。
            #
            # 之前这里先试 `getattr(content, "value")`，bytes 没有这个属性 ——
            # 于是落到 `str(content)`，把整段日志变成一行 `b'...\\n...'` 的
            # Python repr：换行是字面的两个字符，中文变成 \xNN。而这个功能的
            # 全部意义就是「机器起不来时让人读串口输出」。
            if isinstance(content, (bytes, bytearray)):
                # 串口输出不保证是干净的 UTF-8（内核早期可能是别的编码，
                # 也可能被截断在多字节字符中间），所以 replace 而不是 strict——
                # 抓到一半的日志也比抛异常有用。
                text = bytes(content).decode("utf-8", errors="replace")
            elif isinstance(content, str):
                text = content
            else:
                # 兜底：旧 SDK 曾用带 .value 的对象包装。
                text = str(getattr(content, "value", "") or "")
            truncated = False
            if length_bytes and len(text) > length_bytes:
                # 留**末尾**：故障原因在最后。
                text = text[-length_bytes:]
                # 丢掉被切断的首行，避免开头是半行乱码。
                newline = chr(10)
                if newline in text:
                    text = text.split(newline, 1)[1]
                truncated = True
            return OperationResult(
                ok=True,
                message=(
                    f"已抓取控制台输出（{len(text)} 字符"
                    + ("，已截断，只保留最后一段" if truncated else "")
                    + "）"
                ),
                data={"content": text, "history_id": history_id, "truncated": truncated},
            )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            # Surface the only compartment's error instead of empty silent list.
            #
            # 但要说清楚「只扫了一个 compartment」这件事本身，因为这正是间歇性
            # 报错的来源：枚举成功时会扫到子 compartment、找到实例、什么都不报；
            # 枚举失败时退化成只扫根，根里没有实例、又没权限，于是硬报错。同一个
            # 租户、同样的密钥，看起来就是「有时候好有时候坏」。
            # 只抛 errors[0] 的话，操作者拿到的是一句光秃秃的 Oracle 404，
            # 完全看不出扫描范围已经塌缩成一个 compartment 了。
            detail = errors[0]
            # 抛之前做且**只做一次**显式复读，并把结果如实说出来。
            #
            # 这不是「重试策略」，是取一位证据。Oracle 的 API 错误表把
            # 404 NotAuthorizedOrNotFound 的 Retry 列明确标成 "No."，Terraform 的
            # 排障页也把 404 列进「重试也不会成功」那组 —— 所以这里刻意不给 SDK 挂
            # 404 重试策略（那会让每一个真的没权限的调用都白白慢 8 倍）。
            #
            # 但复读一次的价值不同：**复读成功，这个 404 按定义就是瞬时的**，
            # 「权限问题」这个解释当场出局；复读仍然失败，那就是持续问题。
            # 用户现在的变通办法正是手动点三五次刷新，每次都是 1+N 个请求；
            # 这里只补 1 个幂等的 List，请求量严格更少。
            # 同形先例：test_connection 的探针（见那里的注释），理由逐字适用。
            retried_ok = False
            rereads = 0
            if _is_ambiguous_404(Exception(detail)):
                # 走和 get_instance 同一个复读策略（见 _REREAD_DELAYS）。
                #
                # 顺带修掉一个真 bug：这里以前写的是 `if again:` —— 复读**成功但读到
                # 空列表**（这个 compartment 确实一台实例都没有）会被判成复读失败，
                # 于是照样抛出那个 404，把「读得到、只是空的」说成了「没有权限」。
                # 判据必须是「调用成功了没有」，不是「返回的列表空不空」。
                ok_again, again, _exc, rereads = read_with_404_evidence(
                    lambda: _scan(compartments[0])
                )
                if ok_again:
                    retried_ok = True
                    all_items = list(again or [])
                    # 必须留痕：_last_tree_errors 会被路由渲染成 X-Ocibot-Partial 头。
                    self._last_tree_errors = [transient_404_note(detail, rereads)]
            if not retried_ok:
                # 不是 404 时 rereads 为 0、这里为空串 —— 不能对一个从没复读过的
                # 失败（比如 429、5xx）声称「已自动重读」。
                probe = persistent_404_note(rereads)
                if enum_error:
                    raise OCIClientError(
                        f"{detail}\n{probe}\n\n注意：本次未能枚举子 Compartment（{enum_error}），"
                        f"因此只扫描了 {root[-16:]} 这一个 Compartment。"
                        "如果实例其实在子 Compartment 里，这里就会既列不出实例、又报无权限 —— "
                        "而枚举成功的那几次则一切正常，表现为「时好时坏」。"
                        "\n请到租户页点「测试连接」确认具体是哪一项读不到。"
                    )
                raise OCIClientError(f"{detail}\n{probe}" if probe else detail)
        if resolve_ips:
            targets = [i for i in all_items if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
            self._enrich_instances_parallel(targets, root)
        all_items.sort(key=lambda i: (i.lifecycle_state != "RUNNING", i.display_name.lower()))
        return all_items

    def list_availability_domains(self, compartment_id: Optional[str] = None) -> list[str]:
        """可用域列表。

        默认用 resolve_compartment() 而不是写死 tenancy 根：可用域本身是租户级的，
        传哪个 compartment 拿到的都是同一份，但**权限是按 compartment 判的**。
        一个 IAM 策略只覆盖子 compartment 的密钥，问根会得到
        NotAuthorizedOrNotFound —— 而这一步是「加载配置」里第一个硬失败的调用，
        于是整页报一个和可用域毫无关系的 404。
        """
        comp = (compartment_id or self.resolve_compartment()).strip()
        try:
            ads = self.identity.list_availability_domains(comp).data
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
            resp = oci.pagination.list_call_get_all_results(self.compute.list_images, **kwargs,
                retry_strategy=sdk_bounded_paged_retry_strategy(),
            )
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
                resp2 = oci.pagination.list_call_get_all_results(self.compute.list_images, **kwargs,
                    retry_strategy=sdk_bounded_paged_retry_strategy(),
                )
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
                    resp3 = oci.pagination.list_call_get_all_results(self.compute.list_images, **kwargs,
                        retry_strategy=sdk_bounded_paged_retry_strategy(),
                    )
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
            resp = oci.pagination.list_call_get_all_results(
                self.compute.list_shapes,
                retry_strategy=sdk_bounded_paged_retry_strategy(),
                **kwargs,
            )
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
                retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            resp = oci.pagination.list_call_get_all_results(self.network.list_subnets, **kwargs,
                retry_strategy=sdk_bounded_paged_retry_strategy(),
            )
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
            self.network.list_internet_gateways, compartment_id, vcn_id=vcn_id,
            retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            self.network.list_route_tables, compartment_id, vcn_id=vcn_id,
            retry_strategy=sdk_bounded_paged_retry_strategy(),
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
                    # 目的地址**和**下一跳都要对上，不能只看目的地址。
                    #
                    # 只比 destination 的话，一条已经存在但指向别处（NAT 网关、
                    # 另一个 IGW、服务网关）的 ::/0 会被当成「已经配好了」，于是
                    # 什么都不做 —— 实例拿到 IPv6 地址、路由表看着也有 ::/0，
                    # 但出网根本不通，而创建流程一路报成功。
                    v6 = [
                        r
                        for r in rules
                        if (getattr(r, "destination", "") or "").strip() == "::/0"
                    ]
                    correct = any(
                        (getattr(r, "network_entity_id", "") or "") == igw_id for r in v6
                    )
                    if not correct:
                        # 指错地方的那条要换掉，不是再加一条：同一个目的地址出现
                        # 两条规则，Oracle 会直接拒绝这次 update。
                        rules = [r for r in rules if r not in v6]
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
        changed = False
        for rule in desired:
            # 目的地址**和**下一跳都要对上 —— 上面那个 ::/0 分支已经是这么写的，
            # 这条兜底路径当时漏了。
            #
            # 只比 destination 的话，一条已经存在但指向别处的 0.0.0.0/0
            # （NAT 网关、服务网关、另一个 IGW）会被当成「公网路由已就绪」，
            # 于是什么都不做：新建的公网子网里的实例拿到了公网 IP、路由表看着也有
            # 默认路由，但**出网根本不通**，而建网流程一路报成功。
            same_dest = [
                r
                for r in rules
                if (getattr(r, "destination", "") or "").strip() == rule.destination
            ]
            if any(
                (getattr(r, "network_entity_id", "") or "") == igw_id for r in same_dest
            ):
                continue
            # 指错地方的要**换掉**而不是再加一条：同一个目的地址出现两条规则，
            # Oracle 会拒绝整次 update。
            if same_dest:
                rules = [r for r in rules if r not in same_dest]
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
            self.network.list_security_lists, compartment_id, vcn_id=vcn_id,
            retry_strategy=sdk_bounded_paged_retry_strategy(),
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
        # VCN 上设了 dns_label，子网上也要设，主机名解析才真的可用 —— 文档说
        # VCN Resolver 需要**两者都有**。只设 VCN 那一半等于白设。
        #
        # 但这是个可选能力，不值得为它把整条建网路径变脆：dns_label 在同一个 VCN 内
        # 必须唯一，撞名会 409。所以先带着试一次，失败就退回不带。
        def _create(with_label: bool) -> Any:
            kwargs: dict[str, Any] = {}
            if with_label:
                kwargs["dns_label"] = "publicsubnet"
            return self.network.create_subnet(
                oci.core.models.CreateSubnetDetails(
                    compartment_id=compartment_id,
                    vcn_id=vcn_id,
                    cidr_block=cidr_block,
                    display_name=display_name,
                    route_table_id=route_table_id,
                    security_list_ids=[security_list_id],
                    prohibit_public_ip_on_vnic=False,
                    freeform_tags={"ocibot_managed": "true"},
                    **kwargs,
                )
            ).data

        try:
            subnet = _create(True)
        except ServiceError:
            subnet = _create(False)
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
                        self.network.list_subnets, compartment_id=vcn_comp, vcn_id=vcn_id,
                        retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            return OperationResult(ok=False, message=safe_error_text(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            # metadata 的上限是 **32,000 字节**（LaunchInstanceDetails.metadata 的
            # 文档：总大小不超过 32,000 bytes），而上游那道闸门量的是**字符数**
            # （16,000 字符的自定义启动脚本）—— 两者根本不是一个单位：
            # 脚本先被 build_root_cloud_init 包成完整的 cloud-config，再 base64
            # （+33%），中文一个字三字节。16,000 个中文字符能生成 80 KB 以上的
            # metadata，Oracle 会直接 400，而那时网络/NSG 已经建好了。
            # 在这里量一次可以同时覆盖向导和抢机 worker 两条路径。
            meta_bytes = sum(
                len(str(k).encode("utf-8")) + len(str(v).encode("utf-8"))
                for k, v in (metadata or {}).items()
            )
            if meta_bytes > 32000:
                return OperationResult(
                    ok=False,
                    message=(
                        f"启动脚本经 cloud-init 组装并 base64 后为 {meta_bytes} 字节，"
                        "超过 Oracle 对实例 metadata 的 32,000 字节上限。"
                        "请缩短自定义启动脚本（注意中文一个字算三字节，base64 还会再涨约 1/3）。"
                    ),
                )
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            retry_strategy=sdk_bounded_paged_retry_strategy(),
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
    def _ssh_only_specs(include_ipv6: bool) -> list[FirewallRuleSpec]:
        """不勾「允许外网直接访问」时的规则:只放 SSH 入站,出站不限。

        为什么仍然建一个 NSG、而不是干脆不建:
          * 不建的话实例只受子网 Security List 管,而那份规则不由面板掌控 ——
            自建 VCN 和用户既有 VCN 的默认规则不一样,最坏情况是**连 22 都不通**,
            用户创建完直接连不上机器。把人锁在外面比开得太宽更糟。
          * 详情页的防火墙管理、以及创建失败时按 launch_token 回收 NSG,
            都建立在「这台机器有一个自己的托管 NSG」之上。
        """
        specs = [
            FirewallRuleSpec(
                "INGRESS", "6", "0.0.0.0/0", port_min=22, port_max=22,
                description="ocibot SSH 入站",
            ),
            FirewallRuleSpec("EGRESS", "all", "0.0.0.0/0", description="ocibot IPv4 出站"),
        ]
        if include_ipv6:
            specs.extend(
                [
                    FirewallRuleSpec(
                        "INGRESS", "6", "::/0", port_min=22, port_max=22,
                        description="ocibot SSH 入站 (IPv6)",
                    ),
                    FirewallRuleSpec("EGRESS", "all", "::/0", description="ocibot IPv6 出站"),
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
            return OperationResult(ok=False, message=safe_error_text(exc))

    def create_managed_nsg(
        self,
        *,
        vcn_id: str,
        compartment_id: str,
        display_name: str,
        include_ipv6: bool = False,
        launch_token: str = "",
        open_all: bool = True,
    ) -> OperationResult:
        """open_all=False 时只放 SSH 入站。

        默认仍是 True:这个方法还有别的调用方(详情页的「一键开放全部端口」),
        它们的语义本来就是全开放。创建路径会显式传 open_all=表单上那个勾选。
        """
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
            specs = self._open_all_specs(include_ipv6) if open_all else self._ssh_only_specs(include_ipv6)
            added = self.add_nsg_rules(nsg.id, specs)
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

    def get_instance_firewall(self, instance_id: str, compartment_id: str) -> OperationResult:
        try:
            network = self.resolve_primary_network(instance_id, compartment_id)
            groups = []
            for nsg_id in network.nsg_ids:
                nsg = self.network.get_network_security_group(nsg_id).data
                rules = oci.pagination.list_call_get_all_results(
                    self.network.list_network_security_group_security_rules,
                    nsg_id,
                    retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
        else:
            # ICMP 没有端口，但有 type/code —— 丢掉它们等于把一条**精确**的规则
            # 显示成「全放行」。最典型的是 Oracle 默认安全列表里那条只放行
            # ICMPv4 type 3 code 4（Path MTU Discovery）的规则：现在会被读成
            # 「ICMP 全部放行」，让人以为网络比实际开放得多。
            icmp = getattr(rule, "icmp_options", None)
            if icmp is not None:
                itype = getattr(icmp, "type", None)
                icode = getattr(icmp, "code", None)
                if itype is not None:
                    port = f"类型 {itype}" + (f" 代码 {icode}" if icode is not None else "")
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            vpu = _vpus_or_default(getattr(bv, "vpus_per_gb", None))
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
                        retry_strategy=sdk_bounded_paged_retry_strategy(),
                    )
                    for bv in resp.data or []:
                        vid = getattr(bv, "id", "") or ""
                        if not vid or vid in seen:
                            continue
                        state = str(getattr(bv, "lifecycle_state", "") or "")
                        if state in {"TERMINATED", "TERMINATING"}:
                            continue
                        seen.add(vid)
                        vpu = _vpus_or_default(getattr(bv, "vpus_per_gb", None))
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
                        retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            if est.ok and isinstance(est.data, dict):
                object_usage = est.data
            else:
                # 读不到就让 object_usage 留空 —— build_quota_snapshot 据此**省略**
                # 这根仪表，而不是画一根「已用 0 / 20 GB，正常」的假仪表。
                read_incomplete = True
            if est.message:
                # 以前这条在 not est.ok 时会被 append 两次（上面一次、下面一次），
                # 于是同一句话在摘要里重复出现。
                notes.append(est.message)
        except Exception as exc:  # noqa: BLE001
            read_incomplete = True
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
            # 必须看附件状态。
            #
            # ListBootVolumeAttachments 返回的是**历史全部**附件记录，换过引导卷的
            # 实例会有多条：旧盘那条是 DETACHED、新盘那条才是 ATTACHED。原来直接
            # next() 取第一条有 boot_volume_id 的，于是详情页显示的容量、以及
            # 「调整引导卷」实际操作的对象，都可能是那块**已经拆下来的旧盘**。
            #
            # ATTACHING 必须留着：这个函数在创建实例后 wait=True 轮询新盘，
            # 那时状态正是 ATTACHING —— 只认 ATTACHED 会让「创建后调整 VPU」
            # 白等满 150 秒然后放弃。
            usable = [
                a
                for a in attachments
                if getattr(a, "boot_volume_id", "")
                and str(getattr(a, "lifecycle_state", "") or "").upper()
                in {"ATTACHED", "ATTACHING"}
            ]
            # ATTACHED 优先于 ATTACHING。
            usable.sort(
                key=lambda a: str(getattr(a, "lifecycle_state", "") or "").upper() != "ATTACHED"
            )
            att = usable[0] if usable else None
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
        if vpus_per_gb is not None and vpus_per_gb not in (0, 10, 20) and not 30 <= int(vpus_per_gb) <= 120:
            return OperationResult(ok=False, message="性能必须为 0（低成本）、10、20 或 30–120 VPUs/GB")
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
            current = None
            while time.monotonic() < deadline:
                current = self.blockstorage.get_boot_volume(bv_id).data
                state = getattr(current, "lifecycle_state", "")
                if state == "AVAILABLE":
                    break
                if state in ("FAULTY", "TERMINATED", "TERMINATING"):
                    return OperationResult(ok=False, message=f"引导卷状态为 {state}，无法调整")
                time.sleep(3)
            # 「只能扩大」必须在服务端拦。
            #
            # 前端那个输入框的 label 写着「≥ 当前且 ≥50，只能扩大」，但那只是文案 ——
            # 服务端一直没查。同一个文件里的块卷路径是查了的，引导卷这条漏了。
            # 绕过前端直接发一个更小的值，会拿到一个 Oracle 侧的原始报错；
            # 更糟的是这个请求本身就是白花的一次写调用。
            # 这次读取是复用上面等待循环里已经拿到的对象，不额外发请求。
            if size_in_gbs is not None and current is not None:
                cur_size = int(getattr(current, "size_in_gbs", 0) or 0)
                if cur_size and int(size_in_gbs) < cur_size:
                    return OperationResult(
                        ok=False,
                        message=(
                            f"引导卷只能扩大，不能缩小：当前 {cur_size} GB，"
                            f"目标 {int(size_in_gbs)} GB。"
                        ),
                    )
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
                retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            return OperationResult(ok=False, message=safe_error_text(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=safe_error_text(exc))

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
                self.network.list_ipv6s, vnic_id=network.vnic_id,
                retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            # 子网上有**多个** IPv6 前缀时 ipv6_subnet_cidr 是必填的（文档：
            # "Required if the subnet has multiple IPv6 prefixes"）。自带地址
            # （BYOIPv6）或同时有 GUA + ULA 的子网就是这种情况，不传会直接失败。
            # 只有一个前缀时不传 —— 别给单前缀子网引入一个新的失败面。
            ipv6_kwargs: dict[str, Any] = {}
            try:
                subnet = self.network.get_subnet(network.subnet_id).data
                blocks = self._subnet_ipv6_blocks(subnet)
                if len(blocks) > 1:
                    # 挑一个全球单播（GUA）前缀：ULA 是 fc00::/7，也就是首字节
                    # 落在 fc/fd 的那些，它出不了公网。
                    gua = [
                        b
                        for b in blocks
                        if not str(b).lower().lstrip().startswith(("fc", "fd"))
                    ]
                    ipv6_kwargs["ipv6_subnet_cidr"] = (gua or blocks)[0]
            except Exception:  # noqa: BLE001
                # 读不到子网不该让分配直接失败 —— 退回不传，单前缀子网照样能过。
                pass
            details = oci.core.models.CreateIpv6Details(
                vnic_id=network.vnic_id, **ipv6_kwargs
            )
            ipv6 = self.network.create_ipv6(details).data
            address = getattr(ipv6, "ip_address", "") or ""
            route = self.ensure_ipv6_internet_access(network.subnet_id, compartment_id)
            suffix = f"；{route.message}" if route.ok else (
                f"；⚠ 公网路由设置失败，可能仅内网可用：{route.message}"
            )
            # 光有地址和路由还不通 —— 安全规则里只有 IPv4。
            #
            # 以前这里只补 ::/0 路由就报「已分配公网 IPv6」，而 NSG 里那两条规则
            # 写的是 0.0.0.0/0。IPv6 流量在安全组这一层就被丢掉了，用户拿到一个
            # ping 不通的地址，界面却是绿的。
            # 只改**本工具托管**的 NSG；别人的 NSG 和子网安全列表不碰，但要说出来。
            rules_note = self._ensure_ipv6_rules_on_managed_nsgs(network)
            if rules_note:
                suffix += f"；{rules_note}"
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
            return OperationResult(ok=False, message=safe_error_text(exc))

    def _ensure_ipv6_rules_on_managed_nsgs(self, network: Any) -> str:
        """给实例所在的**托管** NSG 补上 IPv6 版安全规则。返回一句给用户看的说明。

        只动带 ocibot 标签的 NSG：别人手工建的 NSG 和子网 Security List 不该被这个
        操作悄悄改写 —— 但要在返回文案里说清楚「没动的是哪些」，否则用户会以为
        IPv6 已经全通了。
        """
        nsg_ids = [n for n in (getattr(network, "nsg_ids", None) or []) if n]
        if not nsg_ids:
            return "未检测到实例专属安全组，IPv6 规则需要自行在子网安全列表里添加"
        touched, skipped = 0, 0
        for nsg_id in nsg_ids:
            try:
                group = self.network.get_network_security_group(nsg_id).data
                if not self._is_ocibot_managed_nsg(getattr(group, "freeform_tags", None)):
                    skipped += 1
                    continue
                existing = oci.pagination.list_call_get_all_results(
                    self.network.list_network_security_group_security_rules,
                    nsg_id,
                    retry_strategy=sdk_bounded_paged_retry_strategy(),
                ).data or []
                have = {
                    (
                        str(getattr(r, "direction", "") or "").upper(),
                        str(getattr(r, "protocol", "") or ""),
                        str(getattr(r, "source", None) or getattr(r, "destination", None) or ""),
                    )
                    for r in existing
                }
                wanted = [
                    spec
                    for spec in self._open_all_specs(include_ipv6=True)
                    if ":" in spec.cidr
                    and (spec.direction.upper(), spec.protocol, spec.cidr) not in have
                ]
                if wanted:
                    self.add_nsg_rules(nsg_id, wanted)
                    touched += len(wanted)
            except Exception:  # noqa: BLE001
                skipped += 1
        parts = []
        if touched:
            parts.append(f"已补 {touched} 条 IPv6 安全规则")
        if skipped:
            parts.append(f"{skipped} 个非托管安全组未改动，如不通请自行添加 ::/0 规则")
        if not parts:
            parts.append("IPv6 安全规则已存在")
        return "，".join(parts)

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
                self.network.list_internet_gateways, vcn_compartment, vcn_id=vcn_id,
                retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            return OperationResult(ok=False, message=safe_error_text(exc))


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
                retry_strategy=sdk_bounded_paged_retry_strategy(),
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
        # 校验必须在**删除已有连接之前**。
        #
        # Oracle 每个实例只允许一个活动的控制台连接，所以下面会先把现有的删掉。
        # 而校验以前放在 try 里、删除之后 —— 一个 ed25519 公钥会先把操作员正在用的
        # 那条连接删光，然后才发现建不出可用的新连接。
        #
        # 串口控制台**只支持 RSA**。文档原文（Connecting to the Serial Console）：
        # "you must use an RSA key"。ed25519 / ECDSA 能把连接建出来，但 ssh 上去
        # 会被拒 —— 所以放行它们等于给一条建得出来、用不了的连接。
        if not re.match(r"^ssh-rsa\s+\S+", key):
            if re.match(r"^(ssh-ed25519|ecdsa-sha2-[^ ]+)\s+\S+", key):
                return OperationResult(
                    ok=False,
                    message=(
                        "Oracle 串口控制台只支持 RSA 密钥（官方文档：you must use an RSA key）。"
                        "ed25519 / ECDSA 能建出连接，但 ssh 上去会被拒绝。"
                        "请另生成一把：ssh-keygen -t rsa -b 2048"
                    ),
                )
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
            state = str(getattr(conn, "lifecycle_state", "") or "")
            while time.monotonic() < deadline:
                conn = self.compute.get_instance_console_connection(conn.id).data
                state = str(getattr(conn, "lifecycle_state", "") or "")
                if state == "ACTIVE":
                    break
                if state in ("FAILED", "DELETED", "DELETING"):
                    return OperationResult(ok=False, message=f"控制台连接创建失败（状态 {state}）")
                time.sleep(3)
            # 循环**超时**退出时以前也走到下面的 ok=True：界面显示「控制台连接已就绪」，
            # 而 connection_string 还是空的 —— 用户拿到一条空的 ssh 命令。
            if state != "ACTIVE":
                return OperationResult(
                    ok=False,
                    message=(
                        f"控制台连接未在 90 秒内就绪（当前状态 {state or '未知'}）。"
                        "连接已经创建出来了，可以稍后点「刷新」继续等，或删除后重建。"
                    ),
                    data={"id": getattr(conn, "id", "") or ""},
                )
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
        # 读取失败不能再被说成「没有数据」。
        #
        # 这四条查询以前都是 `except ServiceError: series[key] = []`，
        # 然后无论如何都返回 ok=True、消息写死成「暂无监控数据
        # （实例需启用计算代理 / 监控插件）」。于是一次 404、一次限流、
        # 或者熝断器打开，给用户的结论都是「去实例里装监控插件」——
        # 又一件把人指向一个本来没坏的东西的事，和 0.4.90 修掉的
        # 「请检查 API Key」是同一类错误。监控又正好是详情页的**默认**标签页。
        read_errors: list[str] = []
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
            except ServiceError as exc:
                series[key] = []
                read_errors.append(_format_service_error(exc).splitlines()[0])
            except Exception as exc:  # noqa: BLE001
                series[key] = []
                read_errors.append(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        if any_data:
            message = "已获取监控数据"
        elif read_errors:
            # 读失败了就说读失败了，并把 Oracle 原话交出去。
            message = (
                f"监控数据读取失败（{len(read_errors)}/{len(queries)} 条查询出错）：{read_errors[0]}"
                + chr(10)
                + "这不等于实例没开监控插件 —— 先看上面这条错误。"
            )
        else:
            message = "暂无监控数据（实例需启用计算代理 / 监控插件）"
        return OperationResult(
            ok=not read_errors or any_data,
            message=message,
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
                #
                # `total` 同理，而它以前是 0 —— 这一段的注释写着这条原则，下一行却
                # 违反了它。前端 `{{ usage?.total ?? '—' }}` 用的是 ??，0 不是
                # nullish，于是「读不到费用」被渲染成「合计：0」。对账单数字来说，
                # 「0」和「读不到」是相反的两个答案。
                data={
                    "daily": [],
                    "by_service": [],
                    "total": None,
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
            # 跟进 opc-next-page。
            #
            # 以前只读第一页：一个用量记录较多的租户，账单页显示的是**部分**合计，
            # 而且没有任何地方说它被截断了 —— 一个权威的、偏小的数字。
            # 硬上限 10 页，别让一个大租户把请求预算打光（CLAUDE.md）。
            items: list[Any] = []
            page: Optional[str] = None
            pages_read = 0
            truncated_pages = False
            for _ in range(10):
                kw = {"page": page} if page else {}
                resp = self.usage.request_summarized_usages(details, **kw)
                usage_data = getattr(resp, "data", None)
                items.extend(list(getattr(usage_data, "items", None) or []))
                pages_read += 1
                page = getattr(resp, "next_page", None) or (
                    (getattr(resp, "headers", None) or {}).get("opc-next-page")
                    if hasattr(resp, "headers")
                    else None
                )
                if not page:
                    break
            else:
                truncated_pages = bool(page)
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
                    # 读不到费用就是 0，**不能拿单价顶上**。
                    #
                    # SDK 的字段说明写得很清楚：computed_amount 是 "The computed cost."，
                    # 而 unit_price 是 "The price per unit." —— 两者差一个用量系数。
                    # computedAmount 为 null 最典型的场景恰恰是「免费额度内的用量」，
                    # 于是每一条这样的记录都会把**每单位价格**当成该行费用累加进
                    # total / month_to_date / by_service，账单页凭空多出一笔钱。
                    cost = 0
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
                message=(
                    (
                        "已获取用量/费用汇总"
                        if daily
                        else "暂无账单数据（免费账号或无 Usage 权限时常见）"
                    )
                    # 截断必须说出来。一个偏小但看起来权威的合计，比读不到更糟。
                    + (
                        f"（注意：用量记录超过 {pages_read} 页，下面的合计**不完整**）"
                        if truncated_pages
                        else ""
                    )
                ),
                data={
                    "daily": daily,
                    "truncated": truncated_pages,
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
                #
                # `total` 同理，而它以前是 0 —— 这一段的注释写着这条原则，下一行却
                # 违反了它。前端 `{{ usage?.total ?? '—' }}` 用的是 ??，0 不是
                # nullish，于是「读不到费用」被渲染成「合计：0」。对账单数字来说，
                # 「0」和「读不到」是相反的两个答案。
                data={
                    "daily": [],
                    "by_service": [],
                    "total": None,
                    "currency": "",
                    "days": days,
                    "month_to_date": None,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return OperationResult(
                ok=False,
                message=safe_error_text(exc),
                # month_to_date is None, not 0: for a cost figure those two mean
                # very different things, and a page that prints 0.00 for a read it
                # never managed to perform is worse than one that prints nothing.
                #
                # `total` 同理，而它以前是 0 —— 这一段的注释写着这条原则，下一行却
                # 违反了它。前端 `{{ usage?.total ?? '—' }}` 用的是 ??，0 不是
                # nullish，于是「读不到费用」被渲染成「合计：0」。对账单数字来说，
                # 「0」和「读不到」是相反的两个答案。
                data={
                    "daily": [],
                    "by_service": [],
                    "total": None,
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
            client = InvoiceServiceClient(cfg, **cb_kwargs("invoice", str(getattr(self.tenant, "id", "") or "")))
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
                # 措辞不能替 Oracle 下结论。NotAuthorizedOrNotFound 同时意味着
                # 「没权限」和「不存在」，把它说成「因此不会产生账单」是从一个
                # 读不到的结果里推出了一个肯定的财务结论 —— 而如果真相是「没有
                # 账单读取权限」，这句话会让一个正在产生费用的账号看起来是免费的。
                return OperationResult(
                    ok=True,
                    message=(
                        "无法读取账单：Oracle 返回「无权限或不存在」，这两种情况它用的是"
                        "同一个错误码，因此无法区分。"
                        "\n · 如果这是 Always Free / 试用账号，多半确实没有订阅，属于正常；"
                        "\n · 如果这是付费账号，请确认当前用户有账单读取权限"
                        "（Allow group <你的组> to read invoices in tenancy）—— "
                        "在确认之前，请勿把这里的空白当作「没有产生费用」。"
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
        # 出网 10 TB 是**整租户**额度，所以要从租户根查。
        #
        # 以前传的是 resolve_compartment()，而 compartment_id_in_subtree=true 只在
        # 租户根上有效（和 ListCompartments 是同一条规则）—— 配了子 compartment 的
        # 租户因此永远读不到完整用量，那根 10 TB 仪表要么偏低要么空。
        tenancy = self.tenant.tenancy_ocid.strip()
        compartment = tenancy or self.resolve_compartment()
        scope_note = ""
        try:
            details = oci.monitoring.models.SummarizeMetricsDataDetails(
                namespace="oci_vcn",
                query="VnicToNetworkBytes[1h].sum()",
                start_time=start,
                end_time=end,
            )
            try:
                resp = self.monitoring.summarize_metrics_data(
                    compartment, details, compartment_id_in_subtree=True
                ).data
            except ServiceError as exc:
                # 租户级读取需要租户级权限。没有的话退化成只查配置的那个
                # compartment —— 但必须**说出来**范围缩小了，而不是把读不全当成读全。
                if int(getattr(exc, "status", 0) or 0) not in (400, 401, 403, 404):
                    raise
                compartment = self.resolve_compartment()
                resp = self.monitoring.summarize_metrics_data(compartment, details).data
                scope_note = (
                    "（无租户级监控权限，只统计了配置的 Compartment，实际用量可能更高）"
                )
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=safe_error_text(exc))

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

        # 十进制 GB，不是 GiB。
        #
        # 这个数字要和 free_quota.FREE_EGRESS_GB 比较，而那条免费额度来自 Oracle
        # 的「每月 10 TB 出网」—— 云厂商的流量一律按十进制计（1 TB = 10^12 字节）。
        # 以前两边都用 1024**3，看着自洽，实际把阈值抬高了约 10%：真实上限是
        # 9313 GiB，而守卫要到 10240 GiB 才报警，中间那 900 多 GiB 是**要计费**的，
        # 面板却还显示在免费额度内。标签写的也一直是「GB」。
        egress_gb = total_bytes / (1000**3)
        return OperationResult(
            ok=True,
            message=scope_note,
            data={
                "egress_gb": round(egress_gb, 3),
                "scope_limited": bool(scope_note),
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
                self.limits.list_limit_values, tenancy_id, service_name="compute",
                retry_strategy=sdk_bounded_paged_retry_strategy(),
            ).data
            # 保留可用域这一维。
            #
            # 服务限额是**按可用域**报的：同一个 name 在 3 个 AD 上有 3 条记录。
            # 以前用 `shown[name] = numeric` 收进一个 dict —— 后写的覆盖先写的，
            # 只剩最后一个 AD 的那条，而且界面上完全看不出它只代表一个 AD。
            # 一个「AD-1 有配额、AD-2/3 没有」的租户，看到的可能正是没有的那个。
            rows: list[dict[str, Any]] = []
            for v in values:
                name = str(getattr(v, "name", "") or "")
                if not name.endswith("count"):
                    continue
                if not any(tag in name for tag in FREE_TIER_LIMIT_TAGS):
                    continue
                value = getattr(v, "value", None)
                try:
                    numeric = float(value) if value is not None else 0.0
                except (TypeError, ValueError):
                    numeric = 0.0
                rows.append(
                    {
                        "name": name,
                        # 空串 = 该限额是租户级/区域级的，不分 AD。
                        "ad": str(getattr(v, "availability_domain", "") or ""),
                        "scope": str(getattr(v, "scope_type", "") or ""),
                        "value": numeric,
                    }
                )
            info["limits"] = sorted(rows, key=lambda r: (r["name"], r["ad"]))[:12]
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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

    @staticmethod
    def _to_local(value: Any) -> Any:
        """把一个 aware datetime 转成本机时区，按**那一刻**的偏移量。

        必须是 `value.astimezone()`（不带参数），不能先取一个时区对象再套上去。
        `datetime.now().astimezone().tzinfo` 拿到的是一个**固定偏移**的
        `datetime.timezone`，记录的是「此刻」的偏移量；而密码到期日通常在 30–365 天
        之后,中间大概率跨过一次夏令时切换。拿今天的偏移去渲染那一刻,算出来的
        日历日会差一天 —— 这正是本函数存在的意义(修「少算一天」),反而在夏令时
        地区把结果弄得比原来的 UTC 版本更糟:
            operator 在 America/New_York,今天 2026-01-15(EST, -05:00),
            到期 2026-05-15T04:30Z(那天已是 EDT, -04:00)
            冻结偏移 -> 2026-05-14  ✗    真实偏移 -> 2026-05-15  ✓    旧的 UTC -> 2026-05-15 ✓
        不带参数的 `.astimezone()` 由 CPython 按该时间戳去问操作系统要偏移量,
        夏令时是对的。

        抽成一个函数是为了留一个测试接缝：时区来自操作系统，而 `time.tzset()` 在
        Windows 上根本不存在，只能 monkeypatch 这里才能确定性地验证
        「UTC 日历日 ≠ 本地日历日」那条分支。
        """
        return value.astimezone()

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
        now = datetime.now(timezone.utc)
        # 到期日期按本地时区渲染。原来 expires_at.strftime() 取的是 UTC 日历日，
        # 而 SPA 里其余日期都是本地时区：一个 2026-09-01T20:00Z 到期的密码，对
        # UTC+8 的操作员来说本地已经是 09-02 了，面板却印 09-01 —— 少一天，
        # 而且和同一屏幕上别处的日期对不上。
        expires_local = cls._to_local(expires_at)
        now_local = cls._to_local(now)
        # 剩余天数取「日历日之差」，和 config_store.TenantConfig.password_days_left() 同一套
        # 语义（今天到期=0、明天=1、昨天=-1）。原来写的是
        #     (expires_at - now).days
        # timedelta.days 向下取整，而 last_set 总是过去若干小时，于是几乎每个真实
        # 密码都少算一天：120 天策略下今天刚改的密码只报 119 天；明天到期的密码
        # 报成「2026-08-25 到期（还有 0 天）」—— 日期说明天、天数说 0，自相矛盾。
        left = (expires_local.date() - now_local.date()).days
        out["expires_at"] = expires_at.isoformat()
        out["days_left"] = left
        date_text = expires_local.strftime("%Y-%m-%d")
        # 是否已过期改用真正的时刻比较。不能再沿用 left < 0：日历日之差对「半小时前
        # 刚过期」给出的是 0（还是同一天），照旧判断会把一个已经登不进控制台的密码
        # 报成「今天到期，还有 0 天」。反过来旧写法对这种情况给 -1，日期与天数同样
        # 打架 —— 一个刚过期半小时的密码不该显示成过期了一整天。
        if user.get("expired") or now >= expires_at:
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
                identity = IdentityClient(self._config_for_region(home), **cb_kwargs("identity", str(getattr(self.tenant, "id", "") or "")))
            except Exception:  # noqa: BLE001
                identity = self.identity

        response = oci.pagination.list_call_get_all_results(
            identity.list_domains,
            compartment_id=tenancy,
            lifecycle_state="ACTIVE",
            retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            **cb_kwargs("identitydomains", str(getattr(self.tenant, "id", "") or "")),
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
            return OperationResult(ok=False, message=safe_error_text(exc))

        home = ""
        regions: list[dict[str, Any]] = []
        for sub in subs:
            name = str(getattr(sub, "region_name", "") or "").strip().lower()
            is_home = bool(getattr(sub, "is_home_region", False))
            # status 只有 READY 才是真的可用。文档给的取值是 READY / IN_PROGRESS ——
            # 一个刚点过「开通副区」、还在 IN_PROGRESS 的区域，资源是建不出来的，
            # 但面板以前只看这一行存不存在就当成「已开通」，于是用户会拿着一个
            # 还没就绪的区域去创建，然后收到一个和区域订阅毫无关系的报错。
            status = str(getattr(sub, "status", "") or "").strip().upper()
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
                    "status": status,
                    # 给调用方一个不用自己解析枚举的判据。
                    "ready": status == "READY",
                    "is_home_region": is_home,
                }
            )
        regions.sort(key=lambda r: (not r["is_home_region"], r["region_name"]))
        if home:
            # Same answer _home_region() would compute; seed its cache for free.
            #
            # 两个字段必须**一起**写。只写 _home_region_name 的话，_home_region()
            # 下次会在 `if cached: return cached` 那里提前 return，永远走不到设置
            # _home_region_resolved 的那几行 —— 于是 home_region_confirmed() 对这个
            # session 永远返回 ""，quota_guard.region_pair 拿到 ("", "")，副区闸门
            # 退回 DB 的 parent_tenant_id hint。手工添加的副区租户没有那个字段，
            # 闸门就此静默失效，一台**计费**机器会被当成免费的放行。
            # 而这里的 home 恰恰是**真的问出来的**（就是上面那次 Oracle 读的结果），
            # resolved 理应为 True —— 少写一个字段把「问出来了」降级成了「猜的」。
            self._home_region_name = home
            self._home_region_resolved = True
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
            return OperationResult(ok=False, message=safe_error_text(exc))
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
                # 「订阅记录存在」不等于「可以用了」。status 为 IN_PROGRESS 时资源
                # 还建不出来，把它当成已开通会让用户拿着一个未就绪的区域去创建，
                # 然后收到一个和区域订阅毫无关系的报错。
                ready = bool(item.get("ready"))
                state = str(item.get("status") or "")
                return OperationResult(
                    ok=True,
                    message=(
                        f"该区域已开通：{item.get('region_name')}"
                        if ready
                        else f"该区域正在开通中（{state or 'IN_PROGRESS'}），"
                        f"尚不能创建资源：{item.get('region_name')}"
                    ),
                    data={
                        "region_name": item.get("region_name") or "",
                        "region_key": item.get("region_key") or "",
                        "already": True,
                        "ready": ready,
                        "status": state,
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
                **cb_kwargs("identity", str(getattr(self.tenant, "id", "") or "")),
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
        # 记下这个值到底是**问出来的**还是**猜的**。
        #
        # 读失败时回退到租户自己填的 region 对预算/账单那几个调用方是合理的兜底,
        # 但它同时让 quota_guard.region_pair 拿到 current == home ——「当前就是主区」。
        # 而 region_pair 的 docstring 和 resolve_secondary 都建立在「读不出来就返回
        # 空串、退回 DB 的 parent_tenant_id」之上。结果是:一个副区租户在区域订阅
        # 读失败时被判成主区,免费额度检查照常跑,而副区的用量快照只统计那一个区域、
        # 读起来是「一点没用」—— 于是一台**计费**机器被当成免费的放行。
        # 副区闸门在最需要它的时候(读不到)恰好失效。
        resolved = False
        try:
            subs = self.identity.list_region_subscriptions(self.tenant.tenancy_ocid.strip()).data
            home = next((s for s in subs if getattr(s, "is_home_region", False)), None)
            if home and getattr(home, "region_name", ""):
                region = home.region_name
                resolved = True
        except Exception:  # noqa: BLE001
            pass
        # 只在**真的问出来**时才写缓存。
        #
        # 0.4.93 加 resolved 标志时把它和 region 一起无条件缓存了，于是一次瞬时的
        # 读取失败（限流、熔断、网络抖动）会被这个 session 永久记住 —— 只要进程还在、
        # 租户配置没改，home_region_confirmed() 就一直返回 ""，副区闸门也就一直
        # 退回 DB hint。既然是「读不到」，下一次就该重新读，而不是把失败也缓存起来。
        if resolved:
            self._home_region_name = region
            self._home_region_resolved = True
        else:
            self._home_region_resolved = False
        return region

    def home_region_confirmed(self) -> str:
        """主区名,**只在真的问出来时**才返回;猜的一律返回 ""。

        给依赖「读不到就别下结论」的调用方用(quota_guard.region_pair)。
        需要兜底值的调用方继续用 home_region()。
        """
        value = self._home_region()
        return value if getattr(self, "_home_region_resolved", False) else ""

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
                    **cb_kwargs("subscription", str(getattr(self.tenant, "id", "") or "")),
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
            return OperationResult(ok=False, message=safe_error_text(exc), data={"recovery_possible": True})

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
                retry_strategy=sdk_bounded_paged_retry_strategy(),
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
                    # 架构**未知**就明说未知。
                    #
                    # Image 模型里没有架构字段，自定义镜像的名字又是用户自己起的
                    # （"my-backup" 之类）—— 前端以前按名字里有没有 arm/aarch64 猜，
                    # 一台 A1 机器做出来的自定义镜像会被判成 x86，然后把 A1.Flex
                    # 从规格下拉里过滤掉，用户根本选不到那个免费机型。
                    # 空串 = 不知道，前端据此**不过滤**，而不是按猜测过滤。
                    "architecture": "",
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
                retry_strategy=sdk_bounded_paged_retry_strategy(),
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
                    # 按状态判断，不看 deprecated 的 private_ip_id —— 见 _public_ip_busy。
                    "assigned": _public_ip_busy(ip),
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
            return OperationResult(ok=False, message=safe_error_text(exc))

    def delete_reserved_public_ip(self, public_ip_id: str) -> OperationResult:
        try:
            ip = self.network.get_public_ip(public_ip_id).data
            if str(getattr(ip, "lifetime", "") or "").upper() != "RESERVED":
                return OperationResult(ok=False, message="仅允许删除保留（RESERVED）公网 IP")
            if _public_ip_busy(ip):
                state = str(getattr(ip, "lifecycle_state", "") or "").upper()
                if state == "ASSIGNING":
                    return OperationResult(
                        ok=False, message="该保留 IP 正在绑定中，请稍候刷新后再操作"
                    )
                return OperationResult(ok=False, message="该保留 IP 仍绑定在实例上，请先解绑")
            self.network.delete_public_ip(public_ip_id)
            return OperationResult(ok=True, message=f"已删除保留 IP {getattr(ip, 'ip_address', '')}")
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            if _public_ip_busy(target):
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
            return OperationResult(ok=False, message=safe_error_text(exc))

    def detach_reserved_public_ip(self, public_ip_id: str) -> OperationResult:
        """Unassign a reserved public IP (the address stays reserved for reuse)."""
        try:
            ip = self.network.get_public_ip(public_ip_id).data
            if str(getattr(ip, "lifetime", "") or "").upper() != "RESERVED":
                return OperationResult(ok=False, message="仅支持解绑保留（RESERVED）公网 IP")
            if not _public_ip_busy(ip):
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
                self.blockstorage.list_boot_volume_backups, **kwargs,
                retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            return OperationResult(ok=False, message=safe_error_text(exc))

    def delete_boot_volume_backup(self, backup_id: str) -> OperationResult:
        try:
            self.blockstorage.delete_boot_volume_backup(backup_id)
            return OperationResult(ok=True, message="已删除引导卷备份")
        except ServiceError as exc:
            return OperationResult(ok=False, message=_format_service_error(exc))
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=safe_error_text(exc))

    def _boot_volume_attachments(self, volume_id: str, availability_domain: str, compartment_id: str) -> list:
        """Live (non-detached) attachments for a boot volume.

        Separate from the block-volume path: boot volumes have their own
        attachment API (`list_boot_volume_attachments`), and it takes the AD
        positionally-first, unlike `list_volume_attachments`. Getting that wrong
        is not loud — it raises TypeError, which an `except Exception` around the
        guard would swallow, leaving "still attached?" silently answered "no".
        That exact mistake is recorded in delete_block_volume above.
        """
        # 分页读：服务端会把 DETACHED 的历史附件一起返回，附件多的卷第一页装不下，
        # 未分页时可能漏掉真正挂着的那条。位置参数顺序是
        # list_boot_volume_attachments(availability_domain, compartment_id, ...)。
        atts = oci.pagination.list_call_get_all_results(
            self.compute.list_boot_volume_attachments,
            availability_domain,
            compartment_id,
            boot_volume_id=volume_id,
            retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
                        retry_strategy=sdk_bounded_paged_retry_strategy(),
                    )
                    for vol in resp.data or []:
                        vid = getattr(vol, "id", "") or ""
                        if not vid or vid in seen:
                            continue
                        state = str(getattr(vol, "lifecycle_state", "") or "")
                        if state in {"TERMINATED", "TERMINATING"}:
                            continue
                        seen.add(vid)
                        vpu = _vpus_or_default(getattr(vol, "vpus_per_gb", None))
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
                        retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            return OperationResult(ok=False, message="性能必须为 0（低成本）、10、20 或 30–120 VPUs/GB")
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
                    # 分页读，理由同上。注意 list_volume_attachments 的
                    # availability_domain 是**关键字**参数，位置参数只有 compartment_id
                    # —— 这一点文件里已有注释警告过，别改坏。
                    atts = oci.pagination.list_call_get_all_results(
                        self.compute.list_volume_attachments,
                        cid,
                        availability_domain=ad,
                        volume_id=volume_id,
                        retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
        if vpus_per_gb is not None and vpus_per_gb not in (0, 10, 20) and not 30 <= int(vpus_per_gb) <= 120:
            return OperationResult(ok=False, message="性能必须为 0（低成本）、10、20 或 30–120 VPUs/GB")
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

    def list_volume_attachments(self, instance_id: str, compartment_id: str = "") -> OperationResult:
        instance_id = (instance_id or "").strip()
        if not instance_id:
            return OperationResult(ok=False, message="缺少 instance_id")
        try:
            inst = self.compute.get_instance(instance_id).data
            ad = getattr(inst, "availability_domain", "") or ""
            cid = (compartment_id or getattr(inst, "compartment_id", "") or self.resolve_compartment()).strip()
            atts = oci.pagination.list_call_get_all_results(
                self.compute.list_volume_attachments,
                cid,
                availability_domain=ad,
                instance_id=instance_id,
                retry_strategy=sdk_bounded_paged_retry_strategy(),
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
                retry_strategy=sdk_bounded_paged_retry_strategy(),
            )
            items = []
            for b in resp.data or []:
                items.append(
                    {
                        "name": getattr(b, "name", "") or "",
                        "namespace": namespace,
                        "compartment_id": getattr(b, "compartment_id", "") or cid,
                        "time_created": self._ts_iso(getattr(b, "time_created", None)),
                        # None，不是 ""。
                        #
                        # ListBuckets 返回的是 BucketSummary，它**根本没有**
                        # public_access_type 这个字段（实测 hasattr 为 False；
                        # docstring 也写着 "A BucketSummary contains only summary
                        # fields for the bucket"）。只有单个 GetBucket 才有。
                        # 原来取到空串、前端再 `|| 'NoPublicAccess'` 兜底，于是
                        # **每一个桶都被断言成「不公开」** —— 包括真正对公网开放读取
                        # 的那些。断言的方向还恰好是让人放心的那一边。
                        # 返回 None，让前端渲染成「未知」而不是替 Oracle 下结论。
                        "public_access_type": None,
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
                # 别把原因咬定成「非空」。文档列出的删桶前置条件有三条：
                # 桶内仍有对象、有未完成的分段上传（multipart upload）、
                # 或存在预验证请求（PAR）。后两种删对象是解决不了的，
                # 而祈使句「请先删除对象」会让人反复删一个已经空了的桶。
                msg = (
                    "存储桶无法删除。OCI 要求桶内没有对象、没有未完成的分段上传、"
                    "也没有预验证请求（PAR）—— 三者任一存在都会被拒。"
                    f"原始错误：{msg}"
                )
            return OperationResult(ok=False, message=msg)
        except Exception as exc:  # noqa: BLE001
            return OperationResult(ok=False, message=safe_error_text(exc))

    def list_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        limit: int = 200,
        namespace: str = "",
        start: str = "",
    ) -> OperationResult:
        """列桶内对象。

        ListObjects **不用**标准的 opc-next-page 分页，用的是 `start` /
        `nextStartWith` 这一对。以前 next_start_with 被算出来并塞进返回值里，
        但没有任何一处能把它传回来 —— 于是永远只看得到第一页，一个有几千个对象的
        桶在界面上就是「只有这 200 个」，而且没有任何地方说它被截断了。
        """
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
            if start:
                kwargs["start"] = start
            resp = self.object_storage.list_objects(namespace, bucket, **kwargs)
            data = resp.data
            objects = []
            for obj in getattr(data, "objects", None) or []:
                size = int(getattr(obj, "size", 0) or 0)
                objects.append(
                    {
                        "name": getattr(obj, "name", "") or "",
                        "size": size,
                        # 十进制 GB：和 FREE_OBJECT_STORAGE_GB(20) 的口径一致，
                        # 也和 Oracle 控制台/账单一致。见 get_network_egress_usage。
                        "size_gb": round(size / (1000**3), 6),
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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
            return OperationResult(ok=False, message=safe_error_text(exc))

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
                size_gb = round(size_bytes / (1000**3), 4)
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
            total_gb = round(total_bytes / (1000**3), 4)
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
                message=safe_error_text(exc),
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
                # account_tier 必须在键里。
                #
                # 会话把 self.tenant 冻结住,而 get_free_quota_usage 从那份冻结的
                # 副本读 tier 并写进快照。tier 不在键里 = 改了等级不重建会话 =
                # 快照里一直是旧等级。两个方向都会出事:
                #   free -> paid:付费租户被当免费的硬拦,抢机任务被判成
                #                enabled=False/failed(不是重试,是永久停止);
                #   paid -> free:降级/试用到期后守卫**静默放行**超额创建。
                tenant.account_tier or "",
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


def is_transient_error(exc: Any = None, text: str = "") -> bool:
    """区分「等一会儿再试就好」和「配置错了，再试一万次也一样」。

    抢机任务的错误分类以前只有两档：容量错误 → 退避重试，**其他一律永久失败**
    （enabled=False、状态 failed、不再调度）。于是一次 DNS 抖动、一次 TLS 握手
    超时、Oracle 侧一个 503，都会把一个准备跑一整夜的任务在第一次抖动时杀死 ——
    操作员早上看到的是「❌ 遇到非容量错误」，机器一台没有，而错误本身早就过去了。

    这里只认那些**明确**属于传输层/服务端临时故障的形态；认不出来的仍然按永久
    错误处理（配置错误必须停下来，而不是拿着错参数无限重发）。
    """
    status = getattr(exc, "status", None) if exc is not None else None
    try:
        # 500/502/503/504：Oracle 自己的服务端故障或网关问题，重试是正确响应。
        # 注意 500 InternalError 里带 "capacity" 的那种由 is_capacity_error 先接走。
        if int(status) in (500, 502, 503, 504):
            return True
    except (TypeError, ValueError):
        pass
    if exc is not None and isinstance(
        exc, (TimeoutError, ConnectionError, socket.timeout, socket.gaierror)
    ):
        return True
    code = str(getattr(exc, "code", "") or "").lower() if exc is not None else ""
    blob = f"{code} {getattr(exc, 'message', '') if exc is not None else ''} {text}".lower()
    return any(
        k in blob
        for k in (
            "[500]",
            "[502]",
            "[503]",
            "[504]",
            "internalservererror",
            "serviceunavailable",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "connection reset",
            "connection aborted",
            "connection refused",
            "connectionerror",
            "read timed out",
            "readtimeout",
            "connecttimeout",
            "timed out",
            "temporary failure in name resolution",
            "name or service not known",
            "eof occurred in violation of protocol",
            "remote end closed connection",
            "max retries exceeded",
        )
    )


def _format_service_error(exc: ServiceError) -> str:
    code = getattr(exc, "code", "") or ""
    status = getattr(exc, "status", "") or ""
    message = getattr(exc, "message", "") or str(exc)
    parts = [p for p in [f"[{status}]", code, message] if p]
    text = " ".join(parts)
    # 把「是哪一次调用失败的」写进错误本身。
    #
    # 这条以前只带 opc-request-id。用户报「实例详情页点刷新报 404」时,那串文本里
    # 没有操作名、没有服务端点,面板自己也不知道是 GetInstance 挂了还是监控挂了 ——
    # 只能靠人去反推调用链。而 operation_name / request_endpoint 本来就在
    # ServiceError 上放着,一直被丢掉。
    #
    # 端点只取主机名,绝不取原文:request_endpoint 里带着未脱敏的完整 compartment
    # OCID(见 _endpoint_host)。主机名本身恰恰是诊断价值最高的一段 ——
    # iaas.* 是 Compute,identity.* 是 Identity,telemetry.* 是监控,
    # 一眼就能把问题切到某个服务上。
    #
    # opc-request-id 是开工单时 Oracle **唯一**认的东西,而且它只存在于这一次响应里 ——
    # 丢掉之后就再也找不回来。
    _rid = str(getattr(exc, "request_id", "") or "")
    _op = str(getattr(exc, "operation_name", "") or "")
    _host = _endpoint_host(exc)
    _tail = [b for b in (_op, f"@ {_host}" if _host else "", f"opc-request-id: {_rid}" if _rid else "") if b]
    if _tail:
        text += " (" + ", ".join(_tail) + ")"
    # 服务端留痕。异常永远不能因为记日志而变形,所以整段吞掉自身的错误。
    #
    # 限流和容量不足降到 INFO:抢机重试循环每 60 秒就会撞一次「容量不足」,那是**预期
    # 内**的循环状态,不是故障。按 WARNING 记会把日志淹掉,而日志一旦变成噪音,
    # 真正要查的那条 404 就又找不到了 —— 正是这次要修的毛病。
    try:
        _expected = is_rate_limit_error(exc) or is_capacity_error(exc)
        _OCI_LOG.log(
            logging.INFO if _expected else logging.WARNING,
            "OCI 调用失败 %s",
            _err_facts(exc),
        )
    except Exception:  # noqa: BLE001
        pass
    # Friendly hints
    #
    # 判断顺序很重要，而且以前是错的：`NotAuthorizedOrNotFound` 里含
    # "notauthorized"，所以它总是先命中「请检查 API Key」那一条，永远走不到
    # 下面那个正确得多的 404 分支。
    #
    # 这不是措辞问题，是把人指向错误的排查方向：`NotAuthorizedOrNotFound` 是
    # Oracle 故意做成模糊的 404，意思是「没权限 **或** 不存在」，谈的是 IAM 策略
    # 的作用范围、compartment 或区域选错了 —— 几乎从来不是密钥本身的问题。而且
    # 「测试连接」通过恰恰证明了密钥是好的，于是操作者被要求去检查一个刚刚验证过
    # 没问题的东西，重新生成密钥也不会有任何变化。
    low = text.lower()
    code_low = code.lower()
    if "notauthorizedornotfound" in code_low or (status == 404 and "notauthorized" in low):
        text += (
            # 纯文本，不要 markdown 星号：这段会直接显示在面板的错误条里，
            # `**` 会原样出现。
            "\n提示：这不是密钥错误。Oracle 用同一个错误码表示「没有权限」和"
            "「资源不存在」。常见原因："
            "\n  1) 该用户的 IAM 策略没有覆盖这个 Compartment（最常见）；"
            "\n  2) 租户里配置的 Compartment OCID 填错，或资源其实在别的 Compartment；"
            "\n  3) 资源在另一个区域。"
            "\n  4) Oracle 侧的瞬时故障 —— 这个码**不保证**是永久的，重读一次就好的情况确实存在。"
            "\n可在租户页点「测试连接」：它会分别探 Compute 和 Identity、区分 read / inspect 两种 verb，"
            "并对 404 自动重读一次，用来判断这次到底是瞬时还是持续。"
        )
    elif "not authenticated" in low or status == 401:
        # 401 才是真正的凭据问题：签名没通过。
        text += "\n提示：签名校验失败，请检查 API Key、Fingerprint、Tenancy/User OCID 是否匹配。"
    elif status == 403 or "notallowed" in code_low:
        text += "\n提示：凭据有效，但该用户没有执行此操作的权限（IAM 策略缺少对应的 verb）。"
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
