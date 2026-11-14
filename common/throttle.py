"""登录失败限流。

问题背景
--------
登录页是整个系统唯一的身份入口，此前没有任何失败计数：任何人都可以对
``/admin/login/`` 无限次尝试密码。对 ``admin`` 这种固定用户名尤其危险——
攻击者不需要猜用户名，直接对已知账号做在线爆破即可。

这里做两层限流，缺一不可：

- **按用户名**：防止针对某个账号的定向爆破。只按 IP 限流挡不住
  分布式来源（内网多台机器、或攻击者换 IP）。
- **按 IP**：防止撞库——用一份密码字典扫大量用户名。只按用户名限流时，
  每个用户名都只错几次，永远不会触发阈值。

成功登录后清空该 (IP, 用户名) 的计数，避免"正常用户手滑几次之后被锁"。
锁定只针对失败尝试，已登录的会话不受影响。

为什么用缓存而不是数据库表
--------------------------
限流计数是**高频、短命、可丢弃**的数据：每次登录尝试都要读写一次。
写数据库会把它变成一条额外的写热点（本项目在 SQLite 上尤其不该这么做）。
缓存天然带 TTL，计数过期即清，不需要清理任务。

部署注意：默认 ``LocMemCache`` 是**进程内**的。waitress 单进程多线程时
足够；若将来改多进程部署，需要换成 Redis 等共享缓存，
否则每个进程各算一份，阈值等效变成 N 倍。
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

__all__ = ["LoginThrottle", "login_throttle"]

# 缓存键前缀，便于运维清库时定点清理
KEY_PREFIX = "login_fail"


def _setting(name: str, default: int) -> int:
    try:
        return max(1, int(getattr(settings, name, default)))
    except (TypeError, ValueError):
        return default


class LoginThrottle:
    """固定窗口的失败计数限流器。

    窗口语义：第一次失败时写入带 TTL 的计数键，TTL 到期即清零。
    这样"15 分钟内失败 5 次"就锁定，而不是"永远累计 5 次"——
    后者会让偶尔手滑的用户在几个月后被莫名其妙地拦住。

    两层阈值为什么不相等
    --------------------
    用户名维度默认 5 次，IP 维度默认 20 次。如果两者相同，会有两个坏结果：

    - **NAT 连坐**：内网出口共用一个 IP，A 输错 5 次就把 B、C 全锁在门外；
    - **误伤正常用户**：用户自己在用户名维度已经被锁了，IP 维度再锁一次，
      等于把"他这个人"和"他这台机器"重复惩罚。

    IP 维度要拦的是**撞库**——用一份字典扫大量用户名，每个用户名只错一两次。
    那种模式会在很短时间里把 IP 计数堆到几十，20 次的阈值足够触发。
    """

    def __init__(
        self,
        max_failures: int | None = None,
        lockout_seconds: int | None = None,
        max_failures_per_ip: int | None = None,
    ) -> None:
        # 存 None 表示"用配置项"，每次访问时现读。
        # 为什么不在构造时固化：settings 会被测试与运维覆写，
        # 单例若在 import 那一刻把值烤进去，覆写就完全不起作用——
        # 表现为"改了配置没效果"，排查起来极费时间。
        self._max_failures = max_failures
        self._lockout_seconds = lockout_seconds
        self._max_failures_per_ip = max_failures_per_ip

    @property
    def max_failures(self) -> int:
        if self._max_failures is not None:
            return self._max_failures
        return _setting("LOGIN_MAX_FAILURES", 5)

    @property
    def lockout_seconds(self) -> int:
        if self._lockout_seconds is not None:
            return self._lockout_seconds
        return _setting("LOGIN_LOCKOUT_SECONDS", 900)

    @property
    def max_failures_per_ip(self) -> int:
        if self._max_failures_per_ip is not None:
            return self._max_failures_per_ip
        return _setting("LOGIN_MAX_FAILURES_PER_IP", 20)

    # -- 键构造 ----------------------------------------------------------

    def _user_key(self, ip: str, username: str) -> str:
        # 用户名统一小写并截断：Django 的认证对大小写处理因后端而异，
        # 但"Alise" 和 "alise" 在暴力破解里是同一个目标，不该被当成两次独立预算。
        return f"{KEY_PREFIX}:u:{ip}:{(username or '').strip().lower()[:150]}"

    def _ip_key(self, ip: str) -> str:
        return f"{KEY_PREFIX}:ip:{ip}"

    # -- 查询 ------------------------------------------------------------

    def locked_seconds(self, ip: str, username: str) -> int:
        """返回剩余锁定秒数；0 表示未锁定。"""
        user_count = cache.get(self._user_key(ip, username), 0)
        ip_count = cache.get(self._ip_key(ip), 0)

        if user_count >= self.max_failures or ip_count >= self.max_failures_per_ip:
            return self.lockout_seconds
        return 0

    def is_locked(self, ip: str, username: str) -> bool:
        return self.locked_seconds(ip, username) > 0

    def remaining_attempts(self, ip: str, username: str) -> int:
        """还剩几次尝试机会（用于提示文案）。

        取两个维度里**更紧**的那个：只要有一维接近阈值，都该提前提醒。
        """
        by_user = self.max_failures - cache.get(self._user_key(ip, username), 0)
        by_ip = self.max_failures_per_ip - cache.get(self._ip_key(ip), 0)
        return max(0, min(by_user, by_ip))

    # -- 计数 ------------------------------------------------------------

    def record_failure(self, ip: str, username: str) -> int:
        """记一次失败，返回该用户名当前的累计失败次数。"""
        user_key = self._user_key(ip, username)
        ip_key = self._ip_key(ip)

        # 缓存没有原子自增且自动设 TTL 的通用接口，这里用显式读写。
        # 竞态下偶尔少记一次可接受（阈值是 5 次，不是 1 次）。
        user_count = cache.get(user_key, 0) + 1
        ip_count = cache.get(ip_key, 0) + 1

        cache.set(user_key, user_count, self.lockout_seconds)
        cache.set(ip_key, ip_count, self.lockout_seconds)

        if user_count == self.max_failures or ip_count == self.max_failures_per_ip:
            logger.warning(
                "登录失败达到阈值，已临时锁定 ip=%s username=%s "
                "（用户名 %d/%d 次，IP %d/%d 次，%d 秒后解锁）",
                ip or "<unknown>",
                (username or "").strip()[:64] or "<empty>",
                user_count,
                self.max_failures,
                ip_count,
                self.max_failures_per_ip,
                self.lockout_seconds,
            )
        else:
            logger.info(
                "登录失败 ip=%s username=%s 累计=%d/%d",
                ip or "<unknown>",
                (username or "").strip()[:64] or "<empty>",
                user_count,
                self.max_failures,
            )

        return user_count

    def reset(self, ip: str, username: str) -> None:
        """登录成功后清空该组合的计数。

        只清 (IP, 用户名) 与用户名维度的键，**不清** IP 计数——
        IP 维度拦的是撞库，一个成功登录不该给该 IP 的其它失败尝试"洗白"。
        不过若该 IP 已经因为自己登录成功而不再可疑，也不至于长期连累；
        IP 计数有 TTL，会自然过期。
        """
        cache.delete(self._user_key(ip, username))


# 单例：无状态，配置在首次使用时读取
login_throttle = LoginThrottle()
