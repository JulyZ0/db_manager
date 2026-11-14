"""角色组的建立与权限同步（挂在 post_migrate 信号上）。

为什么不在数据迁移里做
----------------------
Django 的 ``Permission`` 记录是 ``post_migrate`` 信号创建的，而数据迁移
在它**之前**执行。在迁移里查自定义权限会得到空集：组建好了、一个权限都
没绑，``migrate`` 输出全是 OK、不报任何错，最终表现为「管理员登录后什么
都点不动」。这个坑很难从日志里看出来，所以绑定必须放在这里。

为什么这里自己调用 create_permissions
-------------------------------------
``post_migrate`` 的接收者执行顺序取决于 ``INSTALLED_APPS`` 顺序与信号
连接时机，把「权限点已建好」当成前提是不可靠的——实测就出现过权限点
缺失、同步静默跳过的情况。``create_permissions`` 本身幂等（内部用
``get_or_create``），主动调一次让这个函数**不依赖任何外部时序**：
无论由信号触发、还是运维手工执行管理命令，结果都一样正确。
"""

from __future__ import annotations

import logging

from django.apps import apps as global_apps
from django.contrib.auth.management import create_permissions
from django.contrib.auth.models import Group, Permission

from common.permissions import (
    ROLE_ADMIN,
    ROLE_DEFINITIONS,
    ROLE_OPERATOR,
    ROLE_VIEWER,
)

logger = logging.getLogger(__name__)


def sync_role_groups(sender=None, verbosity: int = 1, **kwargs) -> None:
    """确保三个角色组存在，且权限与代码定义一致。

    只处理 ``ROLE_DEFINITIONS`` 里的组，不动运维自建的其他组。
    """
    # 先保证自定义权限点存在。重复调用安全：内部是 get_or_create。
    try:
        app_config = global_apps.get_app_config("assets")
    except LookupError:
        logger.debug("assets app 未加载，跳过角色同步")
        return
    create_permissions(app_config, verbosity=max(0, verbosity - 1))

    # 权限 codename 里带 ``assets.`` 前缀，去掉后才是 Permission.codename
    wanted: dict[str, list[str]] = {
        role: [perm.split(".", 1)[1] for perm in perms]
        for role, perms in ROLE_DEFINITIONS.items()
    }
    all_codenames = {code for codes in wanted.values() for code in codes}

    available = {
        perm.codename: perm
        for perm in Permission.objects.filter(
            content_type__app_label="assets", codename__in=all_codenames
        )
    }

    missing = all_codenames - set(available)
    if missing:
        # 走到这里说明模型里没声明对应权限点——是代码问题，要能看见。
        logger.warning(
            "角色权限点在 assets 模型上未声明，无法绑定：%s",
            ", ".join(sorted(missing)),
        )

    for role_name, codenames in wanted.items():
        group, created = Group.objects.get_or_create(name=role_name)
        target = [available[code] for code in codenames if code in available]
        if created or set(group.permissions.all()) != set(target):
            group.permissions.set(target)
            if verbosity >= 2:
                logger.info(
                    "角色组「%s」已同步：%s",
                    role_name,
                    "、".join(codenames) or "（无写权限）",
                )


__all__ = ["sync_role_groups", "ROLE_ADMIN", "ROLE_OPERATOR", "ROLE_VIEWER"]
