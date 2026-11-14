"""metrics 接口：每日统计快照的查询与补算。"""

from __future__ import annotations

from datetime import date

from django.http import HttpRequest
from ninja import Query, Router, Schema

from apps.metrics.models import DailyInstanceStat
from apps.metrics.services import aggregate_for_date
from common.auth import django_session_auth
from common.permissions import PERM_ADMIN_REBUILD_STATS, require_perm

router = Router(tags=["统计"])


class DailyStatRowOut(Schema):
    department_id: int | None
    department_name: str
    cluster_id: int | None
    cluster_name: str
    instance_count: int
    active_count: int


class DailyStatOut(Schema):
    stat_date: date
    total_instances: int
    results: list[DailyStatRowOut]


class RebuildOut(Schema):
    stat_date: date
    rows_written: int


@router.get("/daily", response=DailyStatOut, summary="查询某日的部门×集群统计")
def get_daily_stats(
    request: HttpRequest,
    stat_date: date = Query(..., description="统计日期，YYYY-MM-DD"),
    department_id: int | None = Query(None),
    cluster_id: int | None = Query(None),
):
    qs = DailyInstanceStat.objects.filter(stat_date=stat_date)
    if department_id is not None:
        qs = qs.filter(department_id=department_id)
    if cluster_id is not None:
        qs = qs.filter(cluster_id=cluster_id)

    qs = qs.order_by("department_id", "cluster_id")
    rows = list(qs)

    return DailyStatOut(
        stat_date=stat_date,
        total_instances=sum(r.instance_count for r in rows),
        results=[
            DailyStatRowOut(
                department_id=r.department_id,
                department_name=r.department_name,
                cluster_id=r.cluster_id,
                cluster_name=r.cluster_name,
                instance_count=r.instance_count,
                active_count=r.active_count,
            )
            for r in rows
        ],
    )


@router.post(
    "/daily/rebuild",
    response=RebuildOut,
    summary="按日期重算统计（幂等）",
    auth=django_session_auth,
)
def rebuild_daily_stats(request: HttpRequest, stat_date: date = Query(...)):
    require_perm(request.user, PERM_ADMIN_REBUILD_STATS, what="重算统计")
    rows_written = aggregate_for_date(stat_date)
    return RebuildOut(stat_date=stat_date, rows_written=rows_written)
