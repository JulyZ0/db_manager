"""资产台账模型：部门 → 集群 → 实例，以及密码轮换历史。

字段与约束见 PRD §6.2。
加密相关方法（set_password / decrypt_password）留空，由 credentials 应用实现，
避免模型层直接依赖 cryptography。
"""

from __future__ import annotations

from django.db import models


class TimeStampedModel(models.Model):
    """公共时间戳基类。"""

    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class Department(TimeStampedModel):
    """部门：资源归属的最上层维度。"""

    name = models.CharField("部门名称", max_length=100, unique=True)
    code = models.CharField(
        "部门编码", max_length=50, unique=True, null=True, blank=True
    )
    owner = models.CharField("负责人", max_length=64, blank=True, default="")
    description = models.TextField("描述", blank=True, default="")
    is_active = models.BooleanField("是否启用", default=True)

    class Meta:
        db_table = "assets_department"
        verbose_name = "部门"
        verbose_name_plural = "部门"
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name


class Cluster(TimeStampedModel):
    """集群：一组同构实例的逻辑集合，归属唯一部门。"""

    class Engine(models.TextChoices):
        MYSQL = "mysql", "MySQL"
        POSTGRESQL = "postgresql", "PostgreSQL"
        REDIS = "redis", "Redis"
        MONGODB = "mongodb", "MongoDB"
        OTHER = "other", "其他"

    class Env(models.TextChoices):
        DEV = "dev", "开发"
        TEST = "test", "测试"
        PROD = "prod", "生产"

    name = models.CharField("集群名称", max_length=100)
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="clusters",
        verbose_name="所属部门",
    )
    engine = models.CharField(
        "数据库引擎", max_length=20, choices=Engine.choices, default=Engine.MYSQL
    )
    env = models.CharField(
        "环境", max_length=10, choices=Env.choices, default=Env.DEV
    )
    description = models.TextField("描述", blank=True, default="")
    is_active = models.BooleanField("是否启用", default=True)

    class Meta:
        db_table = "assets_cluster"
        verbose_name = "集群"
        verbose_name_plural = "集群"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["department", "name"], name="uniq_cluster_name_per_dept"
            )
        ]
        indexes = [
            models.Index(fields=["department", "env"], name="idx_cluster_dept_env"),
        ]

    def __str__(self) -> str:
        return f"{self.department.name}/{self.name}"


class Instance(TimeStampedModel):
    """数据库实例：可连接的 host:port 端点。"""

    class Role(models.TextChoices):
        MASTER = "master", "主库"
        SLAVE = "slave", "从库"
        PROXY = "proxy", "代理"

    class Status(models.TextChoices):
        UNKNOWN = "unknown", "未知"
        ONLINE = "online", "在线"
        OFFLINE = "offline", "离线"

    name = models.CharField("实例名称", max_length=100)
    cluster = models.ForeignKey(
        Cluster,
        on_delete=models.PROTECT,
        related_name="instances",
        verbose_name="所属集群",
    )
    host = models.CharField("主机地址", max_length=255)
    port = models.PositiveSmallIntegerField("端口")
    role = models.CharField(
        "角色", max_length=10, choices=Role.choices, default=Role.MASTER
    )
    username = models.CharField("登录账号", max_length=64)

    # 敏感字段：Fernet 密文，形如 v1:<base64>
    password_ciphertext = models.TextField("密码密文", blank=True, default="")
    password_version = models.PositiveIntegerField("密码版本", default=0)
    password_updated_at = models.DateTimeField("密码更新时间", null=True, blank=True)

    status = models.CharField(
        "状态", max_length=10, choices=Status.choices, default=Status.UNKNOWN
    )
    last_probed_at = models.DateTimeField("最近探测时间", null=True, blank=True)
    last_probe_ok = models.BooleanField("最近探测是否可达", null=True, blank=True)
    last_probe_latency_ms = models.PositiveIntegerField(
        "最近探测耗时(ms)", null=True, blank=True
    )

    remark = models.TextField("备注", blank=True, default="")
    is_active = models.BooleanField("是否启用", default=True)

    @property
    def password_set(self) -> bool:
        """是否已设置密码。

        只暴露"有没有"，不暴露内容本身：模板与 API 都依赖这个判断。
        """
        return bool(self.password_ciphertext)

    class Meta:
        db_table = "assets_instance"
        verbose_name = "实例"
        verbose_name_plural = "实例"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["cluster", "host", "port"],
                name="uniq_instance_endpoint_per_cluster",
            ),
            models.CheckConstraint(
                condition=models.Q(port__gte=1) & models.Q(port__lte=65535),
                name="chk_instance_port_range",
            ),
        ]
        indexes = [
            models.Index(fields=["cluster", "is_active"], name="idx_instance_cluster_active"),
            models.Index(fields=["host"], name="idx_instance_host"),
            models.Index(fields=["status"], name="idx_instance_status"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.host}:{self.port})"


class PasswordRotationLog(models.Model):
    """密码轮换审计。只存哈希指纹，绝不存明文或密文。"""

    class Source(models.TextChoices):
        SYSTEM = "system", "定时任务"
        MANUAL = "manual", "手动触发"

    class Status(models.TextChoices):
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"

    instance = models.ForeignKey(
        Instance,
        on_delete=models.CASCADE,
        related_name="rotation_logs",
        verbose_name="实例",
    )
    old_fingerprint = models.CharField("旧密码指纹", max_length=32, blank=True, default="")
    new_fingerprint = models.CharField("新密码指纹", max_length=32, blank=True, default="")
    source = models.CharField(
        "触发来源", max_length=10, choices=Source.choices, default=Source.SYSTEM
    )
    status = models.CharField(
        "结果", max_length=10, choices=Status.choices, default=Status.SUCCESS
    )
    error_message = models.TextField("失败原因", blank=True, default="")
    duration_ms = models.PositiveIntegerField("耗时(ms)", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "assets_password_rotation_log"
        verbose_name = "密码轮换记录"
        verbose_name_plural = "密码轮换记录"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["instance", "-created_at"], name="idx_rotation_instance_time"),
        ]

    def __str__(self) -> str:
        return f"{self.instance_id}@{self.created_at:%Y-%m-%d %H:%M} {self.status}"
