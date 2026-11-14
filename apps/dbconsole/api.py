"""连接控制台接口。

两个端点，安全口径刻意不同：

- ``GET /{id}/verify-credentials``：**只读**。不执行语句、不写字段，
  结论只回给调用方看一眼。与详情页的自动验证共用同一套逻辑。
- ``POST /{id}/execute``：**可写**。能执行任意 DML/DDL，因此要求登录；
  破坏性语句（DROP / TRUNCATE）还必须显式 ``confirm=true``。

为什么不把执行端点做成 GET：它能改数据，用 GET 会被浏览器预取、被爬虫
误触，也会让 CSRF 防护失去意义。
"""

from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Schema

from apps.assets.models import Instance
from apps.dbconsole.services import (
    ERROR_MULTI_STATEMENT,
    check_statement,
    classify_statement,
    engine_of,
    execute_statement,
    is_destructive,
    verify_credentials,
)
from common.auth import django_session_auth
from common.exceptions import ConflictError, NotFoundError, ValidationFailedError
from common.permissions import PERM_ADMIN_CONSOLE, require_perm

router = Router(tags=["连接控制台"])

# 单条语句长度上限。正常 SQL 远小于此值，超长多半是粘贴事故或攻击载荷。
MAX_STATEMENT_LENGTH = 10_000


class VerifyOut(Schema):
    instance_id: int
    engine: str
    ok: bool
    latency_ms: int | None = None
    server_version: str = ""
    # auth_failed / timeout / refused / dns_failed / no_password / ...
    error: str | None = None
    hint: str = ""


class ExecuteIn(Schema):
    sql: str
    # 破坏性语句必须显式确认，避免「看错了库」这类事故
    confirm: bool = False


class ExecuteOut(Schema):
    instance_id: int
    ok: bool
    kind: str
    destructive: bool
    columns: list[str] = []
    rows: list[list[str]] = []
    row_count: int = 0
    affected: int | None = None
    duration_ms: int = 0
    truncated: bool = False
    error: str | None = None
    hint: str = ""


def _get_instance(instance_id: int) -> Instance:
    instance = Instance.objects.select_related("cluster").filter(id=instance_id).first()
    if instance is None:
        raise NotFoundError(f"实例 {instance_id} 不存在")
    return instance


@router.get(
    "/{instance_id}/verify-credentials",
    response=VerifyOut,
    summary="验证实例登记的账号密码能否登录目标库",
)
def verify_instance_credentials(request: HttpRequest, instance_id: int):
    """只读验证：真正连一次目标库确认凭据可用。

    失败仍返回 200 —— "连不上 / 密码错"是有效业务结论，不是服务端错误，
    调用方看 ``ok`` 字段判断。
    """
    instance = _get_instance(instance_id)
    result = verify_credentials(instance)

    return VerifyOut(
        instance_id=instance.id,
        engine=result.engine,
        ok=result.ok,
        latency_ms=result.latency_ms,
        server_version=result.server_version,
        error=result.error,
        hint=result.hint,
    )


@router.post(
    "/{instance_id}/execute",
    response=ExecuteOut,
    summary="在实例上执行一条语句（需登录）",
    auth=django_session_auth,
)
def execute_on_instance(request: HttpRequest, instance_id: int, payload: ExecuteIn):
    """在目标库执行一条语句。

    安全边界：
    1. 必须登录（``django_session_auth``，含 CSRF 校验）。
    2. 一次只允许一条语句，``SELECT 1; DROP TABLE x`` 这类会在建连前被拦。
    3. 破坏性语句需 ``confirm=true``，否则返回 409 让调用方二次确认。
    4. 每次尝试都写审计，审计内容含操作人与语句前 500 字符。
    5. 需要 ``admin_console`` 权限：这等于把目标库凭据借给调用方使用。
    """
    require_perm(request.user, PERM_ADMIN_CONSOLE, what="在控制台执行语句")
    instance = _get_instance(instance_id)

    sql = (payload.sql or "").strip()
    if not sql:
        raise ValidationFailedError("语句不能为空")
    if len(sql) > MAX_STATEMENT_LENGTH:
        raise ValidationFailedError(
            f"语句过长（{len(sql)} 字符），上限 {MAX_STATEMENT_LENGTH}"
        )

    # 多语句在这里就拦掉，不等到 services 层：
    # 「SELECT 1; DROP TABLE t」必须先于任何建连尝试被拒绝，
    # 而且这是调用方输入错误，应当以 422 返回而不是 200。
    if check_statement(sql, engine_of(instance)) == ERROR_MULTI_STATEMENT:
        raise ValidationFailedError("一次只能执行一条语句，请分开提交")

    destructive = is_destructive(sql, engine_of(instance))
    if destructive and not payload.confirm:
        raise ConflictError(
            "该语句会不可逆地删除数据或结构，请确认后重试",
            code="CONFIRM_REQUIRED",
            detail={
                "destructive": True,
                "statement_kind": classify_statement(sql, engine_of(instance)),
            },
        )

    user_label = ""
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        user_label = user.get_username()

    result = execute_statement(instance, sql, user_label=user_label)

    return ExecuteOut(
        instance_id=instance.id,
        ok=result.ok,
        kind=classify_statement(sql),
        destructive=destructive,
        columns=result.columns,
        rows=result.rows,
        row_count=result.row_count,
        affected=result.affected,
        duration_ms=result.duration_ms,
        truncated=result.truncated,
        error=result.error,
        hint=result.hint,
    )
