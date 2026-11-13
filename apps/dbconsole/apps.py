"""目标库连接控制台。"""

from django.apps import AppConfig


class DbConsoleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.dbconsole"
    verbose_name = "连接控制台"
