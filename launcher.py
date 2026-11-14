#!/usr/bin/env python
"""一键启动器：给不熟悉命令行的运维人员用。

设计目标：双击 start.bat 之后，不需要输入任何命令，也不需要理解虚拟环境。

它会依次完成：
  1. 检查 Python 版本（要求 3.14+）
  2. 首次运行时创建 .venv 并装好依赖
  3. 生成 .env（自动填好加密密钥，避免"启动即报配置缺失"）
  4. 建表（migrate）
  5. 选一个空闲端口启动服务
  6. 自动打开浏览器
  7. Ctrl+C 时连子进程一起干净退出

本文件只依赖标准库：它要在"还没装任何依赖"的环境里跑起来。
"""

from __future__ import annotations

import argparse
import base64
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

MIN_PYTHON = (3, 14)
DEFAULT_PORT = 8000

# Celery worker 的并发数。采集要等目标库响应（网络 IO），线程池更合适；
# Windows 上 celery 的 prefork 默认池不可用，必须显式 -P threads。
CELERY_CONCURRENCY = 4
# beat 的调度状态文件。显式落到项目根目录，避免散落在启动时的工作目录。
CELERY_BEAT_SCHEDULE_FILE = ROOT / "celerybeat-schedule"
PORT_SCAN_LIMIT = 20

# 国内镜像：PyPI 直连经常超时，先走镜像，失败再回退官方源
PIP_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
PIP_HOST = "mirrors.aliyun.com"


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------


