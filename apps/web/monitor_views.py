"""监控页面视图（集群维度）。

单独成文而不是继续堆进 views.py：那个文件已有 1100 多行，五类资源
（部门/集群/实例/凭据/统计）的视图挤在一起，再加十几个监控页会让
定位成本失控。这里只依赖 monitor 自己的查询层，与业务视图无耦合。
"""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from apps.assets.models import Cluster, Instance
from apps.monitor.queries import latest_values
from apps.monitor.services import latest_run_map

# 各引擎在概览页展示的核心指标。键名与采集器产出一一对应，
# 顺序即展示顺序：先看"活没活着"，再看"忙不忙"，最后看"稳不稳"。
OVERVIEW_METRICS = {
    "mysql": [
        ("mysql.connections", "连接数", ""),
        ("mysql.connection_usage_percent", "连接使用率", "%"),
        ("mysql.qps", "QPS", "次/秒"),
        ("mysql.buffer_pool_hit_percent", "缓冲池命中率", "%"),
        ("mysql.slow_queries_per_sec", "慢查询", "次/秒"),
        ("mysql.replication_delay_seconds", "复制延迟", "秒"),
        ("mysql.total_size_bytes", "数据总量", "bytes"),
    ],
    "postgresql": [
        ("pg.connections", "连接数", ""),
        ("pg.connection_usage_percent", "连接使用率", "%"),
        ("pg.tps", "TPS", "次/秒"),
        ("pg.cache_hit_percent", "缓存命中率", "%"),
        ("pg.longest_transaction_seconds", "最长事务", "秒"),
        ("pg.replication_lag_seconds", "复制延迟", "秒"),
        ("pg.total_size_bytes", "数据总量", "bytes"),
    ],
    "redis": [
        ("redis.connected_clients", "客户端连接", ""),
        ("redis.memory_used_bytes", "已用内存", "bytes"),
        ("redis.memory_usage_percent", "内存使用率", "%"),
        ("redis.ops_per_sec", "OPS", "次/秒"),
        ("redis.cache_hit_percent", "命中率", "%"),
        ("redis.total_keys", "键总数", ""),
        ("redis.mem_fragmentation_ratio", "内存碎片率", ""),
    ],
}

# 空间明细的指标键：MySQL/PG 按库维度落，Redis 没有"库空间"这个概念
SPACE_METRICS = {
    "mysql": "mysql.database_size_bytes",
    "postgresql": "pg.database_size_bytes",
    "redis": "redis.memory_used_bytes",
}


def _metric_specs(engine: str) -> list[tuple[str, str, str]]:
    """取引擎的指标清单。

    未知引擎（mongodb/other）返回空表而不是报错：集群可以先建后补采集，
    页面应当照常打开，只是没有数据。
    """
    return OVERVIEW_METRICS.get(engine, [])


def _setting(name: str, default):
    from django.conf import settings

    return getattr(settings, name, default)


def _instance_metric_map(instance_ids: list[int], engine: str) -> dict[int, list[dict]]:
    """按引擎的指标规格，为每个实例整理出**有序列表**。

    刻意返回 list 而不是 dict：Django 模板无法用变量作字典键
    （``row.metrics[key]`` 里的 key 会被当成字面量），转成列表后
    模板直接 ``{% for cell in row.metrics %}`` 即可，无需自定义过滤器。

    每个元素形如 ``{"key", "label", "unit", "value", "collected_at"}``；
    没有采到该指标时 ``value`` 为 None，模板据此显示"—"而不是 0，
    避免把"没数据"误读成"指标为 0"。
    """
    specs = _metric_specs(engine)
    if not specs or not instance_ids:
        return {}

    keys = [spec[0] for spec in specs]
    raw = latest_values(instance_ids, keys)

    result: dict[int, list[dict]] = {instance_id: [] for instance_id in instance_ids}
    for metric, label, unit in specs:
        per_instance = raw.get(metric, {})
        for instance_id in instance_ids:
            item = per_instance.get(instance_id)
            result[instance_id].append(
                {
                    "key": metric,
                    "label": label,
                    "unit": unit,
                    "value": item["v"] if item else None,
                    "collected_at": item["t"] if item else "",
                }
            )
    return result


