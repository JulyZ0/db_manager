"""monitor：目标库的指标采集与观测。

为什么是「长表」而不是宽表
--------------------------
监控指标随引擎差异极大：MySQL 有 InnoDB 缓冲池命中率，Redis 有内存碎片率，
PG 有事务提交/回滚比。宽表意味着每接一个引擎就 ALTER TABLE 加一批列，
而多数列对其它引擎恒为 NULL——既浪费空间，也让"某个指标从哪天才开始有值"
变得不可知。

这里采用 ``(instance, metric, dimension) -> value`` 的长表：
新增指标只是多一个 ``metric`` 字符串，不改表结构。代价是单实例取多个指标
要一次查出多行再由查询层按 key 归组，这个开销远小于宽表的维护成本。

为什么用 CASCADE 而不是 SET_NULL
--------------------------------
``apps/dbconsole`` 的审计表改成 SET_NULL，因为审计是**留痕**，删实例本身
最该被记录。指标不同：它是**观测数据**，离开实例没有任何意义，保留只会
让趋势图里出现无法归属的孤儿行，并持续占用保留期内的存储。因此这里
跟随实例级联删除，与审计的取舍方向相反——两者目的不同。
"""

from __future__ import annotations

from django.db import models

__all__ = [
    "MetricSample",
    "CollectionRun",
    "AlertRule",
    "AlertEvent",
    "SqlDigest",
    "LockWait",
    "DeadlockEvent",
]


class MetricSample(models.Model):
    """一条指标采样值。

    粒度是「某个实例的某个指标在某一时刻的值」。同一时刻同一指标可能有
    多条（由 ``dimension`` 区分），例如按库统计的空间占用：一个实例下有
    多个 database，每个库一行。
    """

    instance = models.ForeignKey(
        "assets.Instance",
        on_delete=models.CASCADE,
        related_name="metric_samples",
        verbose_name="实例",
    )
    # 指标键，形如 mysql.connections / redis.memory_used。
    # 带引擎前缀是为了让不同引擎的同名含义（如 connections）不会互相混淆，
    # 聚合查询时也能直接用前缀筛出某个引擎的全部指标。
    metric = models.CharField("指标键", max_length=64)
    # 子维度：库名、表名、Redis key 等。空串表示该指标没有子维度。
    #
    # 用 CharField 而不是 JSON：这里只需要"一个可筛选的标签"，JSON 在
    # SQLite 上既不能有效索引也不能高效分组，而按库/表聚合是空间页的核心查询。
    dimension = models.CharField(
        "子维度", max_length=200, blank=True, default=""
    )
    value = models.FloatField("采样值")
    # 采样时刻。与 TimeStampedModel 的 created_at 不同：这是**数据发生的时刻**，
    # 补采历史数据或回放时两者会不一致，因此单独成字段。
    collected_at = models.DateTimeField("采样时刻")

    class Meta:
        db_table = "monitor_metric_sample"
        verbose_name = "指标采样"
        verbose_name_plural = "指标采样"
        # 趋势查询几乎总是「某实例某指标按时间升序」，顺着这个组合索引
        # 可以直接顺序读出，不需要额外排序。
        indexes = [
            models.Index(
                fields=["instance", "metric", "collected_at"],
                name="idx_sample_inst_key_time",
            ),
            # 保留期清理按 collected_at 批量删除，需要单独的时间索引，
            # 否则每次清理都是全表扫描。
            models.Index(fields=["collected_at"], name="idx_sample_time"),
            # 大盘按指标跨实例聚合（"全站 QPS"），需要按 metric 筛选。
            models.Index(fields=["metric", "collected_at"], name="idx_sample_key_time"),
        ]

    def __str__(self) -> str:
        return f"{self.instance_id}:{self.metric}={self.value}"


