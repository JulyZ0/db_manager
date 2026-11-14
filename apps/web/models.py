"""运维界面的本地账号状态。

为什么需要这张表
----------------
``ensure_admin`` 会用固定的默认口令（``Admin@123456``）创建 ``admin`` 账号，
这口令写在仓库与 README 里，等于公开信息。若不在首次登录时强制改掉，
"一键启动"就等于给生产后台留了一道人人可知的门。

为什么存哈希而不是布尔标记
--------------------------
记一个 ``must_change=True`` 的布尔值，就必须在**每一条**改密路径上手工
清标记：本系统的改密页、Django admin 自带的改密页、``manage.py
changepassword``。漏掉任何一条，运维改完密码仍被反复拦在门外——这种
"改了也不生效"的故障最难自己诊断。

改成存"默认口令对应的哈希"后，判断退化为一次等值比较：

- 任何途径改过密码，``user.password`` 的哈希必然变化 → 自动解除；
- 不需要扩展 ``AUTH_USER_MODEL``，升级对既有部署零影响；
- 不存明文、不存可逆信息，泄露本表也拿不到口令。

刻意**不**拦截 API
------------------
强制改密是"人的操作路径"问题。API 调用方是脚本，它没有交互界面可以改密；
把它一起拦住只会让既有自动化突然 401，而真正的风险（默认口令登录后台）
并没有因此降低。因此中间件只作用于页面路径。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class DefaultPasswordWatch(models.Model):
    """记录某个账号"仍在用默认口令"时的密码哈希。

    一旦 ``user.password`` 与该值不再相等，说明密码已经被改过，
    这条记录即失去意义（判断逻辑见 ``is_using_default_password``）。
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="default_password_watch",
        verbose_name="账号",
    )
    # 建号时的密码哈希。只用于等值比较，本身不可逆推口令。
    password_hash = models.CharField("默认口令哈希", max_length=128)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "web_default_password_watch"
        verbose_name = "默认口令监视"
        verbose_name_plural = "默认口令监视"

    def __str__(self) -> str:
        return f"default-password watch for {self.user_id}"


def mark_default_password(user) -> DefaultPasswordWatch:
    """登记"这个账号当前用的就是默认口令"。

    幂等：重复调用只更新哈希。``ensure_admin`` 每次启动都会跑，不能因为
    重复执行而报错。
    """
    watch, _ = DefaultPasswordWatch.objects.update_or_create(
        user=user, defaults={"password_hash": user.password}
    )
    return watch


def is_using_default_password(user) -> bool:
    """当前密码是否仍是登记时的那个默认口令。

    比较整段哈希（含算法、盐、摘要）：``set_password`` 每次都会换新盐，
    所以即便运维把密码设回同样的明文，哈希也不同，判定为"已改过"——
    这是刻意的，改回默认值属于主动行为，不该被系统当成残留风险。
    """
    if user is None or not getattr(user, "pk", None):
        return False

    from django.db.utils import OperationalError, ProgrammingError

    try:
        current = (
            DefaultPasswordWatch.objects.filter(user_id=user.pk)
            .values_list("password_hash", flat=True)
            .first()
        )
    except (OperationalError, ProgrammingError):
        # 迁移尚未执行（例如首次 migrate 之前）时不要让整站 500，
        # 此时也还没建出默认账号，放行是安全的。
        return False

    if not current:
        return False
    return current == user.password
