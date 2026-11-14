"""运维界面路由，挂载在根路径 / 下。

与 /api/v1 的接口层完全独立：页面改动不会影响 API 契约。
"""

from __future__ import annotations

from django.urls import path

from apps.web import (
    monitor_alert_views,
    monitor_instance_views,
    monitor_views,
    views,
)

app_name = "web"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    # 账号：默认口令强制改密的落点，也在侧栏里可直接进入
    path("account/password/", views.password_change, name="password_change"),
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
    path(
        "clusters/<int:cluster_id>/", views.cluster_detail, name="cluster_detail"
    ),
    path(
        "clusters/<int:cluster_id>/topology.json",
        views.cluster_topology,
        name="cluster_topology",
    ),
    # 复制关系编排（架构是集群级决策，编排入口也只放在集群侧）
    path(
        "clusters/<int:cluster_id>/replication/",
        views.cluster_replication,
        name="cluster_replication",
    ),
    path(
        "clusters/<int:cluster_id>/replication/auto/",
        views.cluster_replication_auto,
        name="cluster_replication_auto",
    ),
    # 集群监控（观测视图与资产编辑分开：页面改坏不影响 CRUD 流程）
    path(
        "clusters/<int:cluster_id>/monitor/",
        monitor_views.cluster_overview,
        name="cluster_overview",
    ),
    path(
        "clusters/<int:cluster_id>/monitor/space/",
        monitor_views.cluster_space,
        name="cluster_space",
    ),
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
    # 实例监控（只读观测视图，与资产编辑分开）
    path(
        "instances/<int:instance_id>/monitor/trend/",
        monitor_instance_views.instance_trend,
        name="instance_trend",
    ),
    path(
        "instances/<int:instance_id>/monitor/realtime/",
        monitor_instance_views.instance_realtime,
        name="instance_realtime",
    ),
    path(
        "instances/<int:instance_id>/monitor/space/",
        monitor_instance_views.instance_space,
        name="instance_space",
    ),
    path(
        "instances/<int:instance_id>/monitor/sessions/",
        monitor_instance_views.instance_sessions,
        name="instance_sessions",
    ),
    path(
        "instances/<int:instance_id>/monitor/slowlog/",
        monitor_instance_views.instance_slowlog,
        name="instance_slowlog",
    ),
    path(
        "instances/<int:instance_id>/monitor/profile/",
        monitor_views.instance_profile,
        name="instance_profile",
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
    # 监控大盘（全局观测入口）
    path("monitor/", monitor_views.monitor_dashboard, name="monitor_dashboard"),
    # 告警：事件中心 + 规则管理
    # SQL 洞察：全库 TOP 与单实例两个视角
    # 巡检评分与诊断报告
    path("monitor/inspect/", monitor_views.inspect_overview, name="inspect_overview"),
    path(
        "instances/<int:instance_id>/monitor/report/",
        monitor_views.inspect_report,
        name="inspect_report",
    ),
    path(
        "clusters/<int:cluster_id>/monitor/report/",
        monitor_views.cluster_report,
        name="cluster_report",
    ),
    # 锁分析：全库与单实例两个视角
    path("monitor/locks/", monitor_views.lock_analysis, name="lock_analysis"),
    path(
        "instances/<int:instance_id>/monitor/locks/",
        monitor_views.lock_analysis,
        name="instance_lock_analysis",
    ),
    path("monitor/sql/", monitor_views.sql_insight, name="sql_insight"),
    path(
        "instances/<int:instance_id>/monitor/sql/",
        monitor_views.sql_insight,
        name="instance_sql_insight",
    ),
    path("monitor/alerts/", monitor_alert_views.alert_events, name="alert_events"),
    path("monitor/alerts/rules/", monitor_alert_views.alert_rules, name="alert_rules"),
    path(
        "monitor/alerts/rules/new/",
        monitor_alert_views.alert_rule_create,
        name="alert_rule_create",
    ),
    path(
        "monitor/alerts/rules/<int:rule_id>/edit/",
        monitor_alert_views.alert_rule_edit,
        name="alert_rule_edit",
    ),
    path(
        "monitor/alerts/rules/<int:rule_id>/delete/",
        monitor_alert_views.alert_rule_delete,
        name="alert_rule_delete",
    ),
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
