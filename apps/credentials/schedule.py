"""轮换调度判定层。

为什么需要这一层
----------------
``interval`` 与 ``daily`` 两种口径的到点判定都已经在 ``RotationPolicy`` 上实现，
但调度侧还有两个问题要解决：

1. **Cron 表达不了严格等间隔**。``hour="*/7"`` 是"0/7/14/21 点"，
   第二天又从 0 点开始，并不是每 7 小时一次。所以 interval 口径不能直接
   翻成 crontab 交给 beat，只能由任务自己判断"到点没有"。
2. **两种调度器要共用同一套判定**。Celery Beat 与进程内调度器如果各写一遍
   "现在该不该跑"，迟早会分叉。这里提供一个入口，两边都只调它。

于是 beat 侧注册的任务改为「每分钟检查一次」，真正跑不跑由 ``run_if_due``
决定。代价是每分钟一次空转检查，成本是若干次内存比较，可以忽略。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.utils import timezone

from apps.credentials.models import RotationPolicy, RotationScheduleMode

logger = logging.getLogger(__name__)


def describe_schedule(policy: RotationPolicy) -> str:
    """把策略描述成一句人话，用于页面展示与日志。"""
    if not policy.enabled:
        return "已停用"

    if policy.schedule_mode == RotationScheduleMode.INTERVAL:
        return f"每 {policy.interval_hours} 小时"

    moments = policy.daily_moments()
    return "每天 " + "、".join(f"{m.hour:02d}:{m.minute:02d}" for m in moments)


def describe_next_run(policy: RotationPolicy, *, now: datetime | None = None) -> str:
    """下次执行时间的人话描述，带剩余时长。"""
    next_run = policy.next_run_at(now=now)
    if next_run is None:
        return "不会执行（策略已停用）"

    moment = now or timezone.now()
    moment = timezone.localtime(moment) if timezone.is_aware(moment) else moment
    remaining = next_run - moment

    # 负值说明已经过期（例如服务停机错过了时刻），说人话而不是显示负数
    if remaining.total_seconds() <= 0:
        return "即将执行"

    total_minutes = int(remaining.total_seconds() // 60)
    if total_minutes < 60:
        gap = f"{total_minutes} 分钟后"
    elif total_minutes < 60 * 24:
        gap = f"{total_minutes // 60} 小时后"
    else:
        gap = f"{total_minutes // (60 * 24)} 天后"

    return f"{timezone.localtime(next_run):%Y-%m-%d %H:%M}（{gap}）"


def should_run_now(policy: RotationPolicy, *, now: datetime | None = None) -> bool:
    """是否应当在本轮检查中执行轮换。

    单独包一层而不是直接调 ``policy.is_due``，是为了给"正在跑"留出扩展点：
    未来若加入分布式锁，只需改这里，两个调度器都不用动。
    """
    return policy.is_due(now=now)


def run_if_due(*, now: datetime | None = None) -> dict[str, object]:
    """到点则执行一次全量轮换，否则什么都不做。

    Returns:
        ``{"ran": bool, "reason": str, ...}``。调度器只记录日志，
        不关心具体统计口径。
    """
    policy = RotationPolicy.load()

    if not policy.enabled:
        return {"ran": False, "reason": "策略已停用"}

    if not should_run_now(policy, now=now):
        return {"ran": False, "reason": "未到执行时间"}

    from apps.credentials.services import rotate_all

    target_ids = policy.resolve_instance_ids()
    if not target_ids:
        logger.info("轮换策略到点，但范围内没有启用实例，跳过")
        # 仍然推进 last_run_at：否则每个 tick 都会重复判定为"该跑了"
        policy.last_run_at = _stamp(now)
        policy.last_run_summary = "范围内无启用实例"
        policy.save(update_fields=["last_run_at", "last_run_summary", "updated_at"])
        return {"ran": False, "reason": "范围内无启用实例", "total": 0}

    logger.info(
        "轮换策略到点：%s，范围 %s，共 %d 个实例，密码长度 %d",
        describe_schedule(policy),
        policy.scope_label(),
        len(target_ids),
        policy.password_length,
    )

    try:
        stats = rotate_all(
            instance_ids=target_ids,
            password_length=policy.password_length,
        )
    except Exception as exc:
        logger.exception("按策略执行轮换失败")
        policy.last_run_at = _stamp(now)
        policy.last_run_summary = f"执行失败：{type(exc).__name__}"[:200]
        policy.save(update_fields=["last_run_at", "last_run_summary", "updated_at"])
        raise

    policy.last_run_at = _stamp(now)
    policy.last_run_summary = (
        f"成功 {stats.get('success', 0)}，失败 {stats.get('failed', 0)}，"
        f"共 {stats.get('total', 0)}"
    )[:200]
    policy.save(update_fields=["last_run_at", "last_run_summary", "updated_at"])

    return {"ran": True, "reason": "到点执行", **stats}


def _stamp(now: datetime | None) -> datetime:
    if now is None:
        return timezone.now()
    return now


def catch_up_window(policy: RotationPolicy) -> timedelta:
    """允许"补跑"的时间窗口。

    服务停机导致错过执行时刻时，重启后要不要立刻补一次？
    每天定点口径下应当补——否则一天没换密码；但如果已经错过了很久
    （比如停了两天），补跑反而会在启动瞬间制造一次意外的全量改密。
    这里给出 6 小时的宽限窗口，由调用方决定是否使用。
    """
    if policy.schedule_mode == RotationScheduleMode.DAILY:
        return timedelta(hours=6)
    hours = max(1, int(policy.interval_hours or 0))
    return timedelta(hours=min(hours, 24))
