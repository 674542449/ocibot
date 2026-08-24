"""WebSSH bridge hardening.

Everything here guards a property that failed silently in production rather than
loudly in a log: an unbounded stdin queue that OOMs the SHARED api process, a
strict-UTF-8 channel that a single ``cat /bin/ls`` tears down connection-wide,
guest output able to forge the panel's own control messages, and OCI request
budget spent before any throttle applies.

The host-key ordering invariant (probe -> verify -> connect) is re-asserted at the
bottom: these tests move code around the handler, and reordering it would hand the
user's credentials to whatever answered on the address.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ocibot_webssh_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'ws.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "webssh-test-master-key-0123456789abcdef")
os.environ.setdefault("OCIBOT_JWT_SECRET", "webssh-test-jwt-secret-0123456789abcdef")
sys.path.insert(0, os.path.abspath("."))

import pytest  # noqa: E402

pytest.importorskip("fastapi")
pytest.importorskip("asyncssh")

import asyncssh  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import web.backend.routers.webssh as webssh  # noqa: E402
from web.backend.auth import COOKIE_NAME, create_access_token  # noqa: E402
from web.backend.db import SessionLocal, init_db  # noqa: E402
from web.backend.main import app  # noqa: E402
from web.backend.models import SshHostKey, Tenant, User  # noqa: E402
from web.backend.rate_limit import SlidingWindowLimiter  # noqa: E402

INSTANCE_ID = "ocid1.instance.oc1..webssh"
_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nZmFrZQ==\n-----END OPENSSH PRIVATE KEY-----"


# --------------------------------------------------------------------------- #
# Fake SSH plumbing
# --------------------------------------------------------------------------- #


class _FakeKey:
    """Stands in for an asyncssh SSHKey (never leaves the process)."""

    def __init__(self, fingerprint: str = "SHA256:WEBSSH", algorithm: bytes = b"ssh-ed25519"):
        self._fp = fingerprint
        self.algorithm = algorithm

    def get_fingerprint(self) -> str:
        return self._fp


class _FakeStdin:
    """Mirrors asyncssh's channel encoder, which is the point of several tests.

    With an encoding set the channel accepts only ``str`` and raises TypeError on
    bytes (``utf_8_encode() argument 1 must be str``); with encoding=None it is
    the other way round. Faking a stdin that swallows both would make the
    byte-transparency tests pass against the very code they exist to catch.
    """

    def __init__(self) -> None:
        self.writes: list = []
        self.drains = 0
        self.broken = False
        self.encoding = "utf-8"

    def write(self, data) -> None:
        if self.broken:
            raise BrokenPipeError("stdin closed")
        if self.encoding is None and not isinstance(data, bytes):
            raise TypeError("a bytes-like object is required, not 'str'")
        if self.encoding is not None and not isinstance(data, str):
            raise TypeError("utf_8_encode() argument 1 must be str, not bytes")
        self.writes.append(data)

    async def drain(self) -> None:
        self.drains += 1

    def write_eof(self) -> None:
        pass


class _FakeReader:
    """Yields queued chunks, then either EOF or parks like a live shell.

    Chunks are always stored as bytes; ``encoding`` decides what the reader hands
    back, and a strict decode failure raises the way asyncssh's ProtocolError does
    — connection-level, not channel-level.
    """

    def __init__(self, chunks=None, hold: bool = True) -> None:
        self._chunks = list(chunks or [])
        self._hold = hold
        self.encoding: object = "utf-8"

    async def read(self, n: int = -1):
        if self._chunks:
            data = self._chunks.pop(0)
            if self.encoding is None:
                return data
            return data.decode(str(self.encoding))  # errors='strict', as asyncssh
        if self._hold:
            await asyncio.sleep(3600)  # cancelled when the session tears down
        return b"" if self.encoding is None else ""


class _FakeProcess:
    def __init__(self, stdout_chunks=None, stdout_hold=True) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeReader(stdout_chunks, hold=stdout_hold)
        self.stderr = _FakeReader(hold=True)
        self.sizes: list = []
        self.closed = False

    def apply_encoding(self, encoding) -> None:
        self.stdin.encoding = encoding
        self.stdout.encoding = encoding
        self.stderr.encoding = encoding

    def change_terminal_size(self, cols, rows) -> None:
        self.sizes.append((cols, rows))

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class _FakeConn:
    def __init__(self, rec: dict, proc: _FakeProcess) -> None:
        self._rec = rec
        self._proc = proc

    async def create_process(self, **kwargs):
        self._rec["create_process"] = kwargs
        # asyncssh's default is encoding='utf-8', errors='strict'.
        self._proc.apply_encoding(kwargs.get("encoding", "utf-8"))
        return self._proc

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class Harness:
    """Records what the handler did, and exposes the fake process to the test."""

    def __init__(self) -> None:
        self.rec: dict = {}
        self.process = _FakeProcess()
        self.probe_calls: list = []
        self.get_instance_calls = 0


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with SessionLocal() as db:
        db.query(SshHostKey).delete()
        db.query(Tenant).delete()
        db.query(User).delete()
        db.commit()
    yield


@pytest.fixture
def harness(monkeypatch):
    h = Harness()

    async def _fake_connect(**kwargs):
        h.rec["connect"] = kwargs
        return _FakeConn(h.rec, h.process)

    async def _fake_probe(host, port=22, *, timeout=20.0, prefer_key_type=""):
        h.probe_calls.append({"host": host, "port": port, "prefer_key_type": prefer_key_type})
        return _FakeKey()

    monkeypatch.setattr(asyncssh, "connect", _fake_connect)
    monkeypatch.setattr(asyncssh, "import_private_key", lambda pem: "parsed-key")
    monkeypatch.setattr(webssh, "probe_host_key", _fake_probe)

    def _get_instance(instance_id, resolve_ips=False):
        h.get_instance_calls += 1
        return SimpleNamespace(
            id=instance_id,
            display_name="web-1",
            lifecycle_state="RUNNING",
            public_ip="203.0.113.7",
            private_ip="10.0.0.7",
            ipv6_addresses=[],
        )

    monkeypatch.setattr(
        webssh, "get_session_for_row", lambda row: SimpleNamespace(get_instance=_get_instance)
    )
    # A fresh limiter per test: the module-level one is process-global and would
    # otherwise carry hits between tests. raising=False so that a build without the
    # limiter fails on each test's own assertion instead of erroring in the fixture.
    monkeypatch.setattr(
        webssh, "_handshake_limiter", SlidingWindowLimiter(max_hits=50, window_sec=60), raising=False
    )
    monkeypatch.setattr(webssh, "_user_sessions", {})
    monkeypatch.setattr(webssh, "_instance_sessions", {})
    return h


@pytest.fixture
def client():
    with SessionLocal() as db:
        user = User(username="wsuser", password_hash="x")
        db.add(user)
        db.flush()
        tenant = Tenant(owner_id=user.id, name="T", region="ap-tokyo-1", private_key_encrypted="")
        db.add(tenant)
        db.commit()
        token = create_access_token(
            user_id=user.id, username=user.username, token_version=int(user.token_version or 1)
        )
        tenant_id = tenant.id
    with TestClient(app) as c:
        c.cookies.set(COOKIE_NAME, token)
        c.tenant_id = tenant_id  # type: ignore[attr-defined]
        yield c


def _url(client) -> str:
    return f"/api/tenants/{client.tenant_id}/instances/{INSTANCE_ID}/webssh"


_RECV_TIMEOUT = 5.0


def _recv(ws, timeout: float = _RECV_TIMEOUT) -> dict:
    """``ws.receive()`` with a deadline.

    Several failures here look like "the browser was left hanging": a pump dies,
    nothing is sent, and the socket stays open. Without a deadline that symptom
    is a test run that never finishes instead of a red test. The reader runs on a
    daemon thread so a stuck receive cannot hold up interpreter exit.
    """
    box: list = []

    def _run() -> None:
        try:
            box.append(ws.receive())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            box.append(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if not box:
        raise AssertionError(f"no frame within {timeout}s — the client was left hanging")
    got = box[0]
    if isinstance(got, BaseException):
        raise got
    return got


def _recv_json(ws, timeout: float = _RECV_TIMEOUT) -> dict:
    frame = _recv(ws, timeout)
    assert frame.get("text") is not None, f"expected a TEXT control frame, got {frame!r}"
    return json.loads(frame["text"])


def _recv_bytes(ws, timeout: float = _RECV_TIMEOUT) -> bytes:
    frame = _recv(ws, timeout)
    assert frame.get("bytes") is not None, f"expected a BINARY frame, got {frame!r}"
    return frame["bytes"]


def _connect(client, ws):
    """Run the handshake up to 'connected'; returns the connected payload."""
    while True:
        msg = _recv_json(ws)
        if msg.get("type") == "ready":
            ws.send_text(json.dumps({"username": "ubuntu", "port": 22, "private_key_pem": _PEM}))
            continue
        if msg.get("type") == "hostkey":
            continue
        if msg.get("type") == "connected":
            return msg
        raise AssertionError(f"unexpected handshake frame: {msg}")


# --------------------------------------------------------------------------- #
# 1 — unbounded client->SSH buffering
# --------------------------------------------------------------------------- #


def test_stdin_write_is_drained(client, harness):
    """Without drain() asyncssh queues writes in an uncapped buffer.

    A client that keeps sending while the remote shell has stopped reading grows
    the shared api process's RSS at network speed until the panel OOMs for every
    user — _MAX_PER_USER bounds sessions, not bytes.
    """
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
        ws.send_text("echo hi\n")
        ws.send_text("echo there\n")
        ws.send_text(json.dumps({"type": "ping"}))
        assert _recv_json(ws)["type"] == "pong"

    assert len(harness.process.stdin.writes) == 2
    assert harness.process.stdin.drains == 2, "every stdin write must be followed by drain()"


def test_oversized_frame_is_refused(client, harness):
    """One frame must not be able to be a payload; uvicorn's cap is 16 MiB."""
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
        ws.send_text("A" * (webssh._MAX_FRAME_BYTES + 1))
        msg = _recv_json(ws)
    assert msg["type"] == "error"
    assert "过大" in msg["message"]
    assert harness.process.stdin.writes == [], "oversized frame must never reach the channel"


