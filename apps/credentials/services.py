"""密码轮换服务。

职责分离：
- crypto.py    纯算法（加解密、生成、指纹）
- services.py  业务流程（读旧密码、写新密码、记历史、并发控制）
- tasks.py     Celery 包装（重试、日志、分批）

并发控制（PRD R5）采取双保险：
1. `select_for_update()` —— 在 PostgreSQL 上做行锁。
2. **乐观锁**：更新时带 `password_version=<读到的旧版本>` 条件，
   影响行数为 0 说明已被别处改过，直接判定为并发冲突。
   第 2 层是必需的：SQLite 不支持 SELECT ... FOR UPDATE，
   Django 在 SQLite 上会静默忽略它，只有版本号条件能真正防住竞争。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.assets.models import Instance, PasswordRotationLog
from apps.credentials.changers import PasswordChangeError, change_password
from apps.credentials.crypto import (
    DecryptionError,
    decrypt_secret,
    encrypt_secret,
    fingerprint,
    generate_password,
)
from common.exceptions import RotationInProgressError
from common.sqlite_writes import close_thread_connections, is_sqlite, write_lock
from common.timing import Stopwatch, log_if_slow

logger = logging.getLogger(__name__)

RotationSource = Literal["system", "manual"]

# 单批处理的实例数。过大则单次事务长、失败面大；过小则往返次数多。
DEFAULT_BATCH_SIZE = 100

# 支持行级并发写的后端（PostgreSQL 等）默认并发度。
# SQLite 是单写者模型，默认 1，理由见 rotate_all 的文档。
DEFAULT_CONCURRENCY = 4

# 单个实例轮换超过这个耗时说明有问题（正常在毫秒级）
SLOW_ROTATION_MS = 1000


def _record_failure(
    instance: Instance,
    source: RotationSource,
    old_fingerprint: str,
    new_fingerprint: str,
    exc: BaseException,
    duration_ms: int,
) -> None:
    """写一条 failed 历史。

    必须在业务事务**之外**调用：事务已回滚，若在事务内写，
    这条失败记录会跟着一起回滚，审计就丢了。

    error_message 只放异常类型与简述；本模块所有异常消息都不含
    明文或密文，因此可以安全落库。
    """
    try:
        PasswordRotationLog.objects.create(
            instance_id=instance.pk,
            old_fingerprint=old_fingerprint[:32],
            new_fingerprint=new_fingerprint[:32],
            source=source,
            status=PasswordRotationLog.Status.FAILED,
            error_message=f"{type(exc).__name__}: {str(exc)}"[:500],
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001 - 审计写入失败不能掩盖原始异常
        logger.exception("写入轮换失败记录时出错 instance_id=%s", instance.pk)


def rotate_one(
    instance: Instance,
    *,
    source: RotationSource = "system",
    password_length: int | None = None,
) -> PasswordRotationLog:
    """为单个实例生成新密码，在目标库上真正改掉，然后加密落库。

    三个阶段的顺序是有讲究的：

    1. **读**：短事务读出当前版本与旧明文，只用来连接目标库。
    2. **改**：连目标库执行改密命令（网络 IO）。**不放在事务里**——
       否则一次几秒的连接超时会把 SQLite 的写锁攥住几秒，整站写请求排队。
    3. **落**：改密成功后才本地写新密文并递增版本号。失败则只写 failed
       历史、**不递增版本**，避免出现"本地记录换了、目标库没换"的错位。

    Args:
        password_length: 新密码长度。缺省取 settings.PASSWORD_LENGTH，
            由轮换策略传入时使用策略里配置的档位。

    Raises:
        PasswordChangeError: 目标库改密失败（连不上、认证失败、权限不足等）。
        RotationInProgressError: 同一实例被并发轮换（版本号已变化）。
        其它异常原样抛出，但都会先写一条 failed 历史。
    """
    watch = Stopwatch(f"rotate-one:{instance.pk}")
    old_fingerprint = ""
    new_fingerprint = ""

    try:
        # ---- 阶段 1：读取快照（只读，尽快释放）--------------------------
        current = (
            Instance.objects.select_related("cluster")
            .filter(pk=instance.pk)
            .first()
        )
        if current is None:
            raise Instance.DoesNotExist(f"实例 {instance.pk} 不存在")

        base_version = current.password_version
        if not current.password_ciphertext:
            # 没有旧密码就没法在目标库上"改"它：MySQL/PG 的 ALTER USER
            # 都需要先以该账号登录。此时报错，而不是拿空密码去连、
            # 更不能本地造一个密码假装成功。
            raise PasswordChangeError(
                "no_password",
                "该实例尚未登记密码，无法连接目标库改密。请先在编辑页登记当前密码",
            )

        try:
            old_plaintext = decrypt_secret(current.password_ciphertext)
            old_fingerprint = fingerprint(old_plaintext)
        except DecryptionError:
            # 旧密码解不开就没法连目标库改密——只能报错，不能假装成功。
            # 这类情况通常是更换过 FIELD_ENCRYPTION_KEY。
            raise PasswordChangeError(
                "decrypt_failed",
                "旧密码无法解密，无法连接目标库改密。请在编辑页重新登记密码后再轮换",
            )

        # ---- 阶段 2：生成新密码并在目标库上改掉（无事务，纯网络 IO）-----
        # 新密码只存在于局部变量，不写日志、不进异常消息
        plaintext = generate_password(password_length)
        new_fingerprint = fingerprint(plaintext)

        change_password(current, old_plaintext, plaintext)

        # ---- 阶段 3：本地落库（短事务 + 乐观锁）-------------------------
        ciphertext = encrypt_secret(plaintext)
        with write_lock(), transaction.atomic():
            now = timezone.now()
            # 乐观锁：只有版本号仍是读到的值时才写入。
            # 走到这里目标库已经是新密码了，若版本对不上说明有另一处并发轮换，
            # 必须显式报错让运维介入，绝不能静默覆盖。
            updated = Instance.objects.filter(
                pk=current.pk, password_version=base_version
            ).update(
                password_ciphertext=ciphertext,
                password_version=base_version + 1,
                password_updated_at=now,
                updated_at=now,
            )
            if not updated:
                raise RotationInProgressError(
                    f"实例「{current.name}」在改密期间被另一处轮换，"
                    "目标库密码可能已变化，请重新验证凭据"
                )

            log = PasswordRotationLog.objects.create(
                instance_id=current.pk,
                old_fingerprint=old_fingerprint,
                new_fingerprint=new_fingerprint,
                source=source,
                status=PasswordRotationLog.Status.SUCCESS,
                duration_ms=watch.stop(),
            )

        log_if_slow(
            logger,
            watch,
            SLOW_ROTATION_MS,
            what=f"单实例轮换 {instance.pk}",
        )
        return log

    except Exception as exc:
        duration_ms = watch.stop()
        _record_failure(
            instance, source, old_fingerprint, new_fingerprint, exc, duration_ms
        )
        raise


def rotate_all(
    batch_size: int | None = None,
    *,
    concurrency: int | None = None,
    instance_ids: list[int] | None = None,
    password_length: int | None = None,
) -> dict[str, int]:
    """遍历目标实例分批轮换，单实例失败不中断整批（PRD AC-12）。

    Args:
        batch_size: 每批处理的实例数，缺省取 settings.ROTATION_BATCH_SIZE。
        concurrency: 并发度。缺省（None）按数据库后端自适应：
            - SQLite → 1（单写者模型，并发只会更慢，实测见下）
            - 其它（PostgreSQL 等）→ settings.ROTATION_CONCURRENCY 或 4
        instance_ids: 明确的待轮换实例 id 列表。缺省（None）表示"全部启用实例"，
            与改造前的行为一致；由轮换策略传入时即策略解析出的范围。
            注意：这里仍会再过滤一次 ``is_active=True``，防止调用方传入停用实例。
        password_length: 新密码长度，缺省取 settings.PASSWORD_LENGTH。

    **为什么 SQLite 的默认并发是 1**：本机实测 40 次写事务，
    并发度 1 / 4 / 8 分别耗时 0.25s / 0.35s / 0.58s。SQLite 同一时刻
    只允许一个写事务，多线程只是把排队搬到应用层，还要额外付出
    线程切换与锁竞争的开销。并发只在支持行级写的后端上才有意义。

    **并发安全**：每个实例的轮换都由 rotate_one 用乐观锁保证，
    即使两个 worker 同时抢到同一实例，后到者会拿到
    RotationInProgressError 并被计为失败，绝不会写出重复版本号。

    分批用「id 游标」而不是 `.iterator()`：本函数边读实例边写实例表，
    SQLite 上长时间持有读游标同时写同一张表容易出现
    "database table is locked"。按 id 分批每次都是独立查询，
    既避免全量载入内存，也不会与写入互相卡住。
    """
    if batch_size is None:
        batch_size = int(getattr(settings, "ROTATION_BATCH_SIZE", DEFAULT_BATCH_SIZE))
    if batch_size < 1:
        raise ValueError("batch_size 必须是正整数")

    workers = _resolve_concurrency(concurrency)
    target_ids = _normalize_target_ids(instance_ids)

    watch = Stopwatch("rotate-all")
    success = 0
    failed = 0
    last_id = 0
    processed = 0

    logger.info(
        "批量轮换开始：批大小 %d，并发度 %d（数据库 %s），范围 %s",
        batch_size,
        workers,
        "SQLite" if is_sqlite() else "非 SQLite",
        "指定 id" if target_ids is not None else "全部启用实例",
    )

    while True:
        batch_ids = list(
            Instance.objects.filter(is_active=True, id__gt=last_id)
            .filter(_scope_filter(target_ids))
            .order_by("id")
            .values_list("id", flat=True)[:batch_size]
        )
        if not batch_ids:
            break

        if workers == 1:
            # 串行：没有线程开销，SQLite 上这是最快路径
            ok, bad = _rotate_chunk(
                batch_ids, source="system", password_length=password_length
            )
        else:
            ok, bad = _rotate_concurrently(
                batch_ids, workers, password_length=password_length
            )

        success += ok
        failed += bad
        processed += len(batch_ids)
        last_id = batch_ids[-1]

        logger.info(
            "轮换进度：已处理 %d（成功 %d，失败 %d），当前耗时 %dms",
            processed,
            success,
            failed,
            watch.elapsed_ms,
        )

    total = success + failed
    logger.info(
        "批量轮换结束：成功 %d，失败 %d，共 %d，耗时 %dms",
        success,
        failed,
        total,
        watch.stop(),
    )
    return {"success": success, "failed": failed, "total": total}


def _normalize_target_ids(instance_ids: list[int] | None) -> list[int] | None:
    """把调用方给的范围整理成去重、稳定顺序的 id 列表。

    返回 None 表示"不限范围（全部启用实例）"，与空列表区分开：
    空列表意味着"范围内一个实例都没有"，此时不应退化成"全量轮换"——
    那是非常危险的静默行为。
    """
    if instance_ids is None:
        return None
    seen: list[int] = []
    for value in instance_ids:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number not in seen:
            seen.append(number)
    return seen


def _scope_filter(instance_ids: list[int] | None):
    """把范围拼成 ORM 条件。

    None → 不加条件（全部启用实例）；
    空列表 → 恒假条件，保证"范围内没有实例"时一条都不轮换。
    """
    if instance_ids is None:
        return Q()
    if not instance_ids:
        return Q(pk__in=[])
    return Q(pk__in=instance_ids)


def _resolve_concurrency(concurrency: int | None) -> int:
    """确定实际并发度，并按后端修正。"""
    if concurrency is not None:
        if concurrency < 1:
            raise ValueError("concurrency 必须是正整数")
        return concurrency

    configured = int(getattr(settings, "ROTATION_CONCURRENCY", 0) or 0)

    if is_sqlite():
        # 显式配成 >1 也不拦，但默认给 1，避免"配了并发反而更慢"
        return configured if configured > 0 else 1

    return configured if configured > 0 else DEFAULT_CONCURRENCY


def _rotate_chunk(
    ids: list[int],
    *,
    source: RotationSource,
    password_length: int | None = None,
) -> tuple[int, int]:
    """轮换一批实例，返回 (成功数, 失败数)。

    单条失败只计数不抛出——rotate_one 已经写好 failed 历史。
    结束时关闭本线程的数据库连接：线程池的工作线程会复用，
    但连接若不复位会一直挂在已结束的线程上。
    """
    ok = 0
    bad = 0
    owned_thread = threading.current_thread() is not threading.main_thread()

    try:
        for instance_id in ids:
            instance = Instance.objects.filter(pk=instance_id).first()
            if instance is None:
                # 批次查询与处理之间被删除，跳过即可
                continue
            try:
                rotate_one(instance, source=source, password_length=password_length)
                ok += 1
            except Exception:  # noqa: BLE001 - 单条失败不影响整批
                bad += 1
                logger.exception("实例 %s 轮换失败，继续处理后续实例", instance_id)
    finally:
        if owned_thread:
            close_thread_connections()

    return ok, bad


def _split_chunks(ids: list[int], workers: int) -> list[list[int]]:
    """把 id 列表切成 ``workers`` 份，每份非空。

    用 ``ids[i::workers]`` 交错切片而不是连续切块：实例的轮换耗时并不均匀，
    连续切块容易让某一份特别慢（例如刚好都是同一台慢库上的实例），
    交错能让每份的负载更均匀。
    """
    if not ids:
        return []

    workers = max(1, min(workers, len(ids)))
    return [chunk for chunk in (ids[i::workers] for i in range(workers)) if chunk]


def _rotate_concurrently(
    ids: list[int],
    workers: int,
    *,
    password_length: int | None = None,
) -> tuple[int, int]:
    """把 id 列表切成 ``workers`` 份，用线程池并行轮换。

    采用「每个 worker 一份工作清单」而不是「每个实例一个任务」：
    后者每条都要过一遍线程池的队列与结果对象，在实例多、单条耗时短时
    开销占比很高；分片则让每个 worker 连续处理自己那份，只需一次调度。
    """
    chunks = _split_chunks(ids, workers)
    if not chunks:
        return 0, 0

    ok = 0
    bad = 0

    with ThreadPoolExecutor(
        max_workers=len(chunks), thread_name_prefix="rotate"
    ) as pool:
        futures = [
            pool.submit(
                _rotate_chunk, chunk, source="system", password_length=password_length
            )
            for chunk in chunks
        ]
        for future in as_completed(futures):
            try:
                chunk_ok, chunk_bad = future.result()
            except Exception:  # noqa: BLE001 - 分片整体异常不应中断其它分片
                logger.exception("轮换分片异常退出")
                continue
            ok += chunk_ok
            bad += chunk_bad

    return ok, bad
