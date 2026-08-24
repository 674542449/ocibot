"""Outbound notification hardening.

Every channel here dials a destination the *user* chose, from the panel's egress IP,
with no human watching (the worker calls this). Four things went wrong with that:

* the 15s httpx timeout is per operation and `read` bounds one chunk, so a hostile
  endpoint could hold a request thread — and the worker's capacity tick — forever
  while the response body grew in memory unbounded;
* the SMTP path validated the host but never the port, and reflected the remote's
  SMTP banner back to the caller, which is a port scanner with output;
* STARTTLS not being offered was swallowed, so the mailbox password went out in
  cleartext on exactly the ordinary 587 configuration;
* webhook/Bark response bodies came back verbatim, giving any authenticated user a
  read primitive against everything the panel can reach.
"""

from __future__ import annotations

import logging
import os
import smtplib
import tempfile
import time
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="ocibot_notifyhard_")
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{Path(_TMP, 'n.db').as_posix()}")
os.environ.setdefault("OCIBOT_MASTER_KEY", "notifyhard-master-key-0123456789")
os.environ.setdefault("OCIBOT_JWT_SECRET", "notifyhard-jwt-secret-0123456789")

pytest.importorskip("httpx")
pytest.importorskip("fastapi")

import httpx  # noqa: E402

from web.backend import notify as notify_mod  # noqa: E402
from web.backend import url_safety  # noqa: E402

_WEBHOOK = {"url": "https://hook.example.com/push"}
_BARK = {"server": "https://bark.example.com", "device_key": "k"}
_SMTP_BASE = {
    "host": "smtp.example.com",
    "username": "bot@example.com",
    "password": "hunter2-app-password",
    "to_addr": "ops@example.com",
}


@pytest.fixture
def transport(monkeypatch):
    """Install a MockTransport into the module's client kwargs and skip DNS checks."""

    def _install(handler):
        kw = dict(notify_mod._HTTP_CLIENT_KW)
        kw["transport"] = httpx.MockTransport(handler)
        monkeypatch.setattr(notify_mod, "_HTTP_CLIENT_KW", kw)

    monkeypatch.setattr(notify_mod, "assert_safe_outbound_url", lambda url: None)
    return _install


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    monkeypatch.setattr(url_safety, "resolve_and_check_host", lambda host: None)


# ---------------------------------------------------------------------------
# Response size cap / overall deadline
# ---------------------------------------------------------------------------


def test_a_huge_response_body_is_not_read_into_memory(transport):
    """resp.text[:200] clips only *after* the whole body is buffered."""
    produced = {"chunks": 0}

    def _flood():
        for _ in range(4000):  # 4 MB if fully read
            produced["chunks"] += 1
            yield b"A" * 1024

    transport(lambda request: httpx.Response(500, content=_flood()))
    ok, detail = notify_mod._send_webhook(dict(_WEBHOOK), "t", "b")
    assert ok is False
    assert produced["chunks"] < 100, f"read {produced['chunks']} KiB of a hostile body"
    assert "A" * 50 not in detail


def test_a_dripping_body_cannot_outlive_the_overall_deadline(transport, monkeypatch):
    """The per-chunk read timeout never fires against a server that keeps trickling;
    only a wall-clock deadline stops it, and in the worker that stall runs past
    notify_user's 60s budget (checked only between channels) into the capacity tick."""

    def _drip():
        for _ in range(200):  # 10s of trickle at one byte per 50ms
            time.sleep(0.05)
            yield b"x"

    monkeypatch.setattr(notify_mod, "_TOTAL_DEADLINE_SEC", 0.3)
    transport(lambda request: httpx.Response(500, content=_drip()))
    started = time.monotonic()
    ok, _detail = notify_mod._send_webhook(dict(_WEBHOOK), "t", "b")
    elapsed = time.monotonic() - started
    assert ok is False
    assert elapsed < 3.0, f"one hostile endpoint held the thread for {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Response bodies are not reflected for user-supplied destinations
# ---------------------------------------------------------------------------


def test_webhook_failure_never_echoes_the_response_body(transport):
    transport(lambda request: httpx.Response(403, text="SECRET-INTERNAL-PAGE-CONTENT"))
    ok, detail = notify_mod._send_webhook(dict(_WEBHOOK), "t", "b")
    assert ok is False
    assert "SECRET" not in detail, "arbitrary URL read primitive via the test button"
    assert "403" in detail


def test_bark_failure_never_echoes_the_response_body(transport):
    transport(lambda request: httpx.Response(500, text="SECRET-INTERNAL-PAGE-CONTENT"))
    ok, detail = notify_mod._send_bark(dict(_BARK), "t", "b")
    assert ok is False
    assert "SECRET" not in detail
    assert "500" in detail


def test_the_fixed_vendor_endpoints_keep_their_diagnostics(transport):
    """Telegram's own host is not attacker-chosen, and "chat not found" is the entire
    point of pressing 测试渠道 — so that excerpt stays, sanitized and clipped."""
    transport(
        lambda request: httpx.Response(400, text='{"ok":false,"description":"chat not found"}')
    )
    ok, detail = notify_mod._send_telegram({"bot_token": "1:abc", "chat_id": "9"}, "t", "b")
    assert ok is False
    assert "chat not found" in detail