def test_oversized_auth_frame_is_refused(client, harness):
    with client.websocket_connect(_url(client)) as ws:
        assert _recv_json(ws)["type"] == "ready"
        ws.send_text("{" + "A" * (webssh._MAX_FRAME_BYTES + 1))
        msg = _recv_json(ws)
    assert msg["type"] == "error" and "过大" in msg["message"]


# --------------------------------------------------------------------------- #
# 2 — byte transparency (non-UTF-8 guest output must not kill the connection)
# --------------------------------------------------------------------------- #


def test_session_is_byte_transparent(client, harness):
    """encoding=None, or asyncssh decodes with errors='strict'.

    A strict decode failure raises ProtocolError, which is a CONNECTION-level
    disconnect: one `cat /bin/ls` or a latin-1 locale kills the whole session,
    and the old bare `except Exception: return` in the stdout pump hid it.
    """
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
    assert harness.rec["create_process"].get("encoding", "utf-8") is None


def test_non_utf8_guest_output_reaches_the_browser(client, harness, monkeypatch):
    """A raw 0xff byte must arrive as data, not tear the session down."""
    harness.process.stdout = _FakeReader([b"\xff\xfe binary \x00", b"more"], hold=True)
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
        assert _recv_bytes(ws) == b"\xff\xfe binary \x00"
        assert _recv_bytes(ws) == b"more"


