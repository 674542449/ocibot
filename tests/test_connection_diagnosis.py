"""「测试连接」的诊断不能断言它无法知道的原因。

用户报的真实故障:一个满权限的账号,点「测试连接」**有时**通过、有时报

    凭据有效(用户 …),但对 Compartment k2wjnmitzo4po5wa 的读取权限不足:
      列出实例:NotAuthorizedOrNotFound
    这是 IAM 策略范围的问题,不是密钥的问题 —— 重新生成密钥不会有帮助。

那句结论出自一行注释:「Credentials are good past this point; anything below is a
policy/scope problem」。它是错的:get_user 成功之后,探针仍然可能因为限流(429)、
Oracle 5xx、网络超时、熔断器打开而失败,这些都不是策略问题。而且

  * 旧探针表是 {ListInstances, ListCompartments(ACCESSIBLE), ListAvailabilityDomains}。
    后两个需要的是**同一个**权限 COMPARTMENT_INSPECT;而 ListCompartments 用了
    access_level="ACCESSIBLE",那是个过滤器 —— 零权限时返回空页 + 200,结构上
    不可能失败。所以「2 通过 1 失败」推不出「compartment 读不了」。
  * ListInstances 需要的是 INSTANCE_READ,不是 INSTANCE_INSPECT。一条只写了
    `inspect instance-family` 的策略会**永久**产生一模一样的 1 挂 2 通。

下面钉住的就是:面板只陈述事实 + 给排查顺序,不下结论;并且任何一条诊断文案里
都不能出现完整 OCID。
"""

from __future__ import annotations

import pytest

from app.oci_client import _endpoint_host, _err_facts, _format_probe_report

oci = pytest.importorskip("oci")
from oci.exceptions import ServiceError  # noqa: E402

_COMPARTMENT = "ocid1.compartment.oc1..aaaaaaaak2wjnmitzo4po5wa"


def _ok(label, service, required=False, ms=200):
    return {
        "label": label, "service": service, "verb": "v", "required": required,
        "ok": True, "elapsed_ms": ms,
    }


def _fail(label, service, *, required=False, status=404, code="NotAuthorizedOrNotFound",
          retried_ok=None, request_id="rid-1", host="iaas.us-phoenix-1.oraclecloud.com",
          type_="ServiceError"):
    return {
        "label": label, "service": service, "verb": "v", "required": required,
        "ok": False, "elapsed_ms": 430, "retried_ok": retried_ok, "type": type_,
        "status": status, "code": code, "request_id": request_id,
        "operation": "list_instances", "host": host,
    }


def _report(results):
    return _format_probe_report("u@example.com", "T", "us-phoenix-1", _COMPARTMENT, results)


# ------------------------------------------------------------------ 不再断言


def test_the_report_never_asserts_that_it_is_a_policy_problem():
    """这是本次修复的核心。旧文案里那两句话是断言,而面板没有能力做出这个断言。"""
    text = _report([_fail("列出实例", "Compute", required=True), _ok("列出可用域", "Identity")])

    assert "这是 IAM 策略范围的问题" not in text
    assert "读取权限不足" not in text
    assert "重新生成密钥不会有帮助" not in text
    # 取而代之的是把歧义说清楚。
    assert "面板无法替它区分" in text


def test_a_successful_re_read_kills_the_policy_explanation():
    """同一个调用 1.5 秒后就通了 —— 那它按定义就是瞬时的。

    这种时候还建议人家去改 IAM 策略,就是把他推向一条本来没问题的策略,
    正是这次要修掉的毛病。
    """
    text = _report([
        _fail("列出实例", "Compute", required=True, retried_ok=True),
        _ok("列出规格", "Compute"),
        _ok("读取 Compartment", "Identity"),
        _ok("列出可用域", "Identity"),
    ])

    assert "瞬时" in text
    assert "read instance-family" not in text, "重读成功之后不该再提策略"
    assert "服务端点" not in text, "重读成功之后不该再提区域/端点"


def test_inspect_versus_read_is_called_out_when_shapes_pass_but_instances_fail():
    """这两个调用走同一个服务、同一个 compartment,差别只在 verb ——
    是全部证据里指向性最强的一种形态。"""
    text = _report([
        _fail("列出实例", "Compute", required=True, retried_ok=False),
        _ok("列出规格", "Compute"),
        _ok("读取 Compartment", "Identity"),
        _ok("列出可用域", "Identity"),
    ])

    assert "read instance-family" in text
    assert "inspect" in text


def test_a_whole_service_failing_points_at_the_endpoint_not_the_compartment():
    text = _report([
        _fail("列出实例", "Compute", required=True, retried_ok=False),
        _fail("列出规格", "Compute", retried_ok=False),
        _ok("读取 Compartment", "Identity"),
        _ok("列出可用域", "Identity"),
    ])

    assert "服务端点" in text
    assert "iaas" in text and "identity" in text
    assert "副区" in text


def test_throttling_is_named_as_throttling():
    text = _report([_fail("列出实例", "Compute", required=True, status=429, code="TooManyRequests")])
    assert "限流" in text
    assert "不是权限问题" in text


