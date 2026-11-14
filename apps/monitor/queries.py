"""时序查询层：趋势取数、降采样、最新值。

为什么需要降采样
----------------
采集周期是 1 分钟，30 天就是 43200 个点。把这么多点直接塞给浏览器，
既拖垮渲染，也没有任何信息增量（屏幕只有一千多像素宽）。
这里按查询跨度自动选择分辨率：

- ≤ 6 小时   → 按分钟
- ≤ 2 天     → 按小时
- 更长       → 按天

聚合用 Django 的 ``TruncMinute/TruncHour/TruncDay`` + ``Avg``，
在**数据库里**完成，而不是取回全部行再用 Python 归组——后者在
30 天窗口上要读几十万行。

为什么不用 ``RawSQL``/``date_trunc``：那些写法在 SQLite 与
PostgreSQL 上语法不同，而 ``Trunc*`` 由 Django 翻译成各自的后端实现，
换库时不会突然失效。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db.models import Avg
from django.db.models.functions import TruncDay, TruncHour, TruncMinute
from django.utils import timezone

from apps.monitor.models import MetricSample

__all__ = [
    "resolve_window",
    "fetch_series",
    "latest_values",
    "DEFAULT_HOURS",
    "MAX_POINTS",
]

# 默认看最近 6 小时：足够发现"刚才发生了什么"，又不会一屏塞满噪声
DEFAULT_HOURS = 6
# 允许的最大查询跨度（天）。再长应当用"数量统计"或导出，而不是在线趋势图。
MAX_WINDOW_DAYS = 90
# 单条曲线最多返回多少个点
MAX_POINTS = 360


def resolve_window(
    start: datetime | None,
    end: datetime | None,
    hours: int = DEFAULT_HOURS,
) -> tuple[datetime, datetime]:
    """把请求参数规整成一个合法时间窗。

    非法输入（start > end、跨度超限、未来时间）一律退回默认窗口，
    而不是报错——趋势图是只读展示，宁可显示默认范围也不要给运维
    一个红色错误页。
    """
    end = end or timezone.now()
    start = start or (end - timedelta(hours=max(1, min(24 * 7, hours))))

    if start > end:
        start, end = end, start

    limit = timedelta(days=MAX_WINDOW_DAYS)
    if end - start > limit:
        start = end - limit

    # 未来时间没有数据，把窗口拉回来
    now = timezone.now()
    if end > now:
        end = now
    if start >= end:
        start = end - timedelta(hours=1)
    return start, end


def _trunc_for(start: datetime, end: datetime):
    span = end - start
    if span <= timedelta(hours=6):
        return TruncMinute, "minute"
    if span <= timedelta(days=2):
        return TruncHour, "hour"
    return TruncDay, "day"


def fetch_series(
    instance_ids: list[int],
    metrics: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, dict[int, list[dict]]]:
    """按实例分组取时序。

    返回 ``{metric: {instance_id: [{"t": iso, "v": float}, ...]}}``。
    用嵌套字典而不是扁平列表：前端按曲线渲染，天然需要按
    (指标, 实例) 分组，让后端分好比在 JS 里再 group 一遍更省。
    """
    result: dict[str, dict[int, list[dict]]] = {
        metric: {} for metric in metrics
    }
    if not instance_ids or not metrics:
        return result

    trunc, _ = _trunc_for(start, end)
    rows = (
        MetricSample.objects.filter(
            instance_id__in=instance_ids,
            metric__in=metrics,
            collected_at__gte=start,
            collected_at__lte=end,
        )
        .annotate(bucket=trunc("collected_at"))
        .values("instance_id", "metric", "bucket")
        .annotate(avg_value=Avg("value"))
        .order_by("bucket")
    )

    for row in rows:
        metric = row["metric"]
        if metric not in result:
            continue
        bucket = row["bucket"]
        stamp = bucket.isoformat() if hasattr(bucket, "isoformat") else str(bucket)
        result[metric].setdefault(row["instance_id"], []).append(
            {"t": stamp, "v": round(float(row["avg_value"]), 6)}
        )

    # 超长序列按等间隔抽稀，保证前端点数可控
    for metric, per_instance in result.items():
        for inst_id, points in per_instance.items():
            if len(points) > MAX_POINTS:
                step = len(points) / MAX_POINTS
                per_instance[inst_id] = [
                    points[int(i * step)] for i in range(MAX_POINTS)
                ]
    return result


def latest_values(
    instance_ids: list[int], metrics: list[str]
) -> dict[str, dict[int, dict]]:
    """取每个实例每个指标的最新一条采样。

    用 ``ORDER BY collected_at DESC`` + 逐条挑第一次出现，而不是对每个
    (实例, 指标) 各发一次查询——列表页上那是几十次往返。

    失败样本不参与：采集失败时不会写样本，因此"最新值"天然跳过失败轮次，
    但页面仍需展示最近一次采集的成败（由 ``CollectionRun`` 提供）。
    """
    result: dict[str, dict[int, dict]] = {metric: {} for metric in metrics}
    if not instance_ids or not metrics:
        return result

    rows = (
        MetricSample.objects.filter(
            instance_id__in=instance_ids, metric__in=metrics
        )
        .order_by("-collected_at", "-id")
        .values_list("instance_id", "metric", "value", "collected_at")
    )
    for instance_id, metric, value, collected_at in rows:
        bucket = result.get(metric)
        if bucket is None or instance_id in bucket:
            continue
        bucket[instance_id] = {
            "v": round(float(value), 6),
            "t": collected_at.isoformat(),
        }
    return result
