"""运行期基础设施应用。

职责：在 Web 进程启动时决定是否拉起进程内调度器。
单独建一个 app 而不是塞进 assets/metrics，是为了让「启动期副作用」
只有一个入口，排查问题时不用到处找 ready()。
"""

from __future__ import annotations

import logging
import os
import sys

from django.apps import AppConfig

logger = logging.getLogger(__name__)

# 这些 manage.py 子命令属于一次性操作，不该启动后台调度线程
_SKIP_COMMANDS = {
    "migrate",
    "makemigrations",
    "showmigrations",
    "sqlmigrate",
    "collectstatic",
    "shell",
    "shell_plus",
    "dbshell",
    "createsuperuser",
    "changepassword",
    "dumpdata",
    "loaddata",
    "test",
    "check",
    "flush",
    "aggregate_instance_stats",
}


def is_celery_process(argv: list[str] | None = None) -> bool:
    """判断当前进程是不是 celery 的 worker / beat。

    为什么必须排除
    --------------
    celery 进程同样会加载 Django（`django.setup()`），于是 AppConfig.ready()
    也会跑。若不排除，worker 进程里会**再起一个进程内调度器**，同一批任务
    被执行两遍：轮换会给同一个实例改两次密码、采集会写两份指标、告警会
    重复触发。实测日志里就能看到 worker 打出「进程内调度器已启动」。

    为什么用 argv[0] 而不是环境变量
    ------------------------------
    实测本项目的 Django setup 阶段 `CELERY_WORKER_RUNNING` 仍为 ``None``，
    据此判断会漏掉；而 ``sys.argv[0]`` 在 worker / beat 下都是
    ``<venv>/Scripts/celery.exe``（或 ``celery``），稳定可判。
    """
    argv = list(sys.argv if argv is None else argv)
    if not argv:
        return False

    entry = os.path.basename(argv[0]).lower()
    if entry.endswith(".exe"):
        entry = entry[: -len(".exe")]
    if entry == "celery":
        return True
    # 兼容 python -m celery worker：此时 argv[0] 是 celery 包内的 __main__.py
    parent = os.path.basename(os.path.dirname(argv[0])).lower()
    return entry in ("__main__.py", "__main__") and parent == "celery"


def should_start_scheduler() -> bool:
    """判断当前进程是否应该拉起调度器。

    需要排除的情况：
    1. celery worker / beat 进程 —— 它们本身就是调度执行方。
    2. pytest 进程 —— 测试不应有后台任务干扰断言。
    3. runserver 的 autoreload 父进程 —— 它会 fork 出子进程，
       父进程若也起调度，任务会被执行两次。
    4. 各种一次性管理命令 —— 迁移时数据库结构都可能还不完整。
    5. 用户显式用 DISABLE_SCHEDULER=1 关闭。
    """
    if os.environ.get("DISABLE_SCHEDULER", "").strip().lower() in ("1", "true", "yes"):
        return False

    # 必须放在最前面：celery 进程也会走 AppConfig.ready()，
    # 不先拦住就会在 worker 里重复启动进程内调度器。
    if is_celery_process():
        return False

    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return False

    argv = list(sys.argv)
    if len(argv) > 1:
        command = argv[1]
        if command == "runserver":
            # --noreload 时没有子进程，RUN_MAIN 不会出现
            if "--noreload" in argv:
                return True
            return os.environ.get("RUN_MAIN") == "true"
        if command in _SKIP_COMMANDS:
            return False
        # 未知的 manage.py 子命令，保守起见不启动
        if argv[0].endswith("manage.py"):
            return False

    # WSGI / ASGI / 自定义启动器（launcher.py）场景
    return True


class RuntimeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.runtime"
    verbose_name = "运行期基础设施"

    def ready(self) -> None:
        # 登录成功/失败的记账信号。必须无条件注册：它只在真正发生登录时
        # 才动缓存，与调度器无关，不该跟着 should_start_scheduler() 一起跳过。
        # 不导入就不会注册（Django 靠 import 副作用连接 receiver）。
        from common import auth_signals  # noqa: F401

        if not should_start_scheduler():
            return

        # 延迟导入，避免 app 注册阶段就触碰 settings 之外的东西
        from common.scheduler import start_scheduler_if_needed

        try:
            mode = start_scheduler_if_needed()
        except Exception:  # noqa: BLE001 - 调度失败不应导致 Web 服务起不来
            logger.exception("启动调度器失败，定时任务将不会执行")
            return

        logger.info("调度模式已确定：%s", mode)
