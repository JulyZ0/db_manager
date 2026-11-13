"""每日统计聚合逻辑。

口径（PRD FR-06）：
- 只统计 is_active=True 的实例，按「部门 × 集群」分组。
- 无实例的集群不产生行（题目口径是"统计实例数量"）。
- department_name / cluster_name 写**当时的快照值**，日后改名或删除
  不会篡改历史（PRD AC-16）。
- 幂等：唯一约束 (stat_date, cluster) + update_or_create（PRD AC-15）。
"""

from __future__ import annotations

import logging
from datetime import date

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.assets.models import Instance
from apps.metrics.models import DailyInstanceStat
from common.timing import Stopwatch, log_if_slow

logger = logging.getLogger(__name__)

# 1000 个集群以内应在秒级完成（PRD §8.1）
SLOW_AGGREGATE_MS = 60_000


def aggregate_for_date(stat_date: date | None = None) -> int:
    """按「部门 × 集群」统计实例数量并入库，返回写入/更新的行数。

    重复执行同一日期是安全的：依靠唯一约束做 upsert，
    行数不会增加，也不会产生重复（PRD AC-15）。
    """
    if stat_date is None:
        stat_date = timezone.localdate()

    watch = Stopwatch(f"aggregate:{stat_date}")

    # 一条 SQL 完成分组聚合，不在 Python 里逐集群查询
    rows = (
        Instance.objects.filter(is_active=True)
        .values(
            "cluster_id",
            "cluster__name",
            "cluster__department_id",
            "cluster__department__name",
        )
        .annotate(
            instance_count=Count("id"),
            active_count=Count("id", filter=Q(status=Instance.Status.ONLINE)),
        )
        .order_by("cluster__department_id", "cluster_id")
    )

    written = 0
    with transaction.atomic():
        for row in rows:
            cluster_id = row["cluster_id"]
            if cluster_id is None:
                # 集群被硬删除后 instances 无从归属，跳过更安全
                continue

            DailyInstanceStat.objects.update_or_create(
                stat_date=stat_date,
                cluster_id=cluster_id,
                defaults={
                    "department_id": row["cluster__department_id"],
                    "department_name": row["cluster__department__name"] or "",
                    "cluster_name": row["cluster__name"] or "",
                    "instance_count": row["instance_count"],
                    "active_count": row["active_count"],
                },
            )
            written += 1

    total_ms = watch.stop()
    logger.info("统计 %s 完成：写入 %d 行，耗时 %dms", stat_date, written, total_ms)
    log_if_slow(
        logger,
        watch,
        SLOW_AGGREGATE_MS,
        what=f"每日统计 {stat_date}",
    )
    return written
