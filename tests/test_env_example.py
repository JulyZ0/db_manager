"""`.env.example` 与代码的一致性测试。

为什么需要它
------------
``.env.example`` 是运维唯一的配置说明书。它漏一项的后果很具体：
开关存在于代码里、却没有任何地方告诉运维"可以配"，于是这个人永远
用着默认值，遇到问题也不知道该调什么。

这种漏项是**静默**的——加新配置项时改的是 settings.py，没人会记得
回头同步一个示例文件。所以用测试把它变成"忘了就变红"。

反向也要查：`.env.example` 里写了但代码根本不读的键，会让人以为
配置生效了、实际毫无作用，比漏项更糟。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from django.conf import settings

SETTINGS_DIR = Path(settings.BASE_DIR)

# 代码里读取环境变量的两种写法：env("X") / env_int("X") / ...
_ENV_CALL = re.compile(r"\benv(?:_bool|_int|_list|_float)?\(\s*[\"']([A-Z0-9_]+)[\"']")

# 这些键由启动器/测试自行读取，不属于 .env 说明书范围
_NOT_FOR_ENV_EXAMPLE = {
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "DISABLE_SCHEDULER",
    "PYTEST_CURRENT_TEST",
    "RUN_MAIN",
}


def _read_env_example_keys() -> set[str]:
    text = (SETTINGS_DIR / ".env.example").read_text(encoding="utf-8")
    keys = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    return keys


def _read_code_keys() -> set[str]:
    """扫描 Python 源码，收集 env() 读取过的键。"""
    keys: set[str] = set()
    for path in SETTINGS_DIR.rglob("*.py"):
        # 跳过虚拟环境、迁移缓存、测试自身（测试里会写 env("X") 这种示例，
        # 扫进来会凭空多出一个不存在的键）
        parts = set(path.parts)
        if {".venv", "venv", "__pycache__", "pytest-of-athenavi", "tests"} & parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        keys.update(_ENV_CALL.findall(source))
    keys -= _NOT_FOR_ENV_EXAMPLE
    return keys


class TestEnvExampleCompleteness:
    def test_every_env_key_is_documented(self):
        """代码读的每个环境变量，都必须在 .env.example 里出现。"""
        code_keys = _read_code_keys()
        documented = _read_env_example_keys()
        missing = sorted(code_keys - documented)

        assert not missing, (
            ".env.example 缺少以下环境变量说明（代码会读取，但运维不知道可以配）：\n  "
            + "\n  ".join(missing)
        )

    def test_no_phantom_keys(self):
        """.env.example 里不能有代码根本不读的键。

        写了却不生效比漏写更糟：运维改了它、以为生效了，实际毫无作用。
        """
        documented = _read_env_example_keys()
        code_keys = _read_code_keys()
        phantom = sorted(documented - code_keys)

        assert not phantom, (
            ".env.example 里这些键代码并不读取，会让人误以为配置生效：\n  "
            + "\n  ".join(phantom)
        )

    def test_every_key_has_a_comment_above_or_beside(self):
        """每个配置项都要有说明——小白运维靠这些注释判断"该不该改"。"""
        text = (SETTINGS_DIR / ".env.example").read_text(encoding="utf-8")
        lines = text.splitlines()

        undocumented = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            # 向上找最近的非空行，必须是注释；或本行行尾有 # 注释
            if "#" in stripped.split("=", 1)[1]:
                continue
            prev = index - 1
            while prev >= 0 and not lines[prev].strip():
                prev -= 1
            if prev < 0 or not lines[prev].strip().startswith("#"):
                undocumented.append(stripped.split("=", 1)[0].strip())

        assert not undocumented, (
            ".env.example 里这些配置项没有注释说明：\n  " + "\n  ".join(undocumented)
        )

    def test_debug_defaults_to_false(self):
        """.env.example 里的 DEBUG 必须是 False。

        示例文件会被直接复制成 .env，写成 True 等于把"生产忘记改"这条路
        铺好了：报错页会吐出源码片段、SQL 与配置。
        """
        text = (SETTINGS_DIR / ".env.example").read_text(encoding="utf-8")
        match = re.search(r"^DJANGO_DEBUG=(.*)$", text, re.MULTILINE)
        assert match, ".env.example 缺少 DJANGO_DEBUG"
        assert match.group(1).strip().lower() in {"false", "0", ""}, (
            f"DJANGO_DEBUG 在示例里应为 False，实际是 {match.group(1)!r}"
        )

    def test_https_disabled_by_default(self):
        """内网默认 http://内网IP:8000；示例里打开 HTTPS 会让人直接打不开页面。"""
        text = (SETTINGS_DIR / ".env.example").read_text(encoding="utf-8")
        match = re.search(r"^HTTPS_ENABLED=(.*)$", text, re.MULTILINE)
        assert match, ".env.example 缺少 HTTPS_ENABLED"
        assert match.group(1).strip().lower() in {"false", "0", ""}

    def test_no_real_secret_in_example(self):
        """示例里不能出现真密钥——它会被提交进 git。"""
        text = (SETTINGS_DIR / ".env.example").read_text(encoding="utf-8")

        # FIELD_ENCRYPTION_KEY 必须是空的：示例不存在"合法占位值"
        match = re.search(r"^FIELD_ENCRYPTION_KEY=(.*)$", text, re.MULTILINE)
        assert match, ".env.example 缺少 FIELD_ENCRYPTION_KEY"
        assert match.group(1).strip() == "", (
            "FIELD_ENCRYPTION_KEY 在示例里必须留空，"
            "否则有人会直接用一个公开的密钥加密真实密码"
        )


@pytest.mark.parametrize(
    "key",
    [
        "DJANGO_DEBUG",
        "HTTPS_ENABLED",
        "TRUST_PROXY_HEADERS",
        "SEARCH_MODE",
        "SESSION_COOKIE_AGE",
        "LOGIN_MAX_FAILURES",
        "LOGIN_MAX_FAILURES_PER_IP",
        "SLOW_LOG_RETENTION_DAYS",
        "AUDIT_LOG_RETENTION_DAYS",
        "ROTATION_LOG_RETENTION_DAYS",
        "LOG_CLEANUP_BATCH_SIZE",
        "LOG_CLEANUP_MAX_BATCHES",
        "DB_QUERY_TIMEOUT_SECONDS",
        "CSRF_TRUSTED_ORIGINS",
        "SLOW_LOG_ASYNC",
    ],
)
def test_newly_added_switches_present(key):
    """本轮新增的开关逐项点名，避免被通配逻辑漏掉。"""
    assert key in _read_env_example_keys(), f".env.example 缺少 {key}"
