"""指标目录：指标键 → 中文名。

为什么集中声明
--------------
指标键（``mysql.qps``）会出现在四个地方：采集器产出、趋势页选图、告警规则
配置、API 目录输出。散落在各处时，改一次采集口径就要满仓库找；集中到
这里后，"系统支持哪些指标"只有一个答案。

这里只放**给人看的清单**（键 + 中文名 + 单位），不放引擎差异逻辑——
采集口径在 ``apps.monitor.collectors``，两者职责不同。
"""

from __future__ import annotations

__all__ = ["METRIC_CATALOG", "METRIC_LABELS", "metric_choices", "metrics_for"]

# engine -> [(指标键, 中文名, 单位)]
METRIC_CATALOG: dict[str, list[tuple[str, str, str]]] = {
    "mysql": [
        ("mysql.connections", "当前连接数", ""),
        ("mysql.connection_usage_percent", "连接使用率", "%"),
        ("mysql.qps", "QPS", "次/秒"),
        ("mysql.slow_queries_per_sec", "慢查询速率", "次/秒"),
        ("mysql.buffer_pool_hit_percent", "缓冲池命中率", "%"),
        ("mysql.threads_running", "活跃线程数", ""),
        ("mysql.replication_delay_seconds", "复制延迟", "秒"),
        ("mysql.total_size_bytes", "数据总量", "bytes"),
    ],
    "postgresql": [
        ("pg.connections", "当前连接数", ""),
        ("pg.connection_usage_percent", "连接使用率", "%"),
        ("pg.tps", "TPS", "次/秒"),
        ("pg.rollback_percent", "回滚比例", "%"),
        ("pg.cache_hit_percent", "缓存命中率", "%"),
        ("pg.connections_active", "活跃连接", ""),
        ("pg.longest_transaction_seconds", "最长事务时长", "秒"),
        ("pg.replication_lag_seconds", "复制延迟", "秒"),
        ("pg.total_size_bytes", "数据总量", "bytes"),
    ],
    "redis": [
        ("redis.connected_clients", "客户端连接数", ""),
        ("redis.memory_used_bytes", "已用内存", "bytes"),
        ("redis.memory_usage_percent", "内存使用率", "%"),
        ("redis.ops_per_sec", "OPS", "次/秒"),
        ("redis.cache_hit_percent", "命中率", "%"),
        ("redis.mem_fragmentation_ratio", "内存碎片率", ""),
        ("redis.total_keys", "键总数", ""),
        ("redis.blocked_clients", "阻塞客户端数", ""),
    ],
}

# 扁平索引：指标键 -> 中文名。告警事件与页面只拿到键，需要反查展示名。
METRIC_LABELS: dict[str, str] = {
    key: label
    for entries in METRIC_CATALOG.values()
    for key, label, _unit in entries
}


def metrics_for(engine: str) -> list[tuple[str, str, str]]:
    """取某引擎的指标清单；未知引擎返回空表（页面据此提示"不支持"）。"""
    return METRIC_CATALOG.get(engine, [])


def metric_choices(include_unknown: bool = False) -> list[tuple[str, str]]:
    """给下拉框用的 (值, 展示名) 列表。

    默认按引擎分组加前缀，让运维在下拉里能看出"这是哪个引擎的指标"，
    不必先记住 mysql./pg./redis. 的区别。

    已存在但不在目录里的指标键（例如旧规则指向已下线的指标）通过
    ``include_unknown`` 由调用方补入，否则编辑旧规则时下拉会"选不中"，
    一变即改，等于悄悄改掉了规则。
    """
    choices: list[tuple[str, str]] = []
    for engine, entries in METRIC_CATALOG.items():
        for key, label, unit in entries:
            suffix = f"（{unit}）" if unit and unit != "bytes" else ""
            choices.append((key, f"{engine} · {label}{suffix}"))
    if include_unknown:
        return choices
    return choices
