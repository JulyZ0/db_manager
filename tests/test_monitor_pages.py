"""监控页面测试（客户端可渲染 + 数据正确上屏）。

这类页面最大的风险不是逻辑错，而是**模板渲染时才炸**：
拼错变量名、用了不存在的样式类或 URL name，Django 不会在
import 阶段报错，只有真人点进去才发现。因此这里直接请求页面。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone


@pytest.fixture
def cluster(db):
    from apps.assets.models import Cluster, Department

    dept = Department.objects.create(name="监控页面部")
    return Cluster.objects.create(
        name="页面集群", department=dept, engine="mysql", env="prod"
    )


@pytest.fixture
def instance(cluster):
    from apps.assets.models import Instance

    return Instance.objects.create(
        name="page-1", cluster=cluster, host="10.2.0.1", port=3306, username="mon"
    )


@pytest.mark.django_db
def test_cluster_overview_page_renders(client, cluster, instance):
    resp = client.get(f"/clusters/{cluster.pk}/monitor/")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "采集健康度" in body
    assert "page-1" in body
    # 没有采集记录时给出"尚无采集记录"，而不是空白卡片
    assert "尚无采集记录" in body


@pytest.mark.django_db
def test_cluster_overview_shows_metric_values(client, cluster, instance):
    from apps.monitor.models import CollectionRun, MetricSample

    now = timezone.now()
    MetricSample.objects.create(
        instance=instance, metric="mysql.connections", value=42.0, collected_at=now
    )
    CollectionRun.objects.create(
        instance=instance, collected_at=now, ok=True, metrics_count=1
    )

    resp = client.get(f"/clusters/{cluster.pk}/monitor/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "42" in body
    # 成功采集不应出现红色失败提示
    assert "最近一次采集失败" not in body


@pytest.mark.django_db
def test_cluster_overview_shows_collect_error(client, cluster, instance):
    from apps.monitor.models import CollectionRun

    CollectionRun.objects.create(
        instance=instance,
        collected_at=timezone.now(),
        ok=False,
        error_code="auth_failed",
        message="账号或密码不正确",
    )

    resp = client.get(f"/clusters/{cluster.pk}/monitor/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "auth_failed" in body
    assert "账号或密码不正确" in body


@pytest.mark.django_db
def test_cluster_overview_without_instances(client, cluster):
    resp = client.get(f"/clusters/{cluster.pk}/monitor/")
    assert resp.status_code == 200
    assert "还没有实例" in resp.content.decode()


@pytest.mark.django_db
def test_cluster_overview_unsupported_engine(client, db):
    """mongodb 没有采集器：页面照常打开，给出说明而不是 500。"""
    from apps.assets.models import Cluster, Department

    dept = Department.objects.create(name="其他引擎部")
    cluster = Cluster.objects.create(
        name="mongo集群", department=dept, engine="mongodb"
    )

    resp = client.get(f"/clusters/{cluster.pk}/monitor/")

    assert resp.status_code == 200
    assert "暂不支持指标采集" in resp.content.decode()


@pytest.mark.django_db
def test_cluster_space_page_renders_latest_only(client, cluster, instance):
    """空间页必须只取最新一次快照，不能把历史采样累加。"""
    from apps.monitor.models import MetricSample

    now = timezone.now()
    # 旧快照：50；新快照：80。页面应当显示 80，而不是 130
    MetricSample.objects.create(
        instance=instance,
        metric="mysql.database_size_bytes",
        dimension="appdb",
        value=50.0,
        collected_at=now - timedelta(minutes=5),
    )
    MetricSample.objects.create(
        instance=instance,
        metric="mysql.database_size_bytes",
        dimension="appdb",
        value=80.0,
        collected_at=now,
    )

    resp = client.get(f"/clusters/{cluster.pk}/monitor/space/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "appdb" in body
    # 采用最新快照 80，旧快照 50 不得出现，更不能累加成 130
    assert 'data-bytes="80"' in body
    assert 'data-bytes="50"' not in body
    assert "130" not in body


@pytest.mark.django_db
def test_cluster_space_page_without_data(client, cluster):
    resp = client.get(f"/clusters/{cluster.pk}/monitor/space/")
    assert resp.status_code == 200
    assert "还没有空间数据" in resp.content.decode()


@pytest.mark.django_db
def test_cluster_space_unsupported_engine(client, db):
    from apps.assets.models import Cluster, Department

    dept = Department.objects.create(name="空间其他部")
    cluster = Cluster.objects.create(
        name="存档集群", department=dept, engine="other"
    )

    resp = client.get(f"/clusters/{cluster.pk}/monitor/space/")
    assert resp.status_code == 200
    assert "暂不支持空间采集" in resp.content.decode()


@pytest.mark.django_db
def test_overview_404_for_missing_cluster(client):
    resp = client.get("/clusters/999999/monitor/")
    assert resp.status_code == 404
