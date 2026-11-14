"""MySQL 指标采集。

取数口径
--------
只用 ``SHOW GLOBAL STATUS`` / ``SHOW GLOBAL VARIABLES`` 与
``information_schema``，全部是只读语句，且不需要 ``SUPER`` 之外的
特殊权限。刻意**不**去解析 ``performance_schema``：那里面的表在不同
版本、不同配置（performance_schema 可能被关闭）下差异极大，采集器
一旦依赖它，就会在部分实例上永远失败。

累计计数器 vs 瞬时值
--------------------
``Queries`` / ``Slow_queries`` / ``Innodb_row_lock_waits`` 是**自启动以来的
累计值**，单独看没有意义。这里以 ``*_total`` 命名把它们原样落库，
真正的「每秒速率」由采集任务读取上一条样本后调用
:func:`apps.monitor.collectors.base.compute_rate` 换算。

为什么不在采集器里自己算差值：采集器是无状态纯函数，每次调用都重新
建连；若让它偷偷缓存上一次的计数器，就会在"多进程同时采集"或"手动补采"
时算出错误的速率，而且这种错误不会报错，只会让曲线莫名跳动。
"""

from __future__ import annotations

from apps.monitor.collectors.base import (
    ERROR_DRIVER_MISSING,
    ERROR_QUERY_FAILED,
    CollectResult,
    Sample,
    as_float,
    percent,
)

__all__ = ["collect"]

# 不统计这些系统库的容量：它们不属于业务数据，且会稀释"哪个库在涨"的判断
_SYSTEM_SCHEMAS = ("information_schema", "performance_schema", "mysql", "sys")


def collect(instance, password: str, timeout: int) -> CollectResult:
    """采集一个 MySQL 实例的指标。

    失败时：核心指标（连接、命中率）拿不到就整体判失败；
    空间、复制这类"锦上添花"的查询失败只记 message，不影响 ok。
    """
    try:
        import pymysql
    except ImportError:
        return CollectResult.failed(ERROR_DRIVER_MISSING)

    conn = None
    try:
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

        samples: list[Sample] = []
        status = _fetch_status(conn)
        variables = _fetch_variables(conn)

        if not status:
            return CollectResult.failed(
                ERROR_QUERY_FAILED, "无法读取 SHOW GLOBAL STATUS"
            )

        # -- 连接 ------------------------------------------------------
        connected = as_float(status.get("Threads_connected"))
        running = as_float(status.get("Threads_running"))
        max_conn = as_float(variables.get("max_connections"))

        _add(samples, "mysql.connections", connected)
        _add(samples, "mysql.threads_running", running)
        _add(samples, "mysql.connections_max", max_conn)
        _add(
            samples,
            "mysql.connection_usage_percent",
            percent(connected, max_conn),
        )

        # -- 累计计数器（速率由任务层换算）------------------------------
        _add(samples, "mysql.queries_total", as_float(status.get("Queries")))
        _add(
            samples,
            "mysql.slow_queries_total",
            as_float(status.get("Slow_queries")),
        )
        _add(
            samples,
            "mysql.innodb_row_lock_waits_total",
            as_float(status.get("Innodb_row_lock_waits")),
        )
        _add(
            samples,
            "mysql.innodb_row_lock_time_total_ms",
            as_float(status.get("Innodb_row_lock_time")),
        )
        _add(samples, "mysql.uptime_seconds", as_float(status.get("Uptime")))

        # -- InnoDB 缓冲池命中率 ---------------------------------------
        #
        # 命中率 = 1 - 物理读 / 逻辑读请求。
        # 用 read_requests 而非 read（后者是实际发生的物理读页数），
        # 两者分母口径不同，混用会算出明显偏低的假命中率。
        read_requests = as_float(status.get("Innodb_buffer_pool_read_requests"))
        physical_reads = as_float(status.get("Innodb_buffer_pool_reads"))
        if read_requests and physical_reads is not None and read_requests > 0:
            miss = percent(physical_reads, read_requests)
            if miss is not None:
                _add(samples, "mysql.buffer_pool_hit_percent", 100.0 - miss)

        page_total = as_float(status.get("Innodb_buffer_pool_pages_total"))
        page_free = as_float(status.get("Innodb_buffer_pool_pages_free"))
        page_dirty = as_float(status.get("Innodb_buffer_pool_pages_dirty"))
        page_size = as_float(variables.get("innodb_page_size")) or 16384.0

        if page_total and page_free is not None:
            _add(
                samples,
                "mysql.buffer_pool_used_bytes",
                (page_total - page_free) * page_size,
            )
            _add(
                samples,
                "mysql.buffer_pool_total_bytes",
                page_total * page_size,
            )
            _add(
                samples,
                "mysql.buffer_pool_used_percent",
                percent(page_total - page_free, page_total),
            )
        _add(
            samples,
            "mysql.buffer_pool_dirty_percent",
            percent(page_dirty, page_total),
        )

        # -- 空间（按库）----------------------------------------------
        # 失败不致命：权限不足（某些托管实例禁止读 information_schema）时
        # 仍然应该把连接数、命中率这些核心指标留下来。
        space_ok = _collect_space(conn, samples, instance)

        # -- 复制延迟 --------------------------------------------------
        _collect_replication(conn, samples, instance)

        message = "" if space_ok else "空间统计失败，可能是账号缺少 information_schema 读取权限"
        return CollectResult(ok=True, samples=samples, message=message)

    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return CollectResult.failed(ERROR_QUERY_FAILED, _safe_reason(exc))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _add(
    samples: list[Sample],
    metric: str,
    value: float | None,
    dimension: str = "",
) -> None:
    """只在值有效时追加，避免把 None 写成 0 污染趋势图。"""
    if value is None:
        return
    samples.append(Sample(metric=metric, value=float(value), dimension=dimension))


