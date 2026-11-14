"""管理命令：按保留期清理运行日志。

用法：
    python manage.py cleanup_expired_logs --dry-run   # 只看会删多少条
    python manage.py cleanup_expired_logs             # 真删
    python manage.py cleanup_expired_logs --days-slow-log 30

为什么不给每一项都做参数：日常只需调保留天数（已通过 .env 配置），
命令行参数只为临时覆盖和预演服务。
"""

from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "按保留期清理慢请求记录、语句审计与密码轮换历史（默认 90/180/365 天）"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="只统计过期条数，不实际删除",
        )

    def handle(self, *args, **options) -> None:
        from apps.metrics.retention import cleanup_expired_logs

        dry_run = bool(options.get("dry_run"))
        result = cleanup_expired_logs(dry_run=dry_run)

        for table, info in result["tables"].items():
            days = info.get("days", 0)
            if days == 0:
                self.stdout.write(f"  {table}: 永久保留，跳过")
                continue
            if info.get("error"):
                self.stdout.write(self.style.WARNING(f"  {table}: 清理失败，已跳过"))
                continue
            verb = "将删除" if dry_run else "已删除"
            self.stdout.write(
                f"  {table}: {verb} {info.get('deleted', 0)} 条（保留 {days} 天）"
            )

        total = result["total_deleted"]
        action = "待删除" if dry_run else "已删除"
        self.stdout.write(
            self.style.SUCCESS(
                f"完成：{action}合计 {total} 条，耗时 {result['duration_ms']}ms"
            )
        )
