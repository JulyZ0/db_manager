"""目标库连接与执行。

定位
----
本模块负责"用实例登记的账号密码，真的连一次目标数据库"，是探测之外的第二层
验证：TCP 端口可达并不代表凭据可用，端口通但密码错是运维最常见的一类故障。

四类引擎的差异
--------------
===== ========= ==========================================================
引擎   驱动       "连接成功"的判据
===== ========= ==========================================================
MySQL  pymysql   完成握手并返回 server_version
PG     psycopg   完成认证并返回 server_version
Redis  redis     发出 PING 收到 PONG（Redis 的认证发生在握手阶段）
Mongo  pymongo   admin.command("ping") 返回 ok:1
===== ========= ==========================================================

安全约束
--------
- 解密得到的明文只在内存中存活，用完即弃，不写日志、不进异常消息。
- 异常一律归类成分类码后返回，不把原始异常文本透给调用方：驱动报错常带
  内网地址、库名、甚至部分 SQL，属于信息泄露。
- 连接失败与"密码错误"必须区分开——运维看到 ``auth_failed`` 才知道该改密码，
  看到 ``refused`` 才知道该查网络。
"""

from __future__ import annotations

import errno
import logging
import shlex
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from apps.assets.models import Instance
from apps.credentials.crypto import DecryptionError, decrypt_secret

logger = logging.getLogger(__name__)

# ---- 错误分类码（与探测层的命名保持一致，便于前端统一展示）------------------
ERROR_AUTH_FAILED = "auth_failed"
ERROR_TIMEOUT = "timeout"
ERROR_REFUSED = "refused"
ERROR_DNS_FAILED = "dns_failed"
ERROR_UNREACHABLE = "network_unreachable"
ERROR_NO_PASSWORD = "no_password"
ERROR_DECRYPT_FAILED = "decrypt_failed"
ERROR_DRIVER_MISSING = "driver_missing"
ERROR_UNSUPPORTED = "unsupported_engine"
ERROR_SQL = "sql_error"
ERROR_UNKNOWN = "unknown"

# 给运维看的中文说明。刻意不含任何来自驱动/目标的原始文本。
_ERROR_HINTS = {
    ERROR_AUTH_FAILED: "账号或密码不正确",
    ERROR_TIMEOUT: "连接超时，目标可能在防火墙后或负载过高",
    ERROR_REFUSED: "端口未监听，目标没有运行或端口写错",
    ERROR_DNS_FAILED: "主机名无法解析",
    ERROR_UNREACHABLE: "网络不可达",
    ERROR_NO_PASSWORD: "该实例尚未登记密码，无法验证凭据",
    ERROR_DECRYPT_FAILED: "密码无法解密，可能更换过 FIELD_ENCRYPTION_KEY",
    ERROR_DRIVER_MISSING: "缺少对应数据库驱动，请先安装",
    ERROR_UNSUPPORTED: "该引擎暂不支持此操作",
    ERROR_SQL: "SQL 执行失败",
    ERROR_UNKNOWN: "未知错误",
}

# 默认值；实际以 settings 为准
DEFAULT_CONNECT_TIMEOUT = 5
DEFAULT_QUERY_TIMEOUT = 30
DEFAULT_MAX_ROWS = 500


@dataclass(slots=True)
class ConnectionInfo:
    """一次凭据验证的结果。"""

    ok: bool
    engine: str
    latency_ms: int | None = None
    server_version: str = ""
    error: str | None = None
    message: str = ""

    @property
    def hint(self) -> str:
        return self.message or _ERROR_HINTS.get(self.error or "", "")


@dataclass(slots=True)
class QueryResult:
    """一次语句执行的结果。

    ``rows`` 已全部转成字符串，避免 Decimal / datetime / bytes 在 JSON 序列化
    时各自出问题；页面上显示的就是数据库里的样子。
    """

    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    row_count: int = 0
    affected: int | None = None
    duration_ms: int = 0
    truncated: bool = False
    error: str | None = None
    message: str = ""

    @property
    def hint(self) -> str:
        return self.message or _ERROR_HINTS.get(self.error or "", "")


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------


