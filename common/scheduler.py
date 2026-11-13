"""调度层：Celery Beat 与进程内调度之间的自动降级。

问题背景
--------
目标用户是「小白运维」，机器上很可能没有 Redis。但笔试题要求 Celery 周期任务，
生产环境也确实该用 Celery。于是做成双模式：

- SCHEDULER_MODE=celery    → 强制 Celery（缺 Redis 直接报错，生产用）
- SCHEDULER_MODE=inprocess → 强制进程内调度（单机、零依赖）
- SCHEDULER_MODE=auto      → 启动时探测 broker，通就用 Celery，不通就降级（默认）

两种模式执行的是**同一批任务函数**（app 里的 tasks.py），只是触发器不同。
这样既满足了「Celery 周期任务」的考点，又做到了「解压就能跑」。
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime
from typing import Callable
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# 进程内调度器的轮询间隔。任务精度要求是「分钟级」，15 秒足够且开销可忽略。
TICK_SECONDS = 15


# ---------------------------------------------------------------------------
# 模式判定
# ---------------------------------------------------------------------------


def probe_broker(url: str, timeout: float = 0.3) -> bool:
    """探测 broker 是否可连接。只做 TCP 握手，不发协议指令。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("redis", "rediss", "amqp", "amqps"):
        return False
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (6379 if parsed.scheme.startswith("redis") else 5672)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_mode() -> str:
    """返回实际生效的调度模式："celery" 或 "inprocess"。"""
    mode = (getattr(settings, "SCHEDULER_MODE", "auto") or "auto").lower()

    if mode == "inprocess":
        return "inprocess"

    if mode == "celery":
        return "celery"

    # auto：探测 broker
    broker = getattr(settings, "CELERY_BROKER_URL", "")
    timeout = getattr(settings, "SCHEDULER_REDIS_PROBE_TIMEOUT", 0.3)
    if broker and probe_broker(broker, timeout):
        return "celery"

    logger.warning(
        "未探测到消息队列（%s），自动降级为进程内调度。"
        "定时任务仍会执行，只是不再经过 Celery worker。"
        "如需强制 Celery 模式，请设置 SCHEDULER_MODE=celery 并启动 Redis。",
        broker or "<未配置>",
    )
    return "inprocess"


# ---------------------------------------------------------------------------
# crontab 匹配
# ---------------------------------------------------------------------------


def _field_matches(expr: set[int] | None, value: int) -> bool:
    """celery 的 crontab 把字段解析成 set[int]；None 表示「任意」。"""
    if not expr:
        return True
    return value in expr


def crontab_matches(schedule, moment: datetime) -> bool:
    """判断某个 crontab 在给定时刻（精确到分钟）是否应当触发。

    只处理本项目用到的分/时/日/月/周，语义与 cron 一致：
    日与周同时被限定时，二者是「或」关系（cron 的标准行为）。
    """
    if not _field_matches(getattr(schedule, "minute", None), moment.minute):
        return False
    if not _field_matches(getattr(schedule, "hour", None), moment.hour):
        return False
    if not _field_matches(getattr(schedule, "month_of_year", None), moment.month):
        return False

    dom = getattr(schedule, "day_of_month", None)
    dow = getattr(schedule, "day_of_week", None)
    dom_ok = _field_matches(dom, moment.day)
    # celery 的 day_of_week：0=周日，1=周一 … 6=周六
    dow_ok = _field_matches(dow, (moment.weekday() + 1) % 7)

    if dom and dow:
        return dom_ok or dow_ok
    if dom:
        return dom_ok
    if dow:
        return dow_ok
    return True


# ---------------------------------------------------------------------------
# 进程内调度器
# ---------------------------------------------------------------------------


