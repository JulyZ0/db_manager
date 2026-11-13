"""运维界面测试。

重点验证三件事：
1. 每个页面都能真实渲染（模板语法、URL 反向解析、字段名拼写都对）；
2. 表单能完成创建/编辑的完整往返；
3. 页面上的探测、轮换、统计按钮真的驱动核心算法并写回结果。
"""

from __future__ import annotations

import pytest

from apps.assets.models import Cluster, Department, Instance
from apps.credentials.crypto import encrypt_secret

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded():
    """一份最小可用的台账数据。"""
    dept = Department.objects.create(name="基础架构部", code="INFRA", owner="张三")
    cluster = Cluster.objects.create(
        name="core-mysql-prod",
        department=dept,
        engine=Cluster.Engine.MYSQL,
        env=Cluster.Env.PROD,
        # 用例里要建从库实例，架构必须是主从：架构决定可选角色
        topology=Cluster.Topology.MASTER_SLAVE,
    )
    instance = Instance.objects.create(
        name="core-01",
        cluster=cluster,
        host="10.0.0.1",
        port=3306,
        username="app",
        # 真实改密要先用旧密码登录目标库，因此台账里的实例必须是"已登记密码"的
        password_ciphertext=encrypt_secret("Old@Passw0rd01"),
    )
    return {"dept": dept, "cluster": cluster, "instance": instance}


# ---------------------------------------------------------------------------
# 全部页面可渲染
# ---------------------------------------------------------------------------


class TestPagesRender:
    def test_dashboard(self, api_client, seeded):
        resp = api_client.get("/")
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "总览" in content
        assert "基础架构部" in content

    def test_department_list(self, api_client, seeded):
        resp = api_client.get("/departments/")
        assert resp.status_code == 200
        assert "基础架构部" in resp.content.decode()

    def test_department_create_form(self, api_client):
        resp = api_client.get("/departments/new/")
        assert resp.status_code == 200
        assert "新增部门" in resp.content.decode()

    def test_department_edit_form(self, api_client, seeded):
        resp = api_client.get(f"/departments/{seeded['dept'].id}/edit/")
        assert resp.status_code == 200
        assert "编辑部门" in resp.content.decode()

    def test_cluster_list(self, api_client, seeded):
        resp = api_client.get("/clusters/")
        assert resp.status_code == 200
        assert "core-mysql-prod" in resp.content.decode()

    def test_cluster_create_form(self, api_client, seeded):
        resp = api_client.get("/clusters/new/")
        assert resp.status_code == 200
        assert "新增集群" in resp.content.decode()

    def test_cluster_edit_form(self, api_client, seeded):
        resp = api_client.get(f"/clusters/{seeded['cluster'].id}/edit/")
        assert resp.status_code == 200
        assert "编辑集群" in resp.content.decode()

    def test_instance_list(self, api_client, seeded):
        resp = api_client.get("/instances/")
        assert resp.status_code == 200
        assert "core-01" in resp.content.decode()

    def test_instance_create_form(self, api_client, seeded):
        resp = api_client.get("/instances/new/")
        assert resp.status_code == 200
        assert "新增实例" in resp.content.decode()

    def test_instance_detail(self, api_client, seeded):
        resp = api_client.get(f"/instances/{seeded['instance'].id}/")
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "10.0.0.1:3306" in content
        assert "轮换历史" in content

    def test_instance_edit_form(self, api_client, seeded):
        resp = api_client.get(f"/instances/{seeded['instance'].id}/edit/")
        assert resp.status_code == 200
        assert "编辑实例" in resp.content.decode()

    def test_stats(self, api_client, seeded):
        resp = api_client.get("/stats/")
        assert resp.status_code == 200
        assert "数量统计" in resp.content.decode()

    def test_slow_requests(self, api_client):
        resp = api_client.get("/slow-requests/")
        assert resp.status_code == 200
        assert "慢请求" in resp.content.decode()

    def test_all_nav_links_resolve(self, api_client):
        """侧边栏每个链接都必须能打开，避免 URL 名字写错。"""
        for path in [
            "/",
            "/departments/",
            "/clusters/",
            "/instances/",
            "/stats/",
            "/slow-requests/",
        ]:
            assert api_client.get(path).status_code == 200, path


# ---------------------------------------------------------------------------
# 表单往返
# ---------------------------------------------------------------------------


