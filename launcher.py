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


# ---------------------------------------------------------------------------
# 步骤 5：数据库
# ---------------------------------------------------------------------------


def run_migrate() -> int:
    info("正在初始化数据库（建表）...")
    result = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--noinput"],
        cwd=str(ROOT),
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


def ensure_admin_account() -> int:
    """确保后台管理员账号存在。重复执行安全，不会覆盖已修改的密码。"""
    result = subprocess.run(
        [sys.executable, "manage.py", "ensure_admin"],
        cwd=str(ROOT),
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


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(preferred: int) -> int:
    for offset in range(PORT_SCAN_LIMIT):
        candidate = preferred + offset
        if port_available(candidate):
            return candidate
    return preferred


# ---------------------------------------------------------------------------
# 步骤 7：启动服务
# ---------------------------------------------------------------------------


def wait_until_ready(port: int, timeout: float = 45.0) -> bool:
    """轮询 healthz，确认服务真的能响应再打开浏览器。"""
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


def serve(port: int, open_browser: bool) -> int:
    url = f"http://127.0.0.1:{port}/"
    cmd = [
        sys.executable,
        "manage.py",
        "runserver",
        f"127.0.0.1:{port}",
        "--noreload",
    ]

    print("\n" + "=" * 58, flush=True)
    print("  数据库实例管理系统已启动", flush=True)
    print(f"  界面地址：{url}", flush=True)
    print(f"  接口文档：{url}api/v1/docs", flush=True)
    print("  关闭窗口或按 Ctrl+C 即可停止服务", flush=True)
    print("=" * 58 + "\n", flush=True)

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

    total = 6
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

    step(6, total, "启动服务")
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
