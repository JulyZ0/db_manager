"""统计相关的 Celery 任务。

任务名必须与 settings.CELERY_BEAT_SCHEDULE 中的 task 路径一致：
apps.metrics.tasks.aggregate_daily_stats
"""

from __future__ import annotations

import logging

from celery import shared_task

from common.timing import Stopwatch, log_if_slow

logger = logging.getLogger(__name__)

# 1000 个集群以内的聚合应在秒级完成（PRD §8.1）
SLOW_TASK_MS = 60_000


@shared_task(
    bind=True,
    name="apps.metrics.tasks.aggregate_daily_stats",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=300,
    max_retries=3,
    acks_late=True,
)
def aggregate_daily_stats(self) -> dict[str, object]:
    """每天 00:00 生成「部门 × 集群」实例数量快照。

    真正的聚合逻辑在 apps.metrics.services.aggregate_for_date()，本函数只做
    Celery 包装。幂等性由 services 层的 update_or_create 保证，
    重复执行（含手动补算）不会产生重复行。

    计时统一走 common.timing.Stopwatch，与中间件、轮换任务同一套口径。
    """
    from apps.metrics.services import aggregate_for_date

    watch = Stopwatch("task:aggregate_daily_stats")
    logger.info("开始生成每日实例数量快照")

    try:
        rows = aggregate_for_date()
    except Exception:
        logger.exception("每日统计任务失败，将按退避策略重试")
        raise

    elapsed_ms = watch.stop()
    logger.info("每日统计完成：写入 %s 行，耗时 %dms", rows, elapsed_ms)
    log_if_slow(
        logger,
        watch,
        SLOW_TASK_MS,
        what="每日统计",
        extra="建议检查集群数量与索引",
    )

    return {"rows": rows, "duration_ms": elapsed_ms}
