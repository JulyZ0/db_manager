"""SQLite 并发写入稳定化。

为什么需要这个模块
------------------
SQLite 同一时刻只允许**一个写事务**。默认配置下有两个坑：

1. **锁升级导致的假超时**：事务若先读后写，SQLite 必须在写的那一刻把
   共享锁升级为排他锁。若此时别的连接已经写过，升级会**立刻**返回
   ``SQLITE_BUSY``，并且完全不理会 ``busy_timeout``——等待时长形同虚设。
   解决办法是让事务一开始就用 ``BEGIN IMMEDIATE`` 拿到写意图锁
   （由 Django 的 ``OPTIONS["transaction_mode"]`` 控制）。

2. **并发写不升反降**：即使有 ``busy_timeout``，多线程同时写也只是把
   排队时间拉长。本机实测 40 次写事务：

   ==========  ==========
   并发度      总耗时
   ==========  ==========
   1           0.25s
   4           0.35s
   8           0.58s
   ==========  ==========

   因此进程内再用一把可重入锁把写操作串成队，让它们有序排队，
   而不是互相抢锁后各自重试。

另外开启 WAL：读不再阻塞写、写不再阻塞读，浏览页面与批量轮换可以并行。
``synchronous=NORMAL`` 是 WAL 下的常规搭配，牺牲"掉电可能丢最后一笔事务"
换取写吞吐；对账类场景可改成 ``FULL``。

使用方式
--------
- 连接参数：``DATABASES["default"]["OPTIONS"]`` 里放 :func:`build_options`
- 业务写入：``with write_lock(): ...`` 串行化
- 偶发锁冲突：``retry_on_locked(func, ...)`` 带抖动退避重试
"""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time
from typing import Any, Callable, Iterator, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_BUSY_TIMEOUT_MS = 5_000

# SQLite 锁冲突时异常消息里的特征串（不同版本措辞略有差异）
_LOCKED_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)

# 进程内写锁：可重入，允许 rotate_one 在 rotate_all 的锁内再取一次
_WRITE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 连接参数
# ---------------------------------------------------------------------------


def build_options(
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    journal_mode: str = "WAL",
    synchronous: str = "NORMAL",
) -> dict[str, Any]:
    """构造 ``DATABASES[...]["OPTIONS"]``。

    纯标准库实现、不导入 django.db，因此可以在 settings 加载期安全调用。

    - ``timeout``：传给 ``sqlite3.connect`` 的秒数，与 ``busy_timeout`` 一致。
    - ``init_command``：每条新连接都会执行，用分号分隔多条 PRAGMA。
    - ``transaction_mode``：``IMMEDIATE``，避免读写事务的锁升级假超时。
    """
    pragmas = [
        f"PRAGMA busy_timeout = {int(busy_timeout_ms)}",
        f"PRAGMA journal_mode = {journal_mode}",
        f"PRAGMA synchronous = {synchronous}",
        # 临时表放内存，聚合统计（GROUP BY）会快一些
        "PRAGMA temp_store = MEMORY",
    ]
    return {
        "timeout": busy_timeout_ms / 1000,
        "init_command": ";".join(pragmas),
        "transaction_mode": "IMMEDIATE",
    }


def is_sqlite(alias: str = "default") -> bool:
    """当前默认库是不是 SQLite。"""
    from django.db import connections

    try:
        return connections[alias].vendor == "sqlite"
    except Exception:  # noqa: BLE001 - 连接尚未配置时按非 SQLite 处理
        return False


# ---------------------------------------------------------------------------
# 写串行化
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def write_lock(alias: str = "default") -> Iterator[None]:
    """把写操作串行化；非 SQLite 后端直接放行。

    为什么用锁而不是只依赖 ``busy_timeout``：超时只是"排队等待"，
    等待期间线程仍然占着连接与内存；而有序排队没有重试成本，
    也不会出现"谁等得久谁先成功"的不公平。
    """
    if not is_sqlite(alias):
        yield
        return

    with _WRITE_LOCK:
        yield


# ---------------------------------------------------------------------------
# 锁冲突重试
# ---------------------------------------------------------------------------


def is_locked_error(exc: BaseException) -> bool:
    """判断是不是 SQLite 的锁冲突异常。"""
    from django.db import OperationalError

    if not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in _LOCKED_MARKERS)


def retry_on_locked(
    func: Callable[..., T],
    *args: Any,
    attempts: int = 5,
    base_delay: float = 0.05,
    max_delay: float = 1.0,
    **kwargs: Any,
) -> T:
    """执行 ``func``；仅在遇到 SQLite 锁冲突时带指数退避重试。

    其它异常（校验失败、约束冲突等）原样抛出——重试它们只会放大问题。

    抖动取 ``0.5~1.5`` 倍，避免多个 worker 在同一毫秒齐步重试形成惊群。
    """
    if attempts < 1:
        raise ValueError("attempts 必须是正整数")

    last_exc: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return func(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 只重新抛出非锁冲突异常
            if not is_locked_error(exc):
                raise
            last_exc = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay *= random.uniform(0.5, 1.5)
            logger.warning(
                "SQLite 写锁冲突，%.0fms 后重试（第 %d 次，共 %d 次）",
                delay * 1000,
                attempt,
                attempts - 1,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# 线程收尾
# ---------------------------------------------------------------------------


def close_thread_connections() -> None:
    """关闭**当前线程**持有的数据库连接。

    并发轮换用线程池时，每个工作线程会各自建立连接；线程结束后若不复位，
    连接会一直挂在线程局部变量上，直到线程对象被回收。
    """
    from django.db import connections

    try:
        connections.close_all()
    except Exception:  # noqa: BLE001 - 收尾失败不应影响任务结果
        logger.debug("关闭线程数据库连接失败", exc_info=True)
