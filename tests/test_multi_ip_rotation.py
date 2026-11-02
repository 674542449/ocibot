"""多出口 IP：一键更换公网 IP（私网 IP 不变）。

Oracle 侧是一个有状态的假 VirtualNetworkClient：保留 IP 删除后要经过 TERMINATING
才到 TERMINATED，TERMINATING 的仍然占名额；新 IP 先 ASSIGNING 再 ASSIGNED，
也可以按 UpdatePublicIp 文档说的那样悄悄变回 AVAILABLE。名额上限真的会拒绝申请。
面板每查一次保留 IP 列表，状态往前走一步。时间用假时钟，sleep 不真的等。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from oci.exceptions import ServiceError

from app.oci_client import PrimaryNetworkInfo, TenantSession

BUSY = ("ASSIGNING", "ASSIGNED")


class _Oracle:
    def __init__(self, privs, ips, *, limit=50, release_rounds=2, assign_rounds=1, addresses=None):
        self.privs = list(privs)
        self.ips = {ip["id"]: dict(ip) for ip in ips}
        self.limit = limit
        self.release_rounds = release_rounds
        self.assign_rounds = assign_rounds
        self.addresses = iter(addresses or [f"9.9.9.{n}" for n in range(1, 200)])
        self.calls: list[str] = []
        self.created: list = []
        self.create_errors: dict[str, list] = {}
        self.delete_errors: dict[str, list] = {}
        self.silent_fail: dict[str, int] = {}
        self.stuck: set[str] = set()
        self.terminating_at_create: list[bool] = []
        self.private_ip_writes = 0
        self.list_errors = 0  # 接下来这么多次列表调用直接失败
        self.rebind_lag = 0  # 重绑之后这么多轮列表里仍显示 AVAILABLE
        self.delete_lag = 0  # 删除之后这么多轮列表里仍显示「好好绑着」
        self.hijack: dict[str, str] = {}  # 新 IP 绑好时被别处拿走：私网 IP → 实际绑到的
        self._n = 0

    # ---- VirtualNetworkClient 的那几个方法
    def list_private_ips(self, vnic_id=None, **_kw):
        return SimpleNamespace(data=list(self.privs), has_next_page=False, next_page=None, headers={})

    def create_private_ip(self, *_a, **_k):
        self.private_ip_writes += 1
        raise AssertionError("更换公网 IP 不该新建私网 IP")

    def delete_private_ip(self, *_a, **_k):
        self.private_ip_writes += 1
        raise AssertionError("更换公网 IP 不该删除私网 IP")

    def delete_public_ip(self, public_ip_id):
        self.calls.append(f"delete:{public_ip_id}")
        queue = self.delete_errors.get(public_ip_id)
        err = queue.pop(0) if queue else None
        if err is not None and not isinstance(err, tuple):
            raise err
        ip = self.ips.get(public_ip_id)
        if ip is None or ip["lifecycle_state"] == "TERMINATED":
            raise ServiceError(404, "NotAuthorizedOrNotFound", {}, "gone")
        ip.update(lifecycle_state="TERMINATING", assigned_to="", timer=self.release_rounds,
                  _ghost=self.delete_lag, _was=ip["assigned_to"])
        if err is not None:  # ("after", exc)：Oracle 其实删了，只是响应没回来
            raise err[1]

    def _counted(self):
        return sum(1 for ip in self.ips.values() if ip["lifecycle_state"] != "TERMINATED")

    def _create(self, details):
        self._n += 1
        pid = f"new{self._n}"
        self.ips[pid] = {
            "id": pid,
            "ip_address": next(self.addresses),
            "display_name": details.display_name,
            "lifecycle_state": "ASSIGNING",
            "assigned_to": details.private_ip_id,
            "compartment_id": details.compartment_id,
            "public_ip_pool_id": details.public_ip_pool_id or "",
            "timer": self.assign_rounds,
        }
        self.created.append(details)
        return SimpleNamespace(id=pid, ip_address=self.ips[pid]["ip_address"])

    def create_public_ip(self, details):
        assert details.lifetime == "RESERVED"
        self.calls.append(f"create:{details.private_ip_id}")
        self.terminating_at_create.append(
            any(ip["lifecycle_state"] == "TERMINATING" for ip in self.ips.values())
        )
        queue = self.create_errors.get(details.private_ip_id)
        if queue:
            err = queue.pop(0)
            if isinstance(err, tuple):  # ("after", exc)：Oracle 其实建好了，只是响应出错
                self._create(details)
                raise err[1]
            raise err
        if self._counted() >= self.limit:
            raise ServiceError(400, "LimitExceeded", {}, "reserved-public-ip-count limit exceeded")
        if any(ip["assigned_to"] == details.private_ip_id and ip["lifecycle_state"] in BUSY
               for ip in self.ips.values()):
            raise ServiceError(409, "Conflict", {}, "private IP already has a public IP")
        return SimpleNamespace(data=self._create(details))

    def update_public_ip(self, public_ip_id, details):
        self.calls.append(f"update:{public_ip_id}")
        ip = self.ips[public_ip_id]
        if self.rebind_lag:
            ip.update(assigned_to=details.private_ip_id, _lag=self.rebind_lag)
        else:
            ip.update(assigned_to=details.private_ip_id, lifecycle_state="ASSIGNING", timer=self.assign_rounds)
        return SimpleNamespace(data=SimpleNamespace(id=public_ip_id))

    def get_public_ip_by_private_ip_id(self, details):
        for ip in self.ips.values():
            if ip["assigned_to"] == details.private_ip_id and ip["lifecycle_state"] in BUSY:
                return SimpleNamespace(data=SimpleNamespace(
                    id=ip["id"], ip_address=ip["ip_address"], display_name=ip["display_name"],
                    lifecycle_state=ip["lifecycle_state"], lifetime="RESERVED",
                    compartment_id=ip["compartment_id"], public_ip_pool_id=ip["public_ip_pool_id"],
                ))
        raise ServiceError(404, "NotAuthorizedOrNotFound", {}, "no public IP")

    # ---- TenantSession.list_reserved_public_ips 的替身
    def list_reserved(self, compartment_id=None):
        # 和真的一样：不给区间就是默认区间（resolve_compartment），不是全部区间。
        compartment_id = compartment_id or "comp"
        self.calls.append("list")
        if self.list_errors:
            self.list_errors -= 1
            raise ConnectionError("read timed out")
        self._tick()
        out = []
        for ip in self.ips.values():
            if ip["lifecycle_state"] == "TERMINATED":
                continue
            if compartment_id and ip["compartment_id"] != compartment_id:
                continue
            state, target = ip["lifecycle_state"], ip["assigned_to"]
            if ip.get("_ghost"):  # 列表还没跟上：删了的仍显示绑着
                state, target = "ASSIGNED", ip["_was"]
            out.append({
                "id": ip["id"], "ip_address": ip["ip_address"], "display_name": ip["display_name"],
                "lifecycle_state": state, "assigned": state in BUSY,
                "private_ip_id": target, "compartment_id": ip["compartment_id"],
                "public_ip_pool_id": ip["public_ip_pool_id"],
            })
        return out

    def _tick(self):
        for ip in self.ips.values():
            if ip.get("_lag"):
                ip["_lag"] -= 1
                if not ip["_lag"]:
                    ip.update(lifecycle_state="ASSIGNING", timer=self.assign_rounds)
                continue
            if ip.get("_ghost"):
                ip["_ghost"] -= 1
                continue
            state = ip["lifecycle_state"]
            if state not in ("TERMINATING", "ASSIGNING") or ip["id"] in self.stuck:
                continue
            if ip["timer"] > 0:
                ip["timer"] -= 1
                continue
            if state == "TERMINATING":
                ip["lifecycle_state"] = "TERMINATED"
            elif self.silent_fail.get(ip["assigned_to"], 0) > 0:
                self.silent_fail[ip["assigned_to"]] -= 1
                ip.update(lifecycle_state="AVAILABLE", assigned_to="")
            else:
                ip["lifecycle_state"] = "ASSIGNED"
                ip["assigned_to"] = self.hijack.get(ip["assigned_to"], ip["assigned_to"])

    # ---- 断言用
    def bound(self):
        return {ip["assigned_to"]: ip for ip in self.ips.values() if ip["lifecycle_state"] == "ASSIGNED"}

    def writes(self):
        return [c for c in self.calls if c.split(":")[0] in ("delete", "create", "update")]


def _priv(pid, ip, primary=False):
    return SimpleNamespace(id=pid, ip_address=ip, is_primary=primary, freeform_tags={})


def _oracle(n, *, unbound=(), **kw):
    """主 IP + n 个辅助 IP；第 i 个绑着保留 IP old{i}（1.1.1.i，名字 proxy-0i）。"""
    privs = [_priv("pv-primary", "10.0.0.5", primary=True)] + [
        _priv(f"pv{i}", f"10.0.0.{10 + i}") for i in range(1, n + 1)
    ]
    ips = [
        {"id": f"old{i}", "ip_address": f"1.1.1.{i}", "display_name": f"proxy-0{i}",
         "lifecycle_state": "ASSIGNED", "assigned_to": f"pv{i}", "compartment_id": "comp",
         "public_ip_pool_id": "", "timer": 0}
        for i in range(1, n + 1)
        if i not in unbound
    ]
    return _Oracle(privs, ips, **kw)


def _session(oracle):
    s = TenantSession.__new__(TenantSession)
    s._network = oracle
    s.resolve_primary_network = lambda *_a, **_k: PrimaryNetworkInfo(  # type: ignore[method-assign]
        vnic_id="vnic1", private_ip_id="pv-primary", private_ipv4="10.0.0.5"
    )
    s.resolve_compartment = lambda: "comp"  # type: ignore[method-assign]
    s.list_reserved_public_ips = oracle.list_reserved  # type: ignore[method-assign]
    return s


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch):
    from app import oci_client

    now = [0.0]

    def sleep(sec):
        now[0] += max(float(sec), 0.01)

    monkeypatch.setattr(oci_client, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0], sleep=sleep))
    monkeypatch.setattr("oci.pagination.list_call_get_all_results", lambda fn, **kw: fn(**kw))
    return now


def _rotate(s, ids, **kw):
    plan = s.plan_multi_ip_rotation("inst", "comp", ids, **kw)
    assert plan.ok, plan.message
    return s.rotate_multi_ips(plan.data["items"])


def _by_private(res):
    return {it["private_ip_id"]: it for it in res.data["items"]}


# ---------------------------------------------------------------- 正常路径


def test_with_the_quota_full_everything_is_released_before_anything_is_created():
    """名额正好用满：先建后删一定撞上限，所以要先全部删掉、等真正删除完再申请。"""
    o = _oracle(3, limit=3)
    s = _session(o)
    res = _rotate(s, ["pv1", "pv2", "pv3"])
    assert res.ok, res.message

    writes = o.writes()
    first_create = next(i for i, c in enumerate(writes) if c.startswith("create:"))
    assert writes[:first_create] == ["delete:old1", "delete:old2", "delete:old3"]
    assert o.terminating_at_create and not any(o.terminating_at_create), (
        "TERMINATING 的 IP 还占着名额：要等它们彻底删除才申请"
    )
    assert all(not c.startswith("delete:new") for c in writes)

    bound = o.bound()
    assert set(bound) == {"pv1", "pv2", "pv3"}, "新 IP 绑回原来的私网 IP"
    assert {k: v["display_name"] for k, v in bound.items()} == {
        "pv1": "proxy-01", "pv2": "proxy-02", "pv3": "proxy-03"
    }, "名字沿用旧的"
    assert all(v["ip_address"].startswith("9.9.9.") for v in bound.values())
    assert o.private_ip_writes == 0, "私网 IP 一个都不能动"
    assert [it["status"] for it in res.data["items"]] == ["done"] * 3
    assert "已更换 3 个" in res.message


def test_progress_is_reported_through_every_phase():
    o = _oracle(2)
    phases: list[str] = []
    plan = _session(o).plan_multi_ip_rotation("inst", "comp", ["pv1", "pv2"])
    _session(o).rotate_multi_ips(plan.data["items"], on_progress=lambda phase, _items: phases.append(phase))
    for phase in ("release", "wait_release", "create", "wait_assign"):
        assert phase in phases


def test_a_broken_progress_callback_does_not_stop_the_rotation():
    o = _oracle(1)
    s = _session(o)
    plan = s.plan_multi_ip_rotation("inst", "comp", ["pv1"])

    def broken(_phase, _items):
        raise RuntimeError("db is down")

    res = s.rotate_multi_ips(plan.data["items"], on_progress=broken)
    assert res.ok and set(o.bound()) == {"pv1"}, "旧 IP 已经删了，进度没存上也必须把新 IP 绑上"


def test_a_secondary_ip_without_a_public_ip_just_gets_one():
    o = _oracle(2, unbound=(1,))
    res = _rotate(_session(o), ["pv1", "pv2"])
    assert res.ok, res.message
    assert "delete:old1" not in o.calls
    assert set(o.bound()) == {"pv1", "pv2"}
    assert o.bound()["pv1"]["display_name"] == "ip-10.0.0.11"


def test_the_address_pool_and_compartment_are_kept():
    o = _oracle(1)
    o.ips["old1"].update(public_ip_pool_id="pool-byoip", compartment_id="other")
    res = _rotate(_session(o), ["pv1"])
    assert res.ok, res.message
    (details,) = o.created
    assert details.public_ip_pool_id == "pool-byoip"
    assert details.compartment_id == "other", "旧 IP 在别的区间：默认区间的列表里没有它，要按私网 IP 找到"


# ---------------------------------------------------------------- 选择范围


def test_the_primary_private_ip_is_refused():
    o = _oracle(1)
    res = _session(o).plan_multi_ip_rotation("inst", "comp", ["pv-primary"])
    assert not res.ok and "不是本实例的辅助私网 IP" in res.message
    assert o.writes() == []


def test_an_ip_from_another_instance_is_refused_before_anything_changes():
    o = _oracle(2)
    res = _session(o).plan_multi_ip_rotation("inst", "comp", ["pv1", "pv-elsewhere"])
    assert not res.ok
    assert o.writes() == []


def test_an_empty_selection_is_refused():
    res = _session(_oracle(1)).plan_multi_ip_rotation("inst", "comp", [])
    assert not res.ok


def test_an_ip_that_is_mid_change_is_skipped_and_left_alone():
    o = _oracle(2)
    o.ips["old2"]["lifecycle_state"] = "ASSIGNING"
    o.stuck.add("old2")
    res = _rotate(_session(o), ["pv1", "pv2"])
    assert res.ok, res.message
    assert "delete:old2" not in o.calls
    assert _by_private(res)["pv2"]["status"] == "skipped"
    assert "1 个跳过" in res.message


def test_nothing_to_do_is_reported_instead_of_started():
    o = _oracle(1)
    o.ips["old1"]["lifecycle_state"] = "UNASSIGNING"
    res = _session(o).plan_multi_ip_rotation("inst", "comp", ["pv1"])
    assert not res.ok and "变更中" in res.message


# ---------------------------------------------------------------- Oracle 出状况


def test_limit_exceeded_right_after_the_release_is_retried():
    """名额释放可能有延迟：刚删完就申请会被拒，等一会儿重试。"""
    o = _oracle(1, limit=1)
    o.create_errors["pv1"] = [ServiceError(400, "LimitExceeded", {}, "limit exceeded")] * 2
    res = _rotate(_session(o), ["pv1"])
    assert res.ok, res.message
    assert o.calls.count("create:pv1") == 3


def test_a_throttled_delete_is_retried():
    o = _oracle(1)
    o.delete_errors["old1"] = [ServiceError(429, "TooManyRequests", {}, "slow down")]
    res = _rotate(_session(o), ["pv1"])
    assert res.ok, res.message
    assert o.calls.count("delete:old1") == 2


def test_a_delete_that_fails_leaves_that_ip_alone_and_carries_on():
    o = _oracle(2)
    o.delete_errors["old1"] = [ServiceError(400, "InvalidParameter", {}, "nope")]
    res = _rotate(_session(o), ["pv1", "pv2"])
    assert not res.ok
    items = _by_private(res)
    assert items["pv1"]["status"] == "failed" and "删除旧 IP 失败" in items["pv1"]["message"]
    assert "create:pv1" not in o.calls, "旧 IP 没删掉就申请，只会撞「已有公网 IP」"
    assert items["pv2"]["status"] == "done"


def test_an_assignment_that_silently_fails_is_retried_once():
    o = _oracle(2)
    o.silent_fail["pv2"] = 1
    res = _rotate(_session(o), ["pv1", "pv2"])
    assert res.ok, res.message
    assert set(o.bound()) == {"pv1", "pv2"}
    assert sum(1 for c in o.calls if c.startswith("update:")) == 1


def test_an_assignment_that_keeps_failing_leaves_the_new_ip_in_the_pool():
    o = _oracle(1)
    o.silent_fail["pv1"] = 2
    res = _rotate(_session(o), ["pv1"])
    assert not res.ok
    item = _by_private(res)["pv1"]
    assert item["status"] == "failed" and "留在保留 IP 列表" in item["message"]
    assert not any(c.startswith("delete:new") for c in o.calls), "申请到的新 IP 不能删，用户可以手动绑"


def test_a_create_error_that_actually_succeeded_is_recognised():
    """申请报 5xx，但 Oracle 其实已经建好并绑上：按私网 IP 认回来，不重复申请。"""
    o = _oracle(1)
    o.create_errors["pv1"] = [("after", ServiceError(500, "InternalError", {}, "timeout"))]
    res = _rotate(_session(o), ["pv1"])
    assert res.ok, res.message
    assert o.calls.count("create:pv1") == 1
    assert _by_private(res)["pv1"]["new_ip"] == o.bound()["pv1"]["ip_address"]


def test_a_release_that_never_finishes_does_not_hold_up_the_others(fake_clock):
    o = _oracle(2)
    o.stuck.add("old1")
    s = _session(o)
    s.ROTATE_RELEASE_TIMEOUT = 30  # type: ignore[misc]
    res = _rotate(s, ["pv1", "pv2"])
    assert not res.ok
    items = _by_private(res)
    assert items["pv1"]["status"] == "failed" and "超时" in items["pv1"]["message"]
    assert "create:pv1" not in o.calls
    assert items["pv2"]["status"] == "done"


def test_a_404_on_delete_is_not_taken_as_deleted():
    """Oracle 对没有权限的删除也回 404：旧 IP 还绑着，就不能去申请新的、更不能报「分回原地址」。"""
    o = _oracle(2)
    o.delete_errors["old1"] = [ServiceError(404, "NotAuthorizedOrNotFound", {}, "not authorized")]
    res = _rotate(_session(o), ["pv1", "pv2"])
    assert not res.ok
    items = _by_private(res)
    assert items["pv1"]["status"] == "failed" and "没有被删除" in items["pv1"]["message"]
    assert not items["pv1"].get("same_address")
    assert "create:pv1" not in o.calls
    assert o.bound()["pv1"]["id"] == "old1", "旧 IP 原样留着"
    assert items["pv2"]["status"] == "done"


def test_a_delete_whose_response_was_lost_is_confirmed_from_the_list():
    o = _oracle(1)
    o.delete_errors["old1"] = [("after", ConnectionError("read timed out"))]
    res = _rotate(_session(o), ["pv1"])
    assert res.ok, res.message
    assert o.bound()["pv1"]["id"] != "old1"


def test_a_network_error_on_one_create_does_not_strand_the_rest():
    """旧 IP 已经全删了：一个申请遇到网络异常，其余私网 IP 照样要拿到新 IP。"""
    o = _oracle(3)
    o.create_errors["pv1"] = [ConnectionError("connection reset")]
    res = _rotate(_session(o), ["pv1", "pv2", "pv3"])
    assert not res.ok
    items = _by_private(res)
    assert items["pv1"]["status"] == "failed" and "没有公网 IP" in items["pv1"]["message"]
    assert items["pv2"]["status"] == "done" and items["pv3"]["status"] == "done"


def test_a_list_that_fails_for_a_while_does_not_abort_the_rotation():
    o = _oracle(1)
    s = _session(o)
    plan = s.plan_multi_ip_rotation("inst", "comp", ["pv1"])
    o.list_errors = 3  # 删完之后的几次查询都失败
    res = s.rotate_multi_ips(plan.data["items"])
    assert res.ok, res.message
    assert "pv1" in o.bound()


def test_the_round_right_after_a_rebind_may_still_show_available():
    o = _oracle(1)
    o.silent_fail["pv1"] = 1
    o.rebind_lag = 2  # 重绑后的第一轮列表里仍是 AVAILABLE
    res = _rotate(_session(o), ["pv1"])
    assert res.ok, res.message
    assert "pv1" in o.bound()


def test_a_lost_delete_response_waits_out_a_lagging_list():
    """响应丢了、列表又慢几轮：不能提前判「没删」—— 那样这个私网 IP 最后既没旧 IP 也没新 IP。"""
    o = _oracle(1)
    o.delete_lag = 4
    o.delete_errors["old1"] = [("after", ConnectionError("read timed out"))]
    res = _rotate(_session(o), ["pv1"])
    assert res.ok, res.message
    assert "pv1" in o.bound() and o.bound()["pv1"]["id"] != "old1"


def test_a_new_ip_taken_elsewhere_is_not_reported_as_done():
    """新 IP 被「设为主公网 IP」拿走了：不能算成换好，也不能再把它抢回来。"""
    o = _oracle(1)
    o.hijack["pv1"] = "pv-primary"
    res = _rotate(_session(o), ["pv1"])
    assert not res.ok
    item = _by_private(res)["pv1"]
    assert item["status"] == "failed" and "绑到了别处" in item["message"]
    assert not any(c.startswith("update:") for c in o.calls), "不能把它从主 IP 上挪回来"
    assert o.bound()["pv-primary"]["id"].startswith("new")


def test_internal_bookkeeping_does_not_leak_into_the_result():
    o = _oracle(1)
    o.silent_fail["pv1"] = 1
    o.delete_errors["old1"] = [("after", ConnectionError("lost"))]
    res = _rotate(_session(o), ["pv1"])
    assert not any(k.startswith("_") for it in res.data["items"] for k in it)


def test_the_same_address_coming_back_is_flagged():
    o = _oracle(2, addresses=["9.9.9.1", "1.1.1.2"])
    res = _rotate(_session(o), ["pv1", "pv2"])
    assert res.ok
    item = _by_private(res)["pv2"]
    assert item["same_address"] is True and "原来的地址" in item["message"]
    assert "1 个 Oracle 分回了原地址" in res.message


def test_a_failed_create_can_be_refilled_with_the_original_name():
    """旧 IP 删了、新 IP 没申请到：那个私网 IP 没有公网 IP。再跑一次补上，名字照旧。"""
    from web.backend.ip_rotation import _hints

    o = _oracle(2)
    o.create_errors["pv2"] = [ServiceError(400, "InvalidParameter", {}, "boom")]
    s = _session(o)
    first = _rotate(s, ["pv1", "pv2"])
    assert not first.ok
    assert _by_private(first)["pv2"]["status"] == "failed"
    assert "没有公网 IP" in _by_private(first)["pv2"]["message"]
    assert set(o.bound()) == {"pv1"}

    plan = s.plan_multi_ip_rotation("inst", "comp", ["pv2"], hints=_hints({"items": first.data["items"]}))
    assert plan.ok, plan.message
    (item,) = plan.data["items"]
    assert item["old_ip"] == "" and item["name"] == "proxy-02"
    again = s.rotate_multi_ips(plan.data["items"])
    assert again.ok, again.message
    assert o.bound()["pv2"]["display_name"] == "proxy-02"


# ---------------------------------------------------------------- 后台任务（web/backend/ip_rotation.py）


@pytest.fixture
def job_db(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from web.backend import ip_rotation
    from web.backend.db import Base

    # 文件库、每个会话各自一条连接 —— 和线上一样。内存库 + StaticPool 会让测试线程和
    # 后台线程共用同一条 sqlite 连接，一边的提交 / 回滚打断另一边，整套并行跑时偶发失败。
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'jobs.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, future=True)
    monkeypatch.setattr(ip_rotation, "SessionLocal", maker)

    threads: list[threading.Thread] = []

    class _Recording(threading.Thread):
        def start(self):
            threads.append(self)
            super().start()

    monkeypatch.setattr(ip_rotation, "threading", SimpleNamespace(Thread=_Recording))
    yield maker, threads
    for t in threads:
        t.join(5)
    engine.dispose()


_ITEM = {"private_ip_id": "pv1", "private_ip": "10.0.0.11", "old_public_ip_id": "old1", "old_ip": "1.1.1.1",
         "name": "proxy-01", "compartment_id": "comp", "pool_id": "pool-x", "new_public_ip_id": "",
         "new_ip": "", "status": "pending", "message": ""}


class _FakeSession:
    def __init__(self, gate=None, crash=False):
        self.gate = gate
        self.crash = crash
        self.hints = None

    def plan_multi_ip_rotation(self, instance_id, compartment_id, private_ip_ids, hints=None, expected=None):
        self.hints = hints
        self.expected = expected
        return SimpleNamespace(ok=True, message="", data={"items": [dict(_ITEM)]})

    def rotate_multi_ips(self, items, on_progress=None):
        on_progress("release", [dict(items[0], status="releasing")])
        if self.gate is not None:
            self.gate.wait(5)
        if self.crash:
            raise RuntimeError("boom")
        return SimpleNamespace(ok=True, message="已更换 1 个公网 IP",
                               data={"items": [dict(items[0], status="done", new_ip="9.9.9.1")]})


def _start(db, session):
    from web.backend import ip_rotation

    return ip_rotation.start_rotation(
        db, session=session, owner_id="u1", tenant_id="t1", instance_id="ocid1.instance.oc1..abc",
        compartment_id="comp", private_ip_ids=["pv1"],
    )


def test_the_job_runs_in_the_background_and_one_instance_runs_one_job(job_db):
    from web.backend import ip_rotation
    from web.backend.models import AuditLog

    maker, threads = job_db
    gate = threading.Event()
    with maker() as db:
        out = _start(db, _FakeSession(gate))
        assert out["ok"] and out["job"]["state"] == "running"
        with pytest.raises(ip_rotation.RotationBusy):
            _start(db, _FakeSession())
    gate.set()
    for t in threads:
        t.join(5)
    with maker() as db:
        view = ip_rotation.job_view(ip_rotation.read_job(db, "t1", "ocid1.instance.oc1..abc"))
        assert view["state"] == "done" and view["items"][0]["new_ip"] == "9.9.9.1"
        assert "compartment_id" not in view["items"][0] and "pool_id" not in view["items"][0]
        audit = db.query(AuditLog).filter(AuditLog.action == "multi_ip.rotate_done").one()
        assert "1.1.1.1 → 9.9.9.1" in audit.detail


def test_a_job_whose_process_died_reads_as_interrupted_and_does_not_block(job_db):
    """面板重启后「运行中」的记录没人更新了：不能永远挡住下一次，名字还要沿用。"""
    import json

    from web.backend import ip_rotation
    from web.backend.meta import set_meta

    maker, _threads = job_db
    old = (datetime.now(timezone.utc) - timedelta(seconds=ip_rotation.STALE_SEC + 60)).isoformat()
    with maker() as db:
        set_meta(db, ip_rotation.job_key("t1", "ocid1.instance.oc1..abc"), json.dumps(
            {"state": "running", "phase": "create", "updated_at": old,
             "items": [dict(_ITEM, status="released")]}
        ))
        db.commit()
        view = ip_rotation.job_view(ip_rotation.read_job(db, "t1", "ocid1.instance.oc1..abc"))
        assert view["state"] == "interrupted" and "补上" in view["message"]
        session = _FakeSession()
        assert _start(db, session)["ok"]
        assert session.hints == {"pv1": {"name": "proxy-01", "compartment_id": "comp", "pool_id": "pool-x"}}


def test_a_crash_is_recorded_as_a_failure(job_db):
    from web.backend import ip_rotation

    maker, threads = job_db
    with maker() as db:
        _start(db, _FakeSession(crash=True))
    for t in threads:
        t.join(5)
    with maker() as db:
        view = ip_rotation.job_view(ip_rotation.read_job(db, "t1", "ocid1.instance.oc1..abc"))
    assert view["state"] == "failed" and "更换中断" in view["message"]
    assert view["items"][0]["status"] == "releasing", "进度留在最后一次保存的样子，看得出停在哪"


def test_a_plan_that_fails_starts_nothing(job_db):
    from web.backend import ip_rotation

    maker, threads = job_db

    class _Refuse(_FakeSession):
        def plan_multi_ip_rotation(self, *a, **k):
            return SimpleNamespace(ok=False, message="所选 IP 不是本实例的辅助私网 IP，请刷新后重试", data={})

    with maker() as db:
        out = _start(db, _Refuse())
        assert not out["ok"] and "辅助私网 IP" in out["message"]
        assert ip_rotation.read_job(db, "t1", "ocid1.instance.oc1..abc") is None
    assert threads == []


def test_other_public_ip_changes_are_refused_while_a_rotation_runs(job_db):
    """更换进行中，同一台实例的设为主公网 IP / 多出口绑定 / 解绑都要先挡下（409）。"""
    import json

    from fastapi import HTTPException

    from web.backend import ip_rotation
    from web.backend.meta import set_meta
    from web.backend.routers.instance_ops import _refuse_during_rotation

    maker, _threads = job_db
    key = ip_rotation.job_key("t1", "inst-1")
    with maker() as db:
        _refuse_during_rotation(db, "t1", "inst-1")  # 没有任务：放行
        set_meta(db, key, json.dumps({"state": "running", "updated_at": datetime.now(timezone.utc).isoformat()}))
        db.commit()
        with pytest.raises(HTTPException) as err:
            _refuse_during_rotation(db, "t1", "inst-1")
        assert err.value.status_code == 409
        _refuse_during_rotation(db, "t1", "inst-2")  # 别的实例不受影响
        set_meta(db, key, json.dumps({"state": "done", "updated_at": datetime.now(timezone.utc).isoformat()}))
        db.commit()
        _refuse_during_rotation(db, "t1", "inst-1")


def test_the_guarded_routes_call_the_rotation_check():
    import inspect

    from web.backend.routers import instance_ops

    for fn in (instance_ops.attach_reserved_ip, instance_ops.attach_multi_ips, instance_ops.detach_multi_ips):
        assert "_refuse_during_rotation(db, tenant_id, instance_id)" in inspect.getsource(fn), fn.__name__


def test_nothing_is_deleted_when_the_page_showed_a_different_address():
    """确认框写着「没有公网 IP、直接分配」，Oracle 那边其实绑着一个：不能把它删了。"""
    o = _oracle(2)
    res = _session(o).plan_multi_ip_rotation(
        "inst", "comp", ["pv1", "pv2"], expected={"pv1": "", "pv2": "1.1.1.2"}
    )
    assert not res.ok
    assert "列表已经过期" in res.message and "1.1.1.1" in res.message and "10.0.0.11" in res.message
    assert o.writes() == []


def test_a_stale_address_on_the_page_is_refused_too():
    o = _oracle(1)
    res = _session(o).plan_multi_ip_rotation("inst", "comp", ["pv1"], expected={"pv1": "8.8.8.8"})
    assert not res.ok and "8.8.8.8" in res.message


def test_matching_addresses_go_ahead():
    o = _oracle(2, unbound=(2,))
    res = _rotate(_session(o), ["pv1", "pv2"], expected={"pv1": "1.1.1.1", "pv2": ""})
    assert res.ok, res.message
    assert set(o.bound()) == {"pv1", "pv2"}


def test_the_multi_ip_list_shows_a_public_ip_from_another_compartment():
    """默认区间的列表里没有它；列表上显示成「没有」，确认框就会说错。"""
    o = _oracle(2, unbound=(2,))
    o.ips["old1"]["compartment_id"] = "other"
    res = _session(o).list_multi_ips("inst", "comp")
    assert res.ok, res.message
    rows = {i["private_ip_id"]: i for i in res.data["items"]}
    assert rows["pv1"]["public_ip"] == "1.1.1.1" and rows["pv1"]["public_ip_name"] == "proxy-01"
    assert rows["pv2"]["public_ip"] == ""


def test_the_start_passes_what_the_page_showed_to_the_plan(job_db):
    maker, _threads = job_db
    session = _FakeSession()
    from web.backend import ip_rotation

    with maker() as db:
        ip_rotation.start_rotation(
            db, session=session, owner_id="u1", tenant_id="t1", instance_id="ocid1.instance.oc1..abc",
            compartment_id="comp", private_ip_ids=["pv1"], expected={"pv1": "1.1.1.1"},
        )
    assert session.expected == {"pv1": "1.1.1.1"}


def test_the_route_refuses_an_empty_selection():
    from pydantic import ValidationError

    from web.backend.routers.instance_ops import MultiIpRotate

    with pytest.raises(ValidationError):
        MultiIpRotate(private_ip_ids=[])
