"""多出口 IP：批量保留 IP、多个保留 IP 绑到一台实例、系统侧同步脚本。

Oracle 侧用一个假的 VirtualNetworkClient 驱动 TenantSession 的真实方法；
同步脚本（跑在实例上的那段 Python）把 IMDS / ip 命令换成假的，跑它真实的 main()。
"""

from __future__ import annotations

import subprocess
import shutil
from types import SimpleNamespace

import pytest
from oci.exceptions import ServiceError

from app.oci_client import PrimaryNetworkInfo, TenantSession


# ---------------------------------------------------------------- Oracle 侧


class _FakeNet:
    def __init__(self, privs=None, fail_bind_for=None):
        self.privs = list(privs or [])
        self.fail_bind_for = fail_bind_for
        self.bound: dict[str, str] = {}
        self.deleted: list[str] = []
        self._n = 0

    def list_private_ips(self, vnic_id=None, **_kw):
        return SimpleNamespace(data=list(self.privs), has_next_page=False, next_page=None, headers={})

    def create_private_ip(self, details):
        self._n += 1
        p = SimpleNamespace(
            id=f"pv-new-{self._n}",
            ip_address=f"10.0.0.{100 + self._n}",
            is_primary=False,
            freeform_tags=dict(details.freeform_tags or {}),
        )
        self.privs.append(p)
        return SimpleNamespace(data=p)

    def update_public_ip(self, public_ip_id, details):
        if public_ip_id == self.fail_bind_for:
            raise ServiceError(400, "InvalidParameter", {}, "boom")
        self.bound[public_ip_id] = details.private_ip_id
        return SimpleNamespace(data=SimpleNamespace(id=public_ip_id))

    def delete_private_ip(self, private_ip_id):
        self.deleted.append(private_ip_id)
        self.privs = [p for p in self.privs if p.id != private_ip_id]


def _priv(pid, ip, primary=False, managed=False):
    return SimpleNamespace(
        id=pid, ip_address=ip, is_primary=primary,
        freeform_tags={TenantSession.MULTI_IP_TAG: "1"} if managed else {},
    )


def _session(net, reserved):
    s = TenantSession.__new__(TenantSession)
    s._network = net
    s.resolve_primary_network = lambda *_a, **_k: PrimaryNetworkInfo(  # type: ignore[method-assign]
        vnic_id="vnic1", private_ip_id="pv-primary", private_ipv4="10.0.0.5"
    )
    s.list_reserved_public_ips = lambda *_a, **_k: [dict(r) for r in reserved]  # type: ignore[method-assign]
    return s


def _reserved(rid, ip, assigned=False, private_ip_id="", name=""):
    return {"id": rid, "ip_address": ip, "assigned": assigned,
            "private_ip_id": private_ip_id, "display_name": name}


def test_attach_creates_one_tagged_secondary_ip_per_reserved_ip(monkeypatch):
    monkeypatch.setattr("oci.pagination.list_call_get_all_results", lambda fn, **kw: fn(**kw))
    net = _FakeNet(privs=[_priv("pv-primary", "10.0.0.5", primary=True)])
    s = _session(net, [_reserved("pip1", "1.1.1.1"), _reserved("pip2", "2.2.2.2")])
    res = s.attach_multi_ips("inst", "comp", ["pip1", "pip2", "pip1"])  # 重复的只算一次
    assert res.ok, res.message
    assert set(net.bound) == {"pip1", "pip2"}
    new = [p for p in net.privs if not p.is_primary]
    assert len(new) == 2
    assert all(p.freeform_tags == {TenantSession.MULTI_IP_TAG: "1"} for p in new)


def test_attach_checks_everything_before_the_first_write(monkeypatch):
    """所选里有一个已经绑在别处：一个都不能动。"""
    monkeypatch.setattr("oci.pagination.list_call_get_all_results", lambda fn, **kw: fn(**kw))
    net = _FakeNet(privs=[_priv("pv-primary", "10.0.0.5", primary=True)])
    s = _session(net, [_reserved("pip1", "1.1.1.1"), _reserved("pip2", "2.2.2.2", assigned=True)])
    res = s.attach_multi_ips("inst", "comp", ["pip1", "pip2"])
    assert not res.ok and "2.2.2.2" in res.message
    assert net.bound == {} and len(net.privs) == 1