class TestFormRoundTrip:
    def test_create_department_via_form(self, api_client):
        resp = api_client.post(
            "/departments/new/",
            data={"name": "研发部", "code": "RD", "owner": "李四", "is_active": "on"},
        )
        assert resp.status_code == 302
        assert Department.objects.filter(name="研发部").exists()

    def test_create_department_duplicate_shows_error(self, api_client, seeded):
        resp = api_client.post(
            "/departments/new/", data={"name": "基础架构部", "is_active": "on"}
        )
        assert resp.status_code == 200, "校验失败应重新渲染表单而不是重定向"
        assert Department.objects.count() == 1

    def test_create_cluster_via_form(self, api_client, seeded):
        resp = api_client.post(
            "/clusters/new/",
            data={
                "name": "cache-redis",
                "department": seeded["dept"].id,
                "engine": Cluster.Engine.REDIS,
                "env": Cluster.Env.DEV,
                "is_active": "on",
            },
        )
        assert resp.status_code == 302
        assert Cluster.objects.filter(name="cache-redis").exists()

    def test_create_instance_via_form_without_password(self, api_client, seeded):
        resp = api_client.post(
            "/instances/new/",
            data={
                "name": "core-02",
                "cluster": seeded["cluster"].id,
                "host": "10.0.0.2",
                "port": 3307,
                "role": Instance.Role.SLAVE,
                "username": "app",
                "remark": "",
                "is_active": "on",
            },
        )
        assert resp.status_code == 302
        obj = Instance.objects.get(name="core-02")
        assert obj.password_set is False
        assert obj.password_version == 0

    def test_create_cluster_with_alias(self, api_client, seeded):
        resp = api_client.post(
            "/clusters/new/",
            data={
                "name": "cache-redis",
                "alias": "jq1",
                "department": seeded["dept"].id,
                "engine": Cluster.Engine.REDIS,
                "env": Cluster.Env.DEV,
                "is_active": "on",
            },
        )
        assert resp.status_code == 302
        assert Cluster.objects.get(name="cache-redis").alias == "jq1"

    def test_cluster_alias_must_not_be_all_digits(self, api_client, seeded):
        resp = api_client.post(
            "/clusters/new/",
            data={
                "name": "bad-alias",
                "alias": "123",
                "department": seeded["dept"].id,
                "engine": Cluster.Engine.MYSQL,
                "env": Cluster.Env.DEV,
                "is_active": "on",
            },
        )
        assert resp.status_code == 200, "别名非法应重新渲染表单"
        assert not Cluster.objects.filter(name="bad-alias").exists()

    def test_cluster_alias_rejects_special_chars(self, api_client, seeded):
        for bad in ("jq 1", "jq@1", "jq.1", "jq;1"):
            resp = api_client.post(
                "/clusters/new/",
                data={
                    "name": f"c-{bad}",
                    "alias": bad,
                    "department": seeded["dept"].id,
                    "engine": Cluster.Engine.MYSQL,
                    "env": Cluster.Env.DEV,
                    "is_active": "on",
                },
            )
            assert resp.status_code == 200, bad

    def test_instance_alias_shown_on_detail(self, api_client, seeded):
        instance = seeded["instance"]
        instance.alias = "ins1"
        instance.save(update_fields=["alias"])

        resp = api_client.get(f"/instances/{instance.id}/")
        assert resp.status_code == 200
        assert "ins1" in resp.content.decode()

    def test_create_instance_with_alias(self, api_client, seeded):
        resp = api_client.post(
            "/instances/new/",
            data={
                "name": "core-03",
                "alias": "ins9",
                "cluster": seeded["cluster"].id,
                "host": "10.0.0.3",
                "port": 3308,
                "role": Instance.Role.MASTER,
                "username": "app",
                "remark": "",
                "is_active": "on",
            },
        )
        assert resp.status_code == 302
        assert Instance.objects.get(name="core-03").alias == "ins9"

    def test_instance_port_out_of_range_shows_error(self, api_client, seeded):
        resp = api_client.post(
            "/instances/new/",
            data={
                "name": "bad-port",
                "cluster": seeded["cluster"].id,
                "host": "10.0.0.9",
                "port": 70000,
                "role": Instance.Role.MASTER,
                "username": "app",
                "is_active": "on",
            },
        )
        assert resp.status_code == 200
        assert not Instance.objects.filter(name="bad-port").exists()

    def test_edit_instance_without_password_keeps_version(self, api_client, seeded):
        instance = seeded["instance"]
        instance.password_ciphertext = "v1:DUMMY"
        instance.password_version = 3
        instance.save(update_fields=["password_ciphertext", "password_version"])

        resp = api_client.post(
            f"/instances/{instance.id}/edit/",
            data={
                "name": "core-01-renamed",
                "cluster": seeded["cluster"].id,
                "host": "10.0.0.1",
                "port": 3306,
                "role": Instance.Role.MASTER,
                "username": "app",
                "remark": "改个名",
                "is_active": "on",
                "password": "",
            },
        )
        assert resp.status_code == 302
        instance.refresh_from_db()
        assert instance.name == "core-01-renamed"
        assert instance.password_version == 3, "密码留空不应改动版本号"

    def test_delete_department_with_cluster_blocked(self, api_client, seeded):
        resp = api_client.post(f"/departments/{seeded['dept'].id}/delete/", follow=True)
        assert Department.objects.filter(id=seeded["dept"].id).exists()
        assert "无法删除" in resp.content.decode()

    def test_delete_empty_department_succeeds(self, api_client):
        dept = Department.objects.create(name="空部门")
        resp = api_client.post(f"/departments/{dept.id}/delete/")
        assert resp.status_code == 302
        assert not Department.objects.filter(id=dept.id).exists()

    def test_delete_instance_removes_it(self, api_client, seeded):
        resp = api_client.post(f"/instances/{seeded['instance'].id}/delete/")
        assert resp.status_code == 302
        assert not Instance.objects.filter(id=seeded["instance"].id).exists()


