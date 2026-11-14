"""PostgreSQL 指标采集。

取数口径
--------
只用 ``pg_stat_activity`` / ``pg_stat_database`` / ``pg_stat_replication``
与 ``pg_database_size()``，都是只读视图，普通账号即可读（``pg_stat_activity``
的 query 列对无权限者显示 ``<insufficient privilege>``，但计数不受影响）。

与 MySQL 的两点差异
-------------------
1. **没有 ``SHOW GLOBAL STATUS``**。累计计数器在 ``pg_stat_database`` 里，
   而且是**按库一行**。这里把 cluster 级指标按全库求和，同时把每个库的
   事务数单独落成带 dimension 的样本——「哪个库最忙」是 PG 上高频的排查问题。
2. **事务级回滚率**比 QPS 更能说明问题。PG 的长事务会直接撑爆 autovacuum
   和连接数，因此额外采集 ``max(now() - xact_start)`` 作为最长事务时长。
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



def collect(instance, password: str, timeout: int) -> CollectResult:
    """采集一个 PostgreSQL 实例的指标。"""
    try:
        import psycopg
    except ImportError:
        return CollectResult.failed(ERROR_DRIVER_MISSING)

    conn = None
    try:
        conn = psycopg.connect(
            host=instance.host,
            port=int(instance.port),
            user=instance.username,
            password=password,
            # 连到 postgres 库：监控不假设业务库名，连默认维护库最稳
            dbname="postgres",
            connect_timeout=int(timeout),
            # 与 dbconsole 一致：connect_timeout 只管建连，慢查询要靠服务端
            # statement_timeout 掐断，否则会一直挂到它自己结束
            options=f"-c statement_timeout={int(timeout) * 1000}",
            autocommit=True,
        )

        samples: list[Sample] = []
        _collect_activity(conn, samples)
        _collect_database(conn, samples)
        space_ok = _collect_space(conn, samples)
        _collect_replication(conn, samples)

        # 一条指标都没拿到，等同于采集失败。
        #
        # 否则会返回 ok=True + 空样本：看板上曲线静默断档，采集记录却是绿色，
        # 连续失败告警也不会触发。权限被回收（最常见的事故）正好表现为
        # "能连上、每条查询都被拒"，必须归到失败而不是成功。
        if not samples:
            return CollectResult.failed(
                ERROR_QUERY_FAILED, "未取到任何指标，可能是账号缺少统计视图读取权限"
            )

        message = "" if space_ok else "空间统计失败，可能是账号缺少 pg_database_size 权限"
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
    if value is None:
        return
    samples.append(Sample(metric=metric, value=float(value), dimension=dimension))


def _fetch(conn, sql: str, params=None) -> list[tuple]:
    """执行一条查询并取回全部行；失败返回空列表。"""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return []


def _collect_activity(conn, samples: list[Sample]) -> None:
    """连接数与最长事务时长。

    ``max_connections`` 与 ``superuser_reserved_connections`` 是 GUC 参数，
    连接数上限要减掉保留给超管的配额，否则"还有空位"是假的。
    """
    rows = _fetch(
        conn,
        "SELECT state, count(*) FROM pg_stat_activity "
        "WHERE backend_type = 'client backend' GROUP BY state",
    )
    by_state = {str(state or "unknown"): int(count) for state, count in rows}
    total = sum(by_state.values())
    if by_state:
        _add(samples, "pg.connections", float(total))
        _add(samples, "pg.connections_active", float(by_state.get("active", 0)))
        _add(samples, "pg.connections_idle", float(by_state.get("idle", 0)))
        _add(
            samples,
            "pg.connections_idle_in_transaction",
            float(by_state.get("idle in transaction", 0)),
        )

    limits = _fetch(
        conn,
        "SELECT name, setting FROM pg_settings "
        "WHERE name IN ('max_connections', 'superuser_reserved_connections')",
    )
    settings = {str(name): as_float(value) for name, value in limits}
    max_conn = settings.get("max_connections")
    reserved = settings.get("superuser_reserved_connections") or 0.0
    if max_conn is not None:
        usable = max_conn - reserved
        _add(samples, "pg.connections_max", max_conn)
        _add(samples, "pg.connection_usage_percent", percent(float(total), usable))

    # 最长事务：PG 上长事务会阻塞 vacuum、撑大表膨胀，是最该盯的单点
    longest = _fetch(
        conn,
        "SELECT COALESCE(EXTRACT(EPOCH FROM max(now() - xact_start)), 0) "
        "FROM pg_stat_activity WHERE xact_start IS NOT NULL "
        "AND backend_type = 'client backend'",
    )
    if longest:
        _add(samples, "pg.longest_transaction_seconds", as_float(longest[0][0]))


def _collect_database(conn, samples: list[Sample]) -> None:
    """事务量、回滚率、缓存命中率、临时文件。

    ``pg_stat_database`` 每个库一行，且包含 ``datname IS NULL`` 的汇总行
    （从库上无活动的库），统计总量时要排除它，否则会重复计数。
    """
    rows = _fetch(
        conn,
        "SELECT datname, xact_commit, xact_rollback, blks_read, blks_hit, "
        "tup_returned, tup_fetched, temp_bytes, deadlocks "
        "FROM pg_stat_database WHERE datname IS NOT NULL",
    )
    if not rows:
        return

    totals = {
        "commit": 0.0,
        "rollback": 0.0,
        "read": 0.0,
        "hit": 0.0,
        "temp": 0.0,
        "deadlocks": 0.0,
    }
    per_db: list[tuple[str, float, float]] = []

    for (
        datname,
        commit,
        rollback,
        blks_read,
        blks_hit,
        _tup_returned,
        _tup_fetched,
        temp_bytes,
        deadlocks,
    ) in rows:
        name = str(datname)
        c = as_float(commit) or 0.0
        r = as_float(rollback) or 0.0
        totals["commit"] += c
        totals["rollback"] += r
        totals["read"] += as_float(blks_read) or 0.0
        totals["hit"] += as_float(blks_hit) or 0.0
        totals["temp"] += as_float(temp_bytes) or 0.0
        totals["deadlocks"] += as_float(deadlocks) or 0.0
        per_db.append((name[:200], c, r))

    _add(samples, "pg.xact_commit_total", totals["commit"])
    _add(samples, "pg.xact_rollback_total", totals["rollback"])
    # 回滚率：比绝对次数更能说明"应用在报错"还是"业务在正常回滚"
    _add(
        samples,
        "pg.rollback_percent",
        percent(totals["rollback"], totals["commit"] + totals["rollback"]),
    )
    _add(samples, "pg.deadlocks_total", totals["deadlocks"])
    _add(samples, "pg.temp_bytes_total", totals["temp"])

    # 缓存命中率 = 命中 / (命中 + 物理读)
    _add(
        samples,
        "pg.cache_hit_percent",
        percent(totals["hit"], totals["hit"] + totals["read"]),
    )

    # 按库的累计事务数，供"哪个库最忙"使用
    for name, commit, rollback in per_db:
        _add(samples, "pg.database_xact_total", commit + rollback, dimension=name)


def _collect_space(conn, samples: list[Sample]) -> bool:
    """按库统计磁盘占用。

    用 ``pg_database_size()`` 而不是累加 ``pg_total_relation_size``：
    后者要遍历每张表，库多时非常慢；前者读的是目录元数据，代价小得多。
    """
    rows = _fetch(
        conn,
        "SELECT datname, pg_database_size(datname) FROM pg_database "
        "WHERE datistemplate = false AND datallowconn = true",
    )
    if not rows:
        return False

    total = 0.0
    for datname, size in rows:
        value = as_float(size)
        if value is None:
            continue
        total += value
        _add(samples, "pg.database_size_bytes", value, dimension=str(datname)[:200])
    if rows:
        _add(samples, "pg.total_size_bytes", total)
    return True


def _collect_replication(conn, samples: list[Sample]) -> None:
    """复制延迟与 WAL 落后量。

    延迟用 ``replay_lag``（字节/时间）而不是 ``write_lag``：从库真正"可用"
    取决于回放进度，写进 WAL 但没回放的数据读不到。
    """
    rows = _fetch(
        conn,
        "SELECT COALESCE(EXTRACT(EPOCH FROM replay_lag), 0), "
        "pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) "
        "FROM pg_stat_replication",
    )
    if not rows:
        # 主库且无从库时为空，属正常，不写样本
        return

    max_lag = 0.0
    max_bytes = 0.0
    for lag, lag_bytes in rows:
        max_lag = max(max_lag, as_float(lag) or 0.0)
        max_bytes = max(max_bytes, as_float(lag_bytes) or 0.0)

    _add(samples, "pg.replication_lag_seconds", max_lag)
    _add(samples, "pg.replication_lag_bytes", max_bytes)
    _add(samples, "pg.replica_count", float(len(rows)))


def _safe_reason(exc: BaseException) -> str:
    return f"采集失败：{type(exc).__name__}"
