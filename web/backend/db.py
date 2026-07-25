"""SQLAlchemy engine / session."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from web.backend.config import get_settings


class Base(DeclarativeBase):
    pass


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
            if raw_path and raw_path != ":memory:":
                Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False
        # SQLite: small pool (often NullPool-ish behavior is fine; keep simple).
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

    Base.metadata.create_all(bind=engine)
    _ensure_schema()


def _ensure_schema() -> None:
    """Lightweight auto-migration: add columns that exist on models but not in DB.

    create_all() only creates missing tables; existing installations upgrading to
    a newer schema need the new columns added. Works on SQLite and PostgreSQL
    (both support ALTER TABLE ... ADD COLUMN).
    """
    import logging

    from sqlalchemy import inspect, text

    log = logging.getLogger("ocibot.db")
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
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
                    conn.execute(text(ddl))
                    log.info("schema migration: added %s.%s", table.name, col.name)
                except Exception:  # noqa: BLE001
                    log.exception("schema migration failed: %s", ddl)