def _configure_io() -> None:
    """Windows 下 Python 默认按系统代码页（GBK）输出，重定向到管道时中文会乱码。

    这里统一改成 UTF-8，与 start.bat 里的 PYTHONUTF8=1 相互兜底。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                # 极端情况下（比如 stdout 已被替换）保持原样即可
                pass


def info(msg: str) -> None:
    print(f"  {msg}", flush=True)


def step(index: int, total: int, msg: str) -> None:
    print(f"\n[{index}/{total}] {msg}", flush=True)


def fail(msg: str, hint: str = "") -> int:
    print(f"\n[错误] {msg}", flush=True)
    if hint:
        print(f"       {hint}", flush=True)
    return 1


# ---------------------------------------------------------------------------
# 环境判断
# ---------------------------------------------------------------------------


def in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def in_project_venv() -> bool:
    """当前解释器是否就是项目自己的 .venv。"""
    return in_venv() and Path(sys.prefix).resolve() == VENV_DIR.resolve()


def utf8_child_env() -> dict[str, str]:
    """给子进程用的环境变量：强制 UTF-8，避免中文在 cmd 里乱码。"""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # 直接赋值而不是 setdefault：这里必须保证编码正确，不能被外部旧值影响
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def venv_python() -> Path:
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def venv_ready() -> bool:
    python = venv_python()
    if not python.exists():
        return False
    # 虚拟环境可能被复制或移动，pyvenv.cfg 是判断完整性的最小依据
    return (VENV_DIR / "pyvenv.cfg").exists()


# ---------------------------------------------------------------------------
# 步骤 1：Python 版本
# ---------------------------------------------------------------------------


def check_python() -> int:
    current = sys.version_info[:2]
    if current < MIN_PYTHON:
        return fail(
            f"当前 Python 版本是 {platform.python_version()}，本项目需要 "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}+。",
            "请到 https://www.python.org/downloads/ 安装新版 Python，"
            "安装时勾选 Add Python to PATH。",
        )
    info(f"Python {platform.python_version()} —— 符合要求")
    return 0


# ---------------------------------------------------------------------------
# 步骤 2：虚拟环境
# ---------------------------------------------------------------------------


def create_venv() -> int:
    """创建项目虚拟环境。仅在 .venv 尚不存在时调用。"""
    info(f"首次运行，正在创建虚拟环境 {VENV_DIR.name} ...")
    result = subprocess.run(
        [sys.executable, "-m", "venv", str(VENV_DIR)],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        return fail(
            "创建虚拟环境失败。",
            "可尝试手动执行：python -m venv .venv",
        )

    if not venv_python().exists():
        return fail(f"虚拟环境里没有找到解释器：{venv_python()}")
    return 0


def reexec_in_venv() -> int:
    """用 .venv 的解释器重跑本脚本（带上原始参数）。"""
    result = subprocess.run(
        [str(venv_python()), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(ROOT),
        env=utf8_child_env(),
    )
    return result.returncode


# ---------------------------------------------------------------------------
# 步骤 3：依赖
# ---------------------------------------------------------------------------


REQUIRED_PACKAGES = [
    ("django", "Django"),
    ("ninja", "django-ninja"),
    ("celery", "celery"),
    ("cryptography", "cryptography"),
    # DEBUG=False 时靠它提供 /static/，缺了页面会没样式没脚本
    ("whitenoise", "whitenoise"),
    # 生产 WSGI 服务器；纯 Python，Windows 上无需编译
    ("waitress", "waitress"),
]


def _installed_version(dist_name: str) -> str | None:
    from importlib import metadata

    try:
        return metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return None


def _version_tuple(text: str) -> tuple[int, ...]:
    """把 '6.1.3' 这类版本号转成可比元组，遇到非数字段直接截断。"""
    parts: list[int] = []
    for chunk in text.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def deps_missing() -> list[str]:
    missing: list[str] = []
    for module, dist in REQUIRED_PACKAGES:
        if _installed_version(dist) is None:
            missing.append(dist)
            continue
        if module == "django":
            version = _installed_version(dist) or "0"
            if _version_tuple(version) < (6, 1, 3):
                missing.append(f"{dist}(需 6.1.3+，当前 {version})")
    return missing


def _pip_install(extra_args: list[str]) -> int:
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS), *extra_args]
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def ensure_dependencies() -> int:
    missing = deps_missing()
    if not missing:
        versions = ", ".join(
            f"{name}={_installed_version(name)}" for _, name in REQUIRED_PACKAGES
        )
        info(f"依赖齐全（{versions}）")
        return 0

    info(f"缺少依赖：{', '.join(missing)}")
    info("正在安装（首次安装约 1-2 分钟，请耐心等待）...")

    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "pip", "-q"],
        cwd=str(ROOT),
    )

    code = _pip_install(["-i", PIP_INDEX, "--trusted-host", PIP_HOST])
    if code != 0:
        info("镜像源安装失败，改用官方源重试 ...")
        code = _pip_install([])
    if code != 0:
        return fail(
            "依赖安装失败。",
            "请检查网络连接；如公司网络需要代理，请先配置 HTTP_PROXY 环境变量。",
        )

    still_missing = deps_missing()
    if still_missing:
        return fail(f"安装后仍缺少：{', '.join(still_missing)}")
    info("依赖安装完成")
    return 0


# ---------------------------------------------------------------------------
# 步骤 4：配置文件
# ---------------------------------------------------------------------------


def _generate_fernet_key() -> str:
    """等价于 Fernet.generate_key()，但不需要预先安装 cryptography。"""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


# 与 config/settings.py 共用同一套判定规则（common.env.secret_key_problem）。
# launcher 在此处只负责"从 .env 文本里把值抠出来"，规则本身不复制——
# 两处规则漂移的后果是"启动器认为合法、Django 认为非法"（用户看到报错退出）
# 或"启动器漏修、Django 拒绝启动"，两种都很难排查。
# common.env 只依赖标准库，因此可以在装依赖之前被 import。
try:
    from common.env import secret_key_problem
except ImportError:  # 极端情况：launcher 被单独复制到别处运行
    def secret_key_problem(value: str | None) -> str | None:  # type: ignore[misc]
        if not value or not value.strip():
            return "缺失"
        return None


def _secret_key_problem(content: str) -> str | None:
    """返回现有 DJANGO_SECRET_KEY 的问题描述；没问题返回 None。

    为什么启动器要主动修：settings.py 已经把占位密钥变成硬失败，
    若用户是"复制 .env.example 得到 .env"，双击启动会直接报错退出。
    面向小白运维的一键启动不该停在这里——密钥本来就该由程序生成。
    """
    value = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("DJANGO_SECRET_KEY="):
            value = stripped[len("DJANGO_SECRET_KEY="):].strip()
            break

    return secret_key_problem(value)


def ensure_env_file() -> int:
    if ENV_FILE.exists():
        content = ENV_FILE.read_text(encoding="utf-8")
        changed = False

        # 已有配置但密钥为空时补齐，避免启动时报 ImproperlyConfigured
        if not _has_key(content, "FIELD_ENCRYPTION_KEY"):
            content = content.rstrip("\n") + f"\nFIELD_ENCRYPTION_KEY={_generate_fernet_key()}\n"
            changed = True
            info("已为现有 .env 追加加密密钥")
        elif not _has_value(content, "FIELD_ENCRYPTION_KEY"):
            content = _replace_value(content, "FIELD_ENCRYPTION_KEY", _generate_fernet_key())
            changed = True
            info("已为现有 .env 补写加密密钥")

        # 占位/过短的 SECRET_KEY 会让 settings.py 拒绝启动，这里顺手换成随机值。
        # 换 SECRET_KEY 的代价只是已登录会话失效，不影响密文数据。
        problem = _secret_key_problem(content)
        if problem:
            new_key = secrets.token_urlsafe(50)
            if _has_key(content, "DJANGO_SECRET_KEY"):
                content = _replace_value(content, "DJANGO_SECRET_KEY", new_key)
            else:
                content = content.rstrip("\n") + f"\nDJANGO_SECRET_KEY={new_key}\n"
            changed = True
            info(f"SECRET_KEY {problem}，已生成新的随机密钥（旧登录会话将失效）")

        if changed:
            ENV_FILE.write_text(content, encoding="utf-8")
        else:
            info(".env 已存在，沿用现有配置")
        return 0

    template = ENV_EXAMPLE.read_text(encoding="utf-8") if ENV_EXAMPLE.exists() else ""
    content = template
    if _has_key(template, "FIELD_ENCRYPTION_KEY"):
        content = _replace_value(content, "FIELD_ENCRYPTION_KEY", _generate_fernet_key())
    else:
        content += f"\nFIELD_ENCRYPTION_KEY={_generate_fernet_key()}\n"

    if _has_key(content, "DJANGO_SECRET_KEY"):
        content = _replace_value(content, "DJANGO_SECRET_KEY", secrets.token_urlsafe(50))

    header = (
        "# 本文件由 launcher.py 自动生成，包含加密密钥。\n"
        "# 请勿提交到代码仓库，也请离线备份 FIELD_ENCRYPTION_KEY——\n"
        "# 密钥丢失后，已加密的密码将无法解密。\n\n"
    )
    ENV_FILE.write_text(header + content, encoding="utf-8")
    info(f"已生成配置文件 {ENV_FILE.name}（自动填好加密密钥）")
    return 0


def _has_key(content: str, key: str) -> bool:
    return any(line.strip().startswith(f"{key}=") for line in content.splitlines())


def _has_value(content: str, key: str) -> bool:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return bool(stripped[len(key) + 1 :].strip())
    return False


def _replace_value(content: str, key: str, value: str) -> str:
    lines = []
    replaced = False
    for line in content.splitlines():
        if line.strip().startswith(f"{key}=") and not replaced:
            lines.append(f"{key}={value}")
            replaced = True
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def _env_file_get(key: str, default: str) -> str:
    """从 .env 文本里取一个值；缺失或为空时返回 default。

    启动器在 Django 加载配置之前就要决定监听地址，因此不能直接读
    ``django.conf.settings``（那时还没有 settings）。规则与
    ``common.env.env`` 保持一致：真实环境变量优先，.env 只作补充。
    """
    if (value := os.environ.get(key)) and value.strip():
        return value.strip()

    if not ENV_FILE.is_file():
        return default

    try:
        content = ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return default

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() != key:
            continue
        value = value.strip().strip("'\"")
        return value or default
    return default


# ---------------------------------------------------------------------------
# 步骤 5：数据库
# ---------------------------------------------------------------------------


def run_migrate() -> int:
    info("正在初始化数据库（建表）...")
    result = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput"],
        cwd=str(ROOT),
        env=utf8_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        print(result.stdout[-2000:], flush=True)
        print(result.stderr[-2000:], flush=True)
        return fail(
            "数据库初始化失败。",
            "可手动执行 python manage.py migrate 查看详细错误。",
        )
    info("数据库已就绪")
    return 0


def run_collectstatic() -> int:
    """把 static/ 收集到 staticfiles/，供 whitenoise 直接对外提供。

    DEBUG=False 时 runserver 不再服务 /static/，页面样式与脚本全靠这一步。
    重复执行是安全的（覆盖写入），但为避免残留旧文件先清空目标目录。
    """
    info("正在收集静态文件（页面样式与脚本）...")
    result = subprocess.run(
        [
            sys.executable,
            "manage.py",
            "collectstatic",
            "--noinput",
            "--clear",
            "-v",
            "0",
        ],
        cwd=str(ROOT),
        env=utf8_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        print(result.stdout[-2000:], flush=True)
        print(result.stderr[-2000:], flush=True)
        return fail(
            "静态文件收集失败。",
            "可手动执行 python manage.py collectstatic --noinput 查看详细错误。",
        )
    info("静态文件已就绪")
    return 0


def ensure_admin_account() -> int:
    """确保后台管理员账号存在。重复执行安全，不会覆盖已修改的密码。"""
    result = subprocess.run(
        [sys.executable, "manage.py", "ensure_admin"],
        cwd=str(ROOT),
        env=utf8_child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        # 后台账号非核心功能，失败不应挡住整个启动流程
        info("管理员账号创建失败，可稍后手动执行 python manage.py ensure_admin")
        return 0

    for line in result.stdout.splitlines():
        if line.strip():
            info(line.strip())
    return 0


# ---------------------------------------------------------------------------
# 步骤 6：端口
# ---------------------------------------------------------------------------


def port_available(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否可绑定。

    按**实际要绑的地址**探测，而不是固定 127.0.0.1：绑 0.0.0.0 时若只看
    回环口，可能出现"回环空闲但内网口已被占用"的误判，等到 waitress
    启动才失败，错误信息对小白运维毫无指引。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(preferred: int) -> int:
    host = bind_host()
    probe_host = "0.0.0.0" if host in {"0.0.0.0", "::"} else host
    for offset in range(PORT_SCAN_LIMIT):
        candidate = preferred + offset
        if port_available(candidate, probe_host):
            return candidate
    return preferred


def bind_host() -> str:
    """监听地址：默认 127.0.0.1，可用 .env 的 APP_BIND_HOST 改成内网地址。

    默认只绑回环是刻意的：这套系统持有全部生产库的口令，装在谁的机器上
    不等于该对整张内网开放。放开监听是一个由人明确做出的决定。
    """
    return _env_file_get("APP_BIND_HOST", "127.0.0.1")


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def browse_url(host: str, port: int) -> str:
    """给「打开浏览器用」的地址。

    绑 0.0.0.0 时浏览器不能访问 0.0.0.0（Windows 上会直接失败），
    必须换成本机实际 IP 或回环地址。
    """
    if host in {"0.0.0.0", "::"}:
        lan = _primary_ipv4()
        return f"http://{lan or '127.0.0.1'}:{port}/"
    return f"http://{host}:{port}/"


def _primary_ipv4() -> str | None:
    """本机出口网卡的 IPv4；离线环境返回 None。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 步骤 7：启动服务
