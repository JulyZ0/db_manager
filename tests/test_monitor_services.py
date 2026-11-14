"""采集编排层测试：速率换算、落库、失败记录、清理。

采集器本身已被各自测试覆盖，这里只测「编排」——
包括最容易被忽略的一点：累计计数器要在下一次采集时才算得出速率，
且间隔不合理时必须跳过而不是造出一个假尖峰。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from apps.monitor.collectors.base import CollectResult, Sample


@pytest.fixture
def instance(db):
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="编排测试部")
    cluster = Cluster.objects.create(
        name="编排测试集群", department=dept, engine="mysql"
    )
    return Instance.objects.create(
        name="mon-1", cluster=cluster, host="10.0.0.9", port=3306, username="mon"
    )


def patch_collector(monkeypatch, result):
    from apps.monitor import services

    monkeypatch.setattr(services, "collect_for", lambda inst, pw, timeout: result)


def patch_password(monkeypatch, password="pw", error=None):
    import apps.dbconsole.services as dbconsole

    monkeypatch.setattr(
        dbconsole, "resolve_password", lambda inst: (password, error)
    )


@pytest.mark.django_db
def test_success_writes_samples_and_run(instance, monkeypatch):
    from apps.monitor import services

    patch_password(monkeypatch)
    patch_collector(
        monkeypatch,
        CollectResult(ok=True, samples=[Sample("mysql.connections", 12.0)]),
    )

    run = services.collect_instance(instance)

    assert run.ok
    assert run.metrics_count == 1
    samples = list(instance.metric_samples.all())
    assert len(samples) == 1
    assert samples[0].metric == "mysql.connections"
    assert samples[0].value == 12.0


@pytest.mark.django_db
def test_no_password_records_failure_without_connecting(instance, monkeypatch):
    from apps.monitor import services

    patch_password(monkeypatch, password=None, error="no_password")

    called = []
    monkeypatch.setattr(
        services, "collect_for", lambda *a: called.append(1) or CollectResult(ok=True)
    )

    run = services.collect_instance(instance)

    assert not run.ok
    assert run.error_code == "no_password"
    # 没密码就不该去连目标库
    assert called == []


@pytest.mark.django_db
def test_rate_computed_from_previous_counter(instance, monkeypatch):
    """累计计数器要靠两次采样求差：60 次 / 60 秒 = 1 QPS。"""
    from django.utils import timezone

    from apps.monitor import services
    from apps.monitor.models import MetricSample

    now = timezone.now()
    MetricSample.objects.create(
        instance=instance,
        metric="mysql.queries_total",
        value=100.0,
        collected_at=now - timedelta(minutes=1),
    )

    patch_password(monkeypatch)
    patch_collector(
        monkeypatch,
        CollectResult(ok=True, samples=[Sample("mysql.queries_total", 160.0)]),
    )

    services.collect_instance(instance, now=now)

    rates = {
        s.metric: s.value
        for s in MetricSample.objects.filter(instance=instance, collected_at=now)
        if s.metric == "mysql.qps"
    }
    assert rates["mysql.qps"] == pytest.approx(1.0)


@pytest.mark.django_db
def test_rate_skipped_when_interval_too_long(instance, monkeypatch):
    """间隔过长时算出来是长窗口平均值，标成 QPS 会误导，必须跳过。"""
    from django.utils import timezone

    from apps.monitor import services
    from apps.monitor.models import MetricSample

    now = timezone.now()
    MetricSample.objects.create(
        instance=instance,
        metric="mysql.queries_total",
        value=100.0,
        collected_at=now - timedelta(hours=2),
    )

    patch_password(monkeypatch)
    patch_collector(
        monkeypatch,
        CollectResult(ok=True, samples=[Sample("mysql.queries_total", 100000.0)]),
    )

    services.collect_instance(instance, now=now)

    assert not MetricSample.objects.filter(
        instance=instance, metric="mysql.qps"
    ).exists()


@pytest.mark.django_db
def test_counter_reset_skips_rate(instance, monkeypatch):
    """重启后计数器归零（差值为负）时不产出速率，不做加回最大值的猜测。"""
    from django.utils import timezone

    from apps.monitor import services
    from apps.monitor.models import MetricSample

    now = timezone.now()
    MetricSample.objects.create(
        instance=instance,
        metric="pg.xact_commit_total",
        value=5000.0,
        collected_at=now - timedelta(minutes=1),
    )

    patch_password(monkeypatch)
    patch_collector(
        monkeypatch,
        CollectResult(ok=True, samples=[Sample("pg.xact_commit_total", 10.0)]),
    )

    services.collect_instance(instance, now=now)

    assert not MetricSample.objects.filter(
        instance=instance, metric="pg.tps"
    ).exists()


@pytest.mark.django_db
def test_failed_collect_writes_no_samples(instance, monkeypatch):
    from apps.monitor import services

    patch_password(monkeypatch)
    patch_collector(
        monkeypatch, CollectResult.failed("query_failed", "权限不足")
    )

    run = services.collect_instance(instance)

    assert not run.ok
    assert run.error_code == "query_failed"
    assert run.metrics_count == 0
    assert not instance.metric_samples.exists()


@pytest.mark.django_db
def test_collect_all_isolates_failures(instance, monkeypatch):
    """一个实例采集抛异常，不影响另一个实例。"""
    from apps.monitor import services
    from apps.assets.models import Instance

    second = Instance.objects.create(
        name="mon-2",
        cluster=instance.cluster,
        host="10.0.0.10",
        port=3306,
        username="mon",
    )

    patch_password(monkeypatch)

    def fake_collect(inst, pw, timeout):
        if inst.pk == second.pk:
            raise RuntimeError("boom")
        return CollectResult(ok=True, samples=[Sample("mysql.connections", 1.0)])

    monkeypatch.setattr(services, "collect_for", fake_collect)

    result = services.collect_all()

    assert result["total"] == 2
    assert result["ok"] == 1
    assert result["failed"] == 1


@pytest.mark.django_db
def test_collect_all_skips_when_disabled(instance, monkeypatch, settings):
    from apps.monitor import services

    settings.MONITOR_COLLECT_ENABLED = False
    result = services.collect_all()

    assert result["skipped"] is True


@pytest.mark.django_db
def test_only_active_instances_collected(instance, monkeypatch):
    from apps.monitor import services

    instance.is_active = False
    instance.save(update_fields=["is_active"])

    patch_password(monkeypatch)
    patch_collector(monkeypatch, CollectResult(ok=True))

    assert services.collect_all()["total"] == 0


@pytest.mark.django_db
def test_latest_run_map_picks_newest(instance):
    from django.utils import timezone

    from apps.monitor import services
    from apps.monitor.models import CollectionRun

    now = timezone.now()
    CollectionRun.objects.create(instance=instance, collected_at=now - timedelta(minutes=5), ok=False)
    newest = CollectionRun.objects.create(instance=instance, collected_at=now, ok=True)

    latest = services.latest_run_map([instance.pk])

    assert latest[instance.pk].pk == newest.pk


@pytest.mark.django_db
def test_purge_expired_deletes_old_only(instance, settings):
    from django.utils import timezone

    from apps.monitor import services
    from apps.monitor.models import CollectionRun, MetricSample

    settings.MONITOR_RETENTION_DAYS = 30
    now = timezone.now()

    old = MetricSample.objects.create(
        instance=instance,
        metric="m.old",
        value=1.0,
        collected_at=now - timedelta(days=40),
    )
    fresh = MetricSample.objects.create(
        instance=instance,
        metric="m.new",
        value=1.0,
        collected_at=now,
    )
    CollectionRun.objects.create(
        instance=instance, collected_at=now - timedelta(days=40), ok=True
    )

    stats = services.purge_expired()

    assert stats["metrics"] == 1
    assert stats["runs"] == 1
    assert not MetricSample.objects.filter(pk=old.pk).exists()
    assert MetricSample.objects.filter(pk=fresh.pk).exists()


@pytest.mark.django_db
def test_purge_dry_run_keeps_rows(instance, settings):
    from django.utils import timezone

    from apps.monitor import services
    from apps.monitor.models import MetricSample

    settings.MONITOR_RETENTION_DAYS = 30
    MetricSample.objects.create(
        instance=instance,
        metric="m.old",
        value=1.0,
        collected_at=timezone.now() - timedelta(days=40),
    )

    stats = services.purge_expired(dry_run=True)

    assert stats["metrics"] == 1
    assert MetricSample.objects.count() == 1


@pytest.mark.django_db
def test_purge_metrics_disabled_when_retention_zero(instance, settings):
    """指标保留期为 0 = 永久保留指标，但 SQL 指纹仍按自己的保留期清理。"""
    from django.utils import timezone

    from apps.monitor import services
    from apps.monitor.models import MetricSample

    settings.MONITOR_RETENTION_DAYS = 0
    settings.MONITOR_SQL_RETENTION_DAYS = 90
    MetricSample.objects.create(
        instance=instance,
        metric="m.old",
        value=1.0,
        collected_at=timezone.now() - timedelta(days=4000),
    )

    stats = services.purge_expired()

    assert stats["metrics"] == 0
    assert MetricSample.objects.count() == 1
    # 两套保留期只有一套关掉时，整体不算跳过
    assert stats["skipped"] is False


@pytest.mark.django_db
def test_purge_skipped_only_when_both_retentions_zero(instance, settings):
    from django.utils import timezone

    from apps.monitor import services
    from apps.monitor.models import MetricSample

    settings.MONITOR_RETENTION_DAYS = 0
    settings.MONITOR_SQL_RETENTION_DAYS = 0
    MetricSample.objects.create(
        instance=instance,
        metric="m.old",
        value=1.0,
        collected_at=timezone.now() - timedelta(days=4000),
    )

    stats = services.purge_expired()

    assert stats["skipped"] is True
    assert MetricSample.objects.count() == 1


@pytest.mark.django_db
def test_purge_covers_sql_digest_and_deadlock(instance, settings):
    """SQL 指纹按 last_seen_at、死锁按 detected_at，用独立的更长保留期。"""
    from django.utils import timezone

    from apps.monitor import services
    from apps.monitor.models import DeadlockEvent, SqlDigest
    from apps.monitor.sqltext import digest_of

    settings.MONITOR_RETENTION_DAYS = 30
    settings.MONITOR_SQL_RETENTION_DAYS = 90
    now = timezone.now()

    stale = SqlDigest.objects.create(
        instance=instance,
        engine="mysql",
        digest=digest_of("SELECT a FROM t1"),
        template="SELECT a FROM t1",
        first_seen_at=now - timedelta(days=200),
        last_seen_at=now - timedelta(days=200),
    )
    # 80 天前见过：未超过 90 天，应保留
    # 注意必须用结构不同的语句：SQL 1 与 SQL 2 归一化后都是 "SELECT ?"，
    # 会算出同一个 digest 并撞上唯一约束（这本身证明归一化生效）
    fresh = SqlDigest.objects.create(
        instance=instance,
        engine="mysql",
        digest=digest_of("SELECT b FROM t2"),
        template="SELECT b FROM t2",
        first_seen_at=now - timedelta(days=100),
        last_seen_at=now - timedelta(days=80),
    )
    DeadlockEvent.objects.create(
        instance=instance,
        detected_at=now - timedelta(days=200),
        deadlock_total=5,
    )

    stats = services.purge_expired()

    assert stats["sql_digests"] == 1
    assert stats["deadlocks"] == 1
    assert not SqlDigest.objects.filter(pk=stale.pk).exists()
    assert SqlDigest.objects.filter(pk=fresh.pk).exists()
