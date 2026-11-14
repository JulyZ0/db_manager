"""锁等待与死锁采集（MySQL / PostgreSQL）。

为什么锁采集独立成一个任务，而不是并进指标采集
----------------------------------------------
1. **语义不同**：``LockWait`` 是"此刻的排队"，每轮先清空再写；指标是累积
   时间序列，只追加。两者的写入策略相反，混在一个事务里会互相牵制。
2. **成本不同**：锁查询要 join ``performance_schema`` / ``pg_locks`` 多张
   视图，比取一次 ``SHOW GLOBAL STATUS`` 贵得多。运维可能想每分钟采指标、
   但每五分钟看一次锁。
3. **Redis 无锁概念**：独立任务可以整体跳过，不必在指标链路里到处写
   ``if engine == "redis"``。

锁等待为何"清空重写"而不是追加
--------------------------------
``LockWait`` 描述的是**瞬时状态**。若追加，页面上会同时出现"早已解开的
等待"与"正在发生的等待"，运维无法判断此刻到底还堵不堵；表也会无限膨胀。
死锁相反——它是已经发生的坏事，必须留档（见 ``DeadlockEvent``）。

两个硬约束与其它采集器一致：绝不外抛异常；不泄露凭据（驱动异常只留类型名）。
"""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.monitor.collectors.base import (
    ERROR_DRIVER_MISSING,
    ERROR_QUERY_FAILED,
    ERROR_UNSUPPORTED,
)

logger = logging.getLogger(__name__)

__all__ = ["fetch_locks", "persist_locks", "LOCK_ENGINES", "MAX_WAITS_PER_RUN"]

# 只支持 MySQL / PostgreSQL：Redis 是单线程模型，不存在行锁等待；
# mongodb 本系统只做资产管理，未接监控。
LOCK_ENGINES = ("mysql", "postgresql")

# 单次最多落库多少条等待关系。锁等待成百上千时，先看最久的那批就够了，
# 全量入库会让页面变成一堵墙，也拖慢落库。
MAX_WAITS_PER_RUN = 200

# MySQL 8：data_lock_waits 给出"谁等谁"，再 join data_locks 拿对象与锁类型。
# 刻意不用 sys.innodb_lock_waits：sys schema 不一定安装，而
# performance_schema 从 5.6 起默认存在。
_MYSQL_WAITS_SQL = """
SELECT wt.REQUESTING_THREAD_ID AS waiting_thread,
       wt.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx,
       wt.BLOCKING_THREAD_ID AS blocking_thread,
       wt.BLOCKING_ENGINE_TRANSACTION_ID AS blocking_trx,
       rl.OBJECT_SCHEMA AS waiting_schema,
       rl.OBJECT_NAME AS waiting_table,
       rl.LOCK_TYPE AS lock_type,
       rl.LOCK_MODE AS lock_mode,
       wt.REQUESTING_ENGINE_LOCK_ID AS waiting_lock
FROM performance_schema.data_lock_waits wt
LEFT JOIN performance_schema.data_locks rl
       ON rl.ENGINE_LOCK_ID = wt.REQUESTING_ENGINE_LOCK_ID
LIMIT {limit}
""".strip()

# 死锁计数：MySQL 8.0.15+ 才有 Innodb_deadlocks；老版本返回空集，
# 此时把累计值当 0 处理，页面不显示死锁历史而不是报错。
_MYSQL_DEADLOCK_COUNT = "SHOW GLOBAL STATUS LIKE 'Innodb_deadlocks'"

# PG：pg_locks 里"未被授予"的锁就是等待方，沿着 relation/pid 找到阻塞方。
# 用 pg_blocking_pids()（9.6+）比手工 join 更可靠：它直接给出阻塞者 pid 数组。
_PG_WAITS_SQL = """
SELECT blocked.pid AS waiting_pid,
       blocked.usename AS waiting_user,
       blocked.datname AS waiting_db,
       blocked.wait_event_type AS lock_type,
       EXTRACT(EPOCH FROM (now() - blocked.query_start))::numeric AS wait_seconds,
       LEFT(blocked.query, 200) AS waiting_query,
       blocker.pid AS blocking_pid,
       blocker.usename AS blocking_user,
       LEFT(COALESCE(blocker.query, ''), 200) AS blocking_query
FROM pg_stat_activity blocked
CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bp(blocker_pid)
JOIN pg_stat_activity blocker ON blocker.pid = bp.blocker_pid
WHERE blocked.pid <> pg_backend_pid()
ORDER BY wait_seconds DESC
LIMIT {limit}
""".strip()

