"""目标库连接的单元测试。

不连真实数据库：这里验证的是"分类与分发"这一层逻辑——
端口通了但密码错，运维必须看到 auth_failed 而不是笼统的 unknown，
所以这部分判断值得单独钉死。
"""

from __future__ import annotations

import socket
import sys
import types

import pytest

from apps.assets.models import Cluster, Department, Instance
from apps.credentials.crypto import encrypt_secret
from apps.dbconsole import services

pytestmark = pytest.mark.django_db


@pytest.fixture
def make_instance():
    dept = Department.objects.create(name="基础架构部", code="INFRA")

    def _make(engine: str = "mysql", password: str | None = "secret", **kwargs):
        cluster = Cluster.objects.create(
            name=f"c-{engine}-{password or 'nopass'}",
            department=dept,
            engine=engine,
            env="prod",
        )
        return Instance.objects.create(
            name="i1",
            cluster=cluster,
            host="10.0.0.1",
            port=3306,
            username="app",
            password_ciphertext=encrypt_secret(password) if password else "",
            **kwargs,
        )

    return _make


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


def test_mysql_access_denied_is_auth_failed():
    exc = Exception(1045, "Access denied for user 'app'@'10.0.0.9'")
    assert services._classify(exc) == services.ERROR_AUTH_FAILED


def test_postgres_invalid_password_is_auth_failed():
    exc = Exception("password authentication failed for user \"app\"")
    assert services._classify(exc) == services.ERROR_AUTH_FAILED


def test_redis_noauth_is_auth_failed():
    exc = Exception("NOAUTH Authentication required.")
    assert services._classify(exc) == services.ERROR_AUTH_FAILED


def test_connection_refused_stays_refused():
    """端口没监听是网络问题，不能误报成密码错。"""
    exc = ConnectionRefusedError(10061, "Connection refused")
    assert services._classify(exc) == services.ERROR_REFUSED


def test_dns_error_is_dns_failed():
    exc = socket.gaierror(-2, "Name or service not known")
    assert services._classify(exc) == services.ERROR_DNS_FAILED


def test_timeout_by_type_name():
    class ServerSelectionTimeoutError(Exception):
        pass

    assert services._classify(ServerSelectionTimeoutError()) == services.ERROR_TIMEOUT


def test_unknown_error_falls_back():
    assert services._classify(ValueError("???")) == services.ERROR_UNKNOWN


# ---------------------------------------------------------------------------
# 凭据解析
# ---------------------------------------------------------------------------


def test_instance_without_password_reports_no_password(make_instance):
    instance = make_instance(password=None)
    result = services.verify_credentials(instance)

    assert result.ok is False
    assert result.error == services.ERROR_NO_PASSWORD
    # 提示要能让运维明白下一步做什么
    assert "密码" in result.hint


def test_undecryptable_password_reports_decrypt_failed(make_instance):
    instance = make_instance()
    # 篡改成另一个密钥的密文
    instance.password_ciphertext = "v1:AAAA"
    instance.save(update_fields=["password_ciphertext"])

    result = services.verify_credentials(instance)

    assert result.ok is False
    assert result.error == services.ERROR_DECRYPT_FAILED


def test_unsupported_engine_is_reported(make_instance):
    instance = make_instance(engine="other")
    result = services.verify_credentials(instance)

    assert result.ok is False
    assert result.error == services.ERROR_UNSUPPORTED


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "engine,verifier_name",
    [
        ("mysql", "_verify_mysql"),
        ("postgresql", "_verify_postgres"),
        ("redis", "_verify_redis"),
        ("mongodb", "_verify_mongo"),
    ],
)
def test_verify_dispatches_to_engine_verifier(
    make_instance, monkeypatch, engine, verifier_name
):
    called = {}

    def fake(instance, password, timeout):
        called["password"] = password
        return services.ConnectionInfo(ok=True, engine=engine)

    monkeypatch.setitem(services._VERIFIERS, engine, fake)
    instance = make_instance(engine=engine)

    result = services.verify_credentials(instance)

    assert result.ok is True
    # 明文密码必须被解出来传给驱动
    assert called["password"] == "secret"


def test_verifier_exception_is_not_swallowed(make_instance, monkeypatch):
    """验证器自身崩溃时不该被静默吞掉。

    各引擎验证器内部已用 try/except 把"连不上"收敛成结果对象；
    能走到外层的异常说明是代码缺陷，必须暴露出来而不是伪装成连接失败。
    """

    def boom(instance, password, timeout):
        raise RuntimeError("driver exploded")

    monkeypatch.setitem(services._VERIFIERS, "mysql", boom)
    instance = make_instance()

    with pytest.raises(RuntimeError):
        services.verify_credentials(instance)


