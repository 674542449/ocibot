"""A real, throwaway RSA private key for tests that need a *parseable* one.

Why this module exists: `TenantConfig.validate()` now actually parses the PEM
with `cryptography.serialization.load_pem_private_key` instead of merely
grepping for "BEGIN" + "PRIVATE KEY". That fix is the point — the panel used to
accept `-----BEGIN PRIVATE KEY-----\\nnot-base64\\n-----END PRIVATE KEY-----`
with a 201 and `has_private_key: true`, and since 保存后自动测试连接 defaults
off the operator saw 已添加 and then every later page 502'd with an opaque SDK
deserialization error.

The suite's fixtures were marker-shaped strings like
`"-----BEGIN PRIVATE KEY-----\\nMIIBOgIBAAJBAK\\n-----END PRIVATE KEY-----"`,
which the new validation correctly rejects. The fix is to give the tests a key
that is genuinely well-formed — **not** to loosen the validation, which would
throw the bug away to keep the fixtures.

Generated at import rather than hardcoded: committing a real private key, even
a throwaway, trips secret scanners and makes a reviewer stop and check whether
it is live. One 2048-bit keygen (~0.1s) happens once per test session because
this module is imported once.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

#: PKCS#8 ("BEGIN PRIVATE KEY") — the form the OCI console hands out.
TEST_PEM: str = _key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")

#: PKCS#1 ("BEGIN RSA PRIVATE KEY") — older exports still look like this.
TEST_PEM_PKCS1: str = _key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.TraditionalOpenSSL,
    encryption_algorithm=serialization.NoEncryption(),
).decode("ascii")

#: Passphrase-protected — must be REJECTED with a message that says so, because
#: the OCI console will happily hand the operator one of these.
TEST_PEM_ENCRYPTED: str = _key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.BestAvailableEncryption(b"hunter2"),
).decode("ascii")
