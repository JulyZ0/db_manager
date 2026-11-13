"""骨架冒烟测试。

目的：确认装配层可用（URL 挂载、异常处理器、中间件、OpenAPI 生成），
不覆盖业务逻辑——业务逻辑留给你手写后补充测试。
"""

from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_healthz_ok(api_client):
    resp = api_client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"


def test_docs_page_available(api_client):
    resp = api_client.get("/api/v1/docs")
    assert resp.status_code == 200


def test_openapi_contains_all_endpoints(api_client):
    resp = api_client.get("/api/v1/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]

    expected = [
        "/api/v1/departments",
        "/api/v1/departments/{department_id}",
        "/api/v1/clusters",
        "/api/v1/clusters/{cluster_id}",
        "/api/v1/instances",
        "/api/v1/instances/{instance_id}",
        "/api/v1/instances/{instance_id}/probe",
        "/api/v1/instances/{instance_id}/rotate-password",
        "/api/v1/instances/{instance_id}/rotations",
        "/api/v1/stats/daily",
        "/api/v1/stats/daily/rebuild",
    ]
    missing = [p for p in expected if p not in paths]
    assert not missing, f"OpenAPI 缺少端点: {missing}"


def test_request_id_header_present(api_client):
    resp = api_client.get("/api/v1/departments")
    assert "X-Request-ID" in resp


def test_request_id_header_on_skipped_path(api_client):
    """被统计排除的路径也要带 request_id。"""
    resp = api_client.get("/healthz")
    assert "X-Request-ID" in resp


@pytest.mark.django_db
def test_migrations_apply_cleanly(api_client):
    """模型可写入，说明唯一约束与字段定义无误。"""
    from apps.assets.models import Cluster, Department

    dept = Department.objects.create(name="基础架构部", code="INFRA")
    cluster = Cluster.objects.create(name="core-mysql-prod", department=dept)
    assert cluster.department_id == dept.id
    assert str(cluster) == "基础架构部/core-mysql-prod"


@pytest.mark.django_db
def test_duplicate_cluster_name_in_same_department_rejected(api_client):
    from django.db import IntegrityError, transaction

    from apps.assets.models import Cluster, Department

    dept = Department.objects.create(name="研发部")
    Cluster.objects.create(name="dup", department=dept)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Cluster.objects.create(name="dup", department=dept)


@pytest.mark.django_db
def test_port_range_constraint(api_client):
    from django.db import IntegrityError, transaction

    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="运维部")
    cluster = Cluster.objects.create(name="c1", department=dept)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Instance.objects.create(
                name="bad",
                cluster=cluster,
                host="10.0.0.1",
                port=70000,
                username="u",
            )


@pytest.mark.django_db
def test_not_found_returns_unified_error_body(api_client):
    """资源不存在时返回统一错误体，而不是 Django 默认的 HTML 错误页。"""
    resp = api_client.get("/api/v1/departments/999")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "NOT_FOUND"
    assert set(body) <= {"code", "message", "detail"}
    assert "Traceback" not in resp.content.decode()
