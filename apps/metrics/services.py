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
    # 一条 INSERT ... ON CONFLICT 完成全部 upsert。
    #
    # 此前是「单事务 + 逐集群 update_or_create」：集群上千时，一个事务里要
    # 跑上千次 SELECT + INSERT/UPDATE，既把事务拖得很长（SQLite 上全程攥着
    # 写锁，整站写请求排队），也把 N 次往返变成 N 次。改成批量后只有一条
    # 语句，事务边界也随之消失。
    #
    # 幂等性不变：唯一约束 (stat_date, cluster) + update_conflicts 仍是
    # upsert，重复执行同一天不会产生新行（PRD AC-15）。
    now = timezone.now()
    objects = [
        DailyInstanceStat(
            stat_date=stat_date,
            cluster_id=row["cluster_id"],
            department_id=row["cluster__department_id"],
            # 名称写快照值：日后改名/删除不会篡改历史（PRD AC-16）
            department_name=row["cluster__department__name"] or "",
            cluster_name=row["cluster__name"] or "",
            instance_count=row["instance_count"],
            active_count=row["active_count"],
            # 显式给时间戳：bulk_create 虽然会走 pre_save 填 auto_now*，
            # 但这里统一用同一个 now，避免同一批行的 created_at 出现毫秒级
            # 抖动，也便于排查"这一批是什么时候写的"。
            created_at=now,
            updated_at=now,
        )
        for row in rows
        if row["cluster_id"] is not None
        # 集群被硬删除后 instances 无从归属，跳过更安全
    ]

    if objects:
        DailyInstanceStat.objects.bulk_create(
            objects,
            update_conflicts=True,
            unique_fields=["stat_date", "cluster"],
            update_fields=[
                "department",
                "department_name",
                "cluster_name",
                "instance_count",
                "active_count",
                "updated_at",
            ],
        )
        written = len(objects)

    total_ms = watch.stop()
    logger.info("统计 %s 完成：写入 %d 行，耗时 %dms", stat_date, written, total_ms)
    log_if_slow(
        logger,
        watch,
        SLOW_AGGREGATE_MS,
        what=f"每日统计 {stat_date}",
    )
    return written
