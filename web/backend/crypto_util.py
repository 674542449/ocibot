"""Encrypt OCI private keys at rest (server-side master key)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from web.backend.config import get_settings


def _fernet() -> Fernet:
    settings = get_settings()
    # Derive a stable 32-byte urlsafe key from the configured master secret.
    digest = hashlib.sha256(settings.master_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_text(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_text(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("无法解密私钥：主密钥不匹配或数据已损坏") from exc
