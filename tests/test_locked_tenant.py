"""Default tenant ("锁定为默认") is stored per account, not per browser.

It used to live in localStorage, which meant it did not exist on a second
browser, a phone, or a private window — the operator had to set it again
everywhere. It is a property of the person, so it belongs on the account and
travels with the session.

What has to hold:
  - it survives a completely fresh client (no shared storage): a new HTTP client
    with only the session cookie sees the same default;
  - one account cannot set or observe another's;
  - deleting the tenant clears it, rather than leaving every page silently
    falling through to "first tenant" with nothing explaining why.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_lock_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'l.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "lock-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "lock-jwt-secret-0123456789abcdef")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from web.backend.auth import hash_password  # noqa: E402
from web.backend.crypto_util import encrypt_text  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import Tenant, User  # noqa: E402

_PEM = "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END PRIVATE KEY-----"
_PW = "supersecret123"


def _mk_user(name: str) -> tuple[str, str]:
    """Create a user with one tenant; returns (user_id, tenant_id)."""
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == name).one_or_none()
        if user is None:
            user = User(username=name, password_hash=hash_password(_PW))
            db.add(user)
            db.flush()
        tenant = db.query(Tenant).filter(Tenant.owner_id == user.id).first()
        if tenant is None:
            tenant = Tenant(
                owner_id=user.id,
                name=f"T-{name}",
                region="ap-tokyo-1",
                private_key_encrypted=encrypt_text(_PEM),
            )
            db.add(tenant)
        db.commit()
        return user.id, tenant.id


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    yield


def _login(c: TestClient, name: str) -> None:
    # The login limiter is in-process and keyed on the client IP, which every
    # test in the suite shares. Signing in here would otherwise eat budget that
    # another module needs, failing whichever one happens to run next. The
    # limiter's own behaviour is covered by tests/test_login_audit.py.
    from web.backend.rate_limit import login_ip_limiter, login_user_limiter

    login_ip_limiter._hits.clear()
    login_user_limiter._hits.clear()
    r = c.post("/api/auth/login", json={"username": name, "password": _PW})
    assert r.status_code == 200, r.text


# Module-scoped clients on purpose: the login limiter is per IP and every test
# in the suite shares "testclient", so a client per test drains the budget and
# fails whichever module happens to run next.
@pytest.fixture(scope="module")
def ids(_db):
    return {"a": _mk_user("lock-a"), "b": _mk_user("lock-b"), "c": _mk_user("lock-c")}


@pytest.fixture(scope="module")
def ca(ids):
    with TestClient(app) as c:
        _login(c, "lock-a")
        yield c


@pytest.fixture(scope="module")
def cb(ids):
    with TestClient(app) as c:
        _login(c, "lock-b")
        yield c


@pytest.fixture(autouse=True)
def _clear(ca, ids):
    ca.put("/api/auth/locked-tenant", json={"tenant_id": ""})
    yield


def test_default_is_empty_for_a_new_account(ca):
    assert ca.get("/api/auth/me").json()["locked_tenant_id"] == ""


def test_it_persists_and_is_returned_with_the_session(ca, ids):
    tid = ids["a"][1]
    r = ca.put("/api/auth/locked-tenant", json={"tenant_id": tid})
    assert r.status_code == 200, r.text
    assert r.json()["locked_tenant_id"] == tid
    assert ca.get("/api/auth/me").json()["locked_tenant_id"] == tid


def test_a_fresh_client_sees_the_same_default(ca, ids):
    """The whole point: a second browser shares no storage with the first, only
    the account. If this fails the setting is still browser-local."""
    tid = ids["a"][1]
    ca.put("/api/auth/locked-tenant", json={"tenant_id": tid})
    with TestClient(app) as second:  # new client, new cookie jar
        _login(second, "lock-a")
        assert second.get("/api/auth/me").json()["locked_tenant_id"] == tid


def test_empty_string_clears_it(ca, ids):
    ca.put("/api/auth/locked-tenant", json={"tenant_id": ids["a"][1]})
    assert ca.put("/api/auth/locked-tenant", json={"tenant_id": ""}).json()["locked_tenant_id"] == ""


def test_another_accounts_tenant_is_refused(ca, ids):
    """And with 404, not 403 — an id belonging to someone else must not be
    distinguishable from one that does not exist."""
    r = ca.put("/api/auth/locked-tenant", json={"tenant_id": ids["b"][1]})
    assert r.status_code == 404, r.text
    assert ca.get("/api/auth/me").json()["locked_tenant_id"] == ""


def test_an_unknown_tenant_is_refused(ca):
    assert ca.put("/api/auth/locked-tenant", json={"tenant_id": "no-such-tenant"}).status_code == 404


def test_accounts_do_not_share_a_default(ca, cb, ids):
    ca.put("/api/auth/locked-tenant", json={"tenant_id": ids["a"][1]})
    cb.put("/api/auth/locked-tenant", json={"tenant_id": ids["b"][1]})
    assert ca.get("/api/auth/me").json()["locked_tenant_id"] == ids["a"][1]
    assert cb.get("/api/auth/me").json()["locked_tenant_id"] == ids["b"][1]


def test_deleting_the_tenant_clears_the_default(ca, ids):
    """Otherwise every page keeps falling through to the first tenant with
    nothing in the UI saying why."""
    # lock-c exists only for this test, since it destroys its tenant.
    with TestClient(app) as c:
        _login(c, "lock-c")
        tid = ids["c"][1]
        c.put("/api/auth/locked-tenant", json={"tenant_id": tid})
        assert c.delete(f"/api/tenants/{tid}").status_code == 200
        assert c.get("/api/auth/me").json()["locked_tenant_id"] == ""


def test_it_requires_a_session():
    with TestClient(app) as anon:
        assert anon.put("/api/auth/locked-tenant", json={"tenant_id": "x"}).status_code == 401


# --------------------------------------------------------------------------
# Frontend guard. The self-heal that clears a vanished lock lives in
# web/frontend/src/stores/tenantLock.ts, and there is no JS test runner in this
# repo, so the invariant is asserted against the source instead.
# --------------------------------------------------------------------------

_FRONTEND = Path(__file__).resolve().parents[1] / "web" / "frontend" / "src"


def _call_args(text: str, start: int) -> str:
    """Return the argument text of the pickTenantId( call starting at `start`."""
    depth, i = 0, start
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i]
        i += 1
    return ""


def test_filtered_tenant_lists_pass_the_full_list_to_pick():
    """A page that offers only ENABLED tenants must still judge "is the locked
    tenant gone?" against every tenant.

    Otherwise locking a tenant and then disabling it means that merely opening
    创建实例 or 存储 clears the lock — server-side, permanently, since 0.4.58 —
    while opening 账户 or 实例 (which pass the unfiltered list) keeps it. Whether
    a saved default survived came down to which page was clicked first.
    """
    offenders = []
    for path in sorted(_FRONTEND.glob("views/*.vue")):
        src = path.read_text(encoding="utf-8")
        idx = src.find("pickTenantId(")
        if idx < 0:
            continue
        args = _call_args(src, src.index("(", idx))
        # Only pages that narrow the list need the third argument.
        if ".filter(" not in src.split("pickTenantId(")[0][-600:]:
            continue
        if args.count(",") < 2:
            offenders.append(f"{path.name}: pickTenantId({args.strip()}) — 缺少完整租户列表参数")
    assert not offenders, "\n".join(offenders)


def test_selfheal_requires_absence_from_the_known_list():
    """The clear must be conditional on the tenant being absent from `known`,
    not merely on `known` being non-empty."""
    store = (_FRONTEND / "stores" / "tenantLock.ts").read_text(encoding="utf-8")
    assert "known.some((t) => t.id === lockedId.value)" in store
    assert "if (known.length && !known.some" in store
