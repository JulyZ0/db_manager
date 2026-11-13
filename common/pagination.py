"""分页工具。

PRD §7.1 约定响应结构为 {count, page, page_size, results}。
这里提供纯函数，各 list 视图显式声明 page / page_size 查询参数后调用，
好处是 OpenAPI 文档里 results 的具体类型不会丢失。
"""

from __future__ import annotations

from typing import Any

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def resolve_page_params(page: int, page_size: int) -> tuple[int, int, int]:
    """归一化分页参数，返回 (page, page_size, offset)。"""
    page = max(1, page)
    page_size = min(MAX_PAGE_SIZE, max(1, page_size))
    return page, page_size, (page - 1) * page_size


def paginate_queryset(
    queryset: Any, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
) -> dict[str, Any]:
    """对 QuerySet 切片并组装分页响应体。

    超页兜底：page 超出总页数时返回空列表而不是报错——
    运维在筛选条件变化后回退浏览器历史时很容易落到不存在的页码。
    """
    page, page_size, offset = resolve_page_params(page, page_size)
    total = queryset.count()
    results = list(queryset[offset : offset + page_size])
    return {
        "count": total,
        "page": page,
        "page_size": page_size,
        "results": results,
    }
