"""运维界面的表单。

刻意使用 Django Form 而不是 DRF Serializer：
- 服务端渲染场景下，Form 自带 widget 与错误渲染，模板最省事；
- 与 API 层的 Pydantic Schema 各自独立，API 契约不受页面改动影响。
"""

from __future__ import annotations

from django import forms

from apps.assets.models import Cluster, Department, Instance


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
        fields = ["name", "alias", "department", "engine", "env", "description", "is_active"]
        labels = {
            "name": "集群名称",
            "alias": "集群别名",
            "department": "所属部门",
            "engine": "数据库引擎",
            "env": "环境",
            "description": "描述",
            "is_active": "启用",
        }
        help_texts = {
            "name": "同一部门内不可重名，不同部门可以重名",
            "alias": "可选，全局唯一。控制台里可用「别名.实例别名」快速切换",
            "department": "集群必须归属一个部门",
        }
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "例如：core-mysql-prod"}),
            "alias": forms.TextInput(attrs={"placeholder": "例如：jq1"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.order_by("name")
        self.fields["department"].empty_label = "请选择部门"


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

    def clean_port(self):
        port = self.cleaned_data.get("port")
        if port is None or not (1 <= int(port) <= 65535):
            raise forms.ValidationError("端口必须在 1 - 65535 之间")
        return port