def test_a_circuit_breaker_error_is_not_a_permission_problem():
    """熔断器是 SDK 客户端侧的保护,连续失败后暂停发请求、约 30 秒自愈。
    Identity 等五个 client 默认带它,Compute 不带 —— 所以它确实会出现。"""
    # 现实形态:Identity 的熔断器打开的同时,Compute 的必需探针也在失败。
    # (只有 Identity 侧那五个 client 默认带熔断器,Compute 不带 —— 见
    #  TenantSession._build:六个 client 都只传 retry_strategy。)
    text = _report([
        _fail("列出实例", "Compute", required=True, retried_ok=False),
        _fail("列出可用域", "Identity", status=0, code="", type_="CircuitBreakerError",
              host="identity.us-phoenix-1.oci.oraclecloud.com"),
    ])
    assert "熔断" in text
    assert "与权限无关" in text


def test_a_passing_required_probe_means_success_even_if_extras_failed():
    """必需的那一条(列出实例)通过 = 面板的主功能可用。

    旧逻辑是「任一探针失败即报连接失败」,而旧表里有一个探针结构上不可能失败、
    另外两个测的是同一件事 —— 那种判定既不敏感也不特异。现在只看必需的那条,
    其余作为上下文报出来。
    """
    text = _report([
        _ok("列出实例", "Compute", required=True),
        _fail("列出可用域", "Identity", status=0, code="", type_="CircuitBreakerError"),
    ])
    assert text.startswith("连接成功")
    assert "非必需" in text


# ------------------------------------------------------------------ 编号 / 渲染


def test_the_checklist_is_numbered_consecutively():
    """条目是按本次证据动态挑的,编号必须跟着重排。

    写死前缀的话,只命中第 2、3 条时会渲染出「2) … 2)」这种东西。
    """
    text = _report([
        _fail("列出实例", "Compute", required=True, retried_ok=False),
        _ok("列出规格", "Compute"),
    ])
    numbers = [ln.strip()[0] for ln in text.splitlines() if ln.strip()[:2] in {"1)", "2)", "3)", "4)", "5)"}]
    assert numbers == [str(i) for i in range(1, len(numbers) + 1)], text


def test_the_report_is_plain_text_not_markdown():
    """错误条是纯文本渲染,`**` 会原样显示出来 ——
    _format_service_error 里早有一条同样的注释。"""
    text = _report([
        _fail("列出实例", "Compute", required=True, retried_ok=True),
        _fail("列出规格", "Compute", retried_ok=False),
        _fail("读取 Compartment", "Identity", status=429, code="TooManyRequests"),
    ])
    assert "**" not in text


# ------------------------------------------------------------------ 不泄露


def test_no_full_ocid_ever_reaches_the_message():
    """面板到处只显示 compartment 的后 16 位,这是刻意的约定。"""
    text = _report([_fail("列出实例", "Compute", required=True)])

    assert "ocid1." not in text
    assert _COMPARTMENT not in text
    assert "k2wjnmitzo4po5wa" in text, "尾部 16 位仍要显示，否则无法区分租户行"


def test_endpoint_host_keeps_only_the_host():
    """ServiceError.request_endpoint 的原文里带**完整** compartment OCID,
    而 SDK 的 redact_sensitive_string_for_logs 只脱敏凭据头、不脱敏 compartmentId。
    原样打印等于把 OCID 送进前端和日志。"""

    class _Exc:
        request_endpoint = (
            "GET https://iaas.us-phoenix-1.oraclecloud.com/20160918/instances"
            "?compartmentId=ocid1.compartment.oc1..aaaaaaaasecret&limit=1"
        )

    host = _endpoint_host(_Exc())

    assert host == "iaas.us-phoenix-1.oraclecloud.com"
    assert "ocid1." not in host
    assert "?" not in host


def test_endpoint_host_survives_a_missing_or_odd_field():
    assert _endpoint_host(object()) == ""

    class _Empty:
        request_endpoint = None

    assert _endpoint_host(_Empty()) == ""


# ------------------------------------------------------------------ 事实提取


def test_err_facts_keeps_the_opc_request_id():
    """opc-request-id 是开工单时 Oracle 唯一认的东西,而且只存在于那一次响应里。
    旧代码只留了 `code or status`,把它连同状态码一起丢掉了。"""
    exc = ServiceError(404, "NotAuthorizedOrNotFound", {"opc-request-id": "abc/def/ghi"}, "nope")

    facts = _err_facts(exc)

    assert facts["status"] == 404
    assert facts["code"] == "NotAuthorizedOrNotFound"
    assert facts["request_id"] == "abc/def/ghi"
    assert facts["type"] == "ServiceError"


def test_err_facts_never_raises_on_a_plain_exception():
    """任何异常都要能压成事实集合 —— 诊断路径本身不能再抛一次。"""
    facts = _err_facts(ValueError("boom"))

    assert facts["type"] == "ValueError"
    assert facts["status"] == 0
    assert facts["code"] == ""


def test_err_facts_carries_nothing_secret():
    exc = ServiceError(404, "NotAuthorizedOrNotFound", {}, "nope")
    exc.request_endpoint = (
        "GET https://iaas.us-phoenix-1.oraclecloud.com/20160918/instances"
        "?compartmentId=ocid1.compartment.oc1..aaaaaaaasecret"
    )

    blob = repr(_err_facts(exc))

    assert "ocid1." not in blob
    assert "compartmentId" not in blob
