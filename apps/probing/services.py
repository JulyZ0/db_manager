"""TCP 探测核心逻辑。

这是需要你手写的四个核心算法之一（见 README「待手写清单」）。
接口层只调用 probe_tcp，不关心异常分类细节。
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

# 探测失败时返回的分类码，与 PRD §7.5 一致
ERROR_TIMEOUT = "timeout"
ERROR_REFUSED = "refused"
ERROR_DNS_FAILED = "dns_failed"
ERROR_NETWORK_UNREACHABLE = "network_unreachable"
ERROR_UNKNOWN = "unknown"


@dataclass(slots=True)
class ProbeResult:
    reachable: bool
    latency_ms: int | None
    error: str | None


def probe_tcp(
    host: str, port: int, timeout: float | None = None
) -> ProbeResult:
    """对 host:port 发起一次 TCP 连接尝试。

    TODO(手写) 实现要点：
    1. timeout 默认取 settings.PROBE_TIMEOUT_SECONDS。
    2. 用 time.perf_counter() 计时，socket.create_connection((host, port), timeout)。
    3. 成功：ProbeResult(True, latency_ms, None)，并在 finally 里关闭 socket。
    4. 异常分类：
         socket.timeout / TimeoutError → ERROR_TIMEOUT
         ConnectionRefusedError        → ERROR_REFUSED
         socket.gaierror               → ERROR_DNS_FAILED
         OSError(errno.ENETUNREACH/EHOSTUNREACH) → ERROR_NETWORK_UNREACHABLE
         其他                          → ERROR_UNKNOWN（只记类型名，不抛给调用方）
    5. 任何分支都不得向外抛异常：探测不到是业务结论，不是服务端错误。
    """
    raise NotImplementedError("TODO: 实现 TCP 探测")