def test_an_echoed_excerpt_cannot_contain_newlines(transport):
    """It reaches a line-oriented log; a newline in it forges a record."""
    transport(lambda request: httpx.Response(400, text='{"description":"line1\nline2"}'))
    ok, detail = notify_mod._send_telegram({"bot_token": "1:abc", "chat_id": "9"}, "t", "b")
    assert ok is False
    assert "\n" not in detail and "\r" not in detail


# ---------------------------------------------------------------------------
# SMTP: port allow-list, no banner reflection, no silent TLS downgrade
# ---------------------------------------------------------------------------


class _Refuse:
    """Any construction is a connection attempt that must not have happened."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(f"connected anyway: {args}")


def test_smtp_port_must_be_a_mail_port_on_save():
    with pytest.raises(ValueError) as exc:
        notify_mod.validate_channel_config("smtp", dict(_SMTP_BASE, port=9200))
    assert "9200" in str(exc.value)
    # The real mail ports still work.
    for port in (25, 465, 587, 2525):
        notify_mod.validate_channel_config("smtp", dict(_SMTP_BASE, port=port))


def test_smtp_send_refuses_a_scanning_port_without_connecting(monkeypatch):
    """A channel saved before the allow-list existed must not still be a scanner."""
    monkeypatch.setattr(smtplib, "SMTP", _Refuse)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Refuse)
    ok, detail = notify_mod._send_smtp(dict(_SMTP_BASE, port=22), "t", "b")
    assert ok is False
    assert "22" in detail


def test_smtp_never_reflects_the_remote_banner(monkeypatch):
    """SMTPConnectError carries the peer's first response line. Returning it told
    open-with-banner, open-but-silent and closed apart for any host the caller named."""

    class _Banner:
        def __init__(self, *args, **kwargs):
            raise smtplib.SMTPConnectError(220, "SSH-2.0-OpenSSH_9.6 SECRET-BANNER")

    monkeypatch.setattr(smtplib, "SMTP", _Banner)
    ok, detail = notify_mod._send_smtp(dict(_SMTP_BASE, port=587), "t", "b")
    assert ok is False
    assert "SECRET-BANNER" not in detail
    assert "OpenSSH" not in detail


class _FakeSMTP:
    """Server that never advertises STARTTLS — the downgrade case."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.logged_in: list[tuple[str, str]] = []
        self.sent: list[tuple] = []
        _FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")

    def login(self, user, password):
        self.logged_in.append((user, password))

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent.append((from_addr, to_addrs, msg))

    def quit(self):
        pass


def test_starttls_refusal_aborts_instead_of_authenticating_in_cleartext(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    ok, detail = notify_mod._send_smtp(dict(_SMTP_BASE, port=587), "t", "b")
    assert ok is False
    assert "STARTTLS" in detail
    server = _FakeSMTP.instances[-1]
    assert server.logged_in == [], "AUTH PLAIN <base64 password> went out in cleartext"
    assert server.sent == []


def test_require_tls_false_is_the_only_way_to_opt_into_cleartext(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    ok, _detail = notify_mod._send_smtp(
        dict(_SMTP_BASE, port=587, require_tls=False), "t", "b"
    )
    assert ok is True
    assert _FakeSMTP.instances[-1].logged_in == [("bot@example.com", "hunter2-app-password")]


def test_the_implicit_ssl_path_is_untouched(monkeypatch):
    """465 negotiates TLS in the constructor, so it was never the downgrade case."""

    class _FakeSSL(_FakeSMTP):
        def __init__(self, host, port, timeout=None, context=None):
            super().__init__(host, port, timeout=timeout)

        def starttls(self, context=None):  # never called on an implicit-TLS socket
            raise AssertionError("starttls on SMTP_SSL")

    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSSL)
    ok, detail = notify_mod._send_smtp(dict(_SMTP_BASE, port=465), "t", "b")
    assert (ok, detail) == (True, "sent")
    assert _FakeSMTP.instances[-1].sent


# ---------------------------------------------------------------------------
# Log-line forgery through a channel name
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, name):
        self.name = name
        self.kind = "webhook"
        self.events = ["capacity"]
        self.config_encrypted = ""


class _DB:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self, _stmt):
        rows = self._rows

        class _R:
            def all(self):
                return rows

        return _R()


def test_a_channel_name_cannot_forge_a_worker_log_line(monkeypatch, caplog):
    forged = "x\n2026-08-23 10:00:00 INFO ocibot.worker capacity job 1 succeeded"
    monkeypatch.setattr(notify_mod, "send_to_channel", lambda *a: (False, "boom\nfake detail"))
    with caplog.at_level(logging.WARNING, logger="ocibot.notify"):
        notify_mod.notify_user(_DB([_Row(forged)]), "owner", "capacity", "t", "b")
    assert caplog.records, "the failed send should still be logged"
    for record in caplog.records:
        assert "\n" not in record.getMessage()
        assert "\r" not in record.getMessage()


def test_control_characters_are_stripped_when_the_channel_is_saved():
    from web.backend.routers.notifications import _clean_name

    assert _clean_name("alpha\nbeta", "webhook") == "alpha beta"
    assert _clean_name("  \n\t ", "webhook") == "webhook"
    assert _clean_name("正常名字", "webhook") == "正常名字"
