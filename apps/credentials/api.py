"""credentials 接口：手动轮换与轮换历史查询。

加解密与轮换算法在 crypto.py / services.py，本文件负责
实例存在性校验、停用校验与历史分页。
"""

from __future__ import annotations

from datetime import datetime

from django.http import HttpRequest
from ninja import Query, Router, Schema

from apps.assets.models import Instance, PasswordRotationLog
from apps.assets.schemas import PagedRotationLogOut, RotationLogOut
from apps.credentials.services import rotate_one
from common.auth import django_session_auth
from common.exceptions import ConflictError, NotFoundError
from common.pagination import paginate_queryset
from common.permissions import PERM_OPERATE_ASSETS, require_perm

router = Router(tags=["凭据"])


class RotateOut(Schema):
    instance_id: int
    status: str
    password_version: int
    password_updated_at: datetime | None
    new_fingerprint: str


@router.post(
    "/{instance_id}/rotate-password",
    response=RotateOut,
    summary="手动触发一次密码轮换",
    auth=django_session_auth,
)
def rotate_instance_password(request: HttpRequest, instance_id: int):
    require_perm(request.user, PERM_OPERATE_ASSETS, what="轮换实例密码")
    instance = Instance.objects.filter(id=instance_id).first()
    if instance is None:
        raise NotFoundError(f"实例 {instance_id} 不存在")
    if not instance.is_active:
        raise ConflictError(f"实例「{instance.name}」已停用，不能轮换密码")

    log = rotate_one(instance, source="manual")
    instance.refresh_from_db()

    return RotateOut(
        instance_id=instance.id,
        status=log.status,
        password_version=instance.password_version,
        password_updated_at=instance.password_updated_at,
        new_fingerprint=log.new_fingerprint,
    )


@router.get(
    "/{instance_id}/rotations",
    response=PagedRotationLogOut,
    summary="查询实例的密码轮换历史",
)
def list_rotations(
    request: HttpRequest,
    instance_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if not Instance.objects.filter(id=instance_id).exists():
        raise NotFoundError(f"实例 {instance_id} 不存在")

    qs = PasswordRotationLog.objects.filter(instance_id=instance_id).order_by(
        "-created_at", "-id"
    )
    page_data = paginate_queryset(qs, page, page_size)
    return PagedRotationLogOut(
        count=page_data["count"],
        page=page_data["page"],
        page_size=page_data["page_size"],
        results=[
            RotationLogOut(
                id=o.id,
                source=o.source,
                status=o.status,
                old_fingerprint=o.old_fingerprint,
                new_fingerprint=o.new_fingerprint,
                error_message=o.error_message,
                duration_ms=o.duration_ms,
                created_at=o.created_at,
            )
            for o in page_data["results"]
        ],
    )
