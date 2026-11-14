"""SQL 统计采集与洞察页面测试。

重点：
1. 采集失败不能污染指标采集（两条链路独立成败）；
2. 源端累计值必须**覆盖**而不是累加（重启后累计归零）；
3. 洞察页按不同维度排序，且全库/单实例两个视角都可用。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def instance(db):
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="洞察部")
    cluster = Cluster.objects.create(name="洞察集群", department=dept, engine="mysql")
    return Instance.objects.create(
        name="sql-1", cluster=cluster, host="10.8.0.1", port=3306, username="mon"
    )


def _fake_rows(*rows):
    return list(rows)


@pytest.mark.django_db
def test_persist_normalizes_and_writes(instance):
    from apps.monitor.models import SqlDigest
    from apps.monitor.sql_collector import persist_sql_stats

    count = persist_sql_stats(
        instance,
        [
            ("SELECT * FROM t WHERE id = ?", 10, 100.0, 30.0, 500),
        ],
    )

    assert count == 1
    row = SqlDigest.objects.get()
    assert row.exec_count == 10
    assert row.avg_time_ms == pytest.approx(10.0)
    assert row.engine == "mysql"


@pytest.mark.django_db
def test_persist_overwrites_not_accumulates(instance):
    """源端是累计值：第二轮应覆盖，而不是把两轮相加。"""
    from apps.monitor.models import SqlDigest
    from apps.monitor.sql_collector import persist_sql_stats

    template = "SELECT * FROM t WHERE id = ?"
    persist_sql_stats(instance, [(template, 10, 100.0, 30.0, 500)])
    # 模拟源端重启后重新累计：次数变小
    persist_sql_stats(instance, [(template, 2, 20.0, 12.0, 40)])

    row = SqlDigest.objects.get()
    assert row.exec_count == 2
    assert row.total_time_ms == pytest.approx(20.0)


@pytest.mark.django_db
def test_persist_keeps_first_seen(instance):
    from apps.monitor.models import SqlDigest
    from apps.monitor.sql_collector import persist_sql_stats

    template = "SELECT * FROM t WHERE id = ?"
    persist_sql_stats(instance, [(template, 1, 1.0, 1.0, 1)])
    first = SqlDigest.objects.get().first_seen_at

    persist_sql_stats(instance, [(template, 2, 2.0, 1.0, 2)])

    assert SqlDigest.objects.get().first_seen_at == first


@pytest.mark.django_db
def test_persist_same_template_two_params_one_row(instance):
    """字面量不同但结构相同的语句应合并成一行。"""
    from apps.monitor.models import SqlDigest
    from apps.monitor.sql_collector import persist_sql_stats
    from apps.monitor.sqltext import normalize_sql

    a = normalize_sql("SELECT * FROM t WHERE id = 1")
    b = normalize_sql("SELECT * FROM t WHERE id = 2")
    assert a == b

    persist_sql_stats(instance, [(a, 1, 1.0, 1.0, 1)])
    persist_sql_stats(instance, [(b, 2, 2.0, 1.0, 2)])

    assert SqlDigest.objects.count() == 1


@pytest.mark.django_db
def test_fetch_reports_unsupported_engine(db):
    from apps.assets.models import Cluster, Department, Instance
    from apps.monitor.sql_collector import fetch_sql_stats

    dept = Department.objects.create(name="不支持的部")
    cluster = Cluster.objects.create(name="mongo", department=dept, engine="mongodb")
    inst = Instance.objects.create(
        name="m", cluster=cluster, host="10.8.9.9", port=27017, username="mon"
    )

    result = fetch_sql_stats(inst, "pw", 5)
    assert result["ok"] is False
    assert result["error"] == "unsupported_engine"


@pytest.mark.django_db
def test_fetch_swallows_driver_error(instance, monkeypatch):
    """驱动异常只留类型名，不能把连接串/用户名带到上层。"""
    from apps.monitor import sql_collector

    def boom(inst, password, timeout):
        raise RuntimeError("password=secret host=10.8.0.1")

    monkeypatch.setitem(sql_collector._COLLECTORS, "mysql", boom)
    result = sql_collector.fetch_sql_stats(instance, "pw", 5)

    assert result["ok"] is False
    assert result["error"] == "query_failed"
    assert "secret" not in str(result)


@pytest.mark.django_db
def test_sql_failure_does_not_fail_metric_collection(instance, monkeypatch):
    """语句统计取不到时，指标采集仍应记成功。"""
    from apps.monitor import services, sql_collector
    from apps.monitor.collectors.base import CollectResult, Sample

    monkeypatch.setattr(
        "apps.dbconsole.services.resolve_password", lambda inst: ("pw", None)
    )
    monkeypatch.setattr(
        services,
        "collect_for",
        lambda inst, pw, timeout: CollectResult(
            ok=True, samples=[Sample("mysql.connections", 5.0)]
        ),
    )
    monkeypatch.setattr(
        sql_collector,
        "fetch_sql_stats",
        lambda inst, pw, timeout: {"ok": False, "error": "query_failed", "rows": []},
    )

    run = services.collect_instance(instance)

    assert run.ok is True
    assert instance.metric_samples.count() == 1


@pytest.mark.django_db
def test_insight_page_renders(client, instance):
    from apps.monitor.models import SqlDigest
    from apps.monitor.sql_collector import persist_sql_stats

    persist_sql_stats(instance, [("SELECT * FROM orders WHERE id = ?", 9, 90.0, 20.0, 30)])

    resp = client.get("/monitor/sql/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "SELECT * FROM orders" in body
    assert "9" in body


@pytest.mark.django_db
def test_insight_page_instance_view(client, instance):
    from apps.monitor.sql_collector import persist_sql_stats

    persist_sql_stats(instance, [("SELECT 1", 1, 1.0, 1.0, 0)])

    resp = client.get(f"/instances/{instance.pk}/monitor/sql/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "sql-1" in body
    # 单实例视角下不再重复显示实例列
    assert "全库视图" in body


@pytest.mark.django_db
def test_insight_page_sort_by_avg(client, instance):
    from apps.monitor.sql_collector import persist_sql_stats

    persist_sql_stats(
        instance,
        [
            ("SELECT fast_but_frequent", 1000, 1000.0, 1.0, 0),
            ("SELECT slow_but_rare", 1, 5000.0, 5000.0, 0),
        ],
    )

    body = client.get("/monitor/sql/?sort=avg").content.decode()
    # 平均耗时排序下，单次 5000ms 那条应排在前面
    assert body.index("slow_but_rare") < body.index("fast_but_frequent")


@pytest.mark.django_db
def test_insight_page_sort_by_total(client, instance):
    from apps.monitor.sql_collector import persist_sql_stats

    persist_sql_stats(
        instance,
        [
            ("SELECT fast_but_frequent", 1000, 9000.0, 1.0, 0),
            ("SELECT slow_but_rare", 1, 5000.0, 5000.0, 0),
        ],
    )

    body = client.get("/monitor/sql/?sort=total").content.decode()
    assert body.index("fast_but_frequent") < body.index("slow_but_rare")


@pytest.mark.django_db
def test_insight_page_empty_state(client, db):
    resp = client.get("/monitor/sql/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "还没有 SQL 指纹数据" in body
    # 空状态要给出可执行的排查方向，而不是只说"没有数据"
    assert "performance_schema" in body


@pytest.mark.django_db
def test_insight_page_404_for_missing_instance(client):
    resp = client.get("/instances/999999/monitor/sql/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_sql_digest_cascade_on_instance_delete(instance):
    """洞察数据跟实例走：实例没了，指纹无归属，保留只占空间。"""
    from apps.monitor.models import SqlDigest
    from apps.monitor.sql_collector import persist_sql_stats

    persist_sql_stats(instance, [("SELECT 1", 1, 1.0, 1.0, 0)])
    assert SqlDigest.objects.count() == 1

    instance.delete()

    assert SqlDigest.objects.count() == 0
