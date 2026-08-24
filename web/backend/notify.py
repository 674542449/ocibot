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
# 一次 SMTP 会话的**总**墙钟上限。
#
# _SMTP_TIMEOUT 传给 smtplib 后只是 socket 超时，管的是单次 recv，不是整个会话。
# 一次投递要走 connect / banner / EHLO / STARTTLS / EHLO / AUTH / MAIL / RCPT /
# DATA / end-of-data 十来个来回，每一个都能各自用满 20 秒 —— 实测一台每次响应
# 前拖 0.8 秒的服务器就能把「20 秒超时」变成 6.5 倍，而恶意/半死的服务器可以到
# 200 秒。notify_user 只在**每个渠道之前**看一眼 60 秒预算（_SEND_BUDGET_SEC），
# 中途不看，所以一个卡住的邮件服务器会直接顶穿预算，并且因为
# notify_user 是在 worker 的抢机循环里调用的，全站其他用户的抢机尝试跟着一起等。
# HTTP 路径早就有等价的 _TOTAL_DEADLINE_SEC，SMTP 当时漏掉了。
_SMTP_TOTAL_DEADLINE_SEC = 45.0
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


class UndecryptableConfig(dict):
    """解密失败时代替 `{}` 返回的空 dict，额外带上「为什么」。

    为什么是 dict 的子类而不是抛异常：decode_channel_config 有三个调用方，其中
    routers/notifications.py 的 `_out()` 是列表页每一行都要走的，抛出去会让**整个**
    渠道列表 500 —— 主密钥不匹配时恰恰最需要那个列表还能打开。所以对不关心原因的
    调用方它就是一个空 dict（config_hint 照常渲染成空），而发送路径
    （send_to_channel）能认出它并把真正的原因报出来。

    用子类而不是往 dict 里塞一个 `__decrypt_error__` 键：后者会和用户自己存的配置
    键冲突，也会被 config_hint / _send_* 当成普通字段看。
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason


# 面向操作员的说明。注意只讲现象和处置，绝不带密文、主密钥或任何密钥材料 ——
# 这条字符串会进 API 响应、worker 日志和审计详情。
_DECRYPT_FAILED_DETAIL = (
    "无法解密该渠道的配置：主密钥不匹配或数据已损坏。"
    "这通常是 OCIBOT_MASTER_KEY 变了或 .env 未加载："
    "请恢复原来的主密钥；若主密钥确实无法找回，只能删除该渠道后重新填写配置"
    "（旧密文无法再还原）。"
)


def decode_channel_config(encrypted: str) -> dict[str, Any]:
    """解出渠道配置；解不开时返回 UndecryptableConfig 而不是干净的 `{}`。

    原来这里是一个 `except Exception: return {}`，把「主密钥换了」和「配置真的是
    空的」混成同一件事。后果是主密钥一轮换，Telegram 报「invalid bot_token」、
    webhook 报「URL 不能为空」、SMTP 报「缺少字段 host, username…」—— 五个渠道
    五种说法，每一种都指向操作员去改一个其实没问题的字段，而真正的原因
    （OCIBOT_MASTER_KEY 变了 / .env 没加载）一个字都没提。
    routers/auth.py 的 _verify_totp 对 2FA 密钥就是明确报出这种情况的，照抄那条先例。
    """
    if not encrypted:
        return {}
    try:
        plain = decrypt_text(encrypted)
    except Exception:  # noqa: BLE001
        # decrypt_text 对密钥不匹配抛 ValueError；密文被截断/改坏还可能抛
        # UnicodeEncodeError（.encode("ascii")）。两种都是「这串东西还原不回来」。
        return UndecryptableConfig(_DECRYPT_FAILED_DETAIL)
    try:
        data = json.loads(plain)
    except ValueError:
        # 解密成功但里面不是 JSON：那是真正的数据损坏，跟主密钥无关，保持旧行为。
        return {}
    return data if isinstance(data, dict) else {}


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
    # 配置根本没解开的时候，往下走只会让每个渠道各自报一句「缺少字段」/「URL 非法」。
    # 在这里拦住，是为了让「测试」按钮和 worker 的失败详情都说出同一个真正的原因。
    if isinstance(config, UndecryptableConfig):
        return False, config.reason
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
        # 只有「Markdown 没解析成功」才值得去掉 parse_mode 重发一次。
        #
        # 这里原来是无条件重发：第一次失败是什么原因都不看。于是一个
        # 429 {"parameters":{"retry_after":30}} 变成**两次** POST，第二次紧接着打向
        # 一个刚刚明确要求暂停 30 秒的端点 —— 抢机高峰上每个渠道的请求量直接翻倍，
        # 把本来几十秒就能恢复的限流拖成持续限流，Telegram 侧还可能因此收紧这个
        # bot。chat not found / bot 被踢出群这类 400 同理：纯文本重发一样救不了，
        # 白发一次而已。
        if _telegram_parse_error(status, payload):
            status, payload = _post(
                client, url, json={"chat_id": chat_id, "text": f"{title}\n{body}" if title else body}
            )
            if status == 200 and _json_ok(payload):
                return True, "sent"
        # api.telegram.org is a fixed host, so its error description is ours to show.
        return False, _http_error(status, payload, echo=True)


def _telegram_parse_error(status: int, payload: str) -> bool:
    """这次失败是不是 Markdown 实体没解析出来（去掉 parse_mode 重发才有意义）。

    Telegram 用 400 + description 报解析错误，典型文案是
    `Bad Request: can't parse entities: Can't find end of the entity starting at
    byte offset 7`。判据放宽到 "parse" / "entit"，是因为这句话历年改过好几版
    （曾经是 `can't parse message text`），但这两个词根一直在；同时把 429、
    401、chat not found 这些**重发也没用**的失败排除在外。
    """
    if status != 400:
        return False
    try:
        data = json.loads(payload)
    except ValueError:
        # 400 但连 JSON 都不是（网关插了一页 HTML）：无从判断，按「不是解析错误」
        # 处理 —— 猜错时多发一次请求的代价，大于少发一次。
        return False
    if not isinstance(data, dict):
        return False
    desc = str(data.get("description") or "").lower()
    return "parse" in desc or "entit" in desc


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
        # Server酱把成败放在**响应体的 code 里**，HTTP 状态只在 429 时才是错的。
        # 只看 status == 200 的话，SendKey 过期、当天推送次数用完这类
        # `200 + {"code":40001,...}` 全都会被报成「已发送」——「测试」按钮跟着一起
        # 报绿，操作员于是对一条从来没通过的通知链路建立信心。
        if status == 200:
            try:
                data = json.loads(payload)
            except ValueError:
                data = None
            if isinstance(data, dict) and "code" in data:
                if int(data.get("code") or 0) == 0:
                    return True, "sent"
                # 固定的厂商域名，回显它自己的 message 是唯一的诊断信息。
                reason = _sanitize(str(data.get("message") or ""), 120)
                return False, f"Server酱返回 code={data.get('code')}" + (f"：{reason}" if reason else "")
            # 响应体不是预期的 JSON（网关插了一页 HTML 之类）：保持旧行为，
            # 200 依然算发出去了，不因为解析不了就把能用的渠道判死。
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


def _smtp_step(server: Optional[smtplib.SMTP], deadline: float) -> None:
    """在会话的下一步之前收紧 socket 超时，让每一步都不能超过剩余总预算。

    过了总期限就抛 timeout；否则把 socket 超时压到 min(单步超时, 剩余)，
    这样「所有步骤之和」也被 _SMTP_TOTAL_DEADLINE_SEC 挡住，而不只是单步。
    """
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("smtp session exceeded total deadline")
    if server is not None:
        sock = getattr(server, "sock", None)
        if sock is not None:
            try:
                sock.settimeout(min(_SMTP_TIMEOUT, left))
            except OSError:
                pass


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
    deadline = time.monotonic() + _SMTP_TOTAL_DEADLINE_SEC
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(
                host, port, timeout=_SMTP_TIMEOUT, context=ssl.create_default_context()
            )
        else:
            server = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT)
            _smtp_step(server, deadline)
            try:
                server.starttls(context=ssl.create_default_context())
            except smtplib.SMTPNotSupportedError:
                # This exception means precisely "the server does not advertise
                # STARTTLS", so swallowing it put AUTH PLAIN <base64 password> on a
                # plaintext socket — and use_ssl defaults to False for every 587/25
                # setup, i.e. the ordinary configuration. Not sending beats leaking
                # the mailbox credential to anyone on the path.
                if require_tls:
                    # 提示必须指向操作员**真的做得到**的动作。
                    #
                    # 原文是「或在渠道配置中显式设置 require_tls=false」，读起来像
                    # 面板里有这么一个开关；SettingsView 的 SMTP 表单只有
                    # host/port/username/password/to_addr，操作员会在表单里翻上一
                    # 圈然后来问「require_tls 在哪」。所以：把 465+SSL 这条自助路径
                    # 放在前面，逃生开关说清楚它是 config 里的一个字段、面板没暴露
                    # 时得手工 PATCH，而不是暗示它就在眼前。
                    return False, (
                        "SMTP 服务器不支持 STARTTLS，已中止发送（继续会以明文发送账号密码）。"
                        "推荐修法：把该渠道改成 465 端口并使用 SSL 连接。"
                        "若确实要接受明文风险：设置页「添加渠道」的 SMTP 表单里有"
                        "「传输加密」开关，取消勾选后重新创建一个渠道即可"
                        "（现有渠道没有编辑入口，PATCH 也不行 —— update_channel 是整包"
                        "替换 config 并做必填校验，只提交 require_tls 会被拒成「缺少字段」）。"
                    )
                log.warning(
                    "smtp channel %s:%s authenticating without TLS (require_tls=false)", host, port
                )
        _smtp_step(server, deadline)
        server.login(username, password)
        _smtp_step(server, deadline)
        server.sendmail(from_addr, [to_addr], msg.as_string())
    except TimeoutError:
        log.warning("smtp %s:%s exceeded the %.0fs session deadline", host, port, _SMTP_TOTAL_DEADLINE_SEC)
        return False, f"SMTP 服务器响应过慢，已在 {_SMTP_TOTAL_DEADLINE_SEC:.0f} 秒后放弃本次发送"
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
            # QUIT 也要等对端回一行 221，同样能挂住；给它一个固定的小超时，
            # 别让收尾动作把上面刚立好的总期限又还回去。
            sock = getattr(server, "sock", None)
            if sock is not None:
                try:
                    sock.settimeout(5.0)
                except OSError:
                    pass
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
            select(NotificationChannel)
            .where(
                NotificationChannel.owner_id == owner_id,
                NotificationChannel.enabled.is_(True),
            )
            # 排序不是为了好看，是为了让「预算把扇出截断」这件事可解释。
            #
            # 没有 ORDER BY 时行序由数据库自行决定（SQLite 的 rowid、Postgres 的
            # 堆顺序，都会随 VACUUM / 更新而变）。渠道数超过 _MAX_SENDS_PER_EVENT
            # 或者前面几个渠道拖满 60 秒预算时，**哪几个渠道被丢掉每次都可能不一样**：
            # 同一台机器上同一个用户，这次抢机成功推到了 Telegram，下次只推到了邮件，
            # 而操作员根本无从复现。按创建时间排序后，被丢掉的永远是最后建的那几个，
            # 面板里的渠道列表（routers/notifications.py 也是按 created_at 排的）
            # 从上往下看就是实际的发送顺序。
            #
            # 第二个键是 id：created_at 只精确到微秒，同一次备份导入建出来的多个渠道
            # 完全可能落在同一个值上，只按它排仍然是不确定的。id 是主键，一定唯一。
            # （这一列不会是 NULL：models.py 里 NotificationChannel.created_at 声明成
            # `Mapped[datetime]` 而非 Optional，所以是 NOT NULL。events 那一列才是
            # 后加的、老行上为 NULL 的那个。）
            .order_by(NotificationChannel.created_at.asc(), NotificationChannel.id.asc())
        ).all()
    except Exception:  # noqa: BLE001
        log.exception("notify_user: query channels failed")
        return results
    # None vs []: rows created before the events column existed have NULL and
    # must keep receiving everything (_ensure_schema cannot backfill a callable
    # default), while an explicitly empty list means the user switched every
    # event off and must receive nothing. `list(row.events or [])` collapsed
    # both to "send everything".
    targets = [
        row for row in rows if row.events is None or event in list(row.events)
    ]
    # Bound the fan-out: channels are uncapped per user and each send has a 15-20s
    # timeout, so one trigger could otherwise stall the worker tick for minutes.
    started = time.monotonic()
    sent = 0
    for idx, row in enumerate(targets):
        if sent >= _MAX_SENDS_PER_EVENT or time.monotonic() - started > _SEND_BUDGET_SEC:
            # 报的必须是「订阅了这个事件、但没轮到」的渠道数。
            #
            # 原来是 len(rows) - sent —— rows 是这个用户**全部**启用的渠道，包含刚
            # 才被事件过滤器跳过的那些。一个订了 2 个渠道、另有 100 个只订别的事件
            # 的用户，日志会写「102 个渠道没来得及发」，排障的人于是去查为什么扇出
            # 会到 102，而真实答案是 0 或者 1。
            log.warning(
                "notify budget reached for owner=%s event=%s; %d channel(s) not attempted",
                owner_id,
                event,
                len(targets) - idx,
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
