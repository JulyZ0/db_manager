"""assets 接口层：部门 / 集群 / 实例的 CRUD 实现。

错误约定：
- 资源不存在 → raise NotFoundError（404）
- 名称/端点重复、存在子资源 → raise ConflictError（409）
- 统一由 common.api 的异常处理器转换为 {"code","message","detail"}

密钥约定：任何响应体都不得出现 password / password_ciphertext，
只暴露 password_set 与 password_version。
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, Q
from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Status

from apps.assets.models import Cluster, Department, Instance
from apps.assets.schemas import (
    ClusterIn,
    ClusterOut,
    ClusterPatch,
    DepartmentIn,
    DepartmentOut,
    DepartmentPatch,
    InstanceIn,
    InstanceOut,
    InstancePatch,
    PagedClusterOut,
    PagedDepartmentOut,
    PagedInstanceOut,
)
from common.auth import django_session_auth
from common.exceptions import ConflictError, NotFoundError
from common.pagination import paginate_queryset

departments_router = Router(tags=["部门"])
clusters_router = Router(tags=["集群"])
instances_router = Router(tags=["实例"])


# ---------------------------------------------------------------------------
# 序列化辅助：把模型对象转成响应 Schema
# ---------------------------------------------------------------------------


def _department_out(obj: Department) -> DepartmentOut:
    return DepartmentOut(
        id=obj.id,
        name=obj.name,
        code=obj.code,
        owner=obj.owner,
        description=obj.description,
        is_active=obj.is_active,
        cluster_count=getattr(obj, "cluster_count", 0),
        created_at=obj.created_at,
    )


def _cluster_out(obj: Cluster) -> ClusterOut:
    return ClusterOut(
        id=obj.id,
        name=obj.name,
        alias=obj.alias,
        department_id=obj.department_id,
        department_name=obj.department.name,
        engine=obj.engine,
        env=obj.env,
        description=obj.description,
        is_active=obj.is_active,
        instance_count=getattr(obj, "instance_count", 0),
        created_at=obj.created_at,
    )


def _instance_out(obj: Instance) -> InstanceOut:
    """注意：这里刻意不读取 password_ciphertext 的内容，只判断是否为空。"""
    return InstanceOut(
        id=obj.id,
        name=obj.name,
        alias=obj.alias,
        cluster_id=obj.cluster_id,
        cluster_name=obj.cluster.name,
        cluster_alias=obj.cluster.alias,
        department_id=obj.cluster.department_id,
        host=obj.host,
        port=obj.port,
        role=obj.role,
        username=obj.username,
        password_set=bool(obj.password_ciphertext),
        password_version=obj.password_version,
        password_updated_at=obj.password_updated_at,
        status=obj.status,
        last_probed_at=obj.last_probed_at,
        last_probe_ok=obj.last_probe_ok,
        last_probe_latency_ms=obj.last_probe_latency_ms,
        remark=obj.remark,
        is_active=obj.is_active,
        created_at=obj.created_at,
    )


def _get_department(department_id: int) -> Department:
    obj = Department.objects.filter(id=department_id).first()
    if obj is None:
        raise NotFoundError(f"部门 {department_id} 不存在")
    return obj


def _get_cluster(cluster_id: int) -> Cluster:
    obj = Cluster.objects.select_related("department").filter(id=cluster_id).first()
    if obj is None:
        raise NotFoundError(f"集群 {cluster_id} 不存在")
    return obj


def _get_instance(instance_id: int) -> Instance:
    obj = (
        Instance.objects.select_related("cluster", "cluster__department")
        .filter(id=instance_id)
        .first()
    )
    if obj is None:
        raise NotFoundError(f"实例 {instance_id} 不存在")
    return obj


def _clean_alias(raw: str | None) -> str | None:
    """校验别名格式并归一化。

    规则与模型层一致（``apps.assets.validators``），这里提前做一次是为了
    返回 422 的业务错误体，而不是让数据库约束抛 IntegrityError（500）。
    """
    from apps.assets.validators import normalize_alias, validate_alias

    alias = normalize_alias(raw)
    if alias is None:
        return None
    try:
        validate_alias(alias)
    except DjangoValidationError as exc:
        raise ConflictError(
            str(exc.messages[0]), detail={"field": "alias"}
        ) from exc
    return alias


def _assert_cluster_alias_free(alias: str | None, exclude_id: int | None = None) -> None:
    """集群别名全局唯一。"""
    if alias is None:
        return
    qs = Cluster.objects.filter(alias__iexact=alias)
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    if qs.exists():
        raise ConflictError(f"集群别名「{alias}」已被占用", detail={"field": "alias"})


def _assert_instance_alias_free(
    cluster_id: int, alias: str | None, exclude_id: int | None = None
) -> None:
    """实例别名在集群内唯一。"""
    if alias is None:
        return
    qs = Instance.objects.filter(cluster_id=cluster_id, alias__iexact=alias)
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    if qs.exists():
        raise ConflictError(
            f"该集群内已存在实例别名「{alias}」", detail={"field": "alias"}
        )


# ---------------------------------------------------------------------------


@departments_router.get("", response=PagedDepartmentOut, summary="部门列表")
def list_departments(
    request: HttpRequest,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="按名称/编码模糊匹配"),
    is_active: bool | None = Query(None),
):
    qs = Department.objects.annotate(cluster_count=Count("clusters"))

    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search))
    if is_active is not None:
        qs = qs.filter(is_active=is_active)

    qs = qs.order_by("id")
    page_data = paginate_queryset(qs, page, page_size)
    return PagedDepartmentOut(
        count=page_data["count"],
        page=page_data["page"],
        page_size=page_data["page_size"],
        results=[_department_out(o) for o in page_data["results"]],
    )


@departments_router.post(
    "", response={201: DepartmentOut}, summary="新建部门", auth=django_session_auth
)
def create_department(request: HttpRequest, payload: DepartmentIn):
    if Department.objects.filter(name=payload.name).exists():
        raise ConflictError(f"部门名称「{payload.name}」已存在", detail={"field": "name"})
    if payload.code and Department.objects.filter(code=payload.code).exists():
        raise ConflictError(f"部门编码「{payload.code}」已存在", detail={"field": "code"})

    obj = Department.objects.create(
        name=payload.name,
        code=payload.code or None,
        owner=payload.owner,
        description=payload.description,
        is_active=payload.is_active,
    )
    return Status(201, _department_out(obj))


@departments_router.get("/{department_id}", response=DepartmentOut, summary="部门详情")
def get_department(request: HttpRequest, department_id: int):
    obj = _get_department(department_id)
    obj.cluster_count = obj.clusters.count()
    return _department_out(obj)


@departments_router.patch(
    "/{department_id}",
    response=DepartmentOut,
    summary="更新部门",
    auth=django_session_auth,
)
def update_department(
    request: HttpRequest, department_id: int, payload: DepartmentPatch
):
    obj = _get_department(department_id)
    data = payload.model_dump(exclude_unset=True)

    new_name = data.get("name")
    if new_name and new_name != obj.name:
        if Department.objects.filter(name=new_name).exclude(id=obj.id).exists():
            raise ConflictError(f"部门名称「{new_name}」已存在", detail={"field": "name"})

    new_code = data.get("code")
    if new_code and new_code != obj.code:
        if Department.objects.filter(code=new_code).exclude(id=obj.id).exists():
            raise ConflictError(f"部门编码「{new_code}」已存在", detail={"field": "code"})

    for field, value in data.items():
        setattr(obj, field, value)
    obj.save()

    obj.cluster_count = obj.clusters.count()
    return _department_out(obj)


@departments_router.delete(
    "/{department_id}",
    response={204: None},
    summary="删除部门",
    auth=django_session_auth,
)
def delete_department(request: HttpRequest, department_id: int):
    obj = _get_department(department_id)
    cluster_count = obj.clusters.count()
    if cluster_count:
        raise ConflictError(
            f"部门下仍有 {cluster_count} 个集群，无法删除",
            detail={"cluster_count": cluster_count},
        )
    obj.delete()
    return Status(204, None)


# ---------------------------------------------------------------------------
# 集群 /api/v1/clusters
# ---------------------------------------------------------------------------


@clusters_router.get("", response=PagedClusterOut, summary="集群列表")
def list_clusters(
    request: HttpRequest,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    department_id: int | None = Query(None),
    env: str | None = Query(None),
    engine: str | None = Query(None),
    search: str | None = Query(None),
):
    qs = Cluster.objects.select_related("department").annotate(
        instance_count=Count("instances")
    )

    if department_id is not None:
        qs = qs.filter(department_id=department_id)
    if env:
        qs = qs.filter(env=env)
    if engine:
        qs = qs.filter(engine=engine)
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(department__name__icontains=search))

    qs = qs.order_by("id")
    page_data = paginate_queryset(qs, page, page_size)
    return PagedClusterOut(
        count=page_data["count"],
        page=page_data["page"],
        page_size=page_data["page_size"],
        results=[_cluster_out(o) for o in page_data["results"]],
    )


@clusters_router.post(
    "", response={201: ClusterOut}, summary="新建集群", auth=django_session_auth
)
def create_cluster(request: HttpRequest, payload: ClusterIn):
    department = Department.objects.filter(id=payload.department_id).first()
    if department is None:
        raise NotFoundError(f"部门 {payload.department_id} 不存在")

    if Cluster.objects.filter(
        department_id=payload.department_id, name=payload.name
    ).exists():
        raise ConflictError(
            f"部门「{department.name}」下已存在集群「{payload.name}」",
            detail={"field": "name", "department_id": payload.department_id},
        )

    alias = _clean_alias(payload.alias)
    _assert_cluster_alias_free(alias)

    obj = Cluster.objects.create(
        name=payload.name,
        alias=alias,
        department=department,
        engine=payload.engine,
        env=payload.env,
        description=payload.description,
        is_active=payload.is_active,
    )
    obj.instance_count = 0
    return Status(201, _cluster_out(obj))


@clusters_router.get("/{cluster_id}", response=ClusterOut, summary="集群详情")
def get_cluster(request: HttpRequest, cluster_id: int):
    obj = _get_cluster(cluster_id)
    obj.instance_count = obj.instances.count()
    return _cluster_out(obj)


@clusters_router.patch(
    "/{cluster_id}", response=ClusterOut, summary="更新集群", auth=django_session_auth
)
def update_cluster(request: HttpRequest, cluster_id: int, payload: ClusterPatch):
    obj = _get_cluster(cluster_id)
    data = payload.model_dump(exclude_unset=True)

    if "department_id" in data and data["department_id"] is not None:
        if not Department.objects.filter(id=data["department_id"]).exists():
            raise NotFoundError(f"部门 {data['department_id']} 不存在")

    target_dept = data.get("department_id", obj.department_id)
    target_name = data.get("name", obj.name)
    if (
        Cluster.objects.filter(department_id=target_dept, name=target_name)
        .exclude(id=obj.id)
        .exists()
    ):
        raise ConflictError(
            f"该部门下已存在集群「{target_name}」", detail={"field": "name"}
        )

    if "alias" in data:
        data["alias"] = _clean_alias(data["alias"])
        _assert_cluster_alias_free(data["alias"], exclude_id=obj.id)

    for field, value in data.items():
        setattr(obj, field, value)
    obj.save()
    obj = _get_cluster(obj.id)
    obj.instance_count = obj.instances.count()
    return _cluster_out(obj)


@clusters_router.delete(
    "/{cluster_id}", response={204: None}, summary="删除集群", auth=django_session_auth
)
def delete_cluster(request: HttpRequest, cluster_id: int):
    obj = _get_cluster(cluster_id)
    instance_count = obj.instances.count()
    if instance_count:
        raise ConflictError(
            f"集群下仍有 {instance_count} 个实例，无法删除",
            detail={"instance_count": instance_count},
        )
    obj.delete()
    return Status(204, None)


# ---------------------------------------------------------------------------
# 实例 /api/v1/instances
# ---------------------------------------------------------------------------


@instances_router.get("", response=PagedInstanceOut, summary="实例列表")
def list_instances(
    request: HttpRequest,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    cluster_id: int | None = Query(None),
    department_id: int | None = Query(None, description="跨层过滤：按部门查实例"),
    status: str | None = Query(None),
    role: str | None = Query(None),
    search: str | None = Query(None, description="按名称/主机模糊匹配"),
):
    qs = Instance.objects.select_related("cluster", "cluster__department")

    if cluster_id is not None:
        qs = qs.filter(cluster_id=cluster_id)
    if department_id is not None:
        qs = qs.filter(cluster__department_id=department_id)
    if status:
        qs = qs.filter(status=status)
    if role:
        qs = qs.filter(role=role)
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(host__icontains=search))

    qs = qs.order_by("id")
    page_data = paginate_queryset(qs, page, page_size)
    return PagedInstanceOut(
        count=page_data["count"],
        page=page_data["page"],
        page_size=page_data["page_size"],
        results=[_instance_out(o) for o in page_data["results"]],
    )


@instances_router.post(
    "", response={201: InstanceOut}, summary="新建实例", auth=django_session_auth
)
def create_instance(request: HttpRequest, payload: InstanceIn):
    cluster = Cluster.objects.select_related("department").filter(
        id=payload.cluster_id
    ).first()
    if cluster is None:
        raise NotFoundError(f"集群 {payload.cluster_id} 不存在")

    if Instance.objects.filter(
        cluster_id=payload.cluster_id, host=payload.host, port=payload.port
    ).exists():
        raise ConflictError(
            f"集群内已存在端点 {payload.host}:{payload.port}",
            detail={"field": "host_port"},
        )

    alias = _clean_alias(payload.alias)
    _assert_instance_alias_free(payload.cluster_id, alias)

    ciphertext = ""
    version = 0
    updated_at = None
    if payload.password:
        from apps.credentials.crypto import encrypt_secret

        ciphertext = encrypt_secret(payload.password)
        version = 1
        updated_at = timezone.now()

    obj = Instance.objects.create(
        name=payload.name,
        alias=alias,
        cluster=cluster,
        host=payload.host,
        port=payload.port,
        role=payload.role,
        username=payload.username,
        password_ciphertext=ciphertext,
        password_version=version,
        password_updated_at=updated_at,
        remark=payload.remark,
        is_active=payload.is_active,
    )
    return Status(201, _instance_out(obj))


@instances_router.get("/{instance_id}", response=InstanceOut, summary="实例详情")
def get_instance(request: HttpRequest, instance_id: int):
    return _instance_out(_get_instance(instance_id))


@instances_router.patch(
    "/{instance_id}", response=InstanceOut, summary="更新实例", auth=django_session_auth
)
def update_instance(request: HttpRequest, instance_id: int, payload: InstancePatch):
    obj = _get_instance(instance_id)
    data = payload.model_dump(exclude_unset=True)

    password = data.pop("password", None)

    if "cluster_id" in data and data["cluster_id"] is not None:
        cluster = Cluster.objects.filter(id=data["cluster_id"]).first()
        if cluster is None:
            raise NotFoundError(f"集群 {data['cluster_id']} 不存在")
        obj.cluster = cluster

    target_cluster_id = data.get("cluster_id", obj.cluster_id)
    target_host = data.get("host", obj.host)
    target_port = data.get("port", obj.port)
    if (
        Instance.objects.filter(
            cluster_id=target_cluster_id, host=target_host, port=target_port
        )
        .exclude(id=obj.id)
        .exists()
    ):
        raise ConflictError(
            f"集群内已存在端点 {target_host}:{target_port}",
            detail={"field": "host_port"},
        )

    if "alias" in data:
        data["alias"] = _clean_alias(data["alias"])
        _assert_instance_alias_free(
            target_cluster_id, data["alias"], exclude_id=obj.id
        )

    data.pop("cluster_id", None)
    for field, value in data.items():
        setattr(obj, field, value)

    if password:
        from apps.credentials.crypto import encrypt_secret

        obj.password_ciphertext = encrypt_secret(password)
        obj.password_version += 1
        obj.password_updated_at = timezone.now()

    obj.save()
    obj = _get_instance(obj.id)
    return _instance_out(obj)


@instances_router.delete(
    "/{instance_id}",
    response={204: None},
    summary="删除实例",
    auth=django_session_auth,
)
def delete_instance(request: HttpRequest, instance_id: int):
    obj = _get_instance(instance_id)
    # 轮换历史通过 FK on_delete=CASCADE 自动清理
    obj.delete()
    return Status(204, None)
