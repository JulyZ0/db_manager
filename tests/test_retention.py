"""日志保留期清理的测试。

为什么这件事需要测试
--------------------
三张日志表只增不减，一年就是百万行。清理任务本身又是"删数据"的操作，
写错方向（比如删掉不该删的）不会有任何报错，只会安静地把近期记录抹掉：

- 删错方向 → 保留期内的记录消失，事故现场丢失；
- 不分批   → 一次 DELETE 百万行，SQLite 上整站写入卡住；
- 保留期置 0 被当成 "删全部" 而不是 "永久保留"。

因此这里固定：只删早于保留期的、分批推进、0 表示不删、dry-run 只统计。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.metrics.retention import cleanup_expired_logs

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# 构造数据
# ---------------------------------------------------------------------------


def _make_slow_log(age_days: int, *, request_id: str = "r") -> object:
    from apps.metrics.models import SlowRequestLog

    row = SlowRequestLog.objects.create(
        request_id=request_id,
        method="GET",
        path="/instances/",
        status_code=200,
        duration_ms=900,
    )
    _backdate(row, age_days)
    return row


def _make_audit_log(age_days: int) -> object:
    from apps.dbconsole.models import StatementAuditLog

    row = StatementAuditLog.objects.create(
        engine="mysql",
        kind=StatementAuditLog.Kind.QUERY,
        statement_preview="SELECT 1",
        ok=True,
    )
    _backdate(row, age_days)
    return row


def _make_rotation_log(age_days: int) -> object:
    from apps.assets.models import PasswordRotationLog

    row = PasswordRotationLog.objects.create(
        instance=None,
        instance_label="legacy (10.0.0.1:3306)",
    )
    _backdate(row, age_days)
    return row


def _backdate(row, age_days: int) -> None:
    """把 created_at 改到过去。

    ``auto_now_add=True`` 会在 save 时强行覆盖成当前时间，
    所以只能用 ``update()`` 绕过模型层——这也正是测试里唯一能造出
    "历史数据"的办法。
    """
    moment = timezone.now() - timedelta(days=age_days)
    type(row).objects.filter(pk=row.pk).update(created_at=moment)


# ---------------------------------------------------------------------------
# 只删该删的
# ---------------------------------------------------------------------------


class TestOnlyExpiredIsDeleted:
    def test_old_records_are_deleted(self):
        _make_slow_log(age_days=120)
        result = cleanup_expired_logs()

        from apps.metrics.models import SlowRequestLog

        assert SlowRequestLog.objects.count() == 0
        assert result["tables"]["slow_request_log"]["deleted"] == 1

    def test_recent_records_survive(self):
        """保留期内的记录一条都不能少——这是本任务最大的风险点。"""
        _make_slow_log(age_days=1)
        _make_slow_log(age_days=89, request_id="r2")
        cleanup_expired_logs()

        from apps.metrics.models import SlowRequestLog

        assert SlowRequestLog.objects.count() == 2

    def test_boundary_just_expired(self):
        """恰好越过保留期的记录应被删除（91 天 > 90 天）。"""
        _make_slow_log(age_days=91)
        cleanup_expired_logs()

        from apps.metrics.models import SlowRequestLog

        assert SlowRequestLog.objects.count() == 0

    def test_all_three_tables_are_cleaned(self):
        _make_slow_log(age_days=200)
        _make_audit_log(age_days=400)
        _make_rotation_log(age_days=800)

        result = cleanup_expired_logs()

        from apps.assets.models import PasswordRotationLog
        from apps.dbconsole.models import StatementAuditLog
        from apps.metrics.models import SlowRequestLog

        assert SlowRequestLog.objects.count() == 0
        assert StatementAuditLog.objects.count() == 0
        assert PasswordRotationLog.objects.count() == 0
        assert result["total_deleted"] == 3

    def test_each_table_uses_its_own_retention(self):
        """年老的慢请求该删，同样年老的轮换历史（保留 365 天）该留。"""
        _make_slow_log(age_days=200)
        _make_rotation_log(age_days=200)

        cleanup_expired_logs()

        from apps.assets.models import PasswordRotationLog
        from apps.metrics.models import SlowRequestLog

        assert SlowRequestLog.objects.count() == 0
        assert PasswordRotationLog.objects.count() == 1


# ---------------------------------------------------------------------------
# 保留期配置
# ---------------------------------------------------------------------------


class TestRetentionConfig:
    def test_zero_means_keep_forever(self, settings):
        """0 是「永久保留」，不是「全部删除」。

        这个反向理解会让清理任务把整张表清空，且没有任何报错。
        """
        settings.SLOW_LOG_RETENTION_DAYS = 0
        _make_slow_log(age_days=5000)

        result = cleanup_expired_logs()

        from apps.metrics.models import SlowRequestLog

        assert SlowRequestLog.objects.count() == 1
        assert result["tables"]["slow_request_log"]["skipped"] == "永久保留"

    def test_custom_retention_is_honored(self, settings):
        settings.SLOW_LOG_RETENTION_DAYS = 7
        _make_slow_log(age_days=10)
        _make_slow_log(age_days=3, request_id="r2")

        cleanup_expired_logs()

        from apps.metrics.models import SlowRequestLog

        assert SlowRequestLog.objects.count() == 1

    def test_invalid_value_falls_back_to_default(self, settings):
        settings.SLOW_LOG_RETENTION_DAYS = "not-a-number"
        _make_slow_log(age_days=120)

        cleanup_expired_logs()

        from apps.metrics.models import SlowRequestLog

        # 退回默认 90 天，因此 120 天的记录仍会被删
        assert SlowRequestLog.objects.count() == 0


# ---------------------------------------------------------------------------
# 分批与预演
# ---------------------------------------------------------------------------


class TestBatching:
    def test_deletes_in_multiple_batches(self, settings):
        """批大小小于总量时必须持续删，不能只删一批就停。"""
        settings.LOG_CLEANUP_BATCH_SIZE = 3
        for i in range(10):
            _make_slow_log(age_days=120, request_id=f"r{i}")

        result = cleanup_expired_logs()

        from apps.metrics.models import SlowRequestLog

        assert SlowRequestLog.objects.count() == 0
        assert result["tables"]["slow_request_log"]["deleted"] == 10

    def test_max_batches_caps_one_run(self, settings):
        """单次运行的批数上限生效：宁可下次再删，也不让清理变成慢任务。"""
        settings.LOG_CLEANUP_BATCH_SIZE = 2
        settings.LOG_CLEANUP_MAX_BATCHES = 2
        for i in range(10):
            _make_slow_log(age_days=120, request_id=f"r{i}")

        result = cleanup_expired_logs()

        from apps.metrics.models import SlowRequestLog

        # 只删了 2 批 × 2 条
        assert result["tables"]["slow_request_log"]["deleted"] == 4
        assert SlowRequestLog.objects.count() == 6


class TestDryRun:
    def test_dry_run_counts_without_deleting(self):
        _make_slow_log(age_days=200)
        _make_slow_log(age_days=200, request_id="r2")

        result = cleanup_expired_logs(dry_run=True)

        from apps.metrics.models import SlowRequestLog

        assert SlowRequestLog.objects.count() == 2, "预演不得删除任何数据"
        assert result["dry_run"] is True
        assert result["tables"]["slow_request_log"]["deleted"] == 2


# ---------------------------------------------------------------------------
# 健壮性
# ---------------------------------------------------------------------------


class TestResilience:
    def test_one_table_failure_does_not_block_others(self, monkeypatch):
        """单表出错应被隔离——否则一张表删不动就永远清理不了另两张。"""
        import apps.metrics.retention as retention

        _make_audit_log(age_days=400)

        real_load = retention._load_model
        calls = {"n": 0}

        def flaky_load(path):
            calls["n"] += 1
            if "SlowRequestLog" in path:
                raise RuntimeError("模拟失败")
            return real_load(path)

        monkeypatch.setattr(retention, "_load_model", flaky_load)
        result = cleanup_expired_logs()

        assert result["tables"]["slow_request_log"]["error"] is True
        # 其它表照常清理
        from apps.dbconsole.models import StatementAuditLog

        assert StatementAuditLog.objects.count() == 0

    def test_idempotent(self):
        """重复执行无副作用，第二次不应再删任何东西。"""
        _make_slow_log(age_days=200)

        first = cleanup_expired_logs()
        second = cleanup_expired_logs()

        assert first["total_deleted"] == 1
        assert second["total_deleted"] == 0

    def test_empty_tables_are_fine(self):
        """空表不该报错——新部署的站点第一次跑就是这种情况。"""
        result = cleanup_expired_logs()
        assert result["total_deleted"] == 0


# ---------------------------------------------------------------------------
# 调度接入
# ---------------------------------------------------------------------------


class TestScheduling:
    def test_task_registered_in_beat(self):
        from django.conf import settings

        assert "cleanup-expired-logs" in settings.CELERY_BEAT_SCHEDULE
        entry = settings.CELERY_BEAT_SCHEDULE["cleanup-expired-logs"]
        assert entry["task"] == "apps.metrics.tasks.cleanup_expired_logs"

    def test_task_is_off_peak_hour(self):
        """避开 00:00 的统计任务，减少写锁争抢。"""
        from django.conf import settings

        entry = settings.CELERY_BEAT_SCHEDULE["cleanup-expired-logs"]
        assert entry["schedule"].hour == {3}

    def test_celery_task_is_callable(self):
        from apps.metrics.tasks import cleanup_expired_logs as task

        assert task.name == "apps.metrics.tasks.cleanup_expired_logs"
