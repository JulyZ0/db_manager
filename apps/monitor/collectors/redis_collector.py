"""Redis 指标采集。

为什么不碰 ``KEYS *``
--------------------
``KEYS`` 是 O(N) 且**阻塞单线程**。在一个有千万 key 的生产实例上执行它，
会让整个实例停摆数秒到数十秒——监控把被监控对象打挂，是最不可接受的
事故类型。这里一律用 ``SCAN`` 游标增量遍历，并设采样上限。

Top Key 的实现
--------------
真正"按内存排序的 Top Key"需要对每个 key 测 ``MEMORY USAGE``，代价很高。
这里退一步：SCAN 采样有限个 key，用 pipeline 批量取 type 与 memory_usage，
产出「采样范围内最大的若干个 key」并在指标名上写明 ``sampled_``。
标签里不带 ``top`` 之类会误导的词——运维需要知道这是采样结果，
否则会把"不在列表里"误读成"这个 key 不占内存"。

采样到的 key 名会落库。key 名可能含业务标识（如 ``user:12345``），
这一点在用户指南里明确写出，便于有合规要求的部署选择调小或关闭采样。
"""

from __future__ import annotations

from apps.monitor.collectors.base import (
    ERROR_DRIVER_MISSING,
    ERROR_QUERY_FAILED,
    CollectResult,
    Sample,
    as_float,
    percent,
)

__all__ = ["collect"]

# 单次采集最多查看多少个 key。默认 500：一次 pipeline 两次往返即可完成，
# 对大实例也只需毫秒级；调大意味着更准的 Top Key，但也更慢。
DEFAULT_SCAN_LIMIT = 500
# 落库的最大 key 数（按内存降序取前 N）
TOP_KEY_KEEP = 5
# key 名落库时的长度上限，防止超长 key 撑爆 varchar(200)
KEY_NAME_MAX = 200


def collect(instance, password: str, timeout: int) -> CollectResult:
    """采集一个 Redis 实例的指标。"""
    try:
        import redis
    except ImportError:
        return CollectResult.failed(ERROR_DRIVER_MISSING)

    client = None
    try:
        client = redis.Redis(
            host=instance.host,
            port=int(instance.port),
            password=password or None,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
            decode_responses=True,
        )
        # 先 ping：连不上时 INFO 会抛异常，但 ping 的异常更容易分类，
        # 也避免某些代理在未认证时对 INFO 返回混乱结果。
        client.ping()

        info = client.info()
        if not isinstance(info, dict) or not info:
            return CollectResult.failed(ERROR_QUERY_FAILED, "INFO 返回为空")

        # INFO 的键名在各版本都是小写加下划线，但 section 前缀大小写不一，
        # 统一转小写后再取值，避免版本差异导致整组指标静默取不到。
        flat = {str(k).lower(): v for k, v in info.items()}

        samples: list[Sample] = []
        _collect_memory(flat, samples)
        _collect_clients(flat, samples)
        _collect_hit_rate(flat, samples)
        _collect_counters(flat, samples)
        _collect_persistence(flat, samples)
        _collect_keyspace(flat, samples)
        _collect_replication(flat, samples)
        _collect_slowlog(client, samples)
        _collect_top_keys(client, samples)

        if not samples:
            return CollectResult.failed(
                ERROR_QUERY_FAILED, "未取到任何指标，可能是账号权限受限"
            )
        return CollectResult(ok=True, samples=samples)

    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return CollectResult.failed(ERROR_QUERY_FAILED, _safe_reason(exc))
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _add(
    samples: list[Sample],
    metric: str,
    value: float | None,
    dimension: str = "",
) -> None:
    if value is None:
        return
    samples.append(Sample(metric=metric, value=float(value), dimension=dimension))