_PG_DEADLOCK_COUNT = (
    "SELECT COALESCE(SUM(deadlocks), 0) AS total FROM pg_stat_database"
)

# MySQL 死锁原文：只取 LATEST DETECTED DEADLOCK 那一小段，整段输出太长
_MYSQL_DEADLOCK_DETAIL = "SHOW ENGINE INNODB STATUS"


def _mysql_waits(conn) -> list[dict]:
    waits: list[dict] = []
    with conn.cursor() as cursor:
        cursor.execute(_MYSQL_WAITS_SQL.format(limit=MAX_WAITS_PER_RUN))
        rows = cursor.fetchall()
    for row in rows:
        (
            waiting_thread,
            waiting_trx,
            blocking_thread,
            blocking_trx,
            schema,
            table,
            lock_type,
            lock_mode,
            _waiting_lock,
        ) = row
        waits.append(
            {
                "waiting_id": f"thread:{waiting_thread or waiting_trx}",
                "blocking_id": f"thread:{blocking_thread or blocking_trx}",
                "waiting_label": f"trx {waiting_trx or '-'} / thread {waiting_thread or '-'}",
                "blocking_label": f"trx {blocking_trx or '-'} / thread {blocking_thread or '-'}",
                "lock_type": f"{lock_type or ''} {lock_mode or ''}".strip(),
                "relation": ".".join(x for x in (schema, table) if x),
                # MySQL 的 data_lock_waits 不给等待时长，只能留 0：
                # 编一个数字比留空更糟——运维会以为等了两小时。
                "wait_seconds": 0.0,
                "query": "",
            }
        )
    return waits


def _pg_waits(conn) -> list[dict]:
    waits: list[dict] = []
    with conn.cursor() as cursor:
        cursor.execute(_PG_WAITS_SQL.format(limit=MAX_WAITS_PER_RUN))
        rows = cursor.fetchall()
    for row in rows:
        (
            waiting_pid,
            waiting_user,
            waiting_db,
            lock_type,
            wait_seconds,
            waiting_query,
            blocking_pid,
            blocking_user,
            blocking_query,
        ) = row
        waits.append(
            {
                "waiting_id": f"pid:{waiting_pid}",
                "blocking_id": f"pid:{blocking_pid}",
                "waiting_label": f"{waiting_user or '-'}@{waiting_db or '-'} pid {waiting_pid}",
                "blocking_label": f"{blocking_user or '-'} pid {blocking_pid}",
                "lock_type": lock_type or "",
                "relation": waiting_db or "",
                "wait_seconds": float(wait_seconds or 0),
                "query": (waiting_query or "").strip()[:300],
            }
        )
    return waits


