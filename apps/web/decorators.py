"""页面层鉴权装饰器。

口径与 API 保持一致：
- 浏览类 GET（列表、详情、统计页）**免登录**，方便内网随手查看；
- 写操作（新建 / 修改 / 删除 / 探测 / 轮换 / 统计重算）**必须登录**。

为什么不用 django.contrib.auth.decorators.login_required
--------------------------------------------------------
``login_required`` 直接 302 到登录页。但本项目的写操作都是 POST-only，
浏览器跟随 302 会变成 GET，登录回来后落到 ``405 Method Not Allowed``，
小白用户会看到一个纯报错页。

这里改成：未登录时给出明确提示，并带 ``next`` 回到**用户刚才所在的页面**
（取自 Referer），登录后原地继续点按钮即可。取不到 Referer 时不带 next。
"""

from __future__ import annotations

from functools import wraps
from typing import Callable

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

__all__ = ["login_required_page"]


def _safe_next(request: HttpRequest) -> str | None:
    """从 Referer 里取回跳地址，只接受本站路径，避免开放重定向。"""
    referer = request.META.get("HTTP_REFERER")
    if not referer:
        return None
    # 允许本站域名；url_has_allowed_host_and_scheme 会校验 host
    if not url_has_allowed_host_and_scheme(
        referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return None
    from urllib.parse import urlsplit

    parts = urlsplit(referer)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return path


def login_required_page(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    """写操作守卫：未登录则提示并跳转登录，登录后回到原页面。"""

    @wraps(view)
    def _wrapped(request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if request.user.is_authenticated:
            return view(request, *args, **kwargs)

        messages.warning(
            request,
            "该操作需要登录后执行。请使用管理员账号登录，登录后返回此页面重试。",
        )
        login_url = reverse("admin:login")
        next_path = _safe_next(request)
        if next_path:
            from urllib.parse import urlencode

            login_url = f"{login_url}?{urlencode({'next': next_path})}"
        return redirect(login_url)

    return _wrapped
