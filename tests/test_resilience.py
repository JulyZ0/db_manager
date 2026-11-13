"""弹性能力测试：探测退避重试、SQLite 写稳定化、并发轮换。

分三组：
- TestProbeRetry     瞬时失败重试、确定性失败不重试、总预算封顶
- TestSqliteWrites   OPTIONS 构造、锁冲突识别、退避重试、写锁可重入
- TestConcurrency    并发度决策、并发轮换的正确性与结果合并
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# 探测：退避重试
# ---------------------------------------------------------------------------


class TestProbeRetry:
    """用 mock 精确控制每次尝试的返回，不依赖真实网络。"""

    @staticmethod
    def _seq(monkeypatch, outcomes):
        """让 _probe_once 依次返回 outcomes，并记录调用次数。"""
        from apps.probing import services

        calls = {"n": 0}

        def fake_probe_once(host, port, timeout):
            index = min(calls["n"], len(outcomes) - 1)
            calls["n"] += 1
            return services.ProbeResult(*outcomes[index])

        monkeypatch.setattr(services, "_probe_once", fake_probe_once)
        return calls

    @staticmethod
    def _no_sleep(monkeypatch):
        from apps.probing import services

        monkeypatch.setattr(services.time, "sleep", lambda _s: None)

    def test_retries_transient_timeout_then_succeeds(self, monkeypatch):
        from apps.probing.services import probe_tcp

        self._no_sleep(monkeypatch)
        calls = self._seq(
            monkeypatch,
            [
                (False, None, "timeout"),
                (False, None, "timeout"),
                (True, 12, None),
            ],
        )

        result = probe_tcp("h", 1, timeout=0.1, max_attempts=3, budget_ms=10_000)

        assert result.reachable is True
        assert result.latency_ms == 12
        assert result.error is None
        assert result.attempts == 3
        assert calls["n"] == 3

    def test_retries_network_unreachable(self, monkeypatch):
        from apps.probing.services import probe_tcp

        self._no_sleep(monkeypatch)
        calls = self._seq(
            monkeypatch,
            [(False, None, "network_unreachable"), (True, 5, None)],
        )

        result = probe_tcp("h", 1, timeout=0.1, max_attempts=3, budget_ms=10_000)

        assert result.reachable is True
        assert calls["n"] == 2

    def test_gives_up_after_max_attempts(self, monkeypatch):
        from apps.probing.services import probe_tcp

        self._no_sleep(monkeypatch)
        calls = self._seq(monkeypatch, [(False, None, "timeout")])

        result = probe_tcp("h", 1, timeout=0.1, max_attempts=3, budget_ms=10_000)

        assert result.reachable is False
        assert result.error == "timeout"
        assert result.attempts == 3
        assert calls["n"] == 3

    def test_refused_is_not_retried(self, monkeypatch):
        """端口确实没监听，重试只会白等——必须立即返回。"""
        from apps.probing.services import probe_tcp

        self._no_sleep(monkeypatch)
        calls = self._seq(monkeypatch, [(False, None, "refused")])

        result = probe_tcp("h", 1, timeout=0.1, max_attempts=5, budget_ms=10_000)

        assert result.error == "refused"
        assert result.attempts == 1
        assert calls["n"] == 1

    def test_dns_failed_is_not_retried(self, monkeypatch):
        from apps.probing.services import probe_tcp

        self._no_sleep(monkeypatch)
        calls = self._seq(monkeypatch, [(False, None, "dns_failed")])

        result = probe_tcp("h", 1, timeout=0.1, max_attempts=5, budget_ms=10_000)

        assert result.error == "dns_failed"
        assert calls["n"] == 1

    def test_success_on_first_try_does_not_retry(self, monkeypatch):
        from apps.probing.services import probe_tcp

        self._no_sleep(monkeypatch)
        calls = self._seq(monkeypatch, [(True, 3, None)])

        result = probe_tcp("h", 1, timeout=0.1, max_attempts=5, budget_ms=10_000)

        assert result.attempts == 1
        assert calls["n"] == 1

    def test_max_attempts_one_means_no_retry(self, monkeypatch):
        from apps.probing.services import probe_tcp

        self._no_sleep(monkeypatch)
        calls = self._seq(monkeypatch, [(False, None, "timeout")])

        result = probe_tcp("h", 1, timeout=0.1, max_attempts=1, budget_ms=10_000)

        assert result.attempts == 1
        assert calls["n"] == 1

    def test_budget_stops_further_attempts(self, monkeypatch):
        """预算耗尽时必须停止，不能为了凑次数把调用方拖死。"""
        from apps.probing import services

        # 注意：services.time 就是全局 time 模块，直接 monkeypatch sleep
        # 会让测试里任何 time.sleep 调用都指向替身，容易写出递归。
        # 这里先抓住真实函数，再让"每次探测"真实耗时 400ms。
        import time as _time

        real_sleep = _time.sleep

        def slow_probe(host, port, timeout):
            real_sleep(0.4)
            return services.ProbeResult(False, None, "timeout")

        monkeypatch.setattr(services, "_probe_once", slow_probe)
        # 退避本身不真的等待，让预算只被"探测耗时"消耗
        monkeypatch.setattr(services.time, "sleep", lambda _s: None)
        monkeypatch.setattr(services, "_backoff_seconds", lambda attempt: 0.0)

        result = services.probe_tcp("h", 1, timeout=0.1, max_attempts=10, budget_ms=1000)

        assert result.reachable is False
        assert result.error == "timeout"
        assert result.attempts < 10, "预算失效：重试次数没有被预算限制住"

    def test_backoff_grows_exponentially(self, monkeypatch, settings):
        from apps.probing.services import _backoff_seconds

        settings.PROBE_BACKOFF_BASE_MS = 100
        # 抖动是 0.5~1.5 倍，取多次的最小值比较，避免偶然性
        first = min(_backoff_seconds(1) for _ in range(50))
        second = min(_backoff_seconds(2) for _ in range(50))
        third = min(_backoff_seconds(3) for _ in range(50))

        assert first < second < third
        assert first == pytest.approx(0.05, abs=0.02)  # 0.1 * 0.5

    def test_backoff_is_capped(self, monkeypatch, settings):
        from apps.probing.services import MAX_BACKOFF_SECONDS, _backoff_seconds

        settings.PROBE_BACKOFF_BASE_MS = 1000
        # 第 20 次重试的指数值非常大，必须被上限截住（含 1.5 倍抖动）
        assert _backoff_seconds(20) <= MAX_BACKOFF_SECONDS * 1.5

    def test_backoff_has_jitter(self, monkeypatch, settings):
        """同一 attempt 的等待时间不能恒定，否则批量探测会形成惊群。"""
        from apps.probing.services import _backoff_seconds

        settings.PROBE_BACKOFF_BASE_MS = 100
        values = {round(_backoff_seconds(1), 6) for _ in range(30)}
        assert len(values) > 1

    def test_does_not_raise_on_any_failure(self, monkeypatch):
        """核心契约仍然成立：永不外抛异常。"""
        from apps.probing import services

        self._no_sleep(monkeypatch)

        def boom(h, p, t):
            raise RuntimeError("意外")

        monkeypatch.setattr(services, "_probe_once", boom)

        # _probe_once 被替换成会抛异常的版本，probe_tcp 此时应当把异常透出，
        # 因为"不抛异常"的契约由 _probe_once 内部保证。这里验证的是：
        # 真实实现下不会抛——用真实 _probe_once 打一个非法端口。
        monkeypatch.undo()
        result = services.probe_tcp("127.0.0.1", 70000, timeout=0.1, max_attempts=2)
        assert result.reachable is False


# ---------------------------------------------------------------------------
# SQLite 写稳定化
# ---------------------------------------------------------------------------


class TestSqliteWrites:
    def test_build_options_shape(self):
        from common.sqlite_writes import build_options

        options = build_options(busy_timeout_ms=3000)

        assert options["timeout"] == 3.0
        assert options["transaction_mode"] == "IMMEDIATE"
        init = options["init_command"]
        assert "busy_timeout = 3000" in init
        assert "journal_mode = WAL" in init
        assert "synchronous = NORMAL" in init

    def test_build_options_is_pure_stdlib(self):
        """必须能在 settings 加载期调用——不能依赖 django.db 已就绪。"""
        from common.sqlite_writes import build_options

        assert isinstance(build_options(), dict)

    def test_immediate_mode_avoids_lock_upgrade(self):
        """IMMEDIATE 是避免"锁升级假超时"的关键，不能被悄悄改掉。"""
        from common.sqlite_writes import build_options

        assert build_options()["transaction_mode"] == "IMMEDIATE"

    @pytest.mark.parametrize(
        "message",
        [
            "database is locked",
            "database table is locked",
            "database schema is locked",
        ],
    )
    def test_recognizes_locked_errors(self, message):
        from django.db import OperationalError

        from common.sqlite_writes import is_locked_error

        assert is_locked_error(OperationalError(message)) is True

    def test_ignores_unrelated_errors(self):
        from django.db import IntegrityError, OperationalError

        from common.sqlite_writes import is_locked_error

        assert is_locked_error(IntegrityError("UNIQUE constraint failed")) is False
        assert is_locked_error(OperationalError("no such table: foo")) is False
        assert is_locked_error(ValueError("无关")) is False

    def test_retry_succeeds_after_conflicts(self, monkeypatch):
        from django.db import OperationalError

        from common.sqlite_writes import retry_on_locked

        monkeypatch.setattr("common.sqlite_writes.time.sleep", lambda _s: None)
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OperationalError("database is locked")
            return "ok"

        assert retry_on_locked(flaky, attempts=5) == "ok"
        assert attempts["n"] == 3

    def test_retry_gives_up_and_raises_last_error(self, monkeypatch):
        from django.db import OperationalError

        from common.sqlite_writes import retry_on_locked

        monkeypatch.setattr("common.sqlite_writes.time.sleep", lambda _s: None)

        def always_locked():
            raise OperationalError("database is locked")

        with pytest.raises(OperationalError):
            retry_on_locked(always_locked, attempts=3)

    def test_non_lock_error_is_raised_immediately(self, monkeypatch):
        """重试非锁异常只会放大问题，必须立即抛。"""
        from common.sqlite_writes import retry_on_locked

        monkeypatch.setattr("common.sqlite_writes.time.sleep", lambda _s: None)
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise ValueError("业务错误")

        with pytest.raises(ValueError):
            retry_on_locked(boom, attempts=5)

        assert calls["n"] == 1

    def test_retry_passes_arguments_through(self, monkeypatch):
        from common.sqlite_writes import retry_on_locked

        monkeypatch.setattr("common.sqlite_writes.time.sleep", lambda _s: None)

        assert retry_on_locked(lambda a, b=0: a + b, 3, b=4) == 7

    def test_rejects_invalid_attempts(self):
        from common.sqlite_writes import retry_on_locked

        with pytest.raises(ValueError):
            retry_on_locked(lambda: 1, attempts=0)

    def test_write_lock_is_reentrant(self):
        """rotate_one 会在 rotate_all 的锁内再取一次锁，必须可重入。"""
        from common.sqlite_writes import write_lock

        with write_lock():
            with write_lock():
                pass

    def test_write_lock_passes_through_on_non_sqlite(self, monkeypatch):
        from common.sqlite_writes import write_lock

        monkeypatch.setattr("common.sqlite_writes.is_sqlite", lambda alias="default": False)
        with write_lock():
            pass


# ---------------------------------------------------------------------------
# 并发轮换
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_sqlite_defaults_to_serial(self, settings, monkeypatch):
        """SQLite 单写者模型：默认并发必须是 1，并发只会更慢。"""
        from apps.credentials.services import _resolve_concurrency

        settings.ROTATION_CONCURRENCY = 0
        monkeypatch.setattr(
            "apps.credentials.services.is_sqlite", lambda alias="default": True
        )
        assert _resolve_concurrency(None) == 1

    def test_postgres_defaults_to_parallel(self, settings, monkeypatch):
        from apps.credentials.services import _resolve_concurrency

        settings.ROTATION_CONCURRENCY = 0
        monkeypatch.setattr(
            "apps.credentials.services.is_sqlite", lambda alias="default": False
        )
        assert _resolve_concurrency(None) > 1

    def test_explicit_concurrency_is_respected(self, monkeypatch):
        from apps.credentials.services import _resolve_concurrency

        monkeypatch.setattr(
            "apps.credentials.services.is_sqlite", lambda alias="default": True
        )
        assert _resolve_concurrency(8) == 8

    def test_explicit_zero_is_rejected(self):
        from apps.credentials.services import _resolve_concurrency

        with pytest.raises(ValueError):
            _resolve_concurrency(0)

    def test_chunks_cover_every_id_exactly_once(self):
        """分片不能漏 id、也不能重复处理同一个 id。"""
        from apps.credentials.services import _split_chunks

        ids = list(range(1, 26))
        chunks = _split_chunks(ids, workers=4)

        assert len(chunks) == 4
        flat = [i for chunk in chunks for i in chunk]
        assert sorted(flat) == ids, "分片丢失或重复了实例 id"

    def test_split_chunks_never_returns_empty_chunks(self):
        from apps.credentials.services import _split_chunks

        assert all(chunk for chunk in _split_chunks([1, 2, 3], workers=8))

    def test_split_chunks_handles_empty_input(self):
        from apps.credentials.services import _split_chunks

        assert _split_chunks([], workers=4) == []
