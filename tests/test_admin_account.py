"""管理命令 ensure_admin 的测试。

覆盖三类行为：首次创建、重复执行幂等（不覆盖运维改过的密码）、显式重置密码。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command

from apps.assets.management.commands.ensure_admin import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
)

pytestmark = pytest.mark.django_db


def _admin():
    return get_user_model().objects.get(username=DEFAULT_USERNAME)


def test_creates_superuser_with_working_password():
    assert get_user_model().objects.count() == 0

    call_command("ensure_admin", verbosity=0)

    user = _admin()
    assert user.is_staff is True
    assert user.is_superuser is True
    # 密码必须是可用的哈希，且能用默认密码登录
    assert user.has_usable_password() is True
    assert user.password != DEFAULT_PASSWORD, "密码不能以明文存储"
    assert authenticate(username=DEFAULT_USERNAME, password=DEFAULT_PASSWORD) is not None


def test_rerun_is_idempotent_and_creates_no_duplicate():
    call_command("ensure_admin", verbosity=0)
    call_command("ensure_admin", verbosity=0)

    assert get_user_model().objects.filter(username=DEFAULT_USERNAME).count() == 1


def test_rerun_does_not_overwrite_changed_password():
    """运维改过密码后，再次启动不能被冲回默认密码。"""
    call_command("ensure_admin", verbosity=0)

    user = _admin()
    user.set_password("ChangedByOps@999")
    user.save()

    call_command("ensure_admin", verbosity=0)

    assert authenticate(username=DEFAULT_USERNAME, password="ChangedByOps@999") is not None
    assert authenticate(username=DEFAULT_USERNAME, password=DEFAULT_PASSWORD) is None


def test_reset_password_flag_forces_new_password():
    call_command("ensure_admin", verbosity=0)

    user = _admin()
    user.set_password("ChangedByOps@999")
    user.save()

    call_command("ensure_admin", reset_password=True, verbosity=0)

    assert authenticate(username=DEFAULT_USERNAME, password=DEFAULT_PASSWORD) is not None


def test_rerun_restores_missing_privileges():
    """账号存在但被误降权时，下次启动应补回权限。"""
    call_command("ensure_admin", verbosity=0)

    user = _admin()
    user.is_staff = False
    user.is_superuser = False
    user.save()

    call_command("ensure_admin", verbosity=0)

    user.refresh_from_db()
    assert user.is_staff is True
    assert user.is_superuser is True


def test_custom_username_and_password():
    call_command("ensure_admin", username="ops", password="Ops@2026Pass", verbosity=0)

    user = get_user_model().objects.get(username="ops")
    assert user.is_superuser is True
    assert authenticate(username="ops", password="Ops@2026Pass") is not None
    assert not get_user_model().objects.filter(username=DEFAULT_USERNAME).exists()


def test_admin_can_log_into_django_admin(client):
    """/admin/ 的登录链路要真的能走通，而不只是建了个用户。"""
    call_command("ensure_admin", verbosity=0)

    resp = client.post(
        "/admin/login/",
        {"username": DEFAULT_USERNAME, "password": DEFAULT_PASSWORD},
        follow=False,
    )
    # 登录成功后 Django 会 302 跳转到后台首页
    assert resp.status_code == 302
    assert "/admin/" in resp["Location"]
