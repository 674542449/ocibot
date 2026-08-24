"""Multi-tenant OCI API configuration storage with optional encryption."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


APP_DIR_NAME = "ocibot"
CONFIG_FILE = "tenants.json"
KEY_FILE = ".secret"


def default_data_dir() -> Path:
    """Return the active application data directory."""
    from app.runtime_paths import ensure_writable_directory, resolve_data_dir

    return ensure_writable_directory(resolve_data_dir())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso_dt(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp/date into an aware UTC datetime (or None)."""
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_oci_api_text(text: str) -> dict[str, str]:
    """
    Parse classic OCI API / config text into field dict.

    Accepts:
      - ~/.oci/config style (with or without [PROFILE] header)
      - plain key=value lines
      - common aliases (user_ocid, tenancy_ocid, …)

    Returns keys among:
      user_ocid, tenancy_ocid, fingerprint, region, key_file,
      compartment_ocid, name, profile
    """
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return {}

    # Prefer configparser when it looks like INI
    import configparser
    import re

    result: dict[str, str] = {}
    profile_name = ""

    # Try INI parse first
    try:
        parser = configparser.ConfigParser()
        # ConfigParser needs a section; if missing, inject DEFAULT
        to_parse = raw
        # 逐行判断，不要用 re.search(r"^\s*\[.+\]\s*$", raw, re.M)。
        #
        # `\s` 是包含 `\n` 的，加上 re.M 之后引擎会在**每一个行首**重新起跳，
        # 每次都把 `\s*` 一路回溯到字符串结尾 —— 对空行数量是 O(n²)。
        # api_text 上限 64 000 字符，恰好就是最坏情况：实测 64KB 全空行要跑
        # 13.3 秒，而且是纯 CPU、不放 GIL（4 并发 25.8s / 8 并发 52.9s，
        # 线性劣化）。默认 OCIBOT_API_WORKERS=2，十几个并发请求就能把整个面板
        # 挂死，放大倍数约 20 万倍。任何登录用户都能从 /tenants/parse
        # 和 /tenants/import 打到这里，而这两条路都没有限流。
        #
        # 这里只需要回答「有没有某一行长得像 [section]」，逐行判断天然是线性的。
        if not any(
            line.strip().startswith("[") and line.strip().endswith("]")
            for line in raw.split("\n")
        ):
            to_parse = "[DEFAULT]\n" + raw
        parser.read_string(to_parse)
        # pick first non-empty section, else DEFAULT
        section = None
        for sec in parser.sections():
            if dict(parser[sec]):
                section = sec
                break
        if section is None:
            data = dict(parser.defaults())
            profile_name = "DEFAULT"
        else:
            data = dict(parser[section])
            # merge defaults
            for k, v in parser.defaults().items():
                data.setdefault(k, v)
            profile_name = section
    except Exception:
        # Fallback: line-by-line key=value / key: value
        data = {}
        for line in raw.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                profile_name = m.group(1).strip()
                continue
            if "=" in line:
                k, v = line.split("=", 1)
            elif ":" in line:
                k, v = line.split(":", 1)
            else:
                continue
            data[k.strip().lower()] = v.strip().strip('"').strip("'")

    # Normalize keys (also strip BOM / spaces on keys)
    aliases = {
        "user": "user_ocid",
        "user_ocid": "user_ocid",
        "user_id": "user_ocid",
        "userid": "user_ocid",
        "tenancy": "tenancy_ocid",
        "tenancy_ocid": "tenancy_ocid",
        "tenancy_id": "tenancy_ocid",
        "tenancyid": "tenancy_ocid",
        "fingerprint": "fingerprint",
        "finger_print": "fingerprint",
        "region": "region",
        "key_file": "key_file",
        "keyfile": "key_file",
        "key_path": "key_file",
        "private_key_path": "key_file",
        "compartment": "compartment_ocid",
        "compartment_id": "compartment_ocid",
        "compartment_ocid": "compartment_ocid",
        "name": "name",
        "display_name": "name",
    }
    for k, v in data.items():
        k_norm = str(k).strip().lower().lstrip("﻿")
        key = aliases.get(k_norm)
        if key and str(v).strip():
            val = str(v).strip().strip('"').strip("'")
            # Strip inline comments: value # comment
            if " #" in val:
                val = val.split(" #", 1)[0].strip()
            if val:
                result[key] = val

    if profile_name and "name" not in result:
        result["profile"] = profile_name
        if profile_name.upper() != "DEFAULT":
            result.setdefault("name", profile_name)

    return result


