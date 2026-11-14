"""巡检评分测试。

评分是"给人看的结论"，算错会直接误导运维去修不存在的问题。
这里钉住三件事：
1. 分段线性打分的方向与边界（越大越好 / 越小越好都要对）；
2. **没有数据的项必须跳过**，不能当 0 分——否则健康机器会显示 40 分；
3. 严重告警必须把分数打到 0，警告级扣到一半。
"""

from __future__ import annotations

import pytest

from apps.monitor.inspect import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_UNKNOWN,
    STATUS_WARN,
    _linear,
    grade_of,
    inspect_instance,
)


def make(**overrides):
    params = {
        "instance_id": 1,
        "instance_name": "inst-1",
        "engine": "mysql",
        "values": {
            "mysql.connection_usage_percent": 30.0,
            "mysql.buffer_pool_hit_percent": 99.5,
            "mysql.slow_queries_per_sec": 0.0,
            "mysql.replication_delay_seconds": 0.0,
            "mysql.threads_running": 3.0,
            "mysql.total_size_bytes": 1e9,
        },
        "collect_ok": True,
        "has_password": True,
    }
    params.update(overrides)
    return inspect_instance(**params)


def by_key(report, key):
    return next(r for r in report.results if r.key == key)


# ---------------------------------------------------------------------------
# 线性映射
# ---------------------------------------------------------------------------


def test_linear_smaller_is_better():
    """连接使用率：70% 起扣分，90% 归零。"""
    assert _linear(30.0, 70.0, 90.0) == 100.0
    assert _linear(70.0, 70.0, 90.0) == 100.0
    assert _linear(80.0, 70.0, 90.0) == 50.0
    assert _linear(90.0, 70.0, 90.0) == 0.0
    assert _linear(99.0, 70.0, 90.0) == 0.0


def test_linear_larger_is_better():
    """缓存命中率：99% 满分，90% 归零。"""
    assert _linear(99.5, 99.0, 90.0) == 100.0
    assert _linear(94.5, 99.0, 90.0) == 50.0
    assert _linear(90.0, 99.0, 90.0) == 0.0
    assert _linear(80.0, 99.0, 90.0) == 0.0


def test_grade_thresholds():
    assert grade_of(95) == "优秀"
    assert grade_of(90) == "优秀"
    assert grade_of(89.9) == "良好"
    assert grade_of(75) == "良好"
    assert grade_of(60) == "一般"
    assert grade_of(0) == "较差"


# ---------------------------------------------------------------------------
# 健康实例
# ---------------------------------------------------------------------------


def test_healthy_instance_scores_high():
    report = make()

    assert report.score >= 95
    assert report.grade == "优秀"
    assert all(r.status in (STATUS_OK, STATUS_UNKNOWN) for r in report.results)


def test_counts_reported():
    report = make()
    counts = report.counts

    assert sum(counts.values()) == len(report.results)


# ---------------------------------------------------------------------------
# 单项扣分
# ---------------------------------------------------------------------------


def test_high_connection_usage_deducts():
    report = make(
        values={
            "mysql.connection_usage_percent": 88.0,
            "mysql.buffer_pool_hit_percent": 99.5,
            "mysql.slow_queries_per_sec": 0.0,
            "mysql.replication_delay_seconds": 0.0,
        }
    )

    check = by_key(report, "conn")
    assert check.score == 10.0
    assert check.status == STATUS_FAIL
    assert report.score < 95


def test_low_cache_hit_deducts():
    report = make(
        values={
            "mysql.connection_usage_percent": 30.0,
            "mysql.buffer_pool_hit_percent": 91.0,
            "mysql.slow_queries_per_sec": 0.0,
            "mysql.replication_delay_seconds": 0.0,
        }
    )

    check = by_key(report, "cache")
    # 91% 落在 [99, 90] 区间：(91-99)/(90-99)=0.889 → 100*(1-0.889)=11.1
    assert check.score == pytest.approx(11.1, abs=0.2)


def test_replication_lag_deducts_heavily():
    """复制延迟是可用性问题，权重 2.0，扣分应显著。"""
    report = make(
        values={
            "mysql.connection_usage_percent": 30.0,
            "mysql.buffer_pool_hit_percent": 99.5,
            "mysql.slow_queries_per_sec": 0.0,
            "mysql.replication_delay_seconds": 30.0,
        }
    )

    check = by_key(report, "repl")
    assert check.score == 0.0
    assert check.status == STATUS_FAIL


