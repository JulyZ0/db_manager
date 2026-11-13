"""控制台审计模型。

只记录"谁在什么实例上执行了哪类语句、结果如何"，不记录结果集本身——
结果集可能包含敏感业务数据，落库等于二次泄露。
"""

from __future__ import annotations

from django.db import models


class StatementAuditLog(models.Model):
    """一条语句执行的审计记录。

    设计取舍：
    - 记录 `statement_preview` 而非全文：全文可能很长且含字面量密码，
      截断到 500 字符既能定位问题，又不至于把敏感字面量长期留存。
    - 只记语句类型与耗时，不记返回行内容。
    - `user_label` 存用户名快照，账号被删后审计依然可读。
    """

    class Kind(models.TextChoices):
        QUERY = "query", "查询"
        WRITE = "write", "写入"
        DDL = "ddl", "结构变更"
        OTHER = "other", "其他"

    instance = models.ForeignKey(
        "assets.Instance",
        on_delete=models.CASCADE,
        related_name="statement_logs",
        verbose_name="实例",
    )
    user_label = models.CharField("操作人", max_length=64, blank=True, default="")
    engine = models.CharField("引擎", max_length=20)
    kind = models.CharField("语句类型", max_length=10, choices=Kind.choices)
    statement_preview = models.CharField("语句预览", max_length=500)
    ok = models.BooleanField("是否成功")
    row_count = models.PositiveIntegerField("返回行数", default=0)
    affected = models.IntegerField("影响行数", null=True, blank=True)
    duration_ms = models.PositiveIntegerField("耗时(ms)", default=0)
    error_code = models.CharField("错误码", max_length=32, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "dbconsole_statement_audit_log"
        verbose_name = "语句审计"
        verbose_name_plural = "语句审计"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["-created_at"], name="idx_stmt_audit_time"),
            models.Index(fields=["instance", "-created_at"], name="idx_stmt_audit_inst"),
        ]

    def __str__(self) -> str:
        return f"{self.engine} {self.kind} ok={self.ok}"
