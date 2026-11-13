"""控制台切换目标的解析。

控制台允许用四种写法指向一个实例，解析规则集中在这里，避免散落在视图里：

===============  ==========================================
写法              含义
===============  ==========================================
``3``            实例 id
``jq1.ins1``     集群别名 . 实例别名（两段都可以换成 id）
``1.1``          集群 id . 实例 id
``jq1@10.0.12.1``  集群别名 @ 主机地址（可带端口 :3306）
``jq1``          集群别名（集群内只有一个实例时直接选中）
``ins1``         实例别名（跨集群查找，同名时报歧义）
===============  ==========================================

为什么别名不能含 ``.`` ``@`` ``:`` 或纯数字：这些正是上表的分隔符与 id 形式，
一旦允许会立刻产生歧义。约束在 ``apps.assets.validators`` 里已强制。

大小写策略：先精确匹配，无命中再做一次大小写不敏感查找。
只在不敏感命中也唯一时才采用——多命中就报歧义，绝不猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.assets.models import Cluster, Instance

# ---------------------------------------------------------------------------
# 错误码：调用方按码判断，message 只用于展示
# ---------------------------------------------------------------------------

ERROR_INVALID = "invalid"  # 语法不对，或缺少必要片段
ERROR_NOT_FOUND = "not_found"  # 没这个 id / 别名 / 主机
ERROR_AMBIGUOUS = "ambiguous"  # 命中多个，需要写得更具体
ERROR_CLUSTER_ONLY = "cluster_only"  # 只定位到集群，还要再指定实例

_NUMERIC_RE = re.compile(r"^\d+$")
_HOST_PORT_RE = re.compile(r"^(?P<host>[^:]+)(?::(?P<port>\d{1,5}))?$")

_INSTANCE_SELECT = ("cluster", "cluster__department")


@dataclass
class ResolveResult:
    """解析结果。

    ``instance`` 有值即表示可以直接切换；否则看 ``error`` 与 ``candidates``。
    ``candidates`` 在歧义或"只定位到集群"时给出，供控制台列出来让用户选。
    """

    instance: Instance | None = None
    cluster: Cluster | None = None
    error: str | None = None
    message: str = ""
    candidates: list[Instance] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.instance is not None


# ---------------------------------------------------------------------------
# 查找辅助：先精确、再大小写不敏感（且只在不敏感命中也唯一时才采用）
# ---------------------------------------------------------------------------


def _find_cluster(token: str) -> Cluster | None:
    """按 id 或别名找集群。"""
    if _NUMERIC_RE.match(token):
        return Cluster.objects.select_related("department").filter(id=int(token)).first()

    exact = Cluster.objects.select_related("department").filter(alias=token).first()
    if exact is not None:
        return exact

    loose = list(
        Cluster.objects.select_related("department").filter(alias__iexact=token)[:2]
    )
    # 只在大小写不敏感命中也唯一时才采用，多命中交由上层报歧义
    return loose[0] if len(loose) == 1 else None


def _instances_of(cluster: Cluster) -> list[Instance]:
    return list(
        cluster.instances.select_related(*_INSTANCE_SELECT).order_by("id")
    )


def _find_instance_in_cluster(cluster: Cluster, token: str) -> Instance | None:
    """在指定集群内按 id 或别名找实例。"""
    qs = cluster.instances.select_related(*_INSTANCE_SELECT)

    if _NUMERIC_RE.match(token):
        return qs.filter(id=int(token)).first()

    exact = qs.filter(alias=token).first()
    if exact is not None:
        return exact

    loose = list(qs.filter(alias__iexact=token)[:2])
    return loose[0] if len(loose) == 1 else None


def _find_instances_by_alias(token: str) -> list[Instance]:
    """跨集群按别名找实例（实例别名只在集群内唯一，可能命中多个）。"""
    qs = Instance.objects.select_related(*_INSTANCE_SELECT)

    exact = list(qs.filter(alias=token))
    if exact:
        return exact

    return list(qs.filter(alias__iexact=token))


def _describe(instance: Instance) -> str:
    return (
        f"{instance.cluster.department.name}/{instance.cluster.name}/"
        f"{instance.name} ({instance.host}:{instance.port})"
    )


def _ambiguous_message(token: str, candidates: list[Instance]) -> str:
    names = "、".join(
        f"{c.cluster.alias or c.cluster.id}.{c.alias or c.id}" for c in candidates[:5]
    )
    return f"「{token}」匹配到 {len(candidates)} 个实例（{names}），请用 集群.实例 指定"


# ---------------------------------------------------------------------------
# 四种写法
# ---------------------------------------------------------------------------


def _resolve_single(token: str) -> ResolveResult:
    """不带分隔符：实例 id、集群别名、或实例别名。"""
    if _NUMERIC_RE.match(token):
        instance = (
            Instance.objects.select_related(*_INSTANCE_SELECT)
            .filter(id=int(token))
            .first()
        )
        if instance is not None:
            return ResolveResult(instance=instance, cluster=instance.cluster)

        # 实例 id 不存在时退一步当集群 id：'sw 1' 里的 1 也可能是集群
        cluster = _find_cluster(token)
        if cluster is None:
            return ResolveResult(
                error=ERROR_NOT_FOUND, message=f"没有 id={token} 的实例或集群"
            )
        members = _instances_of(cluster)
        if len(members) == 1:
            return ResolveResult(instance=members[0], cluster=cluster)
        if not members:
            return ResolveResult(
                cluster=cluster,
                error=ERROR_CLUSTER_ONLY,
                message=f"集群 {token} 下还没有实例",
            )
        return ResolveResult(
            cluster=cluster,
            error=ERROR_CLUSTER_ONLY,
            message=(
                f"集群 {token} 下有 {len(members)} 个实例，"
                f"请写成 {token}.<实例别名或id>"
            ),
            candidates=members,
        )

    # 集群别名优先：集群别名全局唯一，是比实例别名更强的定位符
    cluster = _find_cluster(token)
    if cluster is not None:
        members = _instances_of(cluster)
        if len(members) == 1:
            return ResolveResult(instance=members[0], cluster=cluster)
        if not members:
            return ResolveResult(
                cluster=cluster,
                error=ERROR_CLUSTER_ONLY,
                message=f"集群「{token}」下还没有实例",
            )
        return ResolveResult(
            cluster=cluster,
            error=ERROR_CLUSTER_ONLY,
            message=(
                f"集群「{token}」下有 {len(members)} 个实例，"
                f"请写成 {token}.<实例别名或id>"
            ),
            candidates=members,
        )

    # 再按实例别名跨集群找
    matches = _find_instances_by_alias(token)
    if not matches:
        return ResolveResult(
            error=ERROR_NOT_FOUND, message=f"找不到 id / 集群别名 / 实例别名「{token}」"
        )
    if len(matches) == 1:
        return ResolveResult(instance=matches[0], cluster=matches[0].cluster)
    return ResolveResult(
        error=ERROR_AMBIGUOUS,
        message=_ambiguous_message(token, matches),
        candidates=matches,
    )


def _resolve_alias_path(text: str) -> ResolveResult:
    """``集群.实例``：两段都可以是 id 或别名。"""
    left, _, right = text.partition(".")
    left, right = left.strip(), right.strip()
    if not left or not right:
        return ResolveResult(
            error=ERROR_INVALID,
            message="「集群.实例」两段都必填，例如 jq1.ins1 或 1.1",
        )

    cluster = _find_cluster(left)
    if cluster is None:
        return ResolveResult(
            error=ERROR_NOT_FOUND, message=f"找不到集群 id / 别名「{left}」"
        )

    instance = _find_instance_in_cluster(cluster, right)
    if instance is None:
        return ResolveResult(
            cluster=cluster,
            error=ERROR_NOT_FOUND,
            message=f"集群「{left}」下找不到实例 id / 别名「{right}」",
            candidates=_instances_of(cluster),
        )

    return ResolveResult(instance=instance, cluster=cluster)


def _resolve_by_host(text: str) -> ResolveResult:
    """``集群@主机[:端口]``：同一主机多端口时要求写端口。"""
    left, _, right = text.partition("@")
    left, right = left.strip(), right.strip()
    if not left or not right:
        return ResolveResult(
            error=ERROR_INVALID,
            message="「集群@主机」两段都必填，例如 jq1@10.0.12.1",
        )

    matched = _HOST_PORT_RE.match(right)
    if matched is None:
        return ResolveResult(
            error=ERROR_INVALID, message=f"主机地址「{right}」格式不对"
        )
    host = matched.group("host")
    port_text = matched.group("port")
    port = int(port_text) if port_text else None

    cluster = _find_cluster(left)
    if cluster is None:
        return ResolveResult(
            error=ERROR_NOT_FOUND, message=f"找不到集群 id / 别名「{left}」"
        )

    qs = cluster.instances.select_related(*_INSTANCE_SELECT).filter(host=host)
    if port is not None:
        qs = qs.filter(port=port)
    found = list(qs)

    if not found:
        return ResolveResult(
            cluster=cluster,
            error=ERROR_NOT_FOUND,
            message=f"集群「{left}」下没有主机为 {right} 的实例",
            candidates=_instances_of(cluster),
        )
    if len(found) > 1:
        ports = "、".join(str(i.port) for i in found)
        return ResolveResult(
            cluster=cluster,
            error=ERROR_AMBIGUOUS,
            message=(
                f"主机 {host} 在集群「{left}」下有多个端口（{ports}），"
                f"请写成 {left}@{host}:<端口>"
            ),
            candidates=found,
        )

    return ResolveResult(instance=found[0], cluster=cluster)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def resolve_target(raw: str) -> ResolveResult:
    """把控制台输入解析成一个实例。

    优先级：含 ``@`` 走主机匹配，含 ``.`` 走别名路径，否则当作单一标识。
    别名规则已禁止这些字符，因此这里的判断不会误伤合法别名。
    """
    text = (raw or "").strip()
    if not text:
        return ResolveResult(error=ERROR_INVALID, message="请提供要切换的目标")

    if "@" in text:
        return _resolve_by_host(text)
    if "." in text:
        return _resolve_alias_path(text)
    return _resolve_single(text)


def instance_summary(instance: Instance) -> dict:
    """控制台展示用的实例摘要。不含任何凭据内容。"""
    cluster = instance.cluster
    return {
        "id": instance.id,
        "name": instance.name,
        "alias": instance.alias or "",
        "label": f"{cluster.department.name}/{cluster.name}/{instance.name}",
        "cluster_id": cluster.id,
        "cluster_name": cluster.name,
        "cluster_alias": cluster.alias or "",
        "engine": cluster.engine,
        "env": cluster.get_env_display(),
        "endpoint": f"{instance.host}:{instance.port}",
        "username": instance.username,
        "password_set": instance.password_set,
        "status": instance.get_status_display(),
        "is_active": instance.is_active,
    }


def describe(instance: Instance) -> str:
    """一行可读描述，用于控制台回显。"""
    return _describe(instance)
