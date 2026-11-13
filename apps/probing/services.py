"""TCP 探测核心逻辑。

接口层只调用 probe_tcp，不关心异常分类细节。

设计约束（PRD FR-04 / §7.5 / SEC-4）：
- **绝不向外抛异常**："探测不到"是有效的业务结论，不是服务端错误。
- 只返回分类码，不返回异常堆栈或底层消息（避免泄露内网拓扑）。
- 单次耗时不超过 timeout，超时时间由配置项控制。

重试与退避
----------
探测失败分两类，处理方式不同：

- **瞬时失败**（timeout / network_unreachable / unknown）：可能是网络抖动、
  对端瞬时过载，立即失败会误报。按指数退避重试。
- **确定性失败**（refused / dns_failed）：端口确实没监听、域名确实解析不出来，
  重试只是白白拖延。**立即返回**，不做无意义的重试。

两条硬约束，防止重试把调用方拖死：

1. **总预算** `PROBE_TOTAL_BUDGET_MS`：无论配置多少次重试，整个探测过程
   不允许超过这个时间。预算不够就不再发起新一轮，直接返回上次结果。
2. **退避有上限**：第 n 次重试的等待为 base * 2^(n-2)，并叠加抖动，
   避免同一批实例在同一时刻齐步重试（惊群）。

因为探测是同步阻塞的，预算就是 HTTP 请求的最坏耗时，默认 5 秒。
"""

from __future__ import annotations

import errno
import logging
import random
import socket
import time
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

# 探测失败时返回的分类码，与 PRD §7.5 一致
ERROR_TIMEOUT = "timeout"
ERROR_REFUSED = "refused"
ERROR_DNS_FAILED = "dns_failed"
ERROR_NETWORK_UNREACHABLE = "network_unreachable"
ERROR_UNKNOWN = "unknown"

# 重试默认值；实际以 settings 为准（见 config/settings.py 的探测配置段）
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_MS = 200
DEFAULT_TOTAL_BUDGET_MS = 5_000

# 单次退避上限：即使 attempts 配得很大，也不让等待无上限增长
MAX_BACKOFF_SECONDS = 5.0

# 各平台表示"网络不可达"的 errno。
# 用 hasattr 过滤：ENETUNREACH / EHOSTDOWN 等常量在部分平台（或精简版 Python
# 构建）并不存在，直接引用会在 import 时就抛 AttributeError。
# 注意：socket.gaierror 与 ConnectionRefusedError 都是 OSError 子类，
# 因此必须先按异常类型判断，再按 errno 兜底，顺序不能反。
_UNREACHABLE_ERRNOS = {
    getattr(errno, name)
    for name in ("ENETUNREACH", "EHOSTUNREACH", "EHOSTDOWN", "ENETDOWN")
    if hasattr(errno, name)
}

_TIMEOUT_ERRNOS = {getattr(errno, "ETIMEDOUT", None)} - {None}


@dataclass(slots=True)
class ProbeResult:
    reachable: bool
    latency_ms: int | None
    error: str | None
    # 本次探测实际尝试了几次（含首次）。便于运维判断"是不是在抖"
    attempts: int = 1


# 值得重试的失败：网络抖动、对端瞬时过载。
# refused / dns_failed 属于确定性失败——端口确实没监听、域名确实解析不出来，
# 重试只是白白拖长调用方等待，因此不进这个集合。
_RETRIABLE_ERRORS = frozenset(
    {ERROR_TIMEOUT, ERROR_NETWORK_UNREACHABLE, ERROR_UNKNOWN}
)


def _classify(exc: BaseException) -> str:
    """把异常归类成 PRD 定义的错误码。

    判断顺序按"子类优先"：
    gaierror（DNS）> ConnectionRefusedError（拒绝）> 超时 > errno 兜底。
    """
    if isinstance(exc, socket.gaierror):
        return ERROR_DNS_FAILED

    if isinstance(exc, ConnectionRefusedError):
        return ERROR_REFUSED

    # 3.10+ socket.timeout 就是 TimeoutError 的别名
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ERROR_TIMEOUT

    if isinstance(exc, OSError):
        if exc.errno in _UNREACHABLE_ERRNOS:
            return ERROR_NETWORK_UNREACHABLE
        if exc.errno in _TIMEOUT_ERRNOS:
            return ERROR_TIMEOUT
        # Windows 的连接超时表现为 WSAETIMEDOUT，已包含在 ETIMEDOUT 中；
        # 其余 OSError 一律归为 unknown，不把原始消息透出去
        return ERROR_UNKNOWN

    return ERROR_UNKNOWN


