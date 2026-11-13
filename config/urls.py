"""URL 汇总。

/            —— 运维操作界面（服务端渲染）
/api/v1/...  —— 业务接口（django-ninja）
/api/v1/docs —— Swagger UI
/healthz     —— 探活（不经过耗时中间件统计）
/admin/      —— Django 管理后台
"""

from django.contrib import admin
from django.urls import include, path

from common.api import api
from common.health import healthz

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", api.urls),
    path("healthz", healthz, name="healthz"),
    # 运维界面放在最后：避免它的空路径规则遮蔽其他路由
    path("", include("apps.web.urls")),
]
