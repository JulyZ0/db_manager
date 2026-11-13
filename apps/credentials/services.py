"""密码轮换服务。

职责分离：
- crypto.py    纯算法（加解密、生成、指纹）
- services.py  业务流程（读旧密码、写新密码、记历史、并发控制）
- tasks.py     Celery 包装（重试、日志、分批）
"""

from __future__ import annotations

from typing import Literal

from apps.assets.models import Instance, PasswordRotationLog

RotationSource = Literal["system", "manual"]


def rotate_one(
    instance: Instance,
    *,
    source: RotationSource = "system",
) -> PasswordRotationLog:
    """为单个实例生成并写入新密码，返回本次轮换历史记录。

    TODO(手写) 实现要点：
    1. 在事务内对 instance 加行锁（select_for_update），避免并发轮换竞争
       password_version（PRD R5）。
    2. 旧密码：if instance.password_ciphertext: decrypt_secret(...) → fingerprint(old)，
       解密失败不要中断，指纹留空并在日志里告警。
    3. 新密码：generate_password() → encrypt_secret() → 写入 password_ciphertext，
       password_version += 1，password_updated_at = timezone.now()
    4. 写 PasswordRotationLog：old_fingerprint / new_fingerprint / source /
       status=success / duration_ms（time.perf_counter 计时）。
    5. 任何异常：写一条 status=failed 的历史（error_message 只放异常类型与简述，
       绝不含明文或密文），然后按需要向上抛或吞掉——由 tasks 层决定批次是否继续。
    """

    raise NotImplementedError("TODO: 实现单实例轮换")


def rotate_all(batch_size: int = 100) -> dict[str, int]:
    """遍历所有启用实例分批轮换。

    TODO(手写) 实现要点：
    1. queryset = Instance.objects.filter(is_active=True).iterator(chunk_size=batch_size)
    2. 逐条调用 rotate_one，捕获单条异常并记录，不中断整批（PRD AC-12）。
    3. 返回 {"success": n, "failed": m, "total": n+m}。
    4. 注意：不要一次性把全部实例读进内存。
    """
    raise NotImplementedError("TODO: 实现批量轮换")
