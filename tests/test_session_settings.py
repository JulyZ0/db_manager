"""会话过期与 Cookie 属性的测试。

为什么值得钉死
--------------
这些配置的特点是**写错了不会报错、也不会立刻坏**：不设 SESSION_COOKIE_AGE
时 Django 静默用 2 周；SameSite 写错只在跨站场景才表现异常；HTTPONLY
被后一行覆盖时谁生效取决于语句顺序。都是"看着没问题、出事才发现"的类型，
所以用断言把当前的安全基线固定下来。
"""

from __future__ import annotations

import importlib

import pytest
from django.conf import settings

pytestmark = pytest.mark.django_db


class TestSessionAge:
    def test_cookie_age_is_set_and_not_two_weeks(self):
        """默认值必须是显式设置的，且明显短于 Django 的 2 周默认。"""
        assert settings.SESSION_COOKIE_AGE > 0
        assert settings.SESSION_COOKIE_AGE < 14 * 24 * 3600
        # 至少覆盖一个完整班次（8 小时），否则正常使用会被频繁打断
        assert settings.SESSION_COOKIE_AGE >= 8 * 3600

    def test_save_every_request_off_by_default(self):
        """滑动续期默认关闭：否则每个只读请求都会写一次 session 表，
        把只读流量变成写流量（本项目写路径还要过 SQLite 写锁）。"""
        assert settings.SESSION_SAVE_EVERY_REQUEST is False

    def test_browser_close_expiry_off_by_default(self):
        """内网运维习惯让页面开着过夜，默认不按关浏览器过期。"""
        assert settings.SESSION_EXPIRE_AT_BROWSER_CLOSE is False


class TestCookieAttributes:
    def test_samesite_is_lax(self):
        """Lax 而不是 Strict：Strict 下从内部文档点链接进来不带 cookie，
        用户会以为自己没登录。"""
        assert settings.SESSION_COOKIE_SAMESITE == "Lax"
        assert settings.CSRF_COOKIE_SAMESITE == "Lax"

    def test_session_cookie_httponly_on_in_production(self):
        """DEBUG=False 时必须 HttpOnly（这个仓库的 .env 就是 False）。"""
        if not settings.DEBUG:
            assert settings.SESSION_COOKIE_HTTPONLY is True

    def test_csrf_cookie_httponly_on_in_production(self):
        """CSRF cookie 也对 JS 隐藏——页面从 <meta> 拿 token，不读 cookie。"""
        if not settings.DEBUG:
            assert settings.CSRF_COOKIE_HTTPONLY is True

    def test_secure_flags_follow_https_switch(self):
        """Secure 标记只在声明"我在 TLS 后面"时才打开。

        内网默认是 http://内网IP:8000，此时加 Secure 会让浏览器不回传
        cookie，表现为"登录后立刻又变未登录"。
        """
        assert settings.SESSION_COOKIE_SECURE is settings.HTTPS_ENABLED
        assert settings.CSRF_COOKIE_SECURE is settings.HTTPS_ENABLED


class TestNoDuplicateDefinitions:
    """同一配置项不能被"顶层 + 分支"或"顶层两次"重复赋值。

    这条是为一个具体的历史 bug 立的：TRUST_PROXY_HEADERS 曾在两个位置
    各定义一次，后一处静默覆盖前一处，排查时看到的值和实际生效的不是一个。
    SESSION_COOKIE_HTTPONLY 也差点重演。

    用 AST 而不是数文本行：`if not DEBUG:` 块内的赋值靠缩进区分，
    纯文本匹配要么漏（只看行首）要么误报（分支互斥的 if/else 本就各写一次）。
    """

    # 这些项允许在 if/else 两个分支里各写一次（互斥，不会同时生效）
    def test_security_flags_defined_once(self):
        import ast
        from pathlib import Path

        source = Path(settings.BASE_DIR, "config", "settings.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)

        def targets(node):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        yield t.id
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                yield node.target.id

        top_level: dict[str, int] = {}
        for stmt in tree.body:
            for name in targets(stmt):
                top_level[name] = top_level.get(name, 0) + 1

        nested: set[str] = set()
        for stmt in tree.body:
            if stmt in tree.body and isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                continue
            for child in ast.walk(stmt):
                if isinstance(child, (ast.Assign, ast.AnnAssign)):
                    nested.update(targets(child))

        for name in (
            "SESSION_COOKIE_HTTPONLY",
            "SESSION_COOKIE_SECURE",
            "CSRF_COOKIE_SECURE",
            "SESSION_COOKIE_AGE",
            "SESSION_COOKIE_SAMESITE",
            "SETTINGS_DEBUG_FLAG",
        ):
            count = top_level.get(name, 0)
            assert count <= 1, (
                f"{name} 在 settings.py 顶层被赋值 {count} 次，"
                f"后一处会静默覆盖前一处"
            )
            if count == 1:
                assert name not in nested, (
                    f"{name} 同时在顶层和条件块内被赋值，"
                    f"实际生效值取决于语句位置，极易误判"
                )
