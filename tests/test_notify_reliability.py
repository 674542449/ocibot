"""通知链路的可靠性/可观测性回归。

这一批缺陷的共同点是「发不出去的时候没人知道，或者知道的是错的」：

* Telegram 的纯文本重发是无条件的，于是一个 429 立刻变成第二次 POST，打向刚刚
  要求你暂停 30 秒的端点；
* 预算耗尽时报的「还有几个渠道没发」把被事件过滤器跳过的渠道也算了进去；
* 渠道查询没有 ORDER BY，预算截断时丢掉哪几个渠道由数据库心情决定；
* 主密钥轮换后每个渠道各自报「缺少字段」，真正的原因一个字都没提；
* worker 把 notify_user 的返回值整个丢掉，「抢机成功但五个渠道全挂」在面板上
  是一条绿色的成功任务。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_notifyrel_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'n.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "notifyrel-master-key-0123456789")
os.environ.setdefault("OCIBOT_JWT_SECRET", "notifyrel-jwt-secret-0123456789")

pytest.importorskip("httpx")
pytest.importorskip("fastapi")

import httpx  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from web.backend import notify as notify_mod  # noqa: E402
from web.backend.db import Base  # noqa: E402
from web.backend.models import AuditLog, CapacityJob  # noqa: E402

_TG = {"bot_token": "123456:AAbbcc", "chat_id": "42"}


@pytest.fixture
def transport(monkeypatch):
    """把 MockTransport 塞进模块的 client kwargs（和 test_notify_hardening 一致）。"""

    def _install(handler):
        kw = dict(notify_mod._HTTP_CLIENT_KW)
        kw["transport"] = httpx.MockTransport(handler)
        monkeypatch.setattr(notify_mod, "_HTTP_CLIENT_KW", kw)

    return _install


# ---------------------------------------------------------------------------
# 1) Telegram：只有解析错误才值得用纯文本重发
# ---------------------------------------------------------------------------


def test_telegram_does_not_hammer_the_endpoint_after_a_429(transport):
    """429 带 retry_after=30，第二次 POST 是直接违反对端刚下的指令。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            429,
            json={"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 30",
                  "parameters": {"retry_after": 30}},
        )

    transport(handler)
    ok, detail = notify_mod._send_telegram(dict(_TG), "t", "b")
    assert ok is False
    assert len(seen) == 1, f"一次 429 产生了 {len(seen)} 次 POST"
    assert "429" in detail


def test_telegram_does_not_retry_when_the_chat_is_simply_wrong(transport):
    """chat not found 是 400，但纯文本重发同样救不了它 —— 不该白发第二次。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    transport(handler)
    ok, _detail = notify_mod._send_telegram(dict(_TG), "t", "b")
    assert ok is False
    assert len(seen) == 1, f"chat not found 产生了 {len(seen)} 次 POST"


def test_telegram_still_falls_back_to_plain_text_on_a_parse_error(transport):
    """本意（Markdown 解析失败 → 去掉 parse_mode 再发一次）必须保住。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "parse_mode" in body:
            return httpx.Response(
                400,
                json={"ok": False, "description": "Bad Request: can't parse entities: "
                                                  "Can't find end of the entity starting at byte offset 7"},
            )
        return httpx.Response(200, json={"ok": True})

    transport(handler)
    ok, detail = notify_mod._send_telegram(dict(_TG), "t", "b*roken")
    assert (ok, detail) == (True, "sent")
    assert len(seen) == 2
    assert "parse_mode" not in seen[1], "重发必须去掉 parse_mode，否则重发毫无意义"


# ---------------------------------------------------------------------------
# 2)/3) notify_user 的扇出：预算日志的数字，以及确定性顺序
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, i: int, events):
        self.id = f"c{i:03d}"
        self.name = f"ch{i}"
        self.kind = "webhook"
        self.enabled = True
        self.events = events
        self.config_encrypted = ""


class _DB:
    """只实现 notify_user 用到的那一点点 Session 表面，并记下 SELECT 语句。"""

    def __init__(self, rows):
        self._rows = rows
        self.statements: list = []

    def scalars(self, stmt):
        self.statements.append(stmt)
        rows = self._rows

        class _R:
            def all(self_inner):
                return rows

        return _R()


def test_budget_log_counts_only_the_channels_that_would_have_been_sent(monkeypatch, caplog):
    """被事件过滤器跳过的渠道不是「没来得及发」，把它们算进去只会误导排障。"""
    monkeypatch.setattr(notify_mod, "_MAX_SENDS_PER_EVENT", 3)
    monkeypatch.setattr(notify_mod, "send_to_channel", lambda *a: (True, "sent"))
    rows = (
        [_Row(i, ["capacity"]) for i in range(3)]
        + [_Row(100 + i, []) for i in range(10)]
        + [_Row(200 + i, ["capacity"]) for i in range(4)]
    )
    with caplog.at_level(logging.WARNING, logger="ocibot.notify"):
        results = notify_mod.notify_user(_DB(rows), "owner", "capacity", "t", "b")
    assert len(results) == 3
    budget = [r for r in caplog.records if "budget reached" in r.getMessage()]
    assert budget, "预算截断必须留下日志"
    assert budget[0].args[-1] == 4, (
        f"报了 {budget[0].args[-1]} 个渠道没发，真正没发的只有 4 个"
    )


