"""SQL 统计采集：把目标库自带的语句统计汇总成 ``SqlDigest``。

为什么不复用 ``apps.dbconsole.execute_statement``
------------------------------------------------
那个函数每执行一次都会写 ``StatementAuditLog``——那是"谁在控制台手工执行了
什么"的审计。采集是**只读的周期性动作**，混进审计表会让人以为有人每分钟
都在跑这些语句，把真正的操作痕迹淹掉。因此这里直接建连。

为什么自己算 digest 而不用源端的
--------------------------------
- MySQL 的 ``DIGEST`` 在实例重启/统计重置后可能变化；
- PG 的 ``queryid`` 与版本相关，跨版本不稳定；
- 而 ``DIGEST_TEXT`` / ``query`` 里**仍然含字面量**（PG 尤其如此）。

自己归一化再取 sha1，才能保证"同一模板在任意引擎、任意时刻都是同一个键"。
顺带把字面量抹掉，手机号/口令不会进洞察表。

关于累计语义
------------
三张源表（performance_schema、pg_stat_statements、Redis commandstats）给出的
都是**累计值**，因此这里做覆盖而不是累加。源端重置（重启、RESET）后累计值
归零，覆盖正好反映真实状态；若做累加，重置后会出现负增长或虚高的历史。
"""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.monitor.collectors.base import (
    ERROR_DRIVER_MISSING,
    ERROR_QUERY_FAILED,
    ERROR_UNSUPPORTED,
)
from apps.monitor.sqltext import digest_of, normalize_sql

logger = logging.getLogger(__name__)

__all__ = ["collect_sql_stats", "SQL_STAT_QUERIES", "SUPPORTED_ENGINES"]

# 每轮每实例最多入库多少条模板。取 100 是因为"总耗时前 100"已经覆盖
# 绝大多数问题语句；全量入库会让洞察表在几百个实例上迅速膨胀。
MAX_DIGESTS_PER_RUN = 100

# MySQL：TIMER_WAIT 单位是**皮秒**，除以 1e9 才是毫秒。
_MYSQL_SQL = """
SELECT DIGEST_TEXT AS template,
       COUNT_STAR AS exec_count,
       SUM_TIMER_WAIT AS total_pico,
       MAX_TIMER_WAIT AS max_pico,
       SUM_ROWS_EXAMINED AS rows_examined
FROM performance_schema.events_statements_summary_by_digest
WHERE DIGEST_TEXT IS NOT NULL AND SCHEMA_NAME IS NOT NULL
ORDER BY SUM_TIMER_WAIT DESC
LIMIT {limit}
""".strip()

# PG：pg_stat_statements 的时间单位是毫秒。query 含字面量，归一化会处理。
# 只取非 PREPARE/EXECUTE 的用户语句，避免把扩展自身开销算进去。
_PG_SQL = """
SELECT query AS template,
       calls AS exec_count,
       total_exec_time AS total_ms,
       max_exec_time AS max_ms,
       rows AS rows_examined
FROM pg_stat_statements
WHERE query NOT ILIKE '%pg_stat_statements%'
ORDER BY total_exec_time DESC
LIMIT {limit}
""".strip()

SQL_STAT_QUERIES = {"mysql": _MYSQL_SQL, "postgresql": _PG_SQL}
SUPPORTED_ENGINES = tuple(SQL_STAT_QUERIES) + ("redis",)


def _connect_redis(instance, password: str, timeout: int):
    import redis

    client = redis.Redis(
        host=instance.host,
        port=int(instance.port),
        password=password or None,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        decode_responses=True,
    )
    client.ping()
    return client


