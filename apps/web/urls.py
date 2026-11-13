"""运维界面路由，挂载在根路径 / 下。

与 /api/v1 的接口层完全独立：页面改动不会影响 API 契约。
"""

from __future__ import annotations

from django.urls import path

from apps.web import views

app_name = "web"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    # 部门
    path("departments/", views.department_list, name="department_list"),
    path("departments/new/", views.department_create, name="department_create"),
    path(
        "departments/<int:department_id>/edit/",
        views.department_edit,
        name="department_edit",
    ),
    path(
        "departments/<int:department_id>/delete/",
        views.department_delete,
        name="department_delete",
    ),
    # 集群
    path("clusters/", views.cluster_list, name="cluster_list"),
    path("clusters/new/", views.cluster_create, name="cluster_create"),
    path("clusters/<int:cluster_id>/edit/", views.cluster_edit, name="cluster_edit"),
    path(
        "clusters/<int:cluster_id>/delete/", views.cluster_delete, name="cluster_delete"
    ),
    # 实例
    path("instances/", views.instance_list, name="instance_list"),
    path("instances/new/", views.instance_create, name="instance_create"),
    path(
        "instances/<int:instance_id>/", views.instance_detail, name="instance_detail"
    ),
    path(
        "instances/<int:instance_id>/edit/", views.instance_edit, name="instance_edit"
    ),
    path(
        "instances/<int:instance_id>/delete/",
        views.instance_delete,
        name="instance_delete",
    ),
    path(
        "instances/<int:instance_id>/probe/", views.instance_probe, name="instance_probe"
    ),
    # 详情页打开后异步调用：只读验证凭据，不落库
    path(
        "instances/<int:instance_id>/verify-credentials/",
        views.instance_verify_credentials,
        name="instance_verify_credentials",
    ),
    path(
        "instances/<int:instance_id>/rotate/",
        views.instance_rotate,
        name="instance_rotate",
    ),
    # 统计
    path("stats/", views.stats, name="stats"),
    path("stats/rebuild/", views.stats_rebuild, name="stats_rebuild"),
    # 定时轮换策略
    path("rotation/", views.rotation_policy, name="rotation_policy"),
    path("rotation/run/", views.rotation_run_now, name="rotation_run_now"),
    # 慢请求
    path("slow-requests/", views.slow_requests, name="slow_requests"),
    # 终端风格控制台
    path("console/", views.console, name="console"),
    path("console/instances.json", views.console_instances, name="console_instances"),
    path("console/switch.json", views.console_switch, name="console_switch"),
    path(
        "console/<int:instance_id>/verify.json",
        views.console_verify,
        name="console_verify",
    ),
    path(
        "console/<int:instance_id>/exec/",
        views.console_exec,
        name="console_exec",
    ),
]