class CollectionRun(models.Model):
    """一次采集尝试的结果。

    为什么单独记录而不用日志
    ------------------------
    采集失败在生产里是常态（实例重启、网络抖动、权限变更）。运维需要回答
    的是"这个实例的指标为什么断了"，而不是翻几千行应用日志。把每次采集的
    成功与否、错误分类、耗时落库后，页面可以直接展示"最近一次采集：
    5 分钟前，认证失败"，并在连续失败时触发告警。

    与 ``apps/probing`` 的错误码保持同一套语义（timeout / refused /
    dns_failed / auth_failed / ...），避免同一个故障在不同页面有两种说法。
    """

    instance = models.ForeignKey(
        "assets.Instance",
        on_delete=models.CASCADE,
        related_name="collection_runs",
        verbose_name="实例",
    )
    collected_at = models.DateTimeField("采集时刻")
    ok = models.BooleanField("是否成功")
    # 失败分类码；成功时为空串。
    error_code = models.CharField("错误分类", max_length=32, blank=True, default="")
    # 给人看的说明（已脱敏，不含密码）。截断由写入方负责。
    message = models.CharField("说明", max_length=500, blank=True, default="")
    duration_ms = models.PositiveIntegerField("耗时(ms)", default=0)
    metrics_count = models.PositiveIntegerField("写入指标数", default=0)

    class Meta:
        db_table = "monitor_collection_run"
        verbose_name = "采集记录"
        verbose_name_plural = "采集记录"
        ordering = ["-collected_at", "-id"]
        indexes = [
            # 页面/告警只关心「最近一次」，倒序索引让 LIMIT 1 直接命中。
            models.Index(
                fields=["instance", "-collected_at"], name="idx_collect_inst_time"
            ),
            # 保留期清理
            models.Index(fields=["collected_at"], name="idx_collect_time"),
        ]

    def __str__(self) -> str:
        state = "ok" if self.ok else (self.error_code or "failed")
        return f"{self.instance_id}@{self.collected_at}:{state}"


class AlertRule(models.Model):
    """阈值告警规则。

    作用域用**可选外键**表达：``cluster`` 为空表示"全部集群"。
    没有用"多态 + JSON 条件"那种通用表达式引擎，理由很实际：
    运维需要的是"连接使用率 > 80% 持续 5 分钟就叫我"，不是可以写任意
    布尔表达式的 DSL。少一层抽象，配错的概率就低一层。

    ``duration_minutes`` 是防抖的核心：单点毛刺（一次采样恰好越过阈值）
    不该半夜叫醒人，只有**持续**越界才算事件。
    """

    class Operator(models.TextChoices):
        GT = "gt", "大于"
        LT = "lt", "小于"
        GE = "ge", "大于等于"
        LE = "le", "小于等于"

    class Severity(models.TextChoices):
        WARNING = "warning", "警告"
        CRITICAL = "critical", "严重"

    name = models.CharField("规则名", max_length=100, unique=True)
    metric = models.CharField("指标键", max_length=64)
    operator = models.CharField(
        "比较方式", max_length=8, choices=Operator.choices, default=Operator.GT
    )
    threshold = models.FloatField("阈值")
    duration_minutes = models.PositiveIntegerField(
        "持续时长(分钟)", default=0, help_text="0 表示一旦越界立即触发"
    )
    severity = models.CharField(
        "级别", max_length=16, choices=Severity.choices, default=Severity.WARNING
    )
    cluster = models.ForeignKey(
        "assets.Cluster",
        verbose_name="限定集群",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="alert_rules",
        help_text="留空表示对所有集群生效",
    )
    cooldown_minutes = models.PositiveIntegerField(
        "恢复后冷却(分钟)", default=10, help_text="防止抖动导致反复告警"
    )
    enabled = models.BooleanField("启用", default=True)
    remark = models.CharField("备注", max_length=200, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "monitor_alert_rule"
        verbose_name = "告警规则"
        verbose_name_plural = "告警规则"
        ordering = ["name"]
        indexes = [
            # 评估任务只扫启用的规则
            models.Index(fields=["enabled"], name="idx_rule_enabled"),
        ]

    def __str__(self) -> str:
        return f"{self.name}({self.metric} {self.get_operator_display()} {self.threshold})"

    def matches(self, value: float) -> bool:
        """判断一个采样值是否越过阈值。

        用字典映射而不是 if/elif 链：新增比较方式只需加一项，
        不会漏掉某个分支（漏分支的症状是"某些规则永远不触发"，很难查）。
        """
        comparators = {
            self.Operator.GT: lambda a, b: a > b,
            self.Operator.LT: lambda a, b: a < b,
            self.Operator.GE: lambda a, b: a >= b,
            self.Operator.LE: lambda a, b: a <= b,
        }
        compare = comparators.get(self.operator)
        return bool(compare(value, self.threshold)) if compare else False


class AlertEvent(models.Model):
    """一次告警的生命周期：触发 → 持续更新 → 恢复。

    ``rule`` 用 SET_NULL 而不是 CASCADE：删掉规则不应该抹掉历史告警，
    否则"上个月那次连接打满是谁配的规则"就无从追溯。规则名/阈值/级别
    一并快照，规则改了或删了之后，历史事件仍然自解释。

    ``instance`` 仍用 CASCADE（与指标一致）：事件脱离实例没有意义。
    """

    class Status(models.TextChoices):
        FIRING = "firing", "触发中"
        RESOLVED = "resolved", "已恢复"

    rule = models.ForeignKey(
        AlertRule,
        verbose_name="规则",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="events",
    )
    rule_name = models.CharField("规则名快照", max_length=100)
    instance = models.ForeignKey(
        "assets.Instance",
        verbose_name="实例",
        on_delete=models.CASCADE,
        related_name="alert_events",
    )
    instance_label = models.CharField("实例快照", max_length=200, default="")
    metric = models.CharField("指标键", max_length=64)
    operator = models.CharField("比较方式", max_length=8, default="gt")
    threshold = models.FloatField("阈值快照", default=0)
    severity = models.CharField("级别", max_length=16, default="warning")
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.FIRING
    )
    trigger_value = models.FloatField("触发时的值", default=0)
    latest_value = models.FloatField("最近一次的值", default=0)
    started_at = models.DateTimeField("开始时间")
    last_seen_at = models.DateTimeField("最近一次命中")
    resolved_at = models.DateTimeField("恢复时间", null=True, blank=True)
    message = models.CharField("说明", max_length=500, blank=True, default="")

    class Meta:
        db_table = "monitor_alert_event"
        verbose_name = "告警事件"
        verbose_name_plural = "告警事件"
        # 触发中的排前面，同状态内按最近命中倒序：运维最关心"现在还在响的"
        ordering = ["status", "-last_seen_at", "-id"]
        indexes = [
            models.Index(fields=["status", "-started_at"], name="idx_alert_status_time"),
            models.Index(fields=["instance", "-started_at"], name="idx_alert_inst_time"),
        ]

    def __str__(self) -> str:
        return f"{self.rule_name}@{self.instance_label}:{self.status}"


