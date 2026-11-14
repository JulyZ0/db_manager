"""运维界面的表单。

刻意使用 Django Form 而不是 DRF Serializer：
- 服务端渲染场景下，Form 自带 widget 与错误渲染，模板最省事；
- 与 API 层的 Pydantic Schema 各自独立，API 契约不受页面改动影响。
"""

from __future__ import annotations

from django import forms

from apps.assets.models import Cluster, Department, Instance
from apps.monitor.models import AlertRule
from apps.credentials.models import (
    MAX_INTERVAL_HOURS,
    MIN_INTERVAL_HOURS,
    RotationPolicy,
    RotationScheduleMode,
    parse_daily_times,
)


class BootstrapMixin:
    """给所有字段补上 CSS class，避免在模板里逐字段写 widget。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "checkbox-input")
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs.setdefault("class", "select")
            else:
                widget.attrs.setdefault("class", "input")
            if isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("rows", 3)


class TopologyAwareSelect(forms.Select):
    """集群下拉：给每个 option 标注所属集群的架构。

    前端据此实时过滤"角色"下拉，避免用户先选到非法角色再被后端拒绝。
    真正的把关仍在 ``InstanceForm.clean()`` 与 ``Instance.clean()``，
    这里只负责让界面不给出错误选项。
    """

    def create_option(
        self, name, value, label, selected, index, subindex=None, attrs=None
    ):
        option = super().create_option(
            name, value, label, selected, index, subindex, attrs
        )
        cluster = getattr(value, "instance", None)
        if cluster is not None:
            option["attrs"]["data-topology"] = cluster.topology
        return option


class ScopedRoleSelect(forms.Select):
    """角色下拉：给每个 option 标注它适用于哪些架构（逗号分隔）。"""

    def __init__(self, scopes=None, **kwargs):
        self.scopes = scopes or {}
        super().__init__(**kwargs)

    def create_option(
        self, name, value, label, selected, index, subindex=None, attrs=None
    ):
        option = super().create_option(
            name, value, label, selected, index, subindex, attrs
        )
        key = getattr(value, "value", value)
        if key in self.scopes:
            option["attrs"]["data-allow"] = self.scopes[key]
        return option


class DepartmentForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Department
        fields = ["name", "code", "owner", "description", "is_active"]
        labels = {
            "name": "部门名称",
            "code": "部门编码",
            "owner": "负责人",
            "description": "描述",
            "is_active": "启用",
        }
        help_texts = {
            "code": "可留空；一旦填写需全局唯一",
            "is_active": "停用后不影响已有集群，但不应再新增",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "例如：基础架构部"}),
            "code": forms.TextInput(attrs={"placeholder": "例如：INFRA"}),
            "owner": forms.TextInput(attrs={"placeholder": "例如：张三"}),
        }

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip()
        return code or None


class ClusterForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Cluster
        fields = [
            "name",
            "alias",
            "department",
            "engine",
            "env",
            "topology",
            "description",
            "is_active",
        ]
        labels = {
            "name": "集群名称",
            "alias": "集群别名",
            "department": "所属部门",
            "engine": "数据库引擎",
            "env": "环境",
            "topology": "集群架构",
            "description": "描述",
            "is_active": "启用",
        }
        help_texts = {
            "name": "同一部门内不可重名，不同部门可以重名",
            "alias": "可选，全局唯一。控制台里可用「别名.实例别名」快速切换",
            "department": "集群必须归属一个部门",
            "topology": "架构决定实例可选的角色；复制关系在集群详情页统一编排",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "例如：core-mysql-prod"}),
            "alias": forms.TextInput(attrs={"placeholder": "例如：jq1"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.order_by("name")
        self.fields["department"].empty_label = "请选择部门"
        # 架构是"声明"而不是硬性约束：新建时给个安全默认（单机），
        # 也让漏传该字段的旧表单/脚本不至于保存失败。
        self.fields["topology"].required = False
        if not self.instance.pk:
            self.fields["topology"].initial = Cluster.Topology.STANDALONE

    def clean_topology(self):
        return self.cleaned_data.get("topology") or Cluster.Topology.STANDALONE


class InstanceForm(BootstrapMixin, forms.ModelForm):
    """实例表单。

    password 不是模型字段（模型里只有密文），因此单独声明为可选输入框：
    编辑时留空表示"不修改密码"。
    """

    password = forms.CharField(
        label="登录密码",
        required=False,
        widget=forms.PasswordInput(
            attrs={"placeholder": "留空表示不修改", "autocomplete": "new-password"}
        ),
        help_text="提交后立即加密入库，页面上永远不回显",
    )

    class Meta:
        model = Instance
        fields = [
            "name",
            "alias",
            "cluster",
            "host",
            "port",
            "role",
            "username",
            "remark",
            "is_active",
        ]
        labels = {
            "name": "实例名称",
            "alias": "实例别名",
            "cluster": "所属集群",
            "host": "主机地址",
            "port": "端口",
            "role": "角色",
            "username": "登录账号",
            "remark": "备注",
            "is_active": "启用",
        }
        help_texts = {
            "alias": "可选，集群内唯一。控制台里可用「集群别名.本别名」切换",
            "host": "支持 IP 或域名，例如 10.0.12.31 / db-01.internal",
            "port": "1 - 65535",
            "role": "可选范围由集群架构决定；复制关系在集群详情页统一编排",
            "username": "用于连接数据库的账号名",
            "is_active": "只有启用的实例才会参与定时密码轮换",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "例如：core-mysql-prod-01"}),
            "alias": forms.TextInput(attrs={"placeholder": "例如：ins1"}),
            "host": forms.TextInput(attrs={"placeholder": "10.0.12.31"}),
            "port": forms.NumberInput(attrs={"min": 1, "max": 65535}),
            "remark": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cluster"].queryset = (
            Cluster.objects.select_related("department").order_by(
                "department__name", "name"
            )
        )
        self.fields["cluster"].empty_label = "请选择集群"
        # 让下拉框显示"部门/集群"，避免同名集群选错
        self.fields["cluster"].label_from_instance = lambda c: f"{c.department.name} / {c.name}"

        # 角色可选范围由集群架构决定。这里给每个 option 打上 data-allow，
        # 前端据选中集群实时过滤；后端 clean() 再兜一层，避免绕过前端提交。
        cluster_field = self.fields["cluster"]
        cluster_field.widget = TopologyAwareSelect(
            choices=cluster_field.choices, attrs={"class": "select"}
        )

        role_field = self.fields["role"]
        scopes = self._role_scopes()
        role_field.choices = [(value, label) for value, label, _ in scopes]
        role_field.widget = ScopedRoleSelect(
            scopes={value: allow for value, _, allow in scopes},
            choices=role_field.choices,
            attrs={"class": "select"},
        )

    @staticmethod
    def _role_scopes() -> list[tuple[str, str, str]]:
        """(角色值, 显示名, 适用架构列表) —— 供前端过滤与后端校验共用一份口径。"""
        labels = dict(Instance.Role.choices)
        scoped = []
        for value in Instance.Role.values:
            tops = [
                topo
                for topo in Cluster.Topology.values
                if value in Instance.roles_for_topology(topo)
            ]
            scoped.append((value, labels[value], ",".join(tops)))
        return scoped

    def clean(self):
        cleaned = super().clean()
        cluster = cleaned.get("cluster")
        role = cleaned.get("role")

        # 架构是集群级决策，实例只能在架构允许的范围内选角色。
        # 放在表单层拦是为了给出可读提示；模型层 clean() 仍有同样判定，
        # 覆盖管理后台等其它写入路径。
        if cluster and role:
            allowed = Instance.roles_for_topology(cluster.topology)
            if role not in allowed:
                labels = dict(Instance.Role.choices)
                allowed_text = "、".join(labels[r] for r in allowed)
                self.add_error(
                    "role",
                    f"集群「{cluster.name}」的架构是"
                    f"「{cluster.get_topology_display()}」，"
                    f"实例角色只能是：{allowed_text}",
                )
        return cleaned

    def clean_port(self):
        port = self.cleaned_data.get("port")
        if port is None or not (1 <= int(port) <= 65535):
            raise forms.ValidationError("端口必须在 1 - 65535 之间")
        return port


class RotationPolicyForm(BootstrapMixin, forms.ModelForm):
    """轮换策略表单：时间、范围、强度三块。

    三个"模式相关"的字段不能只靠 HTML 必填校验：用户在页面上切换调度方式时
    另一组字段会被隐藏，但浏览器仍会提交（隐藏≠禁用）。所以这里统一放宽
    ``required``，改在 ``clean()`` 里按最终选中的模式校验。
    """

    class Meta:
        model = RotationPolicy
        fields = [
            "enabled",
            "schedule_mode",
            "interval_hours",
            "daily_times",
            "password_length",
            "clusters",
            "instances",
        ]
        labels = {
            "enabled": "启用自动轮换",
            "schedule_mode": "轮换时间",
            "interval_hours": "间隔小时数",
            "daily_times": "每日执行时刻",
            "password_length": "密码长度",
            "clusters": "按集群选择范围",
            "instances": "按实例选择范围",
        }
        help_texts = {
            "enabled": "停用后不会再自动改密，手动轮换仍然可用",
            "interval_hours": f"取值 {MIN_INTERVAL_HOURS} – {MAX_INTERVAL_HOURS}，留空按默认值执行",
            "daily_times": "逗号分隔的 HH:MM，例如 00:00,12:00",
            "password_length": "越长越安全；部分老旧数据库对超长密码有限制，请按实例情况选择",
            "clusters": "勾选后，该集群下所有启用实例都会参与轮换",
            "instances": "与上面的集群范围取并集；两者都不选表示全部启用实例",
        }
        widgets = {
            "interval_hours": forms.NumberInput(
                attrs={"min": MIN_INTERVAL_HOURS, "max": MAX_INTERVAL_HOURS}
            ),
            "daily_times": forms.TextInput(attrs={"placeholder": "00:00,12:00"}),
            "clusters": forms.CheckboxSelectMultiple,
            "instances": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # 模式互斥字段不做 HTML 必填，交给 clean() 按模式判定
        self.fields["interval_hours"].required = False
        self.fields["daily_times"].required = False

        self.fields["clusters"].queryset = Cluster.objects.select_related(
            "department"
        ).order_by("department__name", "name")
        self.fields["clusters"].label_from_instance = (
            lambda c: f"{c.department.name} / {c.name}"
        )

        self.fields["instances"].queryset = Instance.objects.select_related(
            "cluster"
        ).order_by("cluster__name", "name")
        self.fields["instances"].label_from_instance = (
            lambda i: f"{i.cluster.name} / {i.name}（{i.host}:{i.port}）"
        )

    def clean_interval_hours(self):
        value = self.cleaned_data.get("interval_hours")
        if value is None:
            # 留空回落默认值：模型要求 NOT NULL，且 Daily 模式用不到这个字段
            return RotationPolicy._meta.get_field("interval_hours").default
        return value

    def clean_daily_times(self):
        raw = (self.cleaned_data.get("daily_times") or "").strip()
        default = RotationPolicy._meta.get_field("daily_times").default
        if not raw:
            return default
        # parse_daily_times 抛的是 Django 的 ValidationError，表单会直接收下
        moments = parse_daily_times(raw)
        return ",".join(f"{m.hour:02d}:{m.minute:02d}" for m in moments)

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get("schedule_mode")
        hours = cleaned.get("interval_hours")

        # 只校验当前模式用到的字段，另一组即使被提交也不该报错
        if mode == RotationScheduleMode.INTERVAL:
            if hours is None:
                self.add_error("interval_hours", "固定间隔模式必须填写间隔小时数")
            elif not (MIN_INTERVAL_HOURS <= int(hours) <= MAX_INTERVAL_HOURS):
                self.add_error(
                    "interval_hours",
                    f"间隔需在 {MIN_INTERVAL_HOURS} – {MAX_INTERVAL_HOURS} 小时之间",
                )

        # 范围为空是合法配置（= 全部启用实例），不做拦截；
        # 但至少要让人看清这次保存会命中多少实例，提示放在 success 消息里。

        return cleaned


class ChangePasswordForm(BootstrapMixin, forms.Form):
    """修改当前登录账号的密码。

    为什么不用 Django 自带的 ``PasswordChangeForm``
    ----------------------------------------------
    自带的那个把校验全交给 ``AUTH_PASSWORD_VALIDATORS``，默认配置里
    ``MinimumLengthValidator`` 只要求 8 位——对"能操作生产库口令台账"
    的账号太松。这里保留验证器链路（便于统一调整），另加两条业务规则：
    新密码不得与旧密码相同、两次输入必须一致。

    校验顺序刻意先验旧密码：如果先报"新密码太短"，攻击者可以据此判断
    当前密码对不对——虽然本页已要求登录，但没有必要多给一个试探口。
    """

    old_password = forms.CharField(
        label="当前密码",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
        help_text="首次登录时需要填写系统给你的初始密码",
    )
    new_password1 = forms.CharField(
        label="新密码",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="至少 12 位，建议混合大小写字母、数字与符号",
    )
    new_password2 = forms.CharField(
        label="确认新密码",
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="再输入一次，避免手误",
    )

    def __init__(self, user, *args, **kwargs) -> None:
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_old_password(self):
        old_password = self.cleaned_data["old_password"]
        if not self.user.check_password(old_password):
            raise forms.ValidationError("当前密码不正确")
        return old_password

    def clean(self):
        cleaned = super().clean()
        new1 = cleaned.get("new_password1")
        new2 = cleaned.get("new_password2")
        old = cleaned.get("old_password")

        if new1 and new2 and new1 != new2:
            self.add_error("new_password2", "两次输入的新密码不一致")

        if not new1:
            return cleaned

        if old and new1 == old:
            self.add_error("new_password1", "新密码不能与当前密码相同")
            return cleaned

        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        try:
            validate_password(new1, self.user)
        except DjangoValidationError as exc:
            self.add_error("new_password1", exc)

        return cleaned



class AlertRuleForm(BootstrapMixin, forms.ModelForm):
    """告警规则表单。

    ``metric`` 用下拉而不是自由文本：指标键（``mysql.qps``）拼错一个字符，
    规则就会永远不触发，而页面上看不出任何异常。下拉把可选值限定在
    系统真实采集的清单里。

    已存在但不在目录中的旧指标键要补进选项，否则编辑旧规则时下拉会
    "选不中"，保存后等于悄悄改掉了规则指向的指标。
    """

    class Meta:
        model = AlertRule
        fields = [
            "name",
            "metric",
            "operator",
            "threshold",
            "duration_minutes",
            "severity",
            "cluster",
            "cooldown_minutes",
            "enabled",
            "remark",
        ]
        labels = {
            "name": "规则名称",
            "metric": "监控指标",
            "operator": "比较方式",
            "threshold": "阈值",
            "duration_minutes": "持续时长（分钟）",
            "severity": "告警级别",
            "cluster": "限定集群",
            "cooldown_minutes": "恢复后冷却（分钟）",
            "enabled": "启用",
            "remark": "备注",
        }
        help_texts = {
            "name": "全局唯一，便于在事件列表里辨认",
            "threshold": "与指标同单位；命中方式由「比较方式」决定",
            "duration_minutes": "0 表示一越界就告警；大于 0 时需持续越界满该时长",
            "cluster": "留空表示对所有集群生效",
            "cooldown_minutes": "恢复后这段时间内不再重复告警，避免阈值附近抖动刷屏",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        from apps.monitor.catalog import metric_choices

        choices = metric_choices()
        current = None
        if self.instance and self.instance.pk:
            current = self.instance.metric
        elif self.initial.get("metric"):
            current = self.initial["metric"]

        # 旧指标键不在目录里时补一项，避免编辑时被"修正"成别的指标
        known = {value for value, _label in choices}
        if current and current not in known:
            choices.insert(0, (current, f"{current}（已不在指标目录中）"))
        self.fields["metric"] = forms.ChoiceField(
            label="监控指标",
            choices=choices,
            initial=current,
            help_text="只列出系统当前会采集的指标",
        )
