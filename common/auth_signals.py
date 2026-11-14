"""登录成功/失败的记账信号。

与 ``common/auth_backends.py`` 分工：
- 后端负责**登录前**检查是否已锁定（拦下请求）；
- 这里负责**登录后**记账：失败次数 +1，成功则清除。

为什么用信号而不是改登录视图
----------------------------
``user_login_failed`` 由 Django 在**任何**一次认证失败后发出，无论失败
发生在 admin 登录页、自定义登录页还是 API 的登录接口。挂在信号上，
新增入口不会漏掉记账——这与"把检查放认证后端"是同一个思路的两半。

安全约束：日志里**绝不能**出现密码。信号参数里的 ``credentials`` 含明文
密码，这里只取用户名，且只写脱敏后的账号名。
"""

from __future__ import annotations

import logging

from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver

from common.request import client_ip
from common.throttle import login_throttle

logger = logging.getLogger(__name__)

__all__ = ["record_login_failure", "clear_login_failures"]


def _account_from(credentials: dict | None) -> str:
    """从信号参数里取账号名，绝不触碰密码字段。"""
    if not credentials:
        return ""
    # admin 用的是 username；自定义表单可能用 email
    for field in ("username", "email"):
        value = credentials.get(field)
        if value:
            return str(value)[:150]
    return ""


@receiver(user_login_failed)
def record_login_failure(sender, credentials=None, request=None, **kwargs) -> None:
    """每次认证失败记一次。"""
    # 拿不到 request 就没有来源 IP：此时按用户名维度也记不住（键里含 IP），
    # 无法归因。宁可漏记也不要用空 IP 建键——那会把所有无 request 的失败
    # 归到同一个桶里，互相牵连。
    if request is None:
        return

    account = _account_from(credentials)
    ip = client_ip(request)
    login_throttle.record_failure(ip, account)


@receiver(user_logged_in)
def clear_login_failures(sender, request, user, **kwargs) -> None:
    """登录成功后清空该 (IP, 用户名) 的失败计数。

    不清 IP 维度：IP 计数拦的是"同一来源扫多个账号"，
    一次成功登录不该给该 IP 的其它失败尝试洗白。
    """
    ip = client_ip(request) if request is not None else ""
    login_throttle.reset(ip, user.get_username() if user is not None else "")
