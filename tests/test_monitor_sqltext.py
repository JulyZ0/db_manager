"""SQL 归一化测试。

归一化是 SQL 洞察的地基：算错一点，聚合就全错——要么同一语句散成几十行，
要么不同语句被错误合并。因此这里逐条钉住行为。
"""

from __future__ import annotations

import pytest

from apps.monitor.sqltext import digest_of, normalize_sql


def test_same_statement_different_params_collapses():
    a = normalize_sql("SELECT * FROM orders WHERE id = 1")
    b = normalize_sql("SELECT * FROM orders WHERE id = 2")
    assert a == b
    assert digest_of("SELECT * FROM orders WHERE id = 1") == digest_of(
        "SELECT * FROM orders WHERE id = 2"
    )


def test_string_literal_replaced():
    result = normalize_sql("SELECT * FROM users WHERE phone = '13800138000'")
    assert "13800138000" not in result
    assert "?" in result


def test_string_with_embedded_digits_fully_replaced():
    """字符串里的数字不能被单独替换，否则会留下 'abc?' 这种怪模板。"""
    result = normalize_sql("SELECT * FROM t WHERE code = 'abc123'")
    assert "abc123" not in result
    assert "abc?" not in result


def test_escaped_quote_inside_string():
    result = normalize_sql(r"SELECT * FROM t WHERE name = 'O\'Brien'")
    assert "Brien" not in result


def test_double_quoted_replaced():
    result = normalize_sql('SELECT * FROM t WHERE name = "secret_value"')
    assert "secret_value" not in result


def test_in_list_collapsed():
    two = normalize_sql("SELECT * FROM t WHERE id IN (1, 2)")
    three = normalize_sql("SELECT * FROM t WHERE id IN (1, 2, 3)")
    assert two == three
    assert two.count("?") == 1


def test_whitespace_and_newlines_collapsed():
    multiline = "SELECT *\n  FROM t\n WHERE id = 1"
    single = "SELECT * FROM t WHERE id = 1"
    assert normalize_sql(multiline) == normalize_sql(single)


def test_identifier_with_digits_not_mangled():
    """col1 是标识符，不能被当成数字字面量咬掉。"""
    result = normalize_sql("SELECT col1 FROM t2 WHERE x = 5")
    assert "col1" in result
    assert "t2" in result


def test_hex_literal_replaced():
    result = normalize_sql("SELECT * FROM t WHERE b = 0xFF")
    assert "0xFF" not in result


def test_idempotent():
    """归一化必须幂等，否则同一模板再次入库会算出第二个指纹。"""
    once = normalize_sql("SELECT * FROM t WHERE id = 42 AND name = 'x'")
    twice = normalize_sql(once)
    assert once == twice


def test_digest_is_stable_across_calls():
    sql = "SELECT * FROM t WHERE id = 7"
    assert digest_of(sql) == digest_of(sql)
    assert len(digest_of(sql)) == 40


def test_different_statements_have_different_digest():
    assert digest_of("SELECT * FROM a WHERE id = 1") != digest_of(
        "SELECT * FROM b WHERE id = 1"
    )


def test_empty_input():
    assert normalize_sql("") == ""
    assert digest_of("") == digest_of("")


def test_long_template_truncated():
    from apps.monitor.sqltext import MAX_TEMPLATE_LENGTH

    long_sql = "SELECT " + ", ".join(f"c{i}" for i in range(2000)) + " FROM t"
    assert len(normalize_sql(long_sql)) <= MAX_TEMPLATE_LENGTH


@pytest.mark.django_db
def test_digest_unique_per_instance(db):
    """同一实例同一模板只能一行——采集侧的 upsert 依赖这个约束。"""
    from django.db import IntegrityError, transaction

    from apps.assets.models import Cluster, Department, Instance
    from apps.monitor.models import SqlDigest

    dept = Department.objects.create(name="SQL 洞察部")
    cluster = Cluster.objects.create(name="洞察集群", department=dept, engine="mysql")
    inst = Instance.objects.create(
        name="sql-1", cluster=cluster, host="10.7.0.1", port=3306, username="mon"
    )
    from django.utils import timezone

    now = timezone.now()
    digest = digest_of("SELECT * FROM t WHERE id = 1")
    SqlDigest.objects.create(
        instance=inst,
        engine="mysql",
        digest=digest,
        template="SELECT * FROM t WHERE id = ?",
        first_seen_at=now,
        last_seen_at=now,
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            SqlDigest.objects.create(
                instance=inst,
                engine="mysql",
                digest=digest,
                template="SELECT * FROM t WHERE id = ?",
                first_seen_at=now,
                last_seen_at=now,
            )


@pytest.mark.django_db
def test_same_digest_allowed_across_instances(db):
    from django.utils import timezone

    from apps.assets.models import Cluster, Department, Instance
    from apps.monitor.models import SqlDigest

    dept = Department.objects.create(name="跨实例部")
    cluster = Cluster.objects.create(name="跨实例集群", department=dept, engine="mysql")
    a = Instance.objects.create(
        name="a", cluster=cluster, host="10.7.1.1", port=3306, username="mon"
    )
    b = Instance.objects.create(
        name="b", cluster=cluster, host="10.7.1.2", port=3306, username="mon"
    )
    now = timezone.now()
    digest = digest_of("SELECT * FROM t WHERE id = 1")
    for inst in (a, b):
        SqlDigest.objects.create(
            instance=inst,
            engine="mysql",
            digest=digest,
            template="SELECT * FROM t WHERE id = ?",
            first_seen_at=now,
            last_seen_at=now,
        )

    assert SqlDigest.objects.count() == 2
