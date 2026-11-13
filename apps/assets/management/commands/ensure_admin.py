"""管理命令：确保存在一个可登录后台的管理员账号（幂等）。

用法：
    python manage.py ensure_admin
    python manage.py ensure_admin --username admin --password NewPass@123
    python manage.py ensure_admin --reset-password

设计要点：
    - 默认账号密码用于「开箱即用」，真实部署应通过环境变量 ADMIN_USERNAME /
      ADMIN_PASSWORD 或命令行参数覆盖，并在首次登录后立即改密。
    - 默认**不重置**已存在账号的密码：重复启动不会把运维改过的密码冲掉。
      确实需要重置时显式传 --reset-password。
"""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "Admin@123456"


class Command(BaseCommand):
    help = "确保存在一个可登录后台的管理员账号（重复执行安全）"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--username",
            dest="username",
            default=os.environ.get("ADMIN_USERNAME", DEFAULT_USERNAME),
            help=f"管理员用户名，默认 {DEFAULT_USERNAME}（可用 ADMIN_USERNAME 覆盖）",
        )
        parser.add_argument(
            "--password",
            dest="password",
            default=os.environ.get("ADMIN_PASSWORD", DEFAULT_PASSWORD),
            help="管理员密码（可用 ADMIN_PASSWORD 覆盖）",
        )
        parser.add_argument(
            "--reset-password",
            action="store_true",
            help="账号已存在时也重置密码",
        )

    def handle(self, *args, **options) -> None:
        username: str = options["username"]
        password: str = options["password"]
        reset: bool = options["reset_password"]

        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(
            username=username,
            defaults={"is_staff": True, "is_superuser": True},
        )

        if created:
            user.set_password(password)
            user.is_staff = True
            user.is_superuser = True
            user.save()
            self.stdout.write(
                self.style.SUCCESS(f"已创建管理员账号：{username}")
            )
            return

        # 已存在：只补齐权限，默认不动密码
        changed = []
        if not user.is_staff:
            user.is_staff = True
            changed.append("is_staff")
        if not user.is_superuser:
            user.is_superuser = True
            changed.append("is_superuser")
        if reset:
            user.set_password(password)
            changed.append("password")

        if changed:
            user.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"管理员账号 {username} 已更新：{', '.join(changed)}"
                )
            )
        else:
            self.stdout.write(f"管理员账号 {username} 已存在，无需改动")
