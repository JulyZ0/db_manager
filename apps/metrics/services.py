"""每日统计聚合逻辑。

这是需要你手写的四个核心算法之一（见 README「待手写清单」）。
"""

from __future__ import annotations

from datetime import date

from django.utils import timezone


def aggregate_for_date(stat_date: date | None = None) -> int:
    """按「部门 × 集群」统计实例数量并入库，返回写入行数。

    TODO(手写) 实现要点：
    1. stat_date 默认取 timezone.localdate()（Asia/Shanghai 的当天）。
    2. 只统计 is_active=True 的实例，以集群分组：
         Instance.objects.filter(is_active=True)
                 .values("cluster_id", "cluster__name",
                         "cluster__department_id", "cluster__department__name")
                 .annotate(instance_count=Count("id"),
                           active_count=Count("id", filter=Q(status="online")))
    3. 无实例的集群不产生行（题目口径：统计实例数量）。
    4. 落库用 update_or_create(
           stat_date=..., cluster_id=...,
           defaults={department_id, department_name, cluster_name,
                     instance_count, active_count})
       依赖唯一约束 uniq_daily_stat_date_cluster 保证幂等（PRD AC-15）。
    5. department_name / cluster_name 必须写当时的快照值，
       这样日后改名或删除不会篡改历史（PRD AC-16）。
    6. 整个函数包在 transaction.atomic() 里，避免部分写入。
    7. 返回实际写入/更新的行数。
    """
    raise NotImplementedError("TODO: 实现每日统计聚合")
