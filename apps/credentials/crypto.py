"""敏感字段加解密与密码生成。

接口：密文对外形如 `v1:<fernet-token>`，v1 为密钥版本前缀。

安全要点（PRD SEC-1 / SEC-5）：
- 加密每次使用 Fernet 的随机 IV，**相同明文必须得到不同密文**，
  否则库里能直接看出"这两个实例密码一样"。
- 明文绝不进入日志、异常信息或接口响应。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import string
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger(__name__)

CURRENT_KEY_VERSION = "v1"
CIPHERTEXT_SEPARATOR = ":"

# 密码字符集：大小写字母 + 数字 + 符号
LOWERCASE = string.ascii_lowercase
UPPERCASE = string.ascii_uppercase
DIGITS = string.digits
SYMBOLS = "!@#$%^&*()-_=+"
ALL_CHARS = LOWERCASE + UPPERCASE + DIGITS + SYMBOLS

# 四类字符各至少要出现一次，因此长度下限是 4
MIN_PASSWORD_LENGTH = 4


class DecryptionError(Exception):
    """密文损坏、密钥不匹配或版本未知。"""


class EncryptionError(Exception):
    """加密失败：密钥缺失或格式非法。"""


# ---------------------------------------------------------------------------
# 内部：Fernet 实例
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _build_fernet(key: str) -> Fernet:
    """按密钥值缓存 Fernet 实例。

    只缓存 Fernet 对象本身——它不含任何明文或密文，缓存是安全的。
    每次调用都重新构造会重复做一次 base64 解码与长度校验，
    在批量轮换（每个实例至少两次）时是可观的浪费。
    """
    return Fernet(key)


def _get_fernet() -> Fernet:
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    if not key:
        # fail fast：宁可启动后第一次用到就报错，也不允许降级为明文
        raise EncryptionError(
            "FIELD_ENCRYPTION_KEY 未配置，拒绝以明文形式处理密码"
        )
    try:
        return _build_fernet(key)
    except (ValueError, TypeError) as exc:
        raise EncryptionError(
            f"FIELD_ENCRYPTION_KEY 格式非法（应为 32 字节 urlsafe base64）："
            f"{type(exc).__name__}"
        ) from exc


# ---------------------------------------------------------------------------
# 加解密
# ---------------------------------------------------------------------------


def encrypt_secret(plaintext: str) -> str:
    """加密明文，返回 `v1:<token>`；空字符串原样返回 ""（表示未设置）。

    注意这里**故意不加 lru_cache**：
    Fernet 每次加密使用随机 IV，相同明文应得到不同密文。加了缓存会让
    相同密码在库里留下相同密文，泄露"哪些实例共用同一密码"的信息，
    同时把明文长期驻留在进程内存中。
    """
    if not plaintext:
        return ""

    fernet = _get_fernet()
    try:
        token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    except Exception as exc:  # noqa: BLE001 - 统一包装成加密异常
        # 只记录异常类型，不记录明文
        logger.error("加密失败：%s", type(exc).__name__)
        raise EncryptionError(f"加密失败：{type(exc).__name__}") from exc

    return f"{CURRENT_KEY_VERSION}{CIPHERTEXT_SEPARATOR}{token}"


def decrypt_secret(ciphertext: str) -> str:
    """解密 `v1:<token>`，返回明文；空字符串原样返回 ""。

    这里**故意不加 lru_cache**（此前加过 maxsize=512，已移除）。

    缓存的是"密文 → 明文"的映射，看上去 key 不含明文，但 value 就是明文
    本身：最多 512 个实例的登录口令会常驻进程堆内存，直到进程退出或被覆盖。
    代价很不划算：

    - 收益：省下一次 AES 解密，量级是微秒。相对于紧接着要做的 TCP 建连
      （毫秒到秒级）完全可忽略。
    - 风险：core dump、内存转储、debugger 附加、堆内存泄露，都会一次性
      暴露全部实例口令；而这些明文本来只需要存活一次连接的时间。

    Fernet 实例的缓存（``_build_fernet``）保留——它不含任何明文或密文，
    重复构造只是多一次 base64 解码与长度校验。

    注意：若运行期更换了 FIELD_ENCRYPTION_KEY，解密会失败（密钥不匹配），
    换密钥后必须重启进程并重新登记密码。
    """
    if not ciphertext:
        return ""

    version, sep, token = ciphertext.partition(CIPHERTEXT_SEPARATOR)
    if not sep:
        raise DecryptionError("密文格式非法：缺少版本前缀，应为 v1:<token>")
    if version != CURRENT_KEY_VERSION:
        raise DecryptionError(f"未知的密钥版本：{version!r}")

    fernet = _get_fernet()
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        # 密钥不匹配，或密文被截断/篡改
        raise DecryptionError("密文无法解密：密钥不匹配或内容已被篡改") from exc
    except (ValueError, TypeError) as exc:
        raise DecryptionError(f"密文格式非法：{type(exc).__name__}") from exc


# ---------------------------------------------------------------------------
# 密码生成与指纹
# ---------------------------------------------------------------------------


def generate_password(length: int | None = None) -> str:
    """生成强随机密码（密码学安全）。

    保证大小写字母、数字、符号四类各至少出现一次：
    先各取一个，再用全集补足长度，最后整体打乱——
    否则固定的前置字符会泄露结构信息。

    全程使用 secrets 模块；random 是可预测的，绝不能用于密码。
    """
    if length is None:
        length = getattr(settings, "PASSWORD_LENGTH", 32)
    if length < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"密码长度至少为 {MIN_PASSWORD_LENGTH}（需容纳四类字符各至少一个）"
        )

    chars = [
        secrets.choice(LOWERCASE),
        secrets.choice(UPPERCASE),
        secrets.choice(DIGITS),
        secrets.choice(SYMBOLS),
    ]
    chars.extend(secrets.choice(ALL_CHARS) for _ in range(length - len(chars)))
    # secrets 模块没有 shuffle；SystemRandom 用 os.urandom 作为随机源，
    # 是密码学安全的洗牌方式（random.shuffle 可被预测，不能用）。
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def fingerprint(secret: str) -> str:
    """密码指纹：sha256 前 16 位十六进制。

    用于轮换历史审计：可比较两个密码是否相同，但不可逆推出明文。
    只保存指纹，永不保存明文或密文，这是 PRD SEC-5 的要求。

    不加 lru_cache：缓存 key 就是明文密码本身，等于把这些口令按原文
    常驻进程内存，和"永不保存明文"的口径直接冲突；何况 sha256 只有
    微秒级开销，缓存换不来任何有意义的收益。
    """
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
