"""实例监控页面视图：性能趋势 / 实时性能 / 空间 / 会话 / 慢日志。

与 ``monitor_views``（集群维度）分开：这里每个页面都对应一次
**对目标库的实时查询**，错误处理与降级方式完全不同，混在一起会让
"哪段代码会真的连目标库"变得难以审查。
"""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.assets.models import Instance
from apps.dbconsole.services import execute_statement
from apps.monitor.queries import latest_values
from apps.monitor.services import latest_run_map

# 每个引擎的趋势指标清单：(指标键, 显示名, 单位)
TREND_METRICS = {
    "mysql": [
        ("mysql.qps", "QPS", "次/秒"),
        ("mysql.connections", "连接数", ""),
        ("mysql.buffer_pool_hit_percent", "缓冲池命中率", "%"),
        ("mysql.slow_queries_per_sec", "慢查询", "次/秒"),
        ("mysql.threads_running", "活跃线程", ""),
        ("mysql.total_size_bytes", "数据总量", "bytes"),
    ],
    "postgresql": [
        ("pg.tps", "TPS", "次/秒"),
        ("pg.connections", "连接数", ""),
        ("pg.cache_hit_percent", "缓存命中率", "%"),
        ("pg.connections_active", "活跃连接", ""),
        ("pg.longest_transaction_seconds", "最长事务", "秒"),
        ("pg.total_size_bytes", "数据总量", "bytes"),
    ],
    "redis": [
        ("redis.ops_per_sec", "OPS", "次/秒"),
        ("redis.connected_clients", "客户端连接", ""),
        ("redis.memory_used_bytes", "已用内存", "bytes"),
        ("redis.cache_hit_percent", "命中率", "%"),
        ("redis.mem_fragmentation_ratio", "内存碎片率", ""),
        ("redis.total_keys", "键总数", ""),
    ],
}

# 会话查询：各引擎的"当前会话"快照语句。
# 都带 LIMIT，避免目标库连接数上万时把整个结果集拉回来。
SESSION_SQL = {
    "mysql": (
        "SELECT id, user, host, db, command, time, state, LEFT(info, 200) AS info "
        "FROM information_schema.PROCESSLIST ORDER BY time DESC LIMIT 200"
    ),
    "postgresql": (
        "SELECT pid, usename, client_addr, datname, state, "
        "CAST(EXTRACT(EPOCH FROM (now() - query_start)) AS INTEGER) AS duration, "
        "LEFT(query, 200) AS query "
        "FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
        "ORDER BY duration DESC NULLS LAST LIMIT 200"
    ),
    "redis": "CLIENT LIST",
}

# 慢日志：MySQL 用 performance_schema 的 digest 汇总（比重开 slow_query_log 现实），
# PG 依赖 pg_stat_statements 扩展，Redis 用 SLOWLOG。缺失时给出可执行的提示。
SLOW_SQL = {
    "mysql": (
        "SELECT DIGEST_TEXT AS sql_text, COUNT_STAR AS exec_count, "
        "ROUND(SUM_TIMER_WAIT/1000000000000, 3) AS total_sec, "
        "ROUND(AVG_TIMER_WAIT/1000000000, 3) AS avg_ms "
        "FROM performance_schema.events_statements_summary_by_digest "
        "ORDER BY SUM_TIMER_WAIT DESC LIMIT 50"
    ),
    "postgresql": (
        "SELECT LEFT(query, 300) AS sql_text, calls AS exec_count, "
        "ROUND(total_exec_time/1000, 3) AS total_sec, "
        "ROUND(mean_exec_time, 3) AS avg_ms "
        "FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 50"
    ),
    "redis": "SLOWLOG GET 50",
}


def _trend_specs(engine: str) -> list[dict]:
    return [
        {"key": key, "label": label, "unit": unit}
        for key, label, unit in TREND_METRICS.get(engine, [])
    ]


def _run_query(instance: Instance, sql: str) -> dict:
    """执行一条诊断查询，把各种失败收敛成统一结构。

    页面刻意**不因目标库不可达而 500**：运维打开"会话"页时目标库
    恰好挂了，正是最需要看到"连不上、原因是什么"的时候。
    """
    timeout = int(getattr(settings, "MONITOR_TIMEOUT_SECONDS", 5))
    result = execute_statement(instance, sql, timeout=timeout, user_label="monitor")

    if not result.ok:
        return {
            "ok": False,
            "error": result.error,
            "message": result.message,
            "hint": result.hint,
            "columns": [],
            "rows": [],
        }

    return {
        "ok": True,
        "error": "",
        "message": "",
        "hint": "",
        "columns": list(result.columns),
        "rows": [list(row) for row in result.rows],
        "truncated": result.truncated,
    }


def instance_trend(request: HttpRequest, instance_id: int) -> HttpResponse:
    """性能趋势：多指标折线 + 时间窗切换。

    取数走 /api/v1/monitor/series（同一套查询层），页面只负责把
    指标清单和窗口参数交给前端，避免页面与接口各算一套。
    """
    instance = get_object_or_404(
        Instance.objects.select_related("cluster"), id=instance_id
    )
    runs = latest_run_map([instance.pk])

    return render(
        request,
        "web/monitor/instance_trend.html",
        {
            "active_nav": "instances",
            "instance": instance,
            "run": runs.get(instance.pk),
            "specs": _trend_specs(instance.cluster.engine),
            "engine": instance.cluster.engine,
            "hours": int(request.GET.get("hours", 6) or 6),
        },
    )


