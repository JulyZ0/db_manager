"""别名解析器测试。

覆盖 ``sw`` 支持的六种写法，以及"找不到/歧义/只定位到集群"三类失败。
"""

from __future__ import annotations

import pytest

from apps.assets import resolver
from apps.assets.models import Cluster, Department, Instance


@pytest.fixture
def assets(db):
    """两个部门、两个集群、一个多实例集群，用于构造各种歧义场景。"""
    dept = Department.objects.create(name="基础架构部", code="INFRA")
    other = Department.objects.create(name="数据平台部", code="DATA")

    jq1 = Cluster.objects.create(
        name="交易主库", alias="jq1", department=dept, engine="mysql", env="prod"
    )
    jq2 = Cluster.objects.create(
        name="缓存集群", alias="jq2", department=dept, engine="redis", env="prod"
    )
    # 另一个部门下也有个叫 ins1 的实例别名，用来制造跨集群歧义
    dq1 = Cluster.objects.create(
        name="分析库", alias="dq1", department=other, engine="postgresql", env="test"
    )

    def make(cluster, name, alias, host, port):
        return Instance.objects.create(
            name=name,
            alias=alias,
            cluster=cluster,
            host=host,
            port=port,
            username="app",
        )

    i1 = make(jq1, "交易主", "ins1", "10.0.12.1", 3306)
    i2 = make(jq1, "交易从", "ins2", "10.0.12.1", 3307)
    i3 = make(jq2, "缓存一", None, "10.0.12.20", 6379)
    i4 = make(dq1, "分析一", "ins1", "10.0.13.1", 5432)

    return {
        "dept": dept,
        "jq1": jq1,
        "jq2": jq2,
        "dq1": dq1,
        "i1": i1,
        "i2": i2,
        "i3": i3,
        "i4": i4,
    }


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


def test_resolve_by_instance_id(assets):
    result = resolver.resolve_target(str(assets["i1"].id))
    assert result.ok
    assert result.instance.id == assets["i1"].id


def test_resolve_by_cluster_and_instance_alias(assets):
    result = resolver.resolve_target("jq1.ins1")
    assert result.ok
    assert result.instance.id == assets["i1"].id


def test_resolve_by_numeric_path(assets):
    """1.1 形式：集群 id . 实例 id。"""
    text = f"{assets['jq1'].id}.{assets['i2'].id}"
    result = resolver.resolve_target(text)
    assert result.ok
    assert result.instance.id == assets["i2"].id


def test_resolve_mixed_alias_and_id(assets):
    """集群用别名、实例用 id，或反之，都应当成立。"""
    assert resolver.resolve_target(f"jq1.{assets['i2'].id}").instance.id == assets["i2"].id
    assert (
        resolver.resolve_target(f"{assets['jq1'].id}.ins2").instance.id == assets["i2"].id
    )


def test_resolve_by_cluster_at_host(assets):
    result = resolver.resolve_target("jq1@10.0.12.1:3307")
    assert result.ok
    assert result.instance.id == assets["i2"].id


def test_resolve_by_cluster_at_host_without_port_when_unique(assets):
    """同集群该主机只有一个实例时端口可省略。"""
    result = resolver.resolve_target("jq2@10.0.12.20")
    assert result.ok
    assert result.instance.id == assets["i3"].id


def test_resolve_sole_instance_cluster_alias(assets):
    """集群下只有一个实例时，直接写集群别名即可选中。"""
    result = resolver.resolve_target("jq2")
    assert result.ok
    assert result.instance.id == assets["i3"].id


def test_resolve_by_bare_instance_alias(assets):
    """实例别名跨集群唯一时可直接用。"""
    result = resolver.resolve_target("ins2")
    assert result.ok
    assert result.instance.id == assets["i2"].id


def test_resolve_is_case_insensitive_when_unique(assets):
    result = resolver.resolve_target("JQ1.INS1")
    assert result.ok
    assert result.instance.id == assets["i1"].id


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


