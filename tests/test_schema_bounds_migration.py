"""``_ensure_schema()`` 不许在迁移失败之后照常把服务拉起来。

（文件名跟着 ``test_schema_bounds_*`` 这一组走：同一批修复，都是「列和写入对不
上」这一类问题，只不过这里管的是列**存不存在**，隔壁管的是列**多宽**。）

原来的实现有两个问题，合在一起是最坏的组合：

1. 全部 ALTER 共用**一个** ``engine.begin()``。PostgreSQL 上一条语句失败就把事务
   打成 aborted，后面每一条 ``conn.execute`` 都抛 ``InFailedSqlTransaction``，最
   后那个隐式 COMMIT 退化成 ROLLBACK —— **这一轮里之前已经加好的列全部被丢掉**。
2. ``except Exception: log.exception(...)`` 之后继续循环，``init_db()`` 照常返回
   成功。

具体触发器：一个有 DML 权限但不是表 owner 的 Postgres 角色（相当常见的加固配
置）。每条 ALTER 都是 ``must be owner of table``，只有日志里留一行，API 照常起
来，然后第一个 ``POST /api/tenants`` 在运行时因为缺列而失败 —— 正是 CLAUDE.md 记
的 0.4.36 那种「看不出是 schema 问题」的故障。

这些断言都打在**行为契约**上（失败要抛、每条 ALTER 一个事务、并发竞态要放行），
不打在 SQLite 的事务语义上：pysqlite 的 DDL 提交行为和 PostgreSQL 不一样，靠它
是验不出真 bug 的。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("OCIBOT_MASTER_KEY", "migr-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "migr-test-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from sqlalchemy import create_engine, event, inspect  # noqa: E402

import web.backend.db as db_mod  # noqa: E402
from web.backend.db import Base, SchemaMigrationError  # noqa: E402
from web.backend.models import Tenant  # noqa: E402

# 按 models.Tenant 里的定义顺序排列，_ensure_schema 也是按这个顺序 ALTER 的。
# 三列都是普通列（没有索引 / 约束 / FK），SQLite 才肯 DROP COLUMN。
_MISSING = ("description", "color", "account_tier")
_FAILING = "color"  # 中间那一列：既有「它之前的」也有「它之后的」


@pytest.fixture()
def upgraded_db(monkeypatch):
    """一个「差三列」的旧数据库，外加一个指向它的私有 engine。

    私有 engine 而不是共享的 SessionLocal：DATABASE_URL 是哪个测试模块先导入哪个
    说了算，绑到全局 session 上这里的 schema 手术会落到别人的库里。
    """
    tmp = Path(tempfile.mkdtemp(prefix="ocibot_migr_"), "m.db")
    eng = create_engine(f"sqlite+pysqlite:///{tmp.as_posix()}", future=True)
    Base.metadata.create_all(bind=eng)
    eng.dispose()

    con = sqlite3.connect(tmp)
    try:
        for column in _MISSING:
            con.execute(f'ALTER TABLE {Tenant.__tablename__} DROP COLUMN "{column}"')
        con.commit()
    finally:
        con.close()

    eng = create_engine(f"sqlite+pysqlite:///{tmp.as_posix()}", future=True)
    monkeypatch.setattr(db_mod, "engine", eng)
    yield eng
    eng.dispose()


def _columns(eng) -> set[str]:
    return {c["name"] for c in inspect(eng).get_columns(Tenant.__tablename__)}


def _fail_on(eng, column: str) -> None:
    """让某一条 ALTER 失败，模拟「账号不是表 owner」。"""

    @event.listens_for(eng, "before_cursor_execute")
    def _boom(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if f'ADD COLUMN "{column}"' in statement:
            raise sqlite3.OperationalError(f'must be owner of table {Tenant.__tablename__}')


def test_the_fixture_really_is_missing_those_columns(upgraded_db):
    assert not (set(_MISSING) & _columns(upgraded_db))


def test_a_failed_alter_is_fatal_instead_of_booting_with_a_missing_column(upgraded_db):
    _fail_on(upgraded_db, _FAILING)

    with pytest.raises(SchemaMigrationError) as exc:
        db_mod._ensure_schema()

    detail = str(exc.value)
    assert f"{Tenant.__tablename__}.{_FAILING}" in detail, (
        "报错里没有指出是哪一列 —— 运营者拿不到可操作的信息，"
        "又回到了「运行时一个说不清原因的 500」"
    )


def test_one_failed_alter_does_not_discard_the_others(upgraded_db):
    """失败那条之前和之后的列都必须真的加上了。

    共用一个事务时，PostgreSQL 会把整轮一起 ROLLBACK 掉。
    """
    _fail_on(upgraded_db, _FAILING)

    with pytest.raises(SchemaMigrationError):
        db_mod._ensure_schema()

    present = _columns(upgraded_db)
    for column in _MISSING:
        if column == _FAILING:
            continue
        assert column in present, f"{column} 被那条失败的 ALTER 连带丢掉了"


def test_each_alter_gets_its_own_transaction(upgraded_db):
    """结构性断言：N 个缺列 = N 个事务，而不是一个事务包住全部。

    这才是「一条失败不作废其余」在 PostgreSQL 上成立的原因；SQLite 的 DDL 提交
    语义不一样，光看结果是验不出来的。
    """
    real_begin = upgraded_db.begin
    opened: list[int] = []

    def counting_begin(*args, **kwargs):
        opened.append(1)
        return real_begin(*args, **kwargs)

    upgraded_db.begin = counting_begin
    try:
        db_mod._ensure_schema()
    finally:
        upgraded_db.begin = real_begin

    assert len(opened) == len(_MISSING), (
        f"{len(_MISSING)} 条 ALTER 只开了 {len(opened)} 个事务 —— "
        "共用事务时一条失败会作废其余所有"
    )
    assert set(_MISSING) <= _columns(upgraded_db)


def test_a_column_a_concurrent_worker_already_added_is_not_a_failure(upgraded_db, monkeypatch):
    """OCIBOT_API_WORKERS 默认是 2，输的那个进程会撞上 duplicate column。

    那不是故障，报错反而会让一次正常的滚动重启起不来。
    """
    _fail_on(upgraded_db, _FAILING)
    monkeypatch.setattr(db_mod, "_column_exists", lambda _t, _c: True)

    db_mod._ensure_schema()  # 不许抛


def test_a_healthy_upgrade_adds_every_missing_column(upgraded_db):
    db_mod._ensure_schema()
    assert set(_MISSING) <= _columns(upgraded_db)