def _connect_mysql(instance, password: str, timeout: int):
    import pymysql

    return pymysql.connect(
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


def _connect_postgres(instance, password: str, timeout: int):
    import psycopg

    return psycopg.connect(
        host=instance.host,
        port=int(instance.port),
        user=instance.username,
        password=password,
        dbname="postgres",
        connect_timeout=int(timeout),
        options=f"-c statement_timeout={int(timeout) * 1000}",
        autocommit=True,
    )


def _deadlock_total_mysql(conn) -> int:
    with conn.cursor() as cursor:
        cursor.execute(_MYSQL_DEADLOCK_COUNT)
        row = cursor.fetchone()
    if not row:
        return 0
    try:
        return int(row[1])
    except (TypeError, ValueError):
        return 0


def _deadlock_total_pg(conn) -> int:
    with conn.cursor() as cursor:
        cursor.execute(_PG_DEADLOCK_COUNT)
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def _deadlock_detail_mysql(conn) -> str:
    """从 SHOW ENGINE INNODB STATUS 里截出最近一次死锁段落。

    整段输出可能几万字符，且大部分是缓冲池与信号量信息。只留
    ``LATEST DETECTED DEADLOCK`` 到下一个分隔线之间，才是有用的那部分。
    """
    with conn.cursor() as cursor:
        cursor.execute(_MYSQL_DEADLOCK_DETAIL)
        row = cursor.fetchone()
    if not row or len(row) < 3:
        return ""
    status = str(row[2] or "")
    marker = "LATEST DETECTED DEADLOCK"
    start = status.find(marker)
    if start < 0:
        return ""
    segment = status[start : start + 1900]
    return segment[:2000]


def fetch_locks(instance, password: str, timeout: int) -> dict:
    """取一个实例的锁等待与死锁计数，不写本地库。

    返回 ``{"ok", "error", "waits", "deadlock_total", "deadlock_detail"}``。
    两个部分**独立容错**：死锁计数查不到（老版本 MySQL 没有
    ``Innodb_deadlocks``）不应该让锁等待也一起丢掉。
    """
    engine = instance.cluster.engine
    if engine not in LOCK_ENGINES:
        return {
            "ok": True,
            "error": "",
            "waits": [],
            "deadlock_total": 0,
            "deadlock_detail": "",
            "supported": False,
        }

    conn = None
    try:
        if engine == "mysql":
            conn = _connect_mysql(instance, password, timeout)
            waits = _mysql_waits(conn)
            try:
                total = _deadlock_total_mysql(conn)
                detail = _deadlock_detail_mysql(conn) if total else ""
            except Exception:  # noqa: BLE001
                # 老版本没有 Innodb_deadlocks：锁等待仍然有效，别一起丢
                total, detail = 0, ""
        else:
            conn = _connect_postgres(instance, password, timeout)
            waits = _pg_waits(conn)
            try:
                total = _deadlock_total_pg(conn)
                detail = ""
            except Exception:  # noqa: BLE001
                total, detail = 0, ""
    except ImportError:
        return {"ok": False, "error": ERROR_DRIVER_MISSING, "waits": [], "deadlock_total": 0, "deadlock_detail": "", "supported": True}
    except Exception as exc:  # noqa: BLE001
        # 只留类型名：驱动异常消息可能带连接串与用户名
        logger.info(
            "锁采集失败 instance=%s engine=%s err=%s",
            instance.pk,
            engine,
            type(exc).__name__,
        )
        return {"ok": False, "error": ERROR_QUERY_FAILED, "waits": [], "deadlock_total": 0, "deadlock_detail": "", "supported": True}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    return {
        "ok": True,
        "error": "",
        "waits": waits[:MAX_WAITS_PER_RUN],
        "deadlock_total": total,
        "deadlock_detail": detail,
        "supported": True,
    }


def persist_locks(instance, payload: dict, now=None) -> dict:
    """写库：锁等待清空重写，死锁按累计值增量留档。

    死锁只在**累计值变大**时记一条。若每轮都记，一条早已解决的死锁会在
    死锁历史里重复出现几十次，把真正的多次死锁掩盖掉。
    """
    from django.db import transaction

    from apps.monitor.models import DeadlockEvent, LockWait

    now = now or timezone.now()
    stats = {"waits": 0, "deadlocks": 0}

    with transaction.atomic():
        # 清空重写：LockWait 是瞬时状态快照，不是历史
        LockWait.objects.filter(instance=instance).delete()

        waits = payload.get("waits") or []
        if waits:
            LockWait.objects.bulk_create(
                [
                    LockWait(
                        instance=instance,
                        collected_at=now,
                        waiting_id=str(row["waiting_id"])[:64],
                        blocking_id=str(row["blocking_id"])[:64],
                        waiting_label=str(row.get("waiting_label", ""))[:200],
                        blocking_label=str(row.get("blocking_label", ""))[:200],
                        lock_type=str(row.get("lock_type", ""))[:64],
                        relation=str(row.get("relation", ""))[:200],
                        wait_seconds=float(row.get("wait_seconds") or 0),
                        query=str(row.get("query", ""))[:300],
                    )
                    for row in waits
                ],
                batch_size=200,
            )
            stats["waits"] = len(waits)

        total = int(payload.get("deadlock_total") or 0)
        if total:
            # 用 (instance, deadlock_total) 唯一约束去重，而不是"是否大于历史
            # 最大值"：源端重启会把累计值打回 0，之后重新累计出的小值
            # 永远小于历史最大值，用"大于"判断会把这些真实的新死锁全部漏记。
            _, created = DeadlockEvent.objects.get_or_create(
                instance=instance,
                deadlock_total=total,
                defaults={
                    "detected_at": now,
                    "detail": str(payload.get("deadlock_detail") or "")[:2000],
                },
            )
            stats["deadlocks"] = 1 if created else 0

    return stats
