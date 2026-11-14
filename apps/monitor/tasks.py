"""监控采集的周期任务。

任务名必须与 ``settings.CELERY_BEAT_SCHEDULE`` 中的 task 路径一致：
``apps.monitor.tasks.collect_metrics`` / ``apps.monitor.tasks.purge_metric_samples``

本模块只做 Celery 包装（重试、日志、计时），真正的编排在
``apps.monitor.services``。进程内调度器解析的也是这两个函数，
因此两种调度模式下行为完全一致。
"""

from __future__ import annotations

import logging

from celery import shared_task

from common.timing import Stopwatch, log_if_slow

logger = logging.getLogger(__name__)

# 一轮采集超过这个耗时说明并发度或超时设置需要调整：
# 采集周期是分钟级，一轮若逼近周期长度，后续轮次会不断堆叠
SLOW_COLLECT_MS = 120_000
SLOW_PURGE_MS = 120_000


@shared_task(
    bind=True,
    name="apps.monitor.tasks.collect_metrics",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=300,
    max_retries=2,
    acks_late=True,
)
def collect_metrics(self) -> dict[str, object]:
    """按 ``MONITOR_INTERVAL_MINUTES`` 周期采集全部启用实例。

    幂等性说明：采集本身**不是**幂等的——每次运行都会产生新的时间点样本，
    这是时序数据的本意。重试安全的前提是"重复采集只是多一个数据点"，
    不会造成状态错乱，因此直接允许重试。

    单个实例的失败在 services 层已消化为 ``CollectionRun(ok=False)``，
    不会冒泡到这里；这里重试的是整体性故障（如数据库不可用、模块导入失败）。
    """
    from apps.monitor.services import collect_all

    watch = Stopwatch("task:collect_metrics")
    logger.info("开始监控采集")

    try:
        result = collect_all()
    except Exception:
        logger.exception("监控采集任务失败，将按退避策略重试")
        raise

    elapsed = watch.stop()
    if result.get("skipped"):
        logger.info("监控采集已关闭，跳过")
        return {"skipped": True, "duration_ms": elapsed}

    logger.info(
        "监控采集完成：实例 %s（成功 %s / 失败 %s），耗时 %dms",
        result.get("total", 0),
        result.get("ok", 0),
        result.get("failed", 0),
        elapsed,
    )
    log_if_slow(
        logger,
        watch,
        SLOW_COLLECT_MS,
        what="监控采集",
        extra="建议调低 MONITOR_CONCURRENCY 或放宽 MONITOR_TIMEOUT_SECONDS",
    )
    return {**result, "duration_ms": elapsed}


@shared_task(
    bind=True,
    name="apps.monitor.tasks.purge_metric_samples",
    autoretry_for=(Exception,),
    retry_backoff=120,
    retry_backoff_max=600,
    max_retries=2,
    acks_late=True,
)
def purge_metric_samples(self, dry_run: bool = False) -> dict[str, object]:
    """每天深夜按 ``MONITOR_RETENTION_DAYS`` 清理过期指标与采集记录。

    放在采集周期之外的低峰时段执行；分批删除的实现理由见
    ``apps.monitor.services.purge_expired``。
    """
    from apps.monitor.services import purge_expired

    watch = Stopwatch("task:purge_metric_samples")
    logger.info("开始清理过期监控数据（dry_run=%s）", dry_run)

    result = purge_expired(dry_run=dry_run)
    elapsed = watch.stop()

    logger.info(
        "监控数据清理完成：指标 %s 条 / 采集记录 %s 条，耗时 %dms",
        result.get("metrics", 0),
        result.get("runs", 0),
        elapsed,
    )
    log_if_slow(
        logger,
        watch,
        SLOW_PURGE_MS,
        what="监控数据清理",
        extra="建议检查 idx_sample_time / idx_collect_time 索引是否生效",
    )
    return {**result, "duration_ms": elapsed}


@shared_task(
    bind=True,
    name="apps.monitor.tasks.evaluate_alerts",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_backoff_max=300,
    max_retries=2,
    acks_late=True,
)
def evaluate_alerts(self) -> dict[str, object]:
    """按 ``MONITOR_ALERT_INTERVAL_MINUTES`` 评估全部启用的告警规则。

    与采集同频即可：更频繁也没有新数据可评。评估本身只读指标 + 写事件，
    不做任何目标库连接，因此开销远小于采集。
    """
    from django.conf import settings

    from apps.monitor.alerting import evaluate_all

    if not getattr(settings, "MONITOR_ALERT_ENABLED", True):
        logger.info("告警评估已关闭（MONITOR_ALERT_ENABLED=False），跳过")
        return {"skipped": True}

    watch = Stopwatch("task:evaluate_alerts")
    logger.info("开始告警评估")

    try:
        result = evaluate_all()
    except Exception:
        logger.exception("告警评估任务失败，将按退避策略重试")
        raise

    elapsed = watch.stop()
    logger.info(
        "告警评估完成：规则 %s 条（新触发 %s / 更新 %s / 恢复 %s / 自动收敛 %s），耗时 %dms",
        result.get("rules", 0),
        result.get("fired", 0),
        result.get("updated", 0),
        result.get("resolved", 0),
        result.get("stale_resolved", 0),
        elapsed,
    )
    log_if_slow(
        logger,
        watch,
        SLOW_COLLECT_MS,
        what="告警评估",
        extra="规则或实例过多时可拆分规则作用域",
    )
    return {**result, "duration_ms": elapsed}


@shared_task(
    bind=True,
    name="apps.monitor.tasks.collect_locks",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=600,
    max_retries=2,
    acks_late=True,
)
def collect_locks(self) -> dict[str, object]:
    """按 ``MONITOR_LOCK_INTERVAL_MINUTES`` 采集锁等待与死锁。

    与指标采集分开调度：锁查询要 join 多张性能视图，成本更高；且
    ``LockWait`` 是瞬时状态（每轮清空重写），没有按分钟累积的必要。
    """
    from django.conf import settings

    if not getattr(settings, "MONITOR_LOCK_ENABLED", True):
        logger.info("锁采集已关闭（MONITOR_LOCK_ENABLED=False），跳过")
        return {"skipped": True}

    from apps.monitor.services import collect_locks_all

    watch = Stopwatch("task:collect_locks")
    logger.info("开始锁采集")

    try:
        result = collect_locks_all()
    except Exception:
        logger.exception("锁采集任务失败，将按退避策略重试")
        raise

    elapsed = watch.stop()
    logger.info(
        "锁采集完成：实例 %s（等待关系 %s 条 / 新增死锁 %s 条），耗时 %dms",
        result.get("total", 0),
        result.get("waits", 0),
        result.get("deadlocks", 0),
        elapsed,
    )
    log_if_slow(
        logger,
        watch,
        SLOW_COLLECT_MS,
        what="锁采集",
        extra="可调低 MONITOR_LOCK_INTERVAL_MINUTES 或限制采集范围",
    )
    return {**result, "duration_ms": elapsed}
