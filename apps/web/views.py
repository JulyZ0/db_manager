"""运维界面视图（服务端渲染）。

设计原则：
1. 面向不熟悉命令行的运维人员，所有操作都有按钮与表单，不需要记 URL。
2. 危险操作（删除、手动轮换）走 POST + CSRF，不用 GET 触发副作用。
3. 核心算法（探测、加密、轮换、统计）在 apps/*/services.py 与 crypto.py 中实现，
   页面只负责调用与结果提示，不承担业务逻辑。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.contrib import messages
from django.db.models import Count, Max, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.assets.models import Cluster, Department, Instance, PasswordRotationLog
from apps.metrics.models import DailyInstanceStat, SlowRequestLog
from apps.web.decorators import login_required_page
from apps.web.forms import ClusterForm, DepartmentForm, InstanceForm
from common import scheduler as scheduler_mod
from common.pagination import DEFAULT_PAGE_SIZE, paginate_queryset

logger = logging.getLogger(__name__)


def _page(request: HttpRequest) -> int:
    try:
        return max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError):
        return 1


def _base_query(request: HttpRequest) -> str:
    """把当前筛选条件拼成查询串（去掉 page），供分页链接复用。

    没有这一步，翻页会把筛选条件丢掉，运维点第二页就变回全量列表。
    """
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


# ---------------------------------------------------------------------------
# 总览
# ---------------------------------------------------------------------------


def dashboard(request: HttpRequest) -> HttpResponse:
    """一屏看清家底：资产数量、连通状态、最近轮换、最近统计。"""
    # 5 个计数合并成 1 条带条件聚合的 SQL：
    # 原来每项各发一次 COUNT(*)，页面上的数字彼此还可能因并发写入而对不上。
    counts_row = Instance.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
        online=Count("id", filter=Q(status=Instance.Status.ONLINE)),
        offline=Count("id", filter=Q(status=Instance.Status.OFFLINE)),
        unknown=Count("id", filter=Q(status=Instance.Status.UNKNOWN)),
    )
    total_instances = counts_row["total"]

    last_stat_date = (
        DailyInstanceStat.objects.order_by("-stat_date")
        .values_list("stat_date", flat=True)
        .first()
    )

    recent_instances = (
        Instance.objects.select_related("cluster", "cluster__department")
        .order_by("-id")[:8]
    )
    # 取最近 5 条后用第一条当"最近一次轮换"，省掉一次重复查询
    recent_rotations = list(
        PasswordRotationLog.objects.select_related("instance").order_by("-id")[:5]
    )
    last_rotation = recent_rotations[0] if recent_rotations else None

    context = {
        "active_nav": "dashboard",
        "counts": {
            "departments": Department.objects.count(),
            "clusters": Cluster.objects.count(),
            "instances": total_instances,
            "active_instances": counts_row["active"],
        },
        "status_counts": {
            "online": counts_row["online"],
            "offline": counts_row["offline"],
            "unknown": counts_row["unknown"],
        },
        "scheduler_mode": scheduler_mod.current_mode(),
        "last_stat_date": last_stat_date,
        "last_rotation": last_rotation,
        "recent_instances": recent_instances,
        "recent_rotations": recent_rotations,
        "now": timezone.now(),
    }
    return render(request, "web/dashboard.html", context)


# ---------------------------------------------------------------------------
# 部门
# ---------------------------------------------------------------------------


def department_list(request: HttpRequest) -> HttpResponse:
    qs = Department.objects.annotate(cluster_count=Count("clusters"))

    search = (request.GET.get("search") or "").strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search))

    qs = qs.order_by("id")
    page_data = paginate_queryset(qs, _page(request), DEFAULT_PAGE_SIZE)

    context = {
        "active_nav": "departments",
        "departments": page_data["results"],
        "count": page_data["count"],
        "page": page_data["page"],
        "page_size": page_data["page_size"],
        "search": search,
        "base_query": _base_query(request),
        "has_next": page_data["page"] * page_data["page_size"] < page_data["count"],
        "has_prev": page_data["page"] > 1,
    }
    return render(request, "web/department_list.html", context)


@login_required_page
@require_http_methods(["GET", "POST"])
def department_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = DepartmentForm(request.POST)
        if form.is_valid():
            dept = form.save()
            messages.success(request, f"部门「{dept.name}」已创建")
            return redirect("web:department_list")
        messages.error(request, "表单校验未通过，请检查标红项")
    else:
        form = DepartmentForm()

    return render(
        request,
        "web/department_form.html",
        {"active_nav": "departments", "form": form, "is_edit": False},
    )


@login_required_page
@require_http_methods(["GET", "POST"])
def department_edit(request: HttpRequest, department_id: int) -> HttpResponse:
    dept = get_object_or_404(Department, id=department_id)
    if request.method == "POST":
        form = DepartmentForm(request.POST, instance=dept)
        if form.is_valid():
            form.save()
            messages.success(request, f"部门「{dept.name}」已更新")
            return redirect("web:department_list")
        messages.error(request, "表单校验未通过，请检查标红项")
    else:
        form = DepartmentForm(instance=dept)

    return render(
        request,
        "web/department_form.html",
        {"active_nav": "departments", "form": form, "is_edit": True, "object": dept},
    )


@login_required_page
@require_POST
def department_delete(request: HttpRequest, department_id: int) -> HttpResponse:
    dept = get_object_or_404(Department, id=department_id)
    name = dept.name
    cluster_count = dept.clusters.count()
    if cluster_count:
        messages.error(
            request, f"部门「{name}」下仍有 {cluster_count} 个集群，无法删除"
        )
        return redirect("web:department_list")
    dept.delete()
    messages.success(request, f"部门「{name}」已删除")
    return redirect("web:department_list")


# ---------------------------------------------------------------------------
# 集群
# ---------------------------------------------------------------------------


def cluster_list(request: HttpRequest) -> HttpResponse:
    qs = Cluster.objects.select_related("department").annotate(
        instance_count=Count("instances")
    )

    search = (request.GET.get("search") or "").strip()
    department_id = request.GET.get("department_id") or ""
    env = request.GET.get("env") or ""

    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(department__name__icontains=search))
    if department_id.isdigit():
        qs = qs.filter(department_id=int(department_id))
    if env:
        qs = qs.filter(env=env)

    qs = qs.order_by("department_id", "id")
    page_data = paginate_queryset(qs, _page(request), DEFAULT_PAGE_SIZE)

    context = {
        "active_nav": "clusters",
        "clusters": page_data["results"],
        "count": page_data["count"],
        "page": page_data["page"],
        "page_size": page_data["page_size"],
        "search": search,
        "department_id": department_id,
        "env": env,
        "departments": Department.objects.order_by("name"),
        "env_choices": Cluster.Env.choices,
        "base_query": _base_query(request),
        "has_next": page_data["page"] * page_data["page_size"] < page_data["count"],
        "has_prev": page_data["page"] > 1,
    }
    return render(request, "web/cluster_list.html", context)


@login_required_page
@require_http_methods(["GET", "POST"])
def cluster_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = ClusterForm(request.POST)
        if form.is_valid():
            cluster = form.save()
            messages.success(request, f"集群「{cluster.name}」已创建")
            return redirect("web:cluster_list")
        messages.error(request, "表单校验未通过，请检查标红项")
    else:
        initial = {}
        dept_id = request.GET.get("department_id")
        if dept_id and dept_id.isdigit():
            initial["department"] = dept_id
        form = ClusterForm(initial=initial)

    return render(
        request,
        "web/cluster_form.html",
        {"active_nav": "clusters", "form": form, "is_edit": False},
    )


@login_required_page
@require_http_methods(["GET", "POST"])
def cluster_edit(request: HttpRequest, cluster_id: int) -> HttpResponse:
    cluster = get_object_or_404(Cluster, id=cluster_id)
    if request.method == "POST":
        form = ClusterForm(request.POST, instance=cluster)
        if form.is_valid():
            form.save()
            messages.success(request, f"集群「{cluster.name}」已更新")
            return redirect("web:cluster_list")
        messages.error(request, "表单校验未通过，请检查标红项")
    else:
        form = ClusterForm(instance=cluster)

    return render(
        request,
        "web/cluster_form.html",
        {"active_nav": "clusters", "form": form, "is_edit": True, "object": cluster},
    )


@login_required_page
@require_POST
def cluster_delete(request: HttpRequest, cluster_id: int) -> HttpResponse:
    cluster = get_object_or_404(Cluster, id=cluster_id)
    name = cluster.name
    instance_count = cluster.instances.count()
    if instance_count:
        messages.error(
            request, f"集群「{name}」下仍有 {instance_count} 个实例，无法删除"
        )
        return redirect("web:cluster_list")
    cluster.delete()
    messages.success(request, f"集群「{name}」已删除")
    return redirect("web:cluster_list")


# ---------------------------------------------------------------------------
# 实例
# ---------------------------------------------------------------------------


def instance_list(request: HttpRequest) -> HttpResponse:
    qs = Instance.objects.select_related("cluster", "cluster__department")

    search = (request.GET.get("search") or "").strip()
    cluster_id = request.GET.get("cluster_id") or ""
    department_id = request.GET.get("department_id") or ""
    status = request.GET.get("status") or ""
    role = request.GET.get("role") or ""

    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(host__icontains=search))
    if cluster_id.isdigit():
        qs = qs.filter(cluster_id=int(cluster_id))
    if department_id.isdigit():
        qs = qs.filter(cluster__department_id=int(department_id))
    if status:
        qs = qs.filter(status=status)
    if role:
        qs = qs.filter(role=role)

    qs = qs.order_by("cluster_id", "id")
    page_data = paginate_queryset(qs, _page(request), DEFAULT_PAGE_SIZE)

    context = {
        "active_nav": "instances",
        "instances": page_data["results"],
        "count": page_data["count"],
        "page": page_data["page"],
        "page_size": page_data["page_size"],
        "search": search,
        "cluster_id": cluster_id,
        "department_id": department_id,
        "status": status,
        "role": role,
        "departments": Department.objects.order_by("name"),
        "clusters": Cluster.objects.select_related("department").order_by(
            "department__name", "name"
        ),
        "status_choices": Instance.Status.choices,
        "role_choices": Instance.Role.choices,
        "base_query": _base_query(request),
        "has_next": page_data["page"] * page_data["page_size"] < page_data["count"],
        "has_prev": page_data["page"] > 1,
    }
    return render(request, "web/instance_list.html", context)


def instance_detail(request: HttpRequest, instance_id: int) -> HttpResponse:
    instance = get_object_or_404(
        Instance.objects.select_related("cluster", "cluster__department"),
        id=instance_id,
    )
    rotations = PasswordRotationLog.objects.filter(instance=instance).order_by("-id")[:20]

    # 详情页上"会不会被自动轮换"这句说明必须跟当前策略一致，
    # 否则运维看到的是写死的旧周期，与实际的改密节奏对不上。
    from apps.credentials import schedule as credential_schedule
    from apps.credentials.models import RotationPolicy

    policy = RotationPolicy.load()

    context = {
        "active_nav": "instances",
        "instance": instance,
        "rotations": rotations,
        "rotation_count": PasswordRotationLog.objects.filter(instance=instance).count(),
        "rotation_policy": policy,
        "rotation_schedule_text": credential_schedule.describe_schedule(policy),
        "in_rotation_scope": instance.id in set(policy.resolve_instance_ids()),
    }
    return render(request, "web/instance_detail.html", context)


def instance_verify_credentials(request: HttpRequest, instance_id: int) -> JsonResponse:
    """用登记的账号密码连一次目标库，供详情页打开后异步调用。

    为什么是 GET 且免登录
    ---------------------
    这是**只读**操作：不执行任何语句、不写任何字段，结论只回给页面看一眼。
    "端口通不通"回答不了"密码对不对"——运维真正关心的往往后者，
    所以详情页一打开就自动验一次，不需要额外点按钮。

    为什么不落库
    ------------
    验证结果是瞬时状态，5 秒后重验可能就是另一个结论。把它固化进 Instance
    字段会让"上次验证通过"变成误导性的陈旧数据，因此只回显、不存储。

    响应永远是 200：连不上是有效业务结论，不是服务端错误。
    """
    instance = get_object_or_404(
        Instance.objects.select_related("cluster"), id=instance_id
    )

    from apps.dbconsole.services import verify_credentials

    result = verify_credentials(instance)

    return JsonResponse(
        {
            "instance_id": instance.id,
            "engine": result.engine,
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "server_version": result.server_version,
            "error": result.error,
            "hint": result.hint,
        }
    )


@login_required_page
@require_http_methods(["GET", "POST"])
def instance_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = InstanceForm(request.POST)
        if form.is_valid():
            instance: Instance = form.save(commit=False)
            password = form.cleaned_data.get("password") or ""
            if password:
                from apps.credentials.crypto import encrypt_secret

                instance.password_ciphertext = encrypt_secret(password)
                instance.password_version = 1
                instance.password_updated_at = timezone.now()
            instance.save()
            messages.success(request, f"实例「{instance.name}」已创建")
            return redirect("web:instance_detail", instance_id=instance.id)
        messages.error(request, "表单校验未通过，请检查标红项")
    else:
        initial = {}
        cluster_id = request.GET.get("cluster_id")
        if cluster_id and cluster_id.isdigit():
            initial["cluster"] = cluster_id
        form = InstanceForm(initial=initial)

    return render(
        request,
        "web/instance_form.html",
        {"active_nav": "instances", "form": form, "is_edit": False},
    )


@login_required_page
@require_http_methods(["GET", "POST"])
def instance_edit(request: HttpRequest, instance_id: int) -> HttpResponse:
    instance = get_object_or_404(Instance, id=instance_id)
    if request.method == "POST":
        form = InstanceForm(request.POST, instance=instance)
        if form.is_valid():
            obj: Instance = form.save(commit=False)
            password = form.cleaned_data.get("password") or ""
            if password:
                from apps.credentials.crypto import encrypt_secret

                obj.password_ciphertext = encrypt_secret(password)
                obj.password_version += 1
                obj.password_updated_at = timezone.now()
            obj.save()
            messages.success(request, f"实例「{obj.name}」已更新")
            return redirect("web:instance_detail", instance_id=obj.id)
        messages.error(request, "表单校验未通过，请检查标红项")
    else:
        form = InstanceForm(instance=instance)

    return render(
        request,
        "web/instance_form.html",
        {"active_nav": "instances", "form": form, "is_edit": True, "object": instance},
    )


@login_required_page
@require_POST
def instance_delete(request: HttpRequest, instance_id: int) -> HttpResponse:
    instance = get_object_or_404(Instance, id=instance_id)
    name = instance.name
    instance.delete()
    messages.success(request, f"实例「{name}」已删除（含其轮换历史）")
    return redirect("web:instance_list")


@login_required_page
@require_POST
def instance_probe(request: HttpRequest, instance_id: int) -> HttpResponse:
    """页面上的"探测"按钮：探测完回到原页面并提示结果。"""
    instance = get_object_or_404(Instance, id=instance_id)

    from apps.probing.services import probe_tcp

    result = probe_tcp(instance.host, instance.port)

    probed_at = timezone.now()
    instance.last_probed_at = probed_at
    instance.last_probe_ok = result.reachable
    instance.last_probe_latency_ms = result.latency_ms
    instance.status = (
        Instance.Status.ONLINE if result.reachable else Instance.Status.OFFLINE
    )
    instance.save(
        update_fields=[
            "last_probed_at",
            "last_probe_ok",
            "last_probe_latency_ms",
            "status",
            "updated_at",
        ]
    )

    if result.reachable:
        messages.success(
            request,
            f"探测成功：{instance.host}:{instance.port} 可达，耗时 {result.latency_ms}ms",
        )
    else:
        messages.error(
            request,
            f"探测失败：{instance.host}:{instance.port} 不可达（{result.error}）",
        )
    return redirect("web:instance_detail", instance_id=instance.id)


@login_required_page
@require_POST
def instance_rotate(request: HttpRequest, instance_id: int) -> HttpResponse:
    """页面上的"立即轮换密码"按钮。"""
    instance = get_object_or_404(Instance, id=instance_id)
    if not instance.is_active:
        messages.error(request, "实例已停用，不能轮换密码")
        return redirect("web:instance_detail", instance_id=instance.id)

    from apps.credentials.services import rotate_one

    try:
        log = rotate_one(instance, source="manual")
    except Exception as exc:  # noqa: BLE001 - 面向运维的页面，失败要给可读提示而非 500
        # 失败历史已由 rotate_one 写入审计表（含异常类型），这里只提示运维
        messages.error(request, f"轮换失败：{exc}")
        return redirect("web:instance_detail", instance_id=instance.id)

    if log.status == PasswordRotationLog.Status.SUCCESS:
        messages.success(
            request,
            f"密码已轮换，新版本 v{instance.password_version}，"
            f"指纹 {log.new_fingerprint}",
        )
    else:
        messages.error(request, f"轮换失败：{log.error_message or '未知原因'}")
    return redirect("web:instance_detail", instance_id=instance.id)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def stats(request: HttpRequest) -> HttpResponse:
    """按日期查看统计快照，并支持一键重算。"""
    raw_date = request.GET.get("stat_date") or ""
    if raw_date:
        try:
            stat_date = date.fromisoformat(raw_date)
        except ValueError:
            messages.error(request, f"日期格式非法：{raw_date}")
            stat_date = timezone.localdate()
    else:
        stat_date = timezone.localdate()

    rows = DailyInstanceStat.objects.filter(stat_date=stat_date).order_by(
        "department_id", "cluster_id"
    )
    rows = list(rows)

    # 最近 14 天有哪些快照，方便点选
    recent_dates = list(
        DailyInstanceStat.objects.values_list("stat_date", flat=True)
        .distinct()
        .order_by("-stat_date")[:14]
    )

    context = {
        "active_nav": "stats",
        "stat_date": stat_date,
        "rows": rows,
        "total_instances": sum(r.instance_count for r in rows),
        "total_active": sum(r.active_count for r in rows),
        "recent_dates": recent_dates,
        "today": timezone.localdate(),
        "yesterday": timezone.localdate() - timedelta(days=1),
    }
    return render(request, "web/stats.html", context)


@login_required_page
@require_POST
def stats_rebuild(request: HttpRequest) -> HttpResponse:
    """重算指定日期（缺省今天）的统计快照。"""
    raw_date = request.POST.get("stat_date") or ""
    try:
        stat_date = date.fromisoformat(raw_date) if raw_date else timezone.localdate()
    except ValueError:
        messages.error(request, f"日期格式非法：{raw_date}")
        return redirect("web:stats")

    from apps.metrics.services import aggregate_for_date

    rows = aggregate_for_date(stat_date)

    messages.success(request, f"{stat_date} 的统计已重算，写入 {rows} 行")
    return redirect(f"{reverse('web:stats')}?stat_date={stat_date.isoformat()}")


# ---------------------------------------------------------------------------
# 定时轮换策略
# ---------------------------------------------------------------------------


@login_required_page
@require_http_methods(["GET", "POST"])
def rotation_policy(request: HttpRequest) -> HttpResponse:
    """定时轮换策略的查看与编辑。

    整个系统只有一份策略（见 RotationPolicy 的单例设计），所以这里没有
    列表页，进来就是"编辑当前策略"。

    预览（下次执行时间）只反映**已保存**的策略：表单里改了但没提交的
    时间不该显示成"即将生效"，否则运维会对着一个并不存在的计划等待。
    """
    from apps.credentials import schedule as credential_schedule
    from apps.credentials.models import RotationPolicy
    from apps.web.forms import RotationPolicyForm

    policy = RotationPolicy.load()

    if request.method == "POST":
        form = RotationPolicyForm(request.POST, instance=policy)
        if form.is_valid():
            form.save()
            messages.success(request, "轮换策略已保存，下一次执行时间已按新配置重算")
            return redirect("web:rotation_policy")
        messages.error(request, "保存失败，请按提示修正后重试")
    else:
        form = RotationPolicyForm(instance=policy)

    context = {
        "active_nav": "rotation",
        "form": form,
        "policy": policy,
        "schedule_text": credential_schedule.describe_schedule(policy),
        "next_run_text": credential_schedule.describe_next_run(policy),
        "scope_label": policy.scope_label(),
        "scope_count": len(policy.resolve_instance_ids()),
        "mode_interval": "interval",
        "mode_daily": "daily",
    }
    return render(request, "web/rotation_policy.html", context)


@login_required_page
@require_POST
def rotation_run_now(request: HttpRequest) -> HttpResponse:
    """立即按当前策略执行一次轮换，不看时间。

    与定时执行共用 ``schedule.run_now``：范围、密码长度、结果摘要完全一致，
    只是跳过"到点没有"的判定。这样运维手动跑一次不会产出不同规格的密码。
    """
    from apps.credentials import schedule as credential_schedule

    try:
        result = credential_schedule.run_now()
    except Exception as exc:  # noqa: BLE001 - 面向运维的页面要给可读提示
        logger.exception("手动执行轮换策略失败")
        messages.error(request, f"执行失败：{exc}")
        return redirect("web:rotation_policy")

    if result.get("ran"):
        messages.success(
            request,
            f"已按策略执行：成功 {result.get('success', 0)}，"
            f"失败 {result.get('failed', 0)}，共 {result.get('total', 0)}",
        )
    else:
        messages.warning(request, f"本次没有轮换任何实例：{result.get('reason')}")
    return redirect("web:rotation_policy")


# ---------------------------------------------------------------------------
# 慢请求
# ---------------------------------------------------------------------------


def slow_requests(request: HttpRequest) -> HttpResponse:
    qs = SlowRequestLog.objects.all().order_by("-id")
    page_data = paginate_queryset(qs, _page(request), DEFAULT_PAGE_SIZE)

    context = {
        "active_nav": "slow",
        "logs": page_data["results"],
        "count": page_data["count"],
        "page": page_data["page"],
        "page_size": page_data["page_size"],
        "base_query": _base_query(request),
        "has_next": page_data["page"] * page_data["page_size"] < page_data["count"],
        "has_prev": page_data["page"] > 1,
    }
    return render(request, "web/slow_requests.html", context)


# ---------------------------------------------------------------------------
# 终端风格控制台
# ---------------------------------------------------------------------------


def console(request: HttpRequest) -> HttpResponse:
    """终端风格控制台页面。

    设计取向：面向习惯命令行的运维。左侧导航与其它页面一致，主体是黑底
    等宽的「终端」，输入命令即可切换实例、验证凭据、执行语句。

    页面本身不需要登录（只是渲染一个空壳）；真正危险的是执行语句，
    由 ``console_exec`` 单独守卫。
    """
    context = {
        "active_nav": "console",
        "instances": (
            Instance.objects.select_related("cluster", "cluster__department")
            .order_by("cluster__department__name", "cluster__name", "name")
        ),
    }
    return render(request, "web/console.html", context)


@login_required_page
@require_POST
def console_exec(request: HttpRequest, instance_id: int) -> JsonResponse:
    """控制台执行一条语句。要求登录，且需 CSRF（django 中间件默认校验）。"""
    from apps.dbconsole.services import (
        ERROR_MULTI_STATEMENT,
        check_statement,
        classify_statement,
        engine_of,
        execute_statement,
        is_destructive,
    )

    instance = get_object_or_404(
        Instance.objects.select_related("cluster"), id=instance_id
    )

    sql = (request.POST.get("sql") or "").strip()
    if not sql:
        return JsonResponse({"ok": False, "error": "empty", "hint": "语句为空"})

    if check_statement(sql, engine_of(instance)) == ERROR_MULTI_STATEMENT:
        return JsonResponse(
            {
                "ok": False,
                "error": ERROR_MULTI_STATEMENT,
                "hint": "一次只能执行一条语句，请分开提交",
            }
        )

    if is_destructive(sql) and request.POST.get("confirm") != "1":
        return JsonResponse(
            {
                "ok": False,
                "error": "confirm_required",
                "hint": "该语句不可逆，请加 --force 或确认后重试",
            }
        )

    user_label = request.user.get_username() if request.user.is_authenticated else ""
    result = execute_statement(instance, sql, user_label=user_label)

    return JsonResponse(
        {
            "ok": result.ok,
            "kind": classify_statement(sql),
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "affected": result.affected,
            "duration_ms": result.duration_ms,
            "truncated": result.truncated,
            "error": result.error,
            "hint": result.hint,
        }
    )


def console_instances(request: HttpRequest) -> JsonResponse:
    """控制台 ``list`` 命令的数据源：只返回展示需要的字段。

    复用 ``resolver.instance_summary``，保证 ``list`` 与 ``sw`` 的字段口径一致
    （尤其是 ``alias`` / ``cluster_alias``，用户要照着它拼 ``sw`` 的目标）。
    """
    from apps.assets import resolver

    rows = [
        resolver.instance_summary(item)
        for item in Instance.objects.select_related(
            "cluster", "cluster__department"
        ).order_by("cluster__department__name", "cluster__name", "name")
    ]
    return JsonResponse({"count": len(rows), "results": rows})


def console_switch(request: HttpRequest) -> JsonResponse:
    """控制台 ``sw`` 命令：把用户输入解析成一个实例。

    解析规则全部放在 ``apps.assets.resolver``，这里只做 JSON 包装：
    成功回一个实例摘要，失败回错误码 + 说明 +（可选）候选列表。

    用 query 参数而不是路径参数：``sw`` 的输入可能含 ``.`` ``@`` ``:``，
    这些字符在路径里需要转义，放 query 更省事也更不容易出错。

    只读，不写任何字段，因此与其它控制台辅助端点一样允许匿名访问。
    """
    from apps.assets import resolver

    target = (request.GET.get("target") or "").strip()
    result = resolver.resolve_target(target)

    if result.ok:
        return JsonResponse(
            {"ok": True, "instance": resolver.instance_summary(result.instance)}
        )

    return JsonResponse(
        {
            "ok": False,
            "error": result.error,
            "message": result.message,
            # 候选只回前 10 个：终端里再多也看不过来
            "candidates": [
                resolver.instance_summary(item) for item in result.candidates[:10]
            ],
            "candidate_count": len(result.candidates),
        }
    )


def console_verify(request: HttpRequest, instance_id: int) -> JsonResponse:
    """控制台 ``verify`` 命令：连一次目标库确认凭据可用。"""
    from apps.dbconsole.services import verify_credentials

    instance = get_object_or_404(
        Instance.objects.select_related("cluster"), id=instance_id
    )
    result = verify_credentials(instance)
    return JsonResponse(
        {
            "ok": result.ok,
            "engine": result.engine,
            "latency_ms": result.latency_ms,
            "server_version": result.server_version,
            "error": result.error,
            "hint": result.hint,
        }
    )
