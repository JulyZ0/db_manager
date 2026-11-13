"""请求元信息提取。

目前只有一件事：可靠地拿到"这次请求是谁发起的、从哪来"。

关于 X-Forwarded-For
--------------------
不能默认采信：任何人手工加一个请求头就能伪造来源 IP，把审计日志变成
假证据。默认只用 REMOTE_ADDR —— 本项目是内网单机部署，waitress 直连
客户端，REMOTE_ADDR 就是真实地址。

只有当进程确实跑在反向代理（nginx / 公司网关）之后时，才通过
``TRUST_PROXY_HEADERS=True`` 打开；打开后取 XFF 的**最后一个**条目，
因为前面的条目是客户端自己可以随意填写的。
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest

__all__ = ["client_ip", "actor_label"]


def client_ip(request: HttpRequest) -> str:
    """返回发起请求的 IP，拿不到时返回空串。"""
    if getattr(settings, "TRUST_PROXY_HEADERS", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR") or ""
        if forwarded:
            # 取最后一段：代理会把自己的下游地址追加在末尾，
            # 前面的段是客户端可控的，不可信。
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                return parts[-1][:64]

    return (request.META.get("REMOTE_ADDR") or "")[:64]


def actor_label(request: HttpRequest) -> str:
    """返回可追溯的操作人标识。

    已登录用用户名；未登录不要写成空字符串——"匿名"本身也是审计结论，
    显式标出来才能和"日志缺字段"区分开。
    """
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return str(getattr(user, "username", "") or user.pk)[:64]
    return "anonymous"
