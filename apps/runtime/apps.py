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


def should_start_scheduler() -> bool:
    """判断当前进程是否应该拉起调度器。

    需要排除的情况：
    1. pytest 进程 —— 测试不应有后台任务干扰断言。
    2. runserver 的 autoreload 父进程 —— 它会 fork 出子进程，
       父进程若也起调度，任务会被执行两次。
    3. 各种一次性管理命令 —— 迁移时数据库结构都可能还不完整。
    4. 用户显式用 DISABLE_SCHEDULER=1 关闭。
    """
    if os.environ.get("DISABLE_SCHEDULER", "").strip().lower() in ("1", "true", "yes"):
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
