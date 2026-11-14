"""Django 配置。

设计要点：
- 全部配置来自环境变量，仓库不保存任何密钥。
- 默认 SQLite，开箱即用；设置 DB_ENGINE=postgresql 即切换到 PostgreSQL。
- FIELD_ENCRYPTION_KEY 缺失或格式非法时直接抛 ImproperlyConfigured，
  不允许以"明文降级"的方式启动。
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

from common.env import (
    env,
    env_bool,
    env_int,
    env_list,
    load_env_file,
    secret_key_problem,
)

BASE_DIR = Path(__file__).resolve().parent.parent

load_env_file(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY")


def _local_ipv4_addresses() -> list[str]:
    """本机可访问的 IPv4 地址，用于监听内网时自动补全 ALLOWED_HOSTS。

    不引入第三方库：用一个 UDP socket "连"到公网地址只为让内核选出一条
    出口网卡，从而拿到它的本地地址（UDP 不握手，不会真的发包）。
    在完全断网的机器上这一步会失败，此时返回空列表——宁可少补几个
    主机名，也不要把 ``*`` 写进白名单。
    """
    addresses: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("8.8.8.8", 80))
            addresses.append(probe.getsockname()[0])
    except OSError:
        pass

    try:
        # 主机名解析可能给出更多地址（多网卡机器）
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.append(info[4][0])
    except OSError:
        pass

    return [a for a in dict.fromkeys(addresses) if a and a != "0.0.0.0"]


def _validate_secret_key(key: str | None) -> None:
    """拒绝缺失、占位符、以及过短的 SECRET_KEY。

    判定逻辑放在 ``common.env.secret_key_problem``：launcher.py 必须用
    **同一套**规则判断"这个 .env 需不需要修"。两处规则一旦漂移，
    结果就是启动器没修、或修了仍起不来。
    """
    problem = secret_key_problem(key)
    if problem is None:
        return

    raise ImproperlyConfigured(
        f"DJANGO_SECRET_KEY {problem}，拒绝启动。\n"
        "占位值在仓库里是公开的，用它签发的 session 可以被伪造。\n"
        '生成新值：python -c "import secrets; print(secrets.token_urlsafe(50))"\n'
        "（双击 start.bat 启动时，启动器会自动为你生成）"
    )


_validate_secret_key(SECRET_KEY)

# 默认 False：未显式开启即按生产口径运行（不回显 traceback / SQL / 配置）。
# 本地开发需要调试页时才在 .env 里写 DJANGO_DEBUG=True。
DEBUG = env_bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["127.0.0.1", "localhost"])

# 监听地址。默认**只绑本机回环**。
#
# 为什么默认不是 0.0.0.0
# ---------------------
# 这套系统里存着全部生产库的口令（加密后），"装在哪台机器上"不等于
# "该对整张内网开放"。默认绑回环，运维明确知道自己在做什么时才改成
# 内网地址——风险偏好应当由人一次性决定，而不是由默认值替他决定。
#
# 改成 0.0.0.0 / 内网 IP 后，浏览器用的是本机 IP 或域名作为 Host 头；
# 若不把这些地址放进 ALLOWED_HOSTS，访问会被 400 DisallowedHost 挡掉，
# 而且报错信息对小白运维毫无指引。所以这里按绑定值自动补齐。
BIND_HOST = (env("APP_BIND_HOST", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

if BIND_HOST not in _LOOPBACK_HOSTS:
    # 绑 0.0.0.0 时，真正的 Host 头是访问者的那个 IP，补 0.0.0.0 本身没用；
    # 因此把本机可解析到的 IPv4 一并加入（仍不是 "*"，未列出的域名照样拒绝）。
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, BIND_HOST, *_local_ipv4_addresses()]

# 测试进程需要 testserver；开发态放宽到任意本地绑定地址。
# 生产（DEBUG=False 且非测试）只认显式配置的白名单，不做任何自动放宽。
RUNNING_TESTS = "pytest" in sys.modules
if DEBUG or RUNNING_TESTS:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, "0.0.0.0", "testserver"]

# 去重但保序：重复项会让运维读 ALLOWED_HOSTS 时以为配了两遍
ALLOWED_HOSTS = list(dict.fromkeys(ALLOWED_HOSTS))


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 运行期基础设施（调度器生命周期），需在其他业务 app 之前
    "apps.runtime",
    # 业务应用
    "apps.assets",
    "apps.probing",
    "apps.credentials",
    "apps.metrics",
    # 目标库连接控制台（连目标库验证凭据、执行语句）
    "apps.dbconsole",
    # 目标库指标采集与观测（趋势/空间/会话/锁/告警）
    "apps.monitor",
    # 可视化界面（服务端渲染，面向运维人员）
    "apps.web",
]

MIDDLEWARE = [
    # 自定义耗时中间件必须靠前，才能覆盖后续全部处理时间
    "common.middleware.RequestTimingMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # 静态文件由应用进程直接提供：生产下 DEBUG=False，runserver 不再服务
    # /static/，没有这一层页面会丢样式与脚本（控制台直接不可用）
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # 必须在 AuthenticationMiddleware 之后：它要读 request.user。
    # 默认口令未改时把页面请求导回改密页——默认口令写在仓库里，
    # 等于人人可知的后台入口。
    "common.force_password_change.ForcePasswordChangeMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------


def _database_from_url(url: str) -> dict:
    """解析 DATABASE_URL，仅支持 postgres:// 与 postgresql://。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ImproperlyConfigured(
            f"DATABASE_URL 仅支持 postgres:// 前缀，收到：{parsed.scheme}://"
        )
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": (parsed.path or "/").lstrip("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "127.0.0.1",
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60),
    }