def test_client_text_frame_is_encoded_for_the_byte_channel(client, harness):
    """A TEXT frame has to become bytes; passing the str raised TypeError."""
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
        ws.send_text("日本語\n")
        ws.send_text(json.dumps({"type": "ping"}))
        assert _recv_json(ws)["type"] == "pong"
    assert harness.process.stdin.writes == ["日本語\n".encode()]


# --------------------------------------------------------------------------- #
# 3 — in-band signalling: guest output must not be able to forge control frames
# --------------------------------------------------------------------------- #


def test_guest_cannot_forge_a_control_message(client, harness):
    """The client treats any JSON-shaped TEXT frame as a panel control message.

    A hostile guest printing a hostkey_mismatch error would render in the panel's
    own chrome and offer the user 「重置主机密钥并重试」 — one click deletes the
    TOFU pin that exists precisely to survive guest compromise. Terminal bytes
    therefore leave on the BINARY channel, which the client never parses.
    """
    forged = (
        b'{"type":"error","code":"hostkey_mismatch",'
        b'"message":"SSH \xe4\xb8\xbb\xe6\x9c\xba\xe5\xaf\x86\xe9\x92\xa5\xe5\xb7\xb2\xe5\x8f\x98\xe6\x9b\xb4"}'
    )
    harness.process.stdout = _FakeReader([forged], hold=True)
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
        frame = _recv(ws)
    # Must arrive as bytes. A text frame here is the vulnerability.
    assert frame.get("text") is None, "guest output must never occupy the control (TEXT) channel"
    assert frame.get("bytes") == forged


