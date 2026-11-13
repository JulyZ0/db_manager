"""assets 接口契约：Schema 定义。

命名约定：
- *In      创建入参
- *Patch   部分更新入参（全部可选）
- *Out     出参（响应体）
- Paged*   分页包裹
"""

from __future__ import annotations

from datetime import datetime

from ninja import Schema
from pydantic import Field

from apps.assets.models import Cluster, Instance


# ---------------------------------------------------------------------------
# 部门
# ---------------------------------------------------------------------------


class DepartmentIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    code: str | None = Field(default=None, max_length=50)
    owner: str = ""
    description: str = ""
    is_active: bool = True


class DepartmentPatch(Schema):
    name: str | None = Field(default=None, max_length=100)
    code: str | None = Field(default=None, max_length=50)
    owner: str | None = None
    description: str | None = None
    is_active: bool | None = None


class DepartmentOut(Schema):
    id: int
    name: str
    code: str | None
    owner: str
    description: str
    is_active: bool
    cluster_count: int = 0
    created_at: datetime


class PagedDepartmentOut(Schema):
    count: int
    page: int
    page_size: int
    results: list[DepartmentOut]


# ---------------------------------------------------------------------------
# 集群
# ---------------------------------------------------------------------------


class ClusterIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    # 集群别名全局唯一，控制台里可单独作为前缀使用
    alias: str | None = Field(default=None, max_length=32)
    department_id: int
    engine: Cluster.Engine = Cluster.Engine.MYSQL
    env: Cluster.Env = Cluster.Env.DEV
    description: str = ""
    is_active: bool = True


class ClusterPatch(Schema):
    name: str | None = Field(default=None, max_length=100)
    alias: str | None = Field(default=None, max_length=32)
    department_id: int | None = None
    engine: Cluster.Engine | None = None
    env: Cluster.Env | None = None
    description: str | None = None
    is_active: bool | None = None


class ClusterOut(Schema):
    id: int
    name: str
    alias: str | None
    department_id: int
    department_name: str
    engine: str
    env: str
    description: str
    is_active: bool
    instance_count: int = 0
    created_at: datetime


class PagedClusterOut(Schema):
    count: int
    page: int
    page_size: int
    results: list[ClusterOut]


# ---------------------------------------------------------------------------
# 实例
# ---------------------------------------------------------------------------


class InstanceIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    # 实例别名在集群内唯一
    alias: str | None = Field(default=None, max_length=32)
    cluster_id: int
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    role: Instance.Role = Instance.Role.MASTER
    username: str = Field(min_length=1, max_length=64)
    # 只进不出：写入后由服务端加密落库，任何读接口都不返回
    password: str | None = None
    remark: str = ""
    is_active: bool = True


class InstancePatch(Schema):
    name: str | None = Field(default=None, max_length=100)
    alias: str | None = Field(default=None, max_length=32)
    cluster_id: int | None = None
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    role: Instance.Role | None = None
    username: str | None = Field(default=None, max_length=64)
    password: str | None = None
    remark: str | None = None
    is_active: bool | None = None


class InstanceOut(Schema):
    id: int
    name: str
    alias: str | None
    cluster_id: int
    cluster_name: str
    cluster_alias: str | None
    department_id: int
    host: str
    port: int
    role: str
    username: str
    # 密钥字段绝不出现，只暴露"是否已设置"与版本
    password_set: bool
    password_version: int
    password_updated_at: datetime | None
    status: str
    last_probed_at: datetime | None
    last_probe_ok: bool | None
    last_probe_latency_ms: int | None
    remark: str
    is_active: bool
    created_at: datetime


class PagedInstanceOut(Schema):
    count: int
    page: int
    page_size: int
    results: list[InstanceOut]


# ---------------------------------------------------------------------------
# 轮换历史
# ---------------------------------------------------------------------------


class RotationLogOut(Schema):
    id: int
    source: str
    status: str
    old_fingerprint: str
    new_fingerprint: str
    error_message: str
    duration_ms: int
    created_at: datetime


class PagedRotationLogOut(Schema):
    count: int
    page: int
    page_size: int
    results: list[RotationLogOut]
