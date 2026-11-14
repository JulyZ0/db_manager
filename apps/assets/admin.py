"""assets 应用管理后台注册（便于人工查看台账）。"""

from django.contrib import admin

from apps.assets.models import (
    Cluster,
    Department,
    Instance,
    PasswordRotationLog,
)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "code", "owner", "is_active", "created_at")
    search_fields = ("name", "code", "owner")
    list_filter = ("is_active",)


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "department", "engine", "env", "is_active")
    search_fields = ("name",)
    list_filter = ("engine", "env", "is_active")


@admin.register(Instance)
class InstanceAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "cluster",
        "host",
        "port",
        "role",
        "status",
        "is_active",
    )
    search_fields = ("name", "host")
    list_filter = ("status", "role", "is_active")
    # 密文字段不进入后台表单，避免误编辑
    exclude = ("password_ciphertext",)


@admin.register(PasswordRotationLog)
class PasswordRotationLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "instance_label",
        "source",
        "status",
        "duration_ms",
        "created_at",
    )
    list_filter = ("source", "status")
    readonly_fields = ("created_at",)
    # 实例被删后 FK 置空，名称快照是唯一的定位线索，因此允许按它检索
    search_fields = ("instance_label",)
