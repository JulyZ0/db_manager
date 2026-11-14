"""建立三个角色组（只读 / 操作员 / 管理员）。

⚠️ 这里**不绑定权限**，是有意为之。

``Permission`` 记录由 Django 的 ``post_migrate`` 信号创建，而数据迁移在
它之前执行。在迁移里查 ``assets.operate_assets`` 会得到空集：组能建出来，
但一个权限都没绑，而 ``migrate`` 输出全是 OK、不报任何错。最终表现为
「管理员登录后什么都点不动」，且从日志里完全看不出原因。

所以分工是：
- 本迁移：只负责让三个组存在（保证即使迁移后立刻中断，组名也在）。
- ``apps/assets/roles.py::sync_role_groups``（挂 post_migrate）：负责
  把权限绑到组上，并在每次 ``migrate`` 时幂等同步。
"""

from __future__ import annotations

from django.db import migrations

ROLE_NAMES = ["只读", "操作员", "管理员"]


def create_role_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in ROLE_NAMES:
        Group.objects.get_or_create(name=name)


def remove_role_groups(apps, schema_editor):
    """回滚：只删这三个具体名字，不动运维自建的其他组。"""
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=ROLE_NAMES).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0006_rotation_log_fk_set_null"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_role_groups, remove_role_groups),
    ]
