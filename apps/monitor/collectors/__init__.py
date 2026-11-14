"""各引擎的指标采集器。

按引擎分派，避免调用方写 if/elif。注册表用惰性导入：某个引擎的驱动没装
时，其它引擎的采集不受影响——这在生产里很实际，例如只用 MySQL 却装了
Redis 依赖缺失的情况不该让整个采集任务起不来。
"""

from __future__ import annotations

from typing import Callable

from apps.assets.models import Cluster, Instance
from apps.monitor.collectors.base import CollectResult

__all__ = ["collect_for", "supported_engines"]

Collector = Callable[[Instance, str, int], CollectResult]

# 引擎 → (模块路径, 函数名)。用字符串而不是直接 import，理由见模块说明。
_REGISTRY: dict[str, tuple[str, str]] = {
    Cluster.Engine.MYSQL: ("apps.monitor.collectors.mysql", "collect"),
    Cluster.Engine.POSTGRESQL: ("apps.monitor.collectors.postgresql", "collect"),
    Cluster.Engine.REDIS: ("apps.monitor.collectors.redis_collector", "collect"),
}


def supported_engines() -> list[str]:
    """当前代码支持采集的引擎列表。"""
    return list(_REGISTRY)


def _load(engine: str) -> Collector | None:
    entry = _REGISTRY.get(engine)
    if entry is None:
        return None
    module_path, func_name = entry
    try:
        module = __import__(module_path, fromlist=[func_name])
    except ImportError:
        # 驱动缺失时，采集器模块自身仍可导入（它把 import 放在函数内），
        # 这里兜住的是模块文件不存在的情况。
        return None
    return getattr(module, func_name, None)


def collect_for(instance: Instance, password: str, timeout: int) -> CollectResult:
    """按实例所属集群的引擎分派采集。

    引擎取自 ``cluster.engine`` 而非实例上的字段：引擎是集群级属性，
    同一集群的实例必然同构（这是台账建模时的前提）。
    """
    engine = instance.cluster.engine
    collector = _load(engine)
    if collector is None:
        from apps.monitor.collectors.base import ERROR_UNSUPPORTED

        return CollectResult.failed(ERROR_UNSUPPORTED)
    return collector(instance, password, timeout)
