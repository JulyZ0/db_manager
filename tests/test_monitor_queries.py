"""时序查询层与监控 API 测试。

重点钉住三件容易静默出错的事：
1. 窗口非法输入要退回默认，而不是给运维一个错误页；
2. 降采样必须按跨度切到 minute/hour/day，否则 30 天窗口会取回几十万行；
3. latest_values 只取最新一条、且不跨实例串值。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone


@pytest.fixture
def instances(db):
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="查询测试部")
    cluster = Cluster.objects.create(
        name="查询测试集群", department=dept, engine="mysql"
    )
    first = Instance.objects.create(
        name="q-1", cluster=cluster, host="10.0.1.1", port=3306, username="mon"
    )
    second = Instance.objects.create(
        name="q-2", cluster=cluster, host="10.0.1.2", port=3306, username="mon"
    )
    return first, second


def seed(instance, metric, values, start, step=timedelta(minutes=1)):
    from apps.monitor.models import MetricSample

    rows = [
        MetricSample(
            instance=instance,
            metric=metric,
            value=float(value),
            collected_at=start + step * index,
        )
        for index, value in enumerate(values)
    ]
    MetricSample.objects.bulk_create(rows)


@pytest.mark.django_db
def test_window_defaults_and_swaps_reversed_range():
    from apps.monitor.queries import resolve_window

    now = timezone.now()
    start, end = resolve_window(now, now - timedelta(hours=2))
    assert start < end
    assert end - start <= timedelta(days=1)


@pytest.mark.django_db
def test_window_clamped_to_max_span():
    from apps.monitor.queries import MAX_WINDOW_DAYS, resolve_window

    now = timezone.now()
    start, end = resolve_window(now - timedelta(days=400), now)
    assert end - start <= timedelta(days=MAX_WINDOW_DAYS)


@pytest.mark.django_db
def test_window_never_in_future():
    from apps.monitor.queries import resolve_window

    now = timezone.now()
    start, end = resolve_window(now + timedelta(hours=5), now + timedelta(hours=9))
    assert end <= timezone.now() + timedelta(seconds=5)
    assert start < end


@pytest.mark.django_db
def test_series_grouped_by_instance(instances):
    from apps.monitor.queries import fetch_series

    first, second = instances
    now = timezone.now()
    seed(first, "mysql.qps", [1, 2, 3], now - timedelta(minutes=3))
    seed(second, "mysql.qps", [10], now - timedelta(minutes=1))

    result = fetch_series([first.pk, second.pk], ["mysql.qps"], now - timedelta(hours=1), now)

    assert len(result["mysql.qps"][first.pk]) == 3
    assert len(result["mysql.qps"][second.pk]) == 1
    assert result["mysql.qps"][first.pk][0]["v"] == 1.0


@pytest.mark.django_db
def test_series_averages_within_bucket(instances):
    """小时窗口内同一分钟的多条样本取平均，而不是各占一个点。"""
    from apps.monitor.queries import fetch_series

    first, _ = instances
    now = timezone.now()
    base = (now - timedelta(minutes=10)).replace(second=0, microsecond=0)
    seed(first, "mysql.qps", [2, 4], base, step=timedelta(seconds=20))

    result = fetch_series(
        [first.pk], ["mysql.qps"], now - timedelta(days=1), now
    )

    points = result["mysql.qps"][first.pk]
    assert len(points) == 1
    assert points[0]["v"] == pytest.approx(3.0)


@pytest.mark.django_db
def test_series_truncated_to_max_points(instances):
    from apps.monitor.queries import MAX_POINTS, fetch_series

    first, _ = instances
    now = timezone.now()
    seed(
        first,
        "mysql.qps",
        list(range(500)),
        now - timedelta(minutes=500),
    )

    result = fetch_series([first.pk], ["mysql.qps"], now - timedelta(hours=1), now)

    assert len(result["mysql.qps"][first.pk]) <= MAX_POINTS


@pytest.mark.django_db
def test_series_empty_for_unknown_metric(instances):
    from apps.monitor.queries import fetch_series

    first, _ = instances
    now = timezone.now()
    result = fetch_series([first.pk], ["nope.metric"], now - timedelta(hours=1), now)
    assert result["nope.metric"] == {}


@pytest.mark.django_db
def test_latest_values_picks_newest_only(instances):
    from apps.monitor.queries import latest_values

    first, _ = instances
    now = timezone.now()
    seed(first, "mysql.qps", [1, 2, 3], now - timedelta(minutes=3))

    result = latest_values([first.pk], ["mysql.qps"])

    assert result["mysql.qps"][first.pk]["v"] == 3.0


@pytest.mark.django_db
def test_latest_values_do_not_mix_instances(instances):
    from apps.monitor.queries import latest_values

    first, second = instances
    now = timezone.now()
    seed(first, "mysql.qps", [5], now - timedelta(minutes=1))
    seed(second, "mysql.qps", [9], now)

    result = latest_values([first.pk, second.pk], ["mysql.qps"])

    assert result["mysql.qps"][first.pk]["v"] == 5.0
    assert result["mysql.qps"][second.pk]["v"] == 9.0


@pytest.mark.django_db
def test_series_endpoint(api_client, instances):
    first, _ = instances
    now = timezone.now()
    seed(first, "mysql.qps", [1, 2], now - timedelta(minutes=2))

    resp = api_client.get(
        "/api/v1/monitor/series",
        {"metrics": "mysql.qps", "instance_ids": str(first.pk)},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["series"][0]["metric"] == "mysql.qps"
    assert body["series"][0]["instance_name"] == "q-1"


@pytest.mark.django_db
def test_latest_endpoint(api_client, instances):
    first, _ = instances
    seed(first, "mysql.qps", [7], timezone.now())

    resp = api_client.get(
        "/api/v1/monitor/latest",
        {"metrics": "mysql.qps", "instance_ids": str(first.pk)},
    )

    assert resp.status_code == 200
    assert resp.json()["results"][0]["value"] == 7.0


@pytest.mark.django_db
def test_collect_status_reports_no_data(api_client, instances):
    first, _ = instances

    resp = api_client.get(
        "/api/v1/monitor/collect-status", {"instance_ids": str(first.pk)}
    )

    assert resp.status_code == 200
    row = resp.json()["results"][0]
    assert row["ok"] is False
    assert row["error_code"] == "no_data"


@pytest.mark.django_db
def test_collect_status_reports_latest_run(api_client, instances):
    from apps.monitor.models import CollectionRun

    first, _ = instances
    CollectionRun.objects.create(
        instance=first, collected_at=timezone.now(), ok=True, metrics_count=3
    )

    resp = api_client.get(
        "/api/v1/monitor/collect-status", {"instance_ids": str(first.pk)}
    )

    assert resp.json()["results"][0]["ok"] is True


@pytest.mark.django_db
def test_series_endpoint_requires_login(anon_client):
    resp = anon_client.get("/api/v1/monitor/series", {"metrics": "mysql.qps"})
    assert resp.status_code in (401, 403)
