"""monitor 模型的约束测试。

只固定"建模时的设计承诺"，不测采集逻辑（那属于采集器测试）：
- 指标长表能按 (instance, metric, collected_at) 取到有序序列；
- 采样/采集记录随实例级联删除，不留孤儿行；
- 采集记录默认按时间倒序，页面取「最近一次」无需额外排序。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone


@pytest.fixture
def instance(db):
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="监控测试部")
    cluster = Cluster.objects.create(name="监控测试集群", department=dept)
    return Instance.objects.create(
        name="监控测试实例",
        cluster=cluster,
        host="10.1.2.3",
        port=3306,
        username="monitor",
    )


@pytest.mark.django_db
def test_metric_samples_ordered_by_time(instance):
    """同一指标按 collected_at 升序返回，趋势图据此直接出点。"""
    from apps.monitor.models import MetricSample

    now = timezone.now()
    for offset, value in ((2, 30.0), (0, 10.0), (1, 20.0)):
        MetricSample.objects.create(
            instance=instance,
            metric="mysql.connections",
            value=value,
            collected_at=now + timedelta(minutes=offset),
        )

    rows = list(
        MetricSample.objects.filter(
            instance=instance, metric="mysql.connections"
        ).order_by("collected_at")
    )
    assert [r.value for r in rows] == [10.0, 20.0, 30.0]


@pytest.mark.django_db
def test_dimension_separates_rows(instance):
    """同一时刻同一指标可按库名等多行并存，靠 dimension 区分。"""
    from apps.monitor.models import MetricSample

    now = timezone.now()
    for db_name, size in (("app", 100.0), ("report", 250.0)):
        MetricSample.objects.create(
            instance=instance,
            metric="mysql.database_size_bytes",
            dimension=db_name,
            value=size,
            collected_at=now,
        )

    rows = MetricSample.objects.filter(
        instance=instance, metric="mysql.database_size_bytes"
    ).order_by("dimension")
    assert [(r.dimension, r.value) for r in rows] == [("app", 100.0), ("report", 250.0)]


@pytest.mark.django_db
def test_samples_cascade_on_instance_delete(instance):
    """观测数据随实例删除，不留无法归属的孤儿行。"""
    from apps.monitor.models import CollectionRun, MetricSample

    now = timezone.now()
    MetricSample.objects.create(
        instance=instance, metric="mysql.qps", value=1.0, collected_at=now
    )
    CollectionRun.objects.create(instance=instance, collected_at=now, ok=True)

    instance.delete()

    assert MetricSample.objects.count() == 0
    assert CollectionRun.objects.count() == 0


@pytest.mark.django_db
def test_collection_run_defaults_to_desc(instance):
    """默认倒序：页面「最近一次采集」直接 first() 即可。"""
    from apps.monitor.models import CollectionRun

    now = timezone.now()
    old = CollectionRun.objects.create(
        instance=instance, collected_at=now - timedelta(minutes=5), ok=False
    )
    new = CollectionRun.objects.create(
        instance=instance, collected_at=now, ok=True
    )

    assert list(CollectionRun.objects.filter(instance=instance)) == [new, old]
