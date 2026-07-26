"""Parse pasted OCI config / API text (+ embedded private key PEM)."""

from __future__ import annotations

import re
from typing import Any, Optional

from app.config_store import parse_oci_api_text

# Marker-only patterns, matched independently. A single regex spanning BEGIN..END
# with a ".*?" body backtracks quadratically: text with many BEGIN markers and no
# END marker cost ~2.3s of GIL-held CPU per 108KB (4x per doubling), stalling
# every other request in the process. Locating the two markers separately makes
# extraction linear in the input length.
_PEM_BEGIN_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE)
_PEM_END_RE = re.compile(r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE)


def _pem_span(text: str, start: int = 0) -> Optional[tuple[int, int]]:
    """Half-open span of the next complete PEM block at or after ``start``."""
    begin = _PEM_BEGIN_RE.search(text, start)
    if begin is None:
        return None
    end = _PEM_END_RE.search(text, begin.end())
    if end is None:
        return None
    return begin.start(), end.end()


def extract_private_key_pem(text: str) -> str:
    """Return the first PEM private key block found in free text, or empty."""
    if not text:
        return ""
    span = _pem_span(text)
    if span is None:
        return ""
    block = text[span[0] : span[1]].strip()
    # Normalize line endings
    return block.replace("\r\n", "\n").replace("\r", "\n")


def strip_private_key_pem(text: str) -> str:
    """Remove PEM blocks so configparser is not confused by key material."""
    if not text:
        return ""
    out: list[str] = []
    pos = 0
    while True:
        span = _pem_span(text, pos)
        if span is None:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos : span[0]])
        pos = span[1]


