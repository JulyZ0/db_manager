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
from typing import Literal

from django.db import transaction
from django.utils import timezone

from apps.assets.models import Instance, PasswordRotationLog
from apps.credentials.crypto import (
    DecryptionError,
    decrypt_secret,
    encrypt_secret,
    fingerprint,
    generate_password,
)
from common.exceptions import RotationInProgressError
from common.timing import Stopwatch, log_if_slow

logger = logging.getLogger(__name__)

RotationSource = Literal["system", "manual"]

# 单批处理的实例数。过大则单次事务长、失败面大；过小则往返次数多。
DEFAULT_BATCH_SIZE = 100

# 单个实例轮换超过这个耗时说明有问题（正常在毫秒级）
SLOW_ROTATION_MS = 1000


def _current_fingerprint(ciphertext: str) -> str:
    """尽力算出旧密码的指纹。

    解密失败（例如中途换过 FIELD_ENCRYPTION_KEY）不能阻断轮换——
    把旧密码换成新密码恰恰能修复这种情况，只是旧指纹留空。
    """
    if not ciphertext:
        return ""
    try:
        return fingerprint(decrypt_secret(ciphertext))
    except DecryptionError:
        logger.warning("旧密码无法解密，本次轮换不记录旧指纹（可能更换过加密密钥）")
        return ""


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
) -> PasswordRotationLog:
    """为单个实例生成并写入新密码，返回本次轮换历史记录。

    Raises:
        RotationInProgressError: 同一实例被并发轮换（版本号已变化）。
        其它异常原样抛出，但会先写一条 failed 历史。
    """
    watch = Stopwatch(f"rotate-one:{instance.pk}")
    old_fingerprint = ""
    new_fingerprint = ""

    try:
        with transaction.atomic():
            # 行锁：PostgreSQL 生效，SQLite 忽略
            locked = (
                Instance.objects.select_for_update().filter(pk=instance.pk).first()
            )
            if locked is None:
                raise Instance.DoesNotExist(f"实例 {instance.pk} 不存在")

            base_version = locked.password_version
            old_fingerprint = _current_fingerprint(locked.password_ciphertext)

            # 新密码只存在于局部变量，不写日志、不进异常消息
            plaintext = generate_password()
            new_fingerprint = fingerprint(plaintext)
            ciphertext = encrypt_secret(plaintext)

            now = timezone.now()
            # 乐观锁：只有版本号仍是读到的值时才写入
            updated = Instance.objects.filter(
                pk=locked.pk, password_version=base_version
            ).update(
                password_ciphertext=ciphertext,
                password_version=base_version + 1,
                password_updated_at=now,
                updated_at=now,
            )
            if not updated:
                raise RotationInProgressError(
                    f"实例「{locked.name}」正在被另一处轮换，请稍后重试"
                )

            log = PasswordRotationLog.objects.create(
                instance_id=locked.pk,
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


def rotate_all(batch_size: int = DEFAULT_BATCH_SIZE) -> dict[str, int]:
    """遍历所有启用实例分批轮换。

    单实例失败不中断整批（PRD AC-12）：异常在这里被捕获并计数，
    rotate_one 已经写好 failed 历史，这里只负责继续往下走。

    分批用「id 游标」而不是 `.iterator()`：
    本函数边读实例边写实例表，SQLite 上长时间持有读游标同时写同一张表
    容易出现 "database table is locked"。按 id 分批每次都是独立查询，
    既避免全量载入内存，也不会与写入互相卡住。
    """
    if batch_size < 1:
        raise ValueError("batch_size 必须是正整数")

    watch = Stopwatch("rotate-all")
    success = 0
    failed = 0
    last_id = 0

    while True:
        batch_ids = list(
            Instance.objects.filter(is_active=True, id__gt=last_id)
            .order_by("id")
            .values_list("id", flat=True)[:batch_size]
        )
        if not batch_ids:
            break

        for instance_id in batch_ids:
            instance = Instance.objects.filter(pk=instance_id).first()
            if instance is None:
                # 批次查询与处理之间被删除，跳过即可
                continue
            try:
                rotate_one(instance, source="system")
                success += 1
            except Exception:  # noqa: BLE001 - 单条失败不影响整批
                failed += 1
                logger.exception("实例 %s 轮换失败，继续处理后续实例", instance_id)

        last_id = batch_ids[-1]

    total = success + failed
    logger.info(
        "批量轮换结束：成功 %d，失败 %d，共 %d，耗时 %dms",
        success,
        failed,
        total,
        watch.stop(),
    )
    return {"success": success, "failed": failed, "total": total}