def test_driver_missing_is_classified(make_instance, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pymysql":
            raise ImportError("No module named 'pymysql'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    instance = make_instance(engine="mysql")

    result = services.verify_credentials(instance)

    assert result.ok is False
    assert result.error == services.ERROR_DRIVER_MISSING


# ---------------------------------------------------------------------------
# 语句分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT * FROM t", "query"),
        ("  select 1", "query"),
        ("SHOW TABLES", "query"),
        ("EXPLAIN SELECT 1", "query"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "query"),
        ("INSERT INTO t VALUES (1)", "write"),
        ("UPDATE t SET a=1", "write"),
        ("DELETE FROM t", "write"),
        ("REPLACE INTO t VALUES (1)", "write"),
        ("CREATE TABLE t (id int)", "ddl"),
        ("ALTER TABLE t ADD COLUMN b int", "ddl"),
        ("DROP TABLE t", "ddl"),
        ("TRUNCATE TABLE t", "ddl"),
        ("-- 注释\nSELECT 1", "query"),
        ("/* 注释 */ SELECT 1", "query"),
        # 可执行注释：服务端真的会执行里面的内容，必须按实际语句分类
        ("/*!50000 DROP TABLE t*/", "ddl"),
        ("/*!50000 SELECT 1*/", "query"),
        ("# 注释\nDROP TABLE t", "ddl"),
        ("", "other"),
    ],
)
def test_classify_statement(sql, expected):
    assert services.classify_statement(sql) == expected


@pytest.mark.parametrize(
    "sql,destructive",
    [
        ("DROP TABLE t", True),
        ("drop database x", True),
        ("TRUNCATE t", True),
        ("SELECT 1", False),
        ("-- note\nDROP TABLE t", True),
        # MySQL 可执行注释绕过：这是实测能打穿二次确认的写法
        ("/*!50000 DROP TABLE t*/", True),
        ("/*!50000 ; DROP TABLE t */", True),
        ("/*! DROP TABLE t */", True),
        ("/*!40000 TRUNCATE t */", True),
        # 普通块注释里的内容服务端不执行，不能被判成破坏性
        ("/* DROP TABLE t */ SELECT 1", False),
        # 字符串字面量里的内容同样不算语句
        ("SELECT '/*!50000 DROP TABLE t*/'", False),
        # ---- 无条件 DELETE / UPDATE：一行清空整表，必须二次确认 ----
        ("DELETE FROM t", True),
        ("delete from t", True),
        ("DELETE t", True),
        ("UPDATE t SET a=1", True),
        ("UPDATE t SET a = (SELECT max(x) FROM y) WHERE id = 1", False),
        # 带条件的删除/更新不打扰用户
        ("DELETE FROM t WHERE id = 1", False),
        ("DELETE FROM t WHERE id IN (1, 2)", False),
        ("UPDATE t SET a=1 WHERE id=2", False),
        # WHERE 在顶层（哪怕条件里还嵌着子查询）就是有条件删除
        ("DELETE FROM t WHERE id IN (SELECT id FROM x WHERE n = 1)", False),
        # 引号里的 where 是字面量，不构成条件
        ("UPDATE t SET note = 'where'", True),
    ],
)
def test_is_destructive(sql, destructive):
    assert services.is_destructive(sql) is destructive


# Redis 没有 SQL，但一条命令就能清库、停实例、改口令。
# 这些命令必须和 DROP TABLE 一样走二次确认，否则控制台就是个"一键炸库"入口。
@pytest.mark.parametrize(
    "command",
    [
        "FLUSHALL",
        "flushall",
        "FLUSHDB",
        "SHUTDOWN",
        "SHUTDOWN NOSAVE",
        "CONFIG SET requirepass newpass",
        "config rewrite",
        "SCRIPT FLUSH",
        "CLUSTER RESET",
        "CLIENT KILL 127.0.0.1:6379",
        "REPLICAOF NO ONE",
        "DEBUG SLEEP 0",
        "SWAPDB 0 1",
        "KEYS *",
    ],
)
def test_redis_destructive_commands_are_flagged(command):
    assert services.is_destructive(command, "redis") is True


@pytest.mark.parametrize(
    "command",
    [
        "GET foo",
        "SET foo bar",
        "HSET user:1 name tom",
        "DEL foo",
        "TTL foo",
        # 中性命令的非危险子命令不能被误伤
        "CONFIG GET maxmemory",
        "CLIENT LIST",
        "INFO keyspace",
    ],
)
def test_redis_safe_commands_are_not_flagged(command):
    assert services.is_destructive(command, "redis") is False