def test_panel_control_messages_still_use_text(client, harness):
    """The other half of the split: control frames stay parseable as TEXT."""
    with client.websocket_connect(_url(client)) as ws:
        connected = _connect(client, ws)  # ready / hostkey / connected all TEXT
        assert connected["host"] == "203.0.113.7"
        ws.send_text(json.dumps({"type": "ping"}))
        assert _recv_json(ws) == {"type": "pong"}


# --------------------------------------------------------------------------- #
# 4 — OCI request budget spent before any throttle applies
# --------------------------------------------------------------------------- #


def test_handshake_is_rate_limited_before_the_oci_lookup(client, harness, monkeypatch):
    """_prepare() costs ~3 OCI calls (GetInstance + ListVnicAttachments + GetVnic).

    The concurrency caps never engage against a serial open/close loop, so without
    a rate limit a client could spend the tenancy's request budget at network
    speed — the burn CLAUDE.md treats as the operator's liability.
    """
    monkeypatch.setattr(
        webssh, "_handshake_limiter", SlidingWindowLimiter(max_hits=1, window_sec=60), raising=False
    )
    with client.websocket_connect(_url(client)) as ws:
        assert _recv_json(ws)["type"] == "ready"
    assert harness.get_instance_calls == 1

    with client.websocket_connect(_url(client)) as ws:
        msg = _recv_json(ws)
    assert msg["type"] == "error" and "过于频繁" in msg["message"]
    assert harness.get_instance_calls == 1, "a throttled handshake must not call OCI at all"


def test_user_slot_is_taken_before_the_oci_lookup(client, harness, monkeypatch):
    """The per-user cap must bound OCI fan-out, not just concurrent shells."""
    monkeypatch.setattr(webssh, "_user_sessions", {})
    with SessionLocal() as db:
        user_id = db.query(User).one().id
    for _ in range(webssh._MAX_PER_USER):
        assert await_sync(webssh._acquire_user_slot(user_id)) is None
    with client.websocket_connect(_url(client)) as ws:
        msg = _recv_json(ws)
    assert msg["type"] == "error" and "过多" in msg["message"]
    assert harness.get_instance_calls == 0


def test_instance_slot_is_taken_only_after_ownership(client, harness, monkeypatch):
    """The per-instance counter is global across users.

    Taking it before the ownership check would let any logged-in user who knows an
    OCID park sessions on somebody else's instance and lock its owner out.
    """
    seen: dict = {}

    async def _spy(instance_id):
        seen["calls"] = seen.get("calls", 0) + 1
        seen["oci_at_call"] = harness.get_instance_calls
        return None

    monkeypatch.setattr(webssh, "_acquire_instance_slot", _spy)
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
    assert seen["calls"] == 1
    assert seen["oci_at_call"] == 1, "instance slot must be claimed after the ownership/OCI lookup"


def test_slots_are_released_on_every_exit_path(client, harness, monkeypatch):
    """Slots are taken earlier now; a leak would wedge the user at 3 sessions."""
    monkeypatch.setattr(
        webssh, "_handshake_limiter", SlidingWindowLimiter(max_hits=50, window_sec=60), raising=False
    )
    for _ in range(webssh._MAX_PER_USER + 2):
        with client.websocket_connect(_url(client)) as ws:
            _connect(client, ws)
    assert webssh._user_sessions == {}
    assert webssh._instance_sessions == {}


def test_slot_released_when_the_target_is_not_owned(client, harness):
    """The early-return paths between the two acquisitions must not leak either."""
    url = f"/api/tenants/does-not-exist/instances/{INSTANCE_ID}/webssh"
    with client.websocket_connect(url) as ws:
        msg = _recv_json(ws)
    assert msg["type"] == "error"
    assert webssh._user_sessions == {}
    assert webssh._instance_sessions == {}


# --------------------------------------------------------------------------- #
# 5 — single-fingerprint pin -> false "possible MITM"
# --------------------------------------------------------------------------- #


