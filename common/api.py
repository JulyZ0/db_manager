"""django-ninja API 实例与全局异常处理。

所有业务路由最终注册到这里的 `api`，由 config/urls.py 挂到 /api/v1/。
"""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse
from ninja import NinjaAPI
from ninja.errors import HttpError, ValidationError

from common.exceptions import BusinessError

logger = logging.getLogger(__name__)

api = NinjaAPI(
    title="数据库实例管理系统",
    version="1.0.0",
    description=(
        "部门 / 集群 / 实例资产管理，TCP 可达性探测，"
        "凭据加密轮换，每日维度统计。"
    ),
    urls_namespace="api-v1",
    docs_url="/docs",
    openapi_url="/openapi.json",
)


def _error_body(
    code: str, message: str, detail: dict | None = None
) -> dict:
    body = {"code": code, "message": message}
    if detail:
        body["detail"] = detail
    return body


@api.exception_handler(BusinessError)
def _handle_business_error(
    request: HttpRequest, exc: BusinessError
) -> HttpResponse:
    """业务异常 → 统一错误体。"""
    payload = _error_body(exc.code, exc.message, exc.detail)
    return api.create_response(request, payload, status=exc.status_code)


@api.exception_handler(ValidationError)
def _handle_validation_error(
    request: HttpRequest, exc: ValidationError
) -> HttpResponse:
    """Pydantic 校验失败 → 422 + 统一错误体。"""
    payload = _error_body(
        "VALIDATION_ERROR", "请求参数校验失败", {"errors": exc.errors}
    )
    return api.create_response(request, payload, status=422)


@api.exception_handler(HttpError)
def _handle_http_error(
    request: HttpRequest, exc: HttpError
) -> HttpResponse:
    """ninja 内置 HttpError（如 404）→ 统一错误体。"""
    code = {404: "NOT_FOUND", 409: "CONFLICT"}.get(exc.status_code, "ERROR")
    payload = _error_body(code, str(exc))
    return api.create_response(request, payload, status=exc.status_code)


@api.exception_handler(Exception)
def _handle_unexpected_error(
    request: HttpRequest, exc: Exception
) -> HttpResponse:
    """兜底：不向调用方暴露堆栈，只返回 request_id 便于对日志。"""
    request_id = getattr(request, "request_id", "-")
    logger.exception("未捕获异常 request_id=%s path=%s", request_id, request.path)
    payload = _error_body(
        "INTERNAL_ERROR",
        "服务器内部错误",
        {"request_id": request_id},
    )
    return api.create_response(request, payload, status=500)


# --- 路由注册 -----------------------------------------------------------
# 各 app 的 router 在此汇总，避免 config/urls.py 感知业务细节。

from apps.assets.api import (  # noqa: E402
    clusters_router,
    departments_router,
    instances_router,
)
from apps.credentials.api import router as credentials_router  # noqa: E402
from apps.dbconsole.api import router as dbconsole_router  # noqa: E402
from apps.metrics.api import router as metrics_router  # noqa: E402
from apps.probing.api import router as probing_router  # noqa: E402

api.add_router("/departments", departments_router)
api.add_router("/clusters", clusters_router)
api.add_router("/instances", instances_router)
api.add_router("/instances", probing_router)
api.add_router("/instances", credentials_router)
api.add_router("/instances", dbconsole_router)
api.add_router("/stats", metrics_router)