# ---------------------------------------------------------------------------
# 页面上的核心操作真实可用
# ---------------------------------------------------------------------------


class TestCoreActionsWorkFromPages:
    """页面按钮必须真的驱动算法，而不是只给一句提示。"""

    def test_probe_updates_instance_status(self, api_client, seeded):
        """探测只连 host:port，用本地一个必然拒绝的端口验证分类码写回页面。"""
        instance = seeded["instance"]
        instance.host = "127.0.0.1"
        instance.port = 1  # 几乎不可能有服务监听
        instance.save(update_fields=["host", "port"])

        resp = api_client.post(f"/instances/{instance.id}/probe/", follow=True)
        assert resp.status_code == 200

        instance.refresh_from_db()
        assert instance.last_probed_at is not None
        assert instance.last_probe_ok is False
        assert instance.status == Instance.Status.OFFLINE

    def test_rotate_increments_version_and_writes_history(self, api_client, seeded):
        instance = seeded["instance"]
        before = instance.password_version

        resp = api_client.post(f"/instances/{instance.id}/rotate/", follow=True)
        assert resp.status_code == 200

        instance.refresh_from_db()
        assert instance.password_version == before + 1
        assert instance.password_set is True
        assert instance.rotation_logs.count() == 1
        assert instance.rotation_logs.first().status == "success"

    def test_stats_rebuild_writes_rows(self, api_client, seeded):
        resp = api_client.post("/stats/rebuild/", follow=True)
        assert resp.status_code == 200

        from apps.metrics.models import DailyInstanceStat

        assert DailyInstanceStat.objects.count() == 1

    def test_create_instance_with_password_encrypts(self, api_client, seeded):
        resp = api_client.post(
            "/instances/new/",
            data={
                "name": "with-pwd",
                "cluster": seeded["cluster"].id,
                "host": "10.0.0.5",
                "port": 3308,
                "role": Instance.Role.MASTER,
                "username": "app",
                "password": "S3cret!",
                "is_active": "on",
            },
        )
        assert resp.status_code == 302

        created = Instance.objects.get(name="with-pwd")
        assert created.password_set is True
        # 落库的是密文，不是原密码
        assert "S3cret!" not in created.password_ciphertext
        assert created.password_ciphertext.startswith("v1:")
        assert created.password_version == 1


# ---------------------------------------------------------------------------
# 安全：密码不回显
# ---------------------------------------------------------------------------


class TestNoSecretLeak:
    def test_instance_pages_never_render_ciphertext(self, api_client, seeded):
        instance = seeded["instance"]
        instance.password_ciphertext = "v1:THIS_IS_SECRET"
        instance.password_version = 2
        instance.save(update_fields=["password_ciphertext", "password_version"])

        for path in [
            "/",
            "/instances/",
            f"/instances/{instance.id}/",
            f"/instances/{instance.id}/edit/",
        ]:
            body = api_client.get(path).content.decode()
            assert "THIS_IS_SECRET" not in body, path


# ---------------------------------------------------------------------------
# CSRF：生产下 cookie 是 HttpOnly，前端只能从 DOM 拿 token
# ---------------------------------------------------------------------------


