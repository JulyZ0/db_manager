"""请求耗时统计中间件（已实现）。

行为见 PRD FR-07：
1. 每个请求生成 request_id，注入 request.request_id 与响应头 X-Request-ID。
2. 结构化日志记录 method / path / status / duration_ms。
3. 超过 SLOW_REQUEST_MS 的请求额外写一条 SlowRequestLog。

设计取舍：
- 正常请求只写日志不落库，避免每个请求一次 INSERT 拖垮数据库。
- 埋点放在 try/except/else 里，保证视图抛异常时也能记录耗时与 5xx。
- 落库失败不能影响业务响应，因此单独兜住异常。
"""

from __future__ import annotations

import logging
import re
import uuid

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from common.timing import Stopwatch

logger = logging.getLogger(__name__)

# 不参与耗时统计的路径前缀（正则）
SKIP_PATHS = re.compile(
    r"^/(healthz|static/|media/|api/v1/docs|api/v1/openapi\.json)"
)


class RequestTimingMiddleware:
    """统计每个请求耗时。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = uuid.uuid4().hex[:12]
        request.request_id = request_id
        request.META["HTTP_X_REQUEST_ID"] = request_id

        # 被排除的路径仍注入 request_id，便于全链路排障，但不计时不落库
        if SKIP_PATHS.match(request.path):
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            return response

        watch = Stopwatch(f"http:{request.method} {request.path}")

        try:
            response = self.get_response(request)
        except Exception:
            duration_ms = watch.stop()
            logger.warning(
                "%s %s -> 500 in %dms request_id=%s (视图抛出异常)",
                request.method,
                request.path,
                duration_ms,
                request_id,
            )
            self._record_slow_request(request, request_id, 500, duration_ms)
            raise

        duration_ms = watch.stop()
        response["X-Request-ID"] = request_id

        logger.info(
            "%s %s -> %d in %dms request_id=%s",
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        self._record_slow_request(request, request_id, response.status_code, duration_ms)
        return response

    # -- 内部 ------------------------------------------------------------

    @staticmethod
    def _record_slow_request(
        request: HttpRequest, request_id: str, status_code: int, duration_ms: int
    ) -> None:
        threshold = getattr(settings, "SLOW_REQUEST_MS", 500)
        if duration_ms <= threshold:
            return

        user = getattr(request, "user", None)
        user_id = str(user.pk) if (user is not None and user.is_authenticated) else ""

        try:
            from apps.metrics.models import SlowRequestLog

            SlowRequestLog.objects.create(
                request_id=request_id,
                method=request.method,
                path=request.path[:255],
                status_code=status_code,
                duration_ms=duration_ms,
                user_id=user_id,
                remote_addr=request.META.get("REMOTE_ADDR") or None,
            )
        except Exception:  # noqa: BLE001 - 监控写入失败绝不能影响业务响应
            logger.exception("写入慢请求记录失败 request_id=%s", request_id)

        logger.warning(
            "慢请求 %s %s -> %d 耗时 %dms（阈值 %dms）request_id=%s",
            request.method,
            request.path,
            status_code,
            duration_ms,
            threshold,
            request_id,
        )