def test_empty_target_is_invalid(assets):
    for text in ("", "   "):
        result = resolver.resolve_target(text)
        assert result.error == resolver.ERROR_INVALID
        assert not result.ok


def test_unknown_token_is_not_found(assets):
    result = resolver.resolve_target("nosuchthing")
    assert result.error == resolver.ERROR_NOT_FOUND


def test_unknown_instance_id_is_not_found(assets):
    result = resolver.resolve_target("99999")
    assert result.error == resolver.ERROR_NOT_FOUND


def test_cluster_with_many_instances_asks_for_more(assets):
    """jq1 有两个实例，只写集群别名应该报 cluster_only 并给出候选。"""
    result = resolver.resolve_target("jq1")
    assert result.error == resolver.ERROR_CLUSTER_ONLY
    assert len(result.candidates) == 2
    assert "jq1.<实例别名或id>" in result.message


def test_ambiguous_bare_alias(assets):
    """ins1 在两个集群下都存在，裸别名必须报歧义而不是随便挑一个。"""
    result = resolver.resolve_target("ins1")
    assert result.error == resolver.ERROR_AMBIGUOUS
    assert {c.id for c in result.candidates} == {assets["i1"].id, assets["i4"].id}


def test_host_with_multiple_ports_is_ambiguous(assets):
    result = resolver.resolve_target("jq1@10.0.12.1")
    assert result.error == resolver.ERROR_AMBIGUOUS
    assert "3306" in result.message and "3307" in result.message


def test_host_not_in_cluster(assets):
    result = resolver.resolve_target("jq1@10.9.9.9")
    assert result.error == resolver.ERROR_NOT_FOUND


def test_malformed_alias_path(assets):
    for text in ("jq1.", ".ins1", "."):
        result = resolver.resolve_target(text)
        assert result.error == resolver.ERROR_INVALID, text


def test_malformed_host_syntax(assets):
    result = resolver.resolve_target("@10.0.12.1")
    assert result.error == resolver.ERROR_INVALID


def test_numeric_token_prefers_instance_id(assets):
    """数字优先当实例 id：jq1.id 恰好等于 i1.id 时，应选中实例而非集群。"""
    result = resolver.resolve_target(str(assets["jq1"].id))
    assert result.ok
    assert result.instance.id == assets["jq1"].id


def test_numeric_id_falls_back_to_cluster(db):
    """实例 id 不存在时退一步按集群 id 找。

    单独构造数据：两个集群，只有第一个有实例，
    于是第二个集群的 id 不与任何实例 id 重叠。
    """
    dept = Department.objects.create(name="D", code="D")
    c1 = Cluster.objects.create(name="c1", department=dept)
    c2 = Cluster.objects.create(name="c2", department=dept)
    Instance.objects.create(
        name="a", cluster=c1, host="1.1.1.1", port=3306, username="u"
    )

    # 第一个集群的 id 与实例 id 相同 → 优先返回实例
    assert resolver.resolve_target(str(c1.id)).instance is not None

    # 第二个集群没有实例 → 落到集群分支
    result = resolver.resolve_target(str(c2.id))
    assert result.error == resolver.ERROR_CLUSTER_ONLY
    assert result.cluster.id == c2.id


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------


def test_instance_summary_has_no_secrets(assets):
    summary = resolver.instance_summary(assets["i1"])
    assert summary["alias"] == "ins1"
    assert summary["cluster_alias"] == "jq1"
    assert summary["password_set"] is False
    # 任何凭据内容都不得出现在摘要里
    assert "password" not in summary
    assert "password_ciphertext" not in summary


def test_instance_summary_reports_password_presence(assets):
    instance = assets["i1"]
    instance.password_ciphertext = "v1:whatever"
    instance.save(update_fields=["password_ciphertext"])

    summary = resolver.instance_summary(instance)
    assert summary["password_set"] is True
    assert "v1:whatever" not in str(summary)
