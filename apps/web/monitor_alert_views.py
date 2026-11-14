"""告警页面：事件中心与规则管理。

与监控页的分工
--------------
监控页回答"现在什么状况"，这里回答"出过什么事、谁在管这些事"。
因此列表默认按**状态 + 最近命中**排序，触发中的永远在最前面：
运维打开事件中心，第一眼要看到的是"现在还在响的"。
"""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.assets.models import Instance
from apps.monitor.models import AlertEvent, AlertRule
from apps.web.decorators import login_required_page
from apps.web.forms import AlertRuleForm
from common.pagination import DEFAULT_PAGE_SIZE, paginate_queryset
from common.permissions import PERM_OPERATE_ASSETS, deny_page_if_no_perm

# 事件中心的状态过滤器：空值 = 全部
STATUS_FILTERS = {
    "firing": AlertEvent.Status.FIRING,
    "resolved": AlertEvent.Status.RESOLVED,
}


def alert_events(request: HttpRequest) -> HttpResponse:
    """事件中心：默认只看触发中，可切到全部/已恢复。

    默认值刻意选"触发中"而不是"全部"：恢复的事件只会越来越多，
    默认全量会让真正需要注意的那几条被淹没。
    """
    status = (request.GET.get("status") or "firing").strip()
    severity = (request.GET.get("severity") or "").strip()
    instance_id = (request.GET.get("instance_id") or "").strip()

    qs = AlertEvent.objects.select_related("instance", "rule")
    if status in STATUS_FILTERS:
        qs = qs.filter(status=STATUS_FILTERS[status])
    if severity in dict(AlertRule.Severity.choices):
        qs = qs.filter(severity=severity)
    if instance_id.isdigit():
        qs = qs.filter(instance_id=int(instance_id))

    # 触发中排前、同状态按最近命中倒序（模型 Meta 已定义 ordering）
    qs = qs.order_by("status", "-last_seen_at", "-id")

    page = paginate_queryset(qs, _page(request), DEFAULT_PAGE_SIZE)
    counts = {
        "firing": AlertEvent.objects.filter(status=AlertEvent.Status.FIRING).count(),
        "resolved": AlertEvent.objects.filter(status=AlertEvent.Status.RESOLVED).count(),
    }

    return render(
        request,
        "web/monitor/alert_events.html",
        {
            "active_nav": "monitor",
            "page_obj": page,
            "events": page["results"],
            "counts": counts,
            "status": status,
            "severity": severity,
            "instance_id": instance_id,
            "instances": Instance.objects.order_by("name")[:200],
            "severity_choices": AlertRule.Severity.choices,
        },
    )


def alert_rules(request: HttpRequest) -> HttpResponse:
    """规则清单（只读可见，便于只读角色了解告警口径）。"""
    rules = AlertRule.objects.select_related("cluster").order_by("-enabled", "name")
    firing_by_rule: dict[int, int] = {}
    for row in (
        AlertEvent.objects.filter(status=AlertEvent.Status.FIRING)
        .values("rule_id")
        .order_by()
    ):
        rule_id = row["rule_id"]
        if rule_id:
            firing_by_rule[rule_id] = firing_by_rule.get(rule_id, 0) + 1

    can_manage = deny_page_if_no_perm(request, PERM_OPERATE_ASSETS) is None
    return render(
        request,
        "web/monitor/alert_rules.html",
        {
            "active_nav": "monitor",
            "rules": rules,
            "firing_by_rule": firing_by_rule,
            "can_manage": can_manage,
        },
    )


@login_required_page
def alert_rule_create(request: HttpRequest) -> HttpResponse:
    return _rule_form(request, instance=None)


@login_required_page
def alert_rule_edit(request: HttpRequest, rule_id: int) -> HttpResponse:
    rule = get_object_or_404(AlertRule, id=rule_id)
    return _rule_form(request, instance=rule)


def _rule_form(request: HttpRequest, instance: AlertRule | None) -> HttpResponse:
    """新建与编辑共用一套流程。

    写操作要权限：只读角色能看到规则，但不能改——告警阈值是运维决策，
    不该被浏览者顺手改掉。
    """
    resp = deny_page_if_no_perm(request, PERM_OPERATE_ASSETS, what="管理告警规则")
    if resp is not None:
        return resp

    if request.method == "POST":
        form = AlertRuleForm(request.POST, instance=instance)
        if form.is_valid():
            rule = form.save()
            messages.success(
                request, f"规则「{rule.name}」已{'更新' if instance else '创建'}"
            )
            return redirect("web:alert_rules")
    else:
        form = AlertRuleForm(instance=instance)

    return render(
        request,
        "web/monitor/alert_rule_form.html",
        {
            "active_nav": "monitor",
            "form": form,
            "is_edit": instance is not None,
            "rule": instance,
        },
    )


@login_required_page
@require_POST
def alert_rule_delete(request: HttpRequest, rule_id: int) -> HttpResponse:
    """删除规则。

    历史事件**不会**跟着消失（AlertEvent.rule 是 SET_NULL），
    因此这里可以放心删：删的是"以后别再按这条规则告警"，
    不是"抹掉过去发生过的事"。
    """
    resp = deny_page_if_no_perm(request, PERM_OPERATE_ASSETS, what="删除告警规则")
    if resp is not None:
        return resp

    rule = get_object_or_404(AlertRule, id=rule_id)
    name = rule.name
    rule.delete()
    messages.success(request, f"规则「{name}」已删除，历史事件仍会保留")
    return redirect("web:alert_rules")


def _page(request: HttpRequest) -> int:
    raw = request.GET.get("page") or "1"
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1
