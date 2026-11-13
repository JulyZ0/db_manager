"""运维界面测试。

重点验证三件事：
1. 每个页面都能真实渲染（模板语法、URL 反向解析、字段名拼写都对）；
2. 表单能完成创建/编辑的完整往返；
3. 页面上的探测、轮换、统计按钮真的驱动核心算法并写回结果。
"""

from __future__ import annotations

import pytest

from apps.assets.models import Cluster, Department, Instance

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
    )
    instance = Instance.objects.create(
        name="core-01",
        cluster=cluster,
        host="10.0.0.1",
        port=3306,
        username="app",
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
