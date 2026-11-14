"""告警规则与事件生命周期测试。

重点钉住三件容易出事的行为：
1. 持续时长未满**不能**触发（否则一次毛刺就半夜叫人）；
2. 已触发的事件重复命中只刷新、不重复计数（否则事件列表被刷爆）；
3. 悬挂告警要能自己收敛（否则故障早恢复了，告警还在响）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone


@pytest.fixture
def cluster(db):
    from apps.assets.models import Cluster, Department

    dept = Department.objects.create(name="告警部")
    return Cluster.objects.create(name="告警集群", department=dept, engine="mysql")


@pytest.fixture
def instance(cluster):
    from apps.assets.models import Instance

    return Instance.objects.create(
        name="alert-1", cluster=cluster, host="10.5.0.1", port=3306, username="mon"
    )


def make_rule(**kwargs):
    from apps.monitor.models import AlertRule

    defaults = {
        "name": "连接数过高",
        "metric": "mysql.connections",
        "operator": AlertRule.Operator.GT,
        "threshold": 100.0,
        "duration_minutes": 0,
    }
    defaults.update(kwargs)
    return AlertRule.objects.create(**defaults)


def sample(instance, value, minutes_ago=0, metric="mysql.connections"):
    from apps.monitor.models import MetricSample

    return MetricSample.objects.create(
        instance=instance,
        metric=metric,
        value=float(value),
        collected_at=timezone.now() - timedelta(minutes=minutes_ago),
    )


@pytest.mark.django_db
def test_matches_all_operators():
    from apps.monitor.models import AlertRule

    cases = [
        (AlertRule.Operator.GT, 10.0, 11.0, True),
        (AlertRule.Operator.GT, 10.0, 10.0, False),
        (AlertRule.Operator.LT, 10.0, 9.0, True),
        (AlertRule.Operator.LT, 10.0, 10.0, False),
        (AlertRule.Operator.GE, 10.0, 10.0, True),
        (AlertRule.Operator.LE, 10.0, 10.0, True),
        (AlertRule.Operator.LE, 10.0, 10.1, False),
    ]
    for operator, threshold, value, expected in cases:
        rule = make_rule(name=f"r-{operator}-{threshold}-{value}", operator=operator, threshold=threshold)
        assert rule.matches(value) is expected, (operator, threshold, value)


@pytest.mark.django_db
def test_fires_when_breached_without_duration(instance):
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule()
    sample(instance, 150)

    stats = evaluate_rule(rule)

    assert stats["fired"] == 1
    event = AlertEvent.objects.get()
    assert event.status == AlertEvent.Status.FIRING
    assert event.trigger_value == 150.0


@pytest.mark.django_db
def test_no_event_when_within_threshold(instance):
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule()
    sample(instance, 50)

    stats = evaluate_rule(rule)

    assert stats["fired"] == 0
    assert not AlertEvent.objects.exists()


@pytest.mark.django_db
def test_duration_not_met_does_not_fire(instance):
    """只采到一次越界、窗口覆盖不满 → 不触发。"""
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule(duration_minutes=5)
    sample(instance, 150)

    stats = evaluate_rule(rule)

    assert stats["fired"] == 0
    assert not AlertEvent.objects.exists()


@pytest.mark.django_db
def test_duration_met_fires(instance):
    """窗口内持续越界 → 触发。"""
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule(duration_minutes=5)
    for minutes in (5, 4, 3, 2, 1, 0):
        sample(instance, 150, minutes_ago=minutes)

    stats = evaluate_rule(rule)

    assert stats["fired"] == 1
    assert AlertEvent.objects.count() == 1


@pytest.mark.django_db
def test_dip_inside_window_prevents_fire(instance):
    """窗口中间回落过一次 → 视为抖动，重新计时，不触发。"""
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule(duration_minutes=5)
    sample(instance, 150, minutes_ago=5)
    sample(instance, 150, minutes_ago=4)
    sample(instance, 30, minutes_ago=3)  # 回落
    sample(instance, 150, minutes_ago=2)
    sample(instance, 150, minutes_ago=0)

    stats = evaluate_rule(rule)

    assert stats["fired"] == 0
    assert not AlertEvent.objects.exists()


@pytest.mark.django_db
def test_repeat_hit_updates_without_duplicating(instance):
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule()
    sample(instance, 150)
    evaluate_rule(rule)

    sample(instance, 200)
    stats = evaluate_rule(rule)

    assert stats["updated"] == 1
    assert stats["fired"] == 0
    assert AlertEvent.objects.count() == 1
    assert AlertEvent.objects.get().latest_value == 200.0


@pytest.mark.django_db
def test_recovers_when_back_to_normal(instance):
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule()
    sample(instance, 150)
    evaluate_rule(rule)

    sample(instance, 10)
    stats = evaluate_rule(rule)

    assert stats["resolved"] == 1
    event = AlertEvent.objects.get()
    assert event.status == AlertEvent.Status.RESOLVED
    assert event.resolved_at is not None


@pytest.mark.django_db
def test_refires_after_recovery(instance):
    """无冷却（cooldown=0）时，恢复后再次越界应开新事件。"""
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule(cooldown_minutes=0)
    sample(instance, 150)
    evaluate_rule(rule)
    sample(instance, 10)
    evaluate_rule(rule)

    sample(instance, 300)
    stats = evaluate_rule(rule)

    assert stats["fired"] == 1
    # 新事件，而不是复用已恢复的那条
    assert AlertEvent.objects.count() == 2


@pytest.mark.django_db
def test_cooldown_prevents_immediate_refire(instance):
    """冷却期内抖动不该反复开事件，否则运维会被刷屏到关掉告警。"""
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule(cooldown_minutes=10)
    sample(instance, 150)
    evaluate_rule(rule)
    sample(instance, 10)
    evaluate_rule(rule)

    # 刚恢复就再次越界：冷却未过，不应产生新事件
    sample(instance, 300)
    stats = evaluate_rule(rule)

    assert stats["fired"] == 0
    assert AlertEvent.objects.count() == 1


@pytest.mark.django_db
def test_cooldown_expires_and_allows_refire(instance):
    """冷却期过后恢复正常告警能力，不能永久闭麦。"""
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule(cooldown_minutes=10)
    sample(instance, 150)
    evaluate_rule(rule)
    sample(instance, 10)
    evaluate_rule(rule)

    from datetime import timedelta

    from django.utils import timezone

    sample(instance, 300)
    later = timezone.now() + timedelta(minutes=11)
    stats = evaluate_rule(rule, now=later)

    assert stats["fired"] == 1
    assert AlertEvent.objects.count() == 2


@pytest.mark.django_db
def test_inactive_instance_ignored(instance):
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule()
    sample(instance, 150)
    instance.is_active = False
    instance.save(update_fields=["is_active"])

    assert evaluate_rule(rule)["fired"] == 0
    assert not AlertEvent.objects.exists()


@pytest.mark.django_db
def test_cluster_scope_limits_evaluation(cluster, instance):
    from apps.assets.models import Cluster, Department, Instance

    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    other = Cluster.objects.create(
        name="别的集群", department=Department.objects.create(name="别的部"), engine="mysql"
    )
    outsider = Instance.objects.create(
        name="out-1", cluster=other, host="10.5.9.9", port=3306, username="mon"
    )
    rule = make_rule(cluster=cluster)
    sample(instance, 150)
    sample(outsider, 150)

    evaluate_rule(rule)

    assert AlertEvent.objects.count() == 1
    assert AlertEvent.objects.get().instance_id == instance.pk


@pytest.mark.django_db
def test_missing_metric_skipped(cluster, instance):
    """没有该指标采样时跳过，不能当成'不越界'去恢复已有事件。"""
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule(metric="mysql.not_collected")
    sample(instance, 150)  # 采的是别的指标

    assert evaluate_rule(rule)["fired"] == 0
    assert not AlertEvent.objects.exists()


@pytest.mark.django_db
def test_stale_event_auto_resolved(instance):
    """实例停了采集，firing 事件不能永远挂着。"""
    from apps.monitor.alerting import resolve_stale_events
    from apps.monitor.models import AlertEvent

    rule = make_rule()
    old = timezone.now() - timedelta(hours=2)
    event = AlertEvent.objects.create(
        rule=rule,
        rule_name=rule.name,
        instance=instance,
        instance_label="alert-1",
        metric=rule.metric,
        threshold=100.0,
        status=AlertEvent.Status.FIRING,
        started_at=old,
        last_seen_at=old,
    )

    count = resolve_stale_events(grace_minutes=15)

    assert count == 1
    event.refresh_from_db()
    assert event.status == AlertEvent.Status.RESOLVED
    assert "采集已停止" in event.message


@pytest.mark.django_db
def test_fresh_event_not_auto_resolved(instance):
    from apps.monitor.alerting import resolve_stale_events
    from apps.monitor.models import AlertEvent

    rule = make_rule()
    AlertEvent.objects.create(
        rule=rule,
        rule_name=rule.name,
        instance=instance,
        instance_label="alert-1",
        metric=rule.metric,
        threshold=100.0,
        status=AlertEvent.Status.FIRING,
        started_at=timezone.now(),
        last_seen_at=timezone.now(),
    )

    assert resolve_stale_events(grace_minutes=15) == 0
    assert AlertEvent.objects.get().status == AlertEvent.Status.FIRING


@pytest.mark.django_db
def test_event_survives_rule_deletion(instance):
    """删规则不能抹掉历史事件，否则无法追溯。"""
    from apps.monitor.alerting import evaluate_rule
    from apps.monitor.models import AlertEvent

    rule = make_rule()
    sample(instance, 150)
    evaluate_rule(rule)

    rule.delete()

    event = AlertEvent.objects.get()
    assert event.rule is None
    assert event.rule_name == "连接数过高"


@pytest.mark.django_db
def test_disabled_rule_not_evaluated(instance):
    from apps.monitor.alerting import evaluate_all
    from apps.monitor.models import AlertEvent

    make_rule(enabled=False)
    sample(instance, 150)

    total = evaluate_all()

    assert total["rules"] == 0
    assert not AlertEvent.objects.exists()


@pytest.mark.django_db
def test_evaluate_all_aggregates(instance):
    from apps.monitor.alerting import evaluate_all
    from apps.monitor.models import AlertEvent

    make_rule(name="规则A")
    make_rule(name="规则B", metric="mysql.threads_running", threshold=5.0)
    sample(instance, 150)
    from apps.monitor.models import MetricSample

    MetricSample.objects.create(
        instance=instance,
        metric="mysql.threads_running",
        value=9.0,
        collected_at=timezone.now(),
    )

    total = evaluate_all()

    assert total["rules"] == 2
    assert total["fired"] == 2
    assert AlertEvent.objects.count() == 2