def _rows_from_mysql(instance, password: str, timeout: int) -> list[tuple]:
    import pymysql

    conn = pymysql.connect(
        host=instance.host,
        port=int(instance.port),
        user=instance.username,
        password=password,
        connect_timeout=int(timeout),
        read_timeout=int(timeout),
        write_timeout=int(timeout),
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(_MYSQL_SQL.format(limit=MAX_DIGESTS_PER_RUN))
            rows = cursor.fetchall()
    finally:
        conn.close()

    result = []
    for template, exec_count, total_pico, max_pico, examined in rows:
        # 皮秒 → 毫秒
        result.append(
            (
                template,
                int(exec_count or 0),
                float(total_pico or 0) / 1e9,
                float(max_pico or 0) / 1e9,
                int(examined or 0),
            )
        )
    return result


def _rows_from_postgres(instance, password: str, timeout: int) -> list[tuple]:
    import psycopg

    conn = psycopg.connect(
        host=instance.host,
        port=int(instance.port),
        user=instance.username,
        password=password,
        dbname="postgres",
        connect_timeout=int(timeout),
        options=f"-c statement_timeout={int(timeout) * 1000}",
        autocommit=True,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(_PG_SQL.format(limit=MAX_DIGESTS_PER_RUN))
            rows = cursor.fetchall()
    finally:
        conn.close()

    return [
        (template, int(exec_count or 0), float(total_ms or 0), float(max_ms or 0),
         int(examined or 0))
        for template, exec_count, total_ms, max_ms, examined in rows
    ]


def _rows_from_redis(instance, password: str, timeout: int) -> list[tuple]:
    """Redis 没有 SQL，但 ``INFO commandstats`` 同样能回答"哪类操作最耗时间"。

    把命令当成"语句"处理：``GET`` 与 ``HGETALL`` 各成一条模板，
    执行次数与累计耗时映射到同一套字段上，洞察页因此能统一展示三引擎。
    """
    client = _connect_redis(instance, password, timeout)
    try:
        raw = client.info("commandstats")
    finally:
        client.close()

    result = []
    for key, stats in (raw or {}).items():
        if not isinstance(stats, dict):
            continue
        # key 形如 cmdstat_get
        command = key.split("_", 1)[-1] if "_" in key else key
        calls = int(stats.get("calls", 0) or 0)
        usec = float(stats.get("usec", 0) or 0)
        if calls <= 0:
            continue
        total_ms = usec / 1000.0
        result.append(
            (command.upper(), calls, total_ms, total_ms / calls, 0)
        )
    result.sort(key=lambda row: row[2], reverse=True)
    return result[:MAX_DIGESTS_PER_RUN]


_COLLECTORS = {
    "mysql": _rows_from_mysql,
    "postgresql": _rows_from_postgres,
    "redis": _rows_from_redis,
}


def fetch_sql_stats(instance, password: str, timeout: int) -> dict:
    """只取数，不碰本地数据库。

    拆出来是为了让并发采集成为可能：Django 的数据库连接每线程一份，
    子线程里写库在 SQLite 上会直接失败（内存库看不到表，文件库争锁）。
    因此并发只覆盖"网络 IO + 归一化"，落库统一回主线程。
    """
    engine = instance.cluster.engine
    collector = _COLLECTORS.get(engine)
    if collector is None:
        return {"ok": False, "error": ERROR_UNSUPPORTED, "rows": []}

    try:
        rows = collector(instance, password, timeout)
    except ImportError:
        return {"ok": False, "error": ERROR_DRIVER_MISSING, "rows": []}
    except Exception as exc:  # noqa: BLE001
        # 只留类型名：驱动异常消息可能带连接串与用户名
        logger.info(
            "SQL 统计采集失败 instance=%s engine=%s err=%s",
            instance.pk,
            engine,
            type(exc).__name__,
        )
        return {"ok": False, "error": ERROR_QUERY_FAILED, "rows": []}

    # 归一化也放在子线程：纯字符串运算，不涉及数据库
    normalized = []
    for template, exec_count, total_ms, max_ms, examined in rows:
        text = normalize_sql(str(template or ""))
        if not text:
            continue
        normalized.append((text, exec_count, total_ms, max_ms, examined))
    return {"ok": True, "error": "", "rows": normalized}


def persist_sql_stats(instance, rows: list[tuple]) -> int:
    """把取到的语句统计写入 ``SqlDigest``，返回写入条数。

    源端给的是**累计值**，因此覆盖而非累加。源端重置（重启/``RESET``）后
    累计值归零，覆盖正好反映真实状态；若累加会出现负增长或虚高历史。

    ``first_seen_at`` 不在更新列里：它表示"这条模板第一次被我们看到的时刻"，
    每轮覆盖就失去意义了。
    """
    from apps.monitor.models import SqlDigest

    if not rows:
        return 0

    now = timezone.now()
    engine = instance.cluster.engine
    objects = [
        SqlDigest(
            instance=instance,
            engine=engine,
            digest=digest_of(template),
            template=template,
            exec_count=exec_count,
            total_time_ms=total_ms,
            max_time_ms=max_ms,
            avg_time_ms=(total_ms / exec_count) if exec_count else 0.0,
            rows_examined=examined,
            first_seen_at=now,
            last_seen_at=now,
        )
        for template, exec_count, total_ms, max_ms, examined in rows
    ]

    SqlDigest.objects.bulk_create(
        objects,
        update_conflicts=True,
        unique_fields=["instance", "digest"],
        update_fields=[
            "engine",
            "template",
            "exec_count",
            "total_time_ms",
            "max_time_ms",
            "avg_time_ms",
            "rows_examined",
            "last_seen_at",
        ],
        batch_size=200,
    )
    return len(objects)


def collect_sql_stats(instance, password: str, timeout: int) -> dict:
    """取数 + 落库（单实例同步版本，供任务在确认单线程时直接调用）。

    返回 ``{"ok", "error", "count"}``；任何异常都收敛成失败结论，
    不向上抛——一轮采集里某个实例失败不该拖垮其它实例。
    """
    fetched = fetch_sql_stats(instance, password, timeout)
    if not fetched["ok"]:
        return {"ok": False, "error": fetched["error"], "count": 0}

    count = persist_sql_stats(instance, fetched["rows"])
    # 没有数据不等于失败：pg_stat_statements 可能刚启用，MySQL 也可能
    # 还没积累。由页面提示"缺权限/未开启"，而不是每轮记一次采集失败。
    return {"ok": True, "error": "", "count": count}
