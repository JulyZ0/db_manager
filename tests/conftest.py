"""pytest 公共夹具。

鉴权口径（见 common/auth.py 与 apps/web/views.py）：
- 浏览类 GET 免登录，内网工具不强制账号；
- 写操作（新建/修改/删除/探测/轮换/统计重算）必须登录。

因此这里提供两个客户端：
- ``api_client``：已登录的运维账号，绝大多数业务测试用它；
- ``anon_client``：匿名，只用于验证"哪些入口应当被拦、哪些应当放行"。
"""

from __future__ import annotations

import pytest

ADMIN_USERNAME = "tester"
ADMIN_PASSWORD = "Tester@123456"


@pytest.fixture
def api_client(db):
    """已登录的测试客户端（走完整中间件链路）。"""
    from django.contrib.auth import get_user_model
    from django.test import Client

    user, created = get_user_model().objects.get_or_create(
        username=ADMIN_USERNAME,
        defaults={"is_staff": True, "is_superuser": True},
    )
    if created:
        user.set_password(ADMIN_PASSWORD)
        user.save()

    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def login_user(db):
    """返回 api_client 背后的那个用户，便于断言归属。"""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.get(username=ADMIN_USERNAME)


@pytest.fixture
def anon_client():
    """未登录客户端，用于验证鉴权边界。"""
    from django.test import Client

    return Client()