def test_attach_refuses_more_than_the_vnic_can_hold(monkeypatch):
    monkeypatch.setattr("oci.pagination.list_call_get_all_results", lambda fn, **kw: fn(**kw))
    privs = [_priv("pv-primary", "10.0.0.5", primary=True)] + [
        _priv(f"pv{i}", f"10.0.1.{i}") for i in range(63)
    ]
    net = _FakeNet(privs=privs)
    s = _session(net, [_reserved("pip1", "1.1.1.1"), _reserved("pip2", "2.2.2.2")])
    res = s.attach_multi_ips("inst", "comp", ["pip1", "pip2"])
    assert not res.ok and "还能加 1" in res.message
    assert net.bound == {}


def test_a_failed_bind_does_not_leave_an_orphan_private_ip(monkeypatch):
    monkeypatch.setattr("oci.pagination.list_call_get_all_results", lambda fn, **kw: fn(**kw))
    net = _FakeNet(privs=[_priv("pv-primary", "10.0.0.5", primary=True)], fail_bind_for="pip2")
    s = _session(net, [_reserved("pip1", "1.1.1.1"), _reserved("pip2", "2.2.2.2"),
                       _reserved("pip3", "3.3.3.3")])
    res = s.attach_multi_ips("inst", "comp", ["pip1", "pip2", "pip3"])
    assert not res.ok and "1/3" in res.message
    assert set(net.bound) == {"pip1"}, "第一个失败后应停下，pip3 不该再绑"
    assert len(net.deleted) == 1, "绑定失败的那个辅助私网 IP 要删掉"
    assert len([p for p in net.privs if not p.is_primary]) == 1


def test_detach_unbinds_and_deletes_only_panel_made_ips(monkeypatch):
    monkeypatch.setattr("oci.pagination.list_call_get_all_results", lambda fn, **kw: fn(**kw))
    net = _FakeNet(privs=[
        _priv("pv-primary", "10.0.0.5", primary=True),
        _priv("pv1", "10.0.0.11", managed=True),
        _priv("pv-hand", "10.0.0.12"),  # 用户在控制台手工建的
    ])
    s = _session(net, [_reserved("pip1", "1.1.1.1", assigned=True, private_ip_id="pv1")])

    refused = s.detach_multi_ips("inst", "comp", ["pv1", "pv-hand"])
    assert not refused.ok and "不是面板创建" in refused.message
    assert net.deleted == [] and net.bound == {}, "预检失败时一个都不能动"

    res = s.detach_multi_ips("inst", "comp", ["pv1"])
    assert res.ok, res.message
    assert net.bound == {"pip1": ""}, "保留 IP 要先解绑（地址保留）"
    assert net.deleted == ["pv1"]


def test_detach_refuses_the_primary_ip(monkeypatch):
    monkeypatch.setattr("oci.pagination.list_call_get_all_results", lambda fn, **kw: fn(**kw))
    net = _FakeNet(privs=[_priv("pv-primary", "10.0.0.5", primary=True)])
    res = _session(net, []).detach_multi_ips("inst", "comp", ["pv-primary"])
    assert not res.ok and net.deleted == []


def test_batch_create_continues_numbering_and_stops_at_the_limit():
    calls: list[str] = []

    def create(_comp=None, display_name=""):
        calls.append(display_name)
        if len(calls) == 3:
            return SimpleNamespace(ok=False, message="LimitExceeded: reserved public IP limit", data={})
        return SimpleNamespace(ok=True, message="", data={"public_ip_id": f"id{len(calls)}",
                                                          "ip_address": f"9.9.9.{len(calls)}"})

    s = _session(_FakeNet(), [_reserved("a", "1.1.1.1", name="proxy-01"),
                              _reserved("b", "1.1.1.2", name="proxy-07"),
                              _reserved("c", "1.1.1.3", name="other")])
    s.create_reserved_public_ip = create  # type: ignore[method-assign]
    res = s.create_reserved_public_ips(5, "proxy")
    assert calls == ["proxy-08", "proxy-09", "proxy-10"], "接着已有的最大编号往下数"
    assert not res.ok
    assert "2/5" in res.message and "LimitExceeded" in res.message
    assert [c["display_name"] for c in res.data["created"]] == ["proxy-08", "proxy-09"]


# ---------------------------------------------------------------- 实例上的同步脚本


def _load_sync(tmp_path):
    from app import ip_sync

    src = ip_sync._SYNC_PY.strip()
    body, _, last = src.rpartition("\n")
    assert last.startswith("sys.exit(main(")
    ns: dict = {"__name__": "ocibot_ip_sync_test"}
    exec(compile(body, "ocibot-ip-sync", "exec"), ns)
    ns["STATE_DIR"] = str(tmp_path / "state")
    return ns


