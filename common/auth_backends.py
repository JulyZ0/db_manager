"""带登录限流的认证后端。

为什么改认证后端而不是改登录视图
--------------------------------
登录入口不止一个：Django admin 的 ``/admin/login/``、未来可能加的
自定义登录页、以及任何调用 ``authenticate()`` 的代码。如果只在某个视图里
做限流，新加入口会自动绕过——这类"漏加防护"的漏洞很难在测试里发现。

认证后端是所有登录路径的**必经之路**，把检查放在这里，覆盖是结构性的。

两件事分工：

- **本文件**：登录**前**检查是否已锁定。锁定时直接返回 None，
  不再去查数据库校验密码（顺带也消除了被锁期间的时序侧信道）。
- **信号**（见 ``apps/runtime/apps.py``）：登录**后**记账——
  ``user_login_failed`` 记失败，``user_logged_in`` 清除计数。
"""

from __future__ import annotations

import logging

from django.contrib.auth.backends import ModelBackend

from common.request import client_ip
from common.throttle import login_throttle

logger = logging.getLogger(__name__)

__all__ = ["ThrottledModelBackend"]


class ThrottledModelBackend(ModelBackend):
    """在标准 ModelBackend 之上加失败次数限流。"""

    def authenticate(self, request, username=None, password=None, **kwargs):
        # 拿不到 request 时（例如测试或脚本直接调用 authenticate()）跳过限流：
        # 此时没有来源 IP，按 IP 限流无从下手，按用户名限流又会误伤
        # 同一进程里的其它调用方。
        if request is not None:
            ip = client_ip(request)
            account = username or kwargs.get("email") or ""
            if login_throttle.is_locked(ip, account):
                # 不区分"账号不存在/密码错误/已锁定"：对外统一是认证失败，
                # 既避免泄露账号是否存在，也避免攻击者据此判断何时解封。
                logger.warning(
                    "登录被限流拦截 ip=%s username=%s",
                    ip or "<unknown>",
                    str(account)[:64] or "<empty>",
                )
                return None

        return super().authenticate(request, username, password, **kwargs)
