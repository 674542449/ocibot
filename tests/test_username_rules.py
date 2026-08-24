"""Username charset / NFKC rules, and the audit-write ceiling on a login flood.

Registration used to do ``.strip()`` and a 3–64 length check, nothing else. These
all registered successfully alongside ``admin``:

    ad\\nmin      admin\\u200b (ZWSP)      аdmin (Cyrillic а)      ａdmin (fullwidth)

Five accounts, four of them still distinct after NFKC. There is no auth bypass —
login and the uniqueness check use the same codepoint-exact comparison — but the
names land verbatim in ``audit_logs.target``, so an admin reading the audit table
cannot tell which account a row belongs to, which is most of what an audit table
is for.

The second half of this module is the audit-write ceiling: the login limiter
returns False *without* recording a hit, so the 30/5min IP cap throttled guesses
while placing no bound at all on how many audit rows an unauthenticated flood
could write.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_uname_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'a.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "uname-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "uname-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend.auth import hash_password  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.meta import KEY_OPEN_REGISTRATION, get_meta, set_meta  # noqa: E402
from web.backend.models import AuditLog, User  # noqa: E402
from web.backend.rate_limit import (  # noqa: E402
    login_ip_limiter,
    login_user_limiter,
    register_ip_limiter,
)
from web.backend.routers.auth import (  # noqa: E402
    _login_blocked_audit_limiter,
    audit_username,
    normalize_username,
    username_error,
)

_PASSWORD = "supersecret123"


def _reset_limiters() -> None:
    """These limiters are process-wide singletons shared by every test module in
    the session — burning a bucket here would 429 someone else's login."""
    for limiter in (
        login_ip_limiter,
        login_user_limiter,
        register_ip_limiter,
        _login_blocked_audit_limiter,
    ):
        with limiter._lock:  # noqa: SLF001 - test-only reset of shared state
            limiter._hits.clear()  # noqa: SLF001


@pytest.fixture(scope="module", autouse=True)
def open_registration():
    init_db()
    with SessionLocal() as db:
        previous = get_meta(db, KEY_OPEN_REGISTRATION)
        set_meta(db, KEY_OPEN_REGISTRATION, "1")
        db.commit()
    _reset_limiters()
    try:
        yield
    finally:
        with SessionLocal() as db:
            set_meta(db, KEY_OPEN_REGISTRATION, previous if previous is not None else "")
            db.commit()
        _reset_limiters()


def _register(client: TestClient, username: str):
    # The register throttle is 5 per 10 minutes per IP and every case below shares
    # one IP, so it would otherwise decide the outcome instead of the charset rule.
    _reset_limiters()
    return client.post("/api/auth/register", json={"username": username, "password": _PASSWORD})


def _seed(username: str) -> str:
    with SessionLocal() as db:
        row = db.query(User).filter(User.username == username).one_or_none()
        if row is None:
            row = User(username=username, password_hash=hash_password(_PASSWORD))
            db.add(row)
        row.password_hash = hash_password(_PASSWORD)
        row.is_active = True
        row.totp_enabled = False
        db.commit()
        return row.id


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_nfkc_folds_the_compatibility_spellings():
    assert normalize_username("ａdmin") == "admin"
    assert normalize_username("  admin  ") == "admin"
    # NFKC does NOT strip zero-width characters; the charset rule has to.
    assert normalize_username("admin​") != "admin"


@pytest.mark.parametrize(
    "name",
    [
        "ad\nmin",       # control character
        "ad\tmin",
        "admin​",   # zero-width space
        "аdmin",         # Cyrillic а
        "admin‮",   # right-to-left override
        "admin user",    # space
        "_admin",        # must start with a letter or digit
        "ad",            # too short
    ],
)
def test_these_names_are_refused(name):
    assert username_error(name) != ""


@pytest.mark.parametrize("name", ["admin", "ops.team", "user_1", "a-b-c", "Admin2"])
def test_these_names_are_accepted(name):
    assert username_error(name) == ""