class SqlDigest(models.Model):
    """一个实例上的一种 SQL 模板（归一化后的聚合画像）。

    为什么按 ``(instance, digest)`` 唯一
    ------------------------------------
    ``SELECT * FROM t WHERE id = 1`` 与 ``id = 2`` 是同一种语句。归一化后
    取 sha1 作为 ``digest``，再把执行次数、总耗时累加到同一行——否则洞察表
    里每行都是"只执行过一次"的噪声，真正的高频慢 SQL 反而被淹没。

    为什么存 ``template`` 而不是原始 SQL
    ------------------------------------
    原始 SQL 里的字面量可能是手机号、身份证号、口令。归一化时字面量已被
    替换成 ``?``，落库的模板天然不含业务数据。这不是额外加的功能，而是
    「绝不在日志里留存明文」这条红线在洞察模块的延续。

    ``engine`` 单独存：同一段 SQL 在 MySQL 与 PG 上语义不同，混在一起聚合
    会得出错误结论。
    """

    instance = models.ForeignKey(
        "assets.Instance",
        verbose_name="实例",
        on_delete=models.CASCADE,
        related_name="sql_digests",
    )
    engine = models.CharField("引擎", max_length=16, default="")
    digest = models.CharField("语句指纹(sha1)", max_length=40)
    template = models.CharField("归一化模板", max_length=2000)
    exec_count = models.PositiveBigIntegerField("执行次数", default=0)
    total_time_ms = models.FloatField("总耗时(ms)", default=0)
    max_time_ms = models.FloatField("单次最大耗时(ms)", default=0)
    avg_time_ms = models.FloatField("平均耗时(ms)", default=0)
    rows_examined = models.PositiveBigIntegerField("扫描行数累计", default=0)
    first_seen_at = models.DateTimeField("首次出现")
    last_seen_at = models.DateTimeField("最近出现")

    class Meta:
        db_table = "monitor_sql_digest"
        verbose_name = "SQL 指纹"
        verbose_name_plural = "SQL 指纹"
        ordering = ["-total_time_ms", "-id"]
        constraints = [
            # 同一实例同一模板只能有一行，采集侧的 upsert 依赖它
            models.UniqueConstraint(
                fields=["instance", "digest"], name="uniq_sql_digest_per_instance"
            ),
        ]
        indexes = [
            # 洞察页默认按总耗时倒序取前 N 条
            models.Index(
                fields=["instance", "-total_time_ms"], name="idx_digest_inst_time"
            ),
            models.Index(fields=["last_seen_at"], name="idx_digest_last_seen"),
        ]

    def __str__(self) -> str:
        return f"{self.engine}#{self.digest[:8]} x{self.exec_count}"


