"""默认口令强制改密的测试。

为什么值得单独测
----------------
这段逻辑有两种安静的失效方式，都很难在人工点测时发现：

- **拦不住**：登记哈希的时机或比较条件写错，中间件永远放行——表现
  和"没写这段代码"完全一样，只有默认口令被人拿去登录时才暴露。
- **拦太死**：改完密码仍被反复导回改密页，或 API / 静态资源也被拦。
  运维会认为"系统坏了"，而真正的原因（哈希比较没生效）看不出来。

因此这里既验证"该拦的拦"，也验证"改完就放行"。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.assets.management.commands.ensure_admin import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
)
from apps.web.models import is_using_default_password, mark_default_password

pytestmark = pytest.mark.django_db


def _admin():
    return get_user_model().objects.get(username=DEFAULT_USERNAME)


# ---------------------------------------------------------------------------
# 登记与判断
# ---------------------------------------------------------------------------


def test_ensure_admin_marks_default_password():
    call_command("ensure_admin", verbosity=0)

    assert is_using_default_password(_admin()) is True


def test_custom_password_is_not_marked():
    """运维显式指定自己的口令时不该被拦——那已经不是公开信息。"""
    call_command("ensure_admin", password="OpsOwn@2026Pass", verbosity=0)

    assert is_using_default_password(_admin()) is False


def test_changing_password_clears_the_flag():
    """任何途径改密都应自动解除，不依赖某条代码路径去清标记。"""
    call_command("ensure_admin", verbosity=0)
    user = _admin()
    assert is_using_default_password(user) is True

    user.set_password("BrandNew@2026Pass")
    user.save()

    assert is_using_default_password(_admin()) is False


def test_reset_password_re_marks_with_default():
    """显式重置回默认口令时，应重新进入待改密状态。"""
    call_command("ensure_admin", verbosity=0)
    user = _admin()
    user.set_password("BrandNew@2026Pass")
    user.save()
    assert is_using_default_password(_admin()) is False

    call_command("ensure_admin", reset_password=True, verbosity=0)

    assert is_using_default_password(_admin()) is True


def test_unmarked_user_is_never_blocked():
    user = get_user_model().objects.create_user(
        username="plain", password="Whatever@2026Pass"
    )

    assert is_using_default_password(user) is False


def test_mark_is_idempotent():
    call_command("ensure_admin", verbosity=0)
    user = _admin()

    mark_default_password(user)
    mark_default_password(user)

    assert is_using_default_password(user) is True


# ---------------------------------------------------------------------------
# 中间件行为
# ---------------------------------------------------------------------------


@pytest.fixture
def default_password_client(db):
    """已登录、且仍在用默认口令的客户端。"""
    from django.test import Client

    call_command("ensure_admin", verbosity=0)
    client = Client()
    client.force_login(_admin())
    return client


def test_page_redirects_to_password_change(default_password_client):
    response = default_password_client.get("/")

    assert response.status_code == 302
    assert response["Location"] == "/account/password/"


def test_password_change_page_is_reachable(default_password_client):
    """否则会陷入"要改密才能进改密页"的死循环。"""
    response = default_password_client.get("/account/password/")

    assert response.status_code == 200
    assert "修改密码" in response.content.decode()


def test_api_paths_are_not_blocked(default_password_client):
    """API 调用方是脚本，没有界面可以改密；拦住只会让集成突然失败。"""
    response = default_password_client.get("/api/v1/openapi.json")

    assert response.status_code == 200


def test_static_paths_are_not_blocked(default_password_client):
    response = default_password_client.get("/static/css/app.css")

    assert response.status_code == 200


def test_after_changing_password_pages_work(default_password_client):
    response = default_password_client.post(
        "/account/password/",
        {
            "old_password": DEFAULT_PASSWORD,
            "new_password1": "BrandNew@2026Pass",
            "new_password2": "BrandNew@2026Pass",
        },
    )
    assert response.status_code == 302

    # 改完之后不再被导回改密页
    response = default_password_client.get("/")
    assert response.status_code == 200


def test_change_keeps_user_logged_in(default_password_client):
    """改密后必须仍在登录态，否则用户会以为改密失败了。"""
    default_password_client.post(
        "/account/password/",
        {
            "old_password": DEFAULT_PASSWORD,
            "new_password1": "BrandNew@2026Pass",
            "new_password2": "BrandNew@2026Pass",
        },
    )

    assert "_auth_user_id" in default_password_client.session
    response = default_password_client.get("/")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# 表单校验
# ---------------------------------------------------------------------------


def test_wrong_old_password_is_rejected(default_password_client):
    response = default_password_client.post(
        "/account/password/",
        {
            "old_password": "not-my-password",
            "new_password1": "BrandNew@2026Pass",
            "new_password2": "BrandNew@2026Pass",
        },
    )

    assert response.status_code == 200
    assert "当前密码不正确" in response.content.decode()
    assert is_using_default_password(_admin()) is True


def test_mismatched_confirmation_is_rejected(default_password_client):
    response = default_password_client.post(
        "/account/password/",
        {
            "old_password": DEFAULT_PASSWORD,
            "new_password1": "BrandNew@2026Pass",
            "new_password2": "OtherOne@2026Pass",
        },
    )

    assert response.status_code == 200
    assert "不一致" in response.content.decode()


def test_weak_password_is_rejected(default_password_client):
    """AUTH_PASSWORD_VALIDATORS 默认是空列表，必须真的生效。"""
    response = default_password_client.post(
        "/account/password/",
        {
            "old_password": DEFAULT_PASSWORD,
            "new_password1": "12345678",
            "new_password2": "12345678",
        },
    )

    assert response.status_code == 200
    # 仍是默认口令，说明弱口令没被接受
    assert is_using_default_password(_admin()) is True


def test_same_as_old_password_is_rejected(default_password_client):
    response = default_password_client.post(
        "/account/password/",
        {
            "old_password": DEFAULT_PASSWORD,
            "new_password1": DEFAULT_PASSWORD,
            "new_password2": DEFAULT_PASSWORD,
        },
    )

    assert response.status_code == 200
    assert "不能与当前密码相同" in response.content.decode()