def test_probe_requests_the_pinned_key_type_first():
    """Dual-key hosts must not flip to MISMATCH when the negotiation order moves.

    asyncssh's default order puts RSA ahead of ed25519; an upgrade that reorders
    get_default_public_key_algs() would otherwise flip EVERY pinned instance to
    「这可能是中间人攻击」 right after the panel's own one-click update.
    """
    from web.backend.ssh_hostkey import host_key_alg_order

    order = host_key_alg_order("ssh-ed25519")
    assert order, "a pinned key type must produce an explicit preference list"
    assert order[0] == "ssh-ed25519"
    # The rest stay available so a host that genuinely dropped the type still
    # completes the KEX and is reported as a mismatch, not as "unreachable".
    assert len(order) > 1
    assert len(set(order)) == len(order)


def test_rsa_pin_asks_for_the_modern_signature_algorithms():
    """`ssh-rsa` is the KEY type; sshd only offers it under rsa-sha2-*.

    Requesting the stored string verbatim would fail the KEX against the very
    server the fingerprint was learned from.
    """
    from web.backend.ssh_hostkey import host_key_alg_order

    order = host_key_alg_order("ssh-rsa")
    assert order[0].startswith("rsa-sha2-")
    assert "ssh-rsa" in order


def test_unknown_or_missing_key_type_falls_back_to_defaults():
    from web.backend.ssh_hostkey import host_key_alg_order

    assert host_key_alg_order("") == []
    assert host_key_alg_order("not-a-real-alg") == []


def test_probe_passes_the_preference_to_asyncssh(monkeypatch):
    from web.backend import ssh_hostkey

    seen: dict = {}

    async def _fake(host, **kwargs):
        seen.update(kwargs)
        seen["host"] = host
        return _FakeKey()

    monkeypatch.setattr(asyncssh, "get_server_host_key", _fake)
    asyncio.run(ssh_hostkey.probe_host_key("h", 22, prefer_key_type="ssh-ed25519"))
    assert seen["server_host_key_algs"][0] == "ssh-ed25519"

    seen.clear()
    asyncio.run(ssh_hostkey.probe_host_key("h", 22))
    assert "server_host_key_algs" not in seen, "no pin yet -> asyncssh's own order"


def test_webssh_probes_with_the_remembered_key_type(client, harness):
    """Second connect must ask for the same type the first one learned."""
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
    assert harness.probe_calls[0]["prefer_key_type"] == ""  # nothing learned yet

    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
    assert harness.probe_calls[1]["prefer_key_type"] == "ssh-ed25519"


def test_key_type_change_is_named_in_the_mismatch_message():
    """A rotated key type reads as a plain "possible MITM" without this."""
    from web.backend.ssh_hostkey import MISMATCH, HostKeyCheck

    msg = HostKeyCheck(
        verdict=MISMATCH,
        fingerprint="SHA256:B",
        key_type="ssh-rsa",
        expected="SHA256:A",
        expected_key_type="ssh-ed25519",
    ).message()
    assert "ssh-ed25519" in msg and "ssh-rsa" in msg
    assert "密钥类型" in msg


def test_check_instance_host_key_reuses_the_pinned_type(monkeypatch):
    from web.backend import ssh_hostkey

    with SessionLocal() as db:
        user = User(username="hkpin", password_hash="x")
        db.add(user)
        db.commit()
        owner_id = user.id

    seen: list = []

    async def _probe(host, port=22, *, timeout=20.0, prefer_key_type=""):
        seen.append(prefer_key_type)
        return _FakeKey()

    monkeypatch.setattr(ssh_hostkey, "probe_host_key", _probe)
    for _ in range(2):
        with SessionLocal() as db:
            ssh_hostkey.check_instance_host_key(
                db, owner_id=owner_id, instance_id=INSTANCE_ID, host="203.0.113.7", port=22
            )
    assert seen == ["", "ssh-ed25519"]


# --------------------------------------------------------------------------- #
# 6 — a binary client frame must not surface as an internal error
# --------------------------------------------------------------------------- #


def test_binary_client_frame_is_forwarded_verbatim(client, harness):
    """Used to raise TypeError out of the receive loop as "WebSSH 内部错误"."""
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
        ws.send_bytes(b"\x1b[A")
        ws.send_text(json.dumps({"type": "ping"}))
        assert _recv_json(ws)["type"] == "pong"
    assert harness.process.stdin.writes == [b"\x1b[A"]


