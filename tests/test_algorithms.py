"""四个核心算法的针对性测试（覆盖 PRD §10 验收标准）。

分组：
- TestCrypto         加解密、密码生成、指纹（AC-10）
- TestProbeTCP       TCP 探测与错误分类（AC-07/08/09）
- TestRotateOne      单实例轮换（AC-11/13）
- TestRotateAll      批量轮换与失败隔离（AC-12）
- TestAggregate      每日统计与幂等（AC-14/15/16）
"""

from __future__ import annotations

import errno
import socket
from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.assets.models import Cluster, Department, Instance, PasswordRotationLog

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def dept(db):
    return Department.objects.create(name="基础架构部", code="INFRA")


@pytest.fixture
def cluster(dept):
    return Cluster.objects.create(name="core-mysql-prod", department=dept)


@pytest.fixture
def instance(cluster):
    return Instance.objects.create(
        name="core-01",
        cluster=cluster,
        host="10.0.0.1",
        port=3306,
        username="app",
    )


@pytest.fixture
def listening_port():
    """起一个真实监听端口，测完自动关闭。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        yield port
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# crypto.py
# ---------------------------------------------------------------------------


class TestCrypto:
    def test_roundtrip(self):
        from apps.credentials.crypto import decrypt_secret, encrypt_secret

        cipher = encrypt_secret("S3cret!Passw0rd")
        assert cipher.startswith("v1:")
        assert "S3cret!Passw0rd" not in cipher
        assert decrypt_secret(cipher) == "S3cret!Passw0rd"

    def test_same_plaintext_yields_different_ciphertext(self):
        """AC-10：相同明文必须得到不同密文，否则能看出哪些实例密码相同。"""
        from apps.credentials.crypto import encrypt_secret

        a = encrypt_secret("same-password")
        b = encrypt_secret("same-password")
        assert a != b, "Fernet 随机 IV 失效：相同明文产生了相同密文"

    def test_empty_string_passthrough(self):
        from apps.credentials.crypto import decrypt_secret, encrypt_secret

        assert encrypt_secret("") == ""
        assert decrypt_secret("") == ""

    def test_unknown_key_version_rejected(self):
        from apps.credentials.crypto import DecryptionError, decrypt_secret

        with pytest.raises(DecryptionError):
            decrypt_secret("v9:whatever")

    def test_missing_version_prefix_rejected(self):
        from apps.credentials.crypto import DecryptionError, decrypt_secret

        with pytest.raises(DecryptionError):
            decrypt_secret("no-prefix-at-all")

    def test_tampered_ciphertext_rejected(self):
        from apps.credentials.crypto import (
            DecryptionError,
            decrypt_secret,
            encrypt_secret,
        )

        cipher = encrypt_secret("value")
        tampered = cipher[:-4] + "AAAA"
        with pytest.raises(DecryptionError):
            decrypt_secret(tampered)

    def test_generate_password_default_length(self, settings):
        from apps.credentials.crypto import generate_password

        settings.PASSWORD_LENGTH = 32
        pwd = generate_password()
        assert len(pwd) == 32

    def test_generate_password_contains_all_char_classes(self):
        import string

        from apps.credentials.crypto import generate_password

        # 多跑几次，避免偶然性
        for _ in range(20):
            pwd = generate_password(16)
            assert any(c in string.ascii_lowercase for c in pwd)
            assert any(c in string.ascii_uppercase for c in pwd)
            assert any(c in string.digits for c in pwd)
            assert any(not c.isalnum() for c in pwd)

    def test_generate_password_is_random(self):
        from apps.credentials.crypto import generate_password

        assert len({generate_password(16) for _ in range(20)}) == 20

    def test_generate_password_rejects_too_short(self):
        from apps.credentials.crypto import generate_password

        with pytest.raises(ValueError):
            generate_password(3)

    def test_fingerprint_is_stable_and_irreversible(self):
        from apps.credentials.crypto import fingerprint

        assert fingerprint("abc") == fingerprint("abc")
        assert fingerprint("abc") != fingerprint("abd")
        assert len(fingerprint("abc")) == 16
        assert "abc" not in fingerprint("abc")


# ---------------------------------------------------------------------------
# probing/services.py
# ---------------------------------------------------------------------------


class TestProbeTCP:
    def test_reachable_port(self, listening_port):
        from apps.probing.services import probe_tcp

        result = probe_tcp("127.0.0.1", listening_port, timeout=1)
        assert result.reachable is True
        assert result.error is None
        assert result.latency_ms is not None

    # -- 错误分类：用 mock 精确覆盖各分支，不受平台网络行为影响 --------

    @pytest.mark.parametrize(
        "exc, expected",
        [
            (ConnectionRefusedError(), "refused"),
            (socket.gaierror(-2, "Name or service not known"), "dns_failed"),
            (TimeoutError(), "timeout"),
            (socket.timeout(), "timeout"),
            # 用 errno 常量而不是魔数：同一语义在不同平台取不同的整数
            (OSError(errno.ENETUNREACH, "Network is unreachable"), "network_unreachable"),
            (OSError(errno.EHOSTUNREACH, "No route to host"), "network_unreachable"),
            (OSError(errno.ETIMEDOUT, "timed out"), "timeout"),
            (OSError(999, "weird"), "unknown"),
            (RuntimeError("boom"), "unknown"),
        ],
    )
    def test_error_classification(self, monkeypatch, exc, expected):
        from apps.probing.services import probe_tcp

        def fake_create_connection(*args, **kwargs):
            raise exc

        monkeypatch.setattr(
            socket, "create_connection", fake_create_connection
        )
        result = probe_tcp("example.test", 3306, timeout=1)

        assert result.reachable is False
        assert result.error == expected

    def test_oserror_errno_113_is_normalized_to_platform_value(self, monkeypatch):
        """errno 常量在不同平台取值不同，代码必须用常量而不是魔数比较。"""
        import errno

        from apps.probing.services import probe_tcp

        def fake_create_connection(*args, **kwargs):
            raise OSError(errno.EHOSTUNREACH, "No route to host")

        monkeypatch.setattr(socket, "create_connection", fake_create_connection)
        assert probe_tcp("h", 1, timeout=1).error == "network_unreachable"

    def test_socket_is_closed_after_probe(self, monkeypatch):
        """探测结束后必须关闭 socket，否则高并发探测会耗尽文件描述符。"""

        class FakeSock:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        sock = FakeSock()
        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: sock)

        from apps.probing.services import probe_tcp

        probe_tcp("h", 1, timeout=1)
        assert sock.closed is True

    def test_close_failure_does_not_break_success(self, monkeypatch):
        """关闭失败不能把成功的探测变成失败。"""

        class BadSock:
            def close(self):
                raise OSError("close failed")

        monkeypatch.setattr(socket, "create_connection", lambda *a, **k: BadSock())

        from apps.probing.services import probe_tcp

        result = probe_tcp("h", 1, timeout=1)
        assert result.reachable is True

    # -- 真实网络行为：只断言"不抛异常 + 有分类码" ----------------------

    def test_closed_port_never_raises(self):
        """AC-08 的平台无关形态。

        真实网络里"端口无监听"具体表现为 refused 还是 timeout 取决于
        操作系统与防火墙策略（本机 Windows 会丢弃 SYN 导致 timeout），
        因此这里只断言核心契约：不抛异常、不可达、给出分类码。
        """
        from apps.probing.services import probe_tcp

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()

        result = probe_tcp("127.0.0.1", port, timeout=1)
        assert result.reachable is False
        assert result.error in {"refused", "timeout", "unknown"}

    def test_unresolvable_host_never_raises(self):
        """AC-09：无法解析的域名必须归为可读分类码，不能抛异常。"""
        from apps.probing.services import probe_tcp

        result = probe_tcp("no-such-host.invalid", 3306, timeout=1)
        assert result.reachable is False
        assert result.error in {"dns_failed", "network_unreachable", "unknown"}

    def test_bad_port_never_raises(self):
        from apps.probing.services import probe_tcp

        result = probe_tcp("127.0.0.1", 70000, timeout=1)
        assert result.reachable is False
        assert result.error is not None

    def test_uses_settings_timeout_by_default(self, settings, monkeypatch):
        """不传 timeout 时必须取 settings.PROBE_TIMEOUT_SECONDS。"""
        from apps.probing.services import probe_tcp

        settings.PROBE_TIMEOUT_SECONDS = 1
        captured = {}

        def fake_create_connection(address, timeout=None, **kwargs):
            captured["timeout"] = timeout
            raise ConnectionRefusedError()

        monkeypatch.setattr(socket, "create_connection", fake_create_connection)
        probe_tcp("h", 1)

        assert captured["timeout"] == 1


# ---------------------------------------------------------------------------
# credentials/services.py — rotate_one
# ---------------------------------------------------------------------------


class TestRotateOne:
    def test_increments_version_and_sets_ciphertext(self, instance):
        from apps.credentials.services import rotate_one

        before = instance.password_version
        log = rotate_one(instance, source="manual")

        instance.refresh_from_db()
        assert instance.password_version == before + 1
        assert instance.password_set is True
        assert instance.password_ciphertext.startswith("v1:")
        assert instance.password_updated_at is not None
        assert log.status == PasswordRotationLog.Status.SUCCESS
        assert log.source == "manual"

    def test_writes_rotation_log_with_fingerprints(self, instance):
        from apps.credentials.services import rotate_one

        log = rotate_one(instance)
        assert log.new_fingerprint
        assert len(log.new_fingerprint) == 16
        # 首次轮换没有旧密码，旧指纹为空
        assert log.old_fingerprint == ""
        assert PasswordRotationLog.objects.filter(instance=instance).count() == 1

    def test_second_rotation_records_old_fingerprint(self, instance):
        """AC-13：连续两次轮换，指纹不同且第二次能对上次的密码算出旧指纹。"""
        from apps.credentials.services import rotate_one

        first = rotate_one(instance)
        instance.refresh_from_db()
        second = rotate_one(instance)

        assert first.new_fingerprint != second.new_fingerprint
        assert second.old_fingerprint == first.new_fingerprint
        assert PasswordRotationLog.objects.filter(instance=instance).count() == 2

    def test_rotation_does_not_log_plaintext(self, instance, caplog):
        """SEC-1：日志中不得出现明文密码。"""
        import logging

        from apps.credentials.crypto import decrypt_secret
        from apps.credentials.services import rotate_one

        with caplog.at_level(logging.DEBUG):
            rotate_one(instance)

        instance.refresh_from_db()
        plaintext = decrypt_secret(instance.password_ciphertext)
        assert plaintext
        for record in caplog.records:
            assert plaintext not in record.getMessage()

    def test_failure_writes_failed_log_and_raises(self, instance, monkeypatch):
        """单个实例内部报错时必须留审计，并把异常交给调用方。"""
        from apps.credentials import services

        def boom():
            raise RuntimeError("生成失败")

        monkeypatch.setattr(services, "generate_password", boom)

        with pytest.raises(RuntimeError):
            services.rotate_one(instance)

        log = PasswordRotationLog.objects.get(instance=instance)
        assert log.status == PasswordRotationLog.Status.FAILED
        assert "RuntimeError" in log.error_message

    def test_version_conflict_raises_rotation_in_progress(self, instance, monkeypatch):
        """乐观锁：读到的版本号在写入前被别人改掉，必须报并发冲突。"""
        from apps.credentials import services
        from common.exceptions import RotationInProgressError

        original_generate = services.generate_password

        def generate_and_bump():
            # 模拟另一个 worker 抢先完成了一次轮换
            Instance.objects.filter(pk=instance.pk).update(
                password_version=instance.password_version + 1
            )
            return original_generate()

        monkeypatch.setattr(services, "generate_password", generate_and_bump)

        with pytest.raises(RotationInProgressError):
            services.rotate_one(instance)


# ---------------------------------------------------------------------------
# credentials/services.py — rotate_all
# ---------------------------------------------------------------------------


class TestRotateAll:
    def test_empty_database(self):
        from apps.credentials.services import rotate_all

        assert rotate_all() == {"success": 0, "failed": 0, "total": 0}

    def test_rotates_only_active_instances(self, cluster):
        """AC-11：停用实例不被轮换。"""
        from apps.credentials.services import rotate_all

        active = Instance.objects.create(
            name="a", cluster=cluster, host="10.0.0.1", port=3306, username="u"
        )
        inactive = Instance.objects.create(
            name="b",
            cluster=cluster,
            host="10.0.0.2",
            port=3306,
            username="u",
            is_active=False,
        )

        stats = rotate_all()
        assert stats == {"success": 1, "failed": 0, "total": 1}

        active.refresh_from_db()
        inactive.refresh_from_db()
        assert active.password_version == 1
        assert inactive.password_version == 0

    def test_batches_cover_every_instance(self, cluster):
        """分批不能漏实例：25 个实例、批大小 10 应处理 25 个。"""
        from apps.credentials.services import rotate_all

        for i in range(25):
            Instance.objects.create(
                name=f"i{i}",
                cluster=cluster,
                host=f"10.0.1.{i}",
                port=3306,
                username="u",
            )

        stats = rotate_all(batch_size=10)
        assert stats["total"] == 25
        assert stats["success"] == 25
        assert Instance.objects.filter(password_version=1).count() == 25

    def test_single_failure_does_not_abort_batch(self, cluster, monkeypatch):
        """AC-12：一个实例失败，其余继续。"""
        from apps.credentials import services

        for i in range(3):
            Instance.objects.create(
                name=f"i{i}",
                cluster=cluster,
                host=f"10.0.2.{i}",
                port=3306,
                username="u",
            )

        original = services.rotate_one

        def flaky(instance, **kwargs):
            if instance.host.endswith(".1"):
                raise RuntimeError("模拟故障")
            return original(instance, **kwargs)

        monkeypatch.setattr(services, "rotate_one", flaky)

        stats = services.rotate_all()
        assert stats == {"success": 2, "failed": 1, "total": 3}

    def test_rejects_invalid_batch_size(self):
        from apps.credentials.services import rotate_all

        with pytest.raises(ValueError):
            rotate_all(batch_size=0)


# ---------------------------------------------------------------------------
# metrics/services.py — aggregate_for_date
# ---------------------------------------------------------------------------


class TestAggregate:
    def test_counts_instances_per_cluster(self, dept):
        """AC-14：两集群共 3 个启用实例 -> 2 行，合计 3。"""
        from apps.metrics.services import aggregate_for_date

        c1 = Cluster.objects.create(name="c1", department=dept)
        c2 = Cluster.objects.create(name="c2", department=dept)
        for i in range(2):
            Instance.objects.create(
                name=f"a{i}", cluster=c1, host=f"10.1.0.{i}", port=3306, username="u"
            )
        Instance.objects.create(
            name="b0", cluster=c2, host="10.1.1.0", port=3306, username="u"
        )

        written = aggregate_for_date(date(2026, 11, 13))
        assert written == 2

        from apps.metrics.models import DailyInstanceStat

        rows = DailyInstanceStat.objects.filter(stat_date=date(2026, 11, 13))
        assert rows.count() == 2
        assert sum(r.instance_count for r in rows) == 3

    def test_ignores_inactive_instances(self, cluster):
        from apps.metrics.models import DailyInstanceStat
        from apps.metrics.services import aggregate_for_date

        Instance.objects.create(
            name="on", cluster=cluster, host="10.2.0.1", port=3306, username="u"
        )
        Instance.objects.create(
            name="off",
            cluster=cluster,
            host="10.2.0.2",
            port=3306,
            username="u",
            is_active=False,
        )

        aggregate_for_date(date(2026, 11, 13))
        row = DailyInstanceStat.objects.get(stat_date=date(2026, 11, 13))
        assert row.instance_count == 1

    def test_active_count_only_counts_online(self, cluster):
        from apps.metrics.models import DailyInstanceStat
        from apps.metrics.services import aggregate_for_date

        Instance.objects.create(
            name="up",
            cluster=cluster,
            host="10.3.0.1",
            port=3306,
            username="u",
            status=Instance.Status.ONLINE,
        )
        Instance.objects.create(
            name="down",
            cluster=cluster,
            host="10.3.0.2",
            port=3306,
            username="u",
            status=Instance.Status.OFFLINE,
        )

        aggregate_for_date(date(2026, 11, 13))
        row = DailyInstanceStat.objects.get(stat_date=date(2026, 11, 13))
        assert row.instance_count == 2
        assert row.active_count == 1

    def test_idempotent(self, cluster):
        """AC-15：同一天重复执行不产生重复行。"""
        from apps.metrics.models import DailyInstanceStat
        from apps.metrics.services import aggregate_for_date

        Instance.objects.create(
            name="i", cluster=cluster, host="10.4.0.1", port=3306, username="u"
        )

        aggregate_for_date(date(2026, 11, 13))
        aggregate_for_date(date(2026, 11, 13))
        aggregate_for_date(date(2026, 11, 13))

        assert DailyInstanceStat.objects.filter(stat_date=date(2026, 11, 13)).count() == 1

    def test_snapshot_survives_rename(self, cluster):
        """AC-16：集群改名后历史快照仍显示旧名。"""
        from apps.metrics.models import DailyInstanceStat
        from apps.metrics.services import aggregate_for_date

        Instance.objects.create(
            name="i", cluster=cluster, host="10.5.0.1", port=3306, username="u"
        )
        aggregate_for_date(date(2026, 11, 12))

        cluster.name = "renamed-cluster"
        cluster.save(update_fields=["name"])
        aggregate_for_date(date(2026, 11, 13))

        old_row = DailyInstanceStat.objects.get(stat_date=date(2026, 11, 12))
        new_row = DailyInstanceStat.objects.get(stat_date=date(2026, 11, 13))
        assert old_row.cluster_name == "core-mysql-prod"
        assert new_row.cluster_name == "renamed-cluster"

    def test_cluster_without_instances_produces_no_row(self, dept):
        """无实例的集群不产生行（题目口径是统计实例数量）。"""
        from apps.metrics.models import DailyInstanceStat
        from apps.metrics.services import aggregate_for_date

        Cluster.objects.create(name="empty", department=dept)
        assert aggregate_for_date(date(2026, 11, 13)) == 0
        assert DailyInstanceStat.objects.count() == 0

    def test_defaults_to_today(self, cluster):
        from apps.metrics.models import DailyInstanceStat
        from apps.metrics.services import aggregate_for_date

        Instance.objects.create(
            name="i", cluster=cluster, host="10.6.0.1", port=3306, username="u"
        )
        aggregate_for_date()
        assert DailyInstanceStat.objects.filter(
            stat_date=timezone.localdate()
        ).exists()

    def test_separate_dates_are_independent(self, cluster):
        from apps.metrics.models import DailyInstanceStat
        from apps.metrics.services import aggregate_for_date

        Instance.objects.create(
            name="i", cluster=cluster, host="10.7.0.1", port=3306, username="u"
        )
        today = date(2026, 11, 13)
        aggregate_for_date(today)
        aggregate_for_date(today - timedelta(days=1))
        assert DailyInstanceStat.objects.count() == 2
