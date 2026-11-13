"""慢请求日志的异步批量落库。

为什么要异步
------------
慢请求往往发生在数据库**本身就慢**的时候。此时再同步 INSERT 一条日志，
等于在最脆弱的时候再加一根稻草：一次慢查询引发的慢请求，会变成一次
新的写事务，进而拖慢其它请求，形成正反馈。日志是"事后排查用的旁证"，
它的价值远低于"当前这个请求能不能正常返回"，所以绝不该让它决定请求的耗时。

做法
----
- 请求线程只做一次无阻塞的 ``put_nowait``；队列满了就丢弃并计数。
  记不下日志，远好过拖慢一个正在服务的请求。
- 后台单线程按「攒够 ``SLOW_LOG_BATCH_SIZE`` 条」或「每
  ``SLOW_LOG_FLUSH_SECONDS`` 秒」刷一次，把 N 次写事务合并成一次
  ``bulk_create``。
- 写失败只记日志，**不重试、不回队**：数据库都写不进去了，重试只会把
  队列堆满，然后让"丢弃"从异常变成常态。丢弃是有上限的代价，堆积不是。

测试环境
--------
``RUNNING_TESTS`` 下强制同步写。pytest 的每个用例都跑在事务里，后台线程
既看不到未提交的数据、又会在用例回滚后拿到已消失的表；异步只会让断言
随机失败，测不出任何真实问题。
"""

from __future__ import annotations

import atexit
import logging
import queue
import threading

from django.conf import settings

logger = logging.getLogger(__name__)

# 队列里的每条记录就是一个待插入的字段字典
_QUEUE: "queue.Queue[dict]" = queue.Queue(
    maxsize=int(getattr(settings, "SLOW_LOG_QUEUE_SIZE", 2000))
)

_stop = threading.Event()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_atexit_registered = False
_dropped = 0
_dropped_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def submit(record: dict) -> None:
    """投递一条慢请求记录。永不阻塞、永不抛异常。"""
    if _write_sync():
        _write_batch([record])
        return

    try:
        _QUEUE.put_nowait(record)
    except queue.Full:
        _count_drop()
        return

    _ensure_worker()


def flush() -> int:
    """把队列里现有的记录立刻写掉，返回写了多少条。

    用于进程退出与测试；正常运行不需要调用。这里在**当前线程**写，
    不和后台线程抢同一批数据——队列本身线程安全，谁取到谁写，
    不会重复插入。
    """
    drained = _drain()
    if not drained:
        return 0
    _write_batch(drained)
    return len(drained)


def shutdown(timeout: float = 2.0) -> None:
    """停掉后台线程并尽力写完剩余记录（进程退出时调用）。"""
    _stop.set()
    worker = _worker
    if worker is not None and worker.is_alive():
        worker.join(timeout=timeout)
    flush()


def drop_count() -> int:
    """累计因队列满而丢弃的条数，供健康检查/监控读取。"""
    with _dropped_lock:
        return _dropped


def reset_for_tests() -> None:
    """清空队列与丢弃计数（仅供测试使用）。"""
    global _dropped
    _drain()
    with _dropped_lock:
        _dropped = 0
    _stop.clear()


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _write_sync() -> bool:
    """是否同步写。

    测试环境强制同步；生产可通过 SLOW_LOG_ASYNC=False 关掉异步（排障用）。
    """
    if getattr(settings, "RUNNING_TESTS", False):
        return True
    return not bool(getattr(settings, "SLOW_LOG_ASYNC", True))


def _count_drop() -> None:
    global _dropped
    with _dropped_lock:
        _dropped += 1
        # 只在整百处告警，避免队列持续满时把日志本身刷爆
        if _dropped % 100 == 1:
            logger.warning(
                "慢请求日志队列已满，累计丢弃 %d 条（写入跟不上时属预期降级）",
                _dropped,
            )


def _drain(limit: int | None = None) -> list[dict]:
    """把队列里现有的记录取出来，最多 ``limit`` 条。"""
    items: list[dict] = []
    while limit is None or len(items) < limit:
        try:
            items.append(_QUEUE.get_nowait())
        except queue.Empty:
            break
    return items


def _write_batch(records: list[dict]) -> None:
    """批量写库。失败只记日志，绝不上抛。"""
    if not records:
        return

    try:
        from apps.metrics.models import SlowRequestLog

        SlowRequestLog.objects.bulk_create(
            [SlowRequestLog(**record) for record in records]
        )
    except Exception:  # noqa: BLE001 - 监控写入失败绝不能影响业务
        logger.exception("批量写入慢请求日志失败，丢弃 %d 条", len(records))


def _ensure_worker() -> None:
    """确保后台线程已启动（幂等）。"""
    global _worker, _atexit_registered
    if _worker is not None and _worker.is_alive():
        return

    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _stop.clear()
        _worker = threading.Thread(target=_run, name="slow-request-log", daemon=True)
        _worker.start()
        if not _atexit_registered:
            # daemon 线程在进程退出时可能被直接杀掉，来不及写剩余记录，
            # 所以再挂一个 atexit 兜底。
            atexit.register(shutdown)
            _atexit_registered = True
        logger.info(
            "慢请求日志后台写入线程已启动（批大小 %d，最长间隔 %ss）",
            _batch_size(),
            _flush_seconds(),
        )


def _batch_size() -> int:
    return max(1, int(getattr(settings, "SLOW_LOG_BATCH_SIZE", 50)))


def _flush_seconds() -> float:
    return max(0.1, float(getattr(settings, "SLOW_LOG_FLUSH_SECONDS", 5.0)))


def _run() -> None:
    """后台循环：攒批或超时即写。"""
    from common.sqlite_writes import close_thread_connections

    batch_size = _batch_size()
    # 单次等待上限压在 1 秒内：shutdown() 只 join 2 秒，
    # 若一次就阻塞 5 秒，退出时必然等不到它收尾。
    wait_seconds = min(1.0, _flush_seconds())

    try:
        while not _stop.is_set():
            try:
                first = _QUEUE.get(timeout=wait_seconds)
            except queue.Empty:
                continue

            batch = [first]
            # 已有一条就顺手再取，最多取满一批。用 get_nowait 立即返回，
            # 不为"凑满一批"而等待——攒批的目的是合并事务，不是攒够才写。
            while len(batch) < batch_size:
                try:
                    batch.append(_QUEUE.get_nowait())
                except queue.Empty:
                    break

            _write_batch(batch)
    finally:
        # 线程退出前把剩下的尽力写完。
        _write_batch(_drain())
        close_thread_connections()
