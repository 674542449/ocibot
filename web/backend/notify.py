"""Multi-channel push notifications (Telegram / Bark / ServerChan / Webhook / SMTP).

Channel secrets are stored Fernet-encrypted in NotificationChannel.config_encrypted.
All sends are best-effort: failures are logged and returned, never raised.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from web.backend.crypto_util import decrypt_text, encrypt_text
from web.backend.models import NotificationChannel

log = logging.getLogger("ocibot.notify")

CHANNEL_KINDS = ("telegram", "bark", "serverchan", "webhook", "smtp")
EVENT_KEYS = ("capacity", "schedule", "budget", "password_expiry")

_HTTP_TIMEOUT = 15.0


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
    if kind in {"webhook", "bark"}:
        url = str(config.get("url") or config.get("server") or "").strip()
        if url and not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("URL 必须以 http:// 或 https:// 开头")


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
        log.warning("notify %s failed: %s", kind, exc)
        return False, str(exc)[:300]


def _send_telegram(config: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    token = str(config.get("bot_token") or "").strip()
    chat_id = str(config.get("chat_id") or "").strip()
    text = f"*{title}*\n{body}" if title else body
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            return True, "sent"
        # Markdown parse errors: retry as plain text
        resp2 = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"{title}\n{body}" if title else body},
        )
        if resp2.status_code == 200 and resp2.json().get("ok"):
            return True, "sent"
        return False, f"HTTP {resp2.status_code}: {resp2.text[:200]}"


def _send_bark(config: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    server = str(config.get("server") or "https://api.day.app").strip().rstrip("/")
    key = str(config.get("device_key") or "").strip()
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.post(
            f"{server}/push",
            json={"device_key": key, "title": title or "OCIBot", "body": body, "group": "ocibot"},
        )
        if resp.status_code == 200:
            return True, "sent"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def _send_serverchan(config: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    key = str(config.get("send_key") or "").strip()
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": (title or "OCIBot")[:32], "desp": body},
        )
        if resp.status_code == 200:
            return True, "sent"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def _send_webhook(config: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    url = str(config.get("url") or "").strip()
    headers = {}
    secret = str(config.get("secret") or "").strip()
    if secret:
        headers["X-OCIBot-Secret"] = secret
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        resp = client.post(url, json={"title": title, "body": body, "source": "ocibot-web"}, headers=headers)
        if 200 <= resp.status_code < 300:
            return True, "sent"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def _send_smtp(config: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    host = str(config.get("host") or "").strip()
    port = int(config.get("port") or 465)
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    to_addr = str(config.get("to_addr") or "").strip()
    from_addr = str(config.get("from_addr") or username).strip()
    use_ssl = bool(config.get("use_ssl", port == 465))

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(title or "OCIBot 通知", "utf-8")
    msg["From"] = formataddr((str(Header("OCIBot", "utf-8")), from_addr))
    msg["To"] = to_addr

    if use_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    try:
        if not use_ssl:
            try:
                server.starttls(context=ssl.create_default_context())
            except smtplib.SMTPNotSupportedError:
                pass
        server.login(username, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())
    finally:
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
    for row in rows:
        events = list(row.events or [])
        if events and event not in events:
            continue
        config = decode_channel_config(row.config_encrypted)
        ok, detail = send_to_channel(row.kind, config, title, body)
        results.append({"channel": row.name or row.kind, "kind": row.kind, "ok": ok, "detail": detail})
        if not ok:
            log.warning("notify channel %s(%s) failed: %s", row.name, row.kind, detail)
    return results
