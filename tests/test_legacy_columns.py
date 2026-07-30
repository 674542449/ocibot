"""Removing a model attribute must not break INSERT on an existing database.

The bug this pins (0.4.36 → fixed in 0.4.39): budget alerts and the daily egress
check were deleted, and their three `Tenant` columns were deleted from the model
with them. Those columns are `nullable=False` with a `default=` that SQLAlchemy
applies **client-side** — so the database column is NOT NULL with no default of
its own. Dropping the attribute stopped the INSERT from supplying a value, and
`_ensure_schema()` never drops columns, so every upgraded install still had them.

Result: "add tenant" failed with a not-null violation surfacing as a bare HTTP
500, while list / edit / delete kept working — an UPDATE does not have to supply
columns it is not changing, only an INSERT does. That asymmetry is what made it
look like the tenant feature specifically had broken.

The rule: a column that exists in deployed databases stays mapped (marked legacy)
until a migration drops it from the database too.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_legacy_")
_DB = Path(_TMP, "legacy.db")
os.environ.setdefault("OCIBOT_MASTER_KEY", "legacy-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "legacy-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from web.backend.db import Base  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402

# Columns that exist in databases created before 0.4.36. Each is NOT NULL with no
# server default, so an INSERT that omits it fails.
_LEGACY_NOT_NULL = (
    ("budget_monthly_usd", "FLOAT"),
    ("budget_notified_month", "VARCHAR(8)"),
    ("egress_notified_month", "VARCHAR(8)"),
)

# A private engine rather than the shared SessionLocal: whichever test module
# imports first wins DATABASE_URL, so binding to the global session made this
# file's schema surgery land in another module's database (or none at all).
_engine = create_engine(f"sqlite+pysqlite:///{_DB.as_posix()}", future=True)
_Session = sessionmaker(bind=_engine, future=True)


@pytest.fixture(scope="module", autouse=True)
def _upgraded_database():
    """A database that predates the 0.4.36 removal, as every real install does."""
    Base.metadata.create_all(bind=_engine)
    con = sqlite3.connect(_DB)
    existing = {row[1] for row in con.execute("PRAGMA table_info(tenants)")}
    for name, sql_type in _LEGACY_NOT_NULL:
        if name not in existing:
            con.execute(f"ALTER TABLE tenants ADD COLUMN {name} {sql_type} NOT NULL")
    con.commit()
    con.close()
    yield


def SessionLocal():  # noqa: N802 - keeps the test bodies reading like the app's
    return _Session()


def _owner_id() -> str:
    with SessionLocal() as db:
        user = User(username=f"legacy-{os.urandom(4).hex()}", password_hash="x")
        db.add(user)
        db.commit()
        return user.id


def test_every_legacy_not_null_column_is_still_mapped():
    """The cheap guard: if someone deletes one of these attributes again, this
    fails immediately with the reason, instead of a 500 in production."""
    mapped = set(Tenant.__table__.columns.keys())
    missing = [name for name, _ in _LEGACY_NOT_NULL if name not in mapped]
    assert not missing, (
        f"{missing} exist as NOT NULL columns in deployed databases but are no "
        "longer mapped, so INSERT will omit them and adding a tenant will fail "
        "with a not-null violation. Keep them mapped, or add a migration that "
        "drops them from the database."
    )


def test_adding_a_tenant_works_on_an_upgraded_database():
    """The behavioural test — this is exactly what returned 500."""
    with SessionLocal() as db:
        db.add(
            Tenant(
                owner_id=_owner_id(),
                name="东京",
                user_ocid="ocid1.user.oc1..legacy",
                tenancy_ocid="ocid1.tenancy.oc1..legacy",
                fingerprint="3f:9a:1c:57",
                region="ap-tokyo-1",
                private_key_encrypted="encrypted",
            )
        )
        db.commit()  # not-null violation before the fix
    with SessionLocal() as db:
        assert db.query(Tenant).filter(Tenant.name == "东京").count() == 1


def test_the_legacy_columns_get_a_value_without_being_named():
    """They must carry a client-side default, since the database has none."""
    with SessionLocal() as db:
        row = Tenant(
            owner_id=_owner_id(),
            name="大阪",
            user_ocid="ocid1.user.oc1..legacy2",
            tenancy_ocid="ocid1.tenancy.oc1..legacy2",
            fingerprint="3f:9a:1c:58",
            region="ap-osaka-1",
            private_key_encrypted="encrypted",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.budget_monthly_usd == 0.0
        assert row.budget_notified_month == ""
        assert row.egress_notified_month == ""


def test_secondary_region_rows_insert_too():
    """副区 rows are created by a separate code path that also INSERTs."""
    owner = _owner_id()
    with SessionLocal() as db:
        primary = Tenant(
            owner_id=owner,
            name="主区",
            user_ocid="ocid1.user.oc1..legacy3",
            tenancy_ocid="ocid1.tenancy.oc1..legacy3",
            fingerprint="3f:9a:1c:59",
            region="ap-tokyo-1",
            private_key_encrypted="encrypted",
        )
        db.add(primary)
        db.commit()
        child = Tenant(
            owner_id=owner,
            name="主区 · 大阪",
            user_ocid=primary.user_ocid,
            tenancy_ocid=primary.tenancy_ocid,
            fingerprint=primary.fingerprint,
            region="ap-osaka-1",
            private_key_encrypted=primary.private_key_encrypted,
            parent_tenant_id=primary.id,
        )
        db.add(child)
        db.commit()
        assert child.parent_tenant_id == primary.id