def test_binary_frames_bypass_the_control_sniff(client, harness):
    """So a client can send keystrokes as binary and still type JSON literally."""
    payload = json.dumps({"type": "resize", "cols": 999, "rows": 999}).encode()
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
        ws.send_bytes(payload)
        ws.send_text(json.dumps({"type": "ping"}))
        assert _recv_json(ws)["type"] == "pong"
    assert harness.process.stdin.writes == [payload]
    assert harness.process.sizes == [], "a binary frame must not be read as a resize"


def test_dead_stdin_ends_the_session_cleanly(client, harness):
    """Any write failure means the session is over, not a panel fault."""
    harness.process.stdin.broken = True
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)
        ws.send_text("x")
        msg = _recv_json(ws)
    assert msg["type"] == "error"
    assert "内部错误" not in msg["message"]


# --------------------------------------------------------------------------- #
# 7 — no unverified SSH helper left lying around
# --------------------------------------------------------------------------- #


def _code_only(src: str) -> str:
    """Source with whole-line ``#`` comments dropped.

    The tombstone explaining why the unverified helper was deleted necessarily
    quotes the thing it removed, and a raw substring scan would match its own
    warning label.
    """
    return "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))


def test_no_unverified_ssh_helper_remains():
    """ssh_exec/ssh_exec_sync defaulted to known_hosts=None with no callers.

    Dead code, but it carried a comment endorsing the very default the host-key
    work exists to eliminate — the next person needing a remote command would
    have copied it.
    """
    from web.backend import ssh_bridge

    assert not hasattr(ssh_bridge, "ssh_exec")
    assert not hasattr(ssh_bridge, "ssh_exec_sync")


def test_grow_over_ssh_refuses_an_unpinned_host_key():
    """asyncssh reads known_hosts=None as "trust anything".

    The remaining SSH helper keeps a None default for signature compatibility, so
    a caller that simply forgets the argument must be refused rather than handing
    the user's private key to whatever answered on the address.
    """
    from web.backend import ssh_bridge

    result = ssh_bridge.grow_filesystem_over_ssh(
        "203.0.113.7", username="ubuntu", private_key_pem=_PEM
    )
    assert result.ok is False
    assert "主机密钥" in result.message


def test_no_module_hardcodes_an_unverified_connect():
    """No live call site may pass known_hosts=None explicitly."""
    import inspect

    from web.backend import ssh_bridge
    from web.backend.routers import instance_ops

    for mod in (ssh_bridge, webssh, instance_ops):
        src = _code_only(inspect.getsource(mod))
        assert "known_hosts=None" not in src, f"{mod.__name__} passes known_hosts=None"
        assert '"known_hosts": None' not in src, f"{mod.__name__} passes known_hosts=None"


# --------------------------------------------------------------------------- #
# Invariant guard — the host key is verified BEFORE credentials are sent
# --------------------------------------------------------------------------- #


def test_hostkey_is_verified_before_connect():
    """Re-asserted here because these fixes move code around in this handler.

    Verifying after connect() would hand the password / private key to an
    impostor. The order must stay probe -> verify -> connect.
    """
    import inspect

    src = inspect.getsource(webssh.webssh_endpoint)
    probe_at = src.index("probe_host_key(")
    verify_at = src.index("verify_host_key(")
    connect_at = src.index("asyncssh.connect(")
    assert probe_at < verify_at < connect_at
    # And the connection itself is pinned to the key the probe verified.
    assert "known_hosts_for(server_key)" in src


def test_credentials_are_never_sent_on_a_mismatch(client, harness, monkeypatch):
    """End to end: a changed fingerprint must abort before asyncssh.connect()."""
    with client.websocket_connect(_url(client)) as ws:
        _connect(client, ws)  # learns SHA256:WEBSSH
    harness.rec.pop("connect", None)

    async def _evil(host, port=22, *, timeout=20.0, prefer_key_type=""):
        return _FakeKey("SHA256:EVIL")

    monkeypatch.setattr(webssh, "probe_host_key", _evil)
    with client.websocket_connect(_url(client)) as ws:
        while True:
            msg = _recv_json(ws)
            if msg.get("type") == "ready":
                ws.send_text(
                    json.dumps({"username": "ubuntu", "port": 22, "private_key_pem": _PEM})
                )
                continue
            break
    assert msg["type"] == "error" and msg.get("code") == "hostkey_mismatch"
    assert "connect" not in harness.rec, "credentials must not be sent after a mismatch"


def await_sync(coro):
    """Run a coroutine from a sync test (the handler's locks are asyncio ones)."""
    return asyncio.run(coro)