def _wire(ns, vnics, have, cmds, panel=None):
    ns["read_vnics"] = vnics if callable(vnics) else (lambda: vnics)
    ns["read_panel_list"] = panel if callable(panel) else (lambda: panel)
    ns["interfaces_by_mac"] = lambda: {"02:00:17:00:00:01": "enp0s6"}
    ns["current_ipv4"] = lambda dev: set(have)

    def ip_cmd(*args):
        cmds.append(" ".join(args))
        ip = args[2].split("/")[0]
        (have.add if args[1] == "add" else have.discard)(ip)
        return True

    ns["ip_cmd"] = ip_cmd


def _vnic(secondary):
    return [{"macAddr": "02:00:17:00:00:01", "privateIp": "10.0.0.5",
             "subnetCidrBlock": "10.0.0.0/24", "secondaryPrivateIps": secondary}]


def test_sync_adds_missing_ips_and_removes_only_its_own(tmp_path):
    ns = _load_sync(tmp_path)
    have = {"10.0.0.5", "10.0.0.50"}  # .50 是用户自己写进 netplan 的
    cmds: list[str] = []
    _wire(ns, _vnic(["10.0.0.11", "10.0.0.12", "10.0.0.50"]), have, cmds)
    ns["main"]()
    assert cmds == ["addr add 10.0.0.11/24 dev enp0s6", "addr add 10.0.0.12/24 dev enp0s6"]

    # Oracle 那边解绑了 .12 和 .50：只删自己加的 .12，不碰用户的 .50
    cmds.clear()
    _wire(ns, _vnic(["10.0.0.11"]), have, cmds)
    ns["main"]()
    assert cmds == ["addr del 10.0.0.12/24 dev enp0s6"]
    assert "10.0.0.50" in have and "10.0.0.5" in have


def test_sync_changes_nothing_when_metadata_cannot_be_read(tmp_path):
    ns = _load_sync(tmp_path)
    have = {"10.0.0.5"}
    cmds: list[str] = []
    _wire(ns, _vnic(["10.0.0.11"]), have, cmds)
    ns["main"]()
    assert cmds == ["addr add 10.0.0.11/24 dev enp0s6"]

    def broken():
        raise OSError("timed out")

    cmds.clear()
    _wire(ns, broken, have, cmds)
    assert ns["main"]() == 0
    assert cmds == [], "读不到元数据不等于「一个辅助 IP 都没有」"

    # 响应里压根没有那个键，同理不能当成空列表
    _wire(ns, [{"macAddr": "02:00:17:00:00:01", "privateIp": "10.0.0.5",
                "subnetCidrBlock": "10.0.0.0/24"}], have, cmds)
    ns["main"]()
    assert cmds == []
    assert "10.0.0.11" in have


# 用户实例上 /opc/v2/vnics/ 的真实返回（2026-10，ap-singapore-1）：
# 根本没有 secondaryPrivateIps —— 0.4.129 因此一个 IP 都没加上。
_REAL_VNICS = [{
    "ipv6SubnetCidrBlock": "2603:c024:4520:a600::/64",
    "macAddr": "02:00:17:00:00:01",
    "privateIp": "10.0.0.129",
    "subnetCidrBlock": "10.0.0.0/24",
    "virtualRouterIp": "10.0.0.1",
    "vlanTag": 577,
    "vnicId": "ocid1.vnic.oc1..v1",
}]


def test_sync_uses_the_list_the_panel_wrote_into_instance_metadata(tmp_path):
    ns = _load_sync(tmp_path)
    have = {"10.0.0.129"}
    cmds: list[str] = []
    _wire(ns, _REAL_VNICS, have, cmds, panel={"ocid1.vnic.oc1..v1": ["10.0.0.11", "10.0.0.12"]})
    ns["main"]()
    assert cmds == ["addr add 10.0.0.11/24 dev enp0s6", "addr add 10.0.0.12/24 dev enp0s6"]

    # 全部解绑：面板写的是空列表（不是删掉键），于是自己加的都要拿掉
    cmds.clear()
    _wire(ns, _REAL_VNICS, have, cmds, panel={"ocid1.vnic.oc1..v1": []})
    ns["main"]()
    assert sorted(cmds) == ["addr del 10.0.0.11/24 dev enp0s6", "addr del 10.0.0.12/24 dev enp0s6"]
    assert have == {"10.0.0.129"}


