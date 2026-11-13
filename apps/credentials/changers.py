"""真正把新密码写到目标数据库上。

为什么需要这一层
----------------
最初的实现只把新密码加密写进本地表，目标库的密码其实没动。结果是：
连不上库也显示"轮换成功"，连得上库密码也没真的换掉——两头都是假的。
本模块补上的正是这一步：连上目标库、执行改密命令、再验证新密码可用。

四个引擎的改密方式
------------------
======= =========================================================
引擎     改密手段
======= =========================================================
MySQL   ALTER USER 'u'@'h' IDENTIFIED BY '...'
PG      ALTER ROLE "role" WITH PASSWORD '...'
Redis   CONFIG SET requirepass ...
Mongo   admin.updateUser(user, pwd=...)
======= =========================================================

安全约束
--------
- 新旧明文只在本模块的局部变量里流转，不进日志、不进异常消息。
- 异常统一包装成 PasswordChangeError，只带分类码与人话说明；
  驱动原始报错可能含内网地址与库名，不透给上层。
- 生成的密码字符集不含引号与反斜杠，SQL 字面量本就安全；
  各引擎仍走参数化/driver 转义，不靠字符集假设。

失败即失败
----------
改密失败会抛 PasswordChangeError，由 services.rotate_one 记为 failed，
**不会**递增本地版本号——避免"本地记录换了、目标库没换"的错位。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from django.conf import settings

from apps.assets.models import Instance
from apps.dbconsole.services import classify_error, error_hint
from common.exceptions import ConflictError

logger = logging.getLogger(__name__)

# 与 dbconsole 的分类码保持一致，前端可复用同一套文案
ERROR_NO_PASSWORD = "no_password"
ERROR_DECRYPT_FAILED = "decrypt_failed"
ERROR_DRIVER_MISSING = "driver_missing"
ERROR_UNSUPPORTED = "unsupported_engine"
ERROR_VERIFY_FAILED = "verify_failed"
ERROR_CHANGE_FAILED = "change_failed"


class PasswordChangeError(ConflictError):
    """改密失败。

    继承 BusinessError 而不是裸 Exception：API 层才能把它转成
    409 + 具体错误码，而不是"兜底 500 内部错误"——后者会让运维
    以为是自己填错了参数，实际是目标库连不上。

    code 直接复用 dbconsole 的分类码（auth_failed / timeout / refused...），
    前端不必再维护第二套映射表。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message, code=code)
        self.code = code
        self.message = message


def _timeout() -> float:
    return float(getattr(settings, "DB_CONNECT_TIMEOUT_SECONDS", 5))


def _verify_enabled() -> bool:
    """改密后是否重新连一次验证。默认开启。

    关掉能省一次连接，但也失去了"命令返回成功、密码其实没换"的兜底。
    这个场景真实存在（连错实例、权限不足但驱动不报错），因此默认开着。
    """
    return bool(getattr(settings, "ROTATION_VERIFY_AFTER_CHANGE", True))


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------


def _change_mysql(instance: Instance, old: str, new: str, timeout: float) -> None:
    try:
        import pymysql
    except ImportError:
        raise PasswordChangeError(ERROR_DRIVER_MISSING, "缺少 pymysql 驱动")

    conn = pymysql.connect(
        host=instance.host,
        port=int(instance.port),
        user=instance.username,
        password=old,
        connect_timeout=int(timeout),
        read_timeout=int(timeout),
        write_timeout=int(timeout),
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            # current_user() 返回授权表里匹配到的 'user@host'，
            # 比 USER()（连接时自报）更准，改的才是这个账号本身。
            cur.execute("SELECT CURRENT_USER()")
            row = cur.fetchone()
            account = str(row[0]) if row else instance.username
            user, _, host = account.partition("@")
            if not host:
                host = "%"
            # 占位符交给驱动转义，不手工拼字符串
            cur.execute(
                "ALTER USER %s@%s IDENTIFIED BY %s",
                (user, host, new),
            )
    finally:
        conn.close()


def _verify_mysql(instance: Instance, password: str, timeout: float) -> None:
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
    )
    conn.close()


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


