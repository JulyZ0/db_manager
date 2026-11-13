"""统计与观测模型：每日快照、慢请求记录。

字段与约束见 PRD §6.2。
"""

from __future__ import annotations

from django.db import models


class DailyInstanceStat(models.Model):
    """每天 00:00 按「部门 × 集群」生成的实例数量快照。

    department_name / cluster_name 保存快照值，避免后续改名或删除导致历史失真。
    """

    stat_date = models.DateField("统计日期")
    department = models.ForeignKey(
        "assets.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="daily_stats",
        verbose_name="部门",
    )
    cluster = models.ForeignKey(
        "assets.Cluster",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="daily_stats",
        verbose_name="集群",
    )
    department_name = models.CharField("部门名称快照", max_length=100)
    cluster_name = models.CharField("集群名称快照", max_length=100)
    instance_count = models.PositiveIntegerField("实例数量", default=0)
    active_count = models.PositiveIntegerField("在线实例数量", default=0)

    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "metrics_daily_instance_stat"
        verbose_name = "每日实例统计"
        verbose_name_plural = "每日实例统计"
        ordering = ["-stat_date", "department_id", "cluster_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["stat_date", "cluster"], name="uniq_daily_stat_date_cluster"
            )
        ]
        indexes = [
            models.Index(fields=["stat_date", "department"], name="idx_stat_date_dept"),
        ]

    def __str__(self) -> str:
        return f"{self.stat_date} {self.cluster_name}={self.instance_count}"


class SlowRequestLog(models.Model):
    """超过 SLOW_REQUEST_MS 的请求记录。正常请求只写日志，不落库。"""

    request_id = models.CharField("请求ID", max_length=32, db_index=True)
    method = models.CharField("方法", max_length=10)
    path = models.CharField("路径", max_length=255)
    status_code = models.PositiveSmallIntegerField("状态码")
    duration_ms = models.PositiveIntegerField("耗时(ms)")
    user_id = models.CharField("用户", max_length=64, blank=True, default="")
    remote_addr = models.GenericIPAddressField("客户端地址", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "metrics_slow_request_log"
        verbose_name = "慢请求记录"
        verbose_name_plural = "慢请求记录"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["-created_at"], name="idx_slow_request_time"),
        ]

    def __str__(self) -> str:
        return f"{self.method} {self.path} {self.duration_ms}ms"
