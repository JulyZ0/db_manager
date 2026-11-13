"""为模糊搜索建立 pg_trgm GIN 索引（仅 PostgreSQL）。

为什么需要
----------
列表页搜索用的是 ``icontains``，生成 ``ILIKE '%关键词%'``。前缀通配让
普通 B-tree 索引完全失效，只能全表扫描。pg_trgm 把文本切成三元组并建
GIN 索引，使**包含匹配也能走索引**，从而保留现有语义而不牺牲性能。

为什么不用 ``django.contrib.postgres.operations.TrigramExtension``
---------------------------------------------------------------
它会在不支持的后端上直接报错，而本项目默认跑 SQLite（开箱即用）。
这里用 ``RunPython`` + vendor 判断，SQLite 上整段跳过，不产生任何影响。

为什么容错
----------
``CREATE EXTENSION`` 需要超级用户权限。生产库的迁移账号常常没有该权限，
若因此让整个迁移失败，运维会被卡在部署第一步。这里降级为告警：
索引建不上只是"慢"，而迁移失败是"起不来"——后者严重得多。
此时应改用 ``SEARCH_MODE=prefix``（见 common/search.py），它不需要扩展。
"""

from __future__ import annotations

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

# (索引名, 表名, 列名)
_TRGM_INDEXES = (
    ("idx_trgm_department_name", "assets_department", "name"),
    ("idx_trgm_department_code", "assets_department", "code"),
    ("idx_trgm_cluster_name", "assets_cluster", "name"),
    ("idx_trgm_instance_name", "assets_instance", "name"),
    ("idx_trgm_instance_host", "assets_instance", "host"),
)


def _create_trgm_indexes(apps, schema_editor) -> None:
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        # SQLite 没有 pg_trgm；由 SEARCH_MODE 控制性能取舍
        return

    try:
        with connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception:  # noqa: BLE001 - 缺权限只降级，不阻断部署
        logger.warning(
            "无法创建 pg_trgm 扩展（通常是没有超级用户权限）。"
            "模糊搜索将回退为全表扫描；如需确定性性能，请改用 SEARCH_MODE=prefix。"
        )
        return

    for index_name, table, column in _TRGM_INDEXES:
        try:
            schema_editor.execute(
                f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{index_name}" '
                f'ON "{table}" USING gin ("{column}" gin_trgm_ops)'
            )
        except Exception:  # noqa: BLE001 - 单条索引失败不影响其余
            # CONCURRENTLY 失败会留下 invalid 索引，需人工 DROP 后重建
            logger.warning(
                "创建三元组索引 %s 失败，该字段搜索仍为全表扫描", index_name
            )


def _drop_trgm_indexes(apps, schema_editor) -> None:
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return

    for index_name, _table, _column in _TRGM_INDEXES:
        # 扩展本身不删：可能还有其它对象在用它
        schema_editor.execute(f'DROP INDEX IF EXISTS "{index_name}"')


class Migration(migrations.Migration):
    # CONCURRENTLY 不能在事务块内执行；同时它也避免建索引时长时间锁表
    atomic = False

    dependencies = [
        ("assets", "0004_cluster_topology_help_text"),
    ]

    operations = [
        migrations.RunPython(_create_trgm_indexes, _drop_trgm_indexes),
    ]
