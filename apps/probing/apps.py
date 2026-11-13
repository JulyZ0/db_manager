"""probing 应用配置。"""

from django.apps import AppConfig


class ProbingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.probing"
    verbose_name = "连通性探测"