def test_an_invisible_character_is_escaped_for_the_audit_table():
    """Rendered verbatim, this row is indistinguishable from one about `admin`."""
    assert audit_username("admin​") == "admin\\u200b"
    assert audit_username("ａdmin") == "ａdmin (NFKC=admin)"
    assert audit_username("admin") == "admin"


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["uname\nspoof", "unamespoof​", "unamespооf", "uname spoof"],
)
def test_registration_refuses_a_confusable_name(name):
    with TestClient(app) as c:
        r = _register(c, name)
    assert r.status_code == 400, r.text
    assert "用户名" in (r.json().get("detail") or "")


def test_a_fullwidth_name_is_stored_folded_not_as_a_second_account():
    with TestClient(app) as c:
        r = _register(c, "ｕnamefold")
    assert r.status_code == 201, r.text
    assert r.json()["username"] == "unamefold"
    with SessionLocal() as db:
        assert db.query(User).filter(User.username == "ｕnamefold").count() == 0
        assert db.query(User).filter(User.username == "unamefold").count() == 1
    # And the folded spelling can no longer be claimed a second time.
    with TestClient(app) as c:
        again = _register(c, "ｕnamefold")
    assert again.status_code == 400


def test_case_only_variants_cannot_be_registered_alongside_each_other():
    with TestClient(app) as c:
        assert _register(c, "unamecase").status_code == 201
        clash = _register(c, "UnameCase")
    assert clash.status_code == 400
    assert "已存在" in (clash.json().get("detail") or "")


# --------------------------------------------------------------------------
# Login must not lock anyone out
# --------------------------------------------------------------------------


def test_an_account_created_before_the_rule_can_still_log_in():
    """The charset rule applies to registration only. Applying it at login would
    lock out every account whose name predates it."""
    legacy = "uname​legacy"
    _seed(legacy)
    assert username_error(legacy) != "", "precondition: this name would fail the new rule"
    _reset_limiters()
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": legacy, "password": _PASSWORD})
    assert r.status_code == 200, r.text


def test_the_exact_spelling_always_wins_over_the_folded_one():
    """Two accounts that fold together must each keep reaching their own row."""
    _seed("unameboth")
    _seed("ｕnameboth")
    _reset_limiters()
    with TestClient(app) as c:
        for name in ("unameboth", "ｕnameboth"):
            r = c.post("/api/auth/login", json={"username": name, "password": _PASSWORD})
            assert r.status_code == 200, r.text
            assert r.json()["username"] == name


def test_a_failed_login_records_an_escaped_target():
    _reset_limiters()
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "unameghost​", "password": "nope"})
    assert r.status_code == 401
    with SessionLocal() as db:
        targets = [
            row.target
            for row in db.query(AuditLog).filter(AuditLog.action == "auth.login_failed").all()
        ]
    assert "unameghost\\u200b" in targets
    assert "unameghost​" not in targets


# --------------------------------------------------------------------------
# Audit-write ceiling
# --------------------------------------------------------------------------


def _blocked_rows() -> int:
    with SessionLocal() as db:
        return db.query(AuditLog).filter(AuditLog.action == "auth.login_blocked").count()


def test_a_login_flood_cannot_write_one_audit_row_per_request():
    """90 unauthenticated requests used to mean 90 rows, and combined with the
    row-cap pruning added in pass 10 that erases the history around the flood."""
    _reset_limiters()
    before = _blocked_rows()
    blocked_responses = 0
    with TestClient(app) as c:
        for _ in range(60):
            r = c.post("/api/auth/login", json={"username": "unameflood", "password": "nope"})
            if r.status_code == 429:
                blocked_responses += 1
    written = _blocked_rows() - before
    _reset_limiters()

    assert blocked_responses >= 40, "precondition: the limiter has to actually trip"
    # One row per IP per window is enough to answer "is this address being sprayed".
    assert written <= 2, f"{blocked_responses} blocked requests wrote {written} audit rows"
    assert written >= 1, "the event still has to be recorded at least once"


def test_the_recorded_block_says_that_later_ones_are_suppressed():
    """Otherwise the single row reads as a single blocked attempt."""
    with SessionLocal() as db:
        row = (
            db.query(AuditLog)
            .filter(AuditLog.action == "auth.login_blocked")
            .order_by(AuditLog.created_at.desc())
            .first()
        )
    assert row is not None
    assert int(json.loads(row.detail or "{}").get("suppress_window_sec") or 0) > 0