if env("DB_ENGINE", "sqlite") == "postgresql":
    _database_url = env("DATABASE_URL")
    if not _database_url:
        raise ImproperlyConfigured("DB_ENGINE=postgresql 时必须提供 DATABASE_URL。")
    DATABASES = {"default": _database_from_url(_database_url)}
else:
    # SQLite 并发写入稳定化：WAL + busy_timeout + BEGIN IMMEDIATE。
    # 详见 common/sqlite_writes.py 的模块说明（含实测数据）。
    from common.sqlite_writes import build_options as _sqlite_options

    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": _sqlite_options(
                busy_timeout_ms=env_int("DB_BUSY_TIMEOUT_MS", 5000),
                journal_mode=env("DB_JOURNAL_MODE", "WAL"),
                synchronous=env("DB_SYNCHRONOUS", "NORMAL"),
            ),
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# 国际化与静态资源
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

# 登录相关路由
# 不加这两行时，直接访问 /admin/login/ 登录成功会跳到 Django 默认的
# /accounts/profile/，而本项目没有该路由，用户会看到 404。
LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/admin/"
LOGOUT_REDIRECT_URL = "/admin/login/"

# 登录失败限流。
#
# 登录页是唯一的身份入口，此前没有任何失败计数：对 admin 这种固定用户名
# 可以直接在线爆破。两层限流缺一不可——只按 IP 挡不住内网多来源，
# 只按用户名挡不住用一份字典扫大量账号。
#
# 计数放在缓存里：它高频、短命、可丢弃，写数据库等于凭空多一个写热点。
#
# 默认 LocMemCache 是**进程内**的，waitress 单进程多线程足够。
# 改多进程部署（gunicorn 多 worker / 多个 waitress 进程）时必须换成共享缓存：
# 否则每个进程各记一份计数，阈值等效放大 N 倍，限流形同虚设。
# 配 CACHE_URL=redis://... 即自动切到 Redis，无需改代码。
_CACHE_URL = (env("CACHE_URL", "") or "").strip()

if _CACHE_URL.startswith(("redis://", "rediss://", "unix://")):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _CACHE_URL,
            # 缓存断了不应该让整站 500：限流计数丢失的代价远小于登录不可用
            "OPTIONS": {"socket_connect_timeout": 2, "socket_timeout": 2},
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "db_manager-locmem",
        }
    }

