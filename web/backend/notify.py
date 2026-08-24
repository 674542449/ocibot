"""Multi-channel push notifications (Telegram / Bark / ServerChan / Webhook / SMTP).

Channel secrets are stored Fernet-encrypted in NotificationChannel.config_encrypted.
All sends are best-effort: failures are logged and returned, never raised.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from web.backend.crypto_util import decrypt_text, encrypt_text
from web.backend.models import NotificationChannel
from web.backend.url_safety import assert_safe_outbound_url, validate_public_http_url

log = logging.getLogger("ocibot.notify")

CHANNEL_KINDS = ("telegram", "bark", "serverchan", "webhook", "smtp")
# Only capacity retry pushes notifications since 0.4.36 (schedules and budget
# alerts were removed). Channels stored with the old keys keep them harmlessly.
EVENT_KEYS = ("capacity",)

# Per-phase timeouts. httpx applies these to each operation separately and `read`
# bounds ONE chunk, not the whole body, so they are a floor and not a ceiling —
# _TOTAL_DEADLINE_SEC below is the actual ceiling.
_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
# Wall-clock ceiling for one request+response, and the most of a response body we
# are ever willing to pull into memory.
_TOTAL_DEADLINE_SEC = 15.0
_MAX_RESPONSE_BYTES = 16 * 1024
_SMTP_TIMEOUT = 20.0
# Fan-out limits for one notify_user() call.
_MAX_SENDS_PER_EVENT = 20
_SEND_BUDGET_SEC = 60.0
# Outbound client: no env proxy (avoid surprising proxy SSRF), no redirects to internal.
_HTTP_CLIENT_KW = {
    "timeout": _HTTP_TIMEOUT,
    "follow_redirects": False,
    "trust_env": False,
}

# SMTP is the only channel that dials a raw host:port. url_safety's _BLOCKED_PORTS is
# a deny-list written for HTTP webhooks and is unusable here (25 is on it, and 25 is
# SMTP's own port), so restrict by allow-list instead. Without one `port` was any int
# the user liked: 测试渠道 became a port scanner against everything the panel can
# reach, with the probe result — and via SMTPConnectError the remote's banner —
# handed back in the response.
_SMTP_PORTS = frozenset({25, 465, 587, 2525})


def _sanitize(text: str, limit: int) -> str:
    """Flatten control characters and clip.

    Remote-controlled strings end up in worker log lines and in the settings UI. A
    newline inside one forges a complete extra log record, so nothing that came off
    a socket (or out of a user-set channel name) may reach a logger untouched.
    """
    cleaned = "".join(" " if (ch < " " or ch == "\x7f") else ch for ch in str(text or ""))
    return " ".join(cleaned.split())[:limit]


def _http_error(status: int, body: str = "", *, echo: bool = False) -> str:
    """Failure detail for an HTTP push.

    The body is echoed only for the two fixed vendor endpoints (Telegram, ServerChan),
    where "chat not found" is the whole diagnostic value of the test button. For
    webhook and Bark the URL is user-supplied, so echoing would give any authenticated
    user a 200-byte read of an arbitrary URL from the panel's egress IP — an
    authenticated read primitive against everything the panel can reach.
    """
    if echo and body:
        return f"HTTP {status}: {_sanitize(body, 120)}"
    return f"HTTP {status}"


def _post(client: httpx.Client, url: str, **kwargs: Any) -> tuple[int, str]:
    """POST, then read a bounded prefix of the response under one overall deadline.

    `client.post()` buffers the entire body before `resp.text[:200]` clips it, so the
    clip was cosmetic: an endpoint answering 500 and then dripping a byte every ten
    seconds pinned the calling thread forever and grew RSS without bound, because the
    15s read timeout only bounds a single chunk. In the worker that is worse than
    slow — notify_user checks its 60s budget only *between* channels, so one hung send
    sails past it and stalls the capacity retry tick.
    """
    deadline = time.monotonic() + _TOTAL_DEADLINE_SEC
    with client.stream("POST", url, **kwargs) as resp:
        chunks: list[bytes] = []
        size = 0
        for chunk in resp.iter_bytes():
            if size < _MAX_RESPONSE_BYTES:
                chunks.append(chunk[: _MAX_RESPONSE_BYTES - size])
            size += len(chunk)
            if size >= _MAX_RESPONSE_BYTES or time.monotonic() >= deadline:
                break
        return resp.status_code, b"".join(chunks).decode("utf-8", "replace")


def _smtp_port(config: dict[str, Any]) -> int:
    raw = config.get("port")
    try:
        port = int(raw if raw not in (None, "") else 465)
    except (TypeError, ValueError) as exc:
        raise ValueError("SMTP 端口必须是数字") from exc
    if port not in _SMTP_PORTS:
        allowed = ", ".join(str(p) for p in sorted(_SMTP_PORTS))
        raise ValueError(f"SMTP 端口只能是 {allowed}（收到 {port}）")
    return port


def encode_channel_config(config: dict[str, Any]) -> str:
    return encrypt_text(json.dumps(config, ensure_ascii=False))


def decode_channel_config(encrypted: str) -> dict[str, Any]:
    if not encrypted:
        return {}
    try:
        data = json.loads(decrypt_text(encrypted))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def config_hint(kind: str, config: dict[str, Any]) -> str:
    """Human-readable, secret-free summary shown in the settings UI."""

    def _tail(value: Any, n: int = 4) -> str:
        s = str(value or "")
        return ("…" + s[-n:]) if len(s) > n else ("…" if s else "")

    if kind == "telegram":
        return f"chat_id={config.get('chat_id', '')} token{_tail(config.get('bot_token'))}"
    if kind == "bark":
        base = str(config.get("server") or "https://api.day.app")
        return f"{base} key{_tail(config.get('device_key'))}"
    if kind == "serverchan":
        return f"sendkey{_tail(config.get('send_key'))}"
    if kind == "webhook":
        return str(config.get("url") or "")[:120]
    if kind == "smtp":
        return f"{config.get('username', '')}@{config.get('host', '')}:{config.get('port', 465)} → {config.get('to_addr', '')}"
    return ""


def validate_channel_config(kind: str, config: dict[str, Any]) -> None:
    """Raise ValueError when required fields for the channel kind are missing."""
    required: dict[str, tuple[str, ...]] = {
        "telegram": ("bot_token", "chat_id"),
        "bark": ("device_key",),
        "serverchan": ("send_key",),
        "webhook": ("url",),
        "smtp": ("host", "port", "username", "password", "to_addr"),
    }
    if kind not in required:
        raise ValueError(f"不支持的通知渠道类型: {kind}")
    missing = [f for f in required[kind] if not str(config.get(f) or "").strip()]
    if missing:
        raise ValueError(f"{kind} 渠道缺少字段: {', '.join(missing)}")
    if kind == "webhook":
        config["url"] = validate_public_http_url(str(config.get("url") or ""))
    if kind == "bark":
        server = str(config.get("server") or "https://api.day.app").strip()
        config["server"] = validate_public_http_url(server or "https://api.day.app")
    if kind == "smtp":
        host = str(config.get("host") or "").strip()
        # Block obvious cloud-metadata / loopback SMTP abuse.
        from web.backend.url_safety import hostname_is_blocked, resolve_and_check_host

        if hostname_is_blocked(host):
            raise ValueError(f"SMTP 主机不允许为内网/本地地址：{host}")
        try:
            resolve_and_check_host(host)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        # Normalized back into the config so the stored channel can never hold a
        # port the send path would then have to reject.
        config["port"] = _smtp_port(config)


def send_to_channel(kind: str, config: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    """Send one message. Returns (ok, detail). Never raises."""
    try:
        if kind == "telegram":
            return _send_telegram(config, title, body)
        if kind == "bark":
            return _send_bark(config, title, body)
        if kind == "serverchan":
            return _send_serverchan(config, title, body)
        if kind == "webhook":
            return _send_webhook(config, title, body)
        if kind == "smtp":
            return _send_smtp(config, title, body)
        return False, f"未知渠道类型: {kind}"
    except Exception as exc:  # noqa: BLE001
        # An exception message can carry remote bytes (and therefore newlines).
        detail = _sanitize(str(exc), 300)
        log.warning("notify %s failed: %s", kind, detail)
        return False, detail


def _send_telegram(config: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    token = str(config.get("bot_token") or "").strip()
    chat_id = str(config.get("chat_id") or "").strip()
    # Token is path-sensitive; reject weird characters that could alter the path.
    if not token or any(c in token for c in "/?# \t\r\n"):
        return False, "invalid bot_token"
    text = f"*{title}*\n{body}" if title else body
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with httpx.Client(**_HTTP_CLIENT_KW) as client:
        status, payload = _post(
            client, url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        )
        if status == 200 and _json_ok(payload):
            return True, "sent"
        # Markdown parse errors: retry as plain text
        status, payload = _post(
            client, url, json={"chat_id": chat_id, "text": f"{title}\n{body}" if title else body}
        )
        if status == 200 and _json_ok(payload):
            return True, "sent"
        # api.telegram.org is a fixed host, so its error description is ours to show.
        return False, _http_error(status, payload, echo=True)


def _json_ok(payload: str) -> bool:
    try:
        data = json.loads(payload)
    except ValueError:
        return False
    return bool(isinstance(data, dict) and data.get("ok"))


def _send_bark(config: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    server = str(config.get("server") or "https://api.day.app").strip().rstrip("/")
    try:
        assert_safe_outbound_url(server)
    except ValueError as exc:
        return False, str(exc)
    key = str(config.get("device_key") or "").strip()
    with httpx.Client(**_HTTP_CLIENT_KW) as client:
        status, _payload = _post(
            client,
            f"{server}/push",
            json={"device_key": key, "title": title or "OCIBot", "body": body, "group": "ocibot"},
        )
        if status == 200:
            return True, "sent"
        # `server` is user-supplied — status only, never the body. See _http_error.
        return False, _http_error(status)


def _send_serverchan(config: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    key = str(config.get("send_key") or "").strip()
    if not key or any(c in key for c in "/?# \t\r\n"):
        return False, "invalid send_key"
    with httpx.Client(**_HTTP_CLIENT_KW) as client:
        status, payload = _post(
            client,
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": (title or "OCIBot")[:32], "desp": body},
        )
        if status == 200:
            return True, "sent"
        # Fixed vendor host, so a short excerpt is safe and is the only diagnostic.
        return False, _http_error(status, payload, echo=True)


def _send_webhook(config: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    url = str(config.get("url") or "").strip()
    try:
        assert_safe_outbound_url(url)
    except ValueError as exc:
        return False, str(exc)
    headers = {}
    secret = str(config.get("secret") or "").strip()
    if secret:
        headers["X-OCIBot-Secret"] = secret
    with httpx.Client(**_HTTP_CLIENT_KW) as client:
        status, _payload = _post(
            client, url, json={"title": title, "body": body, "source": "ocibot-web"}, headers=headers
        )
        if 200 <= status < 300:
            return True, "sent"
        # `url` is user-supplied — status only, never the body. See _http_error.
        return False, _http_error(status)


def _send_smtp(config: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    host = str(config.get("host") or "").strip()
    # Re-check at send time (DNS may have changed since channel save).
    from web.backend.url_safety import hostname_is_blocked, resolve_and_check_host

    if hostname_is_blocked(host):
        return False, f"SMTP 主机不允许为内网/本地地址：{host}"
    try:
        resolve_and_check_host(host)
    except ValueError as exc:
        return False, str(exc)

    # Re-checked here as well as on save: a stored channel from before the allow-list
    # existed can still name port 9200.
    try:
        port = _smtp_port(config)
    except ValueError as exc:
        return False, str(exc)
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    to_addr = str(config.get("to_addr") or "").strip()
    from_addr = str(config.get("from_addr") or username).strip()
    use_ssl = bool(config.get("use_ssl", port == 465))
    require_tls = bool(config.get("require_tls", True))

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(title or "OCIBot 通知", "utf-8")
    msg["From"] = formataddr((str(Header("OCIBot", "utf-8")), from_addr))
    msg["To"] = to_addr

    server: Optional[smtplib.SMTP] = None
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(
                host, port, timeout=_SMTP_TIMEOUT, context=ssl.create_default_context()
            )
        else:
            server = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT)
            try:
                server.starttls(context=ssl.create_default_context())
            except smtplib.SMTPNotSupportedError:
                # This exception means precisely "the server does not advertise
                # STARTTLS", so swallowing it put AUTH PLAIN <base64 password> on a
                # plaintext socket — and use_ssl defaults to False for every 587/25
                # setup, i.e. the ordinary configuration. Not sending beats leaking
                # the mailbox credential to anyone on the path.
                if require_tls:
                    return False, (
                        "SMTP 服务器不支持 STARTTLS，已中止发送（继续会以明文发送账号密码）。"
                        "请改用 465 端口的 SSL 连接，或在渠道配置中显式设置 require_tls=false "
                        "以接受明文风险"
                    )
                log.warning(
                    "smtp channel %s:%s authenticating without TLS (require_tls=false)", host, port
                )
        server.login(username, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP 认证失败：用户名或密码/授权码不正确"
    except smtplib.SMTPResponseException as exc:
        # exc.smtp_error is the REMOTE's own response line. Returning it (as the
        # blanket handler in send_to_channel did) made 测试渠道 a banner grab: the
        # detail string told open-with-banner, open-but-silent and closed apart for
        # any host:port the caller chose. Keep it in the local log only.
        log.warning("smtp %s:%s response error: %s %r", host, port, exc.smtp_code, exc.smtp_error)
        code = exc.smtp_code if isinstance(exc.smtp_code, int) else 0
        return False, f"SMTP 服务器拒绝了本次请求（错误码 {code}）"
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("smtp %s:%s failed: %s", host, port, _sanitize(str(exc), 200))
        return False, "无法连接 SMTP 服务器（连接失败、超时或 TLS 握手失败）"
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
    return True, "sent"


def notify_user(
    db: Session,
    owner_id: str,
    event: str,
    title: str,
    body: str,
) -> list[dict[str, Any]]:
    """Push to every enabled channel of the user subscribed to `event`.

    Best-effort: DB errors and channel errors are swallowed (logged) so callers
    (the worker loop, HTTP handlers) never fail because of notification issues.
    """
    results: list[dict[str, Any]] = []
    try:
        rows = db.scalars(
            select(NotificationChannel).where(
                NotificationChannel.owner_id == owner_id,
                NotificationChannel.enabled.is_(True),
            )
        ).all()
    except Exception:  # noqa: BLE001
        log.exception("notify_user: query channels failed")
        return results
    # Bound the fan-out: channels are uncapped per user and each send has a 15-20s
    # timeout, so one trigger could otherwise stall the worker tick for minutes.
    started = time.monotonic()
    sent = 0
    for row in rows:
        # None vs []: rows created before the events column existed have NULL and
        # must keep receiving everything (_ensure_schema cannot backfill a callable
        # default), while an explicitly empty list means the user switched every
        # event off and must receive nothing. `list(row.events or [])` collapsed
        # both to "send everything".
        events = row.events
        if events is not None and event not in list(events):
            continue
        if sent >= _MAX_SENDS_PER_EVENT or time.monotonic() - started > _SEND_BUDGET_SEC:
            log.warning(
                "notify budget reached for owner=%s event=%s; %d channel(s) not attempted",
                owner_id,
                event,
                len(rows) - sent,
            )
            break
        config = decode_channel_config(row.config_encrypted)
        ok, detail = send_to_channel(row.kind, config, title, body)
        sent += 1
        results.append({"channel": row.name or row.kind, "kind": row.kind, "ok": ok, "detail": detail})
        if not ok:
            # row.name is user-set and only .strip()[:64] on write, so interior
            # newlines survived into the worker's line-oriented log: a 62-character
            # name is enough to forge a complete extra log record. detail can carry
            # remote bytes for the same reason.
            log.warning(
                "notify channel %s(%s) failed: %s",
                _sanitize(row.name or "", 64),
                _sanitize(row.kind or "", 16),
                _sanitize(detail, 200),
            )
    return results
