"""monitor 应用配置。"""

from django.apps import AppConfig


class MonitorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.monitor"
    verbose_name = "监控与告警"