# 窗口内允许的失败次数与锁定时长（秒）。默认 5 次 / 15 分钟。
LOGIN_MAX_FAILURES = max(1, env_int("LOGIN_MAX_FAILURES", 5))
LOGIN_LOCKOUT_SECONDS = max(10, env_int("LOGIN_LOCKOUT_SECONDS", 900))

# IP 维度阈值单独放宽到 20：内网出口常共用 IP，若与用户名阈值相同，
# 同事 A 输错 5 次会把 B、C 一起锁在门外。IP 维度要拦的是"一份字典扫大量
# 用户名"的撞库，那种模式会在很短时间内把计数堆到几十，20 次足够触发。
LOGIN_MAX_FAILURES_PER_IP = max(
    LOGIN_MAX_FAILURES, env_int("LOGIN_MAX_FAILURES_PER_IP", 20)
)

# 口令强度校验。
#
# Django 的 AUTH_PASSWORD_VALIDATORS 默认是**空列表**——不配置等于
# `validate_password()` 什么都不检查，改密页会接受 "1" 这种口令。这不是
# "默认宽松"，是"默认没有防线"，必须显式声明。
#
# 这套账号能操作生产库的口令台账，按 NIST SP 800-63B 的思路取舍：
# 长度优先、不做组合强制（强制符号只会催生 "Passw0rd!" 这类可预测口令），
# 并拦掉与账号信息相似、以及常见弱口令表里的值。
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# 认证后端：在标准 ModelBackend 之前做锁定检查。
# 放在后端而不是某个登录视图里，是因为登录入口不止一个（admin、未来的
# 自定义登录页、任何 authenticate() 调用），视图层加防护会被新入口绕过。
AUTHENTICATION_BACKENDS = [
    "common.auth_backends.ThrottledModelBackend",
]

# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
#
# 不设 SESSION_COOKIE_AGE 时 Django 默认 2 周。这对一个能改目标库密码的
# 后台太长：运维在工位登录后没点退出，两周内任何拿到那台机器的人
# 直接就是登录态。
#
# 默认 12 小时：覆盖一个完整班次，加班到深夜也不会被踢；过了就要求重新
# 登录。注意这是**固定**过期——不是"空闲 12 小时"。之所以不做滑动续期
# （SESSION_SAVE_EVERY_REQUEST=True），是因为那会让每个请求都写一次
# session 表：本项目的写路径本来就要过 SQLite 写锁，为了"续期"把只读
# 请求也变成写请求，代价不划算。需要滑动过期时用环境变量单独打开。
SESSION_COOKIE_AGE = max(300, env_int("SESSION_COOKIE_AGE", 43200))
SESSION_SAVE_EVERY_REQUEST = env_bool("SESSION_SAVE_EVERY_REQUEST", False)

# 关闭浏览器即失效。默认关：内网工具，运维习惯让页面开着过夜；
# 真按安全基线执行时再打开（它会覆盖 SESSION_COOKIE_AGE 的效果）。
SESSION_EXPIRE_AT_BROWSER_CLOSE = env_bool("SESSION_EXPIRE_AT_BROWSER_CLOSE", False)

# 显式声明 SameSite：Django 默认就是 Lax，但写出来才能防止以后被误改。
# Lax 而不是 Strict——Strict 会让"从内部文档点链接进来"不带 cookie，
# 用户会看到自己像是没登录。
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# SESSION_COOKIE_HTTPONLY 不在这里设置：它属于安全基线，统一由下面的
# `if not DEBUG:` 块强制 True。同一项散落在两处，改了一处忘了另一处时
# 谁生效取决于语句先后顺序——本项目此前就在 TRUST_PROXY_HEADERS 上
# 踩过同样的坑。

# 必须带前导斜杠：写成 "static/" 时 {% static %} 会输出相对路径，
# 在深层页面（如 /instances/3/）会被解析成 /instances/3/static/... 而 404，
# 表现为"详情页样式全丢了、首页却正常"。
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# 页面样式全部本地化，不依赖 CDN：目标机器可能没有外网
STATICFILES_DIRS = [BASE_DIR / "static"]

