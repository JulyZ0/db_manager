"""周期任务包装层测试。

两个 task 是 Celery 包装，真正的算法在 services 里。
这里验证的是包装层本身的行为：

1. 调度器能按 "apps.xxx.tasks.yyy" 路径解析到任务对象；
2. 任务能真实执行并返回统计结果；
3. 真正的整体故障仍然向上抛，让 autoretry 生效。
"""

from __future__ import annotations

import pytest

from apps.credentials import tasks as credential_tasks
from apps.metrics import tasks as metric_tasks
from common.scheduler import InProcessScheduler

pytestmark = pytest.mark.django_db


BEAT_TASK_PATHS = [
    "apps.credentials.tasks.rotate_passwords_by_policy",
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


class TestTaskExecution:
    """算法落地后，任务包装层应真实执行并返回统计结果。"""

    def test_rotation_task_returns_stats(self):
        result = credential_tasks.rotate_all_passwords()
        assert result["total"] == 0
        assert result["success"] == 0
        assert result["failed"] == 0
        assert "duration_ms" in result

    def test_aggregation_task_returns_row_count(self):
        result = metric_tasks.aggregate_daily_stats()
        assert result["rows"] == 0
        assert "duration_ms" in result

    def test_rotation_task_processes_instances(self, db):
        from apps.assets.models import Cluster, Department, Instance
        from apps.credentials.crypto import encrypt_secret

        dept = Department.objects.create(name="任务部")
        cluster = Cluster.objects.create(name="task-cluster", department=dept)
        Instance.objects.create(
            name="t1",
            cluster=cluster,
            host="10.1.1.1",
            port=3306,
            username="u",
            # 真实改密要先用旧密码登录目标库
            password_ciphertext=encrypt_secret("Old@Passw0rd01"),
        )

        result = credential_tasks.rotate_all_passwords()
        assert result["success"] == 1
        assert result["failed"] == 0

    def test_real_failure_still_raises(self, monkeypatch):
        """整体性故障不能被降级逻辑吞掉，否则 autoretry 永远不会触发。"""

        def boom(*args, **kwargs):
            raise RuntimeError("数据库挂了")

        monkeypatch.setattr("apps.credentials.services.rotate_all", boom)
        with pytest.raises(RuntimeError):
            credential_tasks.rotate_all_passwords()


class TestPolicyDrivenTask:
    """定时任务每分钟被戳一次，真正跑不跑由策略决定。"""

    def test_returns_without_work_when_not_due(self, db):
        """未到点时应立刻返回，且不能改动任何实例。"""
        from apps.assets.models import Cluster, Department, Instance

        dept = Department.objects.create(name="策略部")
        cluster = Cluster.objects.create(name="policy-cluster", department=dept)
        instance = Instance.objects.create(
            name="p1", cluster=cluster, host="10.5.5.1", port=3306, username="u"
        )

        result = credential_tasks.rotate_passwords_by_policy()

        assert result["ran"] is False
        assert "duration_ms" in result
        instance.refresh_from_db()
        assert instance.password_version == 0, "未到点却改了密码，属于严重缺陷"

    def test_runs_when_policy_is_due(self, db, monkeypatch):
        """策略到点时任务必须真的发起轮换，并把策略的范围与强度传下去。"""
        from datetime import timedelta

        from django.utils import timezone

        from apps.assets.models import Cluster, Department, Instance
        from apps.credentials.models import RotationPolicy, RotationScheduleMode

        dept = Department.objects.create(name="策略部")
        cluster = Cluster.objects.create(name="due-cluster", department=dept)
        instance = Instance.objects.create(
            name="p2", cluster=cluster, host="10.5.5.2", port=3306, username="u"
        )

        policy = RotationPolicy.load()
        policy.enabled = True
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 1
        policy.password_length = 24
        # 两小时前跑过，间隔 1 小时 → 现在必然到点
        policy.last_run_at = timezone.now() - timedelta(hours=2)
        policy.save()

        captured: dict[str, object] = {}

        def fake_rotate_all(**kwargs):
            captured.update(kwargs)
            return {"success": 1, "failed": 0, "total": 1}

        monkeypatch.setattr("apps.credentials.services.rotate_all", fake_rotate_all)

        result = credential_tasks.rotate_passwords_by_policy()

        assert result["ran"] is True
        assert result["success"] == 1
        assert captured["instance_ids"] == [instance.id]
        assert captured["password_length"] == 24

        policy.refresh_from_db()
        assert policy.last_run_at is not None
        assert "成功 1" in policy.last_run_summary
        # 打桩后不应真的改库，确认测的是接线而不是副作用
        instance.refresh_from_db()
        assert instance.password_version == 0

    def test_beat_checks_every_minute(self, settings):
        """Beat 侧必须是「每分钟检查」，否则策略里的自定义时间根本不生效。"""
        entry = settings.CELERY_BEAT_SCHEDULE["rotate-passwords-check"]
        assert entry["task"] == "apps.credentials.tasks.rotate_passwords_by_policy"

        minute = entry["schedule"].minute
        # crontab(minute="*") 解析成 None 或全集，两者都表示「每分钟」
        assert minute is None or set(minute) == set(range(60))

    def test_inprocess_scheduler_fires_every_minute(self, settings):
        """进程内调度器走同一份 beat 配置，也必须每分钟命中。"""
        from datetime import datetime

        from common.scheduler import InProcessScheduler, crontab_matches

        entry = settings.CELERY_BEAT_SCHEDULE["rotate-passwords-check"]
        schedule = entry["schedule"]

        for minute in (0, 1, 30, 59):
            moment = datetime(2026, 11, 13, 10, minute)
            assert crontab_matches(schedule, moment), minute

        # 同一分钟不重复触发。用不存在的任务路径做探针，
        # 避免真在后台线程里跑一次轮换、污染测试库。
        scheduler = InProcessScheduler(
            {
                "probe": {
                    "task": "apps.credentials.tasks.__missing_probe__",
                    "schedule": schedule,
                }
            }
        )
        fired = scheduler.tick(datetime(2026, 11, 13, 10, 1))
        assert fired == ["probe"]
        assert scheduler.tick(datetime(2026, 11, 13, 10, 1)) == []
