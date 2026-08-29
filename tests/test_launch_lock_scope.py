"""锁里到底发生了哪些 OCI 调用 —— 把这件事钉死。

`tenant_launch_lock` 是一把**每租户串行**的锁：免费额度校验只是 check-then-act，
它不预留任何东西，所以「取用量快照 → 判决 → LaunchInstance」必须整段在锁内，否则
两个标签页各自读到「已用 0」会双双放行（pass 11 审计里的 High）。代价是这把锁的
持有时间等于锁内所有 OCI 往返之和，同一租户的第二次创建要排队等完。

于是锁内多一次调用就是两笔账：多花一次 OCI 请求预算（这个项目刻意把调用量压到
最低，见 CLAUDE.md），以及把别人的创建再多堵住一次往返。这个文件用**真实的**
`TenantSession.get_free_quota_usage` 跑一遍真实的创建路由，把锁内发生的调用逐个
记下来，任何人往锁里加一次 OCI 调用都会在这里红。

两个方向都要拦：
  - 往锁里**加**调用 —— 变慢，见 test_calls_inside_the_lock_are_pinned。
  - 把额度校验**挪出**锁 —— 双花回来了，见 test_the_quota_snapshot_is_taken_inside_the_lock。

顺带记录一个已核实、但不能在本文件的 lane 里修的事实：对象存储枚举
（list_buckets + 每个桶的 list_objects）是在锁内跑的，而它算出来的数字
**从来没有参与过创建的判决**，见 test_object_storage_usage_cannot_change_the_verdict。
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.free_quota import a1_caps

_FREE_CPU, _FREE_MEM = a1_caps("free")

_TMP = tempfile.mkdtemp(prefix="ocibot_lockscope_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'lockscope.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "lockscope-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "lockscope-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import web.backend.routers.instances as instances_router  # noqa: E402
from app import free_quota  # noqa: E402
from app.oci_client import TenantSession  # noqa: E402
from web.backend import quota_guard  # noqa: E402
from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402
from web.backend.quota_guard import launch_lock_held  # noqa: E402

from tests._keys import TEST_PEM

A1 = "VM.Standard.A1.Flex"


class _Res:
    def __init__(self, ok: bool = True, message: str = "", data: Any = None, wr: str = "wr-1"):
        self.ok = ok
        self.message = message
        self.data = {} if data is None else data
        self.work_request_id = wr


class _ObjectPage:
    """`ObjectStorageClient.list_objects(...).data` 的形状，够 estimator 用即可。"""

    def __init__(self, objects: list[Any]):
        self.objects = objects
        self.next_start_with = None


class _SdkObjectStorage:
    """OCI SDK 的 ObjectStorageClient 替身；estimator 只会碰 list_objects。"""

    def __init__(self, log: list[tuple[str, bool]], tenant_id_box: dict[str, str]):
        self._log = log
        self._box = tenant_id_box

    def list_objects(self, namespace: str, bucket: str, **_kw: Any) -> _Res:
        _record(self._log, self._box, f"object_storage.list_objects[{bucket}]")
        return _Res(data=_ObjectPage([SimpleNamespace(name="a.bin", size=1024)]))


def _record(log: list[tuple[str, bool]], box: dict[str, str], name: str) -> None:
    """记下一次 OCI 调用，以及**发生时锁是不是握着的**。

    用 quota_guard.launch_lock_held 而不是自己在上下文管理器里插标记：这是生产代码
    自己的谓词（按线程记账），路由跑在 TestClient 的同一个工作线程里，所以它回答的
    就是「这次调用是不是在锁内发出的」。
    """
    log.append((name, launch_lock_held(box["tenant_id"])))


class _RecordingSession:
    """记录调用的假 TenantSession。

    get_free_quota_usage / estimate_object_storage_usage 用的是 TenantSession 上
    **真实**的实现，不是复述：这个测试要钉的是生产代码在锁里做了什么，自己重写一份
    快照逻辑就只能证明我写的那份做了什么。真实实现会自己决定去调 list_instances_tree /
    list_boot_volumes / list_block_volumes / estimate_object_storage_usage，下面这些
    方法只是把每一次调用记下来。
    """

    get_free_quota_usage = TenantSession.get_free_quota_usage
    estimate_object_storage_usage = TenantSession.estimate_object_storage_usage

    def __init__(self, tenant_id_box: dict[str, str], *, buckets: int = 2):
        self.calls: list[tuple[str, bool]] = []
        self._box = tenant_id_box
        self._buckets = buckets
        self._last_tree_errors: list[str] = []
        self.tenant = SimpleNamespace(account_tier="free", region="ap-tokyo-1")
        self.object_storage = _SdkObjectStorage(self.calls, tenant_id_box)

    def _log(self, name: str) -> None:
        _record(self.calls, self._box, name)

    # --- 免费额度快照要用到的读 ---------------------------------------------
    def list_instances_tree(self, resolve_ips: bool = False, **_kw: Any) -> list[dict[str, Any]]:
        self._log("list_instances_tree")
        return []

    def list_boot_volumes(self, **_kw: Any) -> _Res:
        self._log("list_boot_volumes")
        return _Res(data={"volumes": [], "errors": []})

    def list_block_volumes(self, **_kw: Any) -> _Res:
        self._log("list_block_volumes")
        return _Res(data={"volumes": [], "errors": []})

    def list_buckets(self, compartment_id: str = "") -> _Res:
        self._log("list_buckets")
        return _Res(
            data={
                "namespace": "ns",
                "buckets": [
                    {"name": f"b{i + 1}", "compartment_id": "ocid1.compartment.oc1..c1"}
                    for i in range(self._buckets)
                ],
            }
        )

    # --- 副区判定 -------------------------------------------------------------
    def home_region(self) -> str:
        self._log("home_region")
        return "ap-tokyo-1"

    # --- 真正的创建 -----------------------------------------------------------
    def launch_from_payload(self, payload: dict[str, Any], **_kw: Any) -> _Res:
        self._log("launch_from_payload")
        return _Res(message="创建成功", data={"instance_id": "ocid1.instance.oc1..new"})

    def delete_managed_nsg(self, nsg_id: str) -> _Res:
        self._log("delete_managed_nsg")
        return _Res()


@pytest.fixture(scope="module")
def client() -> Any:
    init_db()
    username = "launch-lock-scope-user"
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).one_or_none()
        if user is None:
            user = User(username=username, password_hash=hash_password("supersecret123"))
            db.add(user)
            db.flush()
        tenant = db.query(Tenant).filter(Tenant.owner_id == user.id).one_or_none()
        if tenant is None:
            tenant = Tenant(
                owner_id=user.id,
                name="LockScope",
                region="ap-tokyo-1",
                user_ocid="ocid1.user.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                tenancy_ocid="ocid1.tenancy.oc1..aaaabbbbccccddddeeeeffffgggghhhh",
                fingerprint="11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00",
                private_key_encrypted=encrypt_text(TEST_PEM),
            )
            db.add(tenant)
        db.commit()
        tenant_id = tenant.id

    with TestClient(app) as c:
        assert (
            c.post(
                "/api/auth/login",
                json={"username": username, "password": "supersecret123"},
            ).status_code
            == 200
        )
        yield c, tenant_id

    # SQLite 回退路径会在数据库文件旁留下一个每租户的锁文件。
    path = quota_guard._lock_file_path(tenant_id)
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


def _wire_route(monkeypatch: pytest.MonkeyPatch, session: _RecordingSession, box: dict[str, str]):
    """把锁**外**的东西替换成不发 OCI 请求的桩，只留锁内那段是真的。

    fetch_launch_meta / build_launch_request 本来就在锁外（它们比锁先跑），换成桩
    是为了让记录下来的调用只剩下真正值得讨论的那些；prepare_launch_network 会写
    Oracle（建 NSG 甚至 VCN），这里记一笔就好。
    """
    monkeypatch.setattr(instances_router, "get_session_for_row", lambda row: session)
    monkeypatch.setattr(
        instances_router,
        "fetch_launch_meta",
        lambda s, *, tenant_id, force=False: (_record(session.calls, box, "fetch_launch_meta"), {})[1],
    )
    monkeypatch.setattr(
        instances_router,
        "build_launch_request",
        lambda body, meta=None: {
            "payload": {
                "display_name": body.get("display_name") or "instance",
                "shape": body.get("shape"),
                "ocpus": body.get("ocpus"),
                "memory_in_gbs": body.get("memory_in_gbs"),
                "boot_volume_size_in_gbs": body.get("boot_volume_size_in_gbs"),
                "boot_volume_vpus_per_gb": 10,
                "compartment_id": "ocid1.compartment.oc1..c1",
                "auth_mode": "key",
            },
            "root_password": "",
            "custom_user_data": "",
            "as_retry": False,
            "fallback_configs": [],
            "availability_domains": [],
            "retry_interval_sec": 180,
            "retry_max_attempts": 200,
        },
    )

    def _prep(s: Any, payload: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        _record(session.calls, box, "prepare_launch_network")
        return payload

    monkeypatch.setattr(instances_router, "prepare_launch_network", _prep)
    monkeypatch.setattr(instances_router, "schedule_post_launch_adjustments", lambda *a, **k: None)


def _launch(client_and_tid: Any, monkeypatch: pytest.MonkeyPatch, **overrides: Any):
    c, tenant_id = client_and_tid
    box = {"tenant_id": tenant_id}
    session = _RecordingSession(box)
    _wire_route(monkeypatch, session, box)
    body = {
        "display_name": "lockscope",
        "shape": A1,
        "image_id": "ocid1.image.oc1..img",
        "subnet_id": "ocid1.subnet.oc1..sub",
        "ocpus": 1,
        "memory_in_gbs": 6,
        "boot_volume_size_in_gbs": 47,
    }
    body.update(overrides)
    resp = c.post(f"/api/tenants/{tenant_id}/launch", json=body)
    return resp, session


def _inside(session: _RecordingSession) -> list[str]:
    return [name for name, held in session.calls if held]


def _outside(session: _RecordingSession) -> list[str]:
    return [name for name, held in session.calls if not held]


# ---------------------------------------------------------------------------
# 锁内调用清单
# ---------------------------------------------------------------------------


def test_calls_inside_the_lock_are_pinned(client, monkeypatch):
    """锁内的 OCI 调用只能是这一串 —— 多一个就要在这里解释为什么。

    这把锁是每租户串行的，持有时间 = 锁内所有 OCI 往返之和。往里加一次调用，
    同一租户排在后面的创建就多等一次往返，而且多花一份 OCI 请求预算 ——
    这个项目连页面进入都不肯自动拉取（CLAUDE.md），锁内更没有免费的调用。
    """
    resp, session = _launch(client, monkeypatch)
    assert resp.status_code == 200, resp.text

    assert _inside(session) == [
        # 副区判定：home_region 在 TenantSession 里是按 session 缓存的，实际只打一次。
        "home_region",
        # 免费额度快照 —— 判决真正需要的三份读。
        "list_instances_tree",
        "list_boot_volumes",
        "list_block_volumes",
        # 对象存储枚举：桶数 × 分页次数。判决从不读它算出来的数字，
        # 见 test_object_storage_usage_cannot_change_the_verdict。
        "list_buckets",
        "object_storage.list_objects[b1]",
        "object_storage.list_objects[b2]",
        # 写 Oracle（可能建 NSG / VCN），必须留在锁内。
        "prepare_launch_network",
        "launch_from_payload",
    ], session.calls

    # launch-meta 是在锁**外**取的。它是这条路由第二贵的读，被挪进锁里过一次就
    # 白白拉长所有人的排队。
    assert _outside(session) == ["fetch_launch_meta"], session.calls


def test_object_storage_enumeration_scales_with_bucket_count(client, monkeypatch):
    """锁内那段对象存储枚举不是一次调用，是「1 + 桶数」次。

    真实上限是 50 个桶 × 每桶最多 20 页 list_objects，还带一个 25 秒的软超时 ——
    换句话说，一个桶多的租户可以让这把每租户互斥锁多握将近半分钟，而这半分钟
    对创建能不能通过没有任何影响。
    """
    c, tenant_id = client
    box = {"tenant_id": tenant_id}
    session = _RecordingSession(box, buckets=7)
    _wire_route(monkeypatch, session, box)
    resp = c.post(
        f"/api/tenants/{tenant_id}/launch",
        json={
            "display_name": "lockscope",
            "shape": A1,
            "image_id": "ocid1.image.oc1..img",
            "subnet_id": "ocid1.subnet.oc1..sub",
            "ocpus": 1,
            "memory_in_gbs": 6,
            "boot_volume_size_in_gbs": 47,
        },
    )
    assert resp.status_code == 200, resp.text
    inside = _inside(session)
    assert inside.count("list_buckets") == 1
    assert len([n for n in inside if n.startswith("object_storage.list_objects")]) == 7


def test_batch_creates_take_one_snapshot_for_the_whole_batch(client, monkeypatch):
    """批量创建只取一次快照、发 N 次 LaunchInstance。

    额度是租户级总量，所以判决必须一次算完整批（tests/test_launch_count.py 管那条
    算术）；这里管的是**代价**：每台机器重取一次快照，就是把锁内最贵的那段乘以 N。
    """
    # 台数按当前免费额度算：1 OCPU 一台，开满为止（原来写死 3，
    # 而免费额度砍半之后 3 台已经超限，测试挂在了一个和它无关的断言上）。
    n = int(_FREE_CPU)
    resp, session = _launch(client, monkeypatch, count=n)
    assert resp.status_code == 200, resp.text
    inside = _inside(session)
    assert inside.count("list_instances_tree") == 1
    assert inside.count("list_buckets") == 1
    assert inside.count("launch_from_payload") == n


# ---------------------------------------------------------------------------
# 正确性方向：额度校验不许被挪出锁
# ---------------------------------------------------------------------------


def test_the_quota_snapshot_is_taken_inside_the_lock(client, monkeypatch):
    """快照必须在锁内取，而且和 LaunchInstance 之间不许有释放锁的间隙。

    这条是给「优化锁内调用」的人设的护栏：把 list_instances_tree 挪到锁外
    （比如为了先算好用量再进锁）看起来只是把慢的一段前移，实际上直接把 pass 11
    修掉的双花放了回来 —— 两个请求各自在锁外读到「已用 0」，再依次进锁创建，
    租户拿到 8 OCPU / 48 GB。
    """
    resp, session = _launch(client, monkeypatch)
    assert resp.status_code == 200, resp.text

    names = [name for name, _ in session.calls]
    held = dict(zip(names, [h for _, h in session.calls]))
    assert held["list_instances_tree"] is True
    assert held["launch_from_payload"] is True
    # 从快照到创建之间不能出现任何一次「锁已放开」的调用。
    first = names.index("list_instances_tree")
    last = len(names) - 1 - names[::-1].index("launch_from_payload")
    assert all(h for _, h in session.calls[first : last + 1]), session.calls


def test_lock_wraps_the_launch_even_when_the_guard_refuses(client, monkeypatch):
    """额度不够时也走同一把锁 —— 拒绝路径不该是另一套代码。"""
    c, tenant_id = client
    box = {"tenant_id": tenant_id}
    session = _RecordingSession(box)
    _wire_route(monkeypatch, session, box)
    resp = c.post(
        f"/api/tenants/{tenant_id}/launch",
        json={
            "display_name": "lockscope",
            "shape": A1,
            "image_id": "ocid1.image.oc1..img",
            "subnet_id": "ocid1.subnet.oc1..sub",
            "ocpus": 8,  # 免费上限是 4
            "memory_in_gbs": 48,
            "boot_volume_size_in_gbs": 47,
        },
    )
    assert resp.status_code == 400, resp.text
    inside = _inside(session)
    assert "list_instances_tree" in inside
    assert "launch_from_payload" not in inside


# ---------------------------------------------------------------------------
# 为什么说对象存储那段是纯开销
# ---------------------------------------------------------------------------


def test_object_storage_usage_cannot_change_the_verdict():
    """对象存储用量对创建判决**完全没有影响**：0 GB 和 500 GB 得出同一个结论。

    validate_launch_against_quota 只读 a1_ocpu / a1_memory_gb / e2_micro_count /
    block_storage_gb 四项；object_storage_gb 只进 buckets/summary_lines，也就是
    仪表盘那四根进度条。所以锁内那段「1 + N 次」的桶枚举，对「能不能创建」这个
    问题是零信息量。

    这不是在这里改掉它 —— 枚举写死在 oci_client.get_free_quota_usage 里
    （它没有 include_object 开关，只有 include_block / include_egress），
    routers/instances.py 够不着。这条断言的用处是：等有人给
    get_free_quota_usage 加上开关时，它是「关掉不会改变判决」的现成证据。
    """

    def verdict(object_gb: float):
        usage = {
            "usage": {
                "a1_ocpu": 2.0,
                "a1_memory_gb": 12.0,
                "e2_micro_count": 0,
                "block_storage_gb": 100.0,
                "object_storage_gb": object_gb,
            },
            "remaining": {
                "a1_ocpu": 2.0,
                "a1_memory_gb": 12.0,
                "e2_micro_count": 2,
                "block_storage_gb": 100.0,
                "object_storage_gb": max(0.0, 20.0 - object_gb),
            },
            "read_incomplete": False,
        }
        return free_quota.validate_launch_against_quota(
            shape=A1,
            ocpus=2,
            memory_in_gbs=12,
            boot_volume_size_in_gbs=47,
            boot_volume_vpus_per_gb=10,
            free_only_mode=True,
            account_tier="free",
            usage=usage,
            count=1,
        )

    empty = verdict(0.0)
    overflowing = verdict(500.0)  # 免费上限只有 20 GB，已经爆表 25 倍
    assert empty.ok is overflowing.ok is True
    assert empty.error_messages() == overflowing.error_messages() == []
    assert [w.code for w in empty.warnings] == [w.code for w in overflowing.warnings]
    assert empty.projected == overflowing.projected