def _looks_like_placeholder_ocid(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return True
    low = v.lower()
    if low.endswith("...") or "xxxxxxxx" in low:
        return True
    if "aaaaaaaa" in low and len(v) < 50:
        return True
    return False


def _is_placeholder_api_text(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    low = raw.lower()
    if "格式示例（请勿原样保存）" in raw:
        return True
    if "user=ocid1.user.oc1..xxxxxxxx" in low and "tenancy=ocid1.tenancy.oc1..xxxxxxxx" in low:
        return True
    if "ocid1.user.oc1..aaaaaaaa..." in low or "ocid1.tenancy.oc1..aaaaaaaa..." in low:
        return True
    return False


def parse_pasted_oci_bundle(
    api_text: str,
    *,
    private_key_pem: str = "",
    name_override: str = "",
    description: str = "",
    compartment_override: str = "",
) -> dict[str, Any]:
    """
    Parse pasted OCI config text and optional PEM.

    Returns dict:
      ok, message, warnings[], fields for tenant create, has_private_key, key_file_hint
    """
    warnings: list[str] = []
    raw = (api_text or "").strip()
    if _is_placeholder_api_text(raw):
        return {
            "ok": False,
            "message": "文本为空或仍是示例内容，请粘贴真实 API 配置",
            "warnings": warnings,
            "name": "",
            "user_ocid": "",
            "tenancy_ocid": "",
            "fingerprint": "",
            "region": "",
            "compartment_ocid": "",
            "private_key_pem": "",
            "has_private_key": False,
            "key_file_hint": "",
        }

    embedded_pem = extract_private_key_pem(raw)
    config_text = strip_private_key_pem(raw).strip()
    parsed = parse_oci_api_text(config_text) if config_text else {}

    # Also try parsing full text if strip left nothing useful (edge cases)
    if not parsed and config_text != raw:
        parsed = parse_oci_api_text(raw)

    pem = (private_key_pem or "").strip() or embedded_pem
    if pem and "PRIVATE KEY" not in pem.upper():
        return {
            "ok": False,
            "message": "私钥内容无效（需要 PEM 格式 PRIVATE KEY）",
            "warnings": warnings,
            "name": "",
            "user_ocid": "",
            "tenancy_ocid": "",
            "fingerprint": "",
            "region": "",
            "compartment_ocid": "",
            "private_key_pem": "",
            "has_private_key": False,
            "key_file_hint": parsed.get("key_file") or "",
        }

    user = (parsed.get("user_ocid") or "").strip()
    tenancy = (parsed.get("tenancy_ocid") or "").strip()
    fingerprint = (parsed.get("fingerprint") or "").strip()
    region = (parsed.get("region") or "").strip() or "ap-tokyo-1"
    compartment = (compartment_override or parsed.get("compartment_ocid") or "").strip()
    key_file_hint = (parsed.get("key_file") or "").strip()

    name = (name_override or "").strip()
    if not name:
        name = (parsed.get("name") or "").strip()
    if not name:
        profile = (parsed.get("profile") or "").strip()
        if profile and profile.upper() != "DEFAULT":
            name = profile
    if not name and region:
        name = f"OCI-{region}"
    if not name:
        name = "未命名租户"

    if not parsed:
        return {
            "ok": False,
            "message": "未能解析出有效字段，请检查是否为 OCI config / key=value 格式",
            "warnings": warnings,
            "name": name,
            "user_ocid": user,
            "tenancy_ocid": tenancy,
            "fingerprint": fingerprint,
            "region": region,
            "compartment_ocid": compartment,
            "private_key_pem": pem,
            "has_private_key": bool(pem),
            "key_file_hint": key_file_hint,
        }

    if _looks_like_placeholder_ocid(user) or _looks_like_placeholder_ocid(tenancy):
        return {
            "ok": False,
            "message": "解析到的 OCID 仍是占位内容，请确认粘贴的是真实配置",
            "warnings": warnings,
            "name": name,
            "user_ocid": user,
            "tenancy_ocid": tenancy,
            "fingerprint": fingerprint,
            "region": region,
            "compartment_ocid": compartment,
            "private_key_pem": pem,
            "has_private_key": bool(pem),
            "key_file_hint": key_file_hint,
        }

    if not pem:
        if key_file_hint:
            warnings.append(
                f"配置里写了 key_file={key_file_hint}，但网页端无法读取你电脑上的本地路径。"
                "请把 .pem 私钥内容一并粘贴到「私钥」框（或与 config 贴在同一段文本里）。"
            )
        else:
            warnings.append("未找到私钥 PEM，请在下方粘贴私钥，或与 config 一起粘贴。")

    missing = []
    if not user:
        missing.append("user")
    if not tenancy:
        missing.append("tenancy")
    if not fingerprint:
        missing.append("fingerprint")
    if not region:
        missing.append("region")
    if missing and not pem:
        return {
            "ok": False,
            "message": "缺少字段：" + ", ".join(missing) + "；且没有私钥",
            "warnings": warnings,
            "name": name,
            "user_ocid": user,
            "tenancy_ocid": tenancy,
            "fingerprint": fingerprint,
            "region": region,
            "compartment_ocid": compartment,
            "private_key_pem": pem,
            "has_private_key": False,
            "key_file_hint": key_file_hint,
        }
    if missing:
        return {
            "ok": False,
            "message": "缺少字段：" + ", ".join(missing),
            "warnings": warnings,
            "name": name,
            "user_ocid": user,
            "tenancy_ocid": tenancy,
            "fingerprint": fingerprint,
            "region": region,
            "compartment_ocid": compartment,
            "private_key_pem": pem,
            "has_private_key": bool(pem),
            "key_file_hint": key_file_hint,
        }

    if not pem:
        return {
            "ok": False,
            "message": "已解析 config，但还需要私钥 PEM 才能保存",
            "warnings": warnings,
            "name": name,
            "user_ocid": user,
            "tenancy_ocid": tenancy,
            "fingerprint": fingerprint,
            "region": region,
            "compartment_ocid": compartment,
            "private_key_pem": "",
            "has_private_key": False,
            "key_file_hint": key_file_hint,
        }

    return {
        "ok": True,
        "message": (
            f"已解析 user={user[:12]}…  tenancy={tenancy[:12]}…  "
            f"region={region}  fingerprint={fingerprint[:18]}…"
        ),
        "warnings": warnings,
        "name": name,
        "user_ocid": user,
        "tenancy_ocid": tenancy,
        "fingerprint": fingerprint,
        "region": region,
        "compartment_ocid": compartment,
        "private_key_pem": pem,
        "has_private_key": True,
        "key_file_hint": key_file_hint,
        "description": (description or "").strip(),
    }
