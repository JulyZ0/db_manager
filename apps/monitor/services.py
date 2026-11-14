"""采集编排、落库与保留期清理。

三层职责分开
------------
- ``apps.monitor.collectors.*``：**纯取数**。一个实例 → 一批 ``Sample``，
  不碰数据库（不写库、不读库），因此可以脱离 Django ORM 单独测试。
- 本模块：**编排**。挑选实例、解密密码、调用采集器、算速率、批量落库、
  记录 ``CollectionRun``。
- ``apps.monitor.tasks``：**Celery/进程内调度的薄包装**，只做重试与日志。

速率为什么在这里算
------------------
采集器只落累计计数器（``queries_total`` 等）。速率需要「上一次的计数器 +
时间差」，这是一次**跨采样的读库**，属于编排职责。放在采集器里会让纯函数
变成有状态对象，多进程并发采集时会互相污染。

速率窗口由 :data:`apps.monitor.collectors.base.MIN_RATE_INTERVAL` /
``MAX_RATE_INTERVAL`` 约束：间隔太短（抖动放大）或太长（算出来是长窗口
平均值，不是"当前速率"）时直接跳过，不产出误导性的点。

写库为什么走 write_lock
-----------------------
一轮采集对 N 个实例各写 M 条样本，是本项目里最密集的写入方之一。
SQLite 上并发写会直接锁冲突，因此与 ``apps/metrics`` 的清理任务一样，
统一用 :func:`common.sqlite_writes.write_lock` 串行化——非 SQLite 后端
该函数直接放行，没有额外成本。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.assets.models import Instance
from apps.dbconsole.services import classify_error
from apps.monitor.collectors import collect_for
from apps.monitor.collectors.base import Sample
from apps.monitor.models import CollectionRun, MetricSample
from common.sqlite_writes import write_lock
from common.timing import Stopwatch

logger = logging.getLogger(__name__)

# 单次批量插入的行数上限。一轮采集可能有上千条样本，一次 bulk_create
# 会把 SQL 变量数顶到 SQLite 的 999 上限（Django 会分批，但分批过大
# 仍会长时间持锁），这里主动切块。
BULK_CHUNK = 500

# 需要换算速率的累计计数器 → 速率指标名
_RATE_METRICS = {
    "mysql.queries_total": "mysql.qps",
    "mysql.slow_queries_total": "mysql.slow_queries_per_sec",
    "mysql.innodb_row_lock_waits_total": "mysql.row_lock_waits_per_sec",
    "pg.xact_commit_total": "pg.tps",
    "pg.xact_rollback_total": "pg.rollback_per_sec",
    "pg.deadlocks_total": "pg.deadlocks_per_sec",
    "redis.commands_processed_total": "redis.commands_per_sec",
    "redis.keyspace_hits_total": "redis.hits_per_sec",
    "redis.keyspace_misses_total": "redis.misses_per_sec",
    "redis.evicted_keys_total": "redis.evicted_per_sec",
    "redis.expired_keys_total": "redis.expired_per_sec",
}


def _setting(name: str, default):
    return getattr(settings, name, default)


def _fetch_instance(
    instance: Instance, now: datetime, timeout: int
) -> tuple[bool, str, str, list[Sample], int, list[tuple]]:
    """只做取数，不碰数据库。

    **必须在子线程里不写库**：Django 的数据库连接是每线程一份，
    SQLite 内存库在子线程里根本看不到表，文件库则会各自争抢写锁。
    因此并发只覆盖「网络 IO + 解析」这一段（这正是采集的耗时所在），
    落库统一回到主线程串行执行。

    返回 ``(ok, error_code, message, samples, duration_ms, sql_rows)``。

    ``sql_rows`` 是同一趟连接顺带取回的语句统计（SQL 洞察用）。它与指标
    采集共用一个密码、一次并发调度，但**独立成败**：语句统计取不到
    （例如 pg_stat_statements 未安装）不应该让指标采集也记成失败。
    """
    from apps.dbconsole.services import resolve_password

    watch = Stopwatch(f"monitor:fetch:{instance.id}")

    password, error = resolve_password(instance)
    if error:
        return False, error, "无法取得密码，跳过采集", [], watch.stop(), []

    try:
        result = collect_for(instance, password, timeout)
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return (
            False,
            classify_error(exc),
            f"采集器异常：{type(exc).__name__}",
            [],
            watch.stop(),
            [],
        )

    sql_rows: list[tuple] = []
    if result.ok and _setting("MONITOR_SQL_INSIGHT_ENABLED", True):
        # 语句统计取不到不影响指标：它是附加视图，不是采集的前提。
        from apps.monitor.sql_collector import fetch_sql_stats

        fetched = fetch_sql_stats(instance, password, timeout)
        if fetched.get("ok"):
            sql_rows = fetched.get("rows") or []

    return (
        result.ok,
        result.error_code,
        result.hint,
        list(result.samples) if result.ok else [],
        watch.stop(),
        sql_rows,
    )


def collect_instance(instance: Instance, now: datetime | None = None) -> CollectionRun:
    """采集单个实例并落库，返回写入的采集记录。

    任何失败都转成 ``CollectionRun(ok=False, error_code=...)``，不向上抛：
    一轮采集里某个实例挂掉，不能让其它实例的采集一起中断。
    """
    now = now or timezone.now()
    timeout = int(_setting("MONITOR_TIMEOUT_SECONDS", 5))

    ok, error_code, message, samples, duration_ms, sql_rows = _fetch_instance(
        instance, now, timeout
    )

    if ok and samples:
        samples.extend(_compute_rate_samples(instance, samples, now))
        _persist_samples(instance, samples, now)

    if ok and sql_rows:
        _persist_sql(instance, sql_rows)

    return _save_run(
        instance,
        now,
        duration_ms,
        ok=ok,
        error_code=error_code,
        message=message,
        metrics_count=len(samples) if ok else 0,
    )


def _save_run(
    instance: Instance,
    now: datetime,
    duration_ms: int,
    *,
    ok: bool,
    error_code: str = "",
    message: str = "",
    metrics_count: int = 0,
) -> CollectionRun:
    """写采集记录。

    ``message`` 截断到 500（模型字段上限）。截断放在最后一刻，
    既不丢上下文，也不会因超长导致整批写入失败。
    """
    with write_lock():
        return CollectionRun.objects.create(
            instance=instance,
            collected_at=now,
            ok=ok,
            error_code=error_code or "",
            message=(message or "")[:500],
            duration_ms=max(0, duration_ms),
            metrics_count=max(0, metrics_count),
        )


def _compute_rate_samples(
    instance: Instance, samples: list[Sample], now: datetime
) -> list[Sample]:
    """为累计计数器补上速率样本。

    上一次的计数器从库里按指标名各取最近一条。用一次 ``filter(metric__in=...)``
    加 Python 归组，而不是逐个指标查一次——后者在实例多、指标多时是明显的 N+1。
    """
    from apps.monitor.collectors.base import compute_rate

    wanted = [s.metric for s in samples if s.metric in _RATE_METRICS]
    if not wanted:
        return []

    previous: dict[str, tuple[float, datetime]] = {}
    rows = (
        MetricSample.objects.filter(instance=instance, metric__in=set(wanted))
        .order_by("collected_at")
        .values_list("metric", "value", "collected_at")
    )
    for metric, value, collected_at in rows:
        # 顺序遍历，最后一条即最近一次
        previous[metric] = (value, collected_at)

    extra: list[Sample] = []
    for sample in samples:
        rate_metric = _RATE_METRICS.get(sample.metric)
        if rate_metric is None:
            continue
        prev = previous.get(sample.metric)
        if prev is None:
            continue
        rate = compute_rate(prev[0], prev[1], sample.value, now)
        if rate is None:
            continue
        extra.append(
            Sample(metric=rate_metric, value=rate, dimension=sample.dimension)
        )
    return extra


def _persist_samples(instance: Instance, samples: list[Sample], now: datetime) -> None:
    """批量写入采样，按 BULK_CHUNK 切块。

    一次 ``bulk_create`` 比逐条 ``create`` 快一个数量级；
    切块是为了不让单个事务持有写锁太久（SQLite 上会阻塞整站）。
    """
    rows = [
        MetricSample(
            instance=instance,
            metric=sample.metric[:64],
            dimension=(sample.dimension or "")[:200],
            value=sample.value,
            collected_at=now,
        )
        for sample in samples
    ]
    for start in range(0, len(rows), BULK_CHUNK):
        chunk = rows[start : start + BULK_CHUNK]
        with write_lock():
            with transaction.atomic():
                MetricSample.objects.bulk_create(chunk, batch_size=BULK_CHUNK)


def _persist_sql(instance: Instance, rows: list[tuple]) -> None:
    """写 SQL 指纹。

    失败只记日志、不影响本轮其他实例：洞察是附加视图，
    让它拖垮指标采集是本末倒置。
    """
    from apps.monitor.sql_collector import persist_sql_stats

    try:
        persist_sql_stats(instance, rows)
    except Exception:  # noqa: BLE001
        logger.exception("实例 %s SQL 指纹落库失败", instance.pk)


def _select_instances(limit: int) -> list[Instance]:
    """挑选本轮要采集的实例。

    只采「启用」的实例；停机中的实例（is_active=False）不采，
    否则会持续产生失败记录和告警噪音。
    """
    qs = (
        Instance.objects.filter(is_active=True)
        .select_related("cluster")
        .order_by("id")
    )
    if limit:
        qs = qs[:limit]
    return list(qs)


def collect_all(now: datetime | None = None) -> dict[str, object]:
    """采集全部启用实例，返回统计摘要。

    分工：**子线程只取数，主线程统一落库**。

    这不是性能取舍而是正确性要求——Django 的数据库连接是每线程一份，
    SQLite 内存库在子线程里看不到任何表，文件库则会各自争抢写锁。
    采集的耗时本来就在"等目标库响应"上，因此并发取数仍然能拿到几乎
    全部收益，而写库串行反而避免了锁争抢。
    """
    if not _setting("MONITOR_COLLECT_ENABLED", True):
        logger.info("监控采集已关闭（MONITOR_COLLECT_ENABLED=False），跳过本轮")
        return {"skipped": True, "total": 0, "ok": 0, "failed": 0}

    now = now or timezone.now()
    watch = Stopwatch("monitor:collect_all")
    limit = int(_setting("MONITOR_MAX_INSTANCES", 0))
    concurrency = max(1, int(_setting("MONITOR_CONCURRENCY", 4)))
    timeout = int(_setting("MONITOR_TIMEOUT_SECONDS", 5))
    instances = _select_instances(limit)

    if not instances:
        return {"total": 0, "ok": 0, "failed": 0, "duration_ms": watch.stop()}

    # 顺序必须与 instances 一致：future → instance 的映射用列表下标，
    # 而不是在回调里比较对象（并发下对象身份不可靠）。
    fetched: dict[int, tuple[bool, str, str, list[Sample], int, list[tuple]]] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(_fetch_instance, inst, now, timeout): inst.pk
            for inst in instances
        }
        for future in as_completed(futures):
            pk = futures[future]
            try:
                fetched[pk] = future.result()
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, (SystemExit, KeyboardInterrupt)):
                    raise
                logger.warning("实例 %s 取数异常：%s", pk, type(exc).__name__)
                fetched[pk] = (
                    False,
                    classify_error(exc),
                    f"采集异常：{type(exc).__name__}",
                    [],
                    0,
                    [],
                )

    ok = 0
    failed = 0
    for instance in instances:
        ok_flag, error_code, message, samples, duration_ms, sql_rows = fetched.get(
            instance.pk, (False, "collection_aborted", "本轮未取到结果", [], 0, [])
        )
        if ok_flag and samples:
            try:
                samples.extend(_compute_rate_samples(instance, samples, now))
                _persist_samples(instance, samples, now)
                if sql_rows:
                    _persist_sql(instance, sql_rows)
            except Exception:  # noqa: BLE001
                # 落库失败只影响这一个实例：继续处理后面的，
                # 并把该实例记为失败，而不是让整轮采集中断。
                logger.exception("实例 %s 指标落库失败", instance.pk)
                ok_flag = False
                error_code = error_code or "write_failed"
                message = "指标落库失败"

        _save_run(
            instance,
            now,
            duration_ms,
            ok=ok_flag,
            error_code=error_code,
            message=message,
            metrics_count=len(samples) if ok_flag else 0,
        )
        if ok_flag:
            ok += 1
        else:
            failed += 1

    elapsed = watch.stop()
    logger.info(
        "监控采集完成：实例 %d（成功 %d / 失败 %d），耗时 %dms",
        len(instances),
        ok,
        failed,
        elapsed,
    )
    return {
        "total": len(instances),
        "ok": ok,
        "failed": failed,
        "duration_ms": elapsed,
    }


def latest_run_map(instance_ids: list[int]) -> dict[int, CollectionRun]:
    """批量取每个实例最近一次采集记录。

    页面要显示"最近一次采集"状态。逐实例 ``.first()`` 在列表页上就是 N 次
    查询，这里一次查回全部再在 Python 里挑最新的。
    """
    if not instance_ids:
        return {}
    latest: dict[int, CollectionRun] = {}
    rows = CollectionRun.objects.filter(instance_id__in=instance_ids).order_by(
        "collected_at", "id"
    )
    for run in rows:
        latest[run.instance_id] = run
    return latest


def _purge_queryset(name: str, qs, stats: dict, dry_run: bool) -> None:
    """分批删除一个查询集，把条数累加进 ``stats[name]``。

    分批的理由与 ``apps/metrics/retention.py`` 相同：一次删百万行会长时间
    持写锁，SQLite 上直接卡死整站。
    """
    if dry_run:
        stats[name] = qs.count()
        return
    while True:
        batch_ids = list(qs.values_list("id", flat=True)[:BULK_CHUNK])
        if not batch_ids:
            break
        with write_lock():
            deleted, _ = qs.model.objects.filter(pk__in=batch_ids).delete()
        stats[name] += deleted
        if len(batch_ids) < BULK_CHUNK:
            break


def purge_expired(dry_run: bool = False) -> dict[str, int]:
    """按保留期清理指标、采集记录、SQL 指纹与死锁历史。

    四类数据用**两套保留期**：

    - 指标 / 采集记录：``MONITOR_RETENTION_DAYS``（默认 30 天）。
      它们按采样点增长，30 天在分钟级采集下已是几十万行。
    - SQL 指纹 / 死锁：``MONITOR_SQL_RETENTION_DAYS``（默认 90 天）。
      它们按**模板/事件**聚合而非按采样点增长，增长速度慢一个量级，
      保留更久才有排查价值（"上季度那次死锁是谁"）。

    两套保留期任一为 0 表示该类永久保留。
    锁等待（LockWait）不参与清理：它每轮采集整体刷新，天然不增长。
    """
    stats: dict[str, int] = {}
    now = timezone.now()

    days = int(_setting("MONITOR_RETENTION_DAYS", 30))
    if days <= 0:
        stats.update({"metrics": 0, "runs": 0})
    else:
        cutoff = now - timedelta(days=days)
        stats.update({"metrics": 0, "runs": 0})
        _purge_queryset(
            "metrics", MetricSample.objects.filter(collected_at__lt=cutoff), stats, dry_run
        )
        _purge_queryset(
            "runs", CollectionRun.objects.filter(collected_at__lt=cutoff), stats, dry_run
        )

    sql_days = int(_setting("MONITOR_SQL_RETENTION_DAYS", 90))
    from apps.monitor.models import DeadlockEvent, SqlDigest

    if sql_days <= 0:
        stats.update({"sql_digests": 0, "deadlocks": 0})
    else:
        sql_cutoff = now - timedelta(days=sql_days)
        stats.update({"sql_digests": 0, "deadlocks": 0})
        _purge_queryset(
            "sql_digests",
            SqlDigest.objects.filter(last_seen_at__lt=sql_cutoff),
            stats,
            dry_run,
        )
        _purge_queryset(
            "deadlocks",
            DeadlockEvent.objects.filter(detected_at__lt=sql_cutoff),
            stats,
            dry_run,
        )

    # 两类保留期都关掉时才算整体跳过
    stats["skipped"] = days <= 0 and sql_days <= 0
    return stats


__all__ = [
    "collect_instance",
    "collect_all",
    "latest_run_map",
    "purge_expired",
    "BULK_CHUNK",
]


def collect_locks_all(now=None) -> dict[str, object]:
    """采集全部启用实例的锁等待与死锁。

    与指标采集同构：**子线程只取数、主线程统一落库**（Django 连接每线程
    一份，子线程写库在 SQLite 上会失败）。失败按实例隔离。
    """
    from apps.monitor.lock_collector import fetch_locks, persist_locks

    if not _setting("MONITOR_LOCK_ENABLED", True):
        logger.info("锁采集已关闭（MONITOR_LOCK_ENABLED=False），跳过")
        return {"skipped": True, "total": 0, "waits": 0, "deadlocks": 0}

    now = now or timezone.now()
    watch = Stopwatch("monitor:collect_locks_all")
    timeout = int(_setting("MONITOR_TIMEOUT_SECONDS", 5))
    concurrency = max(1, int(_setting("MONITOR_CONCURRENCY", 4)))
    limit = int(_setting("MONITOR_MAX_INSTANCES", 0))
    instances = _select_instances(limit)

    # 只留 MySQL / PostgreSQL：Redis 无行锁概念，mongodb 未接监控
    targets = [inst for inst in instances if inst.cluster.engine in ("mysql", "postgresql")]
    if not targets:
        return {"total": 0, "waits": 0, "deadlocks": 0, "duration_ms": watch.stop()}

    from apps.dbconsole.services import resolve_password

    payloads: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        for inst in targets:
            password, error = resolve_password(inst)
            if error:
                payloads[inst.pk] = {
                    "ok": False,
                    "error": error,
                    "waits": [],
                    "deadlock_total": 0,
                    "deadlock_detail": "",
                }
                continue
            futures[pool.submit(fetch_locks, inst, password, timeout)] = inst.pk

        for future in as_completed(futures):
            pk = futures[future]
            try:
                payloads[pk] = future.result()
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, (SystemExit, KeyboardInterrupt)):
                    raise
                payloads[pk] = {
                    "ok": False,
                    "error": classify_error(exc),
                    "waits": [],
                    "deadlock_total": 0,
                    "deadlock_detail": "",
                }

    waits = 0
    deadlocks = 0
    for inst in targets:
        payload = payloads.get(inst.pk)
        if not payload or not payload.get("ok"):
            continue
        try:
            stats = persist_locks(inst, payload, now=now)
        except Exception:  # noqa: BLE001
            logger.exception("实例 %s 锁数据落库失败", inst.pk)
            continue
        waits += stats["waits"]
        deadlocks += stats["deadlocks"]

    elapsed = watch.stop()
    return {
        "total": len(targets),
        "waits": waits,
        "deadlocks": deadlocks,
        "duration_ms": elapsed,
    }
