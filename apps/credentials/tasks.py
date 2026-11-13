"""凭据相关的 Celery 任务。

任务名必须与 settings.CELERY_BEAT_SCHEDULE 中的 task 路径一致：
apps.credentials.tasks.rotate_all_passwords
"""

from __future__ import annotations

import logging
import time

from celery import shared_task

logger = logging.getLogger(__name__)

# 单批 100 个实例的正常耗时在秒级；超过这个值说明分批策略需要调整
SLOW_TASK_SECONDS = 120


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
    """每 12 小时（00:00 / 12:00）轮换所有启用实例的密码。

    真正的算法在 apps.credentials.services.rotate_all()，本函数只做
    Celery 包装：日志、耗时、以及"核心算法还没写完"时的降级。

    重试语义：单实例失败由 rotate_all 内部消化；只有整体性故障（数据库不可用等）
    才会冒泡到这里，交给 autoretry 指数退避重试。
    """
    from apps.credentials.services import rotate_all

    started = time.perf_counter()
    logger.info("开始批量密码轮换")

    try:
        stats = rotate_all()
    except NotImplementedError:
        # 核心算法尚未实现时，不要让 autoretry 空转三次（每次退避最长 5 分钟），
        # 只提示一次，等实现后下个周期自动生效。
        logger.warning(
            "轮换服务尚未实现（apps/credentials/services.py::rotate_all），本周期跳过"
        )
        return {"status": "not_implemented"}
    except Exception:
        logger.exception("批量密码轮换整体失败，将按退避策略重试")
        raise

    elapsed = time.perf_counter() - started
    logger.info(
        "密码轮换完成：成功 %s，失败 %s，共 %s，耗时 %.2fs",
        stats.get("success", 0),
        stats.get("failed", 0),
        stats.get("total", 0),
        elapsed,
    )
    if elapsed > SLOW_TASK_SECONDS:
        logger.warning(
            "密码轮换耗时 %.1fs，超过 %ss 的预期，建议检查分批大小或数据库负载",
            elapsed,
            SLOW_TASK_SECONDS,
        )

    return {**stats, "duration_ms": int(elapsed * 1000)}
