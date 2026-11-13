"""Django 配置。

设计要点：
- 全部配置来自环境变量，仓库不保存任何密钥。
- 默认 SQLite，开箱即用；设置 DB_ENGINE=postgresql 即切换到 PostgreSQL。
- FIELD_ENCRYPTION_KEY 缺失或格式非法时直接抛 ImproperlyConfigured，
  不允许以"明文降级"的方式启动。
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

from common.env import env, env_bool, env_int, env_list, load_env_file

BASE_DIR = Path(__file__).resolve().parent.parent

load_env_file(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "缺少 DJANGO_SECRET_KEY。请复制 .env.example 为 .env 并填写密钥。"
    )

# 默认 False：未显式开启即按生产口径运行（不回显 traceback / SQL / 配置）。
# 本地开发需要调试页时才在 .env 里写 DJANGO_DEBUG=True。
DEBUG = env_bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["127.0.0.1", "localhost"])

# 测试进程需要 testserver；开发态放宽到任意本地绑定地址。
# 生产（DEBUG=False 且非测试）只认显式配置的白名单，不做任何自动放宽。
RUNNING_TESTS = "pytest" in sys.modules
if DEBUG or RUNNING_TESTS:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, "0.0.0.0", "testserver"]


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
