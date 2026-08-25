"""容量雷达：CreateComputeCapacityReport 的只读探测。

这个功能的全部价值建立在一句话上：**「读不到」和「没有容量」是两件事。**
把它们混成一件,面板就会让人放弃一台其实开得出来的机器 —— 而这正是本仓库
0.4.84/0.4.85 已经犯过一次的错(预检比服务端严格,缺权限的租户从 UI 上永久无法
创建实例)。所以下面绝大多数断言都是在钉「失败必须降级成 unknown,而不是冒充无货,
更不能抛异常」。
"""

from __future__ import annotations

import pytest

from app.oci_client import (
    CAPACITY_STATUS_MAP,
    RADAR_AVAILABLE,
    RADAR_NOT_SUPPORTED,
    RADAR_OUT_OF_CAPACITY,
    RADAR_UNKNOWN,
)
from web.backend.capacity_radar import (
    RADAR_SHAPE,
    clear_radar_cache,
    probe_capacity,
    radar_error_hint,
)

oci = pytest.importorskip("oci")
from oci.exceptions import ServiceError  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_radar_cache()
    yield
    clear_radar_cache()


# --------------------------------------------------------------- 枚举映射


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("AVAILABLE", RADAR_AVAILABLE),
        ("OUT_OF_HOST_CAPACITY", RADAR_OUT_OF_CAPACITY),
        ("HARDWARE_NOT_SUPPORTED", RADAR_NOT_SUPPORTED),
        # SDK 把**任何**它不认识的服务端取值映射成这个字面量字符串，而不是抛异常。
        # 不处理的话面板上会渲染出一个原始英文枚举串。
        ("UNKNOWN_ENUM_VALUE", RADAR_UNKNOWN),
        # 字段本身还可能是 None。
        ("", RADAR_UNKNOWN),
        ("SOMETHING_ORACLE_ADDED_LATER", RADAR_UNKNOWN),
    ],
)
def test_every_status_maps_including_the_ones_oracle_has_not_invented_yet(raw, expect):
    assert CAPACITY_STATUS_MAP.get(raw, RADAR_UNKNOWN) == expect


# --------------------------------------------------------------- 桩


class _Result:
    def __init__(self, ok, message="", data=None):
        self.ok = ok
        self.message = message
        self.data = data if data is not None else {}


class _Tenant:
    region = "ap-tokyo-1"
    compartment_ocid = "ocid1.compartment.oc1..child"
    tenancy_ocid = "ocid1.tenancy.oc1..root"


class _Session:
    """只桩 get_capacity_report —— 和 tests/test_endpoint_smoke.py 的会话桩同构。"""

    def __init__(self, per_ad):
        self.tenant = _Tenant()
        self._per_ad = per_ad
        self.calls: list[tuple] = []

    def get_capacity_report(self, ad, shape, configs, **kw):
        self.calls.append((ad, shape, tuple(configs)))
        out = self._per_ad[ad]
        if isinstance(out, Exception):
            raise out
        return out


def _ok(rows):
    return _Result(True, "", {"compartment_id": "c", "used_root_compartment": False, "rows": rows})


def _row(status, fd="FAULT-DOMAIN-1", ocpus=4.0, memory=24.0, count=None):
    return {
        "fault_domain": fd,
        "ocpus": ocpus,
        "memory_in_gbs": memory,
        "available_count": count,
        "status": status,
    }


def _probe(session, ads=("AD-1",), configs=((4.0, 24.0),)):
    return probe_capacity(
        session,
        tenant_id="t1",
        shape=RADAR_SHAPE,
        configs=list(configs),
        availability_domains=list(ads),
    )


# --------------------------------------------------------------- 响应形状


def test_ad_level_summary_row_with_no_fault_domain_does_not_crash():
    """留空 fault_domain 时 Oracle 可能按 FD 逐行返回，也可能只给一行 AD 级汇总。

    实测 CapacityReportShapeAvailability().fault_domain 默认就是 None，所以代码
    不能假设任何一种形状。AD 级汇总行不进 fault_domains 列表(前端据此不渲染 FD 芯片)，
    但它的 status 必须照常参与汇总。
    """
    s = _Session({"AD-1": _ok([_row(RADAR_AVAILABLE, fd=None)])})

    out = _probe(s)

    assert out["results"][0]["status"] == RADAR_AVAILABLE
    assert out["results"][0]["configs"][0]["fault_domains"] == []