# ---------------------------------------------------------------------------


def wait_until_ready(port: int, timeout: float = 45.0) -> bool:
    """轮询 healthz，确认服务真的能响应再打开浏览器。

    固定探测回环地址：服务即使绑定在内网 IP 上，同一台机器的回环口
    通常也可达；而绑 0.0.0.0 时探测本机内网 IP 反而可能被防火墙拦。
    这里只关心"进程是否起来了"，不关心"谁能访问"。
    """
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/healthz"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def _broker_reachable(url: str, timeout: float = 0.5) -> bool:
    """启动器侧的 broker 可达性探测。

    不能复用 ``common.scheduler.probe_broker``：这个时刻 Django settings
    还没加载。判定规则刻意保持一致（只做 TCP 握手），避免启动器与 Web
    进程对"能不能用 Celery"得出相反结论。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("redis", "rediss", "amqp", "amqps"):
        return False
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (6379 if parsed.scheme.startswith("redis") else 5672)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def want_celery() -> bool:
    """启动器是否应该拉起 celery worker 与 beat。

    - ``SCHEDULER_MODE=celery``：明确要求，必须拉起。
    - ``SCHEDULER_MODE=inprocess``：明确不要。
    - ``auto``：broker 可达就拉起——装了 Redis 的机器应当真的用上 Celery，
      而不是"有 Redis 却在 Web 进程里跑定时任务"。不可达则不起，
      保持"零配置也能跑"的承诺，由 Web 进程内调度兜底。
    """
    mode = (_env_file_get("SCHEDULER_MODE", "auto") or "auto").strip().lower()
    if mode == "inprocess":
        return False
    if mode == "celery":
        return True
    return _broker_reachable(_env_file_get("CELERY_BROKER_URL", ""))


def _celery_commands() -> list[tuple[str, list[str], str]]:
    """要拉起的 celery 进程：(名称, 命令, 日志文件名)。"""
    base = [sys.executable, "-m", "celery", "-A", "config"]
    worker = base + [
        "worker",
        "-l",
        "info",
        # 轮换任务走 rotation 队列，其余走 default（见 settings.CELERY_TASK_ROUTES）
        "-Q",
        "default,rotation",
        # Windows 上 celery 的 prefork 默认池不可用，必须显式线程池
        "-P",
        "threads",
        "-c",
        str(CELERY_CONCURRENCY),
        # 每处理若干任务回收一次子线程，避免长跑进程内存缓慢增长
        "--max-tasks-per-child",
        "200",
    ]
    beat = base + [
        "beat",
        "-l",
        "info",
        "--schedule",
        str(CELERY_BEAT_SCHEDULE_FILE),
    ]
    return [
        ("worker", worker, "celery-worker.log"),
        ("beat", beat, "celery-beat.log"),
    ]


def _log_tail(path: Path, lines: int = 15) -> str:
    """取日志末尾若干行，用于把启动失败的原因直接显示给用户。"""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


def start_celery() -> tuple[list[tuple[str, subprocess.Popen]], list[Path]]:
    """拉起 worker 与 beat，返回 (已启动进程, 日志文件)。

    顺序上 worker 先、beat 后：beat 只负责按点发任务，worker 才是执行方。

    子进程输出重定向到日志文件而不是继承控制台：两个 celery 进程的
    INFO 日志非常密，混进启动器界面会把关键提示冲掉；出问题时按提示
    去看日志文件即可。

    启动失败不抛异常、也不让整个服务起不来——定时任务降级由调用方处理。
    """
    started: list[tuple[str, subprocess.Popen]] = []
    logs: list[Path] = []

    for name, cmd, log_name in _celery_commands():
        log_path = ROOT / log_name
        logs.append(log_path)
        try:
            with open(log_path, "ab") as handle:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(ROOT),
                    env=utf8_child_env(),
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                )
        except OSError:
            continue
        started.append((name, proc))

    # 给它们一点时间暴露启动期错误（缺依赖、配置错误、端口/文件占用）。
    # 不这么做的话，进程秒退会被误当成"已启动"，页面转而显示一切正常。
    time.sleep(2.5)
    healthy: list[tuple[str, subprocess.Popen]] = []
    for name, proc in started:
        if proc.poll() is not None:
            log_path = ROOT / f"celery-{name}.log"
            info(f"celery {name} 启动失败（退出码 {proc.returncode}），日志：{log_path}")
            tail = _log_tail(log_path)
            if tail:
                print(tail, flush=True)
            continue
        healthy.append((name, proc))

    return healthy, logs


def stop_processes(procs: list[tuple[str, subprocess.Popen]]) -> None:
    """终止一组子进程（先温和，再强杀）。

    必须显式清理：celery 的 worker/beat 不会因为父进程退出而自动结束，
    残留下来会和下次启动的实例抢同一批任务与 beat 调度文件。
    """
    for name, proc in procs:
        if proc.poll() is not None:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                info(f"{name} 进程未能正常退出，请手动结束")
        except OSError:
            pass


def serve(port: int, open_browser: bool) -> int:
    host = bind_host()
    url = browse_url(host, port)
    # 用 waitress 而不是 runserver：
    # - runserver 只有 DEBUG=True 时才服务 /static/，生产下页面会丢样式与脚本；
    # - runserver 是开发服务器，单进程单线程、无并发保护。
    # waitress 是纯 Python 的多线程 WSGI 服务器，Windows 上零编译依赖。
    cmd = [
        sys.executable,
        "-m",
        "waitress",
        f"--listen={host}:{port}",
        # 控制台执行语句可能连目标库数秒，线程数留足避免互相阻塞
        "--threads=8",
        # 探测/轮换页面的单次请求上限略高于后端 5 秒预算
        "--channel-timeout=120",
        "config.wsgi:application",
    ]

    print("\n" + "=" * 58, flush=True)
    print("  数据库实例管理系统已启动", flush=True)
    print(f"  界面地址：{url}", flush=True)
    print(f"  接口文档：{url}api/v1/docs", flush=True)
    print(f"  监听地址：{host}:{port}", flush=True)
    if not _is_loopback(host):
        # 放开监听后要提醒一次：口令台账从此对内网可达，
        # 靠的是登录鉴权而不是网络隔离。
        print(
            "  注意：已对其它机器开放访问，请确认已修改默认管理员口令",
            flush=True,
        )
    print("  关闭窗口或按 Ctrl+C 即可停止服务", flush=True)
    print("=" * 58 + "\n", flush=True)

    # ------------------------------------------------------------------
    # 先起 celery，再起 Web。
    #
    # 顺序很关键：Web 启动时 auto 模式会探测"有没有 worker 在消费"，
    # 若 worker 还没起来就会降级为进程内调度——那就等于没用上 Celery。
    # ------------------------------------------------------------------
    celery_procs: list[tuple[str, subprocess.Popen]] = []
    if want_celery():
        info("正在启动 Celery worker 与 beat（定时任务由它们执行）...")
        celery_procs, celery_logs = start_celery()
        if celery_procs:
            names = "、".join(name for name, _ in celery_procs)
            info(f"Celery 已启动：{names}")
            info(f"日志：{'、'.join(str(p) for p in celery_logs)}")
        else:
            # 起不来不能挡住 Web：否则运维连界面都打不开，更没法排查。
            # Web 侧会因探测不到 worker 自动降级为进程内调度，任务照常执行。
            info(
                "Celery 未能启动，本次改由 Web 进程内调度执行定时任务；"
                "排查后可重启，详见上方日志路径"
            )
    else:
        info("未启用 Celery，定时任务由 Web 进程内调度执行")

    # 用子进程而不是同进程调用，Ctrl+C 时才能干净地连带退出
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=utf8_child_env())

    if open_browser:
        if wait_until_ready(port):
            webbrowser.open(url)
        else:
            info(f"服务启动较慢，请手动打开 {url}")

    try:
        return proc.wait()
    except KeyboardInterrupt:
        print("\n正在停止服务 ...", flush=True)
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        # celery 进程不会随父进程退出而自动结束，必须显式清理：
        # 残留下来会和下次启动的实例抢同一批任务与 beat 调度文件。
        if celery_procs:
            stop_processes(celery_procs)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def self_check() -> int:
    """自检模式：只做环境检查，不启动服务（便于排查问题）。

    调用前 main() 已确保运行在项目 .venv 中，因此这里看到的依赖状态
    就是实际运行环境，不会出现"系统 Python 里没装 Django"的误报。
    """
    print("=" * 58, flush=True)
    print("  环境自检", flush=True)
    print("=" * 58, flush=True)

    step(1, 5, "检查 Python 版本")
    if (code := check_python()) != 0:
        return code

    step(2, 5, "检查虚拟环境")
    if in_project_venv():
        info(f"当前运行在虚拟环境中：{VENV_DIR}")
    elif venv_ready():
        info(f"虚拟环境存在：{VENV_DIR}")
    else:
        info("虚拟环境尚未创建（首次启动时会自动创建）")

    step(3, 5, "检查依赖")
    missing = deps_missing()
    if missing:
        info(f"缺少：{', '.join(missing)}（首次启动时会自动安装）")
    else:
        for _, name in REQUIRED_PACKAGES:
            info(f"{name} {_installed_version(name)}")

    step(4, 5, "检查配置文件")
    info(".env 已存在" if ENV_FILE.exists() else ".env 不存在（首次启动时会自动生成）")

    step(5, 5, "检查数据库")
    db_file = ROOT / "db.sqlite3"
    info(
        f"SQLite 文件已存在：{db_file.name}"
        if db_file.exists()
        else "数据库尚未创建（首次启动时会自动建表）"
    )

    print("\n自检完成。", flush=True)
    return 0


def _print_banner() -> None:
    print("=" * 58, flush=True)
    print("  数据库实例管理系统 —— 启动器", flush=True)
    print("=" * 58, flush=True)


def main() -> int:
    _configure_io()

    parser = argparse.ArgumentParser(
        description="数据库实例管理系统 —— 一键启动器",
        add_help=True,
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 8000")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument(
        "--check", action="store_true", help="只做环境自检，不启动服务"
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # 阶段一：切换到项目 .venv
    #
    # 依赖装在 .venv 里。若用系统 Python 继续跑，会误报"缺少 Django"，
    # 因此这里用 .venv 的解释器重跑自己。横幅不在此阶段打印，
    # 否则父子进程各打一次，界面上会出现两套提示。
    # ------------------------------------------------------------------
    if not in_project_venv():
        if not venv_ready():
            if args.check:
                # 自检保持无副作用：不创建 .venv，如实报告当前状态
                _print_banner()
                return self_check()
            if (code := check_python()) != 0:
                return code
            if (code := create_venv()) != 0:
                return code
        raise SystemExit(reexec_in_venv())

    # ------------------------------------------------------------------
    # 阶段二：以下全部在 .venv 内执行，提示只打印一套
    # ------------------------------------------------------------------
    _print_banner()

    if args.check:
        return self_check()

    total = 7
    step(1, total, "检查 Python 版本")
    if (code := check_python()) != 0:
        return code

    step(2, total, "准备虚拟环境")
    info(f"已在虚拟环境中：{VENV_DIR}")

    step(3, total, "检查依赖")
    if (code := ensure_dependencies()) != 0:
        return code

    step(4, total, "准备配置文件")
    if (code := ensure_env_file()) != 0:
        return code

    step(5, total, "初始化数据库")
    if (code := run_migrate()) != 0:
        return code
    ensure_admin_account()

    step(6, total, "收集静态文件")
    if (code := run_collectstatic()) != 0:
        return code

    step(7, total, "启动服务")
    port = pick_port(args.port)
    if port != args.port:
        info(f"端口 {args.port} 被占用，改用 {port}")
    return serve(port, open_browser=not args.no_browser)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。", flush=True)
        sys.exit(130)