def test_lock_wait_deducts():
    """150 秒锁等待：分数腰斩，且已属 fail 级——业务请求正在被阻塞。"""
    report = make(longest_lock_wait=150.0)

    check = by_key(report, "lock")
    assert check.score == 50.0
    assert check.status == STATUS_FAIL


def test_collect_failure_deducts():
    report = make(collect_ok=False, collect_message="auth_failed")

    check = by_key(report, "collect")
    assert check.score == 0.0
    assert check.status == STATUS_FAIL
    assert "auth_failed" in check.detail


def test_missing_password_deducts():
    report = make(has_password=False)

    check = by_key(report, "credentials")
    assert check.score == 0.0
    assert "未登记密码" in check.detail


# ---------------------------------------------------------------------------
# 告警
# ---------------------------------------------------------------------------


def test_critical_alert_zeroes_alert_score():
    report = make(critical_alerts=1, firing_alerts=1)

    check = by_key(report, "alert")
    assert check.score == 0.0
    assert check.status == STATUS_FAIL
    assert "严重" in check.detail


def test_warning_alert_halves_alert_score():
    """警告级告警是黄灯：落在 warn 区间，不应渲染成 fail。"""
    report = make(firing_alerts=2)

    check = by_key(report, "alert")
    assert check.score == 70.0
    assert check.status == STATUS_WARN


# ---------------------------------------------------------------------------
# 缺数据：必须跳过，不能当 0 分
# ---------------------------------------------------------------------------


def test_missing_metric_is_skipped_not_zero():
    """没有复制延迟数据（无从库）不能把分数拉低。"""
    report = make(
        values={
            "mysql.connection_usage_percent": 30.0,
            "mysql.buffer_pool_hit_percent": 99.5,
            "mysql.slow_queries_per_sec": 0.0,
            # 没有 replication_delay_seconds
        }
    )

    check = by_key(report, "repl")
    assert check.score is None
    assert check.skipped is True
    assert check.status == STATUS_UNKNOWN
    # 关键：跳过的项不参与加权，健康机器仍是高分
    assert report.score >= 95


def test_no_collect_record_is_skipped():
    report = make(collect_ok=None)

    check = by_key(report, "collect")
    assert check.score is None
    assert "尚无采集记录" in check.detail


def test_all_missing_scores_zero_but_unknown():
    """指标全缺失时分数封顶且评级为「数据不足」，不能显示优秀。

    这条是本模块最重要的防线：若采集/凭据/锁/告警四项默认满分把总分拉成
    100 并评级「优秀」，就等于告诉运维"监控瞎了也是健康"。
    """
    report = make(values={}, collect_ok=None)

    assert report.insufficient is True
    assert report.grade == "数据不足"
    assert report.score <= 59.0
    assert report.counts[STATUS_UNKNOWN] >= 5


# ---------------------------------------------------------------------------
# 引擎差异
# ---------------------------------------------------------------------------


def test_redis_skips_connection_usage():
    report = inspect_instance(
        instance_id=2,
        instance_name="redis-1",
        engine="redis",
        values={
            "redis.cache_hit_percent": 99.5,
            "redis.connected_clients": 5.0,
            "redis.memory_used_bytes": 1e8,
        },
        collect_ok=True,
    )

    check = by_key(report, "conn")
    assert check.score is None
    assert "无连接上限指标" in check.detail


def test_postgresql_skips_slow_rate():
    report = inspect_instance(
        instance_id=3,
        instance_name="pg-1",
        engine="postgresql",
        values={
            "pg.connection_usage_percent": 40.0,
            "pg.cache_hit_percent": 98.0,
            "pg.replication_lag_seconds": 0.0,
        },
        collect_ok=True,
    )

    check = by_key(report, "slow")
    assert check.score is None


def test_unknown_engine_does_not_crash():
    report = inspect_instance(
        instance_id=4,
        instance_name="mongo-1",
        engine="mongodb",
        values={},
        collect_ok=True,
    )

    # 未知引擎没有指标映射：所有指标类检查跳过，但采集与凭据项仍评分
    assert by_key(report, "collect").score == 100.0
    assert by_key(report, "conn").score is None
    assert report.score > 0
