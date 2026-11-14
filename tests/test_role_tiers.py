"""危险操作权限分级测试。

背景：在本轮之前，所有写操作只判断「有没有登录」。一个为了看台账而开的
临时账号，同样能删实例、批量轮换全公司密码、在控制台 `DROP TABLE`。
本文件把三级分档钉死，避免以后有人图省事把某个端点的权限校验去掉。

三级：
- 只读：无自定义权限，能看不能改。
- 操作员 `operate_assets`：台账增删改、单实例探测与轮换。
- 管理员：上面全部 + 控制台执行、批量轮换、重算统计、删除集群/部门。

注意 ``conftest.py`` 的 ``api_client`` 用的是 superuser——``has_perm``
对 superuser 恒为 True。所以这里必须另建普通用户，否则测不出任何拦截。
"""

from __future__ import annotations

import pytest

from apps.assets.models import Cluster, Department, Instance
from apps.assets.roles import sync_role_groups
from common.permissions import (
    PERM_ADMIN_BULK_ROTATE,
    PERM_ADMIN_CONSOLE,
    PERM_ADMIN_DELETE_CASCADE,
    PERM_ADMIN_REBUILD_STATS,
    PERM_OPERATE_ASSETS,
    ROLE_ADMIN,
    ROLE_DEFINITIONS,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    has_perm,
)

pytestmark = pytest.mark.django_db

PASSWORD = "Plain@123456"


# ---------------------------------------------------------------------------
# 夹具：三类用户的已登录客户端
# ---------------------------------------------------------------------------


def _client_for(username: str, groups: list[str] | None = None):
    """建一个非 superuser 的普通账号，按需加入角色组。"""
    from django.contrib.auth.models import Group, User
    from django.test import Client

    user, created = User.objects.get_or_create(
        username=username, defaults={"is_staff": False, "is_superuser": False}
    )
    if created:
        user.set_password(PASSWORD)
        user.save()
    if groups:
        user.groups.set([Group.objects.get(name=name) for name in groups])
    else:
        user.groups.clear()

    client = Client()
    client.force_login(user)
    return client


@pytest.fixture(autouse=True)
def ensure_roles(db):
    """保证角色与权限已同步。

    生产里由 post_migrate 信号完成；测试库建立时迁移也跑过一遍。
    这里再同步一次，让用例不依赖迁移执行顺序。
    """
    sync_role_groups()


@pytest.fixture
def viewer_client(db):
    return _client_for("viewer_user", [ROLE_VIEWER])


@pytest.fixture
def operator_client(db):
    return _client_for("operator_user", [ROLE_OPERATOR])


@pytest.fixture
def admin_client(db):
    """非 superuser 的管理员：真正靠权限点通过，而不是靠 superuser 后门。"""
    return _client_for("plain_admin", [ROLE_ADMIN])


@pytest.fixture
def seeded(db):
    from apps.credentials.crypto import encrypt_secret

    dept = Department.objects.create(name="基础架构部", code="INFRA")
    cluster = Cluster.objects.create(
        name="core-mysql-prod",
        department=dept,
        engine="mysql",
        env="prod",
        topology="master_slave",
    )
    instance = Instance.objects.create(
        name="core-01",
        cluster=cluster,
        host="10.0.0.1",
        port=3306,
        username="app",
        # 轮换要先用旧密码登录目标库，因此必须"已登记密码"
        password_ciphertext=encrypt_secret("Old@Passw0rd01"),
    )
    return {"dept": dept, "cluster": cluster, "instance": instance}


@pytest.fixture
def empty_dept(db):
    """没有下属集群的部门——删除部门要求子资源为空。"""
    return Department.objects.create(name="空部门", code="EMPTY")


@pytest.fixture
def empty_cluster(db, empty_dept):
    """没有下属实例的集群。"""
    return Cluster.objects.create(
        name="empty-cluster", department=empty_dept, engine="mysql", env="dev"
    )


# ---------------------------------------------------------------------------
# 角色组本身
# ---------------------------------------------------------------------------


