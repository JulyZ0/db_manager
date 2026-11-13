"""统一业务异常。

约定：业务错误一律抛 BusinessError 子类，由 common.api 的异常处理器
转换为 {"code", "message", "detail"} 结构，避免各视图各写一套。
"""

from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """业务异常基类，携带 HTTP 状态码与机器可读的错误码。"""

    status_code: int = 400
    code: str = "BUSINESS_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.detail = detail or {}


class NotFoundError(BusinessError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(BusinessError):
    """名称重复、存在子资源、任务并发冲突等。"""

    status_code = 409
    code = "CONFLICT"


class RotationInProgressError(ConflictError):
    code = "ROTATION_IN_PROGRESS"
