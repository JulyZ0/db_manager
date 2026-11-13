"""定时轮换策略。

设计取舍
--------
1. **单例**：整个系统只维护一份轮换策略。运维的心智是"这个系统多久换一次
   密码"，而不是"给每个集群配一套 cron"。需要差异化时，用范围（集群/实例）
   而不是多套策略来表达。用固定主键 1 保证单例，避免出现两份策略互相打架。
2. **两种时间口径并存**：
   - ``interval``：每 N 小时一次，能表达"每 5 小时"这种 cron 表达不了的周期；
   - ``daily``：每天固定时刻，运维更习惯"零点跑一次"。
   两者的"下次执行时间"计算与到点判定都集中在 ``next_run_at`` / ``is_due``，
   调度器只调用这两个方法，不关心是哪种口径。
3. **范围**：留空表示"全部启用实例"，与改造前的行为一致；选了集群就包含
   集群下的启用实例；选了实例就只换这些。两者叠加时取并集。
4. **强度**：只存密码长度档位。字符集与随机源是安全基线，不给用户改——
   允许调低字符集会直接削弱强度，那不叫"配置"叫"留后门"。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

# 时间间隔上限取 30 天：再长就不是"轮换"而是"遗忘"了
MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 24 * 30

# 可选的密码长度档位。未列出的值不接受，避免有人填 4 位图省事。
PASSWORD_LENGTH_CHOICES = (16, 24, 32, 48)
DEFAULT_PASSWORD_LENGTH = 32


class RotationScheduleMode(models.TextChoices):
    INTERVAL = "interval", "固定间隔"
    DAILY = "daily", "每日定点"


def parse_daily_times(raw: str) -> list[time]:
    """把 ``"00:00,12:00"`` 解析成 ``[time(0, 0), time(12, 0)]``。

    只接受 ``HH:MM``，按时间升序返回并去重。格式非法直接抛 ValidationError，
    不做"尽力猜测"——静默纠正会让运维以为自己配的是另一个时刻。
    """
    result: list[time] = []
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2:
            raise ValidationError(f"时刻格式应为 HH:MM，收到「{item}」")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ValidationError(f"时刻「{item}」不是合法数字") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValidationError(f"时刻「{item}」超出范围（00:00–23:59）")
        moment = time(hour, minute)
        if moment not in result:
            result.append(moment)
    if not result:
        raise ValidationError("每日定点至少需要一个时刻，例如 00:00")
    return sorted(result)


def format_daily_times(moments: list[time]) -> str:
    return ",".join(f"{m.hour:02d}:{m.minute:02d}" for m in moments)


class RotationPolicy(models.Model):
    """全局唯一的定时轮换策略。"""

    SINGLETON_PK = 1

    class Meta:
        verbose_name = "轮换策略"
        verbose_name_plural = "轮换策略"

    enabled = models.BooleanField("启用自动轮换", default=True)

    schedule_mode = models.CharField(
        "调度方式",
        max_length=10,
        choices=RotationScheduleMode.choices,
        default=RotationScheduleMode.INTERVAL,
    )
    interval_hours = models.PositiveSmallIntegerField(
        "间隔小时数",
        default=12,
        help_text=f"固定间隔模式下生效，取值 {MIN_INTERVAL_HOURS}–{MAX_INTERVAL_HOURS}",
    )
    daily_times = models.CharField(
        "每日执行时刻",
        max_length=120,
        default="00:00,12:00",
        help_text="每日定点模式下生效，逗号分隔，例如 00:00,12:00",
    )

    password_length = models.PositiveSmallIntegerField(
        "密码长度",
        choices=[(n, f"{n} 位") for n in PASSWORD_LENGTH_CHOICES],
        default=DEFAULT_PASSWORD_LENGTH,
    )

    # 范围：留空 = 全部启用实例
    clusters = models.ManyToManyField(
        "assets.Cluster",
        blank=True,
        related_name="rotation_policies",
        verbose_name="轮换集群范围",
        help_text="勾选的集群下全部启用实例都会参与轮换",
    )
    instances = models.ManyToManyField(
        "assets.Instance",
        blank=True,
        related_name="rotation_policies",
        verbose_name="轮换实例范围",
        help_text="单独勾选的实例，与集群范围取并集",
    )

    last_run_at = models.DateTimeField("上次执行时间", null=True, blank=True)
    last_run_summary = models.CharField("上次执行结果", max_length=200, blank=True, default="")

    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    def __str__(self) -> str:
        return f"轮换策略（{self.get_schedule_mode_display()}）"

    # -- 单例 ---------------------------------------------------------------

    @classmethod
    def load(cls) -> "RotationPolicy":
        """取全局策略，不存在则按默认值创建。"""
        policy, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return policy

    def save(self, *args, **kwargs):
        # 强制主键，杜绝出现第二份策略
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    # -- 校验 ---------------------------------------------------------------

    def clean(self) -> None:
        if self.schedule_mode == RotationScheduleMode.INTERVAL:
            if not (MIN_INTERVAL_HOURS <= self.interval_hours <= MAX_INTERVAL_HOURS):
                raise ValidationError(
                    {
                        "interval_hours": (
                            f"间隔需在 {MIN_INTERVAL_HOURS}–{MAX_INTERVAL_HOURS} 小时之间"
                        )
                    }
                )
        else:
            # 借 parse_daily_times 的校验，异常整形成表单可见的错误
            try:
                parse_daily_times(self.daily_times)
            except ValidationError as exc:
                raise ValidationError({"daily_times": exc.messages}) from exc

        if self.password_length not in PASSWORD_LENGTH_CHOICES:
            raise ValidationError(
                {"password_length": f"密码长度只能是 {PASSWORD_LENGTH_CHOICES} 之一"}
            )

    # -- 调度计算 -----------------------------------------------------------

    def daily_moments(self) -> list[time]:
        try:
            return parse_daily_times(self.daily_times)
        except ValidationError:
            # 存量脏数据不该让调度线程崩掉，退回默认时刻
            return [time(0, 0), time(12, 0)]

    def next_run_at(self, *, now: datetime | None = None) -> datetime | None:
        """返回下一次应当执行的时间；策略停用或无意义时返回 None。

        两种口径都折算成"绝对时刻"，调度器只比较时刻，不关心模式。
        """
        if not self.enabled:
            return None

        moment = now or timezone.now()
        moment = timezone.localtime(moment) if timezone.is_aware(moment) else moment

        if self.schedule_mode == RotationScheduleMode.INTERVAL:
            hours = max(MIN_INTERVAL_HOURS, int(self.interval_hours or 0))
            if self.last_run_at is None:
                # 从未跑过：下一个整点起算，避免保存策略就立刻触发
                base = moment.replace(minute=0, second=0, microsecond=0) + timedelta(
                    hours=1
                )
                return base
            last = timezone.localtime(self.last_run_at)
            return last + timedelta(hours=hours)

        # 每日定点：找今天还没过的第一个时刻；都过了就取明天第一个
        items = self.daily_moments()
        for item in items:
            candidate = moment.replace(
                hour=item.hour, minute=item.minute, second=0, microsecond=0
            )
            if candidate > moment:
                return candidate
        tomorrow = moment.replace(
            hour=items[0].hour,
            minute=items[0].minute,
            second=0,
            microsecond=0,
        ) + timedelta(days=1)
        return tomorrow

    def is_due(self, *, now: datetime | None = None) -> bool:
        """是否应当立刻触发一次轮换。

        用"上次执行时间"而不是"整分钟匹配"来判定，理由：
        - 进程内调度器每 15 秒 tick 一次，分钟匹配会漏掉或重复；
        - 服务重启后不会因为"错过了那个整点"而永远不执行。
        """
        if not self.enabled:
            return False

        moment = now or timezone.now()
        moment = timezone.localtime(moment) if timezone.is_aware(moment) else moment

        if self.schedule_mode == RotationScheduleMode.INTERVAL:
            if self.last_run_at is None:
                # 从未执行过：等一个间隔，别让"刚部署就全量改密"吓到运维
                return False
            hours = max(MIN_INTERVAL_HOURS, int(self.interval_hours or 0))
            return moment >= timezone.localtime(self.last_run_at) + timedelta(hours=hours)

        # 每日定点：该时刻之后还没跑过
        for item in self.daily_moments():
            slot = moment.replace(
                hour=item.hour, minute=item.minute, second=0, microsecond=0
            )
            if slot <= moment:
                if self.last_run_at is None:
                    return True
                if timezone.localtime(self.last_run_at) < slot:
                    return True
        return False

    # -- 范围 ---------------------------------------------------------------

    def resolve_instance_ids(self) -> list[int]:
        """把范围折算成待轮换的实例 id 列表（只含启用实例）。"""
        from apps.assets.models import Instance

        base = Instance.objects.filter(is_active=True)

        cluster_ids = list(self.clusters.values_list("id", flat=True))
        instance_ids = list(self.instances.values_list("id", flat=True))

        if not cluster_ids and not instance_ids:
            # 留空 = 全部启用实例，与改造前行为一致
            return list(base.order_by("id").values_list("id", flat=True))

        qs = base.filter(
            models.Q(cluster_id__in=cluster_ids) | models.Q(id__in=instance_ids)
        )
        # 显式勾选的实例即使属于未勾选的集群也要包含
        return list(qs.order_by("id").values_list("id", flat=True))

    def includes_instance(self, instance_id: int) -> bool:
        """单个实例是否在轮换范围内，供详情页这类"只问一个是非"的场景用。

        为什么单独开一个方法
        --------------------
        详情页只需要一个布尔值。若复用 ``resolve_instance_ids()``，每次打开
        页面都要把全部启用实例的 id 拉成列表再转集合：实例上千时是 O(N)
        的传输与内存开销，只为回答"这一个在不在里面"。

        这里把判断下推给数据库：范围为空时退化成一次 ``is_active`` 查询；
        有范围时用 ``values("id")`` 子查询，不在 Python 侧搬运任何 id 列表。
        语义与 ``resolve_instance_ids()`` 严格一致。
        """
        from apps.assets.models import Instance

        base = Instance.objects.filter(pk=instance_id, is_active=True)

        # 短路求值：clusters 非空就不必再查 instances
        if not self.clusters.exists() and not self.instances.exists():
            # 留空 = 全部启用实例
            return base.exists()

        return base.filter(
            models.Q(cluster_id__in=self.clusters.values("id"))
            | models.Q(id__in=self.instances.values("id"))
        ).exists()

    def scope_label(self) -> str:
        cluster_count = self.clusters.count()
        instance_count = self.instances.count()
        if not cluster_count and not instance_count:
            return "全部启用实例"
        parts = []
        if cluster_count:
            parts.append(f"{cluster_count} 个集群")
        if instance_count:
            parts.append(f"{instance_count} 个实例")
        return " + ".join(parts)
