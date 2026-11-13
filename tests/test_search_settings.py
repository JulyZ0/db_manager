"""搜索策略与生产安全开关的测试。

两件事都属于"配错了也不会立刻报错、但后果很重"的类型：

- 搜索策略：``contains`` 与 ``prefix`` 的语义差异，以及非法配置必须
  安全退回 ``contains``（宁可慢，不能搜不到）。
- HTTPS 开关：默认必须**全部关闭**。内网默认走 http，若默认开
  ``SECURE_SSL_REDIRECT``，用户会被跳到没有 TLS 监听的地址，
  现象是"打不开"——服务其实正常，却像彻底挂了。
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from common.search import MODE_CONTAINS, MODE_PREFIX, build_search_q, current_mode

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded(db):
    """一份最小台账：1 部门 / 1 集群 / 1 实例。

    不 import test_web.py 的同名夹具——测试文件之间互相 import 会让
    两个文件的事实绑定在一起，改一处就牵连另一处。
    """
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="基础架构部", code="INFRA")
    cluster = Cluster.objects.create(name="core-mysql-prod", department=dept)
    Instance.objects.create(
        name="core-01", cluster=cluster, host="10.0.0.1", port=3306, username="app"
    )
    return {"dept": dept, "cluster": cluster}


class TestSearchMode:
    def test_defaults_to_contains(self):
        assert current_mode() == MODE_CONTAINS

    @override_settings(SEARCH_MODE="prefix")
    def test_prefix_mode(self):
        assert current_mode() == MODE_PREFIX

    @override_settings(SEARCH_MODE="PREFIX")
    def test_mode_is_case_insensitive(self):
        assert current_mode() == MODE_PREFIX

    @override_settings(SEARCH_MODE="bogus")
    def test_invalid_mode_falls_back_to_contains(self):
        """配错值不能让搜索直接失效——退回包含匹配。"""
        assert current_mode() == MODE_CONTAINS

    @override_settings(SEARCH_MODE="")
    def test_empty_mode_falls_back(self):
        assert current_mode() == MODE_CONTAINS


class TestBuildSearchQ:
    def test_empty_keyword_matches_nothing(self, seeded):
        """空关键词必须匹配零条，而不是把整表返回给用户。"""
        from apps.assets.models import Instance

        q = build_search_q("   ", ["name", "host"])
        assert Instance.objects.filter(q).count() == 0

    def test_contains_matches_middle(self, seeded):
        from apps.assets.models import Instance

        q = build_search_q("re-0", ["name", "host"])
        assert Instance.objects.filter(q).count() == 1

    @override_settings(SEARCH_MODE="prefix")
    def test_prefix_does_not_match_middle(self, seeded):
        """prefix 模式只能匹配开头——这是它换取索引可用性的代价。"""
        from apps.assets.models import Instance

        assert Instance.objects.filter(build_search_q("re-0", ["name"])).count() == 0
        assert Instance.objects.filter(build_search_q("core", ["name"])).count() == 1

    def test_searches_multiple_fields(self, seeded):
        """两个字段是 OR：按 name 或 host 命中都算。"""
        from apps.assets.models import Instance

        by_name = Instance.objects.filter(build_search_q("core-01", ["name", "host"]))
        by_host = Instance.objects.filter(build_search_q("10.0.0.1", ["name", "host"]))
        assert by_name.count() == 1
        assert by_host.count() == 1
        assert by_name.first() == by_host.first()

    def test_empty_field_list_matches_nothing(self, seeded):
        from apps.assets.models import Instance

        assert Instance.objects.filter(build_search_q("core", [])).count() == 0


class TestProductionSecuritySettings:
    """生产安全开关的默认值必须"安全但可用"。"""

    def test_https_defaults_off_when_not_debug(self):
        """默认不重定向到 https：内网 http 部署不能一上来就打不开。"""
        from django.conf import settings

        # 测试跑在 DEBUG=False 下（settings 默认），因此这些分支已生效
        assert settings.DEBUG is False
        assert settings.SECURE_SSL_REDIRECT is False
        assert settings.SESSION_COOKIE_SECURE is False
        assert settings.CSRF_COOKIE_SECURE is False
        assert settings.SECURE_HSTS_SECONDS == 0

    def test_always_on_hardening(self):
        from django.conf import settings

        assert settings.SECURE_CONTENT_TYPE_NOSNIFF is True
        assert settings.SESSION_COOKIE_HTTPONLY is True
        assert settings.X_FRAME_OPTIONS == "DENY"
        assert settings.SECURE_REFERRER_POLICY == "same-origin"

    def test_deprecated_xss_filter_not_set(self):
        """SECURE_BROWSER_XSS_FILTER 在 Django 6 已移除，不能再出现。"""
        from django.conf import settings

        assert not hasattr(settings, "SECURE_BROWSER_XSS_FILTER")

    def test_csrf_trusted_origins_is_list(self):
        from django.conf import settings

        assert isinstance(settings.CSRF_TRUSTED_ORIGINS, list)
