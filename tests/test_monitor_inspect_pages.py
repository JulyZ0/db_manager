"""巡检评分与诊断报告页面测试。

这三个页面（总览 / 单机报告 / 集群报告）都是只读聚合，不连目标库，
因此不需要 mock。重点断言"排序、扣分项置顶、数据不足降级"。
"""

from __future__ import annotations

import pytest
from django.utils import timezone


@pytest.fixture
def cluster(db):
    from apps.assets.models import Cluster, Department

    dept = Department.objects.create(name="巡检部")
    return Cluster.objects.create(name="巡检集群", department=dept, engine="mysql")


@pytest.fixture
def instance(cluster):
    from apps.assets.models import Instance

    return Instance.objects.create(
        name="insp-1", cluster=cluster, host="10.10.0.1", port=3306, username="mon"
    )


def seed_healthy(instance):
    """写入一套健康的指标 + 一次成功采集。"""
    from apps.monitor.models import CollectionRun, MetricSample

    now = timezone.now()
    for metric, value in (
        ("mysql.connection_usage_percent", 25.0),
        ("mysql.buffer_pool_hit_percent", 99.8),
        ("mysql.slow_queries_per_sec", 0.0),
        ("mysql.replication_delay_seconds", 0.0),
    ):
        MetricSample.objects.create(
            instance=instance, metric=metric, value=value, collected_at=now
        )
    CollectionRun.objects.create(
        instance=instance, collected_at=now, ok=True, metrics_count=4
    )


@pytest.mark.django_db
def test_overview_lists_instances(client, instance):
    seed_healthy(instance)

    resp = client.get("/monitor/inspect/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "巡检评分" in body
    assert "insp-1" in body
    assert "巡检集群" in body


@pytest.mark.django_db
def test_overview_orders_worst_first(client, cluster):
    """按分数升序：最该修的排最前面。"""
    from apps.assets.models import Instance
    from apps.monitor.models import MetricSample

    now = timezone.now()
    healthy = Instance.objects.create(
        name="aaa-healthy", cluster=cluster, host="10.10.0.2", port=3306, username="mon"
    )
    bad = Instance.objects.create(
        name="zzz-bad", cluster=cluster, host="10.10.0.3", port=3306, username="mon"
    )
    seed_healthy(healthy)
    # 连接打满 + 命中率极低 + 复制延迟
    for metric, value in (
        ("mysql.connection_usage_percent", 95.0),
        ("mysql.buffer_pool_hit_percent", 50.0),
        ("mysql.slow_queries_per_sec", 20.0),
        ("mysql.replication_delay_seconds", 120.0),
    ):
        MetricSample.objects.create(
            instance=bad, metric=metric, value=value, collected_at=now
        )

    body = client.get("/monitor/inspect/").content.decode()

    # 尽管名字排序 aaa 在前，差的实例必须排在健康实例之前
    assert body.index("zzz-bad") < body.index("aaa-healthy")


@pytest.mark.django_db
def test_overview_empty_state(client, db):
    body = client.get("/monitor/inspect/").content.decode()
    assert "还没有启用的实例" in body


@pytest.mark.django_db
def test_report_renders_score_and_items(client, instance):
    seed_healthy(instance)

    resp = client.get(f"/instances/{instance.pk}/monitor/report/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "诊断报告" in body
    assert "健康分" in body
    assert "采集状态" in body
    assert "缓存命中率" in body


@pytest.mark.django_db
def test_report_puts_problems_first(client, instance):
    """扣分项单独置顶，且给出的原因要能被搜索到。"""
    from apps.monitor.models import CollectionRun, MetricSample

    now = timezone.now()
    MetricSample.objects.create(
        instance=instance,
        metric="mysql.connection_usage_percent",
        value=95.0,
        collected_at=now,
    )
    CollectionRun.objects.create(
        instance=instance,
        collected_at=now,
        ok=False,
        error_code="auth_failed",
        message="账号或密码不正确",
    )

    body = client.get(f"/instances/{instance.pk}/monitor/report/").content.decode()

    assert "需要处理" in body
    assert "auth_failed" in body
    # 失败项区块应排在校验通过区块之前
    assert body.index("需要处理") < body.index("通过项")


@pytest.mark.django_db
def test_report_marks_insufficient_data(client, instance):
    """指标全缺失时必须明说"分数不代表健康度"。"""
    body = client.get(f"/instances/{instance.pk}/monitor/report/").content.decode()

    assert "数据不足" in body
    assert "不代表健康度" in body
    assert "未参与评分" in body


@pytest.mark.django_db
def test_report_404_for_missing_instance(client):
    assert client.get("/instances/999999/monitor/report/").status_code == 404


@pytest.mark.django_db
def test_cluster_report_aggregates(client, cluster, instance):
    seed_healthy(instance)

    resp = client.get(f"/clusters/{cluster.pk}/monitor/report/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "集群诊断报告" in body
    assert "平均健康分" in body
    assert "insp-1" in body


@pytest.mark.django_db
def test_cluster_report_flags_worst(client, cluster, instance):
    from apps.assets.models import Instance
    from apps.monitor.models import MetricSample

    now = timezone.now()
    seed_healthy(instance)
    bad = Instance.objects.create(
        name="worst-one", cluster=cluster, host="10.10.0.9", port=3306, username="mon"
    )
    for metric, value in (
        ("mysql.connection_usage_percent", 99.0),
        ("mysql.buffer_pool_hit_percent", 40.0),
    ):
        MetricSample.objects.create(
            instance=bad, metric=metric, value=value, collected_at=now
        )

    body = client.get(f"/clusters/{cluster.pk}/monitor/report/").content.decode()

    assert "最需要处理" in body
    assert "worst-one" in body


@pytest.mark.django_db
def test_cluster_report_empty(client, cluster):
    body = client.get(f"/clusters/{cluster.pk}/monitor/report/").content.decode()
    assert "该集群没有实例" in body


@pytest.mark.django_db
def test_cluster_report_404(client):
    assert client.get("/clusters/999999/monitor/report/").status_code == 404


@pytest.mark.django_db
def test_report_reads_alerts(client, instance):
    """有严重告警时报告要把它算进去，而不是只看指标。"""
    from apps.monitor.models import AlertEvent

    seed_healthy(instance)
    AlertEvent.objects.create(
        rule=None,
        rule_name="连接数过高",
        instance=instance,
        instance_label="insp-1",
        metric="mysql.connections",
        threshold=100.0,
        severity="critical",
        status=AlertEvent.Status.FIRING,
        started_at=timezone.now(),
        last_seen_at=timezone.now(),
    )

    body = client.get(f"/instances/{instance.pk}/monitor/report/").content.decode()

    assert "严重告警" in body


@pytest.mark.django_db
def test_report_reads_lock_wait(client, instance):
    """有长锁等待时报告要扣分。"""
    from apps.monitor.models import LockWait

    seed_healthy(instance)
    LockWait.objects.create(
        instance=instance,
        collected_at=timezone.now(),
        waiting_id="thread:1",
        blocking_id="thread:2",
        wait_seconds=200.0,
    )

    body = client.get(f"/instances/{instance.pk}/monitor/report/").content.decode()

    assert "最长等待 200 秒" in body
