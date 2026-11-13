"""metrics 应用配置。"""

from django.apps import AppConfig


class MetricsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.metrics"
    verbose_name = "统计与观测"
