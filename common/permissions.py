"""危险操作权限分级。

为什么需要分级
--------------
在此之前，所有写操作只判断「有没有登录」。这意味着一个为了看台账而
临时开的账号，同样能删实例、能批量轮换全公司生产库的密码、能在控制台
里 ``DROP TABLE``。权限的粒度必须与「后果的不可逆程度」对齐。

三级划分
--------
- **只读**：不需要任何自定义权限。登录后即可浏览（未登录的浏览也照旧放行，
  这是既定口径：内网随手查看台账）。
- **操作员** ``operate_assets``：台账的日常维护——增删改部门/集群/实例，
  单实例的探测与密码轮换。都是"点对点"操作，影响面局限于一个对象。
- **管理员**：影响面是批量的或不可逆的，单独成权：
  - ``admin_console``：在目标库上执行任意 SQL（等于把 root 权限借出去）
  - ``admin_bulk_rotate``：批量轮换、修改轮换策略、立即执行轮换
  - ``admin_rebuild_stats``：重算统计（会覆盖历史快照）
  - ``admin_delete_cascade``：删除集群/部门（会连带影响其下全部实例）

为什么复用 Django 的 Group/Permission
-------------------------------------
``django.contrib.admin`` already ships a working UI for assigning users to
groups and groups to permissions。自建 Role 表意味着重造这套界面与继承
逻辑，而且每加一个权限点都要改模型。用内置机制，运维在 admin 后台点几下
就能把某人调成管理员，不需要开发介入。

关于 superuser
--------------
``User.has_perm()`` 对 superuser **恒返回 True**。所以安装时创建的
``admin`` 账号不受影响，现有部署升级后行为不变——这是刻意的：升级不该
把管理员锁在门外。
"""

from __future__ import annotations

from functools import wraps
from typing import Callable

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect

__all__ = [
    "PERM_OPERATE_ASSETS",
    "PERM_ADMIN_CONSOLE",
    "PERM_ADMIN_BULK_ROTATE",
    "PERM_ADMIN_REBUILD_STATS",
    "PERM_ADMIN_DELETE_CASCADE",
    "ROLE_VIEWER",
    "ROLE_OPERATOR",
    "ROLE_ADMIN",
    "ROLE_DEFINITIONS",
    "has_perm",
    "require_perm",
    "permission_required_page",
    "deny_page_if_no_perm",
]

# -- 权限点 ---------------------------------------------------------------
# 全部挂在 Instance 上（它是本系统的核心资源）。字符串格式必须是
# ``<app_label>.<codename>``，与 Model.Meta.permissions 里声明的一致。
PERM_OPERATE_ASSETS = "assets.operate_assets"
PERM_ADMIN_CONSOLE = "assets.admin_console"
PERM_ADMIN_BULK_ROTATE = "assets.admin_bulk_rotate"
PERM_ADMIN_REBUILD_STATS = "assets.admin_rebuild_stats"
PERM_ADMIN_DELETE_CASCADE = "assets.admin_delete_cascade"

# -- 角色（= Django Group）------------------------------------------------
ROLE_VIEWER = "只读"
ROLE_OPERATOR = "操作员"
ROLE_ADMIN = "管理员"


# 组名 → 权限集合。``None`` 表示不授予任何自定义权限（纯浏览）。
#
# 注意：只读组不给任何权限，但**登录用户本来就能浏览**（视图层不检查
# 权限）。这个组存在的意义是「显式声明此人不该有写权限」，便于审计时
# 一眼看出谁是什么角色，而不是靠"没在任何组里"来推断。
ROLE_DEFINITIONS: dict[str, tuple[str, ...]] = {
    ROLE_VIEWER: (),
    ROLE_OPERATOR: (PERM_OPERATE_ASSETS,),
    ROLE_ADMIN: (
        PERM_OPERATE_ASSETS,
        PERM_ADMIN_CONSOLE,
        PERM_ADMIN_BULK_ROTATE,
        PERM_ADMIN_REBUILD_STATS,
        PERM_ADMIN_DELETE_CASCADE,
    ),
}


def has_perm(user, perm: str) -> bool:
    """判定用户是否具备某个权限。

    未登录一律为 False：权限分级的前提是"先知道你是谁"。
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return bool(user.has_perm(perm))


def require_perm(user, perm: str, *, what: str = "") -> None:
    """API 层用：无权限时抛业务异常（403）。

    ``what`` 是给人看的操作描述，会拼进错误消息，让调用方知道
    自己被拦的是哪一类动作，而不是笼统的"权限不足"。
    """
    from common.exceptions import PermissionDeniedError

    if not has_perm(user, perm):
        action = what or "该操作"
        raise PermissionDeniedError(
            f"当前账号没有「{action}」权限，请联系管理员分配角色",
            detail={"required_permission": perm},
        )


def _deny_page(request: HttpRequest, what: str) -> HttpResponse:
    """已登录但无权限时的统一响应：提示 + 回首页。"""
    messages.warning(
        request,
        f"当前账号没有「{what or '该操作'}」权限。"
        "请联系管理员在后台把你的账号加入「操作员」或「管理员」组。",
    )
    # 不回跳来源页：来源页可能就是触发该操作的页面，回跳后用户
    # 大概率再点一次，形成来回弹提示的循环。回首页最稳。
    return redirect("web:dashboard")


def deny_page_if_no_perm(
    request: HttpRequest, perm: str, *, what: str = ""
) -> HttpResponse | None:
    """视图内部用：无权限返回重定向响应，有权限返回 None。

    用于「同一个视图里 GET 允许只读、POST 需要权限」的场景
    （例如轮换策略页：只读用户该能看到当前策略，但不能改）。
    这类视图无法用一个装饰器表达两种要求。
    """
    if has_perm(request.user, perm):
        return None
    return _deny_page(request, what)


def permission_required_page(
    perm: str, *, what: str = ""
) -> Callable[[Callable[..., HttpResponse]], Callable[..., HttpResponse]]:
    """页面层装饰器：已登录但无权限时给出友好提示，而不是裸 403。

    必须放在 ``@login_required_page`` **之内**（即写在其下方）：
    先确认"你是谁"，再判断"你能不能"。反过来会把未登录用户直接判成
    "权限不足"，误导他去找管理员要权限，而实际只需要登录。
    """

    def decorator(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
        @wraps(view)
        def _wrapped(request: HttpRequest, *args, **kwargs) -> HttpResponse:
            if has_perm(request.user, perm):
                return view(request, *args, **kwargs)
            return _deny_page(request, what)

        return _wrapped

    return decorator