def cluster_overview(request: HttpRequest, cluster_id: int) -> HttpResponse:
    """集群概览：成员实例的实时核心指标 + 采集健康度。

    这是"打开一个集群先看什么"的答案页：谁在线、谁在拖后腿、
    数据是不是新鲜的。深入分析交给趋势/空间/会话等子页面。
    """
    cluster = get_object_or_404(
        Cluster.objects.select_related("department"), id=cluster_id
    )
    instances = list(cluster.instances.order_by("role", "name"))
    instance_ids = [inst.pk for inst in instances]
    runs = latest_run_map(instance_ids)
    metrics = _instance_metric_map(instance_ids, cluster.engine)

    rows = []
    for inst in instances:
        run = runs.get(inst.pk)
        rows.append(
            {
                "instance": inst,
                "metrics": metrics.get(inst.pk, []),
                "run": run,
                "stale": run is None,
            }
        )

    # 采集健康度：有记录且最近一次成功才算健康。没记录不等于失败——
    # 可能是刚建的实例，页面用"尚无数据"区分，不用红色吓人。
    collected = [row for row in rows if row["run"] is not None]
    healthy = [row for row in collected if row["run"].ok]

    return render(
        request,
        "web/monitor/cluster_overview.html",
        {
            "active_nav": "clusters",
            "cluster": cluster,
            "rows": rows,
            "specs": _metric_specs(cluster.engine),
            "has_metrics": bool(_metric_specs(cluster.engine)),
            "interval_minutes": int(_setting("MONITOR_INTERVAL_MINUTES", 1)),
            "summary": {
                "total": len(rows),
                "collected": len(collected),
                "healthy": len(healthy),
                "failing": len(collected) - len(healthy),
            },
        },
    )


def cluster_space(request: HttpRequest, cluster_id: int) -> HttpResponse:
    """集群空间：按库维度汇总各实例的占用。

    只展示**最新一次采样**的分库数据，不做历史趋势——历史趋势
    属于"性能趋势"页的职责，这里回答"现在谁占得多"。
    """
    cluster = get_object_or_404(
        Cluster.objects.select_related("department"), id=cluster_id
    )
    instances = list(cluster.instances.order_by("role", "name"))
    instance_ids = [inst.pk for inst in instances]
    metric = SPACE_METRICS.get(cluster.engine, "")

    databases: list[dict] = []
    totals = {"instance_sum": 0.0, "databases": 0}
    if metric and instance_ids:
        from apps.monitor.models import MetricSample

        # 只取每实例该指标的最新 collected_at 那批，避免把历史快照累加。
        # Redis 无 dimension，单独处理成一条"整体内存"。
        rows = (
            MetricSample.objects.filter(
                instance_id__in=instance_ids, metric=metric
            )
            .order_by("-collected_at")
            .values_list("instance_id", "dimension", "value", "collected_at")
        )
        latest_stamp: dict[int, object] = {}
        for instance_id, _dimension, _value, collected_at in rows:
            latest_stamp.setdefault(instance_id, collected_at)

        seen: set[tuple[int, str]] = set()
        for instance_id, dimension, value, collected_at in rows:
            if collected_at != latest_stamp.get(instance_id):
                continue
            key = (instance_id, dimension or "")
            if key in seen:
                continue
            seen.add(key)
            databases.append(
                {
                    "instance_id": instance_id,
                    "instance_name": next(
                        (inst.name for inst in instances if inst.pk == instance_id), ""
                    ),
                    "database": dimension or "（整体）",
                    "size": float(value),
                }
            )

        databases.sort(key=lambda row: row["size"], reverse=True)
        totals["instance_sum"] = sum(row["size"] for row in databases)
        totals["databases"] = len({row["database"] for row in databases})

    return render(
        request,
        "web/monitor/cluster_space.html",
        {
            "active_nav": "clusters",
            "cluster": cluster,
            "instances": instances,
            "databases": databases,
            "totals": totals,
            "space_supported": bool(metric),
        },
    )


