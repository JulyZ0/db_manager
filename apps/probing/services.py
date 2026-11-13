"""TCP 探测核心逻辑。

接口层只调用 probe_tcp，不关心异常分类细节。

设计约束（PRD FR-04 / §7.5 / SEC-4）：
- **绝不向外抛异常**："探测不到"是有效的业务结论，不是服务端错误。
- 只返回分类码，不返回异常堆栈或底层消息（避免泄露内网拓扑）。
- 单次耗时不超过 timeout，超时时间由配置项控制。
"""

from __future__ import annotations

import errno
import logging
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


def probe_tcp(host: str, port: int, timeout: float | None = None) -> ProbeResult:
    """对 host:port 发起一次 TCP 连接尝试。

    Args:
        host: 主机名或 IP。
        port: 端口。
        timeout: 连接超时秒数，缺省取 settings.PROBE_TIMEOUT_SECONDS。

    Returns:
        ProbeResult。可达时 error 为 None 并带 latency_ms；
        不可达时 reachable=False、latency_ms=None，error 为分类码。
        耗时仍可通过日志观察，但不回填到 latency_ms，
        避免把"超时 2000ms"误当成连接耗时。
    """
    if timeout is None:
        timeout = getattr(settings, "PROBE_TIMEOUT_SECONDS", 2)

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