BACKUP_KDF_ITERATIONS = 390_000
# 读归档时迭代次数来自归档自身，必须夹在一个区间里：往下没有下限的话，
# 攻击者递给你一份 iterations=1 的归档，你就用一个几乎没成本的 KDF 去解它；
# 往上没有上限的话，一份 iterations=10^9 的归档能把一条请求线程钉死几分钟
# （/api/backup/import 是同步 handler，跑在 FastAPI 的线程池里，池子不大）。
_BACKUP_KDF_MIN_ITERATIONS = 100_000
_BACKUP_KDF_MAX_ITERATIONS = 2_000_000


def _derive_fernet(password: str, salt: bytes, iterations: int = BACKUP_KDF_ITERATIONS) -> Fernet:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=int(iterations),
    )
    key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
    return Fernet(key)


# ---------------------------------------------------------------------------
# 备份归档信封（/api/backup/export 与 backup_to_encrypted_zip 共用一种格式）
# ---------------------------------------------------------------------------
#
# 为什么 zip 自己的密码不够：pyzipper 用的 WinZip AES 虽然是 AES-256，但为了
# 和 7-Zip / WinRAR 互通，它的 KDF 被规范钉死成 PBKDF2-HMAC-**SHA1、1000 轮**，
# 无法调高。hashcat 的 13600 模式在一块普通显卡上就有 ~10 MH/s——按这个速度，
# 一个 8 位小写+数字的口令是分钟级的事。而这个归档里装的是每一个租户的
# **明文 OCI 私钥**，拿到就等于拿到整个租户账号；文件又天然会落在
# 下载目录 / OneDrive / NAS 上，长期躺着等人来跑字典。
#
# 所以真正的机密性由 zip **里面**这一层负责：同一个口令，过 PBKDF2-HMAC-SHA256
# 390 000 轮（和 ConfigStore 本地主密码同一条参数），再用 Fernet 封住整个
# tenants.json。zip 的口令保留，只当作第一道门 + 保持「双击能提示输密码」的
# 用户预期。
#
# version 1 = 明文 JSON（0.4.x 早期归档）。读的时候必须继续认，否则一次升级
# 就让操作者手里所有旧备份全部作废——备份作废是只会在真要用它的那天才被
# 发现的那种故障。
BACKUP_ENVELOPE_VERSION = 2
_BACKUP_KDF_ALGORITHM = "pbkdf2-hmac-sha256"


