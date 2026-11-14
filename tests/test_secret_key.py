"""SECRET_KEY 强度校验。

为什么单独测这一块
------------------
``.env.example`` 里放的是公开的示例值。把它直接复制成 ``.env`` 而不改，
服务**照样能起来**——页面正常、登录正常，只有签名密钥其实是全世界都知道
的值。Django 用它签 session cookie，于是攻击者可以自己伪造一个管理员
session。这类问题不会以报错的形式暴露，只能靠启动时硬拦。

因此这里固定三件事：
1. 占位符与过短的密钥必须被拒绝；
2. 合法密钥必须放行（否则升级会把所有人的服务卡死）；
3. 启动器（launcher.py）与 Django settings 必须用**同一套**判定规则——
   两处规则漂移的后果，要么是启动器没修、要么是修了仍起不来。
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from common.env import (
    SECRET_KEY_MIN_LENGTH,
    secret_key_problem,
)

# ---------------------------------------------------------------------------
# 纯函数：判定规则本身
# ---------------------------------------------------------------------------


class TestSecretKeyProblem:
    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
        ],
    )
    def test_missing_is_rejected(self, value):
        assert secret_key_problem(value) == "缺失"

    @pytest.mark.parametrize(
        "value",
        [
            # .env.example 里的原始值
            "change-me-in-production-please-use-50-chars-or-more",
            # 大小写变体同样要命中
            "Change-Me-In-Production-Please-Use-50-Chars",
            "changeme",
            "your-secret-key-here-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "replace-me-please-please-please-please-please-please",
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        ],
    )
    def test_placeholder_is_rejected(self, value):
        problem = secret_key_problem(value)
        assert problem is not None
        assert problem != "缺失"
        # 提示里要说出"是占位值"，否则用户会以为自己填了个长密钥却仍被拒
        assert "占位" in problem

    def test_too_short_is_rejected(self):
        problem = secret_key_problem("a" * (SECRET_KEY_MIN_LENGTH - 1))
        assert problem is not None
        assert "太短" in problem

    def test_min_length_boundary_passes(self):
        """恰好等于下限必须放行——边界差一位会让升级莫名其妙失败。"""
        assert secret_key_problem("a" * SECRET_KEY_MIN_LENGTH) is None

    def test_realistic_key_passes(self):
        # 50 字符的 token_urlsafe 输出，长度 67
        key = "k9q8piKpV3mN7xR2sT5uW8yA1bC4dE6fG0hJ2kL5nP7qS9tV1xZ3"
        assert secret_key_problem(key) is None

    def test_whitespace_is_stripped(self):
        """从 .env 读出来的值可能带首尾空格，不该因此被判太短。"""
        key = "k9q8piKpV3mN7xR2sT5uW8yA1bC4dE6fG0hJ2kL5nP7qS9tV1xZ3"
        assert secret_key_problem(f"  {key}  ") is None


# ---------------------------------------------------------------------------
# Django settings：启动时必须硬拦
# ---------------------------------------------------------------------------


class TestSettingsValidator:
    def _validate(self, value):
        """取 settings 模块里那个在导入时跑过的校验函数。

        直接 import config.settings 会执行一次真校验（用当前 .env 的值），
        这里只是想复用函数本身，所以单独取出来调用。
        """
        import config.settings as project_settings

        return project_settings._validate_secret_key(value)

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "change-me-in-production-please-use-50-chars-or-more",
            "short",
        ],
    )
    def test_invalid_key_blocks_startup(self, value):
        with pytest.raises(ImproperlyConfigured):
            self._validate(value)

    def test_valid_key_does_not_raise(self):
        self._validate(
            "k9q8piKpV3mN7xR2sT5uW8yA1bC4dE6fG0hJ2kL5nP7qS9tV1xZ3"
        )

    def test_error_message_points_to_fix(self):
        """报错必须告诉用户怎么生成新值，否则只会卡在这里。"""
        with pytest.raises(ImproperlyConfigured) as exc:
            self._validate("change-me-in-production-please-use-50-chars-or-more")

        text = str(exc.value)
        assert "token_urlsafe" in text
        # 一键启动的用户需要知道启动器会自动处理
        assert "start.bat" in text


# ---------------------------------------------------------------------------
# 启动器：必须与 settings 用同一套规则，并主动修复
# ---------------------------------------------------------------------------


class TestLauncherParity:
    def test_launcher_reads_value_from_env_text(self):
        from launcher import _secret_key_problem

        assert _secret_key_problem("DJANGO_SECRET_KEY=change-me\n") is not None
        assert _secret_key_problem("DJANGO_SECRET_KEY=\n") == "缺失"
        assert _secret_key_problem("# 没有这一行\n") == "缺失"

    def test_launcher_accepts_valid_key(self):
        from launcher import _secret_key_problem

        key = "k9q8piKpV3mN7xR2sT5uW8yA1bC4dE6fG0hJ2kL5nP7qS9tV1xZ3"
        assert _secret_key_problem(f"DJANGO_SECRET_KEY={key}\n") is None

    @pytest.mark.parametrize(
        "value",
        [
            "change-me-in-production-please-use-50-chars-or-more",
            "short",
            "",
        ],
    )
    def test_launcher_and_settings_agree(self, value):
        """同一输入下两处判定必须一致。

        不一致的两种后果都很难排查：启动器认为合法 → Django 拒绝启动，
        用户看到报错；启动器认为非法 → 生成了新密钥但 Django 仍拒绝。
        """
        from config.settings import _validate_secret_key
        from launcher import _secret_key_problem

        launcher_says_bad = (
            _secret_key_problem(f"DJANGO_SECRET_KEY={value}\n") is not None
        )

        try:
            _validate_secret_key(value)
            settings_says_bad = False
        except ImproperlyConfigured:
            settings_says_bad = True

        assert launcher_says_bad == settings_says_bad

    def test_launcher_replaces_placeholder_in_file(self, tmp_path, monkeypatch):
        """已有 .env 里的占位密钥会被启动器换成随机值，用户无需手工改。"""
        import launcher

        env_file = tmp_path / ".env"
        env_file.write_text(
            "DJANGO_SECRET_KEY=change-me-in-production-please-use-50-chars-or-more\n"
            "FIELD_ENCRYPTION_KEY=abc\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(launcher, "ENV_FILE", env_file)

        assert launcher.ensure_env_file() == 0

        content = env_file.read_text(encoding="utf-8")
        assert "change-me" not in content
        # 换成了合法密钥
        assert launcher._secret_key_problem(content) is None
        # 加密密钥不能被动——换了它已加密的密码就永久解不开了
        assert "FIELD_ENCRYPTION_KEY=abc" in content

    def test_launcher_leaves_valid_env_untouched(self, tmp_path, monkeypatch):
        import launcher

        key = "k9q8piKpV3mN7xR2sT5uW8yA1bC4dE6fG0hJ2kL5nP7qS9tV1xZ3"
        env_file = tmp_path / ".env"
        original = f"DJANGO_SECRET_KEY={key}\nFIELD_ENCRYPTION_KEY=abc\n"
        env_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr(launcher, "ENV_FILE", env_file)

        launcher.ensure_env_file()

        # 幂等：合法配置一个字节都不该变，否则每次启动都让登录会话失效
        assert env_file.read_text(encoding="utf-8") == original