def test_available_count_none_stays_none_and_never_becomes_zero():
    """普通公有云租户这里**恒为 None**(只有 DRCC / 白名单租户拿得到数字)。

    折成 0 会渲染成「可开 0 台」，和「无货」长得一模一样 —— 而它的真实含义是
    「有货，但 Oracle 不告诉你还剩几台」。方向完全相反。
    """
    s = _Session({"AD-1": _ok([_row(RADAR_AVAILABLE, count=None)])})

    cfg = _probe(s)["results"][0]["configs"][0]

    assert cfg["status"] == RADAR_AVAILABLE
    assert cfg["available_count"] is None


def test_a_real_count_is_passed_through_when_oracle_does_give_one():
    s = _Session({"AD-1": _ok([_row(RADAR_AVAILABLE, count=7)])})
    assert _probe(s)["results"][0]["configs"][0]["available_count"] == 7


def test_any_fault_domain_with_stock_makes_the_config_available():
    """FD 之间是「或」的关系：一个 FD 有货就开得出来。

    本项目创建实例时从不指定 fault_domain(SAFE_LAUNCH_FIELDS 里没有这个字段)，
    Oracle 会自己挑一个 —— 所以按最差的 FD 下结论会平白劝退用户。
    """
    s = _Session(
        {
            "AD-1": _ok(
                [
                    _row(RADAR_OUT_OF_CAPACITY, fd="FAULT-DOMAIN-1"),
                    _row(RADAR_AVAILABLE, fd="FAULT-DOMAIN-2"),
                    _row(RADAR_OUT_OF_CAPACITY, fd="FAULT-DOMAIN-3"),
                ]
            )
        }
    )

    out = _probe(s)["results"][0]

    assert out["status"] == RADAR_AVAILABLE
    assert len(out["configs"][0]["fault_domains"]) == 3


# --------------------------------------------------------------- 汇总口径


def test_the_ad_verdict_comes_from_the_primary_config_not_the_best_one():
    """AD 的结论取**主配置**，不是所有配置里最好的那个。

    否则「4C24G 无货、1C6G 有货」会渲染成一个绿色的 AD —— 而用户要的是 4C24G，
    他会照着这个绿标去创建，然后拿到 OutOfHostCapacity。
    """
    s = _Session(
        {
            "AD-1": _ok(
                [
                    _row(RADAR_OUT_OF_CAPACITY, ocpus=4.0, memory=24.0),
                    _row(RADAR_AVAILABLE, ocpus=1.0, memory=6.0),
                ]
            )
        }
    )

    out = _probe(s, configs=((4.0, 24.0), (1.0, 6.0)))["results"][0]

    assert out["status"] == RADAR_OUT_OF_CAPACITY, "主配置无货时 AD 不能报绿"
    assert out["configs"][0]["primary"] is True
    assert out["configs"][0]["status"] == RADAR_OUT_OF_CAPACITY
    # 备用配置的结论照常返回 —— UI 可以据此建议「降到 1C6G 试试」。
    assert out["configs"][1]["primary"] is False
    assert out["configs"][1]["status"] == RADAR_AVAILABLE


def test_overall_takes_the_best_ad():
    s = _Session(
        {
            "AD-1": _ok([_row(RADAR_OUT_OF_CAPACITY)]),
            "AD-2": _ok([_row(RADAR_AVAILABLE)]),
            "AD-3": _ok([_row(RADAR_NOT_SUPPORTED)]),
        }
    )
    assert _probe(s, ads=("AD-1", "AD-2", "AD-3"))["overall"] == RADAR_AVAILABLE


def test_results_keep_the_requested_ad_order_not_the_completion_order():
    """并行探测的完成顺序是不确定的。按完成顺序返回的话，同一次探测每重跑一遍
    卡片就换个位置，看起来像数据在跳。"""
    s = _Session({f"AD-{i}": _ok([_row(RADAR_AVAILABLE)]) for i in (1, 2, 3)})

    out = _probe(s, ads=("AD-3", "AD-1", "AD-2"))

    assert [r["availability_domain"] for r in out["results"]] == ["AD-3", "AD-1", "AD-2"]


# --------------------------------------------------------------- 失败降级


def test_one_failing_ad_does_not_discard_the_other_two():
    s = _Session(
        {
            "AD-1": _ok([_row(RADAR_AVAILABLE)]),
            "AD-2": RuntimeError("boom"),
            "AD-3": _ok([_row(RADAR_OUT_OF_CAPACITY)]),
        }
    )

    out = _probe(s, ads=("AD-1", "AD-2", "AD-3"))

    by_ad = {r["availability_domain"]: r for r in out["results"]}
    assert by_ad["AD-1"]["status"] == RADAR_AVAILABLE
    assert by_ad["AD-3"]["status"] == RADAR_OUT_OF_CAPACITY
    assert by_ad["AD-2"]["status"] == RADAR_UNKNOWN
    assert by_ad["AD-2"]["reason"], "失败的 AD 必须说明为什么"