class TestRoleGroups:
    def test_three_roles_exist(self):
        from django.contrib.auth.models import Group

        names = set(Group.objects.values_list("name", flat=True))
        assert {ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN} <= names

    def test_viewer_has_no_write_perms(self):
        from django.contrib.auth.models import Group

        group = Group.objects.get(name=ROLE_VIEWER)
        assert group.permissions.count() == 0

    def test_admin_group_has_all_declared_perms(self):
        from django.contrib.auth.models import Group

        group = Group.objects.get(name=ROLE_ADMIN)
        codenames = {p.codename for p in group.permissions.all()}
        expected = {p.split(".", 1)[1] for p in ROLE_DEFINITIONS[ROLE_ADMIN]}
        assert codenames == expected

    def test_sync_is_idempotent(self):
        """重复同步不应报错，也不应改变权限集合。"""
        from django.contrib.auth.models import Group

        before = {p.codename for p in Group.objects.get(name=ROLE_ADMIN).permissions.all()}
        sync_role_groups()
        sync_role_groups()
        after = {p.codename for p in Group.objects.get(name=ROLE_ADMIN).permissions.all()}
        assert before == after

    def test_sync_does_not_touch_custom_groups(self):
        """运维自建的组不能被同步逻辑删掉或改权限。"""
        from django.contrib.auth.models import Group, Permission

        custom = Group.objects.create(name="DBA 值班")
        custom.permissions.add(Permission.objects.filter(codename="admin_console").first())

        sync_role_groups()

        assert Group.objects.filter(name="DBA 值班").exists()
        assert Group.objects.get(name="DBA 值班").permissions.count() == 1


# ---------------------------------------------------------------------------
# has_perm 语义
# ---------------------------------------------------------------------------


class TestHasPerm:
    def test_anonymous_has_no_perms(self):
        from django.contrib.auth.models import AnonymousUser

        assert has_perm(AnonymousUser(), PERM_OPERATE_ASSETS) is False

    def test_none_user_has_no_perms(self):
        assert has_perm(None, PERM_OPERATE_ASSETS) is False

    def test_viewer_cannot_operate(self, viewer_client):
        user = viewer_client.session["_auth_user_id"]
        from django.contrib.auth.models import User

        assert has_perm(User.objects.get(pk=user), PERM_OPERATE_ASSETS) is False

    def test_operator_can_operate_but_not_admin_actions(self, operator_client):
        from django.contrib.auth.models import User

        user = User.objects.get(pk=operator_client.session["_auth_user_id"])
        assert has_perm(user, PERM_OPERATE_ASSETS) is True
        assert has_perm(user, PERM_ADMIN_CONSOLE) is False
        assert has_perm(user, PERM_ADMIN_BULK_ROTATE) is False
        assert has_perm(user, PERM_ADMIN_DELETE_CASCADE) is False

    def test_superuser_passes_everything(self, api_client, login_user):
        """升级后管理员不能被锁在门外。

        注意要同时依赖 ``api_client``：``login_user`` 只负责把用户查出来，
        用户本身是 ``api_client`` 建出来的。只写 ``login_user`` 会因为用户
        还不存在而 DoesNotExist。
        """
        for perm in (
            PERM_OPERATE_ASSETS,
            PERM_ADMIN_CONSOLE,
            PERM_ADMIN_BULK_ROTATE,
            PERM_ADMIN_REBUILD_STATS,
            PERM_ADMIN_DELETE_CASCADE,
        ):
            assert has_perm(login_user, perm) is True


# ---------------------------------------------------------------------------
# 页面：只读写操作被拦
# ---------------------------------------------------------------------------


