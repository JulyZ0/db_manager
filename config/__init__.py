"""保证 `celery -A config` 能找到应用实例。"""

from config.celery import app as celery_app

__all__ = ("celery_app",)
