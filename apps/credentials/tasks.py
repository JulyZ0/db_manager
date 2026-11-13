"""凭据相关的 Celery 任务。

任务名必须与 settings.CELERY_BEAT_SCHEDULE 中的 task 路径一致。

两个入口的分工：

- ``rotate_passwords_by_policy``  给定时器用。每分钟被 Beat / 进程内调度器
  叫一次，真正跑不跑由轮换策略判定（``apps.credentials.schedule.run_if_due``）。
  为什么不是「每 12 小时触发一次」：策略允许"每 5 小时""每天 03:00、15:00"
  这类配置，cron 表达不了严格等间隔，只能由任务自己判断到点没有。
- ``rotate_all_passwords``        给运维手动触发、以及需要"立刻全量换一遍"
  的场景用，不看策略时间，但会沿用策略里的密码长度。
"""

from __future__ import annotations

import logging

from celery import shared_task

from common.timing import Stopwatch, log_if_slow

logger = logging.getLogger(__name__)

# 单批 100 个实例的正常耗时在秒级；超过这个值说明分批策略需要调整
SLOW_TASK_MS = 120_000


@shared_task(
    bind=True,
    name="apps.credentials.tasks.rotate_passwords_by_policy",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=300,
    max_retries=3,
    acks_late=True,
)
def rotate_passwords_by_policy(self) -> dict[str, object]:
    """按轮换策略检查一次：到点才执行，未到点立刻返回。

    每分钟被调用一次，绝大多数时候只做几次内存比较就返回，成本可忽略。
    到点与否的判定、范围解析、密码长度都在 ``run_if_due`` 里，
    与进程内调度器共用同一份逻辑，避免两套判定慢慢分叉。

    重试语义：``run_if_due`` 在失败时也会推进 ``last_run_at``，
    所以 autoretry 不会造成重复轮换，只是重新走一遍"是否到点"的检查
    （此时通常已判定为未到点，等于空跑一次）。
    """
    from apps.credentials.schedule import run_if_due

    watch = Stopwatch("task:rotate_passwords_by_policy")
    result = run_if_due()

    if not result.get("ran"):
        # 未到点属于正常高频路径，不打 info 日志，避免每分钟刷屏
        logger.debug("策略未到点：%s", result.get("reason"))
        return {**result, "duration_ms": watch.stop()}

    elapsed_ms = watch.stop()
    logger.info(
        "按策略轮换完成：成功 %s，失败 %s，共 %s，耗时 %dms",
        result.get("success", 0),
        result.get("failed", 0),
        result.get("total", 0),
        elapsed_ms,
    )
    log_if_slow(
        logger,
        watch,
        SLOW_TASK_MS,
        what="按策略批量轮换",
        extra="建议检查分批大小、并发度或数据库负载",
    )
    return {**result, "duration_ms": elapsed_ms}


@shared_task(
    bind=True,
    name="apps.credentials.tasks.rotate_all_passwords",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=300,
    max_retries=3,
    acks_late=True,
)
def rotate_all_passwords(self) -> dict[str, object]:
    """立刻轮换全部启用实例的密码（不看时间，但仍按策略的范围与强度）。

    真正的算法在 apps.credentials.services.rotate_all()，本函数只做
    Celery 包装：日志、耗时、以及整体性故障的重试。

    重试语义：单实例失败由 rotate_all 内部消化；只有整体性故障（数据库不可用等）
    才会冒泡到这里，交给 autoretry 指数退避重试。

    计时统一走 common.timing.Stopwatch，与中间件、单实例轮换同一套口径。
    """
    from apps.credentials.models import RotationPolicy
    from apps.credentials.services import rotate_all

    watch = Stopwatch("task:rotate_all_passwords")
    logger.info("开始批量密码轮换（手动/全量）")

    # 沿用策略里的强度：手动执行不该和定时执行产出不同规格的密码
    policy = RotationPolicy.load()

    try:
        stats = rotate_all(
            instance_ids=policy.resolve_instance_ids(),
            password_length=policy.password_length,
        )
    except Exception:
        logger.exception("批量密码轮换整体失败，将按退避策略重试")
        raise

    elapsed_ms = watch.stop()
    logger.info(
        "密码轮换完成：成功 %s，失败 %s，共 %s，耗时 %dms",
        stats.get("success", 0),
        stats.get("failed", 0),
        stats.get("total", 0),
        elapsed_ms,
    )
    log_if_slow(
        logger,
        watch,
        SLOW_TASK_MS,
        what="批量密码轮换",
        extra="建议检查分批大小、并发度或数据库负载",
    )

    return {**stats, "duration_ms": elapsed_ms}
