"""统一的耗时测量组件。

设计目标：让 **HTTP 中间件** 和 **周期任务** 用同一套口径计时，
避免"中间件的 500ms 慢请求阈值"与"任务的 120s 慢任务阈值"各写一套逻辑、
日志字段也各不相同。两者共用本模块，只是阈值与输出目标不同。

为什么不用 `with timer():` 的上下文管理器：
中间件需要在异常路径上读取耗时之后再决定是否写入慢请求记录，
上下文管理器退出时异常正在传播，取不到中间结果。
显式 stop() 更直白，也更容易在 try/except/else 各分支复用。
"""

from __future__ import annotations

import logging
import time

__all__ = ["Stopwatch", "log_if_slow"]


class Stopwatch:
    """一次耗时测量。

    用法：
        sw = Stopwatch("rotate_all")
        ...  # 被测代码
        elapsed = sw.stop()
    """

    __slots__ = ("label", "_started", "_stopped_ms")

    def __init__(self, label: str = "") -> None:
        self.label = label
        self._started = time.perf_counter()
        self._stopped_ms: int | None = None

    def stop(self) -> int:
        """停止计时并返回毫秒。重复调用返回首次结果，不会越算越大。"""
        if self._stopped_ms is None:
            self._stopped_ms = int((time.perf_counter() - self._started) * 1000)
        return self._stopped_ms

    @property
    def elapsed_ms(self) -> int:
        """当前已耗时（毫秒）。未停止时实时计算。"""
        if self._stopped_ms is not None:
            return self._stopped_ms
        return int((time.perf_counter() - self._started) * 1000)

    def exceeds(self, threshold_ms: int) -> bool:
        return self.elapsed_ms > threshold_ms


def log_if_slow(
    logger: logging.Logger,
    watch: Stopwatch,
    threshold_ms: int,
    *,
    what: str,
    extra: str = "",
) -> bool:
    """超过阈值时记一条 WARNING，返回是否超过。

    日志统一带上 label / 耗时 / 阈值，便于用 grep 直接筛出慢的链路。
    """
    elapsed = watch.elapsed_ms
    if elapsed <= threshold_ms:
        return False

    suffix = f" {extra}" if extra else ""
    logger.warning(
        "%s 耗时 %dms，超过阈值 %dms（%s）%s",
        what,
        elapsed,
        threshold_ms,
        watch.label or "unlabeled",
        suffix,
    )
    return True
