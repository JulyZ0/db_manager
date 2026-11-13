"""调度判定层（apps.credentials.schedule）的行为。

关注点：
- 两种口径都能描述成人话，供页面直接展示；
- ``run_if_due`` 在"未启用 / 未到点 / 范围内无实例 / 真的到点"四种情况下的行为；
- 到点执行后 ``last_run_at`` 必须推进，否则调度器每个 tick 都会重复触发。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from django.utils import timezone

from apps.assets.models import Cluster, Department, Instance
from apps.credentials import schedule
from apps.credentials.models import RotationPolicy, RotationScheduleMode

pytestmark = pytest.mark.django_db


def _at(year, month, day, hour, minute=0):
    naive = datetime(year, month, day, hour, minute)
    return timezone.make_aware(naive, timezone.get_current_timezone())


@pytest.fixture
def dept_cluster_instance(db):
    dept = Department.objects.create(name="基础架构部")
    cluster = Cluster.objects.create(name="c1", department=dept)
    instance = Instance.objects.create(
        name="i1", cluster=cluster, host="10.0.0.1", port=3306, username="app"
    )
    return dept, cluster, instance


# ---------------------------------------------------------------------------
# 描述
# ---------------------------------------------------------------------------


class TestDescribe:
    def test_disabled_policy(self):
        policy = RotationPolicy.load()
        policy.enabled = False
        assert schedule.describe_schedule(policy) == "已停用"

    def test_interval_description(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 12
        assert schedule.describe_schedule(policy) == "每 12 小时"

    def test_daily_description(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.DAILY
        policy.daily_times = "00:00,12:00"
        assert schedule.describe_schedule(policy) == "每天 00:00、12:00"

    def test_next_run_description_is_human_readable(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.DAILY
        policy.daily_times = "12:00"

        text = schedule.describe_next_run(policy, now=_at(2026, 11, 13, 10))
        assert "2026-11-13 12:00" in text
        assert "2 小时后" in text

    def test_next_run_description_for_disabled(self):
        policy = RotationPolicy.load()
        policy.enabled = False
        assert "不会执行" in schedule.describe_next_run(policy)


# ---------------------------------------------------------------------------
# run_if_due 的四种分支
# ---------------------------------------------------------------------------


class TestRunIfDue:
    def test_disabled_does_nothing(self, dept_cluster_instance):
        _, _, instance = dept_cluster_instance
        policy = RotationPolicy.load()
        policy.enabled = False
        policy.last_run_at = _at(2020, 1, 1, 0)
        policy.save()

        result = schedule.run_if_due(now=_at(2026, 11, 13, 10))
        assert result == {"ran": False, "reason": "策略已停用"}
        instance.refresh_from_db()
        assert instance.password_version == 0

    def test_not_due_does_nothing(self, dept_cluster_instance):
        _, _, instance = dept_cluster_instance
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 12
        policy.last_run_at = _at(2026, 11, 13, 8)
        policy.save()

        result = schedule.run_if_due(now=_at(2026, 11, 13, 9))
        assert result["ran"] is False
        instance.refresh_from_db()
        assert instance.password_version == 0

    def test_empty_scope_advances_last_run(self, db):
        """范围内没有实例时也要推进 last_run_at，否则每个 tick 都会判为到点。"""
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 1
        policy.last_run_at = _at(2026, 11, 13, 0)
        policy.save()

        now = _at(2026, 11, 13, 2)
        result = schedule.run_if_due(now=now)

        assert result["ran"] is False
        assert result["reason"] == "范围内无启用实例"
        policy.refresh_from_db()
        assert policy.last_run_at == now

    def test_due_runs_rotation_and_stamps_policy(
        self, dept_cluster_instance, monkeypatch
    ):
        _, _, instance = dept_cluster_instance
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 1
        policy.last_run_at = _at(2026, 11, 13, 0)
        policy.password_length = 48
        policy.save()

        captured: dict[str, object] = {}

        def fake_rotate_all(**kwargs):
            captured.update(kwargs)
            return {"success": 1, "failed": 0, "total": 1}

        monkeypatch.setattr(
            "apps.credentials.services.rotate_all", fake_rotate_all, raising=True
        )

        result = schedule.run_if_due(now=_at(2026, 11, 13, 2))

        assert result["ran"] is True
        assert result["success"] == 1
        # 策略里的强度与范围必须真正传给轮换服务
        assert captured["password_length"] == 48
        assert captured["instance_ids"] == [instance.id]

        policy.refresh_from_db()
        assert policy.last_run_at == _at(2026, 11, 13, 2)
        assert "成功 1" in policy.last_run_summary
        assert instance.password_version == 0, "打桩后不应真的改库"

    def test_failure_is_recorded_and_reraised(self, dept_cluster_instance, monkeypatch):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 1
        policy.last_run_at = _at(2026, 11, 13, 0)
        policy.save()

        def boom(**kwargs):
            raise RuntimeError("数据库挂了")

        monkeypatch.setattr(
            "apps.credentials.services.rotate_all", boom, raising=True
        )

        with pytest.raises(RuntimeError):
            schedule.run_if_due(now=_at(2026, 11, 13, 2))

        policy.refresh_from_db()
        # 失败也要推进时间，否则调度器会疯狂重试
        assert policy.last_run_at == _at(2026, 11, 13, 2)
        assert "失败" in policy.last_run_summary


# ---------------------------------------------------------------------------
# 补跑窗口
# ---------------------------------------------------------------------------


class TestCatchUpWindow:
    def test_daily_window_is_six_hours(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.DAILY
        assert schedule.catch_up_window(policy).total_seconds() == 6 * 3600

    def test_interval_window_capped_at_a_day(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 1
        assert schedule.catch_up_window(policy).total_seconds() == 3600

        policy.interval_hours = 30
        assert schedule.catch_up_window(policy).total_seconds() == 24 * 3600