# Django 5.1+ 的 STORAGES 写法
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # 用 Compressed 而非 Manifest 版本：Manifest 在 collectstatic 未执行时
        # 会直接抛异常（页面全挂），而本项目的静态文件是手工维护的少量资源，
        # gzip 压缩收益已经足够。
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"
    },
}

# whitenoise 直接读 STATIC_ROOT 提供服务，因此启动时必须先 collectstatic
WHITENOISE_ROOT = STATIC_ROOT


# ---------------------------------------------------------------------------
# 安全（生产环境通过环境变量收紧）
# ---------------------------------------------------------------------------

# 是否采信 X-Forwarded-For / X-Forwarded-Proto。默认关闭：
# 这些请求头客户端可任意伪造，直接采信会让审计日志里的来源 IP 变成假证据。
# 只有确实跑在反向代理（nginx / 公司网关）之后时才置 True。
#
# 必须定义在下面的安全块之前：SECURE_PROXY_SSL_HEADER 依赖它。
TRUST_PROXY_HEADERS = env_bool("TRUST_PROXY_HEADERS", False)

if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    # SECURE_BROWSER_XSS_FILTER 在 Django 4.0 起已废弃、Django 6 中已移除
    # （X-XSS-Protection 头部本身被主流浏览器弃用且可能引入漏洞），
    # 这里改用 Referrer-Policy，它才是当前仍有效的防线。
    SECURE_REFERRER_POLICY = "same-origin"
    SESSION_COOKIE_HTTPONLY = True
    CSRF_COOKIE_HTTPONLY = True
    X_FRAME_OPTIONS = "DENY"

    # --- HTTPS 相关 ------------------------------------------------------
    # 默认全部关闭，由 HTTPS_ENABLED 一次性开启。
    #
    # 为什么不默认打开 SECURE_SSL_REDIRECT：本系统面向内网运维，
    # 默认部署是 http://内网IP:8000。若默认开启重定向，用户打开页面会被
    # 跳到一个没有 TLS 监听的 https 地址，表现为"打不开"——服务其实
    # 好好的，却像是彻底挂了。因此必须由部署者显式声明"我在 TLS 后面"。
    HTTPS_ENABLED = env_bool("HTTPS_ENABLED", False)
    # 反向代理卸载 TLS 时，Django 只能从 X-Forwarded-Proto 得知原始协议
    SECURE_PROXY_SSL_HEADER = (
        ("HTTP_X_FORWARDED_PROTO", "https") if TRUST_PROXY_HEADERS else None
    )
    SECURE_SSL_REDIRECT = HTTPS_ENABLED
    SESSION_COOKIE_SECURE = HTTPS_ENABLED
    CSRF_COOKIE_SECURE = HTTPS_ENABLED

    # HSTS 一旦下发就会被浏览器记住，且**无法撤回**：设成 31536000 之后，
    # 在有效期内即使撤掉配置，浏览器仍会强制走 https。所以默认 0（不下发），
    # 确认长期只走 https 后再逐步调大（先 3600 试，再 31536000）。
    SECURE_HSTS_SECONDS = (
        max(0, env_int("SECURE_HSTS_SECONDS", 0)) if HTTPS_ENABLED else 0
    )
    SECURE_HSTS_INCLUDE_SUBDOMAINS = (
        env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False) if HTTPS_ENABLED else False
    )
    # preload 会把域名提交进浏览器的内置列表，退出流程以月计，慎用
    SECURE_HSTS_PRELOAD = (
        env_bool("SECURE_HSTS_PRELOAD", False) if HTTPS_ENABLED else False
    )
else:
    # 开发环境显式置空，避免误配的环境变量在调试时把请求 301 到 https
    HTTPS_ENABLED = False
    SECURE_PROXY_SSL_HEADER = None
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_HSTS_SECONDS = 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False
    SECURE_REFERRER_POLICY = "same-origin"

# 域名白名单：HTTPS 下提交表单必须带 Origin/Referer，Django 会拿它与
# CSRF_TRUSTED_ORIGINS 比对。走 https 且非默认端口时必须显式列出，
# 否则所有 POST 都会 403 —— 这个错误在生产上非常常见。
CSRF_TRUSTED_ORIGINS = [
    item.strip()
    for item in (env("CSRF_TRUSTED_ORIGINS", "") or "").split(",")
    if item.strip()
]


