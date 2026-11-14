"""监控大盘与实例画像页面测试。

大盘的价值在于「只列出有问题的实例」，因此测试重点不是"页面能开"，
而是**该出现的出现、不该出现的不出现**：正常实例不能占屏幕。
"""

from __future__ import annotations

import pytest
from django.utils import timezone


@pytest.fixture
def cluster(db):
    from apps.assets.models import Cluster, Department

    dept = Department.objects.create(name="大盘测试部")
    return Cluster.objects.create(
        name="大盘集群", department=dept, engine="mysql", env="prod"
    )


@pytest.fixture
def instance(cluster):
    from apps.assets.models import Instance

    return Instance.objects.create(
        name="dash-1", cluster=cluster, host="10.4.0.1", port=3306, username="mon"
    )


@pytest.mark.django_db
def test_dashboard_renders_counts(client, instance):
    from apps.monitor.models import CollectionRun

    CollectionRun.objects.create(
        instance=instance, collected_at=timezone.now(), ok=True, metrics_count=5
    )

    resp = client.get("/monitor/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "监控大盘" in body
    # 有一个实例采集正常
    assert "采集正常" in body


@pytest.mark.django_db
def test_dashboard_lists_failing_instance(client, instance):
    from apps.monitor.models import CollectionRun

    CollectionRun.objects.create(
        instance=instance,
        collected_at=timezone.now(),
        ok=False,
        error_code="auth_failed",
        message="账号或密码不正确",
    )

    resp = client.get("/monitor/")
    body = resp.content.decode()

    assert "需要关注的实例（1）" in body
    assert "auth_failed" in body
    assert "账号或密码不正确" in body


@pytest.mark.django_db
def test_dashboard_hides_healthy_instances(client, instance):
    """采集正常的实例不该出现在"需要关注"里。"""
    from apps.monitor.models import CollectionRun

    CollectionRun.objects.create(
        instance=instance, collected_at=timezone.now(), ok=True, metrics_count=5
    )

    resp = client.get("/monitor/")
    body = resp.content.decode()

    assert "需要关注的实例（0）" in body
    assert "全部实例采集正常" in body


@pytest.mark.django_db
def test_dashboard_reports_disabled_collection(client, instance, settings):
    settings.MONITOR_COLLECT_ENABLED = False

    resp = client.get("/monitor/")
    body = resp.content.decode()

    assert resp.status_code == 200
    # 采集关闭必须显式告知，而不是让页面看起来"一切正常但没有数据"
    assert "采集已关闭" in body


@pytest.mark.django_db
def test_dashboard_with_no_instances(client, db):
    resp = client.get("/monitor/")
    assert resp.status_code == 200
    assert "还没有启用的实例" in resp.content.decode()


@pytest.mark.django_db
def test_dashboard_excludes_inactive_instances(client, instance):
    instance.is_active = False
    instance.save(update_fields=["is_active"])

    resp = client.get("/monitor/")
    assert "还没有启用的实例" in resp.content.decode()


@pytest.mark.django_db
def test_profile_page_renders_properties(client, instance):
    from apps.monitor.models import MetricSample

    MetricSample.objects.create(
        instance=instance,
        metric="mysql.connections",
        value=23.0,
        collected_at=timezone.now(),
    )

    resp = client.get(f"/instances/{instance.pk}/monitor/profile/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "实例画像" in body
    assert "10.4.0.1:3306" in body
    assert "23" in body
    # 未登记密码时要明确标出，避免运维误以为采集失败是别的原因
    assert "未登记密码" in body


@pytest.mark.django_db
def test_profile_page_shows_collect_failure(client, instance):
    from apps.monitor.models import CollectionRun

    CollectionRun.objects.create(
        instance=instance,
        collected_at=timezone.now(),
        ok=False,
        error_code="timeout",
        message="连接超时",
    )

    resp = client.get(f"/instances/{instance.pk}/monitor/profile/")
    body = resp.content.decode()

    assert "最近一次采集失败" in body
    assert "timeout" in body


@pytest.mark.django_db
def test_profile_page_lists_siblings(client, instance, cluster):
    from apps.assets.models import Instance

    Instance.objects.create(
        name="dash-2", cluster=cluster, host="10.4.0.2", port=3306, username="mon"
    )

    resp = client.get(f"/instances/{instance.pk}/monitor/profile/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "dash-2" in body


@pytest.mark.django_db
def test_profile_404_for_missing_instance(client):
    resp = client.get("/instances/999999/monitor/profile/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_dashboard_nav_link_present(client, db):
    """大盘要有导航入口，否则运维根本找不到这个页面。"""
    resp = client.get("/")
    body = resp.content.decode()

    assert 'href="/monitor/"' in body
    assert "监控大盘" in body
