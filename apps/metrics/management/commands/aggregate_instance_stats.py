"""管理命令：手动补算某日的实例统计。

用法：
    python manage.py aggregate_instance_stats --date 2026-11-12
    python manage.py aggregate_instance_stats              # 默认当天
"""

from __future__ import annotations

from datetime import date as date_cls

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "按指定日期重算「部门 × 集群」的实例数量统计（幂等）"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--date",
            dest="date",
            default=None,
            help="统计日期 YYYY-MM-DD，缺省为当天",
        )

    def handle(self, *args, **options) -> None:
        raw_date = options.get("date")
        stat_date: date_cls | None = None
        if raw_date:
            try:
                stat_date = date_cls.fromisoformat(raw_date)
            except ValueError as exc:
                raise CommandError(f"日期格式非法：{raw_date}，应为 YYYY-MM-DD") from exc

        from apps.metrics.services import aggregate_for_date

        rows = aggregate_for_date(stat_date)
        self.stdout.write(self.style.SUCCESS(f"统计完成，写入 {rows} 行"))