def test_a_permission_failure_is_unknown_not_out_of_capacity():
    """这是本功能最重要的一条断言。

    CreateComputeCapacityReport 需要一条和 LaunchInstance **完全不相交**的 IAM
    授权(manage compute-capacity-reports)，「能创建实例但调不了报告」是常见配置。
    把它报成「无货」会让一个完全正常的租户永远看到红灯。
    """
    s = _Session(
        {"AD-1": _Result(False, "[404] NotAuthorizedOrNotFound", {"status": 404})}
    )

    out = _probe(s)["results"][0]

    assert out["status"] == RADAR_UNKNOWN
    assert out["status"] != RADAR_OUT_OF_CAPACITY
    assert "compute-capacity-reports" in out["reason"], "必须直接给出要加的那条 IAM 策略"


def test_the_permission_hint_does_not_send_the_operator_to_test_connection():
    """通用的 404 诊断会让人去点「测试连接」—— 而 test_connection 的三个探针
    (list_instances / list_compartments / list_availability_domains)在缺
    manage compute-capacity-reports 时会**全部通过**。把人指向一个刚验证过没问题的
    东西，正是 _format_service_error 那段注释当初为了消除而写的体验。"""
    hint = radar_error_hint("[404] NotAuthorizedOrNotFound", 404)

    assert "测试连接" not in hint or "并不能说明" in hint
    assert "Fingerprint" not in hint
    assert "compute-capacity-reports" in hint


def test_throttling_is_unknown_and_says_so():
    s = _Session({"AD-1": _Result(False, "[429] TooManyRequests", {"status": 429})})

    out = _probe(s)["results"][0]

    assert out["status"] == RADAR_UNKNOWN
    assert "429" in out["reason"] or "限流" in out["reason"]


def test_probe_never_raises_whatever_the_session_does():
    """quota_guard 那边的写法：返回一个「读不全」的结论，而不是抛异常。

    抛出去的话，创建页的预检会整块失败 —— 而预检失败**不该**阻断创建。
    """

    class _Exploding:
        tenant = _Tenant()

        def get_capacity_report(self, *a, **kw):
            raise ValueError("something nobody anticipated")

    out = _probe(_Exploding())
    assert out["results"][0]["status"] == RADAR_UNKNOWN


def test_no_availability_domains_is_a_message_not_a_crash():
    out = probe_capacity(
        _Session({}), tenant_id="t1", shape=RADAR_SHAPE, configs=[(4.0, 24.0)],
        availability_domains=[],
    )
    assert out["ok"] is False
    assert out["results"] == []
    assert "加载配置" in out["message"]


# --------------------------------------------------------------- 缓存隔离


def test_the_cache_is_keyed_on_the_tenant_so_one_account_cannot_see_anothers():
    """**本功能唯一一处真正的多租户隔离点。**

    容量报告是 Oracle 针对**调用方凭据**给出的答案。键里漏掉 tenant，A 用户的
    结论就会渲染到 B 的面板上。
    """
    s = _Session({"AD-1": _ok([_row(RADAR_AVAILABLE)])})
    probe_capacity(s, tenant_id="tenant-A", shape=RADAR_SHAPE, configs=[(4.0, 24.0)],
                   availability_domains=["AD-1"])
    assert len(s.calls) == 1

    probe_capacity(s, tenant_id="tenant-B", shape=RADAR_SHAPE, configs=[(4.0, 24.0)],
                   availability_domains=["AD-1"])
    assert len(s.calls) == 2, "另一个租户必须重新问 Oracle，不能复用缓存"


def test_a_repeat_probe_within_the_ttl_hits_the_cache():
    """连点防线。限流器是进程内的(AUDIT 已接受的缺口，多 worker 下会被放大)，
    真正对连点免疫的是这层缓存。"""
    s = _Session({"AD-1": _ok([_row(RADAR_AVAILABLE)])})

    first = _probe(s)
    second = _probe(s)

    assert len(s.calls) == 1, "同一份参数在 TTL 内不该再问一次 Oracle"
    assert first["results"][0]["cached"] is False
    assert second["results"][0]["cached"] is True


def test_changing_the_shape_config_is_a_different_cache_entry():
    """报告本来就是按 shape config 出的 —— 规格变了结论就可能变，不能复用。"""
    s = _Session({"AD-1": _ok([_row(RADAR_AVAILABLE)])})

    _probe(s, configs=((4.0, 24.0),))
    _probe(s, configs=((1.0, 6.0),))

    assert len(s.calls) == 2


