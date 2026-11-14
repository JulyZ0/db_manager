"""诊断报告：把分散在各表里的证据汇总成一次可读的巡检结论。

为什么单独一层
--------------
``inspect.py`` 是纯函数（只判断），本模块负责**取数**：指标最新值、采集
状态、告警、锁等待。分开后评分规则可以脱离数据库测试，而取数逻辑的
SQL 变化也不会牵动评分口径。

报告刻意不复用页面视图里的查询：页面是"打开就看"的，报告需要一份**稳定、
可导出**的快照。两者对数据新鲜度的要求不同，硬合并会让报告随页面改版而变。
"""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.assets.models import Instance
from apps.monitor.inspect import InspectReport, inspect_instance
from apps.monitor.queries import latest_values
from apps.monitor.services import latest_run_map

logger = logging.getLogger(__name__)

__all__ = ["build_report", "build_cluster_report", "REPORT_METRICS"]


def _metrics_for(engine: str) -> list[str]:
    mapping = {
        "mysql": [
            "mysql.connection_usage_percent",
            "mysql.buffer_pool_hit_percent",
            "mysql.slow_queries_per_sec",
            "mysql.replication_delay_seconds",
            "mysql.threads_running",
            "mysql.total_size_bytes",
        ],
        "postgresql": [
            "pg.connection_usage_percent",
            "pg.cache_hit_percent",
            "pg.replication_lag_seconds",
            "pg.connections_active",
            "pg.total_size_bytes",
        ],
        "redis": [
            "redis.cache_hit_percent",
            "redis.connected_clients",
            "redis.memory_used_bytes",
            "redis.mem_fragmentation_ratio",
        ],
    }
    return mapping.get(engine, [])


# 供页面展示"报告依据了哪些指标"
REPORT_METRICS = {engine: _metrics_for(engine) for engine in ("mysql", "postgresql", "redis")}


def build_report(instance: Instance, *, now=None) -> InspectReport:
    """为一台实例生成诊断报告。

    只读：不改任何状态，也不连目标库。因此可以安全地在页面刷新的同时调用，
    不会给目标库添压力。
    """
    from apps.monitor.models import AlertEvent, LockWait

    now = now or timezone.now()
    engine = instance.cluster.engine

    metrics = _metrics_for(engine)
    raw = latest_values([instance.pk], metrics) if metrics else {}
    values = {
        metric: item["v"]
        for metric, per_instance in raw.items()
        for instance_id, item in per_instance.items()
        if instance_id == instance.pk
    }

    runs = latest_run_map([instance.pk])
    run = runs.get(instance.pk)

    alerts = AlertEvent.objects.filter(
        instance=instance, status=AlertEvent.Status.FIRING
    )
    firing = alerts.count()
    critical = alerts.filter(severity="critical").count() if firing else 0

    longest_wait = (
        LockWait.objects.filter(instance=instance)
        .order_by("-wait_seconds")
        .values_list("wait_seconds", flat=True)
        .first()
        or 0.0
    )

    return inspect_instance(
        instance_id=instance.pk,
        instance_name=instance.name,
        engine=engine,
        values=values,
        collect_ok=None if run is None else run.ok,
        # 同时带错误码与中文说明：错误码便于检索、对照文档与工单，
        # 中文说明便于一眼看懂。只给其中一个都会让排查多绕一步。
        collect_message=(
            " ".join(x for x in (run.error_code, run.message) if x) if run else ""
        ),
        has_password=instance.password_set,
        firing_alerts=firing,
        critical_alerts=critical,
        longest_lock_wait=float(longest_wait),
    )


def build_cluster_report(cluster, *, now=None) -> dict:
    """为一个集群生成汇总报告：逐实例评分 + 集群整体均分。

    集群均分只是"按实例简单平均"，不按权重：集群健康度没有客观权重，
    编一个权重只会让数字看起来精确而实质任意。页面同时展示最低分的实例，
    那才是该先修的。
    """
    now = now or timezone.now()
    instances = list(
        cluster.instances.select_related("cluster").order_by("role", "name")
    )
    reports = [build_report(inst, now=now) for inst in instances]

    scored = [r for r in reports if not r.insufficient]
    average = (
        round(sum(r.score for r in scored) / len(scored), 1) if scored else 0.0
    )
    worst = min(scored, key=lambda r: r.score) if scored else None

    return {
        "cluster": cluster,
        "reports": reports,
        "average": average,
        "worst": worst,
        "generated_at": now,
        "insufficient_count": sum(1 for r in reports if r.insufficient),
    }