def test_channel_fan_out_has_a_deterministic_order():
    """预算把扇出截断时，被丢掉的是哪几个渠道不能由数据库决定。"""
    db = _DB([_Row(0, ["capacity"])])
    notify_mod.notify_user(db, "owner", "capacity", "t", "b")
    assert db.statements, "notify_user 应当发过一条渠道查询"
    sql = str(db.statements[0])
    assert "ORDER BY" in sql.upper(), f"渠道查询没有排序：{sql}"
    assert "created_at" in sql, f"排序键应当是创建时间：{sql}"


# ---------------------------------------------------------------------------
# 4) 主密钥轮换要说人话
# ---------------------------------------------------------------------------


def _corrupt_token(token: str) -> str:
    """把一个合法的 Fernet 密文改坏 —— 等价于换了 OCIBOT_MASTER_KEY。"""
    mid = len(token) // 2
    swap = "A" if token[mid] != "A" else "B"
    return token[:mid] + swap + token[mid + 1 :]


def test_a_rotated_master_key_is_named_instead_of_faking_a_config_error():
    bad = _corrupt_token(notify_mod.encode_channel_config(dict(_TG)))
    config = notify_mod.decode_channel_config(bad)
    ok, detail = notify_mod.send_to_channel("telegram", config, "t", "b")
    assert ok is False
    assert "OCIBOT_MASTER_KEY" in detail, f"没提主密钥，只说了：{detail}"
    assert "bot_token" not in detail, "不该把它说成是字段填错"
    assert bad[:24] not in detail, "错误信息里不能出现密文"


def test_a_readable_channel_is_unaffected():
    config = notify_mod.decode_channel_config(notify_mod.encode_channel_config(dict(_TG)))
    assert config == _TG
    assert notify_mod.decode_channel_config("") == {}


def test_the_channel_list_still_renders_when_the_master_key_no_longer_matches():
    """解不开配置时抛异常会让整个渠道列表 500 —— 恰恰是最需要它能打开的时候。"""
    from web.backend.routers.notifications import _out

    class _Row:
        id = "c1"
        kind = "telegram"
        name = "tg"
        enabled = True
        events = ["capacity"]
        config_encrypted = _corrupt_token(notify_mod.encode_channel_config(dict(_TG)))
        created_at = None

    out = _out(_Row())
    assert out.id == "c1"
    assert "AAbbcc" not in out.config_hint


# ---------------------------------------------------------------------------
# 5) STARTTLS 被拒时的提示不能指向面板里不存在的操作
# ---------------------------------------------------------------------------


def test_starttls_refusal_message_does_not_promise_a_settings_toggle(monkeypatch):
    import smtplib

    from web.backend import url_safety

    monkeypatch.setattr(url_safety, "resolve_and_check_host", lambda host: None)
    monkeypatch.setattr(url_safety, "hostname_is_blocked", lambda host: False)

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.sock = None

        def starttls(self, context=None):
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")

        def quit(self):
            return None

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    ok, detail = notify_mod._send_smtp(
        {
            "host": "smtp.example.com",
            "port": 587,
            "username": "bot@example.com",
            "password": "pw",
            "to_addr": "ops@example.com",
        },
        "t",
        "b",
    )
    assert ok is False
    assert "465" in detail, "必须给出面板里真正做得到的修法"
    assert "在渠道配置中显式设置 require_tls=false" not in detail, (
        "SettingsView 没有这个字段，这句话把操作员送去找一个不存在的开关"
    )


# ---------------------------------------------------------------------------
# 6) worker：推送失败必须在面板里留下痕迹
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    """独立的临时库 —— 绝不碰 web_data/ocibot_web.db。"""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'w.db').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _audit_rows(db) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog)).all())


def test_the_order_by_actually_runs_against_a_real_session(db):
    """排序是加在 ORM 语句上的，得确认它真的能编译执行，而不只是字符串里有。"""
    from datetime import datetime, timedelta, timezone

    from web.backend.models import NotificationChannel

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, offset in enumerate([5, 1, 3]):
        db.add(
            NotificationChannel(
                id=f"ch-{i}",
                owner_id="user-1",
                kind="webhook",
                name=f"n{i}",
                enabled=True,
                config_encrypted="",
                events=["capacity"],
                created_at=base + timedelta(minutes=offset),
            )
        )
    db.flush()
    original = notify_mod.send_to_channel
    notify_mod.send_to_channel = lambda kind, cfg, t, b: (True, "sent")
    try:
        results = notify_mod.notify_user(db, "user-1", "capacity", "t", "b")
    finally:
        notify_mod.send_to_channel = original
    seen = [r["channel"] for r in results]
    assert seen == ["n1", "n2", "n0"], seen