def test_redis_engine_does_not_flip_sql_verdict():
    """DROP 在 Redis 上不是命令，不应被当成破坏性；反之亦然。"""
    assert services.is_destructive("DROP TABLE t", "redis") is False
    assert services.is_destructive("FLUSHALL") is False  # 未指定 engine 时按 SQL 判定


@pytest.mark.parametrize(
    "command,expected",
    [
        ("GET foo", "query"),
        ("info", "query"),
        ("SET foo bar", "write"),
        ("DEL foo", "write"),
        ("FLUSHALL", "other"),
    ],
)
def test_redis_classify(command, expected):
    assert services.classify_statement(command, "redis") == expected


# ---------------------------------------------------------------------------
# 多语句拦截
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE t",
        "SELECT 1;SELECT 2",
        "SELECT 1;\nDELETE FROM t;",
    ],
)
def test_multi_statement_is_blocked(sql):
    assert services.check_statement(sql, "mysql") == services.ERROR_MULTI_STATEMENT


@pytest.mark.parametrize(
    "sql",
    [
        # 分号在字符串字面量里，不是语句分隔符
        "SELECT ';' AS x",
        "SELECT 'a;b' FROM t WHERE n = \"c;d\"",
        # 分号在注释里
        "SELECT 1 -- ; DROP TABLE t",
        # 单条语句，结尾分号已在外层 strip
        "SELECT * FROM t",
    ],
)
def test_semicolon_inside_literal_is_not_blocked(sql):
    assert services.check_statement(sql, "mysql") is None


def test_multi_statement_allowed_for_redis():
    """Redis 命令不走 SQL 分号语法，不做此限制。"""
    assert services.check_statement("GET a; GET b", "redis") is None


# ---------------------------------------------------------------------------
# 执行入口：拦截与审计
# ---------------------------------------------------------------------------


def test_execute_blocks_multi_statement_before_connecting(make_instance, monkeypatch):
    """必须在建连之前拦下，否则破坏性语句已经打到目标库了。"""
    from apps.dbconsole.models import StatementAuditLog

    def should_not_run(instance, password, sql, timeout):
        raise AssertionError("不应发起连接")

    monkeypatch.setitem(services._EXECUTORS, "mysql", should_not_run)
    instance = make_instance()

    result = services.execute_statement(instance, "SELECT 1; DROP TABLE t")

    assert result.ok is False
    assert result.error == services.ERROR_MULTI_STATEMENT
    # 被拦截的尝试也要留痕
    assert StatementAuditLog.objects.filter(instance=instance).count() == 1


# ---------------------------------------------------------------------------
# 控制台改密拦截
# ---------------------------------------------------------------------------
#
# 台账是目标库口令的唯一真相来源。控制台里直接改口令会让目标库与本地
# 密文错位，之后凭据验证、自动轮换全部 auth_failed，且运维看不出原因。


@pytest.mark.parametrize(
    "sql,engine",
    [
        ("ALTER USER 'app'@'%' IDENTIFIED BY 'newpass'", "mysql"),
        ("alter user app identified with mysql_native_password by 'x'", "mysql"),
        ("SET PASSWORD FOR 'app'@'%' = 'x'", "mysql"),
        ("GRANT ALL ON *.* TO 'app'@'%' IDENTIFIED BY 'x'", "mysql"),
        ("ALTER USER app WITH PASSWORD 'x'", "postgresql"),
        ("ALTER ROLE app PASSWORD 'x'", "postgresql"),
        # 可执行注释里的改密同样要识别，否则又是一条绕过路径
        ("/*!50000 ALTER USER 'app'@'%' IDENTIFIED BY 'x'*/", "mysql"),
        ("CONFIG SET requirepass newpass", "redis"),
        ("ACL SETUSER app >newpass", "redis"),
    ],
)
def test_changes_credentials_detected(sql, engine):
    assert services.changes_credentials(sql, engine) is True


@pytest.mark.parametrize(
    "sql,engine",
    [
        ("SELECT user, host FROM mysql.user", "mysql"),
        ("CREATE USER app2", "mysql"),
        ("ALTER USER app ACCOUNT LOCK", "mysql"),
        ("SELECT 1", "postgresql"),
        ("GET foo", "redis"),
        ("CONFIG GET requirepass", "redis"),
    ],
)
def test_changes_credentials_not_false_positive(sql, engine):
    assert services.changes_credentials(sql, engine) is False


