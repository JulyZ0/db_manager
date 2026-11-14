"""PostgreSQL 采集器解析逻辑测试（假驱动，不连真实 PG）。"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def instance(db):
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="PG测试部")
    cluster = Cluster.objects.create(
        name="PG测试集群", department=dept, engine="postgresql"
    )
    return Instance.objects.create(
        name="pg-1", cluster=cluster, host="10.0.0.2", port=5432, username="mon"
    )


class FakeCursor:
    def __init__(self, replies):
        self.replies = replies
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        key = " ".join(str(sql).split()).lower()
        for prefix, value in self.replies.items():
            if key.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                self._rows = value
                return
        raise AssertionError(f"未预期的语句: {sql}")

    def fetchall(self):
        return self._rows


def install_fake_psycopg(monkeypatch, replies):
    class FakeConn:
        def cursor(self):
            return FakeCursor(replies)

        def close(self):
            pass

    module = types.ModuleType("psycopg")
    module.connect = lambda **kwargs: FakeConn()
    monkeypatch.setitem(sys.modules, "psycopg", module)


FULL_REPLIES = {
    "select state, count(*)": [("active", 3), ("idle", 7)],
    "select name, setting": [
        ("max_connections", "100"),
        ("superuser_reserved_connections", "3"),
    ],
    "select coalesce(extract(epoch from max": [(12.5,)],
    "select datname, xact_commit": [
        ("app", 900, 100, 400, 3600, 0, 0, 1024, 2),
        ("report", 500, 0, 100, 1400, 0, 0, 0, 0),
    ],
    "select datname, pg_database_size": [("app", 2048), ("report", 1024)],
    "select coalesce(extract(epoch from replay_lag)": [],
}


@pytest.mark.django_db
def test_collects_core_metrics(instance, monkeypatch):
    from apps.monitor.collectors import postgresql

    install_fake_psycopg(monkeypatch, FULL_REPLIES)
    result = postgresql.collect(instance, "pw", 5)

    assert result.ok
    values = {s.metric: s.value for s in result.samples}
    assert values["pg.connections"] == 10.0
    assert values["pg.connections_active"] == 3.0
    assert values["pg.connections_idle"] == 7.0
    # 上限要扣掉超管保留配额：100 - 3 = 97，10/97
    assert values["pg.connections_max"] == 100.0
    assert values["pg.connection_usage_percent"] == pytest.approx(10 / 97 * 100)
    assert values["pg.longest_transaction_seconds"] == 12.5


@pytest.mark.django_db
def test_database_aggregates_and_rollback_ratio(instance, monkeypatch):
    from apps.monitor.collectors import postgresql

    install_fake_psycopg(monkeypatch, FULL_REPLIES)
    result = postgresql.collect(instance, "pw", 5)

    values = {s.metric: s.value for s in result.samples}
    assert values["pg.xact_commit_total"] == 1400.0
    assert values["pg.xact_rollback_total"] == 100.0
    assert values["pg.rollback_percent"] == pytest.approx(100 / 1500 * 100)
    assert values["pg.deadlocks_total"] == 2.0
    # 命中率 = 5000 / (5000 + 500)
    assert values["pg.cache_hit_percent"] == pytest.approx(5000 / 5500 * 100)

    per_db = {
        s.dimension: s.value
        for s in result.samples
        if s.metric == "pg.database_xact_total"
    }
    assert per_db == {"app": 1000.0, "report": 500.0}


@pytest.mark.django_db
def test_space_by_database(instance, monkeypatch):
    from apps.monitor.collectors import postgresql

    install_fake_psycopg(monkeypatch, FULL_REPLIES)
    result = postgresql.collect(instance, "pw", 5)

    space = {
        s.dimension: s.value
        for s in result.samples
        if s.metric == "pg.database_size_bytes"
    }
    assert space == {"app": 2048.0, "report": 1024.0}
    values = {s.metric: s.value for s in result.samples}
    assert values["pg.total_size_bytes"] == 3072.0


@pytest.mark.django_db
def test_no_replicas_writes_no_lag_sample(instance, monkeypatch):
    """主库无从库时不应写 0 延迟——那会让"延迟 0 秒"看起来像有从库。"""
    from apps.monitor.collectors import postgresql

    install_fake_psycopg(monkeypatch, FULL_REPLIES)
    result = postgresql.collect(instance, "pw", 5)

    metrics = {s.metric for s in result.samples}
    assert "pg.replication_lag_seconds" not in metrics
    assert "pg.replica_count" not in metrics


@pytest.mark.django_db
def test_all_queries_denied_is_failure_not_silent_success(instance, monkeypatch):
    """能连上但每条查询都被拒（权限被回收）必须判失败。

    若返回 ok=True + 空样本，看板曲线静默断档、采集记录却是绿色，
    连续失败告警也不会触发。
    """
    from apps.monitor.collectors import postgresql

    install_fake_psycopg(
        monkeypatch,
        {
            "select state, count(*)": RuntimeError("denied"),
            "select name, setting": RuntimeError("denied"),
            "select coalesce(extract(epoch from max": RuntimeError("denied"),
            "select datname, xact_commit": RuntimeError("denied"),
            "select datname, pg_database_size": RuntimeError("denied"),
            "select coalesce(extract(epoch from replay_lag)": RuntimeError("denied"),
        },
    )
    result = postgresql.collect(instance, "pw", 5)

    assert not result.ok
    assert result.error_code == "query_failed"
    assert "权限" in result.message


@pytest.mark.django_db
def test_missing_driver(instance, monkeypatch):
    from apps.monitor.collectors import postgresql

    monkeypatch.setitem(sys.modules, "psycopg", None)
    result = postgresql.collect(instance, "pw", 5)

    assert not result.ok
    assert result.error_code == "driver_missing"


@pytest.mark.django_db
def test_replication_lag_uses_max_across_replicas(instance, monkeypatch):
    from apps.monitor.collectors import postgresql

    replies = dict(FULL_REPLIES)
    replies["select coalesce(extract(epoch from replay_lag)"] = [(1.0, 100), (7.0, 4096)]
    install_fake_psycopg(monkeypatch, replies)

    result = postgresql.collect(instance, "pw", 5)
    values = {s.metric: s.value for s in result.samples}
    assert values["pg.replication_lag_seconds"] == 7.0
    assert values["pg.replication_lag_bytes"] == 4096.0
    assert values["pg.replica_count"] == 2.0
