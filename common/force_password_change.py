"""强制修改默认口令的守卫中间件。

为什么需要中间件而不是只改登录视图
----------------------------------
登录入口不止一个：``/admin/login/`` 是 Django 自带页面，本系统没有自己的
登录视图。若只在自建入口加跳转，运维从后台登录仍然能带着默认口令一路
操作下去。挂在中间件上，**所有页面路径**统一覆盖，新增入口也不会漏。

为什么放行 API 与静态资源
-------------------------
- API：调用方是脚本/自动化，没有交互界面可以改密，拦住只会让既有集成
  突然 401，而风险（默认口令可登录网页后台）并未降低。
- 静态资源与 healthz：它们不承载任何权限，拦住会让改密页自己都缺样式。
- 改密页与登出：否则会陷入"要改密才能进改密页"的死循环。

为什么在 POST 之后仍然拦截
--------------------------
中间件拿不到"这个响应是否需要登录"的语义，简单按路径判断最不容易出错。
改密页自身在白名单里，所以正常流程不会被打断。
"""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

logger = logging.getLogger(__name__)

# 免于强制改密的路径前缀：改密流程自身、登出、静态资源、探活、接口层。
# 用前缀而非精确匹配：后台的改密页是 ``/admin/password_change/``，
# 且 Django admin 的登录/登出都在 ``/admin/`` 下——但 admin 的其它页面
# 必须拦住，所以不能整段放行，只能逐条列出。
EXEMPT_PREFIXES = (
    "/static/",
    "/media/",
    "/healthz",
    "/api/",
    "/account/password/",
    "/admin/login/",
    "/admin/logout/",
    "/admin/password_change/",
)


class ForcePasswordChangeMiddleware:
    """默认口令未修改时，把页面请求导向改密页。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)

        # 匿名用户交给各自的鉴权逻辑处理，这里不插手
        if user is None or not user.is_authenticated:
            return self.get_response(request)

        if request.path.startswith(EXEMPT_PREFIXES):
            return self.get_response(request)

        from apps.web.models import is_using_default_password

        if not is_using_default_password(user):
            return self.get_response(request)

        logger.warning(
            "账号 %s 仍在使用默认口令，已重定向到改密页 path=%s",
            user.get_username(),
            request.path,
        )
        return redirect(reverse("web:password_change"))
