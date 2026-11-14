"""采集器公共结构。

三个引擎（MySQL / PostgreSQL / Redis）的采集流程完全同构：
取凭据 → 建连 → 跑几条只读查询 → 归一化成 ``Sample`` → 关闭连接。
差异只在"跑哪几条查询、怎么解释结果"，因此把公共部分收敛到本模块，
各采集器只实现 ``collect_samples``。

两条硬约束
----------
1. **绝不外抛异常**。目标库不可达是常态（重启、网络抖动、权限变更），
   采集失败是有效的业务结论，不是服务端错误。集成方拿到的是
   ``CollectResult(ok=False, error_code=...)``，与 ``apps/probing`` 的
   口径一致，避免同一个故障在不同页面有两种说法。
2. **不泄露凭据**。任何返回给上层的文本都不含口令；驱动异常只保留
   分类码，原始消息一律不向上传递（可能含连接串、用户名等）。

关于速率指标
------------
QPS / TPS 这类"每秒多少次"的指标，引擎提供的是**累计计数器**，必须靠
两次采样求差。这里不在采集期做隐式有状态计算，而是提供
:func:`compute_rate`：调用方显式传入上一次的累计值与时刻。

间隔超出 ``[MIN_RATE_INTERVAL, MAX_RATE_INTERVAL]`` 时不计算——用一小时前
的计数器算出"QPS"其实是这一小时的平均值，标成 QPS 会误导看板。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

__all__ = [
    "Sample",
    "CollectResult",
    "compute_rate",
    "MIN_RATE_INTERVAL",
    "MAX_RATE_INTERVAL",
    "ERROR_DRIVER_MISSING",
    "ERROR_UNSUPPORTED",
    "ERROR_NO_PASSWORD",
    "ERROR_DECRYPT_FAILED",
    "ERROR_QUERY_FAILED",
    "ERROR_INVALID_VALUE",
    "ERROR_HINTS",
]

# 速率计算允许的最小/最大采样间隔。小于最小值时计数器差值太小，
# 一点抖动就会让结果上下翻几倍；大于最大值时算出来的是长窗口平均值，
# 与"当前速率"不是一回事。
MIN_RATE_INTERVAL = timedelta(seconds=10)
MAX_RATE_INTERVAL = timedelta(minutes=15)

# 采集侧的错误码。前四个直接复用 dbconsole 的语义（未登记密码、解密失败等），
# 后两个是采集特有：驱动缺失、查询失败、样本值无法解释。
ERROR_DRIVER_MISSING = "driver_missing"
ERROR_UNSUPPORTED = "unsupported_engine"
ERROR_NO_PASSWORD = "no_password"
ERROR_DECRYPT_FAILED = "decrypt_failed"
ERROR_QUERY_FAILED = "query_failed"
ERROR_INVALID_VALUE = "invalid_value"

ERROR_HINTS = {
    ERROR_DRIVER_MISSING: "缺少对应数据库驱动，请先安装",
    ERROR_UNSUPPORTED: "该引擎暂不支持指标采集",
    ERROR_NO_PASSWORD: "该实例尚未登记密码，无法采集",
    ERROR_DECRYPT_FAILED: "密码无法解密，可能更换过 FIELD_ENCRYPTION_KEY",
    ERROR_QUERY_FAILED: "采集查询失败，可能是账号权限不足或引擎版本不支持",
    ERROR_INVALID_VALUE: "采集到的指标值无法解析",
}


@dataclass(slots=True)
class Sample:
    """一条待落库的指标采样。"""

    metric: str
    value: float
    # 子维度（库名、表名、key 等）；无子维度为空串
    dimension: str = ""


@dataclass(slots=True)
class CollectResult:
    """一次采集的结果。

    ``samples`` 在失败时可能非空：部分查询成功、部分失败时，
    已拿到的数据仍然值得落库，但整体标记为失败并把错误码带上，
    这样看板不会因为一个次要查询失败就整体断档。
    """

    ok: bool
    samples: list[Sample] = field(default_factory=list)
    error_code: str = ""
    message: str = ""

    @property
    def hint(self) -> str:
        return self.message or ERROR_HINTS.get(self.error_code, "")

    @classmethod
    def failed(cls, error_code: str, message: str = "") -> "CollectResult":
        return cls(ok=False, error_code=error_code, message=message)


def compute_rate(
    previous_total: float,
    previous_at: datetime | None,
    current_total: float,
    now: datetime,
) -> float | None:
    """由两次累计计数器求每秒速率；间隔不合适或计数器回绕时返回 None。

    计数器回绕（例如重启后归零）会让差值为负。这里不做"加回最大值"的
    修正：任何猜测都可能凭空造出一个尖峰，而漏掉一个点只是趋势图上少一段，
    后者的危害小得多。
    """
    if previous_at is None:
        return None

    elapsed = now - previous_at
    if elapsed < MIN_RATE_INTERVAL or elapsed > MAX_RATE_INTERVAL:
        return None

    delta = current_total - previous_total
    if delta < 0:
        return None

    return delta / elapsed.total_seconds()


def as_float(value: object) -> float | None:
    """把驱动返回的值转成 float；无法解析时返回 None 而不是抛异常。

    MySQL 的 ``SHOW GLOBAL STATUS`` 返回的变量值多数是字符串，
    Redis 的 ``INFO`` 是文本，PG 的 ``pg_stat_*`` 已是数值类型，
    这里统一兜底。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    """安全求比值；分母为 0 或任一为空时返回 None。"""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def percent(numerator: float | None, denominator: float | None) -> float | None:
    """比率转百分比（0-100）。"""
    value = ratio(numerator, denominator)
    return None if value is None else value * 100.0