# ---------------------------------------------------------------------------
# 敏感字段加密
# ---------------------------------------------------------------------------

FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY")
if not FIELD_ENCRYPTION_KEY:
    raise ImproperlyConfigured(
        "缺少 FIELD_ENCRYPTION_KEY，拒绝启动（不允许明文降级）。\n"
        '生成方式：python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())"'
    )


def _validate_fernet_key(key: str) -> None:
    from cryptography.fernet import Fernet

    try:
        Fernet(key.encode())
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都视为配置错误
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY 格式非法，必须是 32 字节 urlsafe-base64 字符串。"
        ) from exc


_validate_fernet_key(FIELD_ENCRYPTION_KEY)


# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------

CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = env(
    "CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1"
)
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True

CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_TASK_ROUTES = {
    # 轮换任务走独立队列，避免与统计任务在 00:00 争抢 worker
    "apps.credentials.tasks.rotate_passwords_by_policy": {"queue": "rotation"},
    "apps.credentials.tasks.rotate_all_passwords": {"queue": "rotation"},
    "apps.metrics.tasks.aggregate_daily_stats": {"queue": "default"},
}

# 直接用 crontab 对象，避免在 celery 侧做二次转换（时序问题）
#
# 轮换任务为什么是「每分钟」而不是「每 12 小时」：
# 轮换时间由页面上的策略决定，可以是"每 5 小时"这种 cron 表达不了的严格间隔
# （cron 的 */7 是"0/7/14/21 点，次日重新从 0 点起"）。所以 beat 只负责
# 每分钟戳一下，真正的"到点没有"由任务读策略判断，与进程内调度器共用同一份逻辑。
CELERY_BEAT_SCHEDULE = {
    "rotate-passwords-check": {
        "task": "apps.credentials.tasks.rotate_passwords_by_policy",
        "schedule": crontab(minute="*"),
    },
    "aggregate-daily-stats": {
        "task": "apps.metrics.tasks.aggregate_daily_stats",
        "schedule": crontab(minute=0, hour=0),
    },
    # 03:30 而非 00:00：避开统计任务与常见的备份/批量轮换窗口，
    # 减少写锁争抢（SQLite 上尤其明显）。
    "cleanup-expired-logs": {
        "task": "apps.metrics.tasks.cleanup_expired_logs",
        "schedule": crontab(minute=30, hour=3),
    },
    # 监控采集：按 MONITOR_INTERVAL_MINUTES 触发。
    #
    # 为什么直接用 crontab(minute=f"*/{N}") 而不写死：周期由环境变量决定，
    # 在 import settings 时就已确定，与 beat 启动时读到的值天然一致。
    # 下限夹到 1 分钟——进程内调度器按分钟去重（minute_key 形如 %Y%m%d%H%M），
    # 写 0 或负数会导致 crontab 表达式非法、任务永不触发。
    "collect-metrics": {
        "task": "apps.monitor.tasks.collect_metrics",
        "schedule": crontab(
            minute=f"*/{max(1, min(60, env_int('MONITOR_INTERVAL_MINUTES', 1)))}"
        ),
    },
    # 04:10 清理监控时序数据：与 03:30 的日志清理错开，
    # 避免两批大批量 DELETE 撞在一起长时间占写锁。
    "purge-metric-samples": {
        "task": "apps.monitor.tasks.purge_metric_samples",
        "schedule": crontab(minute=10, hour=4),
    },
    # 锁采集：默认每 5 分钟。锁查询要 join 多张性能视图，成本高于指标；
    # 且 LockWait 是瞬时状态（每轮清空重写），按分钟累积没有意义。
    "collect-locks": {
        "task": "apps.monitor.tasks.collect_locks",
        "schedule": crontab(
            minute=f"*/{max(1, min(60, env_int('MONITOR_LOCK_INTERVAL_MINUTES', 5)))}"
        ),
    },
    # 告警评估：与采集错开半分钟没有意义（crontab 精度是分钟），
    # 因此同频运行。评估只读本地指标表，不连目标库，开销很小。
    "evaluate-alerts": {
        "task": "apps.monitor.tasks.evaluate_alerts",
        "schedule": crontab(
            minute=f"*/{max(1, min(60, env_int('MONITOR_ALERT_INTERVAL_MINUTES', 1)))}"
        ),
    },
}


