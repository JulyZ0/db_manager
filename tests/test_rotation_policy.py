"""轮换策略的时间解析与范围折算。

这里只测纯逻辑，不触发任何数据库写入或真实轮换：
- 两种调度口径（固定间隔 / 每日定点）折算成绝对时刻是否正确；
- 到点判定在"刚执行过""从未执行""跨天"等边界上的行为；
- 范围折算（全部 / 集群 / 实例 / 并集）是否与页面上的勾选一致。
"""

from __future__ import annotations

from datetime import datetime, timedelta, time

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.assets.models import Cluster, Department, Instance
from apps.credentials.models import (
    RotationPolicy,
    RotationScheduleMode,
    format_daily_times,
    parse_daily_times,
)

pytestmark = pytest.mark.django_db


def _at(year, month, day, hour, minute=0):
    """构造一个当前时区的绝对时刻，避免测试依赖运行机器的时区。"""
    naive = datetime(year, month, day, hour, minute)
    return timezone.make_aware(naive, timezone.get_current_timezone())


# ---------------------------------------------------------------------------
# 时刻解析
# ---------------------------------------------------------------------------


class TestParseDailyTimes:
    def test_parses_single_and_multiple(self):
        assert parse_daily_times("00:00") == [time(0, 0)]
        assert parse_daily_times("00:00,12:00") == [time(0, 0), time(12, 0)]

    def test_sorts_and_dedupes(self):
        assert parse_daily_times("12:00,00:00,12:00") == [time(0, 0), time(12, 0)]

    def test_ignores_blank_segments(self):
        assert parse_daily_times(" 00:00 , , 12:00 ") == [time(0, 0), time(12, 0)]

    @pytest.mark.parametrize("bad", ["0000", "24:00", "00:60", "ab:cd", "12", ""])
    def test_rejects_bad_format(self, bad):
        with pytest.raises(ValidationError):
            parse_daily_times(bad)

    def test_format_round_trips(self):
        raw = "00:00,12:30"
        assert format_daily_times(parse_daily_times(raw)) == raw


# ---------------------------------------------------------------------------
# 下次执行时间
# ---------------------------------------------------------------------------