def _probe_once(host: str, port: int, timeout: float) -> ProbeResult:
    """发起一次连接尝试。**不重试**，也不向外抛异常。"""
    started = time.perf_counter()
    sock: socket.socket | None = None

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(reachable=True, latency_ms=latency_ms, error=None)

    except BaseException as exc:  # noqa: BLE001 - 含 KeyboardInterrupt 的边界见下
        # 不捕获 SystemExit / KeyboardInterrupt 这类进程控制异常
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise

        error = _classify(exc)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        # 只记录分类码与耗时，不记录异常原始消息（可能含内网地址）
        logger.info(
            "TCP 探测失败 %s:%s -> %s（耗时 %dms）", host, port, error, elapsed_ms
        )
        return ProbeResult(reachable=False, latency_ms=None, error=error)

    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                # 关闭失败无关紧要，不能因此把成功的探测变成失败
                pass


def _backoff_seconds(attempt: int) -> float:
    """第 ``attempt`` 次重试前的等待秒数（指数退避 + 抖动）。

    抖动取 0.5~1.5 倍：同一批实例往往在同一个循环里齐步探测，
    固定间隔会让它们在重试时再次同时打到同一个目标。
    """
    base_ms = int(getattr(settings, "PROBE_BACKOFF_BASE_MS", DEFAULT_BACKOFF_BASE_MS))
    base_ms = max(0, base_ms)
    delay = min(base_ms / 1000 * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
    return delay * random.uniform(0.5, 1.5)


def probe_tcp(
    host: str,
    port: int,
    timeout: float | None = None,
    *,
    max_attempts: int | None = None,
    budget_ms: int | None = None,
) -> ProbeResult:
    """对 host:port 发起 TCP 探测，瞬时失败按指数退避重试。

    Args:
        host: 主机名或 IP。
        port: 端口。
        timeout: 单次连接超时秒数，缺省取 settings.PROBE_TIMEOUT_SECONDS。
        max_attempts: 最多尝试次数（含首次），缺省取 settings.PROBE_MAX_ATTEMPTS。
        budget_ms: 整个探测的总时间预算（毫秒），缺省取
            settings.PROBE_TOTAL_BUDGET_MS。预算用完立即返回上次结果，
            **不会**为了凑满重试次数而超出预算。

    Returns:
        ProbeResult。可达时 error 为 None 并带 latency_ms；
        不可达时 reachable=False、latency_ms=None、error 为分类码，
        attempts 为实际尝试次数。

    重试策略见模块文档：只重试瞬时失败，``refused`` / ``dns_failed``
    立即返回；预算不足时不再发起新一轮。
    """
    if timeout is None:
        timeout = getattr(settings, "PROBE_TIMEOUT_SECONDS", 2)
    if max_attempts is None:
        max_attempts = int(
            getattr(settings, "PROBE_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
        )
    if budget_ms is None:
        budget_ms = int(
            getattr(settings, "PROBE_TOTAL_BUDGET_MS", DEFAULT_TOTAL_BUDGET_MS)
        )

    max_attempts = max(1, max_attempts)
    deadline = time.perf_counter() + max(0, budget_ms) / 1000

    result = _probe_once(host, port, timeout)
    attempt = 1

    while attempt < max_attempts and result.error in _RETRIABLE_ERRORS:
        delay = _backoff_seconds(attempt)
        remaining = deadline - time.perf_counter()

        # 预算连"等待"都不够：直接返回，别再拖调用方
        if remaining <= delay:
            logger.info(
                "TCP 探测 %s:%s 重试预算已用尽（剩余 %.0fms < 退避 %.0fms）",
                host,
                port,
                remaining * 1000,
                delay * 1000,
            )
            break

        # 等完退避后剩不下一次完整超时的时间，重试也必然超预算
        if remaining - delay < timeout:
            logger.info(
                "TCP 探测 %s:%s 预算不足以再试一次（剩余 %.0fms）",
                host,
                port,
                remaining * 1000,
            )
            break

        time.sleep(delay)
        attempt += 1
        result = _probe_once(host, port, timeout)

    if attempt > 1:
        logger.info(
            "TCP 探测 %s:%s 重试 %d 次后结束，最终结果 %s",
            host,
            port,
            attempt - 1,
            result.error or "reachable",
        )

    result.attempts = attempt
    return result