def test_execute_blocks_credential_change_before_connecting(make_instance, monkeypatch):
    from apps.dbconsole.models import StatementAuditLog

    def should_not_run(instance, password, sql, timeout):
        raise AssertionError("改密语句不应发起连接")

    monkeypatch.setitem(services._EXECUTORS, "mysql", should_not_run)
    instance = make_instance()

    result = services.execute_statement(
        instance, "ALTER USER 'app'@'%' IDENTIFIED BY 'newpass'"
    )

    assert result.ok is False
    assert result.error == services.ERROR_CREDENTIAL_CHANGE
    # 提示要指向正确入口，否则运维只会反复重试
    assert "轮换" in result.hint
    assert StatementAuditLog.objects.filter(instance=instance).count() == 1


def test_credential_change_blocked_keeps_local_ciphertext(make_instance):
    """拦截后本地密文与版本号必须原样不动。"""
    instance = make_instance()
    before = (instance.password_ciphertext, instance.password_version)

    services.execute_statement(instance, "SET PASSWORD FOR 'app'@'%' = 'x'")

    instance.refresh_from_db()
    assert (instance.password_ciphertext, instance.password_version) == before


@pytest.mark.parametrize(
    "sql,engine",
    [
        ("ALTER USER 'app'@'%' IDENTIFIED BY 'Leak1!'", "mysql"),
        (
            "CREATE USER 'n'@'%' IDENTIFIED WITH mysql_native_password BY 'Leak1!'",
            "mysql",
        ),
        ("SET PASSWORD FOR 'app'@'%' = 'Leak1!'", "mysql"),
        ("GRANT ALL ON d.* TO 'app'@'%' IDENTIFIED BY 'Leak1!'", "mysql"),
        ("ALTER ROLE app WITH PASSWORD 'Leak1!'", "postgresql"),
        ("CREATE ROLE n WITH LOGIN PASSWORD 'Leak1!'", "postgresql"),
        ("SELECT * FROM t WHERE password = 'Leak1!'", "postgresql"),
        ("CONFIG SET requirepass Leak1!", "redis"),
        ("ACL SETUSER bob on >Leak1! ~* +@all", "redis"),
    ],
)
def test_redact_statement_masks_secrets(sql, engine):
    """口令字面量必须被替换，且不残留任何片段。"""
    redacted = services.redact_statement(sql, engine)

    assert "Leak1!" not in redacted
    assert "***" in redacted
    # 替换的是口令，不是整条语句：上下文要留着，否则审计失去排查价值
    assert redacted != "***"


@pytest.mark.parametrize(
    "sql,engine",
    [
        ("SELECT * FROM orders WHERE id = 1", "mysql"),
        ("SELECT user, host FROM mysql.user", "mysql"),
        ("CREATE USER app2", "mysql"),
        ("GET somekey", "redis"),
        ("CONFIG GET requirepass", "redis"),
    ],
)
def test_redact_statement_leaves_normal_sql_untouched(sql, engine):
    """普通语句不脱敏——否则审计可读性会被无谓破坏。"""
    assert services.redact_statement(sql, engine) == sql


def test_blocked_credential_change_audit_has_no_plaintext(make_instance, monkeypatch):
    """拦截分支同样写审计，但明文口令绝不能进库。"""
    from apps.dbconsole.models import StatementAuditLog

    def should_not_run(instance, password, sql, timeout):
        raise AssertionError("改密语句不应发起连接")

    monkeypatch.setitem(services._EXECUTORS, "mysql", should_not_run)
    instance = make_instance()

    services.execute_statement(
        instance, "ALTER USER 'app'@'%' IDENTIFIED BY 'Leak1!'", user_label="admin"
    )

    log = StatementAuditLog.objects.get(instance=instance)
    assert "Leak1!" not in log.statement_preview
    assert "ALTER USER" in log.statement_preview
    assert "***" in log.statement_preview


def test_blocked_create_user_audit_has_no_plaintext(make_instance, monkeypatch):
    """建号拦截分支同理：拒绝了语句，不等于可以留存它的口令。"""
    from apps.dbconsole.models import StatementAuditLog

    def should_not_run(instance, password, sql, timeout):
        raise AssertionError("建号语句不应发起连接")

    monkeypatch.setitem(services._EXECUTORS, "mysql", should_not_run)
    instance = make_instance()

    services.execute_statement(
        instance, "CREATE USER 'n'@'%' IDENTIFIED BY 'Leak1!'", user_label="admin"
    )

    log = StatementAuditLog.objects.get(instance=instance)
    assert "Leak1!" not in log.statement_preview
    assert "CREATE USER" in log.statement_preview


def test_executed_statement_audit_has_no_plaintext(make_instance, monkeypatch):
    """放行并执行的语句也要脱敏：审计里不能有口令，哪怕它执行成功了。"""
    from apps.dbconsole.models import StatementAuditLog

    def fake_exec(instance, password, sql, timeout):
        return services.QueryResult(ok=True, affected=1)

    monkeypatch.setitem(services._EXECUTORS, "mysql", fake_exec)
    instance = make_instance()

    services.execute_statement(
        instance, "SELECT * FROM t WHERE password = 'Leak1!'", user_label="admin"
    )

    log = StatementAuditLog.objects.get(instance=instance)
    assert "Leak1!" not in log.statement_preview


