"""探活接口。

只检查进程存活与数据库连通性，不触发 Celery。
"""

from __future__ import annotations

from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def healthz(request: HttpRequest) -> JsonResponse:
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - 探活接口不应因异常而 500
        db_ok = False

    payload = {
        "status": "ok" if db_ok else "degraded",
        "database": "up" if db_ok else "down",
        "time": timezone.now().isoformat(),
    }
    return JsonResponse(payload, status=200 if db_ok else 503)
