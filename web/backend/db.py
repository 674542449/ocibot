"""SQLAlchemy engine / session."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from web.backend.config import get_settings


class Base(DeclarativeBase):
    pass


def _is_memory_sqlite(url: str) -> bool:
    """True for a SQLite URL that lives in RAM rather than on disk.

    Parsed rather than pattern-matched, because there are three spellings and
    the easy-to-forget one is the bare ``sqlite://`` — no ``:memory:`` text in
    it anywhere, yet still an in-memory database. The others are the explicit
    ``:memory:`` and the URI form ``file:x?mode=memory`` used for a shared one.
    """
    try:
        parsed = make_url(url)
    except Exception:
        # Unparseable: let create_engine raise the real error rather than
        # guessing a pool configuration from a URL we do not understand.
        return False
    if not parsed.database:
        return True  # sqlite:// — no path means memory
    if parsed.database == ":memory:":
        return True
    # In the URI form the mode lands in the query string, not in `database`
    # (which is just "file:shared"), so checking the path alone misses it.
    return str(parsed.query.get("mode", "")).lower() == "memory"


def _make_engine():
    settings = get_settings()
    url = settings.database_url
    connect_args: dict = {}
    engine_kwargs: dict = {
        "pool_pre_ping": True,
        "future": True,
        "connect_args": connect_args,
    }
    if settings.is_sqlite:
        # Ensure parent dir exists for default sqlite path
        if ":///" in url:
            raw_path = url.split(":///", 1)[1]
            if raw_path and not _is_memory_sqlite(url):
                Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False
        # SQLite: small pool (often NullPool-ish behavior is fine; keep simple).
        #
        # Not for in-memory, though: SQLAlchemy's pysqlite dialect gives a memory
        # URL a SingletonThreadPool, which accepts neither of these and raises
        # TypeError from create_engine — at import time, so the whole API dies
        # before it can say why. A file URL gets a QueuePool and is fine.
        if not _is_memory_sqlite(url):
            engine_kwargs["pool_size"] = 5
            engine_kwargs["max_overflow"] = 0
    else:
        # PostgreSQL production pool — faster concurrent API + worker traffic.
        engine_kwargs["pool_size"] = int(getattr(settings, "db_pool_size", 10) or 10)
        engine_kwargs["max_overflow"] = int(getattr(settings, "db_max_overflow", 20) or 20)
        engine_kwargs["pool_recycle"] = int(getattr(settings, "db_pool_recycle", 1800) or 1800)
        engine_kwargs["pool_timeout"] = 30
    engine = create_engine(url, **engine_kwargs)
    if settings.is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragma(dbapi_conn, _connection_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    # Import models so metadata is registered.
    from web.backend import models  # noqa: F401

    from sqlalchemy.exc import DatabaseError

    try:
        Base.metadata.create_all(bind=engine)
    except DatabaseError:
        # The default OCIBOT_API_WORKERS=2 means two processes run this at startup.
        # create_all checks-then-creates, so the loser could hit "table already
        # exists" and take the whole API down with a STARTUP_FAILURE. Retrying
        # re-inspects and is then a no-op.
        Base.metadata.create_all(bind=engine)
    _ensure_schema()


def _column_exists(table_name: str, column_name: str) -> bool:
    """Fresh (uncached) check that a column is present right now."""
    from sqlalchemy import inspect

    try:
        return column_name in {c["name"] for c in inspect(engine).get_columns(table_name)}
    except Exception:  # noqa: BLE001
        return False


class SchemaMigrationError(RuntimeError):
    """Raised when an auto-migration ALTER could not be applied.

    Fatal on purpose — see `_ensure_schema` for why booting anyway is worse.
    """


def _ensure_schema() -> None:
    """Lightweight auto-migration: add columns that exist on models but not in DB.

    create_all() only creates missing tables; existing installations upgrading to
    a newer schema need the new columns added. Works on SQLite and PostgreSQL
    (both support ALTER TABLE ... ADD COLUMN).

    每条 ALTER 各自一个事务，而且失败必须炸。原来是**一个** ``engine.begin()``
    包住全部 ALTER，加一个 ``except: log.exception`` 继续循环 —— 在 PostgreSQL
    上这两点合起来是最坏的组合：一条语句失败之后事务就 aborted，后面每一条
    ``conn.execute`` 都抛 ``InFailedSqlTransaction``，最后那个隐式 COMMIT 退化成
    ROLLBACK，**这一轮里之前已经加好的列全部被丢掉**，而 ``init_db()`` 照样正常
    返回。

    具体触发器：一个有 DML 权限但不是表 owner 的 Postgres 角色（相当常见的加固
    配置）。每条 ALTER 都是 ``must be owner of table``，只有日志里留一行，API 照常
    起来，然后第一个 ``POST /api/tenants`` 在运行时因为缺列而失败 —— 正是
    CLAUDE.md 记的 0.4.36 那种「看不出是 schema 问题」的故障。

    注意：这里只 ADD COLUMN，从不 ALTER 已有列的 TYPE。所以将来把某个
    ``String(n)`` 改宽，在已升级的 Postgres 实例上是**静默无效**的 —— 列宽还是
    旧的，写入照样 StringDataRightTruncation。0.4.77 的 ``app_meta.key`` 就是这么
    栽的。要改宽必须另外写迁移。
    """
    import logging

    from sqlalchemy import inspect, text

    log = logging.getLogger("ocibot.db")
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    failures: list[str] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing_cols:
                continue
            col_type = col.type.compile(engine.dialect)
            default_sql = ""
            default = getattr(col.default, "arg", None)
            if default is not None and not callable(default):
                if isinstance(default, bool):
                    literal = (
                        ("TRUE" if default else "FALSE")
                        if engine.dialect.name == "postgresql"
                        else ("1" if default else "0")
                    )
                elif isinstance(default, (int, float)):
                    literal = str(default)
                else:
                    literal = "'" + str(default).replace("'", "''") + "'"
                default_sql = f" DEFAULT {literal}"
            # New NOT NULL columns need a default for existing rows; otherwise add as nullable.
            not_null_sql = ""
            if not col.nullable and default_sql:
                not_null_sql = " NOT NULL"
            ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col_type}{default_sql}{not_null_sql}'
            try:
                # 一条 ALTER 一个事务：一条失败不会作废同一轮里已经成功的那些。
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                log.info("schema migration: added %s.%s", table.name, col.name)
            except Exception as exc:  # noqa: BLE001
                # OCIBOT_API_WORKERS 默认是 2，两个进程同时跑 init_db()，输的那个
                # 会撞上 duplicate column。重新查一次（不能用上面那个 inspector，
                # 它的快照是循环开始前拍的）：列已经在了就是并发竞态，不是故障 ——
                # 这种情况下报错反而会让一次正常的滚动重启起不来。
                if _column_exists(table.name, col.name):
                    continue
                log.exception("schema migration failed: %s", ddl)
                failures.append(f"{table.name}.{col.name}: {exc}")

    if failures:
        # 带着缺列启动，换来的是运行时一个说不清原因的 500。宁可在启动就说清楚。
        raise SchemaMigrationError(
            "数据库自动迁移失败，以下列没能加上：\n  - "
            + "\n  - ".join(failures)
            + "\n\n最常见的原因是数据库账号不是表的 owner（ALTER TABLE 需要 owner "
            "权限）。请用 owner 账号执行迁移，或把表的 owner 改成当前账号后重启。"
        )
