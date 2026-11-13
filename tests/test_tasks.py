"""周期任务包装层测试。

两个 task 是 Celery 包装，真正的算法在 services 里（待手写）。
这里验证的是包装层本身的行为：

1. 调度器能按 "apps.xxx.tasks.yyy" 路径解析到任务对象；
2. 核心算法还没实现时，任务**返回降级结果**而不是抛异常
   ——否则 autoretry 会空转三次，每次退避最长 5 分钟；
3. 真正的整体故障仍然向上抛，让 autoretry 生效。
"""

from __future__ import annotations

import pytest

from apps.credentials import tasks as credential_tasks
from apps.metrics import tasks as metric_tasks
from common.scheduler import InProcessScheduler

pytestmark = pytest.mark.django_db


BEAT_TASK_PATHS = [
    "apps.credentials.tasks.rotate_all_passwords",
    "apps.metrics.tasks.aggregate_daily_stats",
]


class TestSchedulerCanResolve:
    """进程内调度器靠字符串路径导入任务，路径写错就是静默失联。"""

    @pytest.mark.parametrize("task_path", BEAT_TASK_PATHS)
    def test_task_path_is_importable(self, task_path):
        scheduler = InProcessScheduler({})
        task = scheduler._resolve_task(task_path)
        assert task is not None, f"{task_path} 无法被解析"
        assert callable(task)

    def test_beat_schedule_paths_match_real_tasks(self, settings):
        """settings 里配置的任务路径必须都能解析，否则定时任务永远不会跑。"""
        scheduler = InProcessScheduler({})
        for entry in settings.CELERY_BEAT_SCHEDULE.values():
            task_path = entry["task"]
            assert scheduler._resolve_task(task_path) is not None, task_path


class TestGracefulDegradation:
    """核心算法未实现时应返回降级结果，不触发无限重试。"""

    def test_rotation_returns_not_implemented(self):
        result = credential_tasks.rotate_all_passwords()
        assert result["status"] == "not_implemented"

    def test_aggregation_returns_not_implemented(self):
        result = metric_tasks.aggregate_daily_stats()
        assert result["status"] == "not_implemented"

    def test_degradation_is_logged_as_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            credential_tasks.rotate_all_passwords()
        assert any("尚未实现" in record.message for record in caplog.records)

    def test_real_failure_still_raises(self, monkeypatch):
        """整体性故障不能被降级逻辑吞掉，否则 autoretry 永远不会触发。"""

        def boom(*args, **kwargs):
            raise RuntimeError("数据库挂了")

        monkeypatch.setattr("apps.credentials.services.rotate_all", boom)
        with pytest.raises(RuntimeError):
            credential_tasks.rotate_all_passwords()
