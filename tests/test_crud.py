"""CRUD 接口测试（部门 / 集群 / 实例）。

四个核心算法（探测、加解密、轮换、统计）尚未手写，
涉及它们的路径在本文件里用 monkeypatch 打桩，
这里测的是接口层自身的行为：过滤、唯一性、状态码、密钥不外泄。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def dept(api_client):
    return api_client.post(
        "/api/v1/departments",
        data={"name": "基础架构部", "code": "INFRA", "owner": "张三"},
        content_type="application/json",
    ).json()


@pytest.fixture
def cluster(api_client, dept):
    return api_client.post(
        "/api/v1/clusters",
        data={
            "name": "core-mysql-prod",
            "department_id": dept["id"],
            "env": "prod",
            # 用例里要建从库实例，架构必须是主从：架构决定可选角色
            "topology": "master_slave",
        },
        content_type="application/json",
    ).json()


# ---------------------------------------------------------------------------
# 部门
# ---------------------------------------------------------------------------


class TestDepartmentCRUD:
    def test_create_returns_201_and_fields(self, api_client):
        resp = api_client.post(
            "/api/v1/departments",
            data={"name": "研发部", "code": "RD", "owner": "李四"},
            content_type="application/json",
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "研发部"
        assert body["code"] == "RD"
        assert body["cluster_count"] == 0
        assert body["is_active"] is True

    def test_duplicate_name_returns_409(self, api_client, dept):
        resp = api_client.post(
            "/api/v1/departments",
            data={"name": "基础架构部"},
            content_type="application/json",
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "CONFLICT"
        assert resp.json()["detail"]["field"] == "name"

    def test_duplicate_code_returns_409(self, api_client, dept):
        resp = api_client.post(
            "/api/v1/departments",
            data={"name": "另一个部门", "code": "INFRA"},
            content_type="application/json",
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["field"] == "code"

    def test_list_returns_cluster_count(self, api_client, dept, cluster):
        resp = api_client.get("/api/v1/departments")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["results"][0]["cluster_count"] == 1

    def test_list_search_filters(self, api_client, dept):
        api_client.post(
            "/api/v1/departments",
            data={"name": "市场部", "code": "MKT"},
            content_type="application/json",
        )
        resp = api_client.get("/api/v1/departments?search=市场")
        assert resp.json()["count"] == 1
        assert resp.json()["results"][0]["name"] == "市场部"

    def test_pagination_structure(self, api_client):
        for i in range(5):
            api_client.post(
                "/api/v1/departments",
                data={"name": f"部门{i}"},
                content_type="application/json",
            )
        resp = api_client.get("/api/v1/departments?page=2&page_size=2")
        body = resp.json()
        assert body["count"] == 5
        assert body["page"] == 2
        assert body["page_size"] == 2
        assert len(body["results"]) == 2

    def test_patch_only_updates_given_fields(self, api_client, dept):
        resp = api_client.patch(
            f"/api/v1/departments/{dept['id']}",
            data={"owner": "王五"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["owner"] == "王五"
        assert body["name"] == "基础架构部"  # 未被清空

    def test_delete_with_clusters_returns_409(self, api_client, dept, cluster):
        resp = api_client.delete(f"/api/v1/departments/{dept['id']}")
        assert resp.status_code == 409
        assert resp.json()["detail"]["cluster_count"] == 1

    def test_delete_empty_department_returns_204(self, api_client, dept):
        resp = api_client.delete(f"/api/v1/departments/{dept['id']}")
        assert resp.status_code == 204

    def test_get_missing_returns_404(self, api_client):
        resp = api_client.get("/api/v1/departments/99999")
        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 集群
# ---------------------------------------------------------------------------


class TestClusterCRUD:
    def test_create_requires_existing_department(self, api_client):
        resp = api_client.post(
            "/api/v1/clusters",
            data={"name": "c1", "department_id": 99999},
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_duplicate_name_in_same_department_returns_409(self, api_client, dept, cluster):
        resp = api_client.post(
            "/api/v1/clusters",
            data={"name": "core-mysql-prod", "department_id": dept["id"]},
            content_type="application/json",
        )
        assert resp.status_code == 409

    def test_same_name_in_different_department_allowed(self, api_client, dept, cluster):
        other = api_client.post(
            "/api/v1/departments",
            data={"name": "研发部"},
            content_type="application/json",
        ).json()
        resp = api_client.post(
            "/api/v1/clusters",
            data={"name": "core-mysql-prod", "department_id": other["id"]},
            content_type="application/json",
        )
        assert resp.status_code == 201, "跨部门允许同名集群"

    def test_filter_by_department_env_engine(self, api_client, dept, cluster):
        assert api_client.get(
            f"/api/v1/clusters?department_id={dept['id']}"
        ).json()["count"] == 1
        assert api_client.get("/api/v1/clusters?env=prod").json()["count"] == 1
        assert api_client.get("/api/v1/clusters?env=dev").json()["count"] == 0
        assert api_client.get("/api/v1/clusters?engine=mysql").json()["count"] == 1
        assert api_client.get("/api/v1/clusters?engine=redis").json()["count"] == 0

    def test_delete_with_instances_returns_409(self, api_client, cluster):
        api_client.post(
            "/api/v1/instances",
            data={
                "name": "i1",
                "cluster_id": cluster["id"],
                "host": "10.0.0.1",
                "port": 3306,
                "username": "app",
            },
            content_type="application/json",
        )
        resp = api_client.delete(f"/api/v1/clusters/{cluster['id']}")
        assert resp.status_code == 409
        assert resp.json()["detail"]["instance_count"] == 1

    def test_create_with_alias_returns_it(self, api_client, dept):
        resp = api_client.post(
            "/api/v1/clusters",
            data={"name": "交易主库", "alias": "jq1", "department_id": dept["id"]},
            content_type="application/json",
        )
        assert resp.status_code == 201
        assert resp.json()["alias"] == "jq1"

    def test_duplicate_alias_returns_409(self, api_client, dept, cluster):
        """集群别名全局唯一：跨部门也不能重复。"""
        other = api_client.post(
            "/api/v1/departments", data={"name": "研发部"}, content_type="application/json"
        ).json()
        api_client.patch(
            f"/api/v1/clusters/{cluster['id']}",
            data={"alias": "jq1"},
            content_type="application/json",
        )
        resp = api_client.post(
            "/api/v1/clusters",
            data={"name": "另一个", "alias": "jq1", "department_id": other["id"]},
            content_type="application/json",
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["field"] == "alias"

    def test_all_digit_alias_rejected(self, api_client, dept):
        resp = api_client.post(
            "/api/v1/clusters",
            data={"name": "c-digit", "alias": "123", "department_id": dept["id"]},
            content_type="application/json",
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["field"] == "alias"

    def test_alias_with_special_chars_rejected(self, api_client, dept):
        for bad in ("jq 1", "jq@1", "jq.1", "jq;1"):
            resp = api_client.post(
                "/api/v1/clusters",
                data={"name": f"c-{bad}", "alias": bad, "department_id": dept["id"]},
                content_type="application/json",
            )
            assert resp.status_code == 409, bad

    def test_patch_can_clear_alias(self, api_client, dept, cluster):
        api_client.patch(
            f"/api/v1/clusters/{cluster['id']}",
            data={"alias": "jq1"},
            content_type="application/json",
        )
        resp = api_client.patch(
            f"/api/v1/clusters/{cluster['id']}",
            data={"alias": None},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json()["alias"] is None

    def test_patch_keeps_own_alias(self, api_client, dept, cluster):
        """把别名改成自己原来的值不算冲突。"""
        api_client.patch(
            f"/api/v1/clusters/{cluster['id']}",
            data={"alias": "jq1"},
            content_type="application/json",
        )
        resp = api_client.patch(
            f"/api/v1/clusters/{cluster['id']}",
            data={"alias": "jq1", "env": "test"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json()["alias"] == "jq1"


# ---------------------------------------------------------------------------
# 实例
# ---------------------------------------------------------------------------


class TestInstanceCRUD:
    def _payload(self, cluster_id, **overrides):
        data = {
            "name": "core-01",
            "cluster_id": cluster_id,
            "host": "10.0.12.31",
            "port": 3306,
            "username": "app_rw",
        }
        data.update(overrides)
        return data

    def test_create_without_password(self, api_client, cluster):
        resp = api_client.post(
            "/api/v1/instances",
            data=self._payload(cluster["id"]),
            content_type="application/json",
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["password_set"] is False
        assert body["password_version"] == 0
        assert body["status"] == "unknown"

    def test_create_with_password_never_echoes_secret(self, api_client, cluster, monkeypatch):
        """密码只进不出：写入后响应里不得出现明文或密文。"""
        from apps.credentials import crypto

        monkeypatch.setattr(crypto, "encrypt_secret", lambda p: f"v1:FAKE({p})")

        resp = api_client.post(
            "/api/v1/instances",
            data=self._payload(cluster["id"], password="S3cr3t!"),
            content_type="application/json",
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["password_set"] is True
        assert body["password_version"] == 1
        assert "S3cr3t!" not in resp.content.decode()
        assert "FAKE" not in resp.content.decode()
        assert "password_ciphertext" not in body

    def test_duplicate_endpoint_in_cluster_returns_409(self, api_client, cluster):
        payload = self._payload(cluster["id"])
        assert api_client.post(
            "/api/v1/instances", data=payload, content_type="application/json"
        ).status_code == 201
        resp = api_client.post(
            "/api/v1/instances",
            data=self._payload(cluster["id"], name="另一个"),
            content_type="application/json",
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["field"] == "host_port"

    def test_port_out_of_range_rejected(self, api_client, cluster):
        resp = api_client.post(
            "/api/v1/instances",
            data=self._payload(cluster["id"], port=70000),
            content_type="application/json",
        )
        assert resp.status_code in (409, 422), "端口越界必须被拒绝"

    def test_filter_by_department_crosses_two_tables(self, api_client, dept, cluster):
        api_client.post(
            "/api/v1/instances",
            data=self._payload(cluster["id"]),
            content_type="application/json",
        )
        resp = api_client.get(f"/api/v1/instances?department_id={dept['id']}")
        assert resp.json()["count"] == 1

        other = api_client.post(
            "/api/v1/departments", data={"name": "其他部"}, content_type="application/json"
        ).json()
        assert api_client.get(
            f"/api/v1/instances?department_id={other['id']}"
        ).json()["count"] == 0

    def test_filter_by_status_and_role(self, api_client, cluster):
        api_client.post(
            "/api/v1/instances",
            data=self._payload(cluster["id"]),
            content_type="application/json",
        )
        api_client.post(
            "/api/v1/instances",
            data=self._payload(cluster["id"], host="10.0.12.32", role="slave"),
            content_type="application/json",
        )
        assert api_client.get("/api/v1/instances?role=slave").json()["count"] == 1
        assert api_client.get("/api/v1/instances?status=online").json()["count"] == 0

    def test_patch_password_bumps_version(self, api_client, cluster, monkeypatch):
        from apps.credentials import crypto

        monkeypatch.setattr(crypto, "encrypt_secret", lambda p: f"v1:FAKE({p})")

        created = api_client.post(
            "/api/v1/instances",
            data=self._payload(cluster["id"], password="first"),
            content_type="application/json",
        ).json()
        assert created["password_version"] == 1

        resp = api_client.patch(
            f"/api/v1/instances/{created['id']}",
            data={"password": "second"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.json()["password_version"] == 2

    def test_patch_without_password_keeps_version(self, api_client, cluster, monkeypatch):
        from apps.credentials import crypto

        monkeypatch.setattr(crypto, "encrypt_secret", lambda p: f"v1:FAKE({p})")
        created = api_client.post(
            "/api/v1/instances",
            data=self._payload(cluster["id"], password="first"),
            content_type="application/json",
        ).json()

        resp = api_client.patch(
            f"/api/v1/instances/{created['id']}",
            data={"remark": "改了备注"},
            content_type="application/json",
        )
        assert resp.json()["password_version"] == 1

    def test_delete_cascades_rotation_logs(self, api_client, cluster):
        created = api_client.post(
            "/api/v1/instances",
            data=self._payload(cluster["id"]),
            content_type="application/json",
        ).json()

        from apps.assets.models import Instance, PasswordRotationLog

        PasswordRotationLog.objects.create(
            instance_id=created["id"], new_fingerprint="abc", status="success"
        )
        assert PasswordRotationLog.objects.count() == 1

        assert api_client.delete(f"/api/v1/instances/{created['id']}").status_code == 204
        assert PasswordRotationLog.objects.count() == 0
        assert not Instance.objects.filter(id=created["id"]).exists()

    def test_list_never_exposes_ciphertext(self, api_client, cluster, monkeypatch):
        from apps.credentials import crypto

        monkeypatch.setattr(crypto, "encrypt_secret", lambda p: f"v1:TOPSECRET")
        api_client.post(
            "/api/v1/instances",
            data=self._payload(cluster["id"], password="whatever"),
            content_type="application/json",
        )
        body = api_client.get("/api/v1/instances").content.decode()
        assert "TOPSECRET" not in body
        assert "password_ciphertext" not in body