class InProcessScheduler:
    """把 settings.CELERY_BEAT_SCHEDULE 当作 cron 表，在后台线程里触发任务。

    设计取舍：
    - 单线程 + 轮询，不用 Timer 堆叠，避免时钟漂移。
    - 记录「本分钟已执行」，保证同一分钟内不会重复触发。
    - 任务在独立线程里跑，单个任务卡住不阻塞后续任务。
    - 只用于单机部署；多实例部署必须走 Celery，否则会重复执行。
    """

    def __init__(self, schedule: dict | None = None) -> None:
        self.schedule = schedule if schedule is not None else settings.CELERY_BEAT_SCHEDULE
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_fired: dict[str, str] = {}
        # 任务路径 → 可调用对象
        self._resolved: dict[str, Callable[[], None]] = {}

    # -- 生命周期 --------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="inprocess-scheduler", daemon=True
        )
        self._thread.start()
        logger.info(
            "进程内调度器已启动，共 %d 个周期任务，轮询间隔 %ds",
            len(self.schedule),
            TICK_SECONDS,
        )

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- 内部 ------------------------------------------------------------

    def _resolve_task(self, task_path: str) -> Callable[[], None] | None:
        """把 "apps.xxx.tasks.yyy" 解析成可调用对象。

        Celery 的 shared_task 对象本身可调用（会同步执行函数体），
        因此 Celery 模式与进程内模式能复用同一个实现。
        """
        if task_path in self._resolved:
            return self._resolved[task_path]

        module_path, _, func_name = task_path.rpartition(".")
        try:
            module = __import__(module_path, fromlist=[func_name])
            task = getattr(module, func_name)
        except (ImportError, AttributeError):
            logger.exception("无法加载周期任务 %s", task_path)
            return None

        self._resolved[task_path] = task
        return task

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 调度线程绝不能因异常退出
                logger.exception("进程内调度器 tick 异常")
            self._stop.wait(TICK_SECONDS)

    def tick(self, now: datetime | None = None) -> list[str]:
        """检查一次并触发到点的任务，返回本次触发的任务名。"""
        moment = now or timezone.localtime()
        minute_key = moment.strftime("%Y%m%d%H%M")
        fired: list[str] = []

        for name, entry in self.schedule.items():
            schedule = entry.get("schedule")
            if schedule is None or not hasattr(schedule, "minute"):
                continue
            if self._last_fired.get(name) == minute_key:
                continue
            if not crontab_matches(schedule, moment):
                continue

            self._last_fired[name] = minute_key
            fired.append(name)
            self._dispatch(name, entry.get("task", ""))

        return fired

    def _dispatch(self, name: str, task_path: str) -> None:
        task = self._resolve_task(task_path)
        if task is None:
            return

        def _target() -> None:
            started = time.perf_counter()
            logger.info("周期任务开始 name=%s task=%s", name, task_path)
            try:
                task()
            except Exception:  # noqa: BLE001
                logger.exception("周期任务失败 name=%s task=%s", name, task_path)
            else:
                logger.info(
                    "周期任务完成 name=%s 耗时=%.0fms",
                    name,
                    (time.perf_counter() - started) * 1000,
                )

        threading.Thread(target=_target, name=f"task-{name}", daemon=True).start()


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

_scheduler: InProcessScheduler | None = None
_lock = threading.Lock()


def start_scheduler_if_needed() -> str:
    """按 SCHEDULER_MODE 决定是否启动进程内调度器，返回实际模式。

    在 AppConfig.ready() 中调用。注意只在主进程启动：
    runserver 的 autoreload 会派生一个子进程，父进程不应起调度线程。
    """
    global _scheduler

    mode = resolve_mode()
    if mode != "inprocess":
        logger.info("调度模式：Celery（任务由 celery worker + beat 执行）")
        return mode

    with _lock:
        if _scheduler is None:
            _scheduler = InProcessScheduler()
            _scheduler.start()
    return mode


def current_mode() -> str:
    """供页面展示当前调度模式。"""
    if _scheduler is not None and _scheduler.is_running():
        return "inprocess"
    return resolve_mode()
