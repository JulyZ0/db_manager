"""Redis 采集器解析逻辑测试（假驱动，不连真实 Redis）。

重点覆盖三类容易出错的地方：
- INFO 的 keyspace 段是嵌套 dict，要按 db 拆成多行；
- 命中率的分母是 hits+misses，任一缺失时不能产出 0%；
- SCAN 采样失败不能拖垮核心指标，且绝不调用 KEYS。
"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def instance(db):
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="Redis测试部")
    cluster = Cluster.objects.create(
        name="Redis测试集群", department=dept, engine="redis"
    )
    return Instance.objects.create(
        name="redis-1", cluster=cluster, host="10.0.0.3", port=6379, username="mon"
    )


BASE_INFO = {
    "used_memory": 1048576,
    "used_memory_rss": 2097152,
    "used_memory_peak": 3145728,
    "used_memory_dataset": 524288,
    "maxmemory": 4194304,
    "mem_fragmentation_ratio": 2.0,
    "connected_clients": 12,
    "blocked_clients": 1,
    "rejected_connections": 0,
    "keyspace_hits": 900,
    "keyspace_misses": 100,
    "instantaneous_ops_per_sec": 42,
    "instantaneous_input_kbps": 1.5,
    "instantaneous_output_kbps": 2.5,
    "total_commands_processed": 100000,
    "total_connections_received": 500,
    "evicted_keys": 3,
    "expired_keys": 7,
    "rdb_last_bgsave_status": "ok",
    "rdb_last_bgsave_time_sec": 2,
    "aof_last_bgrewrite_status": "ok",
    "aof_current_size": 4096,
    "db0": {"keys": 100, "expires": 10, "avg_ttl": 0},
    "db1": {"keys": 50, "expires": 5, "avg_ttl": 0},
    "role": "master",
    "connected_slaves": 2,
    "master_repl_offset": 12345,
}


class FakeRedis:
    def __init__(self, info, keys=(), types_=(), usages=(), scan_fails=False):
        self._info = info
        self._keys = list(keys)
        self._types = list(types_)
        self._usages = list(usages)
        self._scan_fails = scan_fails
        self.commands: list[tuple] = []

    def ping(self):
        return True

    def info(self):
        return self._info

    def scan(self, cursor=0, count=100):
        if self._scan_fails:
            raise RuntimeError("scan failed")
        if cursor:
            return 0, []
        return 0, self._keys

    def pipeline(self, transaction=False):
        return FakePipeline(self)

    def execute_command(self, *args):
        self.commands.append(args)
        if args[0].upper() == "SLOWLOG":
            return 5
        return None

    def close(self):
        pass


class FakePipeline:
    def __init__(self, parent):
        self.parent = parent
        self.ops = []

    def type(self, key):
        self.ops.append(("type", key))
        return self

    def memory_usage(self, key):
        self.ops.append(("memory_usage", key))
        return self

    def execute(self):
        if self.ops and self.ops[0][0] == "type":
            return self.parent._types
        return self.parent._usages


def install_fake_redis(monkeypatch, **kwargs):
    holder = {}

    def factory(**conn_kwargs):
        client = FakeRedis(**kwargs)
        holder["client"] = client
        return client

    module = types.ModuleType("redis")
    module.Redis = factory
    monkeypatch.setitem(sys.modules, "redis", module)
    return holder


@pytest.mark.django_db
def test_collects_memory_and_clients(instance, monkeypatch):
    from apps.monitor.collectors import redis_collector

    install_fake_redis(monkeypatch, info=BASE_INFO)
    result = redis_collector.collect(instance, "pw", 5)

    assert result.ok
    values = {s.metric: s.value for s in result.samples}
    assert values["redis.memory_used_bytes"] == 1048576.0
    assert values["redis.mem_fragmentation_ratio"] == 2.0
    # 使用率 = 1048576 / 4194304 = 25%
    assert values["redis.memory_usage_percent"] == pytest.approx(25.0)
    assert values["redis.connected_clients"] == 12.0
    assert values["redis.blocked_clients"] == 1.0


@pytest.mark.django_db
def test_hit_rate(instance, monkeypatch):
    from apps.monitor.collectors import redis_collector

    install_fake_redis(monkeypatch, info=BASE_INFO)
    result = redis_collector.collect(instance, "pw", 5)

    values = {s.metric: s.value for s in result.samples}
    assert values["redis.cache_hit_percent"] == pytest.approx(90.0)
    assert values["redis.ops_per_sec"] == 42.0


@pytest.mark.django_db
def test_keyspace_split_by_db(instance, monkeypatch):
    from apps.monitor.collectors import redis_collector

    install_fake_redis(monkeypatch, info=BASE_INFO)
    result = redis_collector.collect(instance, "pw", 5)

    per_db = {
        s.dimension: s.value
        for s in result.samples
        if s.metric == "redis.database_keys"
    }
    assert per_db == {"db0": 100.0, "db1": 50.0}
    values = {s.metric: s.value for s in result.samples}
    assert values["redis.total_keys"] == 150.0


@pytest.mark.django_db
def test_top_keys_sampled_by_memory(instance, monkeypatch):
    from apps.monitor.collectors import redis_collector

    install_fake_redis(
        monkeypatch,
        info=BASE_INFO,
        keys=["a", "b", "c"],
        types_=["string", "hash", "string"],
        usages=[100, 900, 500],
    )
    result = redis_collector.collect(instance, "pw", 5)

    biggest = [
        (s.dimension, s.value)
        for s in result.samples
        if s.metric == "redis.sampled_biggest_key_bytes"
    ]
    # 按内存降序：b(900) > c(500) > a(100)
    assert biggest == [("b", 900.0), ("c", 500.0), ("a", 100.0)]
    type_counts = {
        s.dimension: s.value
        for s in result.samples
        if s.metric == "redis.sampled_key_type_count"
    }
    assert type_counts == {"string": 2.0, "hash": 1.0}


@pytest.mark.django_db
def test_scan_failure_keeps_core_metrics(instance, monkeypatch):
    """Top Key 采样失败只丢该指标，其它照常。"""
    from apps.monitor.collectors import redis_collector

    install_fake_redis(monkeypatch, info=BASE_INFO, scan_fails=True)
    result = redis_collector.collect(instance, "pw", 5)

    assert result.ok
    metrics = {s.metric for s in result.samples}
    assert "redis.memory_used_bytes" in metrics
    assert "redis.sampled_biggest_key_bytes" not in metrics


@pytest.mark.django_db
def test_never_calls_keys_command(instance, monkeypatch):
    """绝不使用 KEYS：O(N) 且阻塞单线程，会把被监控实例打挂。"""
    from apps.monitor.collectors import redis_collector

    holder = install_fake_redis(monkeypatch, info=BASE_INFO, keys=["k1"])
    redis_collector.collect(instance, "pw", 5)

    sent = [c[0].upper() for c in holder["client"].commands]
    assert "KEYS" not in sent
    assert "SLOWLOG" in sent


@pytest.mark.django_db
def test_slave_role_reports_link_state(instance, monkeypatch):
    from apps.monitor.collectors import redis_collector

    info = dict(BASE_INFO)
    info["role"] = "slave"
    info["master_link_status"] = "down"
    info["slave_repl_offset"] = 999
    info.pop("master_repl_offset", None)

    install_fake_redis(monkeypatch, info=info)
    result = redis_collector.collect(instance, "pw", 5)

    values = {s.metric: s.value for s in result.samples}
    assert values["redis.role_master"] == 0.0
    assert values["redis.master_link_up"] == 0.0
    assert values["redis.slave_repl_offset"] == 999.0


@pytest.mark.django_db
def test_empty_info_is_failure(instance, monkeypatch):
    from apps.monitor.collectors import redis_collector

    install_fake_redis(monkeypatch, info={})
    result = redis_collector.collect(instance, "pw", 5)

    assert not result.ok
    assert result.error_code == "query_failed"


@pytest.mark.django_db
def test_missing_driver(instance, monkeypatch):
    from apps.monitor.collectors import redis_collector

    monkeypatch.setitem(sys.modules, "redis", None)
    result = redis_collector.collect(instance, "pw", 5)

    assert not result.ok
    assert result.error_code == "driver_missing"
