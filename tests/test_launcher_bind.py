"""一键启动器的监听地址逻辑测试。

为什么值得单独测
----------------
默认绑定地址写错的两种方向都很安静，而且要到部署时才暴露：

- **太松**：默认写成 0.0.0.0 而不自知，一套持着全部生产库口令的系统
  从第一次双击起就对整张内网开放，日志里看不出任何异常。
- **太紧/写死**：运维按 README 改了 `.env` 里的 `APP_BIND_HOST`，
  启动器却没有真的读它（例如仍在命令行里硬编码 127.0.0.1），
  表现为"同事打不开"，而配置文件看起来完全正确。

这里用临时文件替身验证 `.env` 解析，不碰仓库里真实的 `.env`。
"""

from __future__ import annotations

import launcher
import pytest


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """把启动器指向一个临时 .env，避免读到仓库里的真实配置。"""
    path = tmp_path / ".env"
    monkeypatch.setattr(launcher, "ENV_FILE", path)
    # 清掉可能存在的真实环境变量，保证用例互不影响
    monkeypatch.delenv("APP_BIND_HOST", raising=False)
    return path


# ---------------------------------------------------------------------------
# 读取配置
# ---------------------------------------------------------------------------


def test_defaults_to_loopback(env_file):
    """没有任何配置时必须是回环地址——这是安全默认值。"""
    assert launcher.bind_host() == "127.0.0.1"


def test_reads_value_from_env_file(env_file):
    env_file.write_text("APP_BIND_HOST=0.0.0.0\n", encoding="utf-8")

    assert launcher.bind_host() == "0.0.0.0"


def test_real_env_var_wins_over_env_file(env_file, monkeypatch):
    """与 common.env 的口径一致：真实环境变量优先于 .env。"""
    env_file.write_text("APP_BIND_HOST=10.1.1.1\n", encoding="utf-8")
    monkeypatch.setenv("APP_BIND_HOST", "10.2.2.2")

    assert launcher.bind_host() == "10.2.2.2"


def test_empty_value_falls_back_to_default(env_file):
    """空值应当被当成"没配"，而不是绑到一个空地址上。"""
    env_file.write_text("APP_BIND_HOST=\n", encoding="utf-8")

    assert launcher.bind_host() == "127.0.0.1"


def test_quoted_value_is_unwrapped(env_file):
    env_file.write_text('APP_BIND_HOST="10.3.3.3"\n', encoding="utf-8")

    assert launcher.bind_host() == "10.3.3.3"


def test_comment_lines_are_ignored(env_file):
    env_file.write_text(
        "# APP_BIND_HOST=0.0.0.0\nAPP_BIND_HOST=127.0.0.1\n", encoding="utf-8"
    )

    assert launcher.bind_host() == "127.0.0.1"


def test_missing_env_file_is_not_an_error(tmp_path, monkeypatch):
    """首次启动时 .env 还不存在，不能因此崩在取配置这一步。"""
    monkeypatch.setattr(launcher, "ENV_FILE", tmp_path / "nope.env")
    monkeypatch.delenv("APP_BIND_HOST", raising=False)

    assert launcher.bind_host() == "127.0.0.1"


# ---------------------------------------------------------------------------
# 浏览器地址
# ---------------------------------------------------------------------------


def test_browse_url_uses_given_host():
    assert launcher.browse_url("10.0.0.5", 8000) == "http://10.0.0.5:8000/"


def test_browse_url_never_uses_wildcard(monkeypatch):
    """0.0.0.0 不是可访问地址，浏览器打开会直接失败。"""
    monkeypatch.setattr(launcher, "_primary_ipv4", lambda: "10.0.0.9")

    assert launcher.browse_url("0.0.0.0", 8000) == "http://10.0.0.9:8000/"


def test_browse_url_falls_back_when_offline(monkeypatch):
    monkeypatch.setattr(launcher, "_primary_ipv4", lambda: None)

    assert launcher.browse_url("0.0.0.0", 8000) == "http://127.0.0.1:8000/"


# ---------------------------------------------------------------------------
# 回环判定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts(host):
    assert launcher._is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "192.168.1.1"])
def test_non_loopback_hosts(host):
    assert launcher._is_loopback(host) is False


# ---------------------------------------------------------------------------
# 端口探测
# ---------------------------------------------------------------------------


def test_port_available_on_free_port():
    assert launcher.port_available(0) is True


def test_port_probe_checks_requested_host():
    """探测必须按实际绑定地址做：绑内网口时不能只看回环口。"""
    assert launcher.port_available(0, "127.0.0.1") is True


def test_pick_port_respects_bind_host(env_file, monkeypatch):
    """pick_port 应把 bind_host 传下去，而不是固定探测回环。"""
    env_file.write_text("APP_BIND_HOST=0.0.0.0\n", encoding="utf-8")
    seen: list[str] = []

    def fake_available(port: int, host: str = "127.0.0.1") -> bool:
        seen.append(host)
        return True

    monkeypatch.setattr(launcher, "port_available", fake_available)
    launcher.pick_port(8000)

    assert seen == ["0.0.0.0"]