def test_execute_writes_audit_on_success(make_instance, monkeypatch):
    from apps.dbconsole.models import StatementAuditLog

    def fake_exec(instance, password, sql, timeout):
        return services.QueryResult(
            ok=True, columns=["id"], rows=[["1"], ["2"]], row_count=2, duration_ms=7
        )

    monkeypatch.setitem(services._EXECUTORS, "mysql", fake_exec)
    instance = make_instance()

    result = services.execute_statement(
        instance, "SELECT id FROM t", user_label="admin"
    )

    assert result.ok is True
    log = StatementAuditLog.objects.get(instance=instance)
    assert log.user_label == "admin"
    assert log.kind == "query"
    assert log.ok is True
    assert log.row_count == 2
    assert log.duration_ms == 7
    # 名称快照要在，删实例后这行是唯一定位线索
    assert instance.name in log.instance_label
    assert instance.host in log.instance_label


def test_audit_survives_instance_deletion(make_instance, monkeypatch):
    """删实例恰恰是最该留痕的操作，审计不能被连带删掉。"""
    from apps.dbconsole.models import StatementAuditLog

    def fake_exec(instance, password, sql, timeout):
        return services.QueryResult(ok=True, columns=["a"], rows=[["1"]], row_count=1)

    monkeypatch.setitem(services._EXECUTORS, "mysql", fake_exec)
    instance = make_instance()
    label = f"{instance.name} ({instance.host}:{instance.port})"

    services.execute_statement(instance, "SELECT 1", user_label="admin")
    instance.delete()

    log = StatementAuditLog.objects.get()
    assert log.instance is None
    assert log.instance_label == label
    assert log.user_label == "admin"
    assert log.kind == "query"


def test_execute_writes_audit_on_failure(make_instance, monkeypatch):
    from apps.dbconsole.models import StatementAuditLog

    def fake_exec(instance, password, sql, timeout):
        return services.QueryResult(
            ok=False, error=services.ERROR_SQL, message="表不存在", duration_ms=3
        )

    monkeypatch.setitem(services._EXECUTORS, "mysql", fake_exec)
    instance = make_instance()

    result = services.execute_statement(instance, "SELECT * FROM nope")

    assert result.ok is False
    log = StatementAuditLog.objects.get(instance=instance)
    assert log.ok is False
    assert log.error_code == services.ERROR_SQL


