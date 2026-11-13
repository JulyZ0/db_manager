"""运维界面视图（服务端渲染）。

设计原则：
1. 面向不熟悉命令行的运维人员，所有操作都有按钮与表单，不需要记 URL。
2. 危险操作（删除、手动轮换）走 POST + CSRF，不用 GET 触发副作用。
3. 四个核心算法（探测、加密、轮换、统计）尚未手写，
   调用它们时捕获 NotImplementedError，页面显示"功能待实现"而不是 500。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.contrib import messages
from django.db.models import Count, Max, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from apps.assets.models import Cluster, Department, Instance, PasswordRotationLog
from apps.metrics.models import DailyInstanceStat, SlowRequestLog
from apps.web.forms import ClusterForm, DepartmentForm, InstanceForm
from common import scheduler as scheduler_mod
from common.pagination import DEFAULT_PAGE_SIZE, paginate_queryset

logger = logging.getLogger(__name__)

# 四个待手写算法的提示语，统一措辞便于运维识别
PENDING_HINT = (
    "该功能依赖尚未实现的核心算法（{what}）。"
    "请先在 {file} 中完成实现，再回来使用。"
)


def _pending_message(what: str, file: str) -> str:
    return PENDING_HINT.format(what=what, file=file)


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
    instance_qs = Instance.objects.all()
    total_instances = instance_qs.count()
    active_instances = instance_qs.filter(is_active=True).count()
    online = instance_qs.filter(status=Instance.Status.ONLINE).count()
    offline = instance_qs.filter(status=Instance.Status.OFFLINE).count()
    unknown = instance_qs.filter(status=Instance.Status.UNKNOWN).count()

    last_stat_date = (
        DailyInstanceStat.objects.order_by("-stat_date")
        .values_list("stat_date", flat=True)
        .first()
    )
    last_rotation = PasswordRotationLog.objects.select_related("instance").first()

    recent_instances = (
        Instance.objects.select_related("cluster", "cluster__department")
        .order_by("-id")[:8]
    )
    recent_rotations = (
        PasswordRotationLog.objects.select_related("instance").order_by("-id")[:5]
    )

    context = {
        "active_nav": "dashboard",
        "counts": {
            "departments": Department.objects.count(),
            "clusters": Cluster.objects.count(),
            "instances": total_instances,
            "active_instances": active_instances,
        },
        "status_counts": {"online": online, "offline": offline, "unknown": unknown},
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

    context = {
        "active_nav": "instances",
        "instance": instance,
        "rotations": rotations,
        "rotation_count": PasswordRotationLog.objects.filter(instance=instance).count(),
    }
    return render(request, "web/instance_detail.html", context)


@require_http_methods(["GET", "POST"])
def instance_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = InstanceForm(request.POST)
        if form.is_valid():
            instance: Instance = form.save(commit=False)
            password = form.cleaned_data.get("password") or ""
            if password:
                # 加密属于待手写的核心算法
                from apps.credentials.crypto import encrypt_secret

                try:
                    instance.password_ciphertext = encrypt_secret(password)
                except NotImplementedError:
                    messages.error(
                        request,
                        _pending_message("密码加密", "apps/credentials/crypto.py"),
                    )
                    return render(
                        request,
                        "web/instance_form.html",
                        {
                            "active_nav": "instances",
                            "form": form,
                            "is_edit": False,
                        },
                    )
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

                try:
                    obj.password_ciphertext = encrypt_secret(password)
                except NotImplementedError:
                    messages.error(
                        request,
                        _pending_message("密码加密", "apps/credentials/crypto.py"),
                    )
                    return render(
                        request,
                        "web/instance_form.html",
                        {
                            "active_nav": "instances",
                            "form": form,
                            "is_edit": True,
                            "object": instance,
                        },
                    )
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


@require_POST
def instance_delete(request: HttpRequest, instance_id: int) -> HttpResponse:
    instance = get_object_or_404(Instance, id=instance_id)
    name = instance.name
    instance.delete()
    messages.success(request, f"实例「{name}」已删除（含其轮换历史）")
    return redirect("web:instance_list")


@require_POST
def instance_probe(request: HttpRequest, instance_id: int) -> HttpResponse:
    """页面上的"探测"按钮：探测完回到原页面并提示结果。"""
    instance = get_object_or_404(Instance, id=instance_id)

    from apps.probing.services import probe_tcp

    try:
        result = probe_tcp(instance.host, instance.port)
    except NotImplementedError:
        messages.warning(
            request, _pending_message("TCP 探测", "apps/probing/services.py")
        )
        return redirect("web:instance_detail", instance_id=instance.id)

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
    except NotImplementedError:
        messages.warning(
            request,
            _pending_message("密码轮换", "apps/credentials/services.py"),
        )
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

    try:
        rows = aggregate_for_date(stat_date)
    except NotImplementedError:
        messages.warning(
            request,
            _pending_message("每日统计聚合", "apps/metrics/services.py"),
        )
        return redirect(f"{reverse('web:stats')}?stat_date={stat_date.isoformat()}")

    messages.success(request, f"{stat_date} 的统计已重算，写入 {rows} 行")
    return redirect(f"{reverse('web:stats')}?stat_date={stat_date.isoformat()}")


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
