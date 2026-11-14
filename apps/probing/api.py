"""probing 接口：按需触发 TCP 可达性探测。

探测算法本身在 apps/probing/services.py，本文件负责：
- 只按实例 ID 取 host/port，不接受客户端传入任意端点（防内网 SSRF）
- 探测失败仍返回 200（"不可达"是有效业务结论）
- 回写实例的探测结果字段
"""

from __future__ import annotations

from datetime import datetime

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema

from apps.assets.models import Instance
from apps.probing.services import probe_tcp
from common.auth import django_session_auth
from common.exceptions import NotFoundError
from common.permissions import PERM_OPERATE_ASSETS, require_perm

router = Router(tags=["探测"])


class ProbeOut(Schema):
    instance_id: int
    reachable: bool
    latency_ms: int | None = None
    # timeout / refused / dns_failed / network_unreachable / unknown
    error: str | None = None
    probed_at: datetime
    # 探测后实例的最新状态，便于前端直接刷新列表
    status: str


@router.post(
    "/{instance_id}/probe",
    response=ProbeOut,
    summary="探测实例 TCP 端口是否可达",
    auth=django_session_auth,
)
def probe_instance(request: HttpRequest, instance_id: int):
    require_perm(request.user, PERM_OPERATE_ASSETS, what="探测实例")
    instance = Instance.objects.filter(id=instance_id).first()
    if instance is None:
        raise NotFoundError(f"实例 {instance_id} 不存在")

    result = probe_tcp(instance.host, instance.port)

    probed_at = timezone.now()
    instance.last_probed_at = probed_at
    instance.last_probe_ok = result.reachable
    instance.last_probe_latency_ms = result.latency_ms
    instance.status = (
        Instance.Status.ONLINE if result.reachable else Instance.Status.OFFLINE
    )
    instance.save(
        update_fields=[
            "last_probed_at",
            "last_probe_ok",
            "last_probe_latency_ms",
            "status",
            "updated_at",
        ]
    )

    return ProbeOut(
        instance_id=instance.id,
        reachable=result.reachable,
        latency_ms=result.latency_ms,
        error=result.error,
        probed_at=probed_at,
        status=instance.status,
    )
