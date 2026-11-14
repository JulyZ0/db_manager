"""极简 .env 读取工具：避免为几行配置引入 django-environ。

约定：
- 每行 KEY=VALUE，忽略空行与 # 开头的注释
- 值可用单引号或双引号包裹，包裹时内容原样保留
- 已存在的真实环境变量优先，不被 .env 覆盖
- 文件不存在时静默跳过（生产环境通常直接用真实环境变量）
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: str | Path) -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env(key: str, default: str | None = None) -> str | None:
    """读取字符串配置，未设置时返回 default。"""
    value = os.environ.get(key)
    if value is None or value == "":
        return default
    return value


def env_bool(key: str, default: bool = False) -> bool:
    value = env(key)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_int(key: str, default: int) -> int:
    value = env(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_list(key: str, default: list[str] | None = None) -> list[str]:
    value = env(key)
    if value is None:
        return list(default or [])
    return [item.strip() for item in value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# 密钥强度判定
# ---------------------------------------------------------------------------
#
# 放在这里而不是各自的调用点（config/settings.py 与 launcher.py），
# 是因为两处判定必须完全一致：settings 认为非法、launcher 认为合法的结果，
# 是启动器"修好了"却又启动失败；反过来则是启动器错过修复、用户看到报错退出。
# 曾经这里是两份复述的常量，改动一处就漂移——所以收敛成单一真相源。

# .env.example 里出现的示例值片段。任何包含这些片段的值都视为"没改过"。
# 小写比较，因此大小写变体同样命中。
SECRET_KEY_PLACEHOLDERS = (
    "change-me",
    "changeme",
    "your-secret-key",
    "your_secret_key",
    "replace-me",
    "please-use-50-chars",
    "xxxxxxxx",
)

# Django 官方脚手架生成 50 字符；取 50 作为下限与文档口径一致。
# 低于这个长度的密钥熵不足，暴力枚举签名的成本会显著下降。
SECRET_KEY_MIN_LENGTH = 50


def secret_key_problem(value: str | None) -> str | None:
    """判断 SECRET_KEY 是否可用；可用返回 None，否则返回问题描述。

    为什么要判占位符
    ----------------
    ``.env.example`` 里放的是一个公开的示例值。谁把它直接复制成 ``.env``
    而不改，就会带着一个**任何人都知道**的密钥上生产：Django 用它签名
    session cookie、密码重置 token、messages 数据。密钥公开意味着这些
    签名可以伪造——攻击者不需要登录就能构造出合法管理员 session。

    这类事故的可怕之处是"看起来一切正常"：服务能起、页面能开。
    """
    if not value or not value.strip():
        return "缺失"

    cleaned = value.strip()
    lowered = cleaned.lower()
    for marker in SECRET_KEY_PLACEHOLDERS:
        if marker in lowered:
            return f"仍是示例占位值（含「{marker}」）"

    if len(cleaned) < SECRET_KEY_MIN_LENGTH:
        return f"太短（{len(cleaned)} 字符，至少 {SECRET_KEY_MIN_LENGTH}）"

    return None
