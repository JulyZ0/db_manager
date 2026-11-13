"""统计相关的 Celery 任务。

任务名必须与 settings.CELERY_BEAT_SCHEDULE 中的 task 路径一致：
apps.metrics.tasks.aggregate_daily_stats
"""

from __future__ import annotations

import logging
import time

from celery import shared_task

logger = logging.getLogger(__name__)

# 1000 个集群以内的聚合应在秒级完成（PRD §8.1）
SLOW_TASK_SECONDS = 60


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
    """
    from apps.metrics.services import aggregate_for_date

    started = time.perf_counter()
    logger.info("开始生成每日实例数量快照")

    try:
        rows = aggregate_for_date()
    except NotImplementedError:
        logger.warning(
            "统计服务尚未实现（apps/metrics/services.py::aggregate_for_date），本次跳过"
        )
        return {"status": "not_implemented"}
    except Exception:
        logger.exception("每日统计任务失败，将按退避策略重试")
        raise

    elapsed = time.perf_counter() - started
    logger.info("每日统计完成：写入 %s 行，耗时 %.2fs", rows, elapsed)
    if elapsed > SLOW_TASK_SECONDS:
        logger.warning(
            "每日统计耗时 %.1fs，超过 %ss 的预期，建议检查集群数量与索引",
            elapsed,
            SLOW_TASK_SECONDS,
        )

    return {"rows": rows, "duration_ms": int(elapsed * 1000)}