def instance_realtime(request: HttpRequest, instance_id: int) -> HttpResponse:
    """实时性能：最近一次采集的全部指标 + 会话数概览。

    不做"每秒刷新"的 SSE/轮询：采集周期是分钟级，前端轮询也拿不到
    更新鲜的数据，反而制造无意义的请求。页面用"刷新"按钮由人触发。
    """
    instance = get_object_or_404(
        Instance.objects.select_related("cluster"), id=instance_id
    )
    specs = _trend_specs(instance.cluster.engine)
    keys = [spec["key"] for spec in specs]
    raw = latest_values([instance.pk], keys)

    cells = []
    for spec in specs:
        item = raw.get(spec["key"], {}).get(instance.pk)
        cells.append(
            {
                "label": spec["label"],
                "unit": spec["unit"],
                "value": item["v"] if item else None,
                "collected_at": item["t"] if item else "",
            }
        )

    runs = latest_run_map([instance.pk])
    return render(
        request,
        "web/monitor/instance_realtime.html",
        {
            "active_nav": "instances",
            "instance": instance,
            "run": runs.get(instance.pk),
            "cells": cells,
            "interval_minutes": int(getattr(settings, "MONITOR_INTERVAL_MINUTES", 1)),
        },
    )


def instance_space(request: HttpRequest, instance_id: int) -> HttpResponse:
    """实例空间：按库维度看当前占用（单实例视角）。"""
    from apps.monitor.models import MetricSample

    instance = get_object_or_404(
        Instance.objects.select_related("cluster"), id=instance_id
    )
    metric = {
        "mysql": "mysql.database_size_bytes",
        "postgresql": "pg.database_size_bytes",
        "redis": "redis.memory_used_bytes",
    }.get(instance.cluster.engine, "")

    databases: list[dict] = []
    total = 0.0
    if metric:
        # 只取最新一个采样时刻：历史快照累加会得出翻倍的数字
        rows = (
            MetricSample.objects.filter(instance=instance, metric=metric)
            .order_by("-collected_at")
            .values_list("dimension", "value", "collected_at")
        )
        latest_stamp = None
        for _dimension, _value, collected_at in rows:
            latest_stamp = collected_at
            break

        seen: set[str] = set()
        if latest_stamp is not None:
            for dimension, value, collected_at in rows:
                if collected_at != latest_stamp:
                    continue
                name = dimension or "（整体）"
                if name in seen:
                    continue
                seen.add(name)
                databases.append({"database": name, "size": float(value)})

        databases.sort(key=lambda row: row["size"], reverse=True)
        total = sum(row["size"] for row in databases)

    return render(
        request,
        "web/monitor/instance_space.html",
        {
            "active_nav": "instances",
            "instance": instance,
            "databases": databases,
            "total": total,
            "space_supported": bool(metric),
        },
    )


def instance_sessions(request: HttpRequest, instance_id: int) -> HttpResponse:
    """实例会话：实时查询目标库的当前会话。

    这是本项目里少数**打开页面就会连目标库**的只读页。仍然免登录，
    与其它只读页一致；查询结果只读，且语句固定（不接受用户输入），
    不构成注入面。
    """
    instance = get_object_or_404(
        Instance.objects.select_related("cluster"), id=instance_id
    )
    sql = SESSION_SQL.get(instance.cluster.engine)

    payload = (
        _run_query(instance, sql)
        if sql
        else {
            "ok": False,
            "error": "unsupported_engine",
            "message": "该引擎暂不支持会话查看",
            "hint": "",
            "columns": [],
            "rows": [],
        }
    )

    return render(
        request,
        "web/monitor/instance_sessions.html",
        {
            "active_nav": "instances",
            "instance": instance,
            "result": payload,
            "checked_at": timezone.now(),
        },
    )


def instance_slowlog(request: HttpRequest, instance_id: int) -> HttpResponse:
    """慢日志：按总耗时排序的慢 SQL 汇总。

    MySQL 走 performance_schema、PG 走 pg_stat_statements、Redis 走 SLOWLOG。
    前两者在实例上默认未开启，因此失败提示要写清"去开什么"，
    否则运维只会看到一个红框。
    """
    instance = get_object_or_404(
        Instance.objects.select_related("cluster"), id=instance_id
    )
    sql = SLOW_SQL.get(instance.cluster.engine)
    payload = (
        _run_query(instance, sql)
        if sql
        else {
            "ok": False,
            "error": "unsupported_engine",
            "message": "该引擎暂不支持慢日志采集",
            "hint": "",
            "columns": [],
            "rows": [],
        }
    )

    return render(
        request,
        "web/monitor/instance_slowlog.html",
        {
            "active_nav": "instances",
            "instance": instance,
            "result": payload,
            "enable_hint": {
                "mysql": "MySQL 需开启 performance_schema（默认开启），"
                "且账号需有 SELECT 权限",
                "postgresql": "PostgreSQL 需先安装扩展并开启 pg_stat_statements："
                "在 postgresql.conf 的 shared_preload_libraries 中加入 "
                "pg_stat_statements 后重启，再执行 CREATE EXTENSION pg_stat_statements;",
                "redis": "Redis 需配置 slowlog-log-slower-than（默认 10000 微秒）",
            }.get(instance.cluster.engine, ""),
        },
    )
