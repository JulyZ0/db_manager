"""列表页模糊搜索的统一策略。

问题
----
``icontains`` 会生成 ``LIKE '%关键词%'``。前缀通配让 B-tree 索引完全失效，
数据库只能全表扫描。单表几千行时无感，上万行后每次搜索都是 O(N)，
并且叠加在分页的 ``COUNT(*)`` 上，慢得很隐蔽（页面看起来"就慢一点点"）。

两种可行的加速路径
------------------
1. **PostgreSQL + pg_trgm**：三元组 GIN 索引能让 ``ILIKE '%x%'`` 也走索引，
   且**完全保留**包含匹配的语义。由迁移
   ``apps/assets/migrations/0005_pg_trgm_search_indexes`` 建立（仅在
   PostgreSQL 上执行，SQLite 自动跳过）。
2. **前缀匹配**：把 ``%x%`` 改成 ``x%``。语义变严格（只能从开头匹配），
   但代价最低、不依赖任何扩展。适合"实例上万、运维只想快速定位"的场景。

因此这里做成可切换的：默认 ``contains`` 保持既有语义与测试不变，
部署到 PostgreSQL 后由索引兜底；若数据量再上一个量级、或跑在 SQLite 上
需要更快的模糊查询，把 ``SEARCH_MODE`` 设成 ``prefix`` 即可。

为什么集中到一处
----------------
六个列表页（部门/集群/实例，Web 与 API 各一套）此前各写各的
``Q(name__icontains=...) | Q(host__icontains=...)``。策略一旦要调整，
就得改六处并保证不遗漏——这类"同样的规则抄六遍"正是最容易慢慢跑偏的地方。
"""

from __future__ import annotations

from typing import Iterable

from django.conf import settings
from django.db.models import Q

# 支持的搜索模式
MODE_CONTAINS = "contains"
MODE_PREFIX = "prefix"
VALID_MODES = (MODE_CONTAINS, MODE_PREFIX)


def current_mode() -> str:
    """当前生效的搜索模式；非法值退回 contains（宁可慢，不可搜不到）。"""
    mode = str(getattr(settings, "SEARCH_MODE", MODE_CONTAINS) or "").strip().lower()
    return mode if mode in VALID_MODES else MODE_CONTAINS


def build_search_q(search: str, fields: Iterable[str], *, mode: str | None = None) -> Q:
    """为若干字段构造模糊匹配条件（字段之间是 OR）。

    Args:
        search: 用户输入的关键词。调用方应已 strip 并判空。
        fields: 参与匹配的字段路径，如 ``["name", "host"]`` 或
            ``["cluster__department__name"]``。
        mode: 覆盖全局 ``SEARCH_MODE``，主要用于测试。

    返回的 ``Q`` 在 fields 为空时是"恒假"条件 —— 与其退化成"匹配全部"
    把整表返回给用户，不如什么都不返回。
    """
    keyword = (search or "").strip()
    if not keyword:
        return _match_nothing()

    lookup = "istartswith" if (mode or current_mode()) == MODE_PREFIX else "icontains"

    condition = Q()
    has_field = False
    for field in fields:
        condition |= Q(**{f"{field}__{lookup}": keyword})
        has_field = True
    # 空的 Q() 在 Django 里等价于"匹配全部"。字段列表为空时必须显式返回
    # 恒假条件，否则调用方一个笔误（字段名写错、列表拼空）就会把整张表
    # 返回给用户——看起来是"搜索没生效"，实际是全量泄漏。
    return condition if has_field else _match_nothing()


def _match_nothing() -> Q:
    """恒假条件（与具体的模型无关）。"""
    return Q(pk__in=[])
