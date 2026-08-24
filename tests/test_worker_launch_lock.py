"""worker 的抢机尝试必须和浏览器端的创建互斥。

AUDIT.md pass 11 把「check_launch_quota 是 check-then-act，没有互斥」列为 High，
修复写的是「Wired around snapshot→launch in routers/instances.py **and the worker**」。
后半句从来没有成立：全仓库 `with tenant_launch_lock` 只有 routers/instances.py 一处。

worker 自己的 `_busy_tenants`（进程内集合）和数据库租约只让 **worker 的任务之间**
串行，对一个正在页面上点「创建实例」的人完全没有作用：两边各自读到同一份
「已用 0」的快照，然后各开一台，Always Free 额度双花。抢机任务一挂一整夜，
撞上的概率并不低。

这里钉住三件事：
  1. worker 在取快照之前确实拿了锁；
  2. 锁被别人占着时，本次尝试**推迟**而不是硬闯，而且不消耗尝试次数；
  3. LaunchInstance 一返回就放锁 —— 后面还有 notify_user 的网络 I/O，
     把跨进程锁攥到那时候会让一次推送超时堵住别人在页面上的创建。
"""

from __future__ import annotations

import inspect

import pytest

from web.backend import worker as worker_mod
from web.backend.quota_guard import TenantLaunchLockBusy


def test_the_worker_takes_the_launch_lock_before_reading_usage():
    """源码顺序断言：拿锁必须早于 usage_snapshot，晚了就等于没拿。

    锁的意义是把「取快照 → 判决 → LaunchInstance」整段圈起来。只圈住判决那一句
    没有用：额度是在 LaunchInstance 之后才变的，判决与创建之间只要还有窗口，
    第二个请求仍然读到旧数字。
    """
    src = inspect.getsource(worker_mod.Worker._run_capacity_once)

    acquire = src.index("_acquire_launch_lock")
    snapshot = src.index("usage_snapshot(")
    launch = src.index("launch_from_payload(")
    release = src.index("_release_launch_lock")

    assert acquire < snapshot, "拿锁必须在取用量快照之前"
    assert snapshot < launch, "快照必须在 LaunchInstance 之前（这是被保护的窗口）"
    assert launch < release, "放锁必须在 LaunchInstance 之后，否则窗口没被盖住"


def test_release_happens_before_the_notification_io():
    """通知在锁外面发。

    notify_user 最坏情况是几十秒的网络 I/O（SMTP 会话总期限 45 秒）。跨进程锁
    攥那么久，等于让一次推送超时去堵住另一个人在页面上的创建。
    """
    src = inspect.getsource(worker_mod.Worker._run_capacity_once)
    assert src.index("_release_launch_lock") < src.index("notify_user(")


class _Job:
    id = "job-1"
    tenant_id = "tenant-1"
    attempts = 3
    next_run_at = None
    status = "running"


def test_a_busy_tenant_defers_the_attempt_instead_of_launching(monkeypatch):
    """别人正在为同一个租户创建 → 推迟，不硬闯。"""
    from contextlib import contextmanager

    @contextmanager
    def _busy(tenant_id, **kw):
        raise TenantLaunchLockBusy(tenant_id)
        yield  # pragma: no cover

    import web.backend.quota_guard as qg

    monkeypatch.setattr(qg, "tenant_launch_lock", _busy)

    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w._launch_lock = None
    job = _Job()
    now = worker_mod._utcnow()

    ok = w._acquire_launch_lock(job, 180, now)

    assert ok is False, "拿不到锁必须返回 False，让调用方直接 return"
    assert job.status == "idle"
    assert job.next_run_at is not None and job.next_run_at > now
    assert job.attempts == 3, "别人在创建不是这个任务的失败，不该消耗尝试次数"


def test_a_broken_lock_mechanism_does_not_wedge_the_retry(monkeypatch):
    """锁本身坏了（只读文件系统、数据库断连…）不能让抢机停摆。

    退回到「无锁」是这次修复之前的行为；拒绝服务比双花更糟 —— 双花只影响一个
    租户的额度，停摆影响的是所有人的抢机任务。
    """
    from contextlib import contextmanager

    @contextmanager
    def _broken(tenant_id, **kw):
        raise OSError("read-only file system")
        yield  # pragma: no cover

    import web.backend.quota_guard as qg

    monkeypatch.setattr(qg, "tenant_launch_lock", _broken)

    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w._launch_lock = None
    job = _Job()

    assert w._acquire_launch_lock(job, 180, worker_mod._utcnow()) is True
    assert w._launch_lock is None, "没拿到锁就不该记着一个要释放的对象"


def test_release_is_idempotent():
    """正常路径在 launch 后放一次，tick_capacity 的 finally 再兜一次 ——
    第二次必须是安全的空操作。"""
    released = []

    class _CM:
        def __exit__(self, *a):
            released.append(a)
            return False

    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w._launch_lock = _CM()

    w._release_launch_lock()
    w._release_launch_lock()

    assert len(released) == 1


def test_lock_is_keyed_on_the_tenant_the_job_belongs_to(monkeypatch):
    """锁的 key 必须是 job.tenant_id。

    用 job.id 做 key 看起来也能跑通所有测试，但那样两个不同任务、同一个租户之间
    就没有互斥了 —— 而互斥的对象本来就是「同一个租户的额度」。
    """
    from contextlib import contextmanager

    seen = {}

    @contextmanager
    def _spy(tenant_id, **kw):
        seen["key"] = tenant_id
        yield

    import web.backend.quota_guard as qg

    monkeypatch.setattr(qg, "tenant_launch_lock", _spy)

    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w._launch_lock = None
    assert w._acquire_launch_lock(_Job(), 180, worker_mod._utcnow()) is True

    assert seen["key"] == "tenant-1"
    w._release_launch_lock()