# ---------------------------------------------------------------------------
# 调度模式（面向小白运维的关键设计）
# ---------------------------------------------------------------------------
# auto       —— 启动时探测 Redis；连得上走 Celery Beat，连不上自动降级为进程内调度
# celery     —— 强制 Celery（缺少 Redis 时启动报错，便于生产环境尽早暴露问题）
# inprocess  —— 强制进程内线程调度（单机部署，无需任何外部依赖）
# 进程内调度在 Web 进程里起一条后台线程，按 crontab 语义触发同一个任务函数，
# 与 Celery 模式共用任务实现，切换无需改代码。
SCHEDULER_MODE = env("SCHEDULER_MODE", "auto")
SCHEDULER_REDIS_PROBE_TIMEOUT = 0.3  # 秒，auto 模式探测 broker 的超时
# auto 模式下探测「是否有 celery worker 在消费」的超时（秒）。
# 只有 broker 通、且 worker 有回应，才判定为 Celery 模式：
# Redis 起着但 worker 没起是最常见的部署状态，此时若判定为 Celery，
# 所有定时任务都会静默不执行而页面显示一切正常。
SCHEDULER_WORKER_PROBE_TIMEOUT = 0.5


# ---------------------------------------------------------------------------
# 业务参数
# ---------------------------------------------------------------------------

PROBE_TIMEOUT_SECONDS = max(1, min(5, env_int("PROBE_TIMEOUT_SECONDS", 2)))
# 单次探测最多尝试几次（含首次）。1 表示不重试。
PROBE_MAX_ATTEMPTS = max(1, min(10, env_int("PROBE_MAX_ATTEMPTS", 3)))
# 首次重试前的退避基数（毫秒），之后按 2 的幂递增并叠加抖动
PROBE_BACKOFF_BASE_MS = max(0, min(5000, env_int("PROBE_BACKOFF_BASE_MS", 200)))
# 整个探测过程的总时间预算（毫秒）。探测是同步阻塞的，
# 这个值就是 HTTP 请求的最坏耗时上限。
PROBE_TOTAL_BUDGET_MS = max(1000, min(30000, env_int("PROBE_TOTAL_BUDGET_MS", 5000)))
SLOW_REQUEST_MS = env_int("SLOW_REQUEST_MS", 500)

# --- 慢请求日志的异步批量写入 -----------------------------------------------
# 慢请求常发生在数据库本身繁忙时，此时再同步 INSERT 会形成正反馈，
# 因此改为「请求线程投递 + 后台线程合批写库」。
# SLOW_LOG_ASYNC=False 可退回同步写（排障用，或极端低配机器）。
SLOW_LOG_ASYNC = env_bool("SLOW_LOG_ASYNC", True)
# 队列上限。满了就丢弃并计数，绝不阻塞请求线程。
SLOW_LOG_QUEUE_SIZE = max(1, env_int("SLOW_LOG_QUEUE_SIZE", 2000))
# 攒够多少条就合并成一次 bulk_create
SLOW_LOG_BATCH_SIZE = max(1, env_int("SLOW_LOG_BATCH_SIZE", 50))
# 最长等待多久必须刷一次，保证慢请求记录不会长时间停在内存里
SLOW_LOG_FLUSH_SECONDS = max(0.1, float(env_int("SLOW_LOG_FLUSH_SECONDS", 5)))