def _collect_memory(flat: dict, samples: list[Sample]) -> None:
    """内存占用与碎片率。

    碎片率 = rss / used_memory。>1.5 通常说明分配器碎片严重（频繁增删大 key、
    或曾经有过内存峰值），<1 说明部分内存被换出到 swap——后者比前者更危险，
    因为 Redis 一旦碰 swap 延迟会暴涨。
    """
    used = as_float(flat.get("used_memory"))
    rss = as_float(flat.get("used_memory_rss"))
    peak = as_float(flat.get("used_memory_peak"))
    maxmemory = as_float(flat.get("maxmemory"))
    dataset = as_float(flat.get("used_memory_dataset"))

    _add(samples, "redis.memory_used_bytes", used)
    _add(samples, "redis.memory_rss_bytes", rss)
    _add(samples, "redis.memory_peak_bytes", peak)
    _add(samples, "redis.memory_dataset_bytes", dataset)
    _add(samples, "redis.memory_max_bytes", maxmemory)
    _add(samples, "redis.mem_fragmentation_ratio", as_float(flat.get("mem_fragmentation_ratio")))

    if maxmemory and used is not None and maxmemory > 0:
        _add(samples, "redis.memory_usage_percent", percent(used, maxmemory))


def _collect_clients(flat: dict, samples: list[Sample]) -> None:
    _add(samples, "redis.connected_clients", as_float(flat.get("connected_clients")))
    _add(samples, "redis.blocked_clients", as_float(flat.get("blocked_clients")))
    # rejected_connections 是累计值：非 0 通常意味着 maxclients 顶到上限
    _add(
        samples,
        "redis.rejected_connections_total",
        as_float(flat.get("rejected_connections")),
    )


def _collect_hit_rate(flat: dict, samples: list[Sample]) -> None:
    """命中率与累计计数器。

    Redis 自带 ``instantaneous_ops_per_sec``（服务端算好的瞬时值），
    直接用它而不去对 ``total_commands_processed`` 求差——引擎自己更清楚
    它想要的时间窗。
    """
    hits = as_float(flat.get("keyspace_hits"))
    misses = as_float(flat.get("keyspace_misses"))
    _add(samples, "redis.keyspace_hits_total", hits)
    _add(samples, "redis.keyspace_misses_total", misses)
    if hits is not None and misses is not None and (hits + misses) > 0:
        _add(samples, "redis.cache_hit_percent", percent(hits, hits + misses))
    _add(
        samples,
        "redis.ops_per_sec",
        as_float(flat.get("instantaneous_ops_per_sec")),
    )
    _add(
        samples,
        "redis.input_kbps",
        as_float(flat.get("instantaneous_input_kbps")),
    )
    _add(
        samples,
        "redis.output_kbps",
        as_float(flat.get("instantaneous_output_kbps")),
    )


def _collect_counters(flat: dict, samples: list[Sample]) -> None:
    _add(
        samples,
        "redis.commands_processed_total",
        as_float(flat.get("total_commands_processed")),
    )
    _add(
        samples,
        "redis.connections_received_total",
        as_float(flat.get("total_connections_received")),
    )
    # 淘汰与过期是内存压力的直接信号
    _add(samples, "redis.evicted_keys_total", as_float(flat.get("evicted_keys")))
    _add(samples, "redis.expired_keys_total", as_float(flat.get("expired_keys")))


def _collect_persistence(flat: dict, samples: list[Sample]) -> None:
    """持久化状态。用 1/0 表示成功/失败，便于直接做阈值告警。"""
    rdb_status = str(flat.get("rdb_last_bgsave_status") or "").lower()
    if rdb_status:
        _add(samples, "redis.rdb_last_bgsave_ok", 1.0 if rdb_status == "ok" else 0.0)
    _add(
        samples,
        "redis.rdb_last_bgsave_time_seconds",
        as_float(flat.get("rdb_last_bgsave_time_sec")),
    )
    aof_status = str(flat.get("aof_last_bgrewrite_status") or "").lower()
    if aof_status:
        _add(samples, "redis.aof_last_bgrewrite_ok", 1.0 if aof_status == "ok" else 0.0)
    _add(
        samples,
        "redis.aof_current_size_bytes",
        as_float(flat.get("aof_current_size")),
    )


