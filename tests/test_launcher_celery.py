"""启动器的 Celery 生命周期测试。

为什么值得单独测
----------------
「用 Celery」这件事有两个失败方向，都很安静：

- **不起进程**：`SCHEDULER_MODE=celery` 但启动器从不拉 worker/beat。
  Web 进程自己降级或干脆没有调度器，页面却显示"Celery 已连接"，
  结果是密码不轮换、指标不采集，而界面上一切正常。
- **起错方式**：Windows 上用 celery 默认的 prefork 池会直接起不来；
  beat 不指定 `--schedule` 会把调度状态散落在启动时的工作目录。

这里只验证「命令怎么拼」与「要不要起」这类可离线判断的部分，
不真的去启动进程（真实拉起由端到端冒烟覆盖）。
"""

from __future__ import annotations

import launcher
import pytest


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """指向临时 .env，避免读到仓库里的真实配置。"""
    path = tmp_path / ".env"
    monkeypatch.setattr(launcher, "ENV_FILE", path)
    for key in ("SCHEDULER_MODE", "CELERY_BROKER_URL"):
        monkeypatch.delenv(key, raising=False)
    return path


# ---------------------------------------------------------------------------
# 是否拉起 Celery
# ---------------------------------------------------------------------------


def test_explicit_celery_always_starts(env_file, monkeypatch):
    env_file.write_text("SCHEDULER_MODE=celery\n", encoding="utf-8")
    # 明确要求时不该再被 broker 探测结果影响
    monkeypatch.setattr(launcher, "_broker_reachable", lambda *a, **k: False)
    assert launcher.want_celery() is True


def test_explicit_inprocess_never_starts(env_file, monkeypatch):
    env_file.write_text("SCHEDULER_MODE=inprocess\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_broker_reachable", lambda *a, **k: True)
    assert launcher.want_celery() is False


def test_auto_starts_when_broker_reachable(env_file, monkeypatch):
    """装了 Redis 就该真的用上 Celery，而不是在 Web 进程里跑定时任务。"""
    env_file.write_text(
        "SCHEDULER_MODE=auto\nCELERY_BROKER_URL=redis://127.0.0.1:6379/0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "_broker_reachable", lambda *a, **k: True)
    assert launcher.want_celery() is True


def test_auto_skips_when_broker_down(env_file, monkeypatch):
    """零配置机器不该因为拉不起 Celery 就起不来服务。"""
    env_file.write_text(
        "SCHEDULER_MODE=auto\nCELERY_BROKER_URL=redis://127.0.0.1:6399/0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "_broker_reachable", lambda *a, **k: False)
    assert launcher.want_celery() is False


def test_broker_reachable_rejects_non_broker_scheme():
    assert launcher._broker_reachable("sqlite:///tmp/x.db") is False
    assert launcher._broker_reachable("") is False


# ---------------------------------------------------------------------------
# 命令拼装
# ---------------------------------------------------------------------------


def test_celery_commands_cover_worker_and_beat(env_file):
    commands = launcher._celery_commands()
    names = [name for name, _cmd, _log in commands]

    assert names == ["worker", "beat"]


def test_worker_uses_threads_pool(env_file):
    """Windows 上 celery 的 prefork 默认池不可用，必须显式 -P threads。"""
    worker = next(cmd for name, cmd, _log in launcher._celery_commands() if name == "worker")

    assert "-P" in worker
    assert worker[worker.index("-P") + 1] == "threads"


def test_worker_consumes_both_queues(env_file):
    """轮换任务走 rotation 队列，只订阅 default 会永不执行。"""
    worker = next(cmd for name, cmd, _log in launcher._celery_commands() if name == "worker")
    queues = worker[worker.index("-Q") + 1]

    assert "default" in queues
    assert "rotation" in queues


def test_beat_has_explicit_schedule_file(env_file):
    """不指定 --schedule，beat 会把状态文件落在启动时的工作目录。"""
    beat = next(cmd for name, cmd, _log in launcher._celery_commands() if name == "beat")

    assert "--schedule" in beat
    assert beat[beat.index("--schedule") + 1] == str(launcher.CELERY_BEAT_SCHEDULE_FILE)


def test_commands_use_current_interpreter(env_file):
    """必须用 .venv 的解释器，否则会用到系统 Python 里缺依赖的 celery。"""
    import sys

    for _name, cmd, _log in launcher._celery_commands():
        assert cmd[0] == sys.executable
        assert cmd[1:4] == ["-m", "celery", "-A"]


def test_log_files_are_dedicated(env_file):
    """每个进程一个日志文件：混在一起无法判断是谁启动失败。"""
    logs = [log for _name, _cmd, log in launcher._celery_commands()]

    assert logs == ["celery-worker.log", "celery-beat.log"]
