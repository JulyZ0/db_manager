"""目标库连接的单元测试。

不连真实数据库：这里验证的是"分类与分发"这一层逻辑——
端口通了但密码错，运维必须看到 auth_failed 而不是笼统的 unknown，
所以这部分判断值得单独钉死。
"""

from __future__ import annotations

import socket

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
        ("DELETE FROM t", False),
        ("SELECT 1", False),
        ("-- note\nDROP TABLE t", True),
    ],
)
def test_is_destructive(sql, destructive):
    assert services.is_destructive(sql) is destructive


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