def _collect_keyspace(flat: dict, samples: list[Sample]) -> None:
    """按 db 统计 key 数。

    INFO 的 keyspace 段形如 ``db0: keys=123,expires=10,avg_ttl=0``，
    redis-py 已解析成 dict 值。这里逐 db 落一行。
    """
    total_keys = 0.0
    for key, raw in flat.items():
        if not key.startswith("db") or not isinstance(raw, dict):
            continue
        keys = as_float(raw.get("keys"))
        if keys is None:
            continue
        total_keys += keys
        _add(samples, "redis.database_keys", keys, dimension=key[:200])
        _add(
            samples,
            "redis.database_expires",
            as_float(raw.get("expires")),
            dimension=key[:200],
        )
    if total_keys:
        _add(samples, "redis.total_keys", total_keys)


def _collect_replication(flat: dict, samples: list[Sample]) -> None:
    role = str(flat.get("role") or "").lower()
    if not role:
        return
    _add(samples, "redis.role_master", 1.0 if role == "master" else 0.0)
    _add(samples, "redis.connected_slaves", as_float(flat.get("connected_slaves")))

    if role == "slave":
        link = str(flat.get("master_link_status") or "").lower()
        _add(samples, "redis.master_link_up", 1.0 if link == "up" else 0.0)
        # slave_repl_offset 与 master_repl_offset 的差值需要主库数据才能算，
        # 这里只落自身偏移量，供跨实例对比判断落后
        _add(
            samples,
            "redis.slave_repl_offset",
            as_float(flat.get("slave_repl_offset") or flat.get("replica_repl_offset")),
        )
    else:
        _add(samples, "redis.master_repl_offset", as_float(flat.get("master_repl_offset")))


def _collect_slowlog(client, samples: list[Sample]) -> None:
    """慢日志条数。

    只取 ``SLOWLOG LEN``，不用 ``SLOWLOG GET``：后者会返回命令原文，
    可能含业务数据甚至凭据，落进监控库等于二次泄露。要看具体慢命令，
    应当到实例上按需查询（控制台已提供）。
    """
    try:
        length = client.execute_command("SLOWLOG", "LEN")
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return
    _add(samples, "redis.slowlog_length", as_float(length))


def _collect_top_keys(client, samples: list[Sample]) -> None:
    """SCAN 采样，产出「采样范围内内存最大的若干个 key」。

    两步 pipeline，共 2 次网络往返：
    1. SCAN 一批 key（单次 cursor 迭代，count 由服务端决定）
    2. 批量 TYPE + MEMORY USAGE

    任何一步失败都只跳过该指标，不影响其它采集——Top Key 是锦上添花，
    不该让核心指标一起丢掉。
    """
    keys: list[str] = []
    try:
        cursor = 0
        while True:
            cursor, batch = client.scan(cursor=cursor, count=100)
            keys.extend(batch)
            if cursor == 0 or len(keys) >= DEFAULT_SCAN_LIMIT:
                break
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return

    keys = keys[:DEFAULT_SCAN_LIMIT]
    if not keys:
        return

    # 类型分布：直接回答"这个实例里都是什么数据结构"
    type_counts: dict[str, int] = {}
    sizes: list[tuple[str, float]] = []
    try:
        pipe = client.pipeline(transaction=False)
        for key in keys:
            pipe.type(key)
        types = pipe.execute()

        pipe = client.pipeline(transaction=False)
        for key in keys:
            pipe.memory_usage(key)
        usages = pipe.execute()
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return

    for key, key_type, usage in zip(keys, types, usages):
        name = str(key_type)
        type_counts[name] = type_counts.get(name, 0) + 1
        size = as_float(usage)
        if size is not None:
            sizes.append((str(key), size))

    for name, count in sorted(type_counts.items()):
        _add(samples, "redis.sampled_key_type_count", float(count), dimension=name[:200])

    sizes.sort(key=lambda item: item[1], reverse=True)
    for key, size in sizes[:TOP_KEY_KEEP]:
        _add(
            samples,
            "redis.sampled_biggest_key_bytes",
            size,
            dimension=key[:KEY_NAME_MAX],
        )
    _add(samples, "redis.sampled_key_count", float(len(keys)))


def _safe_reason(exc: BaseException) -> str:
    return f"采集失败：{type(exc).__name__}"
