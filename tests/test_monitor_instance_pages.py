"""实例监控页面测试。

会话页与慢日志页**打开就会连目标库**，因此这里一律 mock 掉
``execute_statement``：否则测试会真的去连 10.2.0.x，每个用例白等 5 秒超时，
而且结果取决于跑测试的机器能不能路由到那个网段。
"""

from __future__ import annotations

import pytest

from apps.dbconsole.services import QueryResult


@pytest.fixture
def instance(db):
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="实例监控部")
    cluster = Cluster.objects.create(
        name="实例监控集群", department=dept, engine="mysql", env="prod"
    )
    return Instance.objects.create(
        name="inst-mon", cluster=cluster, host="10.3.0.1", port=3306, username="mon"
    )


def stub_query(monkeypatch, result: QueryResult):
    from apps.web import monitor_instance_views

    monkeypatch.setattr(
        monitor_instance_views, "execute_statement", lambda *a, **kw: result
    )


@pytest.mark.django_db
def test_trend_page_renders_charts(client, instance):
    resp = client.get(f"/instances/{instance.pk}/monitor/trend/")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "性能趋势" in body
    # 每个指标一个图表容器，key 必须真的出现在 data-metric 上
    assert 'data-metric="mysql.qps"' in body
    assert "charts.js" in body


@pytest.mark.django_db
def test_trend_page_warns_on_collect_failure(client, instance):
    from django.utils import timezone

    from apps.monitor.models import CollectionRun

    CollectionRun.objects.create(
        instance=instance,
        collected_at=timezone.now(),
        ok=False,
        error_code="timeout",
        message="连接超时",
    )

    resp = client.get(f"/instances/{instance.pk}/monitor/trend/")
    body = resp.content.decode()

    assert "最近一次采集失败" in body
    assert "timeout" in body


@pytest.mark.django_db
def test_trend_page_hours_filter_preserved(client, instance):
    resp = client.get(f"/instances/{instance.pk}/monitor/trend/?hours=24")
    assert resp.status_code == 200
    assert 'value="24" selected' in resp.content.decode()


@pytest.mark.django_db
def test_realtime_page_shows_latest_values(client, instance):
    from django.utils import timezone

    from apps.monitor.models import MetricSample

    MetricSample.objects.create(
        instance=instance,
        metric="mysql.connections",
        value=17.0,
        collected_at=timezone.now(),
    )

    resp = client.get(f"/instances/{instance.pk}/monitor/realtime/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "17" in body


@pytest.mark.django_db
def test_realtime_page_without_data(client, instance):
    resp = client.get(f"/instances/{instance.pk}/monitor/realtime/")
    assert resp.status_code == 200
    assert "尚无采集记录" in resp.content.decode()


@pytest.mark.django_db
def test_instance_space_uses_latest_snapshot(client, instance):
    from datetime import timedelta

    from django.utils import timezone

    from apps.monitor.models import MetricSample

    now = timezone.now()
    MetricSample.objects.create(
        instance=instance,
        metric="mysql.database_size_bytes",
        dimension="shop",
        value=100.0,
        collected_at=now - timedelta(minutes=5),
    )
    MetricSample.objects.create(
        instance=instance,
        metric="mysql.database_size_bytes",
        dimension="shop",
        value=250.0,
        collected_at=now,
    )

    resp = client.get(f"/instances/{instance.pk}/monitor/space/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "shop" in body
    assert 'data-bytes="250"' in body
    assert 'data-bytes="100"' not in body


@pytest.mark.django_db
def test_sessions_page_renders_rows(client, instance, monkeypatch):
    stub_query(
        monkeypatch,
        QueryResult(
            ok=True,
            columns=["id", "user", "command"],
            rows=[[7, "app", "Query"]],
            row_count=1,
            affected=0,
            duration_ms=3,
            truncated=False,
            error="",
            message="",
        ),
    )

    resp = client.get(f"/instances/{instance.pk}/monitor/sessions/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "当前会话（1 条）" in body
    assert "app" in body


@pytest.mark.django_db
def test_sessions_page_reports_connection_error(client, instance, monkeypatch):
    """目标库连不上时页面必须照常打开，并写清原因——这正是运维最需要看到的。"""
    stub_query(
        monkeypatch,
        QueryResult(
            ok=False,
            columns=[],
            rows=[],
            row_count=0,
            affected=0,
            duration_ms=0,
            truncated=False,
            error="auth_failed",
            message="账号或密码不正确",
        ),
    )

    resp = client.get(f"/instances/{instance.pk}/monitor/sessions/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "无法获取会话列表" in body
    assert "auth_failed" in body
    assert "账号或密码不正确" in body


@pytest.mark.django_db
def test_slowlog_page_renders_and_hints(client, instance, monkeypatch):
    stub_query(
        monkeypatch,
        QueryResult(
            ok=False,
            columns=[],
            rows=[],
            row_count=0,
            affected=0,
            duration_ms=0,
            truncated=False,
            error="sql_error",
            message="Table 'performance_schema...' doesn't exist",
        ),
    )

    resp = client.get(f"/instances/{instance.pk}/monitor/slowlog/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "无法获取慢日志" in body
    # 失败时要告诉运维"去开什么"，而不是只有一个红框
    assert "如何启用" in body
    assert "performance_schema" in body


@pytest.mark.django_db
def test_unsupported_engine_pages(client, db):
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="其他引擎部2")
    cluster = Cluster.objects.create(name="mongo监控", department=dept, engine="mongodb")
    inst = Instance.objects.create(
        name="mongo-1", cluster=cluster, host="10.3.0.9", port=27017, username="mon"
    )

    for suffix in ("trend", "realtime", "space", "sessions", "slowlog"):
        resp = client.get(f"/instances/{inst.pk}/monitor/{suffix}/")
        assert resp.status_code == 200, suffix


@pytest.mark.django_db
def test_monitor_pages_404_for_missing_instance(client):
    for suffix in ("trend", "realtime", "space", "sessions", "slowlog"):
        resp = client.get(f"/instances/999999/monitor/{suffix}/")
        assert resp.status_code == 404, suffix