class LockWait(models.Model):
    """一条锁等待关系（谁在等谁）。

    这是**瞬时状态快照**，不是历史：采集每轮先清空该实例的旧记录再写入，
    否则表会无限增长，而且页面上会同时出现"已经解开的等待"和"正在发生
    的等待"，运维无法判断当前到底还堵不堵。

    需要历史时看 ``DeadlockEvent``（死锁是事件，值得留档）；
    普通锁等待只是"此刻的排队"，留档价值低、噪声大。
    """

    instance = models.ForeignKey(
        "assets.Instance",
        verbose_name="实例",
        on_delete=models.CASCADE,
        related_name="lock_waits",
    )
    collected_at = models.DateTimeField("采集时刻")
    waiting_id = models.CharField("等待方", max_length=64)
    blocking_id = models.CharField("阻塞方", max_length=64)
    waiting_label = models.CharField("等待方描述", max_length=200, blank=True, default="")
    blocking_label = models.CharField("阻塞方描述", max_length=200, blank=True, default="")
    lock_type = models.CharField("锁类型", max_length=64, blank=True, default="")
    relation = models.CharField("对象", max_length=200, blank=True, default="")
    wait_seconds = models.FloatField("已等待(秒)", default=0)
    query = models.CharField("等待中的语句", max_length=300, blank=True, default="")

    class Meta:
        db_table = "monitor_lock_wait"
        verbose_name = "锁等待"
        verbose_name_plural = "锁等待"
        # 等得最久的排前面：那才是要先处理的
        ordering = ["-wait_seconds", "-id"]
        indexes = [
            models.Index(fields=["instance", "-wait_seconds"], name="idx_lock_inst_wait"),
        ]

    def __str__(self) -> str:
        return f"{self.waiting_id}<-{self.blocking_id} {self.wait_seconds:.0f}s"


class DeadlockEvent(models.Model):
    """一次死锁记录。

    与 ``LockWait`` 的取舍相反：死锁是**已经发生的坏事**，必须留档——
    "上周那次死锁是哪些语句互相等"是排查设计缺陷的关键线索。

    ``deadlock_total`` 存源端的累计计数，用于判断"自上次采集以来是否
    新增了死锁"；``detail`` 存服务端给的死锁原文摘要（已截断）。
    """

    instance = models.ForeignKey(
        "assets.Instance",
        verbose_name="实例",
        on_delete=models.CASCADE,
        related_name="deadlock_events",
    )
    detected_at = models.DateTimeField("发现时刻")
    deadlock_total = models.PositiveBigIntegerField("累计死锁数", default=0)
    detail = models.CharField("死锁摘要", max_length=2000, blank=True, default="")

    class Meta:
        db_table = "monitor_deadlock_event"
        verbose_name = "死锁事件"
        verbose_name_plural = "死锁事件"
        ordering = ["-detected_at", "-id"]
        constraints = [
            # 每个观测到的累计值只留一条：既避免同一累计值被反复记录，
            # 又能在源端重启归零后重新累计时正确记下新事件。
            # 若改用"是否大于历史最大值"判断，归零后的小值会被永久漏记。
            models.UniqueConstraint(
                fields=["instance", "deadlock_total"],
                name="uniq_deadlock_total_per_instance",
            ),
        ]
        indexes = [
            models.Index(fields=["instance", "-detected_at"], name="idx_deadlock_inst_time"),
        ]

    def __str__(self) -> str:
        return f"{self.instance_id}@{self.detected_at}:{self.deadlock_total}"
