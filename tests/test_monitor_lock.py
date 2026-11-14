"""锁等待与死锁采集测试。

重点钉住两条相反的持久化语义：
- ``LockWait`` 是**瞬时状态**：每轮清空重写，否则页面会同时显示"早已解开的
  等待"和"正在发生的等待"，运维无法判断此刻到底还堵不堵；
- ``DeadlockEvent`` 是**历史事件**：只在累计值变大时留档，否则一条早已解决的
  死锁会重复出现几十次，把真正的多次死锁掩盖掉。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def instance(db):
    from apps.assets.models import Cluster, Department, Instance

    dept = Department.objects.create(name="锁分析部")
    cluster = Cluster.objects.create(name="锁集群", department=dept, engine="mysql")
    return Instance.objects.create(
        name="lock-1", cluster=cluster, host="10.9.0.1", port=3306, username="mon"
    )


def wait_row(waiting="thread:11", blocking="thread:22", seconds=30.0):
    return {
        "waiting_id": waiting,
        "blocking_id": blocking,
        "waiting_label": "trx 11",
        "blocking_label": "trx 22",
        "lock_type": "RECORD X",
        "relation": "shop.orders",
        "wait_seconds": seconds,
        "query": "UPDATE orders SET status = ? WHERE id = ?",
    }


@pytest.mark.django_db
def test_persist_writes_waits(instance):
    from apps.monitor.lock_collector import persist_locks
    from apps.monitor.models import LockWait

    stats = persist_locks(instance, {"waits": [wait_row()], "deadlock_total": 0})

    assert stats["waits"] == 1
    row = LockWait.objects.get()
    assert row.waiting_id == "thread:11"
    assert row.relation == "shop.orders"
    assert row.wait_seconds == 30.0


@pytest.mark.django_db
def test_persist_clears_previous_waits(instance):
    """第二轮解开了等待：旧记录必须消失，否则页面还显示着"正在堵"。"""
    from apps.monitor.lock_collector import persist_locks
    from apps.monitor.models import LockWait

    persist_locks(instance, {"waits": [wait_row(waiting="thread:1")], "deadlock_total": 0})
    assert LockWait.objects.count() == 1

    persist_locks(instance, {"waits": [], "deadlock_total": 0})

    assert LockWait.objects.count() == 0


@pytest.mark.django_db
def test_persist_replaces_with_new_waiters(instance):
    from apps.monitor.lock_collector import persist_locks
    from apps.monitor.models import LockWait

    persist_locks(instance, {"waits": [wait_row(waiting="thread:old")], "deadlock_total": 0})
    persist_locks(instance, {"waits": [wait_row(waiting="thread:new")], "deadlock_total": 0})

    assert LockWait.objects.count() == 1
    assert LockWait.objects.get().waiting_id == "thread:new"


@pytest.mark.django_db
def test_deadlock_recorded_on_increase(instance):
    from apps.monitor.lock_collector import persist_locks
    from apps.monitor.models import DeadlockEvent

    stats = persist_locks(instance, {"waits": [], "deadlock_total": 3})

    assert stats["deadlocks"] == 1
    assert DeadlockEvent.objects.get().deadlock_total == 3


@pytest.mark.django_db
def test_deadlock_not_duplicated_when_unchanged(instance):
    """累计值没变说明没有新死锁，不能再记一条。"""
    from apps.monitor.lock_collector import persist_locks
    from apps.monitor.models import DeadlockEvent

    persist_locks(instance, {"waits": [], "deadlock_total": 3})
    stats = persist_locks(instance, {"waits": [], "deadlock_total": 3})

    assert stats["deadlocks"] == 0
    assert DeadlockEvent.objects.count() == 1


@pytest.mark.django_db
def test_deadlock_recorded_after_source_reset(instance):
    """源端重启后累计值归零再增长：变小也要能重新记，不能被历史卡住。"""
    from apps.monitor.lock_collector import persist_locks
    from apps.monitor.models import DeadlockEvent

    persist_locks(instance, {"waits": [], "deadlock_total": 10})
    # 重启后归零，本轮无死锁
    persist_locks(instance, {"waits": [], "deadlock_total": 0})
    # 归零后重新累计到 2：这是新发生的死锁
    persist_locks(instance, {"waits": [], "deadlock_total": 2})

    assert DeadlockEvent.objects.count() == 2


@pytest.mark.django_db
def test_deadlock_detail_truncated(instance):
    from apps.monitor.lock_collector import persist_locks
    from apps.monitor.models import DeadlockEvent

    persist_locks(
        instance, {"waits": [], "deadlock_total": 1, "deadlock_detail": "x" * 5000}
    )

    assert len(DeadlockEvent.objects.get().detail) == 2000


@pytest.mark.django_db
def test_fetch_skips_unsupported_engine(db):
    """Redis 是单线程模型，不存在行锁等待：应返回 supported=False 而非报错。"""
    from apps.assets.models import Cluster, Department, Instance
    from apps.monitor.lock_collector import fetch_locks

    dept = Department.objects.create(name="Redis部")
    cluster = Cluster.objects.create(name="redis锁", department=dept, engine="redis")
    inst = Instance.objects.create(
        name="r-1", cluster=cluster, host="10.9.1.1", port=6379, username="mon"
    )

    result = fetch_locks(inst, "pw", 5)

    assert result["ok"] is True
    assert result["supported"] is False
    assert result["waits"] == []


@pytest.mark.django_db
def test_fetch_swallows_driver_error(instance, monkeypatch):
    """驱动异常只留类型名，不能把连接串/用户名带到上层。"""
    from apps.monitor import lock_collector

    def boom(inst, password, timeout):
        raise RuntimeError("password=secret host=10.9.0.1")

    monkeypatch.setattr(lock_collector, "_connect_mysql", boom)

    result = lock_collector.fetch_locks(instance, "pw", 5)

    assert result["ok"] is False
    assert result["error"] == "query_failed"
    assert "secret" not in str(result)


@pytest.mark.django_db
def test_collect_locks_all_skips_redis_only(instance, monkeypatch):
    """全部实例都是 Redis 时不产生任何调用。"""
    from apps.assets.models import Cluster, Department, Instance
    from apps.monitor import services

    instance.delete()
    dept = Department.objects.create(name="纯Redis部")
    cluster = Cluster.objects.create(name="纯redis", department=dept, engine="redis")
    Instance.objects.create(
        name="r-only", cluster=cluster, host="10.9.2.1", port=6379, username="mon"
    )

    called = []
    monkeypatch.setattr(
        "apps.monitor.lock_collector.fetch_locks",
        lambda *a: called.append(1) or {"ok": True, "waits": []},
    )

    result = services.collect_locks_all()

    assert result["total"] == 0
    assert called == []


@pytest.mark.django_db
def test_collect_locks_all_isolates_failures(instance, monkeypatch):
    """一个实例取数失败不影响另一个。"""
    from apps.assets.models import Instance
    from apps.monitor import services

    second = Instance.objects.create(
        name="lock-2", cluster=instance.cluster, host="10.9.0.2", port=3306, username="mon"
    )

    monkeypatch.setattr(
        "apps.dbconsole.services.resolve_password", lambda inst: ("pw", None)
    )

    def fake_fetch(inst, pw, timeout):
        if inst.pk == second.pk:
            raise RuntimeError("boom")
        return {"ok": True, "waits": [wait_row()], "deadlock_total": 0}

    monkeypatch.setattr("apps.monitor.lock_collector.fetch_locks", fake_fetch)

    result = services.collect_locks_all()

    assert result["total"] == 2
    assert result["waits"] == 1


@pytest.mark.django_db
def test_collect_locks_all_respects_switch(instance, monkeypatch, settings):
    from apps.monitor import services

    settings.MONITOR_LOCK_ENABLED = False

    assert services.collect_locks_all()["skipped"] is True


@pytest.mark.django_db
def test_lock_wait_cascade_on_instance_delete(instance):
    from apps.monitor.lock_collector import persist_locks
    from apps.monitor.models import LockWait

    persist_locks(instance, {"waits": [wait_row()], "deadlock_total": 1})
    instance.delete()

    assert LockWait.objects.count() == 0