def encode_backup_payload(payload: dict[str, Any], password: str) -> bytes:
    """Wrap the tenant payload in the strong-KDF envelope (format version 2)."""
    salt = secrets.token_bytes(16)
    inner = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    token = _derive_fernet(password, salt, BACKUP_KDF_ITERATIONS).encrypt(inner)
    envelope = {
        "version": BACKUP_ENVELOPE_VERSION,
        "kdf": {
            "algorithm": _BACKUP_KDF_ALGORITHM,
            "iterations": BACKUP_KDF_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        "cipher": "fernet",
        "ciphertext": token.decode("ascii"),
    }
    return json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")


def decode_backup_payload(raw: bytes, password: str) -> Any:
    """Read either envelope format. Returns the decoded payload (dict or list).

    Raises ValueError with a user-facing message; the caller turns that into a 400.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("备份内容格式无效，可能密码错误或文件损坏") from exc
    if not isinstance(data, dict) or "ciphertext" not in data:
        # 旧格式（明文 JSON）。zip 的口令已经验过了，这里直接放行。
        return data

    kdf = data.get("kdf") if isinstance(data.get("kdf"), dict) else {}
    algorithm = str(kdf.get("algorithm") or "").strip().lower()
    if algorithm and algorithm != _BACKUP_KDF_ALGORITHM:
        raise ValueError(f"备份使用了本版本不支持的密钥派生算法：{algorithm}")
    try:
        salt = base64.b64decode(str(kdf.get("salt") or ""), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("备份文件的密钥派生参数已损坏") from exc
    if not salt:
        raise ValueError("备份文件的密钥派生参数已损坏")
    try:
        iterations = int(kdf.get("iterations") or BACKUP_KDF_ITERATIONS)
    except (TypeError, ValueError):
        iterations = BACKUP_KDF_ITERATIONS
    iterations = max(_BACKUP_KDF_MIN_ITERATIONS, min(iterations, _BACKUP_KDF_MAX_ITERATIONS))

    try:
        plain = _derive_fernet(password, salt, iterations).decrypt(
            str(data.get("ciphertext") or "").encode("ascii")
        )
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ValueError("无法解密备份内容，请检查密码是否正确") from exc
    try:
        return json.loads(plain.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("备份内容格式无效") from exc


def _machine_secret(path: Path) -> bytes:
    """Create or load a machine-local secret used to encrypt private keys at rest."""
    if path.exists():
        return path.read_bytes()
    secret = secrets.token_bytes(32)
    path.write_bytes(secret)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


# OCI 区域名的字符集：字母、数字、连字符。刻意**不**放行 "." / "@" / ":" / "/"
# —— 见 TenantConfig.validate() 里的说明，这四个字符任意一个都能把 SDK 拼出的
# endpoint 主机名改写成任意地址。宽度取 63，够任何现有及未来的 OCI 区域名，
# 同时也卡在 tenants.region 这一列 String(64) 的宽度之内。
_REGION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}")


def private_key_error(pem: str) -> str:
    """Return a user-facing reason this PEM cannot be used, or "" if it loads.

    在这之前，全系统对私钥的唯一校验就是「字符串里有 BEGIN 和 PRIVATE KEY」，
    OCI SDK 也不会替我们补上：构造 Signer 时它并不解析密钥，等到第一次真的发
    请求才炸。实测

        -----BEGIN ENCRYPTED PRIVATE KEY-----
        not-base64-at-all
        -----END ENCRYPTED PRIVATE KEY-----

    能拿到 201 和 has_private_key: true。而「保存后自动测试连接」默认是关的，
    所以操作者看到的是「已添加」，之后每个页面都 502，报的还是 SDK 抛出的
    看不懂的错——错误离原因隔了好几个页面，基本不可能自己定位到是私钥贴错了。

    带口令的私钥（Encrypted PRIVATE KEY）单独给一句话：Oracle 控制台生成 API
    Key 时就可能给你这种文件，这是最常见的一种「贴进去、看着对、其实用不了」。
    """
    text = (pem or "").strip()
    if "BEGIN" not in text or "PRIVATE KEY" not in text:
        return "私钥内容无效（需要 PEM 格式 PRIVATE KEY）"
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization

    try:
        serialization.load_pem_private_key(text.encode("utf-8"), password=None)
    except TypeError:
        # cryptography 对「密钥是加密的但没给口令」抛的正是 TypeError。
        return (
            "私钥带有口令保护（ENCRYPTED PRIVATE KEY），面板无法使用。"
            "请用 `openssl pkcs8 -topk8 -nocrypt -in 原文件 -out 新文件` 去掉口令后再粘贴，"
            "或在 Oracle 控制台重新生成一对不带口令的 API 密钥"
        )
    except UnsupportedAlgorithm:
        return "私钥使用了不受支持的算法，请改用 Oracle 控制台生成的 RSA API 密钥"
    except ValueError:
        return (
            "私钥无法解析：内容不是完整有效的 PEM 私钥"
            "（常见原因是只复制了一部分、粘贴时被编辑器换行/空格改写，或贴成了公钥）"
        )
    except Exception:  # noqa: BLE001
        return "私钥无法解析，请重新导出后粘贴"
    return ""


@dataclass
class TenantConfig:
    """One Oracle Cloud tenancy / API profile."""

    id: str
    name: str
    user_ocid: str
    tenancy_ocid: str
    fingerprint: str
    region: str
    private_key_pem: str
    compartment_ocid: str = ""
    description: str = ""
    enabled: bool = True
    color: str = "#3B82F6"
    # Oracle console password expiry reminder (local tracking; Oracle requires
    # periodic password changes or the account/free resources may be reclaimed).
    password_changed_at: str = ""       # ISO date of last change; empty = use created_at
    password_expiry_days: int = 120     # 0 disables the reminder
    # Cached account tier so the sidebar can show it without a network call:
    # "paid" | "free" | "" (unknown / not yet detected)
    account_tier: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)

    def display_label(self) -> str:
        region = self.region or "?"
        return f"{self.name}  ·  {region}"

    def short_tenancy(self) -> str:
        t = self.tenancy_ocid or ""
        if len(t) <= 18:
            return t or "—"
        return f"{t[:10]}…{t[-6:]}"

    # ------------------------------------------------------------------
    # Password expiry reminder
    # ------------------------------------------------------------------
    def password_baseline(self) -> Optional[datetime]:
        """The date the expiry countdown starts from (last change, else created)."""
        return _parse_iso_dt(self.password_changed_at) or _parse_iso_dt(self.created_at)

    def password_expiry_date(self) -> Optional[datetime]:
        days = int(self.password_expiry_days or 0)
        if days <= 0:
            return None
        base = self.password_baseline()
        if base is None:
            return None
        return base + timedelta(days=days)

    def password_days_left(self) -> Optional[int]:
        """Whole days until the password should be changed (negative if overdue).

        「今天」取**本地**日历日,而不是 UTC 日历日:这个数字是给操作员看的
        「还有几天」,那就该按操作员自己的日历数。UTC-5 的操作员在当地 20:00 时,
        UTC 已经是第二天,按 UTC 数会平白少一天。

        和 `oci_client.TenantSession._effective_password_expiry` 的**契约不同**,
        两者不该互相对齐,这是刻意的:
          * 这里的输入是操作员自己填的一个**日期**(password_changed_at 常常就是
            "2026-01-15"),加上天数之后仍然是一个日期。把它做时区换算是错的 ——
            他填的那一天不会因为人在纽约就变成前一天。所以只换算「今天」。
          * 那边的输入是 Oracle 给的一个**时刻**(last_set + 策略天数),必须整体
            换算到本地,因为它同时还要印出一个日期,两者必须出自同一个时区。
        同一个到期时刻交给两边会差一天(实测 UTC+8 下 9 对 8),那不是 bug,
        是两种输入形状的正确答案不同。改动任何一边之前先读这两段。
        """
        expiry = self.password_expiry_date()
        if expiry is None:
            return None
        return (expiry.date() - datetime.now().astimezone().date()).days

    def tier_label(self) -> str:
        """Short account-tier text for the sidebar: 免费 / 已升级 / 未知."""
        return {"paid": "已升级", "free": "免费"}.get(self.account_tier, "未知")

    def area_label(self) -> str:
        """Region name of this tenant, e.g. 'eu-amsterdam-1' -> '阿姆斯特丹'."""
        from app.formatting import region_area

        return region_area(self.region)

    def sidebar_label(self) -> str:
        """Sidebar row text: '区域 - 等级 - 显示名称' (+ optional note)."""
        base = f"{self.area_label()} - {self.tier_label()} - {self.name}"
        note = (self.description or "").strip()
        if note:
            # Collapse whitespace so multi-line notes stay one listbox row.
            note = " ".join(note.split())
            return f"{base} · {note}"
        return base

    def password_status(self, warn_within: int = 14) -> tuple[str, str]:
        """Return (level, text): 'off' | 'ok' | 'warn' | 'expired'."""
        left = self.password_days_left()
        if left is None:
            return "off", "未启用密码到期提醒"
        if left < 0:
            return "expired", f"密码已过期 {abs(left)} 天，请尽快登录甲骨文修改"
        if left <= max(0, warn_within):
            return "warn", f"密码将在 {left} 天后到期"
        return "ok", f"密码将在 {left} 天后到期"

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not (self.name or "").strip():
            errors.append("显示名称不能为空")
        user = (self.user_ocid or "").strip()
        tenancy = (self.tenancy_ocid or "").strip()
        if not user.startswith("ocid1.user."):
            errors.append("User OCID 格式不正确（应以 ocid1.user. 开头）")
        elif user.endswith("...") or "xxxxxxxx" in user.lower() or len(user) < 40:
            # Real user OCIDs are long; demo used short aaaaaaaa... placeholders
            errors.append("User OCID 仍是示例占位内容，请粘贴真实配置")
        if not tenancy.startswith("ocid1.tenancy."):
            errors.append("Tenancy OCID 格式不正确（应以 ocid1.tenancy. 开头）")
        elif tenancy.endswith("...") or "xxxxxxxx" in tenancy.lower() or len(tenancy) < 44:
            errors.append("Tenancy OCID 仍是示例占位内容，请粘贴真实配置")
        fp = (self.fingerprint or "").strip()
        if not fp:
            errors.append("Fingerprint 不能为空")
        elif fp.endswith("...") or fp.lower() in ("aa:bb:cc", "aa:bb:cc:..."):
            errors.append("Fingerprint 仍是示例占位内容")
        elif fp.count(":") < 5 and len(fp) < 16:
            errors.append("Fingerprint 格式看起来不正确")
        region = (self.region or "").strip()
        if not region:
            errors.append("Region 不能为空")
        elif not _REGION_RE.fullmatch(region):
            # 这不是「格式好看点」的校验，是一个 SSRF 边界。
            #
            # region 会被原样塞进 OCI SDK 的 config，而 SDK 的 endpoint 拼装
            # （oci/regions.py::_endpoint_for）有一条「region 里带 '.' 就当成完整
            # 域名、不再追加 oraclecloud.com」的向后兼容分支。于是：
            #
            #   region='attacker.example.com:6379' -> https://iaas.attacker.example.com:6379/...
            #   region='@127.0.0.1:6379'           -> https://iaas.@127.0.0.1:6379/...
            #                                         实际拨号 host=127.0.0.1 port=6379
            #   region='@169.254.169.254'          -> 实际拨号 host=169.254.169.254（云元数据）
            #
            # 「iaas.」被当成 userinfo 吃掉，真正连的是 '@' 后面那截。这条路径
            # 完全不经过 url_safety.py，所有地址/端口/IDNA 控制都不生效，而且
            # /tenants/{id}/test 会把连接结果同步回显，等于一个内网端口探测器。
            # oci.config.validate_config 也管不着——它的 PATTERNS 只有
            # fingerprint / tenancy / user 三项。
            #
            # 只要禁掉 '.'、'@'、':'、'/' 这几个字符，上面每一种构造都不成立：
            # 拼出来的 host 必然是 <service>.<region>.oraclecloud.com。
            errors.append("Region 格式不正确（只允许字母、数字和连字符，如 ap-tokyo-1）")
        key_error = private_key_error(self.private_key_pem)
        if key_error:
            errors.append(key_error)
        if self.compartment_ocid and not self.compartment_ocid.startswith("ocid1."):
            errors.append("Compartment OCID 格式不正确")
        return errors

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize without private key (for UI lists / export of metadata)."""
        d = asdict(self)
        d.pop("private_key_pem", None)
        d["has_private_key"] = bool(self.private_key_pem)
        return d

    def to_storage_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TenantConfig":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in known}
        if "id" not in filtered or not filtered["id"]:
            filtered["id"] = str(uuid.uuid4())
        return cls(**filtered)

    @classmethod
    def new_empty(cls, name: str = "新租户") -> "TenantConfig":
        return cls(
            id=str(uuid.uuid4()),
            name=name,
            user_ocid="",
            tenancy_ocid="",
            fingerprint="",
            region="ap-tokyo-1",
            private_key_pem="",
            compartment_ocid="",
            description="",
            enabled=True,
            color="#3B82F6",
        )


class ConfigStore:
    """Load / save multiple tenant configs under the user data directory."""

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        master_password: Optional[str] = None,
        strict_load: bool = False,
    ):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / CONFIG_FILE
        self.key_path = self.data_dir / KEY_FILE
        self._master_password = master_password
        self._strict_load = strict_load
        self._tenants: dict[str, TenantConfig] = {}
        self._active_tenant_id: Optional[str] = None
        self._fernet = self._build_fernet()
        self.load()

    def _build_fernet(self) -> Fernet:
        if self._master_password:
            salt_path = self.data_dir / ".salt"
            if salt_path.exists():
                salt = salt_path.read_bytes()
            else:
                salt = secrets.token_bytes(16)
                salt_path.write_bytes(salt)
            return _derive_fernet(self._master_password, salt)
        # Machine-local key (no user password). Better than plaintext PEM on disk.
        secret = _machine_secret(self.key_path)
        digest = hashlib.sha256(secret).digest()
        key = base64.urlsafe_b64encode(digest)
        return Fernet(key)

    def _encrypt(self, plain: str) -> str:
        token = self._fernet.encrypt(plain.encode("utf-8"))
        return token.decode("ascii")

    def _decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("无法解密私钥，密钥文件可能已损坏或主密码不匹配") from exc

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def load(self) -> None:
        if not self.config_path.exists():
            self._tenants = {}
            self._active_tenant_id = None
            return
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取租户配置文件: {self.config_path}\n{exc}") from exc
        self._active_tenant_id = raw.get("active_tenant_id")
        tenants: dict[str, TenantConfig] = {}
        healed = False
        for item in raw.get("tenants", []):
            item = deepcopy(item)
            enc = item.pop("private_key_encrypted", None)
            try:
                if enc:
                    item["private_key_pem"] = self._decrypt(enc)
                else:
                    item.setdefault("private_key_pem", "")
                tenant = TenantConfig.from_dict(item)
                # Guard against empty / duplicate ids: colliding ids would make
                # tenants overwrite each other in this dict, so distinct accounts
                # could not be switched between. Regenerate on collision.
                if not tenant.id or tenant.id in tenants:
                    tenant.id = str(uuid.uuid4())
                    healed = True
                tenants[tenant.id] = tenant
            except Exception as exc:
                if self._strict_load:
                    raise ValueError("租户配置包含无法解密或损坏的条目") from exc
                # Skip corrupt entry instead of failing entire startup
                continue
        self._tenants = tenants
        if self._active_tenant_id not in self._tenants:
            self._active_tenant_id = next(iter(self._tenants), None)
        if healed:
            # Persist the repaired ids so the fix sticks across restarts.
            try:
                self.save()
            except Exception:  # noqa: BLE001
                pass

    def save(self) -> None:
        payload = {
            "version": 1,
            "active_tenant_id": self._active_tenant_id,
            "tenants": [],
        }
        for tenant in self._tenants.values():
            item = tenant.to_storage_dict()
            pem = item.pop("private_key_pem", "") or ""
            item["private_key_encrypted"] = self._encrypt(pem) if pem else ""
            payload["tenants"].append(item)

        # Atomic write (os.replace works on Windows when target exists)
        fd, tmp_name = tempfile.mkstemp(prefix="ocibot_", suffix=".json", dir=str(self.data_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self.config_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def list_tenants(self, include_disabled: bool = True) -> list[TenantConfig]:
        items = list(self._tenants.values())
        if not include_disabled:
            items = [t for t in items if t.enabled]
        # Chronological (order the accounts were added), not alphabetical.
        items.sort(key=lambda t: (t.created_at or "", t.name.lower()))
        return items

    def get(self, tenant_id: str) -> Optional[TenantConfig]:
        return self._tenants.get(tenant_id)

    def get_active(self) -> Optional[TenantConfig]:
        if not self._active_tenant_id:
            return None
        return self._tenants.get(self._active_tenant_id)

    def set_active(self, tenant_id: str) -> None:
        if tenant_id not in self._tenants:
            raise KeyError(f"租户不存在: {tenant_id}")
        self._active_tenant_id = tenant_id
        self.save()

    def upsert(self, tenant: TenantConfig, make_active: bool = False) -> TenantConfig:
        errors = tenant.validate()
        if errors:
            raise ValueError("；".join(errors))
        # An empty id would collide with any other id-less tenant and silently
        # overwrite it — always ensure a unique, non-empty id.
        if not tenant.id:
            tenant.id = str(uuid.uuid4())
        tenant.updated_at = _utc_now_iso()
        if tenant.id not in self._tenants:
            tenant.created_at = tenant.created_at or _utc_now_iso()
        self._tenants[tenant.id] = tenant
        if make_active or not self._active_tenant_id:
            self._active_tenant_id = tenant.id
        self.save()
        return tenant

    def delete(self, tenant_id: str) -> None:
        if tenant_id not in self._tenants:
            return
        del self._tenants[tenant_id]
        if self._active_tenant_id == tenant_id:
            self._active_tenant_id = next(iter(self._tenants), None)
        self.save()

    def count(self) -> int:
        return len(self._tenants)

    # ------------------------------------------------------------------
    # Import / Export
    # ------------------------------------------------------------------
    def export_tenant(self, tenant_id: str, path: Path, include_private_key: bool = True) -> None:
        tenant = self.get(tenant_id)
        if not tenant:
            raise KeyError("租户不存在")
        data = tenant.to_storage_dict()
        if not include_private_key:
            data["private_key_pem"] = ""
        path = Path(path)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def export_all(self, path: Path, include_private_key: bool = True) -> None:
        items = []
        for t in self.list_tenants():
            d = t.to_storage_dict()
            if not include_private_key:
                d["private_key_pem"] = ""
            items.append(d)
        Path(path).write_text(
            json.dumps({"version": 1, "tenants": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _export_payload(self, include_private_key: bool = True) -> dict[str, Any]:
        items = []
        for t in self.list_tenants():
            d = t.to_storage_dict()
            if not include_private_key:
                d["private_key_pem"] = ""
            items.append(d)
        return {"version": 1, "tenants": items}

    def backup_to_encrypted_zip(self, path: Path, password: str) -> int:
        """Write all tenants (including private keys) into an AES-256 encrypted zip.

        The zip password is the outer door only — the payload itself is sealed with
        the PBKDF2-SHA256/390k envelope (see ``encode_backup_payload``), because
        WinZip AES pins its own KDF at SHA1/1000 rounds for interoperability and
        that alone is not enough to protect plaintext OCI private keys sitting in
        a file that lives in someone's Downloads folder. Returns the tenant count.
        """
        import pyzipper

        password = (password or "").strip()
        if len(password) < 6:
            raise ValueError("备份密码至少需要 6 位")
        if self.count() == 0:
            raise ValueError("没有可备份的租户")
        payload = self._export_payload(include_private_key=True)
        content = encode_backup_payload(payload, password)
        path = Path(path)
        with pyzipper.AESZipFile(
            path, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
        ) as zf:
            zf.setpassword(password.encode("utf-8"))
            zf.writestr("tenants.json", content)
        return len(payload["tenants"])

    def restore_from_encrypted_zip(
        self, path: Path, password: str, make_active: bool = True
    ) -> list[TenantConfig]:
        """Restore tenants from an AES-encrypted zip created by backup_to_encrypted_zip."""
        import pyzipper

        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"备份文件不存在: {path}")
        try:
            with pyzipper.AESZipFile(path, "r") as zf:
                zf.setpassword((password or "").encode("utf-8"))
                names = zf.namelist()
                member = "tenants.json" if "tenants.json" in names else (names[0] if names else "")
                if not member:
                    raise ValueError("备份文件为空")
                raw = zf.read(member)
        except (RuntimeError, pyzipper.BadZipFile) as exc:
            # Wrong password surfaces as RuntimeError / bad CRC in pyzipper.
            raise ValueError("无法解密备份文件，请检查密码是否正确") from exc
        # Reads both formats: version 2 (encrypted envelope) and the plain JSON that
        # every archive written before it used. Dropping the old format would quietly
        # invalidate every backup an operator already holds — a failure they would
        # only discover on the day they actually need one.
        data = decode_backup_payload(raw, password or "")
        imported: list[TenantConfig] = []
        tenants = data.get("tenants", data if isinstance(data, list) else [])
        for item in tenants:
            imported.append(self.import_from_dict(item, make_active=False))
        if make_active and imported:
            self.set_active(imported[-1].id)
        return imported

    def import_from_dict(self, data: dict[str, Any], make_active: bool = False) -> TenantConfig:
        """Import a single tenant dict (id will be regenerated to avoid collisions)."""
        data = deepcopy(data)
        data["id"] = str(uuid.uuid4())
        # Accept common alias field names from oci config / other tools
        aliases = {
            "user": "user_ocid",
            "tenancy": "tenancy_ocid",
            "key_content": "private_key_pem",
            "key_file_content": "private_key_pem",
            "compartment_id": "compartment_ocid",
            "compartment": "compartment_ocid",
        }
        for src, dst in aliases.items():
            if src in data and not data.get(dst):
                data[dst] = data[src]
        if "name" not in data or not data["name"]:
            data["name"] = data.get("profile") or data.get("tenancy_ocid", "导入租户")[:24]
        tenant = TenantConfig.from_dict(data)
        return self.upsert(tenant, make_active=make_active)

    def import_from_file(self, path: Path, make_active: bool = False) -> list[TenantConfig]:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        imported: list[TenantConfig] = []
        if isinstance(raw, dict) and "tenants" in raw:
            for item in raw["tenants"]:
                imported.append(self.import_from_dict(item, make_active=False))
            if make_active and imported:
                self.set_active(imported[-1].id)
        elif isinstance(raw, dict):
            imported.append(self.import_from_dict(raw, make_active=make_active))
        elif isinstance(raw, list):
            for item in raw:
                imported.append(self.import_from_dict(item, make_active=False))
            if make_active and imported:
                self.set_active(imported[-1].id)
        else:
            raise ValueError("无法识别的配置文件格式")
        return imported

    def import_from_oci_config(
        self,
        config_path: Path,
        profile: str = "DEFAULT",
        name: Optional[str] = None,
        compartment_ocid: str = "",
        make_active: bool = True,
    ) -> TenantConfig:
        """Import from a classic ~/.oci/config + key_file setup."""
        import configparser

        parser = configparser.ConfigParser()
        if not parser.read(Path(config_path), encoding="utf-8"):
            raise FileNotFoundError(f"无法读取 OCI 配置文件: {config_path}")
        section = profile if parser.has_section(profile) else (
            "DEFAULT" if parser.has_section("DEFAULT") else None
        )
        if section is None:
            # configparser may put DEFAULT keys only in defaults
            data = dict(parser.defaults())
            if not data:
                raise KeyError(f"配置文件中找不到 profile: {profile}")
        else:
            data = dict(parser[section])

        key_file = data.get("key_file", "")
        private_key = ""
        if key_file:
            key_path = Path(os.path.expanduser(key_file))
            if not key_path.is_absolute():
                key_path = (Path(config_path).parent / key_path).resolve()
            private_key = key_path.read_text(encoding="utf-8")

        tenant = TenantConfig(
            id=str(uuid.uuid4()),
            name=name or profile or "OCI Config",
            user_ocid=data.get("user", ""),
            tenancy_ocid=data.get("tenancy", ""),
            fingerprint=data.get("fingerprint", ""),
            region=data.get("region", "ap-tokyo-1"),
            private_key_pem=private_key,
            compartment_ocid=compartment_ocid or data.get("compartment_id", ""),
            description=f"Imported from {config_path} [{profile}]",
        )
        return self.upsert(tenant, make_active=make_active)

    def data_location(self) -> str:
        return str(self.config_path)
