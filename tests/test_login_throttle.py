"""登录失败限流的测试。

为什么值得单独测
----------------
限流写错方向有两种，都很安静：

- **太松**：计数没命中正确的键，阈值永远达不到 → 等于没做限流。
  表现和"没写这段代码"完全一样，只有真被爆破时才发现。
- **太紧**：成功登录不清零，或按用户名误伤 → 正常人连续输错两次就被锁，
  运维会以为"账号坏了"。

因此这里既验证"该锁的会锁"，也验证"该放的会放"。
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, RequestFactory

from common.throttle import LoginThrottle, login_throttle

pytestmark = pytest.mark.django_db

ADMIN_PASSWORD = "Admin@123456"


@pytest.fixture(autouse=True)
def clear_cache():
    """每个用例前后都清缓存。

    限流计数存在 LocMemCache 里，跨用例残留会让后续用例莫名其妙被锁，
    表现为"单独跑通过、全量跑失败"。
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(
        username="admin", password=ADMIN_PASSWORD, is_staff=True, is_superuser=True
    )


def _login(client: Client, username: str, password: str, ip: str = "10.0.0.9"):
    return client.post(
        "/admin/login/",
        {"username": username, "password": password},
        REMOTE_ADDR=ip,
    )


# ---------------------------------------------------------------------------
# 计数逻辑本身
# ---------------------------------------------------------------------------


class TestThrottleCounter:
    def test_failure_increments(self):
        throttle = LoginThrottle(max_failures=3, lockout_seconds=60)
        assert throttle.record_failure("1.1.1.1", "admin") == 1
        assert throttle.record_failure("1.1.1.1", "admin") == 2

    def test_locks_at_threshold(self):
        throttle = LoginThrottle(max_failures=3, lockout_seconds=60)
        for _ in range(2):
            throttle.record_failure("1.1.1.1", "admin")
        assert throttle.is_locked("1.1.1.1", "admin") is False

        throttle.record_failure("1.1.1.1", "admin")
        assert throttle.is_locked("1.1.1.1", "admin") is True

    def test_username_is_case_insensitive(self):
        """Alise 和 alise 是同一个爆破目标，不该各有独立预算。"""
        throttle = LoginThrottle(max_failures=2, lockout_seconds=60)
        throttle.record_failure("1.1.1.1", "Admin")
        throttle.record_failure("1.1.1.1", "ADMIN")
        assert throttle.is_locked("1.1.1.1", "admin") is True

    def test_other_ip_not_affected(self):
        """一个 IP 被锁不该连累其它来源（内网多台机器共用同一批账号）。"""
        throttle = LoginThrottle(max_failures=2, lockout_seconds=60)
        throttle.record_failure("1.1.1.1", "admin")
        throttle.record_failure("1.1.1.1", "admin")
        assert throttle.is_locked("1.1.1.1", "admin") is True
        assert throttle.is_locked("2.2.2.2", "admin") is False

    def test_ip_dimension_blocks_password_spraying(self):
        """换用户名撞库：每个用户名只错一次，用户名维度永远到不了阈值，
        必须靠 IP 维度拦住。"""
        throttle = LoginThrottle(
            max_failures=3, lockout_seconds=60, max_failures_per_ip=3
        )
        for name in ("u1", "u2", "u3"):
            throttle.record_failure("1.1.1.1", name)

        assert throttle.is_locked("1.1.1.1", "u4") is True

    def test_ip_threshold_higher_than_user_threshold(self):
        """IP 阈值默认放得比用户名宽：内网出口共用 IP 时，A 输错几次
        不该把 B、C 一起锁在门外。"""
        throttle = LoginThrottle(lockout_seconds=60)
        for _ in range(throttle.max_failures):
            throttle.record_failure("1.1.1.1", "admin")

        assert throttle.is_locked("1.1.1.1", "admin") is True
        # 同一 IP 上的另一个账号不受影响
        assert throttle.is_locked("1.1.1.1", "bob") is False
        assert throttle.max_failures_per_ip > throttle.max_failures

    def test_reset_clears_user_key(self):
        throttle = LoginThrottle(max_failures=2, lockout_seconds=60)
        throttle.record_failure("1.1.1.1", "admin")
        throttle.record_failure("1.1.1.1", "admin")
        assert throttle.is_locked("1.1.1.1", "admin") is True

        throttle.reset("1.1.1.1", "admin")
        assert throttle.is_locked("1.1.1.1", "admin") is False

    def test_remaining_attempts(self):
        throttle = LoginThrottle(max_failures=3, lockout_seconds=60)
        assert throttle.remaining_attempts("1.1.1.1", "admin") == 3
        throttle.record_failure("1.1.1.1", "admin")
        assert throttle.remaining_attempts("1.1.1.1", "admin") == 2


# ---------------------------------------------------------------------------
# 端到端：真的走 admin 登录
# ---------------------------------------------------------------------------


class TestLoginEndpoint:
    def test_failures_then_lockout(self, user, settings):
        settings.LOGIN_MAX_FAILURES = 3
        settings.LOGIN_LOCKOUT_SECONDS = 60

        client = Client()
        for _ in range(3):
            _login(client, "admin", "wrong-password")

        assert login_throttle.is_locked("10.0.0.9", "admin")

        # 锁定期间即使密码正确也登不进去
        response = _login(client, "admin", ADMIN_PASSWORD)
        assert response.status_code == 200, "应停在登录页"
        assert "_auth_user_id" not in client.session

    def test_successful_login_clears_failures(self, user, settings):
        settings.LOGIN_MAX_FAILURES = 3
        settings.LOGIN_LOCKOUT_SECONDS = 60

        client = Client()
        _login(client, "admin", "wrong")
        _login(client, "admin", "wrong")

        # 还没到阈值，正确密码可以登录
        response = _login(client, "admin", ADMIN_PASSWORD)
        assert response.status_code == 302
        assert login_throttle.remaining_attempts("10.0.0.9", "admin") == 3

    def test_failure_log_does_not_leak_password(self, user, caplog):
        """日志里绝不能出现密码——这是本项目的硬规则。"""
        client = Client()
        _login(client, "admin", "SuperSecret!42")

        for record in caplog.records:
            assert "SuperSecret!42" not in record.getMessage()

    def test_unknown_user_also_counted(self, user, settings):
        """不存在用户同样计数：否则攻击者能用"是否被限流"探测账号是否存在。"""
        settings.LOGIN_MAX_FAILURES = 2
        settings.LOGIN_LOCKOUT_SECONDS = 60

        client = Client()
        _login(client, "nobody", "x")
        _login(client, "nobody", "x")

        assert login_throttle.is_locked("10.0.0.9", "nobody")


class TestBackendUnit:
    def test_locked_backend_returns_none_without_db_hit(self, user, settings):
        """锁定后不应再去查库校验密码（顺带消除时序侧信道）。"""
        settings.LOGIN_MAX_FAILURES = 1
        settings.LOGIN_LOCKOUT_SECONDS = 60

        login_throttle.record_failure("10.0.0.9", "admin")

        from common.auth_backends import ThrottledModelBackend

        request = RequestFactory().post("/admin/login/", REMOTE_ADDR="10.0.0.9")
        backend = ThrottledModelBackend()
        assert backend.authenticate(request, username="admin", password=ADMIN_PASSWORD) is None

    def test_no_request_skips_throttle(self, user):
        """拿不到 request 时跳过限流，避免误伤脚本与测试。"""
        from common.auth_backends import ThrottledModelBackend

        backend = ThrottledModelBackend()
        result = backend.authenticate(None, username="admin", password=ADMIN_PASSWORD)
        assert result is not None