def _change_postgres(instance: Instance, old: str, new: str, timeout: float) -> None:
    try:
        import psycopg
        from psycopg import sql
    except ImportError:
        raise PasswordChangeError(ERROR_DRIVER_MISSING, "缺少 psycopg 驱动")

    conn = psycopg.connect(
        host=instance.host,
        port=int(instance.port),
        user=instance.username,
        password=old,
        dbname="postgres",
        connect_timeout=int(timeout),
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            row = cur.fetchone()
            role = str(row[0]) if row else instance.username
            # 角色名是标识符、密码是字面量，分别用 Identifier/Literal 转义
            cur.execute(
                sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(new)
                )
            )
        conn.commit()
    finally:
        conn.close()


def _verify_postgres(instance: Instance, password: str, timeout: float) -> None:
    import psycopg

    conn = psycopg.connect(
        host=instance.host,
        port=int(instance.port),
        user=instance.username,
        password=password,
        dbname="postgres",
        connect_timeout=int(timeout),
    )
    conn.close()


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


def _change_redis(instance: Instance, old: str, new: str, timeout: float) -> None:
    try:
        import redis
    except ImportError:
        raise PasswordChangeError(ERROR_DRIVER_MISSING, "缺少 redis 驱动")

    client = redis.Redis(
        host=instance.host,
        port=int(instance.port),
        password=old or None,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        decode_responses=True,
    )
    try:
        # CONFIG SET 改变的是实例级 requirepass，会影响所有连接，
        # 这是 Redis 本身的口径（它没有"用户密码"以外的改密方式）。
        client.config_set("requirepass", new)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _verify_redis(instance: Instance, password: str, timeout: float) -> None:
    import redis

    client = redis.Redis(
        host=instance.host,
        port=int(instance.port),
        password=password or None,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        decode_responses=True,
    )
    try:
        client.ping()
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------


def _mongo_client(instance: Instance, password: str, timeout: float) -> Any:
    import pymongo

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
        kwargs["authSource"] = "admin"
    return pymongo.MongoClient(**kwargs)


def _change_mongo(instance: Instance, old: str, new: str, timeout: float) -> None:
    try:
        import pymongo  # noqa: F401
    except ImportError:
        raise PasswordChangeError(ERROR_DRIVER_MISSING, "缺少 pymongo 驱动")

    client = _mongo_client(instance, old, timeout)
    try:
        client.admin.command("updateUser", instance.username, pwd=new)
    finally:
        client.close()


def _verify_mongo(instance: Instance, password: str, timeout: float) -> None:
    client = _mongo_client(instance, password, timeout)
    try:
        client.admin.command("ping")
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------

_CHANGERS = {
    "mysql": (_change_mysql, _verify_mysql),
    "postgresql": (_change_postgres, _verify_postgres),
    "redis": (_change_redis, _verify_redis),
    "mongodb": (_change_mongo, _verify_mongo),
}


def change_password(instance: Instance, old_password: str, new_password: str) -> None:
    """在目标库上把 old_password 改成 new_password，并按需验证。

    成功返回 None；任何失败都抛 PasswordChangeError。
    调用方（services.rotate_one）拿到异常后写 failed 历史、不递增版本号。
    """
    engine = instance.cluster.engine
    pair = _CHANGERS.get(engine)
    if pair is None:
        raise PasswordChangeError(
            ERROR_UNSUPPORTED, f"引擎「{engine}」暂不支持自动改密"
        )

    changer, verifier = pair
    timeout = _timeout()
    started = time.perf_counter()

    try:
        changer(instance, old_password, new_password, timeout)
    except PasswordChangeError:
        raise
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        code = classify_error(exc)
        logger.info(
            "改密失败 instance=%s engine=%s -> %s", instance.id, engine, code
        )
        raise PasswordChangeError(code, error_hint(code)) from exc

    changed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "目标库改密完成 instance=%s engine=%s 耗时=%dms", instance.id, engine, changed_ms
    )

    if not _verify_enabled():
        return

    try:
        verifier(instance, new_password, timeout)
    except PasswordChangeError:
        raise
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        code = classify_error(exc)
        logger.warning(
            "改密后验证失败 instance=%s engine=%s -> %s", instance.id, engine, code
        )
        raise PasswordChangeError(
            ERROR_VERIFY_FAILED,
            f"改密命令已执行，但用新密码重新连接失败（{error_hint(code)}）",
        ) from exc