class TestViewerBlockedOnPages:
    @pytest.mark.parametrize(
        "path",
        ["/departments/new/", "/clusters/new/", "/instances/new/"],
    )
    def test_viewer_cannot_open_create_forms(self, viewer_client, path):
        resp = viewer_client.get(path)
        # 回首页并给出提示，而不是裸 403——运维要知道该找谁
        assert resp.status_code == 302
        assert resp["Location"] == "/"

    def test_viewer_cannot_create_department(self, viewer_client):
        viewer_client.post("/departments/new/", {"name": "只读用户偷加的部门"})
        assert Department.objects.filter(name="只读用户偷加的部门").exists() is False

    def test_viewer_cannot_delete_instance(self, viewer_client, seeded):
        viewer_client.post(f"/instances/{seeded['instance'].id}/delete/")
        assert Instance.objects.filter(pk=seeded["instance"].pk).exists() is True

    def test_viewer_cannot_probe(self, viewer_client, seeded):
        viewer_client.post(f"/instances/{seeded['instance'].id}/probe/")
        seeded["instance"].refresh_from_db()
        assert seeded["instance"].last_probed_at is None

    def test_viewer_cannot_rotate(self, viewer_client, seeded):
        viewer_client.post(f"/instances/{seeded['instance'].id}/rotate/")
        seeded["instance"].refresh_from_db()
        assert seeded["instance"].password_version == 0

    def test_viewer_cannot_rebuild_stats(self, viewer_client):
        viewer_client.post("/stats/rebuild/")
        from apps.metrics.models import DailyInstanceStat

        assert DailyInstanceStat.objects.count() == 0

    def test_viewer_cannot_run_bulk_rotation(self, viewer_client):
        resp = viewer_client.post("/rotation/run/")
        assert resp.status_code == 302

    def test_viewer_can_still_browse(self, viewer_client, seeded):
        """只读账号的价值就是能看——不能被误伤。"""
        assert viewer_client.get("/").status_code == 200
        assert viewer_client.get("/instances/").status_code == 200
        assert (
            viewer_client.get(f"/instances/{seeded['instance'].id}/").status_code == 200
        )

    def test_viewer_can_view_rotation_policy_page(self, viewer_client):
        """知道系统什么时候会自动改密码，本身是只读用户该看到的信息。"""
        assert viewer_client.get("/rotation/").status_code == 200

    def test_viewer_cannot_modify_rotation_policy(self, viewer_client):
        from apps.credentials.models import RotationPolicy

        before = RotationPolicy.load().enabled
        viewer_client.post(
            "/rotation/",
            {"enabled": "" if before else "on", "schedule_mode": "interval", "interval_hours": 12},
        )
        assert RotationPolicy.load().enabled == before


# ---------------------------------------------------------------------------
# 页面：操作员能做的与不能做的
# ---------------------------------------------------------------------------


class TestOperatorAllowed:
    def test_operator_can_create_department(self, operator_client):
        operator_client.post("/departments/new/", {"name": "操作员建的部门", "code": "OPD"})
        assert Department.objects.filter(name="操作员建的部门").exists() is True

    def test_operator_can_delete_instance(self, operator_client, seeded):
        operator_client.post(f"/instances/{seeded['instance'].id}/delete/")
        assert Instance.objects.filter(pk=seeded["instance"].pk).exists() is False

    def test_operator_can_probe(self, operator_client, seeded):
        operator_client.post(f"/instances/{seeded['instance'].id}/probe/")
        seeded["instance"].refresh_from_db()
        assert seeded["instance"].last_probed_at is not None