def _fetch_status(conn) -> dict[str, str]:
    """SHOW GLOBAL STATUS → {变量名: 值}。

    一次取全表而不是逐个 ``SHOW STATUS LIKE``：后者每条语句一次往返，
    十来个指标就是十几次网络往返，实例网络稍有抖动就会显著拖慢采集。

    **保留原始大小写**（如 ``Threads_connected``）：取值处按 MySQL 的
    官方变量名读取，转小写会让下面每一个 ``status.get("Threads_connected")``
    全部落空——采集"成功"但一个指标都没有，这种静默失败最难排查。
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW GLOBAL STATUS")
            return {str(row[0]): str(row[1]) for row in cur.fetchall()}
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return {}


def _fetch_variables(conn) -> dict[str, str]:
    """SHOW GLOBAL VARIABLES → {变量名: 值}；同样保留原始大小写。"""
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW GLOBAL VARIABLES")
            return {str(row[0]): str(row[1]) for row in cur.fetchall()}
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return {}


def _collect_space(conn, samples: list[Sample], instance) -> bool:
    """按库统计数据+索引占用的字节数。

    返回是否成功。用 ``SUM(data_length + index_length)`` 而不是统计表行数：
    行数是估算值（InnoDB 的 table_rows 是采样统计），用作容量判断会误导。
    """
    placeholders = ", ".join(["%s"] * len(_SYSTEM_SCHEMAS))
    sql = (
        "SELECT table_schema, "
        "SUM(COALESCE(data_length, 0) + COALESCE(index_length, 0)) AS bytes, "
        "COUNT(*) AS tables "
        "FROM information_schema.tables "
        f"WHERE table_schema NOT IN ({placeholders}) "
        "GROUP BY table_schema"
    )

    try:
        with conn.cursor() as cur:
            # 只影响这一条语句：MySQL 没有 statement_timeout 变量，
            # 用 optimizer hint 又依赖 8.0+。这里靠驱动的 read_timeout 兜底，
            # 因此不做额外设置，保持对 5.7 的兼容。
            cur.execute(sql, _SYSTEM_SCHEMAS)
            rows = cur.fetchall()
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return False

    total = 0.0
    for schema, size, tables in rows:
        size_value = as_float(size)
        if size_value is None:
            continue
        total += size_value
        _add(
            samples,
            "mysql.database_size_bytes",
            size_value,
            dimension=str(schema)[:200],
        )
        _add(
            samples,
            "mysql.database_table_count",
            as_float(tables),
            dimension=str(schema)[:200],
        )
    if rows:
        _add(samples, "mysql.total_size_bytes", total)
    return True


def _collect_replication(conn, samples: list[Sample], instance) -> None:
    """从库复制延迟。

    8.0.22 起 ``SHOW SLAVE STATUS`` 被 ``SHOW REPLICA STATUS`` 取代，
    但旧版本不认新语法、新版本仍兼容旧语法（仅弃用告警）。因此先试新语法，
    失败再回退旧语法——反过来会在 5.7 上直接语法报错。
    """
    rows = None
    for statement in ("SHOW REPLICA STATUS", "SHOW SLAVE STATUS"):
        try:
            with conn.cursor() as cur:
                cur.execute(statement)
                fetched = cur.fetchall()
                columns = [d[0] for d in cur.description] if cur.description else []
            rows = (fetched, columns)
            break
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (SystemExit, KeyboardInterrupt)):
                raise
            continue

    if not rows:
        return
    fetched, columns = rows
    if not fetched:
        # 主库没有复制状态，这是正常的，不写任何样本
        return

    row = dict(zip(columns, fetched[0]))
    delay = as_float(row.get("Seconds_Behind_Master", row.get("Seconds_Behind_Source")))
    _add(samples, "mysql.replication_delay_seconds", delay)

    io_running = str(row.get("Slave_IO_Running") or row.get("Replica_IO_Running") or "")
    sql_running = str(
        row.get("Slave_SQL_Running") or row.get("Replica_SQL_Running") or ""
    )
    _add(samples, "mysql.replica_io_running", 1.0 if io_running.lower() == "yes" else 0.0)
    _add(
        samples,
        "mysql.replica_sql_running",
        1.0 if sql_running.lower() == "yes" else 0.0,
    )


def _safe_reason(exc: BaseException) -> str:
    """只保留异常类型名，不向上传递驱动原文。

    驱动异常常含连接串、用户名、主机名，属于不该进入响应或日志正文的内容。
    运维需要的是分类（连不上 / 认证失败 / 超时），那由 dbconsole 的
    ``classify_error`` 给出，本函数只补一个足够定位的线索。
    """
    return f"采集失败：{type(exc).__name__}"
