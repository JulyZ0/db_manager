"""运行日志的保留期清理。

问题背景
--------
项目里有三张「只增不减」的表：

- ``metrics_slow_request_log``   慢请求记录，每次超阈值写一条
- ``dbconsole_statement_audit_log`` 控制台语句审计，每执行一条写一条
- ``assets_password_rotation_log``  密码轮换历史

它们都没有天然的生命周期终点。生产跑上一年，审计表动辄百万行，
后果不只是占磁盘：

1. 列表页的分页查询在 ``ORDER BY created_at DESC`` 上越来越慢；
2. 备份/恢复时间被日志表主导；
3. 真正需要保留的近期记录淹没在历史里，排障时反而找不到。

因此按保留期裁剪：默认 90 / 180 / 365 天，全部可用环境变量调整，
置 0 表示永久保留（合规场景可能需要）。

设计要点
--------
- **分批删除**：一次 ``DELETE`` 百万行会长时间持有写锁，
  在 SQLite 上直接卡死整个站点。这里每批 ``LOG_CLEANUP_BATCH_SIZE`` 条，
  批间让出锁。
- **有上限**：``LOG_CLEANUP_MAX_BATCHES`` 限制单次运行的总批数，
  宁可下次再删，也不让清理任务本身成为慢任务。
- **可预演**：``dry_run=True`` 只统计会删多少条，便于运维先看一眼。
- **幂等**：重复执行无副作用，删除条件只依赖 ``created_at``。

清理掉轮换历史不会影响轮换调度——「上次执行时间」记在
``RotationPolicy.last_run_at`` 上，与日志表无关。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from common.sqlite_writes import write_lock
from common.timing import Stopwatch, log_if_slow

logger = logging.getLogger(__name__)

# 清理属于后台维护任务，超过这个耗时说明批大小或索引需要调整
SLOW_CLEANUP_MS = 60_000

DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_BATCHES = 200


def _resolve_days(setting_name: str, default: int) -> int:
    """读取保留天数。非法值退回默认，负数按 0（永久保留）处理。"""
    raw = getattr(settings, setting_name, default)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning("配置 %s 不是整数（%r），按默认 %d 天处理", setting_name, raw, default)
        return default


def _delete_in_batches(model, cutoff, batch_size: int, max_batches: int) -> int:
    """按主键顺序分批删除 ``created_at < cutoff`` 的记录，返回实际删除行数。

    为什么要显式 ``order_by("pk")``：这几张表的 ``Meta.ordering`` 是
    ``["-created_at", "-id"]``，直接切片会让数据库每批都对全表排序；
    按主键升序则能顺序扫描。被删掉的行不会再次出现，所以每轮切片都会推进。

    锁只覆盖「单批删除」，不覆盖整个循环：一次清理可能要删几十万行、
    跑上几分钟，若整段持锁，站点在这期间完全无法写入。每批之间让出锁，
    其它写操作就能插进来。
    """
    queryset = model.objects.filter(created_at__lt=cutoff).order_by("pk")
    deleted_total = 0

    for _ in range(max_batches):
        ids = list(queryset.values_list("pk", flat=True)[:batch_size])
        if not ids:
            break

        with write_lock():
            deleted, _per_model = model.objects.filter(pk__in=ids).delete()
        deleted_total += deleted

        # 不足一批说明已经删到底了，省掉一次空查询
        if len(ids) < batch_size:
            break

    return deleted_total


def _count_expired(model, cutoff) -> int:
    return model.objects.filter(created_at__lt=cutoff).count()


# 表名 → (模型路径, 保留天数配置项, 默认天数)
# 用字符串路径而不是直接 import 模型：本模块会被 tasks 与管理命令共同引用，
# 延迟导入可避免 app 加载期的循环依赖。
_RETENTION_PLAN = (
    (
        "slow_request_log",
        "apps.metrics.models.SlowRequestLog",
        "SLOW_LOG_RETENTION_DAYS",
        90,
    ),
    (
        "statement_audit_log",
        "apps.dbconsole.models.StatementAuditLog",
        "AUDIT_LOG_RETENTION_DAYS",
        180,
    ),
    (
        "password_rotation_log",
        "apps.assets.models.PasswordRotationLog",
        "ROTATION_LOG_RETENTION_DAYS",
        365,
    ),
)


def _load_model(dotted_path: str):
    module_path, _, class_name = dotted_path.rpartition(".")
    module = __import__(module_path, fromlist=[class_name])
    return getattr(module, class_name)


def cleanup_expired_logs(*, dry_run: bool = False) -> dict[str, object]:
    """按保留期清理三张日志表，返回每张表的处理结果。

    返回结构::

        {
            "dry_run": False,
            "duration_ms": 1234,
            "tables": {
                "slow_request_log": {"days": 90, "deleted": 1000},
                ...
            },
            "total_deleted": 3000,
        }

    单表失败不阻断其它表：日志清理是维护性任务，
    某张表因为锁冲突删不动，不该把另外两张也一起放弃。
    """
    watch = Stopwatch("task:cleanup_expired_logs")
    now = timezone.now()
    batch_size = getattr(settings, "LOG_CLEANUP_BATCH_SIZE", DEFAULT_BATCH_SIZE)
    max_batches = getattr(settings, "LOG_CLEANUP_MAX_BATCHES", DEFAULT_MAX_BATCHES)

    tables: dict[str, dict[str, object]] = {}
    total_deleted = 0

    # 注意这里**不**加 write_lock()：单批删除内部已经加锁，
    # 若把整个循环包进锁里，清理期间站点完全无法写入。
    for label, dotted_path, setting_name, default_days in _RETENTION_PLAN:
        days = _resolve_days(setting_name, default_days)
        if days <= 0:
            tables[label] = {"days": 0, "deleted": 0, "skipped": "永久保留"}
            logger.debug("表 %s 配置为永久保留，跳过清理", label)
            continue

        cutoff = now - timedelta(days=days)
        try:
            model = _load_model(dotted_path)
            if dry_run:
                deleted = _count_expired(model, cutoff)
            else:
                deleted = _delete_in_batches(
                    model, cutoff, batch_size, max_batches
                )
        except Exception:  # noqa: BLE001 - 单表失败不影响其它表
            logger.exception("清理 %s 失败，本次跳过", label)
            tables[label] = {"days": days, "deleted": 0, "error": True}
            continue

        tables[label] = {"days": days, "deleted": deleted}
        total_deleted += deleted

        if deleted:
            logger.info(
                "清理 %s：删除 %s 条（保留 %d 天，截止 %s）",
                label,
                deleted,
                days,
                cutoff.strftime("%Y-%m-%d"),
            )

    elapsed_ms = watch.stop()
    log_if_slow(
        logger,
        watch,
        SLOW_CLEANUP_MS,
        what="日志清理",
        extra="建议调小 LOG_CLEANUP_BATCH_SIZE 或加索引",
    )

    return {
        "dry_run": dry_run,
        "duration_ms": elapsed_ms,
        "tables": tables,
        "total_deleted": total_deleted,
    }
