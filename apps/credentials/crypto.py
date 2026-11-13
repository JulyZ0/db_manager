"""敏感字段加解密与密码生成。

这是需要你手写的四个核心算法之一（见 README「待手写清单」）。
接口：密文对外形如 `v1:<fernet-token>`，v1 为密钥版本前缀。
"""

from __future__ import annotations

import hashlib

from django.conf import settings

CURRENT_KEY_VERSION = "v1"
CIPHERTEXT_SEPARATOR = ":"


class DecryptionError(Exception):
    """密文损坏、密钥不匹配或版本未知。"""


def encrypt_secret(plaintext: str) -> str:
    """加密明文，返回 `v1:<token>`。

    TODO(手写) 实现要点：
    1. 从 settings.FIELD_ENCRYPTION_KEY 构造 Fernet（每次调用新建开销很小，
       若要优化可加 lru_cache）。
    2. token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    3. 返回 f"{CURRENT_KEY_VERSION}:{token}"
    4. 空字符串如何处理由你决定（建议返回 ""，表示未设置密码）。
    """
    raise NotImplementedError("TODO: 实现加密")


def decrypt_secret(ciphertext: str) -> str:
    """解密 `v1:<token>`，返回明文。

    TODO(手写) 实现要点：
    1. 空字符串直接返回 ""。
    2. 按 CIPHERTEXT_SEPARATOR 拆分，校验版本前缀；未知版本抛 DecryptionError。
    3. cryptography.fernet.InvalidToken → DecryptionError（说明密钥不匹配或密文被篡改）。
    """
    raise NotImplementedError("TODO: 实现解密")


def generate_password(length: int | None = None) -> str:
    """生成强随机密码（密码学安全）。

    TODO(手写) 实现要点：
    1. length 默认取 settings.PASSWORD_LENGTH（32）。
    2. 字符集：大小写字母 + 数字 + 符号，例如
       string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    3. 强制四类字符各至少一个：先各取一个，再用 secrets.choice 补足，最后 secrets.shuffle。
    4. 全程使用 secrets 模块，禁止 random。
    """
    raise NotImplementedError("TODO: 实现密码生成")


def fingerprint(secret: str) -> str:
    """密码指纹：sha256 前 16 位十六进制。

    用于轮换历史审计：可比较两个密码是否相同，但不可逆推出明文。
    只保存指纹，永不保存明文或密文，这是 PRD SEC-5 的要求。
    """
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