def test_no_panel_list_and_no_vnics_field_changes_nothing(tmp_path):
    """真实的 IMDS 返回 + 面板还没写过元数据：不能当成「一个都不要」去删。"""
    ns = _load_sync(tmp_path)
    have = {"10.0.0.129", "10.0.0.11"}
    cmds: list[str] = []
    _wire(ns, _REAL_VNICS, have, cmds, panel={"ocid1.vnic.oc1..v1": ["10.0.0.11"]})
    ns["main"]()  # .11 已经在网卡上（手工加的），不重复加、也不收编
    _wire(ns, _REAL_VNICS, have, cmds, panel=None)
    ns["main"]()
    assert cmds == [] and "10.0.0.11" in have


def test_install_mode_waits_for_the_panel_list_to_show_up(tmp_path):
    ns = _load_sync(tmp_path)
    have = {"10.0.0.129"}
    cmds: list[str] = []
    answers = [None, None, {"ocid1.vnic.oc1..v1": ["10.0.0.11"]}]
    _wire(ns, _REAL_VNICS, have, cmds, panel=lambda: answers.pop(0))
    ns["time"] = SimpleNamespace(time=__import__("time").time, sleep=lambda _s: None)
    ns["main"](wait=60)
    assert cmds == ["addr add 10.0.0.11/24 dev enp0s6"]
    assert answers == []


# ---------------------------------------------------------------- 写实例元数据


class _FakeCompute:
    def __init__(self, metadata):
        self.metadata = dict(metadata)
        self.updates: list = []

    def get_instance(self, instance_id):
        return SimpleNamespace(data=SimpleNamespace(metadata=dict(self.metadata)), headers={"etag": "e1"})

    def update_instance(self, instance_id, details, **kw):
        self.updates.append((details.metadata, kw))
        self.metadata = dict(details.metadata)


def test_publishing_keeps_every_existing_metadata_key(monkeypatch):
    """UpdateInstance 的 metadata 是整体替换；user_data / ssh_authorized_keys 必须原样带回。"""
    monkeypatch.setattr("oci.pagination.list_call_get_all_results", lambda fn, **kw: fn(**kw))
    net = _FakeNet(privs=[
        _priv("pv-primary", "10.0.0.129", primary=True),
        _priv("pv2", "10.0.0.20", managed=True),
        _priv("pv1", "10.0.0.3", managed=True),
    ])
    s = _session(net, [])
    s._compute = _FakeCompute({"ssh_authorized_keys": "ssh-ed25519 AAA", "user_data": "IyEvYmlu"})
    res = s.publish_secondary_ips("inst", "comp")
    assert res.ok, res.message
    (md, kw), = s._compute.updates
    assert md["ssh_authorized_keys"] == "ssh-ed25519 AAA" and md["user_data"] == "IyEvYmlu"
    assert md[TenantSession.SECONDARY_IPS_METADATA_KEY] == '{"vnic1":["10.0.0.3","10.0.0.20"]}'
    assert kw == {"if_match": "e1"}, "要用 etag 防止覆盖别人同时做的修改"

    # 内容没变就不再写一次
    assert s.publish_secondary_ips("inst", "comp").ok
    assert len(s._compute.updates) == 1


def test_sync_parses_real_ip_addr_output(tmp_path):
    ns = _load_sync(tmp_path)
    sample = (
        "2: enp0s6    inet 10.0.0.5/24 metric 100 brd 10.0.0.255 scope global dynamic enp0s6"
        "\\       valid_lft 85814sec preferred_lft 85814sec\n"
        "2: enp0s6    inet 10.0.0.11/24 scope global secondary enp0s6"
        "\\       valid_lft forever preferred_lft forever\n"
    )
    ns["subprocess"] = SimpleNamespace(
        run=lambda *a, **k: SimpleNamespace(stdout=sample, returncode=0), PIPE=None
    )
    assert ns["current_ipv4"]("enp0s6") == {"10.0.0.5", "10.0.0.11"}


def test_install_script_is_valid_bash_and_embeds_valid_python():
    from app import ip_sync

    compile(ip_sync._SYNC_PY, "ocibot-ip-sync", "exec")
    script = ip_sync.build_install_script()
    assert "OCIBOT_SYNC_PY" in script and "systemctl enable --now ocibot-ip-sync.timer" in script
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    res = subprocess.run([bash, "-n"], input=script, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
