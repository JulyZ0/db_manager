"""集群拓扑的构建逻辑。

一份数据、两个出口：
- ``/api/v1/clusters/{id}/topology`` 供前端画布与外部系统消费；
- 集群详情页把它序列化进页面，既省一次请求，也让无 JS 环境仍能看到节点清单。

这里只做"读"：把 Cluster + Instance 的关系整理成 nodes / edges / warnings。
写入侧的约束（同集群、非自指）在 ``apps.assets.api._resolve_replica_upstream``
与 ``Instance.clean()`` 里，不在这里重复。
"""

from __future__ import annotations

from apps.assets.models import Cluster, Instance

# 一条复制边在图上怎么画，由 kind 区分；目前只有"复制"一种语义，
# 留成字段是为了将来能加"代理转发""分片路由"而不用改契约。
EDGE_REPLICATION = "replication"


def _build_nodes(cluster: Cluster) -> list[dict]:
    instances = (
        Instance.objects.filter(cluster=cluster)
        .select_related("replica_of")
        .order_by("role", "name")
    )
    nodes = []
    for obj in instances:
        nodes.append(
            {
                "id": obj.id,
                "name": obj.name,
                "alias": obj.alias,
                "host": obj.host,
                "port": obj.port,
                "role": obj.role,
                "status": obj.status,
                "is_active": obj.is_active,
                "password_set": obj.password_set,
                "has_upstream": obj.replica_of_id is not None,
                "upstream_name": obj.replica_of.name if obj.replica_of_id else "",
            }
        )
    return nodes


def _build_edges(cluster: Cluster) -> list[dict]:
    """从实例的 replica_of 反推有向边：source 从 target 同步。

    只收同集群的边。跨集群关系本应写不进来（接口与模型都拦），
    这里再过滤一次是防御性的：历史数据或直接改库都可能留下脏数据，
    宁可图上少画一条，也不要画出一条指向别的集群的线。
    """
    edges = []
    qs = (
        Instance.objects.filter(cluster=cluster, replica_of__isnull=False)
        .select_related("replica_of")
        .order_by("id")
    )
    for obj in qs:
        if obj.replica_of.cluster_id != cluster.id:
            continue
        edges.append(
            {
                "source": obj.id,
                "target": obj.replica_of_id,
                "kind": EDGE_REPLICATION,
            }
        )
    return edges


def _find_cycle_pairs(edges: list[dict]) -> set[tuple[int, int]]:
    """找出互为上游的实例对（双主的正常形态）。

    返回 {frozenset} 不好比较，用有序 tuple 的集合表示一对。
    """
    pairs = set()
    directed = {(e["source"], e["target"]) for e in edges}
    for source, target in directed:
        if (target, source) in directed:
            pairs.add((min(source, target), max(source, target)))
    return pairs


def _build_warnings(
    cluster: Cluster, nodes: list[dict], edges: list[dict]
) -> list[dict]:
    """架构声明与实际复制关系的软提示。

    一律只提示不阻断：新建集群时通常还没有实例，若在保存时硬校验，
    录入流程会被卡在"先有鸡还是先有蛋"上。
    """
    warnings: list[dict] = []
    topology = cluster.topology
    node_count = len(nodes)
    edge_count = len(edges)
    pairs = _find_cycle_pairs(edges)

    if topology == Cluster.Topology.STANDALONE:
        if edge_count:
            warnings.append(
                {
                    "code": "standalone_has_replication",
                    "message": (
                        f"集群声明为「单机」，但存在 {edge_count} 条复制关系。"
                        "若确实是主从/双主部署，请把集群架构改成对应类型。"
                    ),
                }
            )
    elif topology == Cluster.Topology.MASTER_SLAVE:
        if node_count > 1 and edge_count == 0:
            warnings.append(
                {
                    "code": "master_slave_without_links",
                    "message": (
                        "集群声明为「主从」，但没有任何实例填写了「复制上游」。"
                        "请在从库上指定它跟随的主库，拓扑图才能画出复制方向。"
                    ),
                }
            )
        if pairs:
            warnings.append(
                {
                    "code": "master_slave_with_cycle",
                    "message": (
                        "集群声明为「主从」，但存在互指上游的实例对（双主形态）。"
                        "若确实是双主部署，请把集群架构改成「双主」。"
                    ),
                }
            )
    elif topology == Cluster.Topology.DUAL_MASTER:
        if node_count >= 2 and not pairs:
            warnings.append(
                {
                    "code": "dual_master_without_pair",
                    "message": (
                        "集群声明为「双主」，但没有任何两台实例互指上游。"
                        "双主需要在两台主库的「复制上游」里互相填写对方。"
                    ),
                }
            )

    # 逐实例的一致性：角色标注与实际关系对不上时提示
    upstream_by_source = {e["source"]: e["target"] for e in edges}
    for node in nodes:
        if node["role"] == Instance.Role.SLAVE and not node["has_upstream"]:
            warnings.append(
                {
                    "code": "slave_without_upstream",
                    "message": f"「{node['name']}」标注为从库，但没有填写复制上游。",
                }
            )
        if node["role"] == Instance.Role.MASTER and node["has_upstream"]:
            # 双主是"两台主库互指"，这本身就该有上游；
            # 只有"主库指向了一个不指向它的上游"才值得提醒。
            upstream_id = upstream_by_source.get(node["id"])
            if upstream_id is not None and (
                min(node["id"], upstream_id),
                max(node["id"], upstream_id),
            ) in pairs:
                continue
            warnings.append(
                {
                    "code": "master_with_upstream",
                    "message": (
                        f"「{node['name']}」标注为主库，却填写了复制上游"
                        f"（{node['upstream_name']}）。双主场景两台主库互指是正常的，"
                        "其余情况请确认角色是否标错。"
                    ),
                }
            )

    return warnings


def build_topology(cluster: Cluster) -> dict:
    """把一个集群整理成完整的拓扑描述。"""
    nodes = _build_nodes(cluster)
    edges = _build_edges(cluster)
    warnings = _build_warnings(cluster, nodes, edges)

    return {
        "cluster_id": cluster.id,
        "cluster_name": cluster.name,
        "cluster_alias": cluster.alias,
        "topology": cluster.topology,
        "topology_label": cluster.get_topology_display(),
        "engine": cluster.engine,
        "env": cluster.env,
        "department_name": cluster.department.name,
        "nodes": nodes,
        "edges": edges,
        "warnings": warnings,
    }