def _resolve_password(instance: Instance) -> tuple[str | None, str | None]:
    """取出明文密码，返回 (password, error_code)。

    解密失败不能向上抛：调用方要的是"这次验证失败及原因"，不是 500。
    """
    if not instance.password_ciphertext:
        return None, ERROR_NO_PASSWORD
    try:
        return decrypt_secret(instance.password_ciphertext), None
    except DecryptionError:
        logger.warning("实例 %s 的密码无法解密", instance.id)
        return None, ERROR_DECRYPT_FAILED


def _connect_timeout() -> float:
    return float(getattr(settings, "DB_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT))


def _query_timeout() -> int:
    return int(getattr(settings, "DB_QUERY_TIMEOUT_SECONDS", DEFAULT_QUERY_TIMEOUT))


def _max_rows() -> int:
    return max(1, int(getattr(settings, "DB_MAX_ROWS", DEFAULT_MAX_ROWS)))


# ---------------------------------------------------------------------------
# 异常分类
# ---------------------------------------------------------------------------

_UNREACHABLE_ERRNOS = {
    getattr(errno, name)
    for name in ("ENETUNREACH", "EHOSTUNREACH", "EHOSTDOWN", "ENETDOWN")
    if hasattr(errno, name)
}


def _is_auth_error(exc: BaseException) -> bool:
    """判断是否为认证失败。

    各驱动没有统一的异常基类，只能按错误码与文本识别。这里刻意不 import
    具体驱动异常，好处是"驱动没装"时也能走到分类逻辑。
    """
    # MySQL: 1045 access denied；PG: 28P01 invalid password
    args = getattr(exc, "args", ())
    if args:
        first = str(args[0])
        if "1045" in first or "Access denied" in first:
            return True
        if "28P01" in first or "password authentication failed" in first.lower():
            return True

    # Redis: NOAUTH / WRONGPASS；pymongo: authentication failed
    text = str(exc).lower()
    if "authentication failed" in text or "invalid password" in text:
        return True
    if "noauth" in text or "wrongpass" in text:
        return True
    return False


def _classify(exc: BaseException) -> str:
    """把驱动异常归类成分类型码。"""
    if _is_auth_error(exc):
        return ERROR_AUTH_FAILED

    if isinstance(exc, socket.gaierror):
        return ERROR_DNS_FAILED

    if isinstance(exc, ConnectionRefusedError):
        return ERROR_REFUSED

    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ERROR_TIMEOUT

    name = type(exc).__name__
    # redis.TimeoutError / pymongo.ServerSelectionTimeoutError 等
    if "Timeout" in name:
        return ERROR_TIMEOUT

    if isinstance(exc, OSError):
        if exc.errno in _UNREACHABLE_ERRNOS:
            return ERROR_UNREACHABLE
        if exc.errno in {getattr(errno, "ETIMEDOUT", None)} - {None}:
            return ERROR_TIMEOUT
        if exc.errno == 111:  # EACCES/ECONNREFUSED 的平台差异兜底
            return ERROR_REFUSED

    return ERROR_UNKNOWN


def _safe_message(exc: BaseException) -> str:
    """给一句能定位方向、又不泄露目标的说明。

    SQL 语法错误对运维有价值（知道改哪），但可能含表名，属于可接受范围；
    网络类错误一律用固定文案。
    """
    code = _classify(exc)
    if code == ERROR_SQL:
        return f"{_ERROR_HINTS[ERROR_SQL]}：{type(exc).__name__}"
    return _ERROR_HINTS.get(code, _ERROR_HINTS[ERROR_UNKNOWN])


# ---------------------------------------------------------------------------
# 各引擎适配
# ---------------------------------------------------------------------------


def _verify_mysql(instance: Instance, password: str, timeout: float) -> ConnectionInfo:
    try:
        import pymysql
    except ImportError:
        return ConnectionInfo(False, "mysql", error=ERROR_DRIVER_MISSING)

    started = time.perf_counter()
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
        )
        version = f"{conn.get_server_info()}"
        return ConnectionInfo(
            ok=True,
            engine="mysql",
            latency_ms=int((time.perf_counter() - started) * 1000),
            server_version=version,
        )
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        code = _classify(exc)
        logger.info("MySQL 连接失败 instance=%s -> %s", instance.id, code)
        return ConnectionInfo(
            ok=False,
            engine="mysql",
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=code,
            message=_safe_message(exc),
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _verify_postgres(instance: Instance, password: str, timeout: float) -> ConnectionInfo:
    try:
        import psycopg
    except ImportError:
        return ConnectionInfo(False, "postgresql", error=ERROR_DRIVER_MISSING)

    started = time.perf_counter()
    conn = None
    try:
        # 不指定库名时默认连与用户同名的库，很多实例没有；用 postgres 更通用
        conn = psycopg.connect(
            host=instance.host,
            port=int(instance.port),
            user=instance.username,
            password=password,
            dbname="postgres",
            connect_timeout=int(timeout),
        )
        version = f"PostgreSQL {conn.info.server_version}"
        return ConnectionInfo(
            ok=True,
            engine="postgresql",
            latency_ms=int((time.perf_counter() - started) * 1000),
            server_version=version,
        )
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        code = _classify(exc)
        logger.info("PostgreSQL 连接失败 instance=%s -> %s", instance.id, code)
        return ConnectionInfo(
            ok=False,
            engine="postgresql",
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=code,
            message=_safe_message(exc),
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _verify_redis(instance: Instance, password: str, timeout: float) -> ConnectionInfo:
    try:
        import redis
    except ImportError:
        return ConnectionInfo(False, "redis", error=ERROR_DRIVER_MISSING)

    started = time.perf_counter()
    client = None
    try:
        client = redis.Redis(
            host=instance.host,
            port=int(instance.port),
            password=password or None,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
            decode_responses=True,
        )
        # 认证发生在握手，PING 成功即说明密码可用
        client.ping()
        info = client.info("server")
        version = f"Redis {info.get('redis_version', '')}".strip()
        return ConnectionInfo(
            ok=True,
            engine="redis",
            latency_ms=int((time.perf_counter() - started) * 1000),
            server_version=version,
        )
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        code = _classify(exc)
        logger.info("Redis 连接失败 instance=%s -> %s", instance.id, code)
        return ConnectionInfo(
            ok=False,
            engine="redis",
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=code,
            message=_safe_message(exc),
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _verify_mongo(instance: Instance, password: str, timeout: float) -> ConnectionInfo:
    try:
        import pymongo
    except ImportError:
        return ConnectionInfo(False, "mongodb", error=ERROR_DRIVER_MISSING)

    started = time.perf_counter()
    client = None
    try:
        kwargs: dict[str, Any] = {
            "host": instance.host,
            "port": int(instance.port),
            "serverSelectionTimeoutMS": int(timeout * 1000),
            "connectTimeoutMS": int(timeout * 1000),
            "socketTimeoutMS": int(timeout * 1000),
        }
        if instance.username:
            kwargs["username"] = instance.username
            kwargs["password"] = password or None
            # 未指定库时 MongoDB 默认 authSource=admin
            kwargs["authSource"] = "admin"

        client = pymongo.MongoClient(**kwargs)
        client.admin.command("ping")
        version = f"MongoDB {client.server_info().get('version', '')}".strip()
        return ConnectionInfo(
            ok=True,
            engine="mongodb",
            latency_ms=int((time.perf_counter() - started) * 1000),
            server_version=version,
        )
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        code = _classify(exc)
        logger.info("MongoDB 连接失败 instance=%s -> %s", instance.id, code)
        return ConnectionInfo(
            ok=False,
            engine="mongodb",
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=code,
            message=_safe_message(exc),
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


_VERIFIERS = {
    "mysql": _verify_mysql,
    "postgresql": _verify_postgres,
    "redis": _verify_redis,
    "mongodb": _verify_mongo,
}


def engine_of(instance: Instance) -> str:
    """取实例所属集群的引擎标识。"""
    return instance.cluster.engine


def verify_credentials(instance: Instance, timeout: float | None = None) -> ConnectionInfo:
    """用实例登记的账号密码连一次目标库，验证凭据是否可用。

    与 TCP 探测的区别：探测只回答"端口通不通"，这里回答"这组账号密码
    能不能真的登录"。两者都通过，实例才算真正可用。

    本函数**不抛业务异常**：连不上是有效结论，不是服务端错误。
    """
    engine = engine_of(instance)
    verifier = _VERIFIERS.get(engine)
    if verifier is None:
        return ConnectionInfo(
            ok=False,
            engine=engine,
            error=ERROR_UNSUPPORTED,
            message=f"引擎「{engine}」暂不支持凭据验证",
        )

    password, error = _resolve_password(instance)
    if error is not None:
        return ConnectionInfo(ok=False, engine=engine, error=error)

    return verifier(instance, password or "", timeout or _connect_timeout())


# ---------------------------------------------------------------------------
# 语句执行
# ---------------------------------------------------------------------------


def _to_text(value: Any) -> str:
    """把驱动返回的值转成终端里能直接显示的字符串。"""
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    return str(value)


def _execute_mysql(instance: Instance, password: str, sql: str, timeout: int) -> QueryResult:
    try:
        import pymysql
    except ImportError:
        return QueryResult(ok=False, error=ERROR_DRIVER_MISSING)

    started = time.perf_counter()
    conn = None
    try:
        conn = pymysql.connect(
            host=instance.host,
            port=int(instance.port),
            user=instance.username,
            password=password,
            connect_timeout=int(timeout),
            read_timeout=timeout,
            write_timeout=timeout,
            charset="utf8mb4",
            # 终端要的是"立刻生效"，与命令行客户端行为一致
            autocommit=True,
        )
        with conn.cursor() as cur:
            cur.execute(sql)
            duration = int((time.perf_counter() - started) * 1000)
            if cur.description is None:
                return QueryResult(
                    ok=True,
                    affected=cur.rowcount if cur.rowcount >= 0 else None,
                    duration_ms=duration,
                )
            columns = [d[0] for d in cur.description]
            limit = _max_rows()
            raw = cur.fetchmany(limit + 1)
            truncated = len(raw) > limit
            rows = [[_to_text(v) for v in row] for row in raw[:limit]]
            return QueryResult(
                ok=True,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                duration_ms=duration,
                truncated=truncated,
            )
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return QueryResult(
            ok=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=ERROR_SQL,
            message=_sql_message(exc),
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _execute_postgres(
    instance: Instance, password: str, sql: str, timeout: int
) -> QueryResult:
    try:
        import psycopg
    except ImportError:
        return QueryResult(ok=False, error=ERROR_DRIVER_MISSING)

    started = time.perf_counter()
    conn = None
    try:
        conn = psycopg.connect(
            host=instance.host,
            port=int(instance.port),
            user=instance.username,
            password=password,
            dbname="postgres",
            connect_timeout=int(timeout),
            autocommit=True,
        )
        with conn.cursor() as cur:
            cur.execute(sql)
            duration = int((time.perf_counter() - started) * 1000)
            if cur.description is None:
                return QueryResult(
                    ok=True,
                    affected=cur.rowcount if cur.rowcount >= 0 else None,
                    duration_ms=duration,
                )
            columns = [d.name for d in cur.description]
            limit = _max_rows()
            raw = cur.fetchmany(limit + 1)
            truncated = len(raw) > limit
            rows = [[_to_text(v) for v in row] for row in raw[:limit]]
            return QueryResult(
                ok=True,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                duration_ms=duration,
                truncated=truncated,
            )
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        return QueryResult(
            ok=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=ERROR_SQL,
            message=_sql_message(exc),
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _execute_redis(instance: Instance, password: str, sql: str, timeout: int) -> QueryResult:
    """Redis 没有 SQL，这里按 Redis 命令语法执行（如 GET foo / KEYS *）。"""
    try:
        import redis
    except ImportError:
        return QueryResult(ok=False, error=ERROR_DRIVER_MISSING)

    try:
        argv = shlex.split(sql)
    except ValueError as exc:
        return QueryResult(ok=False, error=ERROR_SQL, message=f"命令解析失败：{exc}")
    if not argv:
        return QueryResult(ok=False, error=ERROR_SQL, message="命令为空")

    started = time.perf_counter()
    client = None
    try:
        client = redis.Redis(
            host=instance.host,
            port=int(instance.port),
            password=password or None,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
            decode_responses=True,
        )
        result = client.execute_command(*argv)
        duration = int((time.perf_counter() - started) * 1000)

        # 把任意返回结构铺平成二维表：单值一行，列表逐行
        if isinstance(result, (list, tuple)):
            rows = [[_to_text(v)] for v in list(result)[: _max_rows()]]
        elif result is None:
            rows = []
        else:
            rows = [[_to_text(result)]]
        return QueryResult(
            ok=True,
            columns=["result"],
            rows=rows,
            row_count=len(rows),
            duration_ms=duration,
            truncated=isinstance(result, (list, tuple)) and len(result) > _max_rows(),
        )
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        code = _classify(exc)
        return QueryResult(
            ok=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=ERROR_SQL if code == ERROR_UNKNOWN else code,
            message=_sql_message(exc),
        )
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _sql_message(exc: BaseException) -> str:
    """SQL 报错要给运维可操作的信息，但只取第一行、限长。

    MySQL/PostgreSQL 的报错首行通常就是"表不存在 / 语法错误在第 N 个字符"，
    对排障有价值；后面的堆栈与连接串没有价值且可能泄露内网信息。
    """
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:300]


_EXECUTORS = {
    "mysql": _execute_mysql,
    "postgresql": _execute_postgres,
    "redis": _execute_redis,
}

# ---------------------------------------------------------------------------
# 语句分类与拦截
# ---------------------------------------------------------------------------

# 语句首关键字 → 类型。只取首词判断，避免 "select" 出现在字段名里被误判。
_QUERY_KEYWORDS = frozenset(
    {"select", "show", "desc", "describe", "explain", "with", "table", "values"}
)
_DDL_KEYWORDS = frozenset(
    {
        "create",
        "drop",
        "alter",
        "truncate",
        "rename",
        "comment",
        "grant",
        "revoke",
    }
)
# 这些语句无法回滚，且一旦误执行破坏面极大。执行前必须由用户显式确认。
_DESTRUCTIVE_KEYWORDS = frozenset({"drop", "truncate"})

ERROR_MULTI_STATEMENT = "multi_statement"


def _strip_leading_noise(sql: str) -> str:
    """去掉开头空白与注释，便于取首关键字。"""
    text = sql.lstrip()
    while text.startswith("--") or text.startswith("/*"):
        if text.startswith("--"):
            _, _, rest = text.partition("\n")
            text = rest.lstrip()
        else:
            end = text.find("*/")
            if end == -1:
                return ""
            text = text[end + 2 :].lstrip()
    return text


def classify_statement(sql: str) -> str:
    """判断语句类型，用于审计与风险提示。"""
    text = _strip_leading_noise(sql)
    if not text:
        return "other"
    head = text.split(None, 1)[0].lower().strip("(")

    if head in _QUERY_KEYWORDS:
        return "query"
    if head in _DDL_KEYWORDS:
        return "ddl"
    if head in {"insert", "update", "delete", "replace", "merge", "upsert", "set"}:
        return "write"
    if head in {"call", "exec", "execute", "do"}:
        return "other"
    return "other"


def is_destructive(sql: str) -> bool:
    """是否为破坏性语句（DROP / TRUNCATE）。

    这类语句无法回滚，页面上要求二次确认；API 层要求显式传 confirm=true。
    """
    text = _strip_leading_noise(sql)
    if not text:
        return False
    head = text.split(None, 1)[0].lower().strip("(")
    return head in _DESTRUCTIVE_KEYWORDS


def _split_top_level(text: str, sep: str = ";") -> list[str]:
    """按分号切分，但跳过引号与注释内的分号。

    不追求完整的 SQL 词法分析：目标只是识别"用户一次提交了多条语句"，
    宁可漏判也不要误伤 ``SELECT ';'`` 这类合法语句。
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if quote is not None:
            buf.append(ch)
            # SQL 里两个连续引号表示转义（'' 或 ""），跳过后继续
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                quote = None
            elif ch == "\\" and i + 1 < n:
                # MySQL 默认把反斜杠当转义符
                buf.append(text[i + 1])
                i += 2
                continue
            i += 1
            continue

        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue

        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            end = text.find("\n", i)
            if end == -1:
                buf.append(text[i:])
                break
            buf.append(text[i:end])
            i = end
            continue

        if ch == sep:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    parts.append("".join(buf))
    return parts


def check_statement(sql: str, engine: str) -> str | None:
    """执行前的安全校验，返回错误码；None 表示放行。

    只拦截"一次提交多条语句"这种明确危险的情况：多语句让用户看到的
    确认提示与实际执行内容不一致（第一条是 SELECT，第二条是 DROP）。
    Redis 命令本身用分号分隔的方式不常见，因此不做此限制。
    """
    if engine == "redis":
        return None
    parts = [p.strip() for p in _split_top_level(sql)]
    non_empty = [p for p in parts if p]
    if len(non_empty) > 1:
        return ERROR_MULTI_STATEMENT
    return None


def record_audit(
    instance: Instance,
    *,
    user_label: str,
    statement: str,
    result: QueryResult,
) -> None:
    """写一条审计记录。

    审计失败不能影响主流程：用户已经拿到了执行结果，此时再因为写日志
    失败而报错，反而让人以为语句没执行。
    """
    try:
        from apps.dbconsole.models import StatementAuditLog

        StatementAuditLog.objects.create(
            instance=instance,
            user_label=user_label[:64],
            engine=engine_of(instance)[:20],
            kind=classify_statement(statement),
            statement_preview=statement[:500],
            ok=result.ok,
            row_count=result.row_count,
            affected=result.affected,
            duration_ms=result.duration_ms,
            error_code=(result.error or "")[:32],
        )
    except Exception:  # noqa: BLE001
        logger.warning("写语句审计失败 instance=%s", instance.id, exc_info=True)


def execute_statement(
    instance: Instance,
    statement: str,
    timeout: int | None = None,
    *,
    user_label: str = "",
) -> QueryResult:
    """在目标库上执行一条语句。

    注意：这是"完整 SQL 执行"，包含写入与 DDL。调用方（API/页面）必须
    已做登录校验——本函数本身不做权限判断。

    安全边界（只做这三件事，不做语句白名单）：
    1. 拦截一次提交多条语句：`SELECT 1; DROP TABLE x` 会让用户看到的确认
       提示与实际执行内容不一致。
    2. 破坏性语句（DROP / TRUNCATE）由 ``is_destructive`` 识别，调用方
       负责二次确认；本函数不擅自拦截，否则合法运维操作也会被挡。
    3. 每次实际尝试都写审计（``record_audit``），审计失败不影响主流程。

    MongoDB 不支持 SQL，返回 ``unsupported_engine``；请改用 ``verify_credentials``。
    """
    statement = (statement or "").strip().rstrip(";").strip()
    if not statement:
        return QueryResult(ok=False, error=ERROR_SQL, message="语句为空")

    engine = engine_of(instance)
    executor = _EXECUTORS.get(engine)
    if executor is None:
        return QueryResult(
            ok=False,
            error=ERROR_UNSUPPORTED,
            message=f"引擎「{engine}」不支持执行语句",
        )

    blocked = check_statement(statement, engine)
    if blocked is not None:
        result = QueryResult(
            ok=False,
            error=blocked,
            message="一次只能执行一条语句，请分开提交",
        )
        record_audit(
            instance,
            user_label=user_label,
            statement=statement,
            result=result,
        )
        return result

    password, error = _resolve_password(instance)
    if error is not None:
        result = QueryResult(ok=False, error=error)
        record_audit(
            instance,
            user_label=user_label,
            statement=statement,
            result=result,
        )
        return result

    logger.info(
        "在实例 %s(%s) 上执行语句，长度 %d，类型 %s",
        instance.id,
        engine,
        len(statement),
        classify_statement(statement),
    )
    result = executor(instance, password or "", statement, timeout or _query_timeout())
    record_audit(
        instance,
        user_label=user_label,
        statement=statement,
        result=result,
    )
    return result
