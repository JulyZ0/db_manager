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
