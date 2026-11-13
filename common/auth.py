"""统一认证入口。

口径（与页面层一致）：
- **读操作保持免登录**：内网工具，浏览台账不需要账号。
- **写操作必须登录**：新建 / 修改 / 删除 / 探测 / 轮换 / 统计重算。
  这些都改变数据，必须能追溯到人。

API 复用 Django 会话（SessionAuth），不引入第二套用户体系：
浏览器已登录即可直接调；脚本需要先登录拿 sessionid + csrftoken。

关于 CSRF（重要）
-----------------
SessionAuth 继承自 APIKeyCookie，构造参数默认 ``csrf=True``，会在读
session cookie 之前先做一次 CSRF 校验。这条不能关：Cookie 认证若不校验
CSRF，攻击者只要让已登录用户的浏览器发起一个跨站 POST，就能借用户的
会话改数据——等于把漏洞从页面挪到了 API。django-ninja 的视图在 Django
中间件层是 csrf_exempt 的，所以这里的校验是唯一防线。

测试里 ``Client()`` 默认 ``enforce_csrf_checks=False``，会给请求打上
``_dont_enforce_csrf_checks`` 标记，SessionAuth 的 CSRF 检查因此放行，
测试无需手工搬 csrf token；真实浏览器请求则会正常校验。
"""

from __future__ import annotations

from ninja.security import SessionAuth

__all__ = ["django_session_auth"]


# 单例：SessionAuth 无状态，重复构造只会让 OpenAPI 里出现多条同名安全方案
django_session_auth = SessionAuth()