class TestCsrfTokenInDom:
    """生产口径（DEBUG=False）下 CSRF_COOKIE_HTTPONLY=True。

    此时 JS 读不到 csrftoken cookie，凡是靠 document.cookie 取 token 的写法
    都会静默拿到空串，结果是所有写操作 403。所以页面必须把 token 渲染进
    <meta name="csrf-token">，由 JS 从 DOM 读。
    """

    def test_pages_expose_csrf_meta(self, api_client, seeded):
        for path in ["/", "/instances/", "/console/"]:
            body = api_client.get(path).content.decode()
            assert '<meta name="csrf-token" content="' in body, path

    def test_csrf_meta_is_not_empty(self, api_client):
        body = api_client.get("/").content.decode()
        start = body.index('<meta name="csrf-token" content="')
        start += len('<meta name="csrf-token" content="')
        token = body[start : body.index('"', start)]
        assert len(token) >= 32, "CSRF token 为空或被模板当成变量名原样输出"

    def test_console_js_reads_token_from_dom(self):
        """两个前端取 token 的入口都必须先读 meta，而不是先读 cookie。"""
        from pathlib import Path

        from django.conf import settings

        js = Path(settings.BASE_DIR) / "static" / "js" / "quick_console.js"
        body = js.read_text(encoding="utf-8")
        assert 'meta[name="csrf-token"]' in body

        # 页面内联脚本同样要读 meta
        from django.template.loader import render_to_string

        page = render_to_string("web/console.html")
        assert 'meta[name="csrf-token"]' in page


# ---------------------------------------------------------------------------
# 详情页凭据验证（异步自动执行）
# ---------------------------------------------------------------------------


def test_detail_page_has_async_verify_hook(api_client, seeded):
    """详情页要自带验证端点地址，页面打开即触发，不需要用户点按钮。"""
    resp = api_client.get(f"/instances/{seeded['instance'].id}/")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert f"/instances/{seeded['instance'].id}/verify-credentials/" in body