def monitor_dashboard(request: HttpRequest) -> HttpResponse:
    """监控大盘：一屏看清"谁在报错、谁数据不新鲜、各引擎规模"。

    与总览页（资产家底）不同，这里只回答**观测层面**的问题。两者分开是因为
    运维的两种动作不同：资产页用来"加/改/删"，大盘用来"看有没有事"。
    """
    from apps.monitor.models import CollectionRun

    instances = list(
        Instance.objects.filter(is_active=True)
        .select_related("cluster", "cluster__department")
        .order_by("cluster__name", "name")
    )
    instance_ids = [inst.pk for inst in instances]
    runs = latest_run_map(instance_ids)

    # 按引擎汇总：每个引擎多少实例、多少在报错
    engines: dict[str, dict] = {}
    for inst in instances:
        engine = inst.cluster.engine
        slot = engines.setdefault(
            engine,
            {"engine": engine, "total": 0, "ok": 0, "failed": 0, "no_data": 0},
        )
        slot["total"] += 1
        run = runs.get(inst.pk)
        if run is None:
            slot["no_data"] += 1
        elif run.ok:
            slot["ok"] += 1
        else:
            slot["failed"] += 1

    # 只列有问题的实例，正常的不用占屏幕
    problems = []
    for inst in instances:
        run = runs.get(inst.pk)
        if run is not None and run.ok:
            continue
        problems.append({"instance": inst, "run": run})

    # 最近的采集记录：看"采集任务本身还活着吗"
    recent_runs = list(
        CollectionRun.objects.select_related("instance").order_by(
            "-collected_at", "-id"
        )[:15]
    )

    totals = {
        "instances": len(instances),
        "ok": sum(1 for inst in instances if runs.get(inst.pk) and runs[inst.pk].ok),
        "failed": sum(
            1 for inst in instances if runs.get(inst.pk) and not runs[inst.pk].ok
        ),
        "no_data": sum(1 for inst in instances if runs.get(inst.pk) is None),
    }

    return render(
        request,
        "web/monitor/dashboard.html",
        {
            "active_nav": "monitor",
            "totals": totals,
            "engines": sorted(engines.values(), key=lambda row: row["engine"]),
            "problems": problems,
            "recent_runs": recent_runs,
            "interval_minutes": int(_setting("MONITOR_INTERVAL_MINUTES", 1)),
            "collect_enabled": bool(_setting("MONITOR_COLLECT_ENABLED", True)),
        },
    )


def instance_profile(request: HttpRequest, instance_id: int) -> HttpResponse:
    """实例画像：把散落在各页的信息收在一页，回答"这台实例整体什么状况"。

    只读聚合，不做任何实时查询——打开画像页不应该给目标库添压力。
    需要实时数据时，页面提供跳转到会话/慢日志页的入口。
    """
    instance = get_object_or_404(
        Instance.objects.select_related("cluster", "cluster__department", "replica_of"),
        id=instance_id,
    )
    specs = _metric_specs(instance.cluster.engine)
    keys = [spec[0] for spec in specs]
    raw = latest_values([instance.pk], keys) if keys else {}

    cells = []
    for key, label, unit in specs:
        item = raw.get(key, {}).get(instance.pk)
        cells.append(
            {
                "label": label,
                "unit": unit,
                "value": item["v"] if item else None,
                "collected_at": item["t"] if item else "",
            }
        )

    runs = latest_run_map([instance.pk])
    siblings = (
        instance.cluster.instances.exclude(pk=instance.pk)
        .order_by("role", "name")[:10]
    )

    return render(
        request,
        "web/monitor/instance_profile.html",
        {
            "active_nav": "instances",
            "instance": instance,
            "run": runs.get(instance.pk),
            "cells": cells,
            "has_metrics": bool(specs),
            "siblings": siblings,
        },
    )


def sql_insight(request: HttpRequest, instance_id: int | None = None) -> HttpResponse:
    """SQL 洞察：按总耗时排序的语句模板聚合。

    支持两种视角：
    - 不传实例：全库 TOP（跨实例横向比，回答"哪个实例最吃力"）
    - 传实例：单实例 TOP（纵向看，回答"这台机器上哪条 SQL 最贵"）

    ``sort`` 可切总耗时 / 平均耗时 / 执行次数：只按总耗时排会让"每秒跑一万次
    的快查询"永远排第一，而真正要优化的是偶尔跑一次就扫全表的那个。
    """
    from apps.monitor.models import SqlDigest

    sort = (request.GET.get("sort") or "total").strip()
    sort_field = {
        "total": "-total_time_ms",
        "avg": "-avg_time_ms",
        "count": "-exec_count",
    }.get(sort, "-total_time_ms")

    qs = SqlDigest.objects.select_related("instance", "instance__cluster")
    instance = None
    if instance_id is not None:
        instance = get_object_or_404(
            Instance.objects.select_related("cluster"), id=instance_id
        )
        qs = qs.filter(instance=instance)

    rows = list(qs.order_by(sort_field, "-id")[:100])

    return render(
        request,
        "web/monitor/sql_insight.html",
        {
            "active_nav": "instances" if instance else "monitor",
            "instance": instance,
            "rows": rows,
            "sort": sort,
            "total_count": qs.count() if instance else SqlDigest.objects.count(),
        },
    )


