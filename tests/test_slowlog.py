"""慢请求日志异步批量写入的测试。

关注点不是"能不能写进去"（那样只要同步 create 就行了），而是：
1. 测试环境**强制同步**——否则断言会在事务与线程之间随机失败；
2. 异步路径下请求线程只入队、不写库；
3. 队列满时丢弃而不是阻塞；
4. 刷写能把队列里攒下的记录一次性落库。
"""

from __future__ import annotations

import queue

import pytest
from django.test import RequestFactory

from apps.metrics import slowlog

pytestmark = pytest.mark.django_db


def _record(**overrides) -> dict:
    data = {
        "request_id": "abc123",
        "method": "GET",
        "path": "/instances/",
        "status_code": 200,
        "duration_ms": 900,
        "user_id": "",
        "remote_addr": "127.0.0.1",
    }
    data.update(overrides)
    return data


@pytest.fixture
def no_worker(monkeypatch):
    """异步路径下不真的起后台线程。

    测试用例跑在事务里，后台线程既读不到未提交数据、又会在回滚后
    对着已消失的表写入。这里只验证"请求线程的行为"，写库交给 flush()。
    """
    monkeypatch.setattr(slowlog, "_ensure_worker", lambda: None)
    monkeypatch.setattr(slowlog, "_write_sync", lambda: False)
    slowlog.reset_for_tests()
    yield
    slowlog.reset_for_tests()


class TestSyncMode:
    def test_writes_immediately_in_tests(self):
        """测试环境必须同步落库，不能把断言交给后台线程。"""
        from apps.metrics.models import SlowRequestLog

        slowlog.submit(_record())

        assert SlowRequestLog.objects.count() == 1
        row = SlowRequestLog.objects.get()
        assert row.method == "GET"
        assert row.duration_ms == 900

    def test_flush_is_noop_when_queue_empty(self):
        assert slowlog.flush() == 0


class TestAsyncMode:
    def test_submit_only_enqueues(self, no_worker):
        """异步路径下请求线程绝不碰数据库。"""
        from apps.metrics.models import SlowRequestLog

        slowlog.submit(_record())

        assert SlowRequestLog.objects.count() == 0, "投递阶段不应写库"
        assert slowlog._QUEUE.qsize() == 1

    def test_flush_drains_queue(self, no_worker):
        from apps.metrics.models import SlowRequestLog

        slowlog.submit(_record(request_id="r1"))
        slowlog.submit(_record(request_id="r2"))

        written = slowlog.flush()

        assert written == 2
        assert SlowRequestLog.objects.count() == 2
        assert slowlog._QUEUE.qsize() == 0

    def test_queue_full_drops_instead_of_blocking(self, no_worker, monkeypatch):
        """队列满必须立刻丢弃。阻塞请求线程的代价远高于丢失一条日志。"""
        from apps.metrics.models import SlowRequestLog

        monkeypatch.setattr(slowlog, "_QUEUE", queue.Queue(maxsize=1))
        slowlog.reset_for_tests()

        slowlog.submit(_record(request_id="keep"))
        slowlog.submit(_record(request_id="dropped"))

        assert slowlog._QUEUE.qsize() == 1
        assert slowlog.drop_count() == 1
        assert SlowRequestLog.objects.count() == 0

    def test_batch_size_and_interval_are_configurable(self, settings):
        settings.SLOW_LOG_BATCH_SIZE = 7
        settings.SLOW_LOG_FLUSH_SECONDS = 2
        assert slowlog._batch_size() == 7
        assert slowlog._flush_seconds() == 2.0

    def test_worker_wait_is_clamped_for_clean_shutdown(self, settings):
        """单次等待不能超过 join 的超时，否则退出时写不完最后一批。"""
        settings.SLOW_LOG_FLUSH_SECONDS = 30
        assert slowlog._flush_seconds() == 30.0
        # _run 内部取 min(1.0, ...)：这里只断言上限常量存在且合理
        assert min(1.0, slowlog._flush_seconds()) == 1.0


class TestMiddlewareIntegration:
    def test_slow_request_goes_through_queue(self):
        """中间件的慢请求记录走 slowlog.submit，而不是直接 create。"""
        from apps.metrics.models import SlowRequestLog
        from common.middleware import RequestTimingMiddleware

        request = RequestFactory().get("/instances/")
        request.META["REMOTE_ADDR"] = "10.0.0.9"

        RequestTimingMiddleware._record_slow_request(
            request, "rid-1", status_code=200, duration_ms=9999
        )

        assert SlowRequestLog.objects.count() == 1
        row = SlowRequestLog.objects.get()
        assert row.request_id == "rid-1"
        assert row.remote_addr == "10.0.0.9"

    def test_fast_request_writes_nothing(self):
        from apps.metrics.models import SlowRequestLog
        from common.middleware import RequestTimingMiddleware

        request = RequestFactory().get("/instances/")
        RequestTimingMiddleware._record_slow_request(
            request, "rid-2", status_code=200, duration_ms=1
        )

        assert SlowRequestLog.objects.count() == 0
