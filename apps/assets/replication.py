"""按集群架构生成默认复制关系。

架构是集群级决策：运维先在集群上声明「单机 / 主从 / 双主 / 分片」，
这里据此把「谁复制谁」一次性铺好，再回到集群详情页微调个别实例。

为什么单独成模块而不是塞进 topology.py：
- ``topology.py`` 只做"读"（把现有关系整理成 nodes/edges），
  这里做的是"写"（给出建议关系并落库），读写分开便于各自测试；
- 页面、接口、将来的批量导入都要复用同一份建议逻辑。
"""

from __future__ import annotations

from apps.assets.models import Cluster, Instance

# 建议结果：{实例 id: 上游实例 id 或 None}
Plan = dict[int, "int | None"]


def _instances(cluster: Cluster) -> list[Instance]:
    return list(Instance.objects.filter(cluster=cluster).order_by("id"))


def suggest_upstreams(cluster: Cluster) -> tuple[Plan, list[str]]:
    """按架构给出建议的复制关系。

    返回 ``(plan, notes)``：``plan`` 只包含需要改动的实例，
    ``notes`` 是给运维看的说明（无法自动判定时说明原因）。

    分片架构刻意不自动生成：一组分片里谁和谁是一对主从，
    系统无从得知，猜错比不猜更糟。
    """
    instances = _instances(cluster)
    if not instances:
        return {}, ["集群里还没有实例，无法生成复制关系"]

    masters = [i for i in instances if i.role == Instance.Role.MASTER]
    slaves = [i for i in instances if i.role == Instance.Role.SLAVE]
    plan: Plan = {}
    notes: list[str] = []

    if cluster.topology == Cluster.Topology.STANDALONE:
        for obj in instances:
            if obj.replica_of_id is not None:
                plan[obj.id] = None
        if len(instances) > 1:
            notes.append(
                f"单机架构下不应存在复制关系；已清空 {len(instances)} 个实例的上游。"
                "若确实是多实例部署，请先把集群架构改成主从或双主。"
            )
        return plan, notes

    if cluster.topology == Cluster.Topology.MASTER_SLAVE:
        if not masters:
            return {}, ["集群里没有主库，无法确定从库跟随谁；请先把一台实例设为「主库」"]
        if len(masters) > 1:
            notes.append(
                f"集群里有 {len(masters)} 台主库，已选用 id 最小的一台"
                f"「{masters[0].name}」作为唯一上游。其余主库请人工确认。"
            )
        master = masters[0]
        for obj in instances:
            if obj.id == master.id:
                if obj.replica_of_id is not None:
                    plan[obj.id] = None
                continue
            if obj.role == Instance.Role.SLAVE:
                if obj.replica_of_id != master.id:
                    plan[obj.id] = master.id
            elif obj.replica_of_id is not None:
                # 代理等非从库角色不该有上游
                plan[obj.id] = None
        if not slaves:
            notes.append("集群里没有从库实例，本次只清理了多余的上游关系")
        return plan, notes

    if cluster.topology == Cluster.Topology.DUAL_MASTER:
        if len(masters) < 2:
            return {}, [
                f"双主需要至少两台主库，当前只有 {len(masters)} 台；"
                "请先把第二台实例的角色改成「主库」"
            ]
        first, second = masters[0], masters[1]
        if len(masters) > 2:
            notes.append(
                f"集群里有 {len(masters)} 台主库，只把「{first.name}」与"
                f"「{second.name}」配成互指；其余请人工编排。"
            )
        for obj in instances:
            if obj.id == first.id:
                plan[obj.id] = second.id
            elif obj.id == second.id:
                plan[obj.id] = first.id
            elif obj.replica_of_id is not None:
                plan[obj.id] = None
        return plan, notes

    if cluster.topology == Cluster.Topology.SHARDED:
        return {}, [
            "分片架构不自动生成复制关系：每组分片的主从归属系统无从推断，"
            "请在下表逐台指定。"
        ]

    return {}, [f"未知架构「{cluster.topology}」，未做任何改动"]


def apply_plan(cluster: Cluster, plan: Plan) -> int:
    """把建议关系写回数据库，返回实际改动的条数。

    用 ``queryset.update()`` 而不是逐条 ``save()``：这里只动 replica_of_id，
    不涉及别名归一化，也不需要触发 ``clean()``（角色没变，
    且上游已由调用方保证属于本集群、非自身）。
    """
    if not plan:
        return 0

    # 只允许指向本集群的实例，防止调用方传入跨集群 id 留下脏数据
    valid_ids = set(
        Instance.objects.filter(cluster=cluster).values_list("id", flat=True)
    )
    changed = 0
    for instance_id, upstream_id in plan.items():
        if instance_id not in valid_ids:
            continue
        if upstream_id is not None and upstream_id not in valid_ids:
            continue
        if upstream_id == instance_id:
            continue
        changed += Instance.objects.filter(id=instance_id).update(
            replica_of_id=upstream_id
        )
    return changed


def validate_plan(cluster: Cluster, plan: Plan) -> list[str]:
    """校验手工编排的关系，返回错误说明（空列表表示全部合法）。

    规则与 ``Instance.clean()`` 一致：不能指向自己、必须同集群。
    这里集中判定是为了在页面上一次性列出所有问题，而不是逐个报错。
    """
    errors: list[str] = []
    instances = {obj.id: obj for obj in _instances(cluster)}

    for instance_id, upstream_id in plan.items():
        obj = instances.get(instance_id)
        if obj is None:
            errors.append(f"实例 {instance_id} 不属于本集群")
            continue
        if upstream_id is None:
            continue
        if upstream_id == instance_id:
            errors.append(f"「{obj.name}」不能把复制上游指向自己")
        elif upstream_id not in instances:
            errors.append(f"「{obj.name}」的复制上游不属于本集群")
    return errors


def save_manual(cluster: Cluster, plan: Plan) -> int:
    """保存手工编排。先整体校验，任一条不合法就一条都不写。"""
    errors = validate_plan(cluster, plan)
    if errors:
        raise ValueError("；".join(errors))
    return apply_plan(cluster, plan)


def current_plan(cluster: Cluster) -> Plan:
    """当前实际关系，用于表单回填与"有无变化"的比较。"""
    return dict(
        Instance.objects.filter(cluster=cluster).values_list("id", "replica_of_id")
    )
