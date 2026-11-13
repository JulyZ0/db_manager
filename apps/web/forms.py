"""运维界面的表单。

刻意使用 Django Form 而不是 DRF Serializer：
- 服务端渲染场景下，Form 自带 widget 与错误渲染，模板最省事；
- 与 API 层的 Pydantic Schema 各自独立，API 契约不受页面改动影响。
"""

from __future__ import annotations

from django import forms

from apps.assets.models import Cluster, Department, Instance
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
            "topology": "声明部署形态；实例之间谁跟谁同步，在实例上填「复制上游」",
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
            "replica_of",
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
            "replica_of": "复制上游",
            "username": "登录账号",
            "remark": "备注",
            "is_active": "启用",
        }
        help_texts = {
            "alias": "可选，集群内唯一。控制台里可用「集群别名.本别名」切换",
            "host": "支持 IP 或域名，例如 10.0.12.31 / db-01.internal",
            "port": "1 - 65535",
            "role": "主库/从库/代理；仅用于标注这台实例的定位",
            "replica_of": "从库填它跟随的主库；双主场景两台互填；单机留空",
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

        # 复制上游只允许选同集群的实例；这里先给出全量候选项，
        # 真正"必须同集群"由 clean() 判定（切集群时下拉不会跟着变）
        replica_field = self.fields["replica_of"]
        replica_field.queryset = (
            Instance.objects.select_related("cluster").order_by(
                "cluster__name", "name"
            )
        )
        replica_field.empty_label = "无（单机 / 主库）"
        replica_field.label_from_instance = lambda i: (
            f"{i.cluster.name} / {i.name} ({i.host}:{i.port})"
        )
        if self.instance.pk:
            # 不能把自己选成自己的上游
            replica_field.queryset = replica_field.queryset.exclude(
                id=self.instance.pk
            )

    def clean(self):
        cleaned = super().clean()
        cluster = cleaned.get("cluster")
        upstream = cleaned.get("replica_of")

        if upstream is None:
            return cleaned

        if cluster and upstream.cluster_id != cluster.id:
            self.add_error(
                "replica_of",
                "复制上游必须与本实例属于同一集群：跨集群的复制关系不在台账登记范围",
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

