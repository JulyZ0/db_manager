"""集群架构与复制关系的模型层约束。

这里只测「数据能不能正确表达拓扑」这件事本身：
- 集群能声明架构类型；
- 实例能指向同集群内的另一台实例作为复制上游；
- 跨集群、自指这两类错误关系必须被拦住。
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from apps.assets.models import Cluster, Department, Instance

pytestmark = pytest.mark.django_db


@pytest.fixture
def topology_setup():
    """两个集群：A 用来放合法关系，B 用来验证跨集群被拒。"""
    dept = Department.objects.create(name="架构部")
    cluster = Cluster.objects.create(
        name="core-mysql", department=dept, topology=Cluster.Topology.MASTER_SLAVE
    )
    other = Cluster.objects.create(name="other-mysql", department=dept)

    master = Instance.objects.create(
        name="m1", alias="m1", cluster=cluster, host="10.0.0.1", port=3306, username="u"
    )
    return {"dept": dept, "cluster": cluster, "other": other, "master": master}


class TestClusterTopology:
    def test_default_topology_is_standalone(self, topology_setup):
        """没显式声明架构时按单机处理，避免存量数据出现空值歧义。"""
        assert topology_setup["other"].topology == Cluster.Topology.STANDALONE

    def test_topology_can_be_changed(self, topology_setup):
        cluster = topology_setup["cluster"]
        cluster.topology = Cluster.Topology.DUAL_MASTER
        cluster.save()

        cluster.refresh_from_db()
        assert cluster.topology == "dual_master"

    def test_topology_choices_cover_required_shapes(self):
        """题目要求能配主从/双主，这两项必须在选项里。"""
        values = {value for value, _ in Cluster.Topology.choices}
        assert {"standalone", "master_slave", "dual_master", "sharded"} <= values


class TestReplicaOf:
    def test_slave_can_point_to_master(self, topology_setup):
        master = topology_setup["master"]
        slave = Instance.objects.create(
            name="s1",
            cluster=topology_setup["cluster"],
            host="10.0.0.2",
            port=3306,
            username="u",
            role=Instance.Role.SLAVE,
            replica_of=master,
        )
        slave.full_clean()

        slave.refresh_from_db()
        assert slave.replica_of_id == master.id
        assert slave.upstream_name == "m1"
        assert list(master.replicas.all()) == [slave]

    def test_dual_master_is_mutual_reference(self, topology_setup):
        """双主不是"两台都留空"，而是两台互指上游。"""
        m1 = topology_setup["master"]
        m2 = Instance.objects.create(
            name="m2",
            cluster=topology_setup["cluster"],
            host="10.0.0.9",
            port=3306,
            username="u",
            role=Instance.Role.MASTER,
            replica_of=m1,
        )
        m1.replica_of = m2
        m1.save()

        m1.refresh_from_db()
        m2.refresh_from_db()
        assert m1.replica_of_id == m2.id
        assert m2.replica_of_id == m1.id

    def test_self_reference_rejected(self, topology_setup):
        master = topology_setup["master"]
        master.replica_of = master

        with pytest.raises(ValidationError) as exc:
            master.full_clean()
        assert "replica_of" in exc.value.message_dict

    def test_cross_cluster_reference_rejected(self, topology_setup):
        """从库必须跟同集群的主库，跨集群关系不在台账登记范围。"""
        master = topology_setup["master"]
        outsider = Instance.objects.create(
            name="x1",
            cluster=topology_setup["other"],
            host="10.0.0.3",
            port=3306,
            username="u",
            role=Instance.Role.SLAVE,
            replica_of=master,
        )

        with pytest.raises(ValidationError) as exc:
            outsider.full_clean()
        assert "replica_of" in exc.value.message_dict

    def test_upstream_delete_keeps_replica(self, topology_setup):
        """SET_NULL：主库被删，从库本身不该跟着消失，只是失去上游。"""
        master = topology_setup["master"]
        slave = Instance.objects.create(
            name="s2",
            cluster=topology_setup["cluster"],
            host="10.0.0.4",
            port=3306,
            username="u",
            role=Instance.Role.SLAVE,
            replica_of=master,
        )

        master.delete()

        slave.refresh_from_db()
        assert slave.replica_of_id is None
        assert Instance.objects.filter(id=slave.id).exists()

    def test_no_upstream_is_allowed(self, topology_setup):
        """单机/独立实例没有上游，clean 不应报错。"""
        topology_setup["master"].full_clean()


# ---------------------------------------------------------------------------
# API 契约：架构字段与复制上游
# ---------------------------------------------------------------------------


class TestTopologyAPI:
    def test_cluster_exposes_topology(self, api_client, topology_setup):
        cluster = topology_setup["cluster"]
        resp = api_client.get(f"/api/v1/clusters/{cluster.id}")

        assert resp.status_code == 200
        assert resp.json()["topology"] == "master_slave"

    def test_cluster_topology_is_patchable(self, api_client, topology_setup):
        cluster = topology_setup["cluster"]
        resp = api_client.patch(
            f"/api/v1/clusters/{cluster.id}",
            data={"topology": "dual_master"},
            content_type="application/json",
        )

        assert resp.status_code == 200
        assert resp.json()["topology"] == "dual_master"

    def test_create_instance_with_upstream(self, api_client, topology_setup):
        cluster = topology_setup["cluster"]
        master = topology_setup["master"]

        resp = api_client.post(
            "/api/v1/instances",
            data={
                "name": "s1",
                "cluster_id": cluster.id,
                "host": "10.0.0.2",
                "port": 3306,
                "username": "u",
                "role": "slave",
                "replica_of_id": master.id,
            },
            content_type="application/json",
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["replica_of_id"] == master.id
        assert body["replica_of_name"] == "m1"

    def test_cross_cluster_upstream_rejected(self, api_client, topology_setup):
        """跨集群复制不是台账该登记的事，接口层就要挡住。"""
        resp = api_client.post(
            "/api/v1/instances",
            data={
                "name": "x1",
                "cluster_id": topology_setup["other"].id,
                "host": "10.0.0.3",
                "port": 3306,
                "username": "u",
                "replica_of_id": topology_setup["master"].id,
            },
            content_type="application/json",
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["field"] == "replica_of_id"

    def test_missing_upstream_returns_404(self, api_client, topology_setup):
        resp = api_client.post(
            "/api/v1/instances",
            data={
                "name": "s9",
                "cluster_id": topology_setup["cluster"].id,
                "host": "10.0.0.8",
                "port": 3306,
                "username": "u",
                "replica_of_id": 999999,
            },
            content_type="application/json",
        )

        assert resp.status_code == 404

    def test_self_upstream_rejected(self, api_client, topology_setup):
        master = topology_setup["master"]
        resp = api_client.patch(
            f"/api/v1/instances/{master.id}",
            data={"replica_of_id": master.id},
            content_type="application/json",
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["field"] == "replica_of_id"

    def test_patch_null_clears_upstream(self, api_client, topology_setup):
        """显式传 null 表示解除复制关系——这是"改单机"的必经路径。"""
        master = topology_setup["master"]
        slave = Instance.objects.create(
            name="s3",
            cluster=topology_setup["cluster"],
            host="10.0.0.5",
            port=3306,
            username="u",
            role=Instance.Role.SLAVE,
            replica_of=master,
        )

        resp = api_client.patch(
            f"/api/v1/instances/{slave.id}",
            data={"replica_of_id": None},
            content_type="application/json",
        )

        assert resp.status_code == 200
        assert resp.json()["replica_of_id"] is None
        slave.refresh_from_db()
        assert slave.replica_of_id is None


# ---------------------------------------------------------------------------
# 拓扑数据契约：/api/v1/clusters/{id}/topology
# ---------------------------------------------------------------------------


@pytest.fixture
def topo_cluster():
    """主从形态：1 主 2 从，从库都指向主库。"""
    dept = Department.objects.create(name="数据平台部")
    cluster = Cluster.objects.create(
        name="order-mysql",
        department=dept,
        topology=Cluster.Topology.MASTER_SLAVE,
        env=Cluster.Env.PROD,
    )
    master = Instance.objects.create(
        name="order-m1",
        cluster=cluster,
        host="10.1.0.1",
        port=3306,
        username="u",
        role=Instance.Role.MASTER,
    )
    slaves = [
        Instance.objects.create(
            name=f"order-s{i}",
            cluster=cluster,
            host=f"10.1.0.{i + 1}",
            port=3306,
            username="u",
            role=Instance.Role.SLAVE,
            replica_of=master,
        )
        for i in (1, 2)
    ]
    return {"cluster": cluster, "master": master, "slaves": slaves}


class TestTopologyEndpoint:
    def test_returns_nodes_and_edges(self, api_client, topo_cluster):
        cluster = topo_cluster["cluster"]
        resp = api_client.get(f"/api/v1/clusters/{cluster.id}/topology")

        assert resp.status_code == 200
        body = resp.json()
        assert body["cluster_id"] == cluster.id
        assert body["topology"] == "master_slave"
        assert body["topology_label"] == "主从"
        assert len(body["nodes"]) == 3
        # 两个从库各一条指向主库的边
        assert len(body["edges"]) == 2
        for edge in body["edges"]:
            assert edge["target"] == topo_cluster["master"].id
            assert edge["kind"] == "replication"

    def test_node_carries_terminal_hint(self, api_client, topo_cluster):
        """节点要带 password_set，前端据此决定终端按钮是否可点。"""
        cluster = topo_cluster["cluster"]
        body = api_client.get(f"/api/v1/clusters/{cluster.id}/topology").json()

        slave_node = next(
            n for n in body["nodes"] if n["name"] == "order-s1"
        )
        assert slave_node["password_set"] is False
        assert slave_node["has_upstream"] is True
        assert slave_node["upstream_name"] == "order-m1"
        assert "password_ciphertext" not in slave_node

    def test_dual_master_produces_two_opposite_edges(self, api_client):
        """双主：两台互指，图上必须是两条方向相反的边。"""
        dept = Department.objects.create(name="交易部")
        cluster = Cluster.objects.create(
            name="trade-mysql", department=dept, topology=Cluster.Topology.DUAL_MASTER
        )
        a = Instance.objects.create(
            name="trade-a", cluster=cluster, host="10.2.0.1", port=3306, username="u"
        )
        b = Instance.objects.create(
            name="trade-b",
            cluster=cluster,
            host="10.2.0.2",
            port=3306,
            username="u",
            replica_of=a,
        )
        a.replica_of = b
        a.save()

        body = api_client.get(f"/api/v1/clusters/{cluster.id}/topology").json()

        edges = {(e["source"], e["target"]) for e in body["edges"]}
        assert edges == {(a.id, b.id), (b.id, a.id)}
        # 双主互指是正常形态，不该报警告
        assert body["warnings"] == []

    def test_empty_cluster_has_no_nodes(self, api_client):
        dept = Department.objects.create(name="空部")
        cluster = Cluster.objects.create(name="empty", department=dept)

        body = api_client.get(f"/api/v1/clusters/{cluster.id}/topology").json()

        assert body["nodes"] == []
        assert body["edges"] == []
        assert body["topology"] == "standalone"

    def test_missing_cluster_returns_404(self, api_client):
        resp = api_client.get("/api/v1/clusters/999999/topology")

        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"

    def test_topology_is_readable_anonymously(self, anon_client, topo_cluster):
        """拓扑与实例列表同属浏览信息，匿名也应能看。"""
        cluster = topo_cluster["cluster"]
        resp = anon_client.get(f"/api/v1/clusters/{cluster.id}/topology")

        assert resp.status_code == 200


class TestTopologyWarnings:
    def test_master_slave_without_links_warns(self, api_client):
        dept = Department.objects.create(name="告警部")
        cluster = Cluster.objects.create(
            name="ms-no-link", department=dept, topology=Cluster.Topology.MASTER_SLAVE
        )
        Instance.objects.create(
            name="a", cluster=cluster, host="10.3.0.1", port=3306, username="u"
        )
        Instance.objects.create(
            name="b", cluster=cluster, host="10.3.0.2", port=3306, username="u"
        )

        body = api_client.get(f"/api/v1/clusters/{cluster.id}/topology").json()

        codes = {w["code"] for w in body["warnings"]}
        assert "master_slave_without_links" in codes

    def test_dual_master_without_pair_warns(self, api_client):
        dept = Department.objects.create(name="告警部2")
        cluster = Cluster.objects.create(
            name="dm-no-pair", department=dept, topology=Cluster.Topology.DUAL_MASTER
        )
        Instance.objects.create(
            name="a", cluster=cluster, host="10.4.0.1", port=3306, username="u"
        )
        Instance.objects.create(
            name="b", cluster=cluster, host="10.4.0.2", port=3306, username="u"
        )

        body = api_client.get(f"/api/v1/clusters/{cluster.id}/topology").json()

        codes = {w["code"] for w in body["warnings"]}
        assert "dual_master_without_pair" in codes

    def test_standalone_with_replication_warns(self, api_client):
        dept = Department.objects.create(name="告警部3")
        cluster = Cluster.objects.create(
            name="sa-with-link", department=dept, topology=Cluster.Topology.STANDALONE
        )
        master = Instance.objects.create(
            name="a", cluster=cluster, host="10.5.0.1", port=3306, username="u"
        )
        Instance.objects.create(
            name="b",
            cluster=cluster,
            host="10.5.0.2",
            port=3306,
            username="u",
            role=Instance.Role.SLAVE,
            replica_of=master,
        )

        body = api_client.get(f"/api/v1/clusters/{cluster.id}/topology").json()

        codes = {w["code"] for w in body["warnings"]}
        assert "standalone_has_replication" in codes

    def test_slave_without_upstream_warns(self, api_client):
        dept = Department.objects.create(name="告警部4")
        cluster = Cluster.objects.create(name="slave-loose", department=dept)
        Instance.objects.create(
            name="lonely",
            cluster=cluster,
            host="10.6.0.1",
            port=3306,
            username="u",
            role=Instance.Role.SLAVE,
        )

        body = api_client.get(f"/api/v1/clusters/{cluster.id}/topology").json()

        assert any(w["code"] == "slave_without_upstream" for w in body["warnings"])

    def test_warnings_do_not_block_saving(self, api_client):
        """软提示：架构声明与关系不符时，接口仍应正常返回而不是报错。"""
        dept = Department.objects.create(name="告警部5")
        cluster = Cluster.objects.create(
            name="soft", department=dept, topology=Cluster.Topology.DUAL_MASTER
        )
        resp = api_client.post(
            "/api/v1/instances",
            data={
                "name": "only-one",
                "cluster_id": cluster.id,
                "host": "10.7.0.1",
                "port": 3306,
                "username": "u",
            },
            content_type="application/json",
        )

        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# 集群详情页：画布数据 + 软提示 + 无实例时的引导
# ---------------------------------------------------------------------------


class TestClusterDetailPage:
    def test_renders_canvas_and_inline_data(self, api_client, topo_cluster):
        cluster = topo_cluster["cluster"]
        resp = api_client.get(f"/clusters/{cluster.id}/")

        assert resp.status_code == 200
        html = resp.content.decode()
        # 画布容器 + 本地 cytoscape（无 CDN）
        assert 'id="topology-canvas"' in html
        assert "js/vendor/cytoscape.min.js" in html
        assert "js/topology.js" in html
        # 拓扑数据内联，首屏不必再请求一次
        assert 'id="topology-data"' in html
        assert "order-m1" in html

    def test_inline_data_matches_api(self, api_client, topo_cluster):
        """页面内联的数据必须和接口同源，否则会出现"图和清单对不上"。"""
        import json

        cluster = topo_cluster["cluster"]
        html = api_client.get(f"/clusters/{cluster.id}/").content.decode()

        start = html.index('id="topology-data"')
        payload = html[html.index(">", start) + 1 : html.index("</script>", start)]
        page_data = json.loads(payload)
        api_data = api_client.get(f"/api/v1/clusters/{cluster.id}/topology").json()

        assert page_data["nodes"] == api_data["nodes"]
        assert page_data["edges"] == api_data["edges"]

    def test_node_table_lists_every_instance(self, api_client, topo_cluster):
        cluster = topo_cluster["cluster"]
        html = api_client.get(f"/clusters/{cluster.id}/").content.decode()

        for name in ("order-m1", "order-s1", "order-s2"):
            assert name in html

    def test_warnings_are_shown_as_soft_hints(self, api_client):
        dept = Department.objects.create(name="提示部")
        cluster = Cluster.objects.create(
            name="ms-loose", department=dept, topology=Cluster.Topology.MASTER_SLAVE
        )
        Instance.objects.create(
            name="a", cluster=cluster, host="10.9.0.1", port=3306, username="u"
        )
        Instance.objects.create(
            name="b", cluster=cluster, host="10.9.0.2", port=3306, username="u"
        )

        html = api_client.get(f"/clusters/{cluster.id}/").content.decode()

        assert "alert-warning" in html
        assert "没有任何实例填写了「复制上游」" in html

    def test_empty_cluster_shows_guidance(self, api_client):
        dept = Department.objects.create(name="空集群部")
        cluster = Cluster.objects.create(name="nothing", department=dept)

        html = api_client.get(f"/clusters/{cluster.id}/").content.decode()

        assert "这个集群还没有实例" in html
        assert 'id="topology-canvas"' not in html

    def test_missing_cluster_returns_404(self, api_client):
        assert api_client.get("/clusters/999999/").status_code == 404

    def test_page_is_readable_anonymously(self, anon_client, topo_cluster):
        """集群详情属于浏览类页面，匿名可读；写操作仍在各自视图里拦。"""
        cluster = topo_cluster["cluster"]
        assert anon_client.get(f"/clusters/{cluster.id}/").status_code == 200

    def test_json_endpoint_matches_api(self, api_client, topo_cluster):
        cluster = topo_cluster["cluster"]
        page_json = api_client.get(f"/clusters/{cluster.id}/topology.json").json()
        api_json = api_client.get(f"/api/v1/clusters/{cluster.id}/topology").json()

        assert page_json["nodes"] == api_json["nodes"]
        assert page_json["edges"] == api_json["edges"]