class TestOperatorBlockedOnAdminActions:
    def test_operator_cannot_delete_cluster(self, operator_client, seeded):
        """删集群会连带其下全部实例，影响面是批量的。"""
        operator_client.post(f"/clusters/{seeded['cluster'].id}/delete/")
        assert Cluster.objects.filter(pk=seeded["cluster"].pk).exists() is True

    def test_operator_cannot_delete_department(self, operator_client, seeded):
        operator_client.post(f"/departments/{seeded['dept'].id}/delete/")
        assert Department.objects.filter(pk=seeded["dept"].pk).exists() is True

    def test_operator_cannot_execute_in_console(self, operator_client, seeded):
        resp = operator_client.post(
            f"/console/{seeded['instance'].id}/exec/", {"sql": "SELECT 1"}
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "permission_denied"

    def test_operator_cannot_rebuild_stats(self, operator_client):
        operator_client.post("/stats/rebuild/")
        from apps.metrics.models import DailyInstanceStat

        assert DailyInstanceStat.objects.count() == 0

    def test_operator_cannot_run_bulk_rotation(self, operator_client):
        resp = operator_client.post("/rotation/run/")
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# 页面：普通管理员通过（不靠 superuser）
# ---------------------------------------------------------------------------


class TestPlainAdminAllowed:
    def test_admin_can_delete_cluster(self, admin_client, empty_cluster):
        """删空集群能成功。带实例的集群会被业务规则拦下，那是另一回事。"""
        admin_client.post(f"/clusters/{empty_cluster.id}/delete/")
        assert Cluster.objects.filter(pk=empty_cluster.pk).exists() is False

    def test_admin_can_delete_department(self, admin_client, empty_dept):
        admin_client.post(f"/departments/{empty_dept.id}/delete/")
        assert Department.objects.filter(pk=empty_dept.pk).exists() is False

    def test_admin_delete_cluster_with_instances_still_blocked(self, admin_client, seeded):
        """权限够也不行：有实例的集群必须先去删实例，避免误删整片。"""
        admin_client.post(f"/clusters/{seeded['cluster'].id}/delete/")
        assert Cluster.objects.filter(pk=seeded["cluster"].pk).exists() is True

    def test_admin_can_modify_rotation_policy(self, admin_client):
        resp = admin_client.post(
            "/rotation/",
            {
                "enabled": "on",
                "schedule_mode": "interval",
                "interval_hours": 6,
                # password_length 是下拉选项（16/24/32/48），漏了或填非法值
                # 都会被表单拦下，那样测的就不是权限了
                "password_length": 32,
            },
        )
        assert resp.status_code == 302
        from apps.credentials.models import RotationPolicy

        assert RotationPolicy.load().interval_hours == 6


# ---------------------------------------------------------------------------
# API：403 而非 401
# ---------------------------------------------------------------------------


class TestApiPermissionDenied:
    def test_viewer_api_create_department_403(self, viewer_client):
        resp = viewer_client.post(
            "/api/v1/departments",
            data={"name": "只读用户 API 建的"},
            content_type="application/json",
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["code"] == "PERMISSION_DENIED"
        assert body["detail"]["required_permission"] == PERM_OPERATE_ASSETS
        assert Department.objects.filter(name="只读用户 API 建的").exists() is False

    def test_operator_api_delete_cluster_403(self, operator_client, seeded):
        resp = operator_client.delete(f"/api/v1/clusters/{seeded['cluster'].id}")
        assert resp.status_code == 403
        assert Cluster.objects.filter(pk=seeded["cluster"].pk).exists() is True

    def test_operator_api_console_execute_403(self, operator_client, seeded):
        resp = operator_client.post(
            f"/api/v1/instances/{seeded['instance'].id}/execute",
            data={"sql": "SELECT 1"},
            content_type="application/json",
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "PERMISSION_DENIED"

    def test_operator_api_rebuild_stats_403(self, operator_client):
        resp = operator_client.post("/api/v1/stats/daily/rebuild?stat_date=2026-11-13")
        assert resp.status_code == 403

    def test_operator_api_rotate_allowed(self, operator_client, seeded, stub_password_change):
        """操作员可以轮换单实例密码——这是日常运维动作。"""
        resp = operator_client.post(
            f"/api/v1/instances/{seeded['instance'].id}/rotate-password"
        )
        assert resp.status_code == 200

    def test_403_is_not_401(self, viewer_client):
        """401 会让调用方去重新登录，而实际上登录了也没用——必须区分。"""
        resp = viewer_client.post(
            "/api/v1/departments",
            data={"name": "x"},
            content_type="application/json",
        )
        assert resp.status_code == 403
        assert resp.status_code != 401

    def test_anonymous_still_401(self, anon_client):
        """未登录仍是 401：先知道你是谁，才谈得上你有没有权限。"""
        resp = anon_client.post(
            "/api/v1/departments",
            data={"name": "匿名建的"},
            content_type="application/json",
        )
        assert resp.status_code == 401