# --- 运行日志的保留期 -------------------------------------------------------
# 三张「只增不减」的表按保留期裁剪，见 apps/metrics/retention.py。
# 置 0 表示永久保留（合规要求长期留档时使用，但列表页会随数据量变慢）。
# 慢请求记录量最大、排障价值衰减最快，所以保留最短。
SLOW_LOG_RETENTION_DAYS = env_int("SLOW_LOG_RETENTION_DAYS", 90)
# 语句审计涉及「谁改了什么」，保留久一些
AUDIT_LOG_RETENTION_DAYS = env_int("AUDIT_LOG_RETENTION_DAYS", 180)
# 轮换历史用于追溯密码变更，保留一年
ROTATION_LOG_RETENTION_DAYS = env_int("ROTATION_LOG_RETENTION_DAYS", 365)
# 每批删除条数。一次 DELETE 百万行会长时间持有写锁，SQLite 上会卡死站点。
LOG_CLEANUP_BATCH_SIZE = max(1, env_int("LOG_CLEANUP_BATCH_SIZE", 500))
# 单次运行最多删多少批（安全阀）。宁可下次再删，也不让清理本身变成慢任务。
LOG_CLEANUP_MAX_BATCHES = max(1, env_int("LOG_CLEANUP_MAX_BATCHES", 200))

# --- 列表页模糊搜索 ---------------------------------------------------------
# contains（默认）：LIKE '%关键词%'，语义与界面提示一致；在 PostgreSQL 上
#   由 pg_trgm 的 GIN 索引兜底（见 assets/migrations/0005）。
# prefix：LIKE '关键词%'，能走普通 B-tree 索引，但只能匹配开头。
# 实例上万且跑在 SQLite 上时，可改成 prefix 换取确定性性能。
SEARCH_MODE = env("SEARCH_MODE", "contains")

PASSWORD_LENGTH = env_int("PASSWORD_LENGTH", 32)

# 批量轮换的并发度。
# SQLite 是单写者模型，并发只会增加锁竞争（实测 40 次写事务：
# 并发 1/4/8 分别 0.25s / 0.35s / 0.58s），因此默认 1；
# PostgreSQL 等支持行级并发写的后端默认 4。
ROTATION_CONCURRENCY = env_int("ROTATION_CONCURRENCY", 0)  # 0 = 按后端自适应
ROTATION_BATCH_SIZE = max(1, env_int("ROTATION_BATCH_SIZE", 100))
# 改密后是否用新密码重连一次验证。
# 默认开启：驱动报"命令成功"但密码其实没换的情况真实存在（连错实例、
# 账号权限不足却未报错），没有这一步就无法确认轮换真的生效。
ROTATION_VERIFY_AFTER_CHANGE = env_bool("ROTATION_VERIFY_AFTER_CHANGE", True)

# --- 目标库连接（控制台）----------------------------------------------------
# 连目标库的超时。与 PROBE_TIMEOUT_SECONDS 分开口径：
# 探测只做 TCP 握手（秒级即可），而认证还要过一次服务端处理，给得宽一些。
DB_CONNECT_TIMEOUT_SECONDS = max(1, min(30, env_int("DB_CONNECT_TIMEOUT_SECONDS", 5)))
# 单条语句的执行超时（驱动层读超时）
DB_QUERY_TIMEOUT_SECONDS = max(1, min(300, env_int("DB_QUERY_TIMEOUT_SECONDS", 30)))
# 控制台单次查询最多回显多少行。防止 SELECT * 把内存和页面撑爆。
DB_MAX_ROWS = max(1, min(5000, env_int("DB_MAX_ROWS", 500)))

