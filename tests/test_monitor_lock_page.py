"""锁分析页面测试。

页面本身不连目标库（读的是采集落库的快照），因此这里不需要 mock，
直接断言"该显示的显示、不该显示的不显示"。
"""

from __future__ import annotations

import pytest
from django.utils import timezone


@pytest.fixture
def instance(db):
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="锁页面部")
    cluster = Cluster.objects.create(name="锁页面集群", department=dept, engine="mysql")
    return Instance.objects.create(
        name="lockp-1", cluster=cluster, host="10.9.5.1", port=3306, username="mon"
    )


def make_wait(instance, waiting="thread:11", blocking="thread:22", seconds=30.0):
    from apps.monitor.models import LockWait

    return LockWait.objects.create(
        instance=instance,
        collected_at=timezone.now(),
        waiting_id=waiting,
        blocking_id=blocking,
        # label 跟随 waiting 才能让不同等待在页面上可区分——模板渲染的是
        # label（含 thread/trx 号），不是 waiting_id
        waiting_label=waiting,
        blocking_label=blocking,
        lock_type="RECORD X",
        relation="shop.orders",
        wait_seconds=seconds,
        query="UPDATE orders SET status = ? WHERE id = ?",
    )


def make_deadlock(instance, total=3, detail=""):
    from apps.monitor.models import DeadlockEvent

    return DeadlockEvent.objects.create(
        instance=instance,
        detected_at=timezone.now(),
        deadlock_total=total,
        detail=detail,
    )


@pytest.mark.django_db
def test_lock_page_renders_waits(client, instance):
    make_wait(instance)

    resp = client.get("/monitor/locks/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "当前锁等待（1 条）" in body
    assert "thread:11" in body
    assert "shop.orders" in body


@pytest.mark.django_db
def test_lock_page_orders_by_wait_time(client, instance):
    """等得最久的要排最前面——那才是该先处理的那个。"""
    make_wait(instance, waiting="thread:short", seconds=5.0)
    make_wait(instance, waiting="thread:long", seconds=600.0)

    body = client.get("/monitor/locks/").content.decode()

    assert body.index("thread:long") < body.index("thread:short")


@pytest.mark.django_db
def test_lock_page_empty_state(client, instance):
    resp = client.get("/monitor/locks/")
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "当前没有锁等待" in body


@pytest.mark.django_db
def test_lock_page_shows_deadlock_history(client, instance):
    make_deadlock(instance, total=7, detail="LATEST DETECTED DEADLOCK ...")

    body = client.get("/monitor/locks/").content.decode()

    assert "死锁历史" in body
    assert "7" in body
    assert "LATEST DETECTED DEADLOCK" in body


@pytest.mark.django_db
def test_lock_page_without_deadlocks(client, instance):
    body = client.get("/monitor/locks/").content.decode()
    assert "没有记录到死锁" in body


@pytest.mark.django_db
def test_instance_lock_page_filters_scope(client, instance):
    """单实例视角不显示「实例」列，也不含别的实例数据。"""
    from apps.assets.models import Instance

    other = Instance.objects.create(
        name="lockp-other", cluster=instance.cluster, host="10.9.5.2", port=3306, username="mon"
    )
    make_wait(instance, waiting="thread:mine")
    make_wait(other, waiting="thread:other")

    body = client.get(f"/instances/{instance.pk}/monitor/locks/").content.decode()

    assert "thread:mine" in body
    assert "thread:other" not in body
    assert "全库视图" in body


@pytest.mark.django_db
def test_lock_page_global_view_shows_instance_names(client, instance):
    make_wait(instance)
    body = client.get("/monitor/locks/").content.decode()
    assert "lockp-1" in body


@pytest.mark.django_db
def test_lock_page_unsupported_engine(client, db):
    """Redis 无行锁概念：给说明而不是空白表格。"""
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="Redis锁页面部")
    cluster = Cluster.objects.create(name="redis锁页面", department=dept, engine="redis")
    inst = Instance.objects.create(
        name="r-lock", cluster=cluster, host="10.9.6.1", port=6379, username="mon"
    )

    body = client.get(f"/instances/{inst.pk}/monitor/locks/").content.decode()

    assert "没有行锁等待的概念" in body


@pytest.mark.django_db
def test_lock_page_404_for_missing_instance(client):
    assert client.get("/instances/999999/monitor/locks/").status_code == 404


@pytest.mark.django_db
def test_mysql_wait_without_duration_shows_dash(client, instance):
    """MySQL 不提供等待时长，应显示 — 而不是编一个 0 秒。"""
    make_wait(instance, seconds=0.0)

    body = client.get("/monitor/locks/").content.decode()

    assert "data_lock_waits 不提供等待时长" in body