def test_a_failed_probe_is_not_cached():
    """失败缓存 60 秒 = 一次限流让用户在整整一分钟里都看不到真实结论。"""
    s = _Session({"AD-1": _Result(False, "[429] TooManyRequests", {"status": 429})})

    _probe(s)
    _probe(s)

    assert len(s.calls) == 2


# ------------------------------------------------- SDK 调用层（get_capacity_report）

from types import SimpleNamespace  # noqa: E402

from app.oci_client import TenantSession  # noqa: E402


class _FakeCompute:
    """记下每次调用的 details 和 kwargs，并按脚本抛错或返回。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[tuple] = []

    def create_compute_capacity_report(self, details, **kwargs):
        self.calls.append((details, kwargs))
        outcome = self._script.pop(0) if self._script else None
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(data=SimpleNamespace(shape_availabilities=outcome or []))


def _sdk_session(script, *, compartment="ocid1.compartment.oc1..child"):
    s = TenantSession.__new__(TenantSession)
    s.tenant = SimpleNamespace(
        region="ap-tokyo-1",
        compartment_ocid=compartment,
        tenancy_ocid="ocid1.tenancy.oc1..root",
        name="T",
    )
    s._compute = _FakeCompute(script)
    return s


def _avail(status="AVAILABLE", fd="FAULT-DOMAIN-1", ocpus=4.0, memory=24.0, count=None):
    return SimpleNamespace(
        fault_domain=fd,
        availability_status=status,
        available_count=count,
        instance_shape_config=SimpleNamespace(ocpus=ocpus, memory_in_gbs=memory),
    )


def test_the_probe_disables_sdk_retries():
    """不关掉的话一次被限流的探测最多变成 8 个 HTTP 请求，并把调用线程占住 600 秒。

    本项目所有路由都是同步 def，每个请求占一个 anyio 线程(默认 40 × 2 个 worker
    = 80 个槽)，卡住几十个就足以让整个面板看起来像死了。LaunchInstance 出于同样的
    理由早就这么做了。
    """
    s = _sdk_session([[_avail()]])

    s.get_capacity_report("AD-1", RADAR_SHAPE, [(4.0, 24.0)])

    _details, kwargs = s._compute.calls[0]
    assert isinstance(kwargs.get("retry_strategy"), oci.retry.NoneRetryStrategy)


def test_the_request_sets_neither_fault_domain_nor_burstable_fields():
    """三个字段都不能设：

    * fault_domain —— 本项目创建实例时从不指定故障域，按 FD 提问会问出一个系统
      给不了的选择；留空还能让 Oracle 自己决定返回 FD 明细还是 AD 级汇总。
    * baseline_ocpu_utilization —— A1 不是 burstable 机型，而这是个枚举字段，
      给一个 SDK 不认识的值会被静默序列化成字符串 "UNKNOWN_ENUM_VALUE" 发给 Oracle。
    * nvmes —— A1.Flex 没有本地 NVMe。
    """
    s = _sdk_session([[_avail()]])

    s.get_capacity_report("AD-1", RADAR_SHAPE, [(4.0, 24.0)])

    details, _kwargs = s._compute.calls[0]
    entry = details.shape_availabilities[0]
    assert entry.fault_domain is None
    assert entry.instance_shape == RADAR_SHAPE
    assert entry.instance_shape_config.ocpus == 4.0
    assert entry.instance_shape_config.memory_in_gbs == 24.0
    assert entry.instance_shape_config.baseline_ocpu_utilization is None
    assert entry.instance_shape_config.nvmes is None


def test_all_configs_ride_one_request_per_ad():
    """shape_availabilities 是个**列表**：主配置 + 全部备用配置是一次请求的事。

    成本 = AD 数，不是 AD 数 × 配置数。按配置逐个发请求会把一次三 AD 的探测从
    3 个请求变成 9 个 —— 花在同一个 per-tenancy 速率桶上。
    """
    s = _sdk_session([[_avail()]])

    s.get_capacity_report("AD-1", RADAR_SHAPE, [(4.0, 24.0), (2.0, 12.0), (1.0, 6.0)])

    assert len(s._compute.calls) == 1
    details, _ = s._compute.calls[0]
    assert len(details.shape_availabilities) == 3


def test_a_404_falls_back_to_the_root_compartment_once():
    """SDK 的字段 docstring 说 compartment "should always be the root compartment"，
    但 list_availability_domains 上面那段注释记着相反的坑：一个 IAM 只覆盖子
    compartment 的密钥，问根会拿到 NotAuthorizedOrNotFound。两类租户都要能用，
    所以先问配置的那个、只在 404/403 上回退一次根。
    """
    s = _sdk_session([ServiceError(404, "NotAuthorizedOrNotFound", {}, "nope"), [_avail()]])

    result = s.get_capacity_report("AD-1", RADAR_SHAPE, [(4.0, 24.0)])

    assert result.ok is True
    assert len(s._compute.calls) == 2
    assert s._compute.calls[0][0].compartment_id == "ocid1.compartment.oc1..child"
    assert s._compute.calls[1][0].compartment_id == "ocid1.tenancy.oc1..root"
    assert result.data["used_root_compartment"] is True


def test_a_429_does_not_fall_back():
    """被限流时再发一次只会让情况更糟 —— 一次限流变成两个请求。"""
    s = _sdk_session([ServiceError(429, "TooManyRequests", {}, "slow down")])

    result = s.get_capacity_report("AD-1", RADAR_SHAPE, [(4.0, 24.0)])

    assert result.ok is False
    assert len(s._compute.calls) == 1, "429 绝不回退"
    assert result.data["status"] == 429


def test_no_fallback_when_the_tenant_has_no_separate_compartment():
    """compartment 没配时它本来就等于 tenancy 根，没有第二个目标可试。"""
    s = _sdk_session([ServiceError(404, "NotAuthorizedOrNotFound", {}, "nope")], compartment="")

    result = s.get_capacity_report("AD-1", RADAR_SHAPE, [(4.0, 24.0)])

    assert result.ok is False
    assert len(s._compute.calls) == 1


def test_unknown_enum_from_the_wire_becomes_unknown_not_a_raw_string():
    s = _sdk_session([[_avail(status="UNKNOWN_ENUM_VALUE")]])
    rows = s.get_capacity_report("AD-1", RADAR_SHAPE, [(4.0, 24.0)]).data["rows"]
    assert rows[0]["status"] == RADAR_UNKNOWN


def test_a_missing_shape_config_echo_does_not_drop_the_row():
    """Oracle 不回显 instance_shape_config 时也不能把这一行丢掉 ——
    丢掉就等于把「有货」悄悄变成「没有结论」。"""
    s = _sdk_session([[_avail(ocpus=None, memory=None)]])
    rows = s.get_capacity_report("AD-1", RADAR_SHAPE, [(4.0, 24.0)]).data["rows"]
    assert len(rows) == 1
    assert rows[0]["ocpus"] is None


# ------------------------------------------------- 路由层：雷达绝不能触发写操作

def test_the_radar_route_never_calls_fetch_launch_meta():
    """源码断言。fetch_launch_meta 的冷调用里有

        f_network = pool.submit(session.ensure_default_network,
                                compartment_id=..., create_if_missing=True)

    它会在租户里**创建 VCN、子网、网关和路由表**并等它们变可用，自己的 docstring
    写着「A minute or more is normal」。

    0.4.88 首版为了拿一份可用域列表就调了它，后果有两层：
      1. 用户在雷达页(从没点过「加载配置」，缓存是冷的)点探测，请求要跑一分钟以上，
         浏览器/反代先超时 —— 表现就是「探测没结果」；
      2. 一个从页面副标题到 CHANGELOG 都写着「只读，绝不创建任何实例」的功能，
         会创建网络资源。

    用源码断言而不是行为断言：真正的危险是**有人以后为了图省事又把它加回来**，
    而那条路径在测试里是被桩掉的，行为断言看不见。
    """
    import inspect

    from web.backend.routers import instances as inst

    src = inspect.getsource(inst.capacity_report)

    # 匹配**调用**(带左括号)而不是名字：路由里那段解释为什么不能用它的注释
    # 本身就提到了这个名字，按名字匹配会被自己的注释绊倒。
    assert "fetch_launch_meta(" not in src, (
        "雷达路由不能调 fetch_launch_meta() —— 它的冷调用会 "
        "ensure_default_network(create_if_missing=True)，在租户里真的建 VCN"
    )
    assert "peek_launch_meta(" in src, "应当先看缓存(零 Oracle 请求)"
    assert "list_availability_domains(" in src, "缓存没命中时单发一次纯读调用"


def test_the_radar_route_takes_no_launch_lock():
    """雷达什么都不改，进 tenant_launch_lock 只会让探测把别人的创建堵住。
    tests/test_launch_lock_scope.py 也钉着锁内的调用序列。"""
    import inspect

    from web.backend.routers import instances as inst

    # 同上：按调用而不是按名字匹配。
    assert "tenant_launch_lock(" not in inspect.getsource(inst.capacity_report)