def test_audit_failure_does_not_break_execution(make_instance, monkeypatch):
    """审计写不进去时，用户仍应拿到真实执行结果。"""

    def fake_exec(instance, password, sql, timeout):
        return services.QueryResult(ok=True, columns=["a"], rows=[["1"]], row_count=1)

    def boom(**kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setitem(services._EXECUTORS, "mysql", fake_exec)
    monkeypatch.setattr(
        "apps.dbconsole.models.StatementAuditLog.objects.create", boom
    )
    instance = make_instance()

    result = services.execute_statement(instance, "SELECT 1")

    assert result.ok is True


def test_empty_statement_is_rejected(make_instance):
    instance = make_instance()
    result = services.execute_statement(instance, "   ")
    assert result.ok is False


def test_mongodb_reports_unsupported_for_sql(make_instance):
    instance = make_instance(engine="mongodb")
    result = services.execute_statement(instance, "SELECT 1")
    assert result.ok is False
    assert result.error == services.ERROR_UNSUPPORTED


# ---------------------------------------------------------------------------
# API：鉴权边界与执行保护
# ---------------------------------------------------------------------------


def test_api_verify_is_read_only_and_anonymous_allowed(anon_client, make_instance):
    """只读验证不写字段，匿名可调；失败也返回 200。"""
    instance = make_instance(password=None)

    resp = anon_client.get(f"/api/v1/instances/{instance.id}/verify-credentials")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["error"] == services.ERROR_NO_PASSWORD


def test_api_execute_requires_login(anon_client, make_instance):
    instance = make_instance()

    resp = anon_client.post(
        f"/api/v1/instances/{instance.id}/execute",
        data={"sql": "SELECT 1"},
        content_type="application/json",
    )

    assert resp.status_code == 401


def test_api_execute_works_when_logged_in(api_client, make_instance, monkeypatch):
    def fake_exec(instance, statement, timeout=None, *, user_label=""):
        return services.QueryResult(
            ok=True,
            columns=["n"],
            rows=[["1"]],
            row_count=1,
            duration_ms=3,
        )

    monkeypatch.setattr("apps.dbconsole.api.execute_statement", fake_exec)
    instance = make_instance()

    resp = api_client.post(
        f"/api/v1/instances/{instance.id}/execute",
        data={"sql": "SELECT 1"},
        content_type="application/json",
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["kind"] == "query"
    assert data["destructive"] is False
    assert data["rows"] == [["1"]]


@pytest.mark.parametrize(
    "sql",
    ["", "   "],
)
def test_api_execute_rejects_empty_statement(api_client, make_instance, sql):
    instance = make_instance()
    resp = api_client.post(
        f"/api/v1/instances/{instance.id}/execute",
        data={"sql": sql},
        content_type="application/json",
    )
    assert resp.status_code == 422


def test_api_execute_rejects_oversized_statement(api_client, make_instance):
    from apps.dbconsole.api import MAX_STATEMENT_LENGTH

    instance = make_instance()
    resp = api_client.post(
        f"/api/v1/instances/{instance.id}/execute",
        data={"sql": "SELECT '" + "x" * MAX_STATEMENT_LENGTH + "'"},
        content_type="application/json",
    )
    assert resp.status_code == 422


def test_api_execute_blocks_multi_statement(api_client, make_instance, monkeypatch):
    def should_not_run(*args, **kwargs):
        raise AssertionError("多语句必须在执行前被拦下")

    monkeypatch.setattr("apps.dbconsole.api.execute_statement", should_not_run)
    instance = make_instance()

    resp = api_client.post(
        f"/api/v1/instances/{instance.id}/execute",
        data={"sql": "SELECT 1; DROP TABLE t"},
        content_type="application/json",
    )

    assert resp.status_code == 422


def test_api_destructive_requires_confirm(api_client, make_instance, monkeypatch):
    called = {"n": 0}

    def fake_exec(*args, **kwargs):
        called["n"] += 1
        return services.QueryResult(ok=True, affected=0, duration_ms=1)

    monkeypatch.setattr("apps.dbconsole.api.execute_statement", fake_exec)
    instance = make_instance()

    resp = api_client.post(
        f"/api/v1/instances/{instance.id}/execute",
        data={"sql": "DROP TABLE t"},
        content_type="application/json",
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFIRM_REQUIRED"
    # 未确认时绝不能真的执行
    assert called["n"] == 0


def test_api_destructive_runs_with_confirm(api_client, make_instance, monkeypatch):
    monkeypatch.setattr(
        "apps.dbconsole.api.execute_statement",
        lambda *a, **kw: services.QueryResult(ok=True, affected=0, duration_ms=1),
    )
    instance = make_instance()

    resp = api_client.post(
        f"/api/v1/instances/{instance.id}/execute",
        data={"sql": "DROP TABLE t", "confirm": True},
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert resp.json()["destructive"] is True
    assert resp.json()["kind"] == "ddl"


def test_api_execute_passes_logged_in_user_to_audit(
    api_client, make_instance, monkeypatch
):
    captured = {}

    def fake_exec(instance, statement, timeout=None, *, user_label=""):
        captured["user_label"] = user_label
        return services.QueryResult(ok=True, columns=["a"], rows=[["1"]], row_count=1)

    monkeypatch.setattr("apps.dbconsole.api.execute_statement", fake_exec)
    instance = make_instance()

    api_client.post(
        f"/api/v1/instances/{instance.id}/execute",
        data={"sql": "SELECT 1"},
        content_type="application/json",
    )

    assert captured["user_label"] == "tester"


def test_api_execute_404_for_missing_instance(api_client):
    resp = api_client.post(
        "/api/v1/instances/999999/execute",
        data={"sql": "SELECT 1"},
        content_type="application/json",
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PostgreSQL 读超时
# ---------------------------------------------------------------------------
#
# psycopg 的 connect_timeout 只覆盖建连阶段。建连成功之后，一条慢查询会
# 一直挂到它自己结束，DB_QUERY_TIMEOUT_SECONDS 就形同虚设。只能用服务端
# 参数 statement_timeout 让 PG 自己掐断。


def _install_fake_psycopg(monkeypatch, captured: dict) -> None:
    """注入假 psycopg，只为捕获 connect() 收到的参数。"""

    class FakeCursor:
        description = [types.SimpleNamespace(name="id")]
        rowcount = -1

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def execute(self, sql):
            captured["sql"] = sql

        def fetchmany(self, size):
            return [(1,)]

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            captured["closed"] = True

    fake = types.ModuleType("psycopg")

    def connect(**kwargs):
        captured["connect"] = kwargs
        return FakeConn()

    fake.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg", fake)


def test_postgres_sets_statement_timeout(make_instance, monkeypatch):
    captured: dict = {}
    _install_fake_psycopg(monkeypatch, captured)

    instance = make_instance(engine="postgresql")
    result = services.execute_statement(instance, "SELECT 1", timeout=30)

    assert result.ok is True
    kwargs = captured["connect"]
    # 单位是毫秒，且与 connect_timeout 同源，避免两套上限互相矛盾
    assert kwargs["options"] == "-c statement_timeout=30000"
    assert kwargs["connect_timeout"] == 30
    assert captured["closed"] is True


def test_postgres_statement_timeout_scales_with_timeout(make_instance, monkeypatch):
    captured: dict = {}
    _install_fake_psycopg(monkeypatch, captured)

    instance = make_instance(engine="postgresql")
    services.execute_statement(instance, "SELECT 1", timeout=5)

    assert captured["connect"]["options"] == "-c statement_timeout=5000"


# ---------------------------------------------------------------------------
# 新建带口令账号同样绕过密码台账
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,engine",
    [
        ("CREATE USER 'n'@'%' IDENTIFIED BY 'p'", "mysql"),
        ("create user n identified with mysql_native_password by 'p'", "mysql"),
        ("CREATE USER n IDENTIFIED BY 'p'", "mysql"),
        ("CREATE ROLE r2 WITH LOGIN PASSWORD 'p'", "postgresql"),
        ("CREATE USER r3 PASSWORD 'p'", "postgresql"),
        ("/*!50000 CREATE USER 'n'@'%' IDENTIFIED BY 'p'*/", "mysql"),
    ],
)
def test_creates_credentials_detected(sql, engine):
    assert services.creates_credentials(sql, engine) is True


@pytest.mark.parametrize(
    "sql,engine",
    [
        # 不带口令的建号是正常授权场景，不该拦
        ("CREATE USER app2", "mysql"),
        ("CREATE USER app3@'%'", "mysql"),
        ("CREATE ROLE readonly", "postgresql"),
        ("SELECT 1", "postgresql"),
        ("GRANT SELECT ON *.* TO 'app'@'%'", "mysql"),
        ("GET foo", "redis"),
    ],
)
def test_creates_credentials_not_false_positive(sql, engine):
    assert services.creates_credentials(sql, engine) is False


def test_creates_credentials_ignores_redis():
    """Redis 的 ACL SETUSER 走改密通道，不算"新建账号"。"""
    assert services.creates_credentials("ACL SETUSER app >p", "redis") is False


def test_execute_blocks_credential_create_before_connecting(
    make_instance, monkeypatch
):
    from apps.dbconsole.models import StatementAuditLog

    def should_not_run(instance, password, sql, timeout):
        raise AssertionError("新建带口令账号不应发起连接")

    monkeypatch.setitem(services._EXECUTORS, "mysql", should_not_run)
    instance = make_instance()

    result = services.execute_statement(instance, "CREATE USER 'n'@'%' IDENTIFIED BY 'p'")

    assert result.ok is False
    assert result.error == services.ERROR_CREDENTIAL_CREATE
    # 指引要说清"为什么"和"怎么做"，否则运维只会换个写法硬试
    assert "台账" in result.hint
    assert StatementAuditLog.objects.filter(instance=instance).count() == 1


def test_execute_allows_plain_create_user(make_instance, monkeypatch):
    """不带口令的建号必须照常执行——否则正常授权会被误伤。"""
    captured: dict = {}

    def fake_exec(instance, password, sql, timeout):
        captured["sql"] = sql
        return services.QueryResult(ok=True)

    monkeypatch.setitem(services._EXECUTORS, "mysql", fake_exec)
    instance = make_instance()

    result = services.execute_statement(instance, "CREATE USER app2")

    assert result.ok is True
    assert captured["sql"] == "CREATE USER app2"


def test_credential_create_keeps_local_ciphertext(make_instance):
    """拦截后本地密文与版本号必须原样不动。"""
    instance = make_instance()
    before = (instance.password_ciphertext, instance.password_version)

    services.execute_statement(instance, "CREATE USER 'n'@'%' IDENTIFIED BY 'p'")

    instance.refresh_from_db()
    assert (instance.password_ciphertext, instance.password_version) == before


def test_credential_create_distinct_from_change(make_instance, monkeypatch):
    """两个错误码不能混用：运维要采取的动作不同。"""
    monkeypatch.setitem(services._EXECUTORS, "mysql", lambda *a: services.QueryResult(ok=True))
    instance = make_instance()

    change = services.execute_statement(instance, "ALTER USER 'a'@'%' IDENTIFIED BY 'p'")
    create = services.execute_statement(instance, "CREATE USER 'b'@'%' IDENTIFIED BY 'p'")

    assert change.error == services.ERROR_CREDENTIAL_CHANGE
    assert create.error == services.ERROR_CREDENTIAL_CREATE
    assert change.error != create.error


# ---------------------------------------------------------------------------
# 改名 / 删号
# ---------------------------------------------------------------------------
#
# 口令没变，改变的是"台账靠什么找到这个账号"。这类语句此前完全放行：
# 执行成功后目标库的账号已经不叫台账里那个名字（或已不存在），
# 之后每一次凭据验证、每一次自动轮换都会 auth_failed，而台账上看不出原因。


@pytest.mark.parametrize(
    "sql,engine",
    [
        ("RENAME USER 'app'@'%' TO 'app2'@'%'", "mysql"),
        ("rename user app to app2", "mysql"),
        ("ALTER ROLE app RENAME TO app2", "postgresql"),
        ("ALTER USER app RENAME TO app2", "postgresql"),
        ("DROP USER 'app'@'%'", "mysql"),
        ("DROP USER IF EXISTS 'app'@'%'", "mysql"),
        ("DROP ROLE app", "postgresql"),
        ("DROP USER app", "postgresql"),
        # 可执行注释不能成为绕过路径，与改密/建号保持一致的展开口径
        ("/*!50000 RENAME USER 'app'@'%' TO 'x'@'%'*/", "mysql"),
        ("ACL DELUSER app", "redis"),
    ],
)
def test_deletes_credentials_detected(sql, engine):
    assert services.deletes_credentials(sql, engine) is True


@pytest.mark.parametrize(
    "sql,engine",
    [
        # 数据破坏，与凭据台账无关：交给 is_destructive 要求二次确认，
        # 不该给出"去修账号"的错误指引
        ("DROP TABLE t", "mysql"),
        ("DROP DATABASE d", "postgresql"),
        ("TRUNCATE TABLE t", "mysql"),
        ("DELETE FROM t WHERE id = 1", "mysql"),
        # 正常的权限调整不该拦
        ("GRANT SELECT ON *.* TO 'app'@'%'", "mysql"),
        ("REVOKE ALL ON *.* FROM 'app'@'%'", "mysql"),
        ("CREATE USER app2", "mysql"),
        ("SELECT user, host FROM mysql.user", "mysql"),
        ("GET foo", "redis"),
        ("ACL LIST", "redis"),
    ],
)
def test_deletes_credentials_not_false_positive(sql, engine):
    assert services.deletes_credentials(sql, engine) is False


def test_execute_blocks_credential_rename_before_connecting(
    make_instance, monkeypatch
):
    """拒绝必须发生在建连之前：连上去执行成功，坏状态就已经产生了。"""
    from apps.dbconsole.models import StatementAuditLog

    def should_not_run(instance, password, sql, timeout):
        raise AssertionError("改名/删号语句不应发起连接")

    monkeypatch.setitem(services._EXECUTORS, "mysql", should_not_run)
    instance = make_instance()

    result = services.execute_statement(instance, "RENAME USER 'app'@'%' TO 'x'@'%'")

    assert result.ok is False
    assert result.error == services.ERROR_CREDENTIAL_IDENTITY
    # 审计要留痕，但不得含明文口令（本语句没有口令，这里守的是不回归）
    log = StatementAuditLog.objects.get(instance=instance)
    assert log.ok is False


def test_credential_rename_keeps_local_ciphertext(make_instance):
    """拦截后本地密文与版本号必须原样不动。"""
    instance = make_instance()
    before = (instance.password_ciphertext, instance.password_version)

    services.execute_statement(instance, "DROP USER 'app'@'%'")

    instance.refresh_from_db()
    assert (instance.password_ciphertext, instance.password_version) == before


def test_credential_identity_distinct_from_other_codes(make_instance, monkeypatch):
    """三个错误码不能混用：各自的补救动作不同。"""
    monkeypatch.setitem(
        services._EXECUTORS, "mysql", lambda *a: services.QueryResult(ok=True)
    )
    instance = make_instance()

    change = services.execute_statement(instance, "ALTER USER 'a'@'%' IDENTIFIED BY 'p'")
    create = services.execute_statement(instance, "CREATE USER 'b'@'%' IDENTIFIED BY 'p'")
    identity = services.execute_statement(instance, "DROP USER 'c'@'%'")

    codes = {
        services.ERROR_CREDENTIAL_CHANGE,
        services.ERROR_CREDENTIAL_CREATE,
        services.ERROR_CREDENTIAL_IDENTITY,
    }
    assert {change.error, create.error, identity.error} == codes

