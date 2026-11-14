"""monitor 接口：时序指标查询。

只读接口，登录后可用（与统计接口同一套会话认证）。页面自身也是登录后
可见，因此这里不再单独加权限——监控数据是运维的日常输入，只读不构成风险。
"""

from __future__ import annotations

from datetime import datetime

from django.http import HttpRequest
from ninja import Query, Router, Schema

from apps.assets.models import Cluster, Instance
from apps.monitor.models import CollectionRun
from apps.monitor.queries import DEFAULT_HOURS, fetch_series, latest_values, resolve_window
from apps.monitor.services import latest_run_map
from common.auth import django_session_auth

router = Router(tags=["监控"], auth=django_session_auth)


class PointOut(Schema):
    t: str
    v: float


class SeriesOut(Schema):
    metric: str
    instance_id: int
    instance_name: str
    points: list[PointOut]


class SeriesResponse(Schema):
    start: datetime
    end: datetime
    series: list[SeriesOut]


class LatestItemOut(Schema):
    metric: str
    instance_id: int
    instance_name: str
    value: float
    collected_at: datetime


class LatestResponse(Schema):
    results: list[LatestItemOut]


class CollectStatusOut(Schema):
    instance_id: int
    instance_name: str
    ok: bool
    error_code: str
    message: str
    collected_at: datetime | None
    duration_ms: int


class CollectStatusResponse(Schema):
    results: list[CollectStatusOut]


def _instance_ids_for_cluster(cluster_id: int | None, instance_ids: list[int]) -> list[int]:
    """确定查询范围：显式实例 > 集群 > 全部启用实例。"""
    if instance_ids:
        return instance_ids
    if cluster_id is not None:
        return list(
            Instance.objects.filter(cluster_id=cluster_id, is_active=True).values_list(
                "id", flat=True
            )
        )
    return list(Instance.objects.filter(is_active=True).values_list("id", flat=True))


@router.get("/series", response=SeriesResponse, summary="查询指标时序")
def get_series(
    request: HttpRequest,
    metrics: str = Query(..., description="逗号分隔的指标键，如 mysql.qps,pg.tps"),
    instance_ids: str = Query("", description="逗号分隔的实例 id，留空表示按集群/全部"),
    cluster_id: int | None = Query(None),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    hours: int = Query(DEFAULT_HOURS, description="未给 start 时回看多少小时"),
):
    metric_list = [m.strip() for m in metrics.split(",") if m.strip()][:20]
    ids = [int(x) for x in instance_ids.split(",") if x.strip().isdigit()]
    ids = _instance_ids_for_cluster(cluster_id, ids)

    start_at, end_at = resolve_window(start, end, hours)
    raw = fetch_series(ids, metric_list, start_at, end_at)

    names = dict(
        Instance.objects.filter(id__in=ids).values_list("id", "name")
    )
    series: list[SeriesOut] = []
    for metric, per_instance in raw.items():
        for instance_id, points in per_instance.items():
            series.append(
                SeriesOut(
                    metric=metric,
                    instance_id=instance_id,
                    instance_name=names.get(instance_id, str(instance_id)),
                    points=[PointOut(t=p["t"], v=p["v"]) for p in points],
                )
            )

    return SeriesResponse(start=start_at, end=end_at, series=series)


@router.get("/latest", response=LatestResponse, summary="查询指标最新值")
def get_latest(
    request: HttpRequest,
    metrics: str = Query(..., description="逗号分隔的指标键"),
    instance_ids: str = Query(""),
    cluster_id: int | None = Query(None),
):
    metric_list = [m.strip() for m in metrics.split(",") if m.strip()][:50]
    ids = [int(x) for x in instance_ids.split(",") if x.strip().isdigit()]
    ids = _instance_ids_for_cluster(cluster_id, ids)

    raw = latest_values(ids, metric_list)
    names = dict(Instance.objects.filter(id__in=ids).values_list("id", "name"))

    results: list[LatestItemOut] = []
    for metric, per_instance in raw.items():
        for instance_id, item in per_instance.items():
            results.append(
                LatestItemOut(
                    metric=metric,
                    instance_id=instance_id,
                    instance_name=names.get(instance_id, str(instance_id)),
                    value=item["v"],
                    collected_at=item["t"],
                )
            )
    return LatestResponse(results=results)


@router.get("/collect-status", response=CollectStatusResponse, summary="查询最近一次采集状态")
def get_collect_status(
    request: HttpRequest,
    instance_ids: str = Query(""),
    cluster_id: int | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    ids = [int(x) for x in instance_ids.split(",") if x.strip().isdigit()]
    ids = _instance_ids_for_cluster(cluster_id, ids)[:limit]

    instances = Instance.objects.filter(id__in=ids).values("id", "name")
    name_map = {row["id"]: row["name"] for row in instances}
    runs = latest_run_map(ids)

    results: list[CollectStatusOut] = []
    for instance_id in ids:
        run: CollectionRun | None = runs.get(instance_id)
        results.append(
            CollectStatusOut(
                instance_id=instance_id,
                instance_name=name_map.get(instance_id, str(instance_id)),
                ok=bool(run and run.ok),
                error_code=run.error_code if run else "no_data",
                message=run.message if run else "尚无采集记录",
                collected_at=run.collected_at if run else None,
                duration_ms=run.duration_ms if run else 0,
            )
        )
    return CollectStatusResponse(results=results)


@router.get("/engines", summary="各引擎当前支持的指标键")
def get_metric_catalog(request: HttpRequest):
    """给前端做指标选择器用。

    指标键集中在后端声明，避免页面里散落字符串常量——将来采集口径调整时
    只需改这一处。
    """
    catalog = {
        "mysql": [
            "mysql.connections",
            "mysql.connection_usage_percent",
            "mysql.qps",
            "mysql.slow_queries_per_sec",
            "mysql.buffer_pool_hit_percent",
            "mysql.total_size_bytes",
            "mysql.replication_delay_seconds",
        ],
        "postgresql": [
            "pg.connections",
            "pg.connection_usage_percent",
            "pg.tps",
            "pg.rollback_percent",
            "pg.cache_hit_percent",
            "pg.longest_transaction_seconds",
            "pg.total_size_bytes",
            "pg.replication_lag_seconds",
        ],
        "redis": [
            "redis.memory_used_bytes",
            "redis.memory_usage_percent",
            "redis.mem_fragmentation_ratio",
            "redis.connected_clients",
            "redis.ops_per_sec",
            "redis.cache_hit_percent",
            "redis.total_keys",
        ],
    }
    clusters = Cluster.objects.values_list("id", "name", "engine")
    return {
        "catalog": catalog,
        "engines": sorted({row[2] for row in clusters}),
    }
