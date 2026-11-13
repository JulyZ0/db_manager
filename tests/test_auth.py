"""鉴权边界测试。

口径（用户已确认）：
- 浏览类 GET 免登录 —— 内网运维随手看台账，不强制账号；
- 写操作必须登录 —— 新建/修改/删除/探测/轮换/统计重算，API 与页面一致。

这些用例的存在意义是：把"哪些入口必须被拦、哪些必须放行"钉死，
避免以后有人图省事给某个写端点去掉 auth。
"""

from __future__ import annotations

import pytest

from apps.assets.models import Cluster, Department, Instance

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded():
    dept = Department.objects.create(name="基础架构部", code="INFRA")
    cluster = Cluster.objects.create(
        name="core-mysql-prod", department=dept, engine="mysql", env="prod"
    )
    return Instance.objects.create(
        name="core-01", cluster=cluster, host="10.0.0.1", port=3306, username="app"
    )


# ---------------------------------------------------------------------------
# 页面：读放行
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/departments/",
        "/clusters/",
        "/instances/",
        "/stats/",
        "/slow-requests/",
    ],
)
def test_anonymous_can_browse_pages(anon_client, path):
    assert anon_client.get(path).status_code == 200


def test_anonymous_can_view_instance_detail(anon_client, seeded):
    assert anon_client.get(f"/instances/{seeded.id}/").status_code == 200


# ---------------------------------------------------------------------------
# 页面：写拦截
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/departments/new/", "/clusters/new/", "/instances/new/"],
)
def test_anonymous_cannot_open_create_forms(anon_client, path):
    resp = anon_client.get(path)
    assert resp.status_code == 302
    assert "/admin/login/" in resp["Location"]


@pytest.mark.parametrize(
    "method,path_builder",
    [
        ("post", lambda i: f"/instances/{i.id}/delete/"),
        ("post", lambda i: f"/instances/{i.id}/probe/"),
        ("post", lambda i: f"/instances/{i.id}/rotate/"),
    ],
)
def test_anonymous_cannot_run_dangerous_instance_actions(
    anon_client, seeded, method, path_builder
):
    resp = getattr(anon_client, method)(path_builder(seeded))
    assert resp.status_code == 302
    assert "/admin/login/" in resp["Location"]


def test_anonymous_delete_does_not_actually_delete(anon_client, seeded):
    """光看状态码不够——必须确认数据还在。"""
    anon_client.post(f"/instances/{seeded.id}/delete/")
    assert Instance.objects.filter(pk=seeded.pk).exists() is True


def test_anonymous_cannot_create_department(anon_client):
    anon_client.post("/departments/new/", {"name": "偷偷加的部门"})
    assert Department.objects.filter(name="偷偷加的部门").exists() is False


def test_anonymous_rebuild_stats_blocked(anon_client):
    resp = anon_client.post("/stats/rebuild/")
    assert resp.status_code == 302
    assert "/admin/login/" in resp["Location"]


# ---------------------------------------------------------------------------
# 页面：登录后放行
# ---------------------------------------------------------------------------


def test_logged_in_can_open_create_form(api_client):
    assert api_client.get("/departments/new/").status_code == 200


def test_logged_in_can_delete_instance(api_client, seeded):
    api_client.post(f"/instances/{seeded.id}/delete/")
    assert Instance.objects.filter(pk=seeded.pk).exists() is False


# ---------------------------------------------------------------------------
# API：读放行、写拦截
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/departments",
        "/api/v1/clusters",
        "/api/v1/instances",
    ],
)
def test_anonymous_api_read_allowed(anon_client, path):
    assert anon_client.get(path).status_code == 200


def test_anonymous_api_create_department_rejected(anon_client):
    resp = anon_client.post(
        "/api/v1/departments",
        data={"name": "未授权部门"},
        content_type="application/json",
    )
    assert resp.status_code == 401
    assert Department.objects.filter(name="未授权部门").exists() is False


def test_anonymous_api_patch_rejected(anon_client, seeded):
    resp = anon_client.patch(
        f"/api/v1/instances/{seeded.id}",
        data={"name": "改名"},
        content_type="application/json",
    )
    assert resp.status_code == 401
    seeded.refresh_from_db()
    assert seeded.name == "core-01"


def test_anonymous_api_delete_rejected(anon_client, seeded):
    resp = anon_client.delete(f"/api/v1/instances/{seeded.id}")
    assert resp.status_code == 401
    assert Instance.objects.filter(pk=seeded.pk).exists() is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/instances/1/probe",
        "/api/v1/instances/1/rotate-password",
        "/api/v1/stats/daily/rebuild?stat_date=2026-11-13",
    ],
)
def test_anonymous_api_action_endpoints_rejected(anon_client, path):
    assert anon_client.post(path).status_code == 401


def test_logged_in_api_create_works(api_client):
    resp = api_client.post(
        "/api/v1/departments",
        data={"name": "基础架构部"},
        content_type="application/json",
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# 防开放重定向
# ---------------------------------------------------------------------------


def test_next_param_ignores_foreign_host(anon_client, seeded):
    """Referer 指向外站时不能把 next 写成外站地址，否则就是开放重定向。"""
    resp = anon_client.post(
        f"/instances/{seeded.id}/delete/",
        headers={"Referer": "https://evil.example/steal"},
    )
    assert resp.status_code == 302
    assert "evil.example" not in resp["Location"]


def test_next_param_keeps_same_host_path(anon_client, seeded):
    resp = anon_client.post(
        f"/instances/{seeded.id}/delete/",
        headers={"Referer": f"http://testserver/instances/{seeded.id}/"},
    )
    assert resp.status_code == 302

    # next 在 query 里是 URL 编码过的，解码后再比较
    from urllib.parse import parse_qs, urlsplit

    location = urlsplit(resp["Location"])
    assert location.path == "/admin/login/"
    next_target = parse_qs(location.query)["next"][0]
    assert next_target == f"/instances/{seeded.id}/"
