"""Celery 应用装配。

配置统一从 Django settings 读取（namespace="CELERY"），
任务模块由 autodiscover_tasks 在各 app 的 tasks.py 中自动发现。
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("db_manager")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
