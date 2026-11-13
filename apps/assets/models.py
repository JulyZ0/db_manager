"""资产台账模型：部门 → 集群 → 实例，以及密码轮换历史。

字段与约束见 PRD §6.2。
加密相关方法（set_password / decrypt_password）留空，由 credentials 应用实现，
避免模型层直接依赖 cryptography。
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from apps.assets.validators import (
    ALIAS_HELP_TEXT,
    ALIAS_MAX_LENGTH,
    normalize_alias,
    validate_alias,
)


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

    class Topology(models.TextChoices):
        """集群架构类型。

        只声明"这一组实例按什么形态部署"，具体的复制方向由
        Instance.replica_of 表达：双主就是两台互指上游。
        """

        STANDALONE = "standalone", "单机"
        MASTER_SLAVE = "master_slave", "主从"
        DUAL_MASTER = "dual_master", "双主"
        SHARDED = "sharded", "分片集群"

    name = models.CharField("集群名称", max_length=100)
    # 集群别名全局唯一：控制台里可以只写一个词就切到某个集群
    alias = models.CharField(
        "集群别名",
        max_length=ALIAS_MAX_LENGTH,
        unique=True,
        null=True,
        blank=True,
        validators=[validate_alias],
        help_text=ALIAS_HELP_TEXT,
    )
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
    topology = models.CharField(
        "集群架构",
        max_length=20,
        choices=Topology.choices,
        default=Topology.STANDALONE,
        help_text=(
            "声明部署形态。架构是集群级决策：它决定实例可以担任哪些角色，"
            "复制关系在集群详情页统一编排"
        ),
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

    def save(self, *args, **kwargs):
        # 空串会互相冲突（NULL 不会），所以"没填别名"必须统一存 NULL
        self.alias = normalize_alias(self.alias)
        super().save(*args, **kwargs)


class Instance(TimeStampedModel):
    """数据库实例：可连接的 host:port 端点。"""

    class Role(models.TextChoices):
        MASTER = "master", "主库"
        SLAVE = "slave", "从库"
        PROXY = "proxy", "代理"

    # 架构决定实例可以担任什么角色。
    # 架构是集群级决策，实例只在这个范围内选自己的定位，不能反过来改变架构。
    # 主从/分片里从库才有意义；双主是两台主库互指，出现"从库"就说明架构填错了。
    ROLES_BY_TOPOLOGY = {
        Cluster.Topology.STANDALONE: (Role.MASTER, Role.PROXY),
        Cluster.Topology.MASTER_SLAVE: (Role.MASTER, Role.SLAVE, Role.PROXY),
        Cluster.Topology.DUAL_MASTER: (Role.MASTER, Role.PROXY),
        Cluster.Topology.SHARDED: (Role.MASTER, Role.SLAVE, Role.PROXY),
    }

    @classmethod
    def roles_for_topology(cls, topology: str) -> tuple[str, ...]:
        """某种架构下允许的角色。未知架构放行全部，避免历史数据卡住。"""
        return cls.ROLES_BY_TOPOLOGY.get(topology, tuple(cls.Role.values))

    class Status(models.TextChoices):
        UNKNOWN = "unknown", "未知"
        ONLINE = "online", "在线"
        OFFLINE = "offline", "离线"

    name = models.CharField("实例名称", max_length=100)
    # 实例别名在**集群内**唯一：不同集群可以有同名实例，
    # 因此控制台里跨集群切换要用「集群别名.实例别名」
    alias = models.CharField(
        "实例别名",
        max_length=ALIAS_MAX_LENGTH,
        null=True,
        blank=True,
        validators=[validate_alias],
        help_text=ALIAS_HELP_TEXT,
    )
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
    # 复制上游：这一台从谁同步。有方向，所以用自引用而不是 M2M。
    # 双主就是两台互指；级联复制 A→B→C 只需要 C.replica_of = B。
    replica_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,  # 主库被删后从库保留，只是失去上游
        related_name="replicas",
        verbose_name="复制上游",
        null=True,
        blank=True,
        help_text="从库填写它所跟随的主库；双主场景两台互相填写",
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

    def save(self, *args, **kwargs):
        # 集群内别名唯一；空串会互相冲突，统一存 NULL
        self.alias = normalize_alias(self.alias)
        super().save(*args, **kwargs)

    def clean(self):
        """校验角色与复制关系是否自洽。

        两条规矩都在模型层：API 与页面两条写入路径都要生效，
        Django 会在 full_clean() 时调用。

        1. 角色必须是集群架构允许的角色——架构是集群级决策，
           实例只能在架构范围内选自己的定位，不能反过来改变架构。
        2. 复制关系必须指向同集群的另一个实例。
        """
        super().clean()

        errors = {}

        if self.cluster_id and self.role:
            allowed = self.roles_for_topology(self.cluster.topology)
            if self.role not in allowed:
                labels = dict(self.Role.choices)
                errors["role"] = (
                    f"集群架构是「{self.cluster.get_topology_display()}」，"
                    f"实例角色只能是：{'、'.join(labels[r] for r in allowed)}"
                )

        if self.replica_of is not None:
            if self.replica_of_id == self.pk:
                errors["replica_of"] = "复制上游不能是自己"
            elif self.cluster_id and self.replica_of.cluster_id != self.cluster_id:
                errors["replica_of"] = (
                    f"复制上游「{self.replica_of.name}」不属于本集群，"
                    "跨集群的复制关系不在本系统登记范围内"
                )

        if errors:
            raise ValidationError(errors)

    @property
    def upstream_name(self) -> str:
        """拓扑图与列表要显示的"上游是谁"，避免模板里到处判空。"""
        return self.replica_of.name if self.replica_of_id else ""

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
            # 集群内别名唯一；不同集群可以同名，所以跨集群切换要写「集群.实例」
            models.UniqueConstraint(
                fields=["cluster", "alias"],
                name="uniq_instance_alias_per_cluster",
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