# --- 监控采集 ---------------------------------------------------------------
# 是否启用目标库指标采集。默认 True；关掉后所有 monitor 页面仍可打开，
# 但只展示历史数据并提示"采集已关闭"，不会静默消失。
MONITOR_COLLECT_ENABLED = env_bool("MONITOR_COLLECT_ENABLED", True)
# 采集周期（分钟）。下限 1 分钟是硬约束：进程内调度器按分钟去重
# （minute_key = %Y%m%d%H%M），低于 1 分钟无法表达，写 0 会导致永远不触发。
MONITOR_INTERVAL_MINUTES = max(1, min(60, env_int("MONITOR_INTERVAL_MINUTES", 1)))
# 单个实例单次采集的超时（秒）。这是**每个查询**的上限，不是整轮采集的上限。
MONITOR_TIMEOUT_SECONDS = max(1, min(60, env_int("MONITOR_TIMEOUT_SECONDS", 5)))
# 每轮采集最多并发几个实例。并发过高会把自己的连接数打满，
# 也会在 SQLite 上造成写锁争抢。
MONITOR_CONCURRENCY = max(1, min(32, env_int("MONITOR_CONCURRENCY", 4)))
# 指标与采集记录的保留天数。0 表示永久保留（不建议：时序表增长很快）。
MONITOR_RETENTION_DAYS = max(0, min(3650, env_int("MONITOR_RETENTION_DAYS", 30)))
# 单轮采集最多处理多少个实例（0 = 不限）。超大部署可分批轮转，
# 避免一轮太久把自己的采集周期拖长。
MONITOR_MAX_INSTANCES = max(0, min(10000, env_int("MONITOR_MAX_INSTANCES", 0)))

# --- 告警 -------------------------------------------------------------------
# 是否启用告警评估。关掉后规则与历史事件都保留，只是不再产生新事件。
MONITOR_ALERT_ENABLED = env_bool("MONITOR_ALERT_ENABLED", True)
# 告警评估周期（分钟）。与采集同频即可：更频繁没有新数据可评。
MONITOR_ALERT_INTERVAL_MINUTES = max(
    1, min(60, env_int("MONITOR_ALERT_INTERVAL_MINUTES", 1))
)
# 告警事件"多久没再命中"就自动恢复（分钟）。
# 采集停掉时 firing 事件不会自己恢复，需要这个兜底，否则故障早好了告警还在响。
MONITOR_ALERT_STALE_MINUTES = max(
    1, min(1440, env_int("MONITOR_ALERT_STALE_MINUTES", 15))
)
# 通知 webhook 地址（留空 = 不通知）。企业微信/钉钉机器人等直接填 URL。
# 只发"新触发"与"恢复"两类事件，不重复推送，避免刷屏。
MONITOR_ALERT_WEBHOOK_URL = (env("MONITOR_ALERT_WEBHOOK_URL", "") or "").strip()
# webhook 请求超时（秒）。告警通知不该拖慢评估任务。
MONITOR_ALERT_WEBHOOK_TIMEOUT = max(
    1, min(30, env_int("MONITOR_ALERT_WEBHOOK_TIMEOUT", 5))
)

# --- SQL 洞察 ---------------------------------------------------------------
# 是否随采集顺带抓取语句统计（MySQL performance_schema / PG pg_stat_statements
# / Redis commandstats）。关掉后洞察页仍可打开，只是不再新增指纹。
MONITOR_SQL_INSIGHT_ENABLED = env_bool("MONITOR_SQL_INSIGHT_ENABLED", True)
# SQL 指纹保留天数。0 = 永久保留。指纹比指标增长慢（按模板聚合而非按采样点），
# 因此默认给更长的窗口。
MONITOR_SQL_RETENTION_DAYS = max(
    0, min(3650, env_int("MONITOR_SQL_RETENTION_DAYS", 90))
)

# --- 锁分析 ---------------------------------------------------------------
# 是否采集锁等待与死锁。默认 True；关掉后锁分析页仍可打开，只是不再更新。
MONITOR_LOCK_ENABLED = env_bool("MONITOR_LOCK_ENABLED", True)
# 锁采集周期（分钟）。默认 5：锁查询要 join 多张视图，比指标查询贵得多，
# 没必要与指标同频；而且锁等待是瞬时状态，采太密只是重复看到同一批。
MONITOR_LOCK_INTERVAL_MINUTES = max(
    1, min(60, env_int("MONITOR_LOCK_INTERVAL_MINUTES", 5))
)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": (
                "{asctime} {levelname} {name} "
                "request_id={request_id} {message}"
            ),
            "style": "{",
            "defaults": {"request_id": "-"},
        },
        "simple": {"format": "{asctime} {levelname} {message}", "style": "{"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "stream": sys.stdout,
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "common": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "celery": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
