"""assets 应用配置。"""

from django.apps import AppConfig


class AssetsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.assets"
    verbose_name = "资产台账"

    def ready(self) -> None:
        # 把三个角色组的权限绑定挂到 post_migrate 上。
        #
        # 必须在 post_migrate，不能写在数据迁移里：``Permission`` 记录是
        # Django 的 post_migrate 信号创建的，而数据迁移在它**之前**运行。
        # 在迁移里查 Permission 会得到空集，组是建好了、但一个权限都没绑，
        # 表现为「管理员登录后什么都不能做」，且 migrate 输出全是 OK，
        # 不报任何错。
        from django.db.models.signals import post_migrate

        from apps.assets.roles import sync_role_groups

        post_migrate.connect(
            sync_role_groups,
            sender=self,
            dispatch_uid="assets.sync_role_groups",
        )