def test_verify_endpoint_returns_ok(api_client, seeded, monkeypatch):
    from apps.dbconsole import services

    def fake(instance, timeout=None):
        return services.ConnectionInfo(
            ok=True,
            engine="mysql",
            latency_ms=12,
            server_version="8.0.36",
        )

    monkeypatch.setattr("apps.dbconsole.services.verify_credentials", fake)
    resp = api_client.get(
        f"/instances/{seeded['instance'].id}/verify-credentials/"
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["latency_ms"] == 12
    assert data["server_version"] == "8.0.36"


def test_verify_endpoint_reports_auth_failure_without_500(
    api_client, seeded, monkeypatch
):
    """密码错是业务结论，HTTP 仍应为 200，前端据 ok 字段渲染。"""
    from apps.dbconsole import services

    def fake(instance, timeout=None):
        return services.ConnectionInfo(
            ok=False,
            engine="mysql",
            error=services.ERROR_AUTH_FAILED,
            message="账号或密码不正确",
        )

    monkeypatch.setattr("apps.dbconsole.services.verify_credentials", fake)
    resp = api_client.get(
        f"/instances/{seeded['instance'].id}/verify-credentials/"
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["error"] == "auth_failed"
    assert data["hint"] == "账号或密码不正确"


def test_verify_endpoint_does_not_persist_anything(api_client, seeded, monkeypatch):
    """验证结果不落库：字段值必须原样，避免陈旧结论误导运维。"""
    from apps.assets.models import Instance

    before = Instance.objects.get(id=seeded["instance"].id)
    snapshot = (
        before.status,
        before.last_probe_ok,
        before.last_probed_at,
        before.updated_at,
    )

    from apps.dbconsole import services

    monkeypatch.setattr(
        "apps.dbconsole.services.verify_credentials",
        lambda instance, timeout=None: services.ConnectionInfo(
            ok=True, engine="mysql", latency_ms=5
        ),
    )
    api_client.get(f"/instances/{seeded['instance'].id}/verify-credentials/")

    after = Instance.objects.get(id=seeded["instance"].id)
    assert (
        after.status,
        after.last_probe_ok,
        after.last_probed_at,
        after.updated_at,
    ) == snapshot


def test_verify_endpoint_404_for_missing_instance(api_client):
    resp = api_client.get("/instances/999999/verify-credentials/")
    assert resp.status_code == 404


class TestVerifyEndpointIsLogged:
    """验证端点保持匿名可访问，但每次调用都要留痕。

    这个端点会用登记的真实凭据发起主动连接，属于有副作用的操作：
    匿名者能拿它刷目标库连接数，也能借 error 码 + 响应耗时做内网探测。
    口径是"不拦、但要能追溯"，所以日志里必须有操作人与来源 IP。
    """

    def test_anonymous_can_still_verify(self, anon_client, seeded, monkeypatch, caplog):
        from apps.dbconsole import services

        monkeypatch.setattr(
            "apps.dbconsole.services.verify_credentials",
            lambda instance, timeout=None: services.ConnectionInfo(
                ok=True, engine="mysql", latency_ms=7
            ),
        )

        with caplog.at_level("INFO"):
            resp = anon_client.get(
                f"/instances/{seeded['instance'].id}/verify-credentials/"
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert any("凭据验证" in r.message for r in caplog.records)

    def test_log_records_actor_and_ip(self, api_client, login_user, seeded, monkeypatch, caplog):
        from apps.dbconsole import services

        monkeypatch.setattr(
            "apps.dbconsole.services.verify_credentials",
            lambda instance, timeout=None: services.ConnectionInfo(ok=True, engine="mysql"),
        )

        with caplog.at_level("INFO"):
            api_client.get(
                f"/instances/{seeded['instance'].id}/verify-credentials/",
                REMOTE_ADDR="10.20.30.40",
            )

        line = next(r.getMessage() for r in caplog.records if "凭据验证" in r.message)
        assert login_user.username in line
        assert "10.20.30.40" in line

    def test_console_verify_is_logged_too(self, api_client, seeded, monkeypatch, caplog):
        from apps.dbconsole import services

        monkeypatch.setattr(
            "apps.dbconsole.services.verify_credentials",
            lambda instance, timeout=None: services.ConnectionInfo(ok=True, engine="mysql"),
        )

        with caplog.at_level("INFO"):
            resp = api_client.get(f"/console/{seeded['instance'].id}/verify.json")

        assert resp.status_code == 200
        assert any("控制台凭据验证" in r.message for r in caplog.records)


def test_client_ip_ignores_spoofable_header_by_default(settings):
    """默认不采信 X-Forwarded-For：该头客户端可任意伪造。"""
    from django.test import RequestFactory

    from common.request import client_ip

    settings.TRUST_PROXY_HEADERS = False
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="1.2.3.4", REMOTE_ADDR="10.0.0.9"
    )
    assert client_ip(request) == "10.0.0.9"

    # 明确跑在反向代理之后时才取 XFF，且取最后一段（前面的客户端可伪造）
    settings.TRUST_PROXY_HEADERS = True
    request = RequestFactory().get(
        "/", HTTP_X_FORWARDED_FOR="1.2.3.4, 10.0.0.9", REMOTE_ADDR="127.0.0.1"
    )
    assert client_ip(request) == "10.0.0.9"


# ---------------------------------------------------------------------------
# 终端风格控制台
# ---------------------------------------------------------------------------


def test_console_page_renders(api_client, seeded):
    """控制台页面可渲染，且带有终端骨架与命令提示。"""
    resp = api_client.get("/console/")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'id="term-body"' in body
    assert 'id="term-input"' in body
    assert "help" in body
    # 页面自带终端配色，不依赖外部样式表
    assert "#0b0f14" in body


def test_console_page_is_browsable_anonymously(anon_client):
    """渲染空壳不需要登录；危险操作在执行端点单独守卫。"""
    assert anon_client.get("/console/").status_code == 200


def test_console_instances_lists_all(anon_client, seeded):
    resp = anon_client.get("/console/instances.json")

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    # 与其它列表接口同一套分页口径
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert data["has_next"] is False
    item = data["results"][0]
    assert item["endpoint"] == "10.0.0.1:3306"
    assert item["label"] == "基础架构部/core-mysql-prod/core-01"
    assert item["engine"] == "mysql"
    assert item["env"] == "生产"
    # 只暴露"有没有密码"，不暴露内容
    assert item["password_set"] is True
    assert "password" not in item
    assert "password_ciphertext" not in item


def test_console_instances_paginates(anon_client, seeded):
    """一批实例一次只回一页，避免控制台被大响应体拖住。"""
    cluster = seeded["cluster"]
    # cluster + host + port 有唯一约束，用不同端口区分
    Instance.objects.bulk_create(
        [
            Instance(
                name=f"bulk-{i:02d}", cluster=cluster, host="10.0.0.2", port=3400 + i
            )
            for i in range(5)
        ]
    )

    first = anon_client.get("/console/instances.json?page=1&page_size=2").json()
    assert first["count"] == 6
    assert first["page_size"] == 2
    assert len(first["results"]) == 2
    assert first["has_next"] is True

    last = anon_client.get("/console/instances.json?page=3&page_size=2").json()
    assert len(last["results"]) == 2
    assert last["has_next"] is False


def test_console_instances_bad_params_fall_back_to_defaults(anon_client, seeded):
    """控制台是自由文本输入，`list abc` 不能把接口打成 500。"""
    data = anon_client.get("/console/instances.json?page=abc&page_size=xyz").json()

    assert data["page"] == 1
    assert data["page_size"] == 20


def test_console_instances_page_size_is_capped(anon_client, seeded):
    """page_size 上限 100，防止有人用它当批量导出接口。"""
    data = anon_client.get("/console/instances.json?page_size=100000").json()

    assert data["page_size"] <= 100


def test_console_switch_by_instance_id(anon_client, seeded):
    """sw 支持按实例 id 切换（原 use <id> 的能力必须保留）。"""
    resp = anon_client.get(f"/console/switch.json?target={seeded['instance'].id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["instance"]["id"] == seeded["instance"].id
    assert data["instance"]["endpoint"] == "10.0.0.1:3306"


def test_console_switch_by_cluster_and_instance_alias(anon_client, seeded):
    cluster = seeded["cluster"]
    instance = seeded["instance"]
    cluster.alias = "jq1"
    cluster.save(update_fields=["alias"])
    instance.alias = "ins1"
    instance.save(update_fields=["alias"])

    resp = anon_client.get("/console/switch.json?target=jq1.ins1")

    data = resp.json()
    assert data["ok"] is True
    assert data["instance"]["id"] == instance.id
    assert data["instance"]["alias"] == "ins1"
    assert data["instance"]["cluster_alias"] == "jq1"


def test_console_switch_by_numeric_path(anon_client, seeded):
    """集群 id . 实例 id 也能用。"""
    target = f"{seeded['cluster'].id}.{seeded['instance'].id}"
    data = anon_client.get(f"/console/switch.json?target={target}").json()

    assert data["ok"] is True
    assert data["instance"]["id"] == seeded["instance"].id


def test_console_switch_by_cluster_at_host(anon_client, seeded):
    cluster = seeded["cluster"]
    cluster.alias = "jq1"
    cluster.save(update_fields=["alias"])

    data = anon_client.get("/console/switch.json?target=jq1@10.0.0.1").json()

    assert data["ok"] is True
    assert data["instance"]["id"] == seeded["instance"].id


def test_console_switch_reports_missing_target(anon_client, seeded):
    data = anon_client.get("/console/switch.json?target=nosuch").json()

    assert data["ok"] is False
    assert data["error"] == "not_found"
    assert "message" in data


def test_console_switch_reports_ambiguity_with_candidates(anon_client, seeded):
    """同主机多端口时必须报歧义并给出候选，不能随便挑一个。"""
    Instance.objects.create(
        name="core-02",
        cluster=seeded["cluster"],
        host="10.0.0.1",
        port=3307,
        username="app",
    )
    seeded["cluster"].alias = "jq1"
    seeded["cluster"].save(update_fields=["alias"])

    data = anon_client.get("/console/switch.json?target=jq1@10.0.0.1").json()

    assert data["ok"] is False
    assert data["error"] == "ambiguous"
    assert data["candidate_count"] == 2
    assert {c["endpoint"] for c in data["candidates"]} == {
        "10.0.0.1:3306",
        "10.0.0.1:3307",
    }


def test_console_switch_never_leaks_password(anon_client, seeded):
    """切换回包同样不得带出任何凭据内容。"""
    seeded["instance"].password_ciphertext = "v1:TOPSECRET"
    seeded["instance"].save(update_fields=["password_ciphertext"])

    body = anon_client.get(
        f"/console/switch.json?target={seeded['instance'].id}"
    ).content.decode()

    assert "TOPSECRET" not in body
    assert "password_ciphertext" not in body


def test_console_help_mentions_sw_not_use(api_client, seeded):
    """控制台帮助里应当只讲 sw；use 已被移除以免与 SQL 的 USE 冲突。"""
    body = api_client.get("/console/").content.decode()

    assert "sw <目标>" in body
    assert "use <id>" not in body


def test_console_verify_reports_no_password(anon_client, seeded):
    """未登记密码的实例，验证结果必须是 no_password，而不是"成功"。"""
    from apps.assets.models import Instance

    bare = Instance.objects.create(
        name="no-pwd",
        cluster=seeded["cluster"],
        host="10.0.0.7",
        port=3306,
        username="app",
    )

    resp = anon_client.get(f"/console/{bare.id}/verify.json")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["error"] == "no_password"


def test_console_exec_requires_login(anon_client, seeded):
    resp = anon_client.post(
        f"/console/{seeded['instance'].id}/exec/", {"sql": "SELECT 1"}
    )

    # 未登录时被 login_required_page 拦到登录页，不会执行
    assert resp.status_code == 302
    assert "/admin/login/" in resp["Location"]


def test_console_exec_rejects_empty_sql(api_client, seeded):
    resp = api_client.post(f"/console/{seeded['instance'].id}/exec/", {"sql": "  "})

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["error"] == "empty"


def test_console_exec_blocks_multi_statement(api_client, seeded, monkeypatch):
    def should_not_run(*args, **kwargs):
        raise AssertionError("多语句必须在执行前被拦下")

    monkeypatch.setattr("apps.web.views.execute_statement", should_not_run, raising=False)
    monkeypatch.setattr(
        "apps.dbconsole.services.execute_statement", should_not_run, raising=False
    )

    resp = api_client.post(
        f"/console/{seeded['instance'].id}/exec/", {"sql": "SELECT 1; DROP TABLE t"}
    )

    assert resp.status_code == 200
    assert resp.json()["error"] == "multi_statement"


def test_console_exec_requires_confirm_for_destructive(api_client, seeded, monkeypatch):
    called = {"n": 0}

    def fake_exec(*args, **kwargs):
        called["n"] += 1
        return __import__(
            "apps.dbconsole.services", fromlist=["QueryResult"]
        ).QueryResult(ok=True, affected=0, duration_ms=1)

    monkeypatch.setattr("apps.dbconsole.services.execute_statement", fake_exec)

    resp = api_client.post(
        f"/console/{seeded['instance'].id}/exec/", {"sql": "TRUNCATE TABLE t"}
    )

    assert resp.status_code == 200
    assert resp.json()["error"] == "confirm_required"
    assert called["n"] == 0


def test_console_exec_runs_select(api_client, seeded, monkeypatch):
    from apps.dbconsole import services

    monkeypatch.setattr(
        "apps.dbconsole.services.execute_statement",
        lambda *a, **kw: services.QueryResult(
            ok=True, columns=["n"], rows=[["1"]], row_count=1, duration_ms=4
        ),
    )

    resp = api_client.post(f"/console/{seeded['instance'].id}/exec/", {"sql": "SELECT 1"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["kind"] == "query"
    assert data["rows"] == [["1"]]
    assert data["duration_ms"] == 4


def test_console_exec_passes_username_to_audit(api_client, seeded, monkeypatch):
    from apps.dbconsole import services

    captured = {}

    def fake_exec(instance, statement, timeout=None, *, user_label=""):
        captured["user"] = user_label
        return services.QueryResult(ok=True, columns=["a"], rows=[["1"]], row_count=1)

    monkeypatch.setattr("apps.dbconsole.services.execute_statement", fake_exec)

    api_client.post(f"/console/{seeded['instance'].id}/exec/", {"sql": "SELECT 1"})

    assert captured["user"] == "tester"


# ---------------------------------------------------------------------------
# 定时轮换管理页面
# ---------------------------------------------------------------------------


def test_rotation_policy_page_renders(api_client, seeded):
    resp = api_client.get("/rotation/")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "定时轮换" in body
    # 三类可调项都要出现在页面上：时间、范围、强度
    assert 'name="schedule_mode"' in body
    assert 'name="interval_hours"' in body
    assert 'name="password_length"' in body
    assert 'name="clusters"' in body
    assert 'name="instances"' in body


def test_rotation_policy_saves_interval_and_strength(api_client, seeded):
    from apps.credentials.models import RotationPolicy

    resp = api_client.post(
        "/rotation/",
        data={
            "enabled": "on",
            "schedule_mode": "interval",
            "interval_hours": 5,
            "daily_times": "00:00,12:00",
            "password_length": 16,
            "clusters": [seeded["cluster"].id],
        },
    )

    assert resp.status_code == 302
    policy = RotationPolicy.load()
    assert policy.interval_hours == 5
    assert policy.password_length == 16
    assert list(policy.clusters.values_list("id", flat=True)) == [seeded["cluster"].id]


def test_rotation_policy_saves_daily_times(api_client, seeded):
    from apps.credentials.models import RotationPolicy

    resp = api_client.post(
        "/rotation/",
        data={
            "enabled": "on",
            "schedule_mode": "daily",
            "interval_hours": 12,
            "daily_times": " 06:30 , 18:30 ",
            "password_length": 24,
        },
    )

    assert resp.status_code == 302
    policy = RotationPolicy.load()
    assert policy.schedule_mode == "daily"
    # 归一化：去掉空格、按时间升序、零填充
    assert policy.daily_times == "06:30,18:30"


def test_rotation_policy_rejects_bad_daily_time(api_client, seeded):
    from apps.credentials.models import RotationPolicy

    resp = api_client.post(
        "/rotation/",
        data={
            "enabled": "on",
            "schedule_mode": "daily",
            "interval_hours": 12,
            "daily_times": "25:00",
            "password_length": 32,
        },
    )

    assert resp.status_code == 200, "校验失败应当回显表单而不是重定向"
    assert "超出范围" in resp.content.decode()
    assert RotationPolicy.load().daily_times == "00:00,12:00", "非法输入不应落库"


def test_rotation_policy_rejects_out_of_range_interval(api_client, seeded):
    from apps.credentials.models import RotationPolicy

    resp = api_client.post(
        "/rotation/",
        data={
            "enabled": "on",
            "schedule_mode": "interval",
            "interval_hours": 999,
            "password_length": 32,
        },
    )

    assert resp.status_code == 200
    assert RotationPolicy.load().interval_hours == 12


def test_rotation_policy_run_now_executes_once(api_client, seeded):
    from apps.credentials.models import RotationPolicy

    instance = seeded["instance"]
    assert instance.password_version == 0

    resp = api_client.post("/rotation/run/")

    assert resp.status_code == 302
    instance.refresh_from_db()
    assert instance.password_version == 1

    policy = RotationPolicy.load()
    assert policy.last_run_at is not None
    assert "成功 1" in policy.last_run_summary


def test_rotation_policy_run_now_requires_login(anon_client, seeded):
    from apps.credentials.models import RotationPolicy

    resp = anon_client.post("/rotation/run/")

    assert resp.status_code == 302
    assert "/admin/login/" in resp["Location"]
    assert RotationPolicy.load().last_run_at is None
    seeded["instance"].refresh_from_db()
    assert seeded["instance"].password_version == 0


def test_rotation_policy_save_requires_login(anon_client, seeded):
    from apps.credentials.models import RotationPolicy

    resp = anon_client.post(
        "/rotation/", data={"schedule_mode": "interval", "interval_hours": 1}
    )

    assert resp.status_code == 302
    assert RotationPolicy.load().interval_hours == 12


def test_detail_page_shows_policy_schedule(api_client, seeded):
    from apps.credentials.models import RotationPolicy

    policy = RotationPolicy.load()
    policy.interval_hours = 6
    policy.save()

    body = api_client.get(f"/instances/{seeded['instance'].id}/").content.decode()

    assert "每 6 小时" in body
    assert "每 12 小时" not in body, "不能残留写死的旧周期"


def test_detail_page_marks_instance_outside_scope(api_client, seeded):
    from apps.credentials.models import RotationPolicy

    other_cluster = Cluster.objects.create(
        name="other-cluster",
        department=seeded["dept"],
        engine=Cluster.Engine.MYSQL,
        env=Cluster.Env.DEV,
    )
    policy = RotationPolicy.load()
    policy.clusters.set([other_cluster])
    policy.save()

    body = api_client.get(f"/instances/{seeded['instance'].id}/").content.decode()

    assert "在本次轮换范围之外" in body


# ---------------------------------------------------------------------------
# 详情页快速连接（右侧可折叠终端面板）
# ---------------------------------------------------------------------------


def test_detail_page_has_quick_connect_button(api_client, seeded):
    """详情页要有快速连接入口，且带上实例 id 供面板切换目标。"""
    instance = seeded["instance"]

    body = api_client.get(f"/instances/{instance.id}/").content.decode()

    assert 'id="quick-connect"' in body
    assert f'data-instance-id="{instance.id}"' in body


def test_detail_page_quick_connect_disabled_until_verified(api_client, seeded):
    """默认是置灰的：没验证过凭据之前不允许连接。"""
    body = api_client.get(f"/instances/{seeded['instance'].id}/").content.decode()

    start = body.index('id="quick-connect"')
    tag = body[start:start + 400]
    assert "disabled" in tag


def test_quick_console_drawer_present_on_every_page(api_client, seeded):
    """抽屉骨架挂在 base.html 上，任何页面都能唤起。"""
    for path in ["/", "/instances/", f"/instances/{seeded['instance'].id}/", "/rotation/"]:
        body = api_client.get(path).content.decode()
        assert 'id="qc-drawer"' in body, path
        assert 'id="qc-handle"' in body, path
        assert "js/quick_console.js" in body, path


def test_quick_console_script_exists_and_reuses_endpoints():
    """静态脚本要真的存在，且复用既有端点而不是另造执行通道。

    这里直接读文件而不是走 HTTP：``python manage.py test`` / pytest 的
    Client 不挂 StaticFilesHandler（那是 runserver 才有的），请求 /static/
    永远 404，用它对静态资源做断言只会得到一个假阴性。
    """
    from pathlib import Path

    from django.conf import settings

    path = Path(settings.BASE_DIR) / "static" / "js" / "quick_console.js"
    assert path.exists(), "缺少 static/js/quick_console.js"

    body = path.read_text(encoding="utf-8")
    assert "QuickConsole" in body
    assert "/console/switch.json" in body
    assert "/exec/" in body
    # 危险语句的二次确认口径必须与全屏控制台一致
    assert "confirm_required" in body


def test_detail_page_quick_connect_requires_active_instance(api_client, seeded):
    """停用实例的按钮不能被 PSA 放开：data-active 交给前端判定。"""
    instance = seeded["instance"]
    instance.is_active = False
    instance.save(update_fields=["is_active"])

    body = api_client.get(f"/instances/{instance.id}/").content.decode()

    assert 'data-active="0"' in body