def lock_analysis(request: HttpRequest, instance_id: int | None = None) -> HttpResponse:
    """锁分析：当前锁等待关系 + 死锁历史。

    两个视角：不传实例看全库（哪个实例最堵），传实例看单机（谁在等谁）。
    锁等待是**瞬时状态**（采集每轮清空重写），因此这里只展示"此刻"；
    死锁是历史事件，单独一张表按时间倒序。
    """
    from apps.monitor.models import DeadlockEvent, LockWait

    instance = None
    waits_qs = LockWait.objects.select_related("instance")
    deadlocks_qs = DeadlockEvent.objects.select_related("instance")

    if instance_id is not None:
        instance = get_object_or_404(
            Instance.objects.select_related("cluster"), id=instance_id
        )
        waits_qs = waits_qs.filter(instance=instance)
        deadlocks_qs = deadlocks_qs.filter(instance=instance)

    waits = list(waits_qs.order_by("-wait_seconds", "-id")[:200])
    deadlocks = list(deadlocks_qs.order_by("-detected_at", "-id")[:50])

    return render(
        request,
        "web/monitor/lock_analysis.html",
        {
            "active_nav": "instances" if instance else "monitor",
            "instance": instance,
            "waits": waits,
            "deadlocks": deadlocks,
            "has_waiters": bool(waits),
            "lock_interval": int(_setting("MONITOR_LOCK_INTERVAL_MINUTES", 5)),
            "lock_supported": (
                instance.cluster.engine in ("mysql", "postgresql")
                if instance
                else True
            ),
        },
    )


def inspect_overview(request: HttpRequest) -> HttpResponse:
    """巡检评分总览：全部启用实例的健康分排行。

    按分数升序：最该修的排最前面。这是"巡检"页面存在的唯一理由——
    不是把数据摆出来，而是给出**处理顺序**。
    """
    from apps.monitor.reports import build_report

    instances = list(
        Instance.objects.filter(is_active=True)
        .select_related("cluster", "cluster__department")
        .order_by("cluster__name", "name")
    )
    # 报告本身只带 instance_id/name/engine，页面还要显示所属集群与部门，
    # 因此在这里把实例对象一并带上——模板不能反查（那会变成 N 次查询）。
    rows = [{"report": build_report(inst), "instance": inst} for inst in instances]
    # 数据不足的排在最后：它们还不能下结论，不该挤占处理优先级
    rows.sort(key=lambda row: (row["report"].insufficient, row["report"].score))

    reports = [row["report"] for row in rows]
    scored = [r for r in reports if not r.insufficient]
    summary = {
        "total": len(reports),
        "average": round(sum(r.score for r in scored) / len(scored), 1) if scored else 0.0,
        "poor": sum(1 for r in scored if r.score < 60),
        "insufficient": sum(1 for r in reports if r.insufficient),
    }

    return render(
        request,
        "web/monitor/inspect_overview.html",
        {
            "active_nav": "monitor",
            "rows": rows,
            "summary": summary,
        },
    )


def inspect_report(request: HttpRequest, instance_id: int) -> HttpResponse:
    """单实例诊断报告。

    报告只读、不连目标库，因此可随时刷新。页面把"扣分项"单独置顶：
    运维打开报告是为了找问题，不是为了看满分项。
    """
    from apps.monitor.reports import REPORT_METRICS, build_report

    instance = get_object_or_404(
        Instance.objects.select_related("cluster", "cluster__department", "replica_of"),
        id=instance_id,
    )
    report = build_report(instance)

    problems = [r for r in report.results if r.status in ("fail", "warn")]
    passed = [r for r in report.results if r.status == "ok"]
    unknown = [r for r in report.results if r.status == "unknown"]

    return render(
        request,
        "web/monitor/inspect_report.html",
        {
            "active_nav": "instances",
            "instance": instance,
            "report": report,
            "problems": problems,
            "passed": passed,
            "unknown": unknown,
            "metrics": REPORT_METRICS.get(instance.cluster.engine, []),
            "generated_at": timezone.now(),
        },
    )


def cluster_report(request: HttpRequest, cluster_id: int) -> HttpResponse:
    """集群诊断报告：成员实例的评分汇总。"""
    from apps.monitor.reports import build_cluster_report

    cluster = get_object_or_404(
        Cluster.objects.select_related("department"), id=cluster_id
    )
    payload = build_cluster_report(cluster)

    return render(
        request,
        "web/monitor/cluster_report.html",
        {
            "active_nav": "clusters",
            **payload,
        },
    )
