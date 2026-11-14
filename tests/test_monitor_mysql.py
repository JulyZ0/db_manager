"""MySQL 采集器解析逻辑测试。

用假驱动替代 pymysql：采集器的价值在于「把 SHOW STATUS 的口径翻译成指标」，
这部分不需要真实 MySQL 就能完整验证；真实建连由手工验证覆盖。
"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def instance(db):
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="采集测试部")
    cluster = Cluster.objects.create(
        name="采集测试集群", department=dept, engine="mysql"
    )
    return Instance.objects.create(
        name="mysql-1",
        cluster=cluster,
        host="10.0.0.1",
        port=3306,
        username="mon",
    )


class FakeCursor:
    def __init__(self, replies):
        self.replies = replies
        self.description = None
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
                if isinstance(value, tuple):
                    self.description = [(c,) for c in value[0]]
                    self._rows = value[1]
                else:
                    self.description = [("Variable_name",), ("Value",)]
                    self._rows = list(value.items())
                return
        raise AssertionError(f"未预期的语句: {sql}")

    def fetchall(self):
        return self._rows


def install_fake_pymysql(monkeypatch, replies):
    """把假 pymysql 装进 sys.modules，并返回连接的关闭计数。"""
    closed = []

    class FakeConn:
        def cursor(self):
            return FakeCursor(replies)

        def close(self):
            closed.append(True)

    module = types.ModuleType("pymysql")
    module.connect = lambda **kwargs: FakeConn()
    monkeypatch.setitem(sys.modules, "pymysql", module)
    return closed


BASE_STATUS = {
    "Threads_connected": "12",
    "Threads_running": "2",
    "Queries": "1000",
    "Slow_queries": "7",
    "Innodb_buffer_pool_read_requests": "1000",
    "Innodb_buffer_pool_reads": "50",
    "Innodb_buffer_pool_pages_total": "100",
    "Innodb_buffer_pool_pages_free": "25",
    "Innodb_buffer_pool_pages_dirty": "10",
    "Uptime": "3600",
}
BASE_VARS = {"max_connections": "200", "innodb_page_size": "16384"}


@pytest.mark.django_db
def test_collects_core_metrics(instance, monkeypatch):
    from apps.monitor.collectors import mysql

    install_fake_pymysql(
        monkeypatch,
        {
            "show global status": BASE_STATUS,
            "show global variables": BASE_VARS,
            "select table_schema": ("x", []),
        },
    )

    result = mysql.collect(instance, "pw", 5)

    assert result.ok
    values = {s.metric: s.value for s in result.samples}
    assert values["mysql.connections"] == 12.0
    assert values["mysql.connections_max"] == 200.0
    assert values["mysql.connection_usage_percent"] == pytest.approx(6.0)
    # 累计计数器原样保留，速率由任务层换算
    assert values["mysql.queries_total"] == 1000.0
    assert values["mysql.slow_queries_total"] == 7.0
    # 命中率 = 100 - 50/1000*100 = 95
    assert values["mysql.buffer_pool_hit_percent"] == pytest.approx(95.0)
    # 缓冲池用量 = (100-25) * 16384
    assert values["mysql.buffer_pool_used_bytes"] == pytest.approx(75 * 16384)


@pytest.mark.django_db
def test_space_is_split_by_database(instance, monkeypatch):
    from apps.monitor.collectors import mysql

    install_fake_pymysql(
        monkeypatch,
        {
            "show global status": BASE_STATUS,
            "show global variables": BASE_VARS,
            "select table_schema": (
                ("t", "b", "c"),
                [("app", 1024, 3), ("report", 2048, 5)],
            ),
        },
    )

    result = mysql.collect(instance, "pw", 5)

    space = {
        s.dimension: s.value
        for s in result.samples
        if s.metric == "mysql.database_size_bytes"
    }
    assert space == {"app": 1024.0, "report": 2048.0}
    assert any(
        s.metric == "mysql.total_size_bytes" and s.value == 3072.0
        for s in result.samples
    )


@pytest.mark.django_db
def test_missing_driver_reports_code(instance, monkeypatch):
    from apps.monitor.collectors import mysql

    monkeypatch.setitem(sys.modules, "pymysql", None)

    result = mysql.collect(instance, "pw", 5)

    assert not result.ok
    assert result.error_code == "driver_missing"


@pytest.mark.django_db
def test_status_unreadable_fails_whole_collect(instance, monkeypatch):
    from apps.monitor.collectors import mysql

    install_fake_pymysql(
        monkeypatch,
        {
            "show global status": RuntimeError("boom"),
            "show global variables": BASE_VARS,
        },
    )

    result = mysql.collect(instance, "pw", 5)

    assert not result.ok
    assert result.error_code == "query_failed"


@pytest.mark.django_db
def test_space_failure_keeps_core_metrics(instance, monkeypatch):
    """空间统计失败不该让连接数、命中率一起丢掉。"""
    from apps.monitor.collectors import mysql

    install_fake_pymysql(
        monkeypatch,
        {
            "show global status": BASE_STATUS,
            "show global variables": BASE_VARS,
            "select table_schema": RuntimeError("denied"),
            "show replica status": RuntimeError("no"),
            "show slave status": RuntimeError("no"),
        },
    )

    result = mysql.collect(instance, "pw", 5)

    assert result.ok
    metrics = {s.metric for s in result.samples}
    assert "mysql.connections" in metrics
    assert "mysql.database_size_bytes" not in metrics
    assert "权限" in result.message


@pytest.mark.django_db
def test_replication_delay_and_thread_state(instance, monkeypatch):
    from apps.monitor.collectors import mysql

    install_fake_pymysql(
        monkeypatch,
        {
            "show global status": BASE_STATUS,
            "show global variables": BASE_VARS,
            "select table_schema": ("x", []),
            "show replica status": (
                ("Seconds_Behind_Source", "Replica_IO_Running", "Replica_SQL_Running"),
                [(3, "Yes", "No")],
            ),
        },
    )

    result = mysql.collect(instance, "pw", 5)

    values = {s.metric: s.value for s in result.samples}
    assert values["mysql.replication_delay_seconds"] == 3.0
    assert values["mysql.replica_io_running"] == 1.0
    assert values["mysql.replica_sql_running"] == 0.0


@pytest.mark.django_db
def test_invalid_values_are_skipped_not_zeroed(instance, monkeypatch):
    """无法解析的值直接跳过：写 0 会让趋势图出现假的"归零"。"""
    from apps.monitor.collectors import mysql

    install_fake_pymysql(
        monkeypatch,
        {
            "show global status": {
                "Threads_connected": "abc",
                "Queries": "10",
            },
            "show global variables": {"max_connections": "0"},
            "select table_schema": ("x", []),
        },
    )

    result = mysql.collect(instance, "pw", 5)

    metrics = {s.metric for s in result.samples}
    assert "mysql.connections" not in metrics
    # max_connections=0 时百分比无意义，不能产出 0% 或无穷大
    assert "mysql.connection_usage_percent" not in metrics