def test_a_failed_push_is_recorded_where_the_operator_can_see_it(db, monkeypatch):
    """「抢机成功，五个渠道全挂」以前只在 worker 容器日志里有一行 warning。"""
    from web.backend import worker as worker_mod

    job = CapacityJob(
        id="job-1",
        owner_id="user-1",
        tenant_id="tenant-1",
        name="夜间抢机",
        attempts=7,
        max_attempts=10,
        last_error="Out of host capacity",
    )
    db.add(job)
    db.flush()

    monkeypatch.setattr(
        worker_mod,
        "notify_user",
        lambda *a, **kw: [
            {"channel": "tg", "kind": "telegram", "ok": False, "detail": "HTTP 429"},
            {"channel": "邮件", "kind": "smtp", "ok": False, "detail": "SMTP 认证失败"},
        ],
    )
    w = worker_mod.Worker.__new__(worker_mod.Worker)  # 不跑 __init__（要读配置、建 OCI session）
    w._notify_capacity_end(db, job, reason="permanent")
    db.commit()

    rows = _audit_rows(db)
    assert rows, "推送全部失败，面板上却一条记录都没有"
    assert rows[0].action.startswith("notify."), rows[0].action
    assert rows[0].owner_id == "user-1"
    assert "job-1" in (rows[0].target or "")
    detail = json.loads(rows[0].detail)
    assert detail["failed"] == 2
    assert {c["kind"] for c in detail["channels"]} == {"telegram", "smtp"}


def test_a_successful_push_writes_no_audit_noise(db, monkeypatch):
    from web.backend import worker as worker_mod

    job = CapacityJob(id="job-2", owner_id="user-1", tenant_id="tenant-1", name="ok")
    db.add(job)
    db.flush()
    monkeypatch.setattr(
        worker_mod,
        "notify_user",
        lambda *a, **kw: [{"channel": "tg", "kind": "telegram", "ok": True, "detail": "sent"}],
    )
    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w._notify_capacity_end(db, job, reason="max_attempts")
    db.commit()
    assert _audit_rows(db) == []


def test_recording_works_on_the_success_path_where_the_transaction_was_just_committed(db):
    """抢机成功那条路径是 `db.commit()` → notify_user → 记录，session 是干净的。"""
    from web.backend import worker as worker_mod

    db.add(CapacityJob(id="job-4", owner_id="user-1", tenant_id="tenant-1", name="ok"))
    db.commit()
    worker_mod.Worker._record_notify_failures(
        db,
        owner_id="user-1",
        target="capacity_job:job-4",
        event="capacity",
        results=[{"channel": "tg", "kind": "telegram", "ok": False, "detail": "HTTP 500"}],
    )
    db.commit()
    rows = _audit_rows(db)
    assert len(rows) == 1 and rows[0].action == "notify.failed"


def test_recording_the_failure_cannot_roll_back_the_job_state(db, monkeypatch):
    """审计写入失败是可以接受的；把调用方那笔事务打坏不是。

    _log_attempt 的注释记着这条教训：worker 里任何「顺手写一行」的操作一旦回滚了
    attempts += 1，max_attempts 那道上限就永远够不到，租约一过期任务重新认领、
    再发一次 LaunchInstance，无限循环。
    """
    from web.backend import worker as worker_mod

    job = CapacityJob(id="job-3", owner_id="user-1", tenant_id="tenant-1", name="x", attempts=3)
    db.add(job)
    db.flush()

    job.attempts = 4
    job.status = "failed"
    # 让写入在 **flush** 阶段炸，而不是在构造阶段。
    #
    # 原来这里是 `AuditLog = lambda **kw: raise RuntimeError(...)`，异常发生在
    # db.add() 之前 —— SAVEPOINT 里一条 SQL 都没发出去，回滚的是一个空 savepoint。
    # 那样只证明了「异常被接住」，完全没有验证 docstring 声称的那件事：写入**发出去
    # 之后**失败时，调用方那笔还没提交的改动能不能活下来。
    # action 是 NOT NULL，给它 None 就会在 flush 时触发 IntegrityError，
    # 这才是真实的失败形状（超长字段、约束冲突都走这条路）。
    _real = worker_mod.AuditLog
    monkeypatch.setattr(
        worker_mod, "AuditLog", lambda **kw: _real(**{**kw, "action": None})
    )
    worker_mod.Worker._record_notify_failures(
        db,
        owner_id="user-1",
        target="capacity_job:job-3",
        event="capacity",
        results=[{"channel": "tg", "kind": "telegram", "ok": False, "detail": "HTTP 500"}],
    )
    db.commit()
    fresh = db.get(CapacityJob, "job-3")
    assert (fresh.attempts, fresh.status) == (4, "failed"), "审计写失败把任务状态一起回滚了"
