"""告警阈值评估与事件生命周期。

为什么单独成文
--------------
评估逻辑需要"看起来不显然"的三条规则，混在采集编排里会让它们难以被发现：

1. **持续时长**：单点越界不算事件。规则要求持续 N 分钟，那么从第一次
   越界起必须一直越界到 ``started_at + N`` 才算触发。中途回落即作废。
2. **去抖/冷却**：恢复后 ``cooldown_minutes`` 内不再重复开新事件，
   否则在阈值附近抖动的指标会刷出几十条告警把人吵到直接关掉告警。
3. **恢复判定**：事件已触发后，只要出现一次**不再越界**就恢复。
   这里刻意不做"连续 N 次正常才算恢复"——恢复慢一步没有代价，
   而漏报恢复会让运维对着一堆其实已经好的告警排查。

事件用「同一 (rule, instance) 上唯一一条 firing」表达，靠
:func:`evaluate_rule` 里的 ``status=FIRING`` 查询保证，不额外加唯一约束：
Django 的部分唯一索引在 SQLite/PG 上语法不同，而这里并发只会出现在
单进程评估任务里，查询前置足够。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.assets.models import Instance
from apps.monitor.models import AlertEvent, AlertRule, MetricSample
from apps.monitor.notify import notify_event

logger = logging.getLogger(__name__)

__all__ = ["evaluate_all", "evaluate_rule", "resolve_stale_events"]


def _latest_value(instance_id: int, metric: str):
    return (
        MetricSample.objects.filter(instance_id=instance_id, metric=metric)
        .order_by("-collected_at", "-id")
        .values_list("value", "collected_at")
        .first()
    )


def _firing_event(rule: AlertRule, instance_id: int) -> AlertEvent | None:
    """取该规则在该实例上仍处于 firing 的事件。

    顺序上先按 id 倒序：理论上同 (rule, instance) 只会有一条 firing，
    但历史数据或并发可能留下多条，取最新那条继续用，避免"多条同时更新"。
    """
    return (
        AlertEvent.objects.filter(
            rule=rule, instance_id=instance_id, status=AlertEvent.Status.FIRING
        )
        .order_by("-id")
        .first()
    )


def _resolve(event: AlertEvent, value: float, now) -> None:
    event.status = AlertEvent.Status.RESOLVED
    event.resolved_at = now
    event.latest_value = value
    event.last_seen_at = now
    event.save(
        update_fields=["status", "resolved_at", "latest_value", "last_seen_at"]
    )


def evaluate_rule(rule: AlertRule, now=None) -> dict[str, int]:
    """评估一条规则，返回 ``{"fired", "updated", "resolved"}`` 计数。"""
    now = now or timezone.now()

    qs = Instance.objects.filter(is_active=True)
    if rule.cluster_id:
        qs = qs.filter(cluster_id=rule.cluster_id)

    stats = {"fired": 0, "updated": 0, "resolved": 0}

    for instance in qs.only("id", "name", "host", "port"):
        latest = _latest_value(instance.pk, rule.metric)
        if latest is None:
            continue
        value, collected_at = latest
        breached = rule.matches(value)
        event = _firing_event(rule, instance.pk)

        if breached:
            if event is None:
                # 未触发过：先看是否满足"持续时长"
                if not _sustained(rule, instance.pk, now):
                    continue
                # 恢复后冷却期内不再开新事件：阈值附近抖动会反复
                # 触发/恢复，把运维吵到直接关掉告警。
                if _in_cooldown(rule, instance.pk, now):
                    continue
                with transaction.atomic():
                    event = AlertEvent.objects.create(
                        rule=rule,
                        rule_name=rule.name,
                        instance=instance,
                        instance_label=f"{instance.name} ({instance.host}:{instance.port})",
                        metric=rule.metric,
                        operator=rule.operator,
                        threshold=rule.threshold,
                        severity=rule.severity,
                        status=AlertEvent.Status.FIRING,
                        trigger_value=value,
                        latest_value=value,
                        started_at=now,
                        last_seen_at=now,
                        message=(
                            f"{rule.metric} {rule.get_operator_display()} "
                            f"{rule.threshold:g}（当前 {value:g}）"
                        ),
                    )
                stats["fired"] += 1
                logger.warning(
                    "告警触发 rule=%s instance=%s value=%s", rule.name, instance.pk, value
                )
                # 通知放在事务外：推送失败不能回滚已落库的事件
                notify_event(event, "fired")
            else:
                # 已在告警中：只刷新最新值与命中时间，不重复计数也不重复推送
                event.latest_value = value
                event.last_seen_at = now
                event.save(update_fields=["latest_value", "last_seen_at"])
                stats["updated"] += 1
        else:
            if event is not None:
                with transaction.atomic():
                    _resolve(event, value, now)
                stats["resolved"] += 1
                logger.info(
                    "告警恢复 rule=%s instance=%s value=%s", rule.name, instance.pk, value
                )
                notify_event(event, "resolved")

    return stats


def _in_cooldown(rule: AlertRule, instance_id: int, now) -> bool:
    """恢复后冷却期内返回 True。

    ``cooldown_minutes`` 为 0 表示不设冷却（立即允许再次触发）。
    只参考**最近一条已恢复**事件：更早的不相关。
    """
    if rule.cooldown_minutes <= 0:
        return False
    last_resolved = (
        AlertEvent.objects.filter(
            rule=rule,
            instance_id=instance_id,
            status=AlertEvent.Status.RESOLVED,
            resolved_at__isnull=False,
        )
        .order_by("-resolved_at")
        .values_list("resolved_at", flat=True)
        .first()
    )
    if last_resolved is None:
        return False
    return (now - last_resolved) < timedelta(minutes=rule.cooldown_minutes)


def _sustained(rule: AlertRule, instance_id: int, now) -> bool:
    """检查是否已**持续**越界满 ``duration_minutes``。

    实现方式：从最新一条采样向前回溯，只要连续越界就继续，遇到不越界即止；
    该连续段的跨度达到 ``duration`` 才算满足。

    为什么不用"取 [now-duration, now] 窗口内的采样，要求全部越界"：
    窗口起点是一个精确时间点，而采样是离散的（每分钟一条），边界那条通常
    落在窗口外一个周期，于是"持续满 5 分钟"实际要 6 分钟才触发——规则写了
    5 分钟却晚一个周期报警，运维会怀疑告警不准。回溯法直接度量"连续越界
    了多久"，与采集周期怎么对齐无关。

    多取 ``grace`` 一个周期外的数据，是为了让回溯有足够的历史可看：
    若只取 duration 窗口，回溯可能刚走两步就到窗口边界，误判"连续性不够"。
    """
    if rule.duration_minutes <= 0:
        return True

    grace_minutes = max(2, 2 * int(getattr(settings, "MONITOR_INTERVAL_MINUTES", 1)))
    since = now - timedelta(minutes=rule.duration_minutes + grace_minutes)
    rows = list(
        MetricSample.objects.filter(
            instance_id=instance_id,
            metric=rule.metric,
            collected_at__gte=since,
        )
        .order_by("collected_at")
        .values_list("value", "collected_at")
    )
    if not rows:
        return False

    # 从最新往回找连续越界段的起点
    continuous_start = None
    for value, collected_at in reversed(rows):
        if not rule.matches(value):
            break
        continuous_start = collected_at
    if continuous_start is None:
        return False

    return (now - continuous_start) >= timedelta(minutes=rule.duration_minutes)


def resolve_stale_events(now=None, grace_minutes: int = 15) -> int:
    """收敛"实例已无采集"的悬挂告警。

    实例被停用、或采集彻底停掉时，firing 事件不会自己恢复——它只能靠
    "出现一个不越界的值"来恢复。这类悬挂告警会让人误以为故障仍在持续。
    这里把"超过 grace 分钟没有任何命中"的事件标记为已恢复，并写清原因。
    """
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=max(1, grace_minutes))
    stale = AlertEvent.objects.filter(
        status=AlertEvent.Status.FIRING, last_seen_at__lt=cutoff
    )
    count = 0
    for event in stale:
        event.status = AlertEvent.Status.RESOLVED
        event.resolved_at = now
        event.message = (event.message + " ｜采集已停止，自动恢复")[:500]
        event.save(update_fields=["status", "resolved_at", "message"])
        count += 1
    if count:
        logger.info("自动收敛悬挂告警 %d 条", count)
    return count


def evaluate_all(now=None) -> dict[str, int]:
    """评估全部启用规则。"""
    now = now or timezone.now()
    total = {"fired": 0, "updated": 0, "resolved": 0, "rules": 0}
    for rule in AlertRule.objects.filter(enabled=True).select_related("cluster"):
        stats = evaluate_rule(rule, now=now)
        total["rules"] += 1
        for key in ("fired", "updated", "resolved"):
            total[key] += stats[key]
    total["stale_resolved"] = resolve_stale_events(now=now)
    return total
