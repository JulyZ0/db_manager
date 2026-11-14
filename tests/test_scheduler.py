"""调度器测试：crontab 匹配语义与触发去重。

这些测试不依赖 Redis，也不需要 Celery worker，
验证的正是「无 Redis 环境下定时任务仍会按时触发」这一核心承诺。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from celery.schedules import crontab

from common.scheduler import InProcessScheduler, crontab_matches, probe_broker


class TestCrontabMatches:
    def test_every_12_hours_fires_at_midnight(self):
        schedule = crontab(minute=0, hour="0,12")
        assert crontab_matches(schedule, datetime(2026, 11, 13, 0, 0))
        assert crontab_matches(schedule, datetime(2026, 11, 13, 12, 0))

    def test_every_12_hours_does_not_fire_between(self):
        schedule = crontab(minute=0, hour="0,12")
        assert not crontab_matches(schedule, datetime(2026, 11, 13, 0, 1))
        assert not crontab_matches(schedule, datetime(2026, 11, 13, 11, 59))
        assert not crontab_matches(schedule, datetime(2026, 11, 13, 6, 0))

    def test_daily_at_midnight(self):
        schedule = crontab(minute=0, hour=0)
        assert crontab_matches(schedule, datetime(2026, 11, 13, 0, 0))
        assert not crontab_matches(schedule, datetime(2026, 11, 13, 12, 0))


class TestInProcessScheduler:
    def _schedule(self, **crontab_kwargs):
        from celery.schedules import crontab as _crontab

        return {
            "demo": {
                "task": "tests.fake_tasks.noop",
                "schedule": _crontab(**crontab_kwargs),
            }
        }

    def test_tick_fires_matching_task(self, monkeypatch):
        calls: list[str] = []
        scheduler = InProcessScheduler(self._schedule(minute=0, hour=0))
        monkeypatch.setattr(
            scheduler, "_dispatch", lambda name, task: calls.append(name)
        )

        fired = scheduler.tick(datetime(2026, 11, 13, 0, 0))
        assert fired == ["demo"]
        assert calls == ["demo"]

    def test_tick_does_not_fire_twice_in_same_minute(self, monkeypatch):
        calls: list[str] = []
        scheduler = InProcessScheduler(self._schedule(minute=0, hour=0))
        monkeypatch.setattr(
            scheduler, "_dispatch", lambda name, task: calls.append(name)
        )

        moment = datetime(2026, 11, 13, 0, 0)
        scheduler.tick(moment)
        scheduler.tick(moment)
        assert calls == ["demo"], "同一分钟内不应重复触发"

    def test_tick_skips_non_matching_minute(self, monkeypatch):
        calls: list[str] = []
        scheduler = InProcessScheduler(self._schedule(minute=0, hour=0))
        monkeypatch.setattr(
            scheduler, "_dispatch", lambda name, task: calls.append(name)
        )

        fired = scheduler.tick(datetime(2026, 11, 13, 0, 5))
        assert fired == []
        assert calls == []

    def test_tick_fires_again_next_day(self, monkeypatch):
        calls: list[str] = []
        scheduler = InProcessScheduler(self._schedule(minute=0, hour=0))
        monkeypatch.setattr(
            scheduler, "_dispatch", lambda name, task: calls.append(name)
        )

        scheduler.tick(datetime(2026, 11, 13, 0, 0))
        scheduler.tick(datetime(2026, 11, 14, 0, 0))
        assert calls == ["demo", "demo"]


class TestBrokerProbe:
    def test_unreachable_port_returns_false(self):
        # 一个几乎不可能开放的端口，且超时极短
        assert probe_broker("redis://127.0.0.1:6399/0", timeout=0.05) is False

    def test_unsupported_scheme_returns_false(self):
        assert probe_broker("sqlite:///tmp/x.db", timeout=0.05) is False


@pytest.mark.parametrize(
    ("mode", "expected_probe_called"),
    [("inprocess", False), ("celery", False)],
)
def test_explicit_mode_skips_probe(monkeypatch, settings, mode, expected_probe_called):
    """显式指定模式时不应去探测 broker，避免启动被网络超时拖慢。"""
    from common import scheduler as scheduler_mod

    settings.SCHEDULER_MODE = mode

    called = []
    monkeypatch.setattr(
        scheduler_mod, "probe_broker", lambda *a, **k: called.append(1) or True
    )

    assert scheduler_mod.resolve_mode() == mode
    assert bool(called) is expected_probe_called


def test_auto_mode_falls_back_when_broker_down(monkeypatch, settings):
    settings.SCHEDULER_MODE = "auto"
    settings.CELERY_BROKER_URL = "redis://127.0.0.1:6399/0"

    from common import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "probe_broker", lambda *a, **k: False)
    assert scheduler_mod.resolve_mode() == "inprocess"


def test_auto_mode_uses_celery_only_when_worker_responsive(monkeypatch, settings):
    """broker 通且 worker 有回应，才判定为 Celery。"""
    settings.SCHEDULER_MODE = "auto"
    settings.CELERY_BROKER_URL = "redis://127.0.0.1:6379/0"

    from common import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "probe_broker", lambda *a, **k: True)
    monkeypatch.setattr(scheduler_mod, "probe_worker", lambda *a, **k: True)
    assert scheduler_mod.resolve_mode() == "celery"


def test_auto_mode_degrades_when_broker_up_but_no_worker(monkeypatch, settings):
    """**关键回归：Redis 起着但 worker 没起，必须降级。**

    这是最常见的部署状态（Redis 开机自启、worker 需手动拉）。若此时判定为
    Celery，所有定时任务都会静默不执行——密码不轮换、指标不采集、
    告警不评估——而页面显示"已连接消息队列，按计划执行"。
    """
    settings.SCHEDULER_MODE = "auto"
    settings.CELERY_BROKER_URL = "redis://127.0.0.1:6379/0"

    from common import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "probe_broker", lambda *a, **k: True)
    monkeypatch.setattr(scheduler_mod, "probe_worker", lambda *a, **k: False)
    assert scheduler_mod.resolve_mode() == "inprocess"


def test_broker_down_does_not_probe_worker(monkeypatch, settings):
    """broker 都不通就不必再探 worker，省掉一次 0.5 秒超时。"""
    settings.SCHEDULER_MODE = "auto"
    settings.CELERY_BROKER_URL = "redis://127.0.0.1:6399/0"

    from common import scheduler as scheduler_mod

    called = []
    monkeypatch.setattr(scheduler_mod, "probe_broker", lambda *a, **k: False)
    monkeypatch.setattr(
        scheduler_mod, "probe_worker", lambda *a, **k: called.append(1) or True
    )

    assert scheduler_mod.resolve_mode() == "inprocess"
    assert called == []


def test_explicit_celery_not_overridden_by_missing_worker(monkeypatch, settings):
    """显式 SCHEDULER_MODE=celery 时尊重配置，不静默改跑进程内调度。

    否则运维以为任务在 Celery 里跑、实际却在 Web 进程内跑，
    多进程部署下还会重复执行。
    """
    settings.SCHEDULER_MODE = "celery"

    from common import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "probe_worker", lambda *a, **k: False)
    assert scheduler_mod.resolve_mode() == "celery"


def test_probe_worker_returns_false_on_error(monkeypatch):
    """探测自身出错（无 broker/无驱动）时必须返回 False 而不是抛出。"""
    from common import scheduler as scheduler_mod

    class Boom:
        class control:
            @staticmethod
            def inspect(*args, **kwargs):
                raise RuntimeError("no broker")

    monkeypatch.setattr("config.celery.app", Boom(), raising=False)
    assert scheduler_mod.probe_worker(timeout=0.01) is False