class TestNextRunAt:
    def test_disabled_policy_has_no_next_run(self):
        policy = RotationPolicy.load()
        policy.enabled = False
        assert policy.next_run_at(now=_at(2026, 11, 13, 10)) is None

    def test_interval_never_run_starts_from_next_hour(self):
        """从未跑过时，从下一个整点起算，避免一保存策略就立刻全量改密。"""
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 12
        policy.last_run_at = None

        assert policy.next_run_at(now=_at(2026, 11, 13, 10, 37)) == _at(2026, 11, 13, 11)

    def test_interval_uses_last_run_plus_hours(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 6
        policy.last_run_at = _at(2026, 11, 13, 8)

        assert policy.next_run_at(now=_at(2026, 11, 13, 9)) == _at(2026, 11, 13, 14)

    def test_daily_picks_next_slot_today(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.DAILY
        policy.daily_times = "00:00,12:00"

        assert policy.next_run_at(now=_at(2026, 11, 13, 10)) == _at(2026, 11, 13, 12)

    def test_daily_rolls_over_to_tomorrow(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.DAILY
        policy.daily_times = "00:00,12:00"

        assert policy.next_run_at(now=_at(2026, 11, 13, 13)) == _at(2026, 11, 14, 0)

    def test_daily_ignores_past_slots(self):
        """13:00 时，今天的 00:00 与 12:00 都已过，必须等到明天 00:00。"""
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.DAILY
        policy.daily_times = "00:00,12:00"

        next_run = policy.next_run_at(now=_at(2026, 11, 13, 13))
        assert next_run.date() == _at(2026, 11, 14, 0).date()


# ---------------------------------------------------------------------------
# 到点判定
# ---------------------------------------------------------------------------


class TestIsDue:
    def test_disabled_never_due(self):
        policy = RotationPolicy.load()
        policy.enabled = False
        policy.last_run_at = _at(2020, 1, 1, 0)
        assert policy.is_due(now=_at(2026, 11, 13, 10)) is False

    def test_interval_never_run_is_not_due(self):
        """刚部署不该立刻触发全量改密。"""
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 12
        policy.last_run_at = None
        assert policy.is_due(now=_at(2026, 11, 13, 10)) is False

    def test_interval_due_after_elapsed(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 12
        policy.last_run_at = _at(2026, 11, 13, 0)
        assert policy.is_due(now=_at(2026, 11, 13, 11, 59)) is False
        assert policy.is_due(now=_at(2026, 11, 13, 12, 1)) is True

    def test_interval_supports_non_cron_periods(self):
        """每 5 小时这种周期 cron 表达不了，固定间隔口径必须支持。"""
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 5
        policy.last_run_at = _at(2026, 11, 13, 0)
        assert policy.is_due(now=_at(2026, 11, 13, 4, 59)) is False
        assert policy.is_due(now=_at(2026, 11, 13, 5, 1)) is True

    def test_daily_never_run_is_due_right_after_slot(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.DAILY
        policy.daily_times = "00:00,12:00"
        policy.last_run_at = None
        assert policy.is_due(now=_at(2026, 11, 13, 12, 1)) is True

    def test_daily_not_due_before_first_slot(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.DAILY
        policy.daily_times = "00:00,12:00"
        policy.last_run_at = _at(2026, 11, 12, 12, 0)
        # 11-13 的 00:00 已经过去但没跑过，应当到点
        assert policy.is_due(now=_at(2026, 11, 13, 0, 5)) is True

    def test_daily_not_due_twice_in_same_slot(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.DAILY
        policy.daily_times = "00:00,12:00"
        policy.last_run_at = _at(2026, 11, 13, 12, 0)
        assert policy.is_due(now=_at(2026, 11, 13, 12, 30)) is False
        assert policy.is_due(now=_at(2026, 11, 14, 0, 1)) is True


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


class TestPolicyValidation:
    def test_interval_out_of_range_rejected(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.INTERVAL
        policy.interval_hours = 0
        with pytest.raises(ValidationError):
            policy.full_clean()

    def test_interval_too_long_rejected(self):
        policy = RotationPolicy.load()
        policy.interval_hours = 24 * 31
        with pytest.raises(ValidationError):
            policy.full_clean()

    def test_daily_times_invalid_rejected(self):
        policy = RotationPolicy.load()
        policy.schedule_mode = RotationScheduleMode.DAILY
        policy.daily_times = "25:00"
        with pytest.raises(ValidationError):
            policy.full_clean()

    def test_password_length_outside_choices_rejected(self):
        policy = RotationPolicy.load()
        policy.password_length = 8
        with pytest.raises(ValidationError):
            policy.full_clean()

    def test_save_is_always_singleton(self):
        """即使有人把 pk 改成别的值，落库时也会被拉回单例主键。"""
        first = RotationPolicy.load()
        rogue = RotationPolicy.load()
        rogue.pk = 99
        rogue.interval_hours = 3
        rogue.save()

        assert RotationPolicy.objects.count() == 1
        assert RotationPolicy.objects.first().pk == RotationPolicy.SINGLETON_PK
        assert first.pk == RotationPolicy.SINGLETON_PK
        assert RotationPolicy.load().interval_hours == 3

    def test_load_is_idempotent(self):
        """重复调用不会造出第二条策略。"""
        first = RotationPolicy.load()
        second = RotationPolicy.load()
        assert first.pk == second.pk
        assert RotationPolicy.objects.count() == 1


# ---------------------------------------------------------------------------
# 范围折算
# ---------------------------------------------------------------------------


@pytest.fixture
def two_clusters(db):
    dept = Department.objects.create(name="基础架构部")
    c1 = Cluster.objects.create(name="c1", department=dept)
    c2 = Cluster.objects.create(name="c2", department=dept)
    return dept, c1, c2


def _instance(cluster, name, host, *, active=True):
    return Instance.objects.create(
        name=name,
        cluster=cluster,
        host=host,
        port=3306,
        username="app",
        is_active=active,
    )


class TestScopeResolution:
    def test_empty_scope_means_all_active(self, two_clusters):
        _, c1, c2 = two_clusters
        a = _instance(c1, "a", "10.0.0.1")
        b = _instance(c2, "b", "10.0.0.2")
        _instance(c2, "stopped", "10.0.0.3", active=False)

        policy = RotationPolicy.load()
        assert sorted(policy.resolve_instance_ids()) == sorted([a.id, b.id])

    def test_cluster_scope_covers_children(self, two_clusters):
        _, c1, c2 = two_clusters
        a = _instance(c1, "a", "10.0.0.1")
        b = _instance(c1, "b", "10.0.0.2")
        _instance(c2, "c", "10.0.0.3")

        policy = RotationPolicy.load()
        policy.clusters.set([c1])
        assert sorted(policy.resolve_instance_ids()) == sorted([a.id, b.id])

    def test_instance_scope_is_explicit(self, two_clusters):
        _, c1, c2 = two_clusters
        a = _instance(c1, "a", "10.0.0.1")
        b = _instance(c1, "b", "10.0.0.2")
        c = _instance(c2, "c", "10.0.0.3")

        policy = RotationPolicy.load()
        policy.instances.set([c])
        assert policy.resolve_instance_ids() == [c.id]

    def test_cluster_and_instance_scope_union(self, two_clusters):
        _, c1, c2 = two_clusters
        a = _instance(c1, "a", "10.0.0.1")
        b = _instance(c1, "b", "10.0.0.2")
        c = _instance(c2, "c", "10.0.0.3")

        policy = RotationPolicy.load()
        policy.clusters.set([c1])
        policy.instances.set([c])
        assert sorted(policy.resolve_instance_ids()) == sorted([a.id, b.id, c.id])

    def test_inactive_instances_never_included(self, two_clusters):
        _, c1, _ = two_clusters
        a = _instance(c1, "a", "10.0.0.1")
        stopped = _instance(c1, "stopped", "10.0.0.2", active=False)

        policy = RotationPolicy.load()
        policy.clusters.set([c1])
        ids = policy.resolve_instance_ids()
        assert a.id in ids
        assert stopped.id not in ids

    def test_explicit_inactive_instance_is_excluded(self, two_clusters):
        """停用实例即使被手动勾选也不轮换——停用语义优先。"""
        _, c1, _ = two_clusters
        stopped = _instance(c1, "stopped", "10.0.0.1", active=False)

        policy = RotationPolicy.load()
        policy.instances.set([stopped])
        assert policy.resolve_instance_ids() == []

    def test_scope_label_reflects_selection(self, two_clusters):
        _, c1, _ = two_clusters
        a = _instance(c1, "a", "10.0.0.1")

        policy = RotationPolicy.load()
        assert policy.scope_label() == "全部启用实例"

        policy.clusters.set([c1])
        assert policy.scope_label() == "1 个集群"

        policy.instances.set([a])
        assert policy.scope_label() == "1 个集群 + 1 个实例"
