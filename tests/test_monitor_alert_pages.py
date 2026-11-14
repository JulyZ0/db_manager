"""告警页面与通知通道测试。

重点：
1. 事件中心默认只看「触发中」——恢复事件会越积越多，默认全量会淹没重点；
2. 规则管理是写操作，只读角色能看不能改；
3. webhook 失败不能让评估任务崩掉。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.utils import timezone


@pytest.fixture
def cluster(db):
    from apps.assets.models import Cluster, Department

    dept = Department.objects.create(name="告警页面部")
    return Cluster.objects.create(name="告警页面集群", department=dept, engine="mysql")


@pytest.fixture
def instance(cluster):
    from apps.assets.models import Instance

    return Instance.objects.create(
        name="evt-1", cluster=cluster, host="10.6.0.1", port=3306, username="mon"
    )


def make_rule(**kwargs):
    from apps.monitor.models import AlertRule

    defaults = {
        "name": "连接数过高",
        "metric": "mysql.connections",
        "operator": AlertRule.Operator.GT,
        "threshold": 100.0,
    }
    defaults.update(kwargs)
    return AlertRule.objects.create(**defaults)


def make_event(rule, instance, status="firing", severity="warning", **kwargs):
    from apps.monitor.models import AlertEvent

    now = timezone.now()
    return AlertEvent.objects.create(
        rule=rule,
        rule_name=rule.name,
        instance=instance,
        instance_label=f"{instance.name} ({instance.host}:{instance.port})",
        metric=rule.metric,
        operator=rule.operator,
        threshold=rule.threshold,
        severity=severity,
        status=status,
        trigger_value=150.0,
        latest_value=kwargs.pop("latest_value", 150.0),
        started_at=now,
        last_seen_at=kwargs.pop("last_seen_at", now),
        resolved_at=kwargs.pop("resolved_at", None),
    )


@pytest.mark.django_db
def test_events_page_defaults_to_firing(client, instance):
    rule = make_rule()
    make_event(rule, instance, status="firing")
    make_event(rule, instance, status="resolved", resolved_at=timezone.now())

    resp = client.get("/monitor/alerts/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "触发中" in body
    # 触发中 1 条、已恢复 1 条；表格里只应出现触发中那条
    assert "正在告警" not in body  # 那是规则页的词
    assert body.count("badge-red") >= 1


@pytest.mark.django_db
def test_events_page_can_show_resolved(client, instance):
    rule = make_rule()
    make_event(rule, instance, status="resolved", resolved_at=timezone.now())

    resp = client.get("/monitor/alerts/?status=resolved")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "已恢复" in body


@pytest.mark.django_db
def test_events_page_empty_state(client, db):
    resp = client.get("/monitor/alerts/")
    assert resp.status_code == 200
    assert "当前没有触发中的告警" in resp.content.decode()


@pytest.mark.django_db
def test_events_page_filters_by_severity(client, instance):
    rule = make_rule()
    make_event(rule, instance, severity="critical")
    make_event(rule, instance, severity="warning")

    resp = client.get("/monitor/alerts/?severity=critical")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "严重" in body


@pytest.mark.django_db
def test_events_page_swallows_bad_page_param(client, instance):
    """page=abc 不能让页面 500——运维手改 URL 很常见。"""
    resp = client.get("/monitor/alerts/?page=abc")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_rules_page_lists_rules(client):
    make_rule(name="规则甲")
    make_rule(name="规则乙", metric="mysql.threads_running")

    resp = client.get("/monitor/alerts/rules/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "规则甲" in body
    assert "规则乙" in body


@pytest.mark.django_db
def test_rules_page_shows_firing_count(client, instance):
    rule = make_rule()
    make_event(rule, instance)

    resp = client.get("/monitor/alerts/rules/")
    assert "正在告警 1" in resp.content.decode()


@pytest.mark.django_db
def test_rule_create_requires_login(anon_client):
    resp = anon_client.get("/monitor/alerts/rules/new/")
    assert resp.status_code == 302
    assert "login" in resp["Location"]


@pytest.mark.django_db
def test_rule_create_by_admin(api_client):
    resp = api_client.post(
        "/monitor/alerts/rules/new/",
        {
            "name": "新建规则",
            "metric": "mysql.qps",
            "operator": "gt",
            "threshold": "500",
            "duration_minutes": "5",
            "severity": "critical",
            "cluster": "",
            "cooldown_minutes": "10",
            "enabled": "on",
            "remark": "",
        },
    )

    assert resp.status_code == 302
    from apps.monitor.models import AlertRule

    rule = AlertRule.objects.get(name="新建规则")
    assert rule.threshold == 500.0
    assert rule.duration_minutes == 5


@pytest.mark.django_db
def test_rule_form_rejects_bad_threshold(api_client):
    resp = api_client.post(
        "/monitor/alerts/rules/new/",
        {
            "name": "坏规则",
            "metric": "mysql.qps",
            "operator": "gt",
            "threshold": "abc",
            "duration_minutes": "0",
            "severity": "warning",
            "cooldown_minutes": "10",
        },
    )

    assert resp.status_code == 200  # 回显表单而非跳转
    assert "阈值" in resp.content.decode()
    from apps.monitor.models import AlertRule

    assert not AlertRule.objects.filter(name="坏规则").exists()


@pytest.mark.django_db
def test_rule_edit_keeps_unknown_metric(api_client):
    """旧规则指向已下线的指标时，编辑保存不能把指标悄悄改掉。"""
    rule = make_rule(name="老规则", metric="mysql.legacy_metric")

    resp = api_client.get(f"/monitor/alerts/rules/{rule.pk}/edit/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "mysql.legacy_metric" in body
    assert "已不在指标目录中" in body


@pytest.mark.django_db
def test_rule_delete_keeps_events(api_client, instance):
    rule = make_rule()
    make_event(rule, instance)

    resp = api_client.post(f"/monitor/alerts/rules/{rule.pk}/delete/")

    assert resp.status_code == 302
    from apps.monitor.models import AlertEvent, AlertRule

    assert not AlertRule.objects.exists()
    assert AlertEvent.objects.count() == 1


@pytest.mark.django_db
def test_rule_delete_rejects_get(api_client):
    rule = make_rule()
    resp = api_client.get(f"/monitor/alerts/rules/{rule.pk}/delete/")
    assert resp.status_code == 405


@pytest.mark.django_db
def test_alerts_nav_link_present(client, db):
    resp = client.get("/")
    body = resp.content.decode()
    assert 'href="/monitor/alerts/"' in body


# ---------------------------------------------------------------------------
# 通知通道
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_notify_skipped_without_webhook(instance, settings):
    from apps.monitor.notify import notify_event

    settings.MONITOR_ALERT_WEBHOOK_URL = ""
    rule = make_rule()
    event = make_event(rule, instance)

    # 未配置时视为成功且不发请求
    assert notify_event(event, "fired") is True


@pytest.mark.django_db
def test_notify_posts_payload(instance, settings, monkeypatch):
    from apps.monitor import notify

    settings.MONITOR_ALERT_WEBHOOK_URL = "https://example.invalid/hook"
    rule = make_rule()
    event = make_event(rule, instance, severity="critical")

    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    assert notify.notify_event(event, "fired") is True
    assert captured["url"] == "https://example.invalid/hook"
    # 同时带企微/钉钉与飞书的字段，一份配置打通三家
    assert "msgtype" in captured["body"]
    assert "msg_type" in captured["body"]
    assert "evt-1" in captured["body"]["text"]["content"]


@pytest.mark.django_db
def test_notify_failure_does_not_raise(instance, settings, monkeypatch):
    from apps.monitor import notify

    settings.MONITOR_ALERT_WEBHOOK_URL = "https://example.invalid/hook"
    rule = make_rule()
    event = make_event(rule, instance)

    def boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)

    # 通知失败不能抛：否则评估任务会重试并把同一批事件再评估一遍
    assert notify.notify_event(event, "fired") is False


@pytest.mark.django_db
def test_alert_evaluation_notifies_on_fire_and_resolve(instance, monkeypatch, settings):
    from apps.monitor import alerting
    from apps.monitor.models import MetricSample

    sent = []
    monkeypatch.setattr(
        alerting, "notify_event", lambda event, kind: sent.append(kind) or True
    )
    rule = make_rule()
    MetricSample.objects.create(
        instance=instance,
        metric="mysql.connections",
        value=150.0,
        collected_at=timezone.now(),
    )

    alerting.evaluate_rule(rule)
    assert sent == ["fired"]

    MetricSample.objects.create(
        instance=instance,
        metric="mysql.connections",
        value=10.0,
        collected_at=timezone.now() + timedelta(seconds=1),
    )
    alerting.evaluate_rule(rule)

    # 恢复也要通知，否则运维不知道可以收工了
    assert sent == ["fired", "resolved"]


@pytest.mark.django_db
def test_alert_evaluation_does_not_renotify_repeat(instance, monkeypatch):
    from apps.monitor import alerting
    from apps.monitor.models import MetricSample

    sent = []
    monkeypatch.setattr(
        alerting, "notify_event", lambda event, kind: sent.append(kind) or True
    )
    rule = make_rule()
    MetricSample.objects.create(
        instance=instance,
        metric="mysql.connections",
        value=150.0,
        collected_at=timezone.now(),
    )
    alerting.evaluate_rule(rule)
    alerting.evaluate_rule(rule)

    # 重复命中不重复推送，否则每分钟一条把人吵到关掉告警
    assert sent == ["fired"]
