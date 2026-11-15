# 数据库实例管理系统

企业内部数据库实例的资产台账与运维平台：

- **资产台账**：部门 → 集群（含架构与拓扑）→ 实例（含角色与复制上游）
- **连通性**：TCP 端口探测，失败按瞬时/确定性分类，瞬时失败带退避重试
- **密码轮换**：按策略定时执行，**真的连到目标库执行改密命令并回连验证**，再以密文落库
- **统计**：每天 00:00 按「部门 × 集群」快照实例数量
- **监控**：指标采集、SQL 洞察、锁等待与死锁、阈值告警、巡检评分、诊断报告
- **审计**：语句执行审计、密码轮换历史、慢请求记录

**交付形态：双击 `start.bat` 即可运行，全程无需输入命令。**

---

## 一、给运维人员：怎么用

### 1. 启动

1. 确认电脑已安装 **Python 3.14 或更高版本**
   （下载地址 <https://www.python.org/downloads/>，安装时**务必勾选** `Add Python to PATH`）。
2. **双击 `start.bat`**。

首次启动会自动完成下面这些事（约 1～2 分钟），共 7 步：

| 步骤 | 说明 |
|---|---|
| 检查 Python 版本 | 版本不够会在窗口里给出提示并停住 |
| 准备虚拟环境 | 在项目目录下生成 `.venv`，不影响系统 Python |
| 检查依赖 | 缺失才安装，自动使用国内镜像，失败会回退官方源 |
| 准备配置文件 | 自动创建 `.env` 并填好两个密钥，无需手工配置 |
| 初始化数据库 | `migrate`，建表并同步角色组权限；随后幂等执行 `ensure_admin` |
| 收集静态文件 | `collectstatic`（生产模式下 runserver 不再服务 `/static/`） |
| 启动服务 | 自动挑空闲端口、拉起 Celery worker 与 beat（可用时）、打开浏览器 |

第二次及以后启动会跳过已完成的步骤，通常几秒就能用。

启动器支持的参数（一般用不到）：

```bash
python launcher.py                # 正常启动
python launcher.py --port 9000    # 指定端口（被占用仍会自动顺延）
python launcher.py --no-browser   # 不自动打开浏览器
python launcher.py --check        # 只做环境自检，不启动服务（无副作用，不建 .venv）
```

### 2. 常用地址

启动窗口里会打印实际端口（默认 `8000`，被占用时自动顺延，最多试 20 个）：

| 页面 | 地址 |
|---|---|
| 总览 | `http://127.0.0.1:8000/` |
| 操作控制台 | `http://127.0.0.1:8000/console/` |
| 监控大盘 | `http://127.0.0.1:8000/monitor/` |
| 锁分析 | `http://127.0.0.1:8000/monitor/locks/` |
| SQL 洞察 | `http://127.0.0.1:8000/monitor/sql/` |
| 告警事件 | `http://127.0.0.1:8000/monitor/alerts/` |
| 巡检评分 | `http://127.0.0.1:8000/monitor/inspect/` |
| 接口文档 | `http://127.0.0.1:8000/api/v1/docs` |
| Django 后台 | `http://127.0.0.1:8000/admin/` |
| 健康检查 | `http://127.0.0.1:8000/healthz` |

### 2.1 管理员账号

首次启动会自动创建管理员，账号密码固定如下：

| 用户名 | 密码 |
|---|---|
| `admin` | `Admin@123456` |

> **首次登录会被强制改密。** 这个初始密码写在代码和 README 里，等于公开信息，
> 系统把它当「临时口令」处理：登录后除改密页、接口文档与静态资源之外的**页面**
> 都会跳到 `/account/password/`，改完才能继续用。
> （实现见 `common/force_password_change.py` 的 `EXEMPT_PREFIXES`：
> `/static/`、`/media/`、`/healthz`、`/api/`、`/account/password/`、
> `/admin/login/`、`/admin/logout/`、`/admin/password_change/` 放行。）
>
> 判断依据不是布尔开关，而是比对「建号时的密码哈希」
> （`apps/web/models.py` 的 `DefaultPasswordWatch`）。所以不管用哪种方式改密
> ——界面改密页、后台右上角「修改密码」、`manage.py changepassword`——
> 只要密码变了限制就自动解除，不会出现「改了还被反复拦住」。
>
> 接口（`/api/v1/`）不受此限制：调用方是脚本，没有界面可以改密。

也可以不写进代码，用环境变量指定：

```
set ADMIN_USERNAME=ops
set ADMIN_PASSWORD=你的强密码
python manage.py ensure_admin
```

指定了**非默认**密码时不会被要求改密；想恢复默认值用 `--reset-password`
（重置后下次登录会**重新**要求改密）。

### 2.2 谁能做什么（鉴权 + 权限分档）

「能不能看」与「能不能改」是两件事，且写操作内部还按**后果的不可逆程度**分了三级
（`common/permissions.py`，权限点挂在 `Instance` 上，角色即 Django Group）：

| 角色（Django Group） | 拥有的权限点 | 能做 |
|---|---|---|
| 只读 | —（空权限集） | 总览、各列表与详情、统计、慢请求、监控页、巡检评分 |
| 操作员 | `assets.operate_assets` | 增删改部门 / 集群 / 实例、单实例探测、单实例轮换、验证凭据、管理告警规则 |
| 管理员 | 上面 + `assets.admin_console`、`assets.admin_bulk_rotate`、`assets.admin_rebuild_stats`、`assets.admin_delete_cascade` | 在控制台执行任意 SQL、批量轮换与修改轮换策略、重算统计、删集群 / 部门 |

要点：

- **浏览免登录**（内网随手看），**写操作必须登录**，未登录点写按钮会被引导到登录页，
  登录后**自动回到原页面**。
- 已登录但缺权限时给的是「请联系管理员分配角色」的提示并回首页，不是裸 403。
- `superuser`（初始 `admin` 即 superuser）对所有权限点恒为 True，升级不会把管理员锁在门外。
- 超出「只读」的写操作若被拦，页面提示与 API 的 403 错误体里都会说明缺哪个权限点
  （`detail.required_permission`）。
- API 的写接口复用 Django 会话（`django_session_auth`，含 CSRF 校验）。脚本调写接口需先登录取得
  `sessionid` 与 `csrftoken` 两个 Cookie；`/api/v1/monitor/*` 整组路由都要求登录。
- 想调整某人权限：`/admin/` → 用户 / 组，把账号放进「只读」「操作员」「管理员」三个组之一。

### 3. 日常操作

| 我想…… | 怎么做 |
|---|---|
| 登记一个部门 | 左侧「部门」→「新增部门」 |
| 登记一个集群 | 「集群」→「新增集群」，选部门、引擎、环境、**架构** |
| 登记一个实例 | 「实例」→「新增实例」，填主机、端口、账号；按架构选**角色**，从库填复制上游 |
| 编排复制关系 | 集群详情 →「复制关系」；架构确定后可用「按架构自动生成」 |
| 看集群拓扑图 | 集群详情（数据来自 `/clusters/<id>/topology.json`） |
| 看某个实例能不能连上 | 实例详情 →「立即探测」（只验端口） |
| 确认登记的账号密码可用 | 打开实例详情即自动验证一次；或控制台 `verify` |
| 立刻更换某个实例的密码 | 实例详情 →「立即轮换」 |
| 看密码换过几次 | 实例详情下方的「轮换历史」 |
| 改轮换周期 / 范围 / 密码长度 | 左侧「轮换策略」→ 保存（保存与「立即执行」都需要管理员权限） |
| 看昨天各部门有多少实例 | 「数量统计」→ 选日期 → 查询 |
| 找出慢的请求 | 「慢请求」 |
| 直接对某个库执行 SQL | 左侧「控制台」→ `list` → `sw <目标>` → 输入 SQL |
| 看某台实例当前什么状况 | 实例详情 →「监控」→ 实时性能 |
| 看某台实例最近几小时的曲线 | 实例详情 →「监控」→ 性能趋势（1 小时 ~ 30 天） |
| 看某台实例的空间与连接 | 实例监控页 →「空间」「会话」 |
| 看谁在等锁、有没有死锁 | 「锁分析」（全库视角）或实例监控页 |
| 找出最耗时的 SQL | 「SQL 洞察」，可切总耗时 / 单次平均 / 执行次数 |
| 一眼看全站有没有事 | 左侧「监控大盘」：只列有问题的实例 |
| 配一条「连接数超 80% 就通知我」 | 「告警」→「告警规则」→ 新增规则 |
| 今天该先修哪台机器 | 左侧「巡检评分」：按分数升序，最该修的排最前 |
| 要对某个库做整体体检 | 集群详情 →「监控」→ 集群概览 → 诊断报告 |

### 3.1 控制台怎么用

「控制台」是一个黑底等宽的终端页面，适合习惯命令行的运维。打开后先输入 `help`：

| 命令 | 作用 |
|---|---|
| `help` / `?` | 显示帮助 |
| `list` / `ls` `[页码]` | 分页列出实例，带 id、别名、引擎、环境、地址、密码是否已设置 |
| `sw <目标>` | 切换当前实例。目标可写 `id`、`集群别名.实例别名`、`集群别名@主机` |
| `status` | 看当前实例的信息 |
| `verify` | 真正连一次目标库，确认登记的账号密码能用 |
| `clear` / `cls` | 清屏（也可以按 `Ctrl+L`） |
| `exit` / `quit` | 提示关标签页退出 |
| 其它输入 | 当作 SQL 发给当前实例执行 |

几个要点：

- **执行 SQL 需要登录，且需要 `admin_console` 权限**（等于把目标库凭据借出去用）。
  没登录时控制台会明确提示去登录，而不是报一个看不懂的错。
- 查询结果以等宽表格显示，并标出返回行数、是否被截断、耗时。
- 结果默认最多返回 500 行（`DB_MAX_ROWS` 可调），避免一条 `SELECT *` 把内存打满。
- **`DROP` / `TRUNCATE` 会要求二次确认**：第一次输入只给警告，把同一条语句再输一次才真的执行。
  走 API 时对应的是 `confirm=true`，否则返回 409（`code=CONFIRM_REQUIRED`）。
- 一次只允许一条语句（上限 10000 字符），`SELECT 1; DROP TABLE t` 会在建连前被直接拒绝。
- 每一条执行（含被拒绝的）都会写进 `StatementAuditLog`，记录操作人、语句前 500 字符与耗时。

> 反过来，**实例详情页**的「凭据验证」卡片和「立即探测」按钮是给不想碰命令行的人用的：
> 打开详情页即自动验证一次账号密码（只读，不落库），点「立即探测」则只验证端口通不通。

### 3.2 监控与告警怎么用

监控数据来自**目标库自身**，因此实例必须已登记密码、且账号有权读取统计视图：

| 引擎 | 需要的权限 / 开关 |
|---|---|
| MySQL | 可读 `information_schema`、`performance_schema` |
| PostgreSQL | 可读 `pg_stat_activity`、`pg_database_size`；SQL 洞察需 `pg_stat_statements` 扩展 |
| Redis | 账号可执行 `INFO`；SQL 洞察走 `INFO commandstats`；慢日志需配 `slowlog-log-slower-than` |

> 采集器目前只实现了这三种引擎（`apps/monitor/collectors/`）。MongoDB 实例可以登记、
> 可以探活、也可以轮换密码，但**没有指标采集**，监控页会显示无数据。

三件容易踩的事：

1. **采集周期最短 1 分钟**。写 0 会让任务永不触发（进程内调度器按分钟去重，crontab 也非法）。
2. **页面显示「尚无采集记录」不是故障**，多半是刚加实例，等一轮即可。
   若长期如此，看「监控大盘」的「需要关注的实例」，那里直接给出错误码。
3. **告警不会因为「连不上」而响**。连不上目标库时没有指标可评，
   这类问题在「监控大盘」里以采集失败的形式列出，不走阈值规则。

可选的 webhook 通知（企业微信 / 钉钉 / 飞书群机器人）：

```ini
MONITOR_ALERT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
```

只推「新触发」与「恢复」两类事件，不重复推送——否则持续越界的指标会每分钟推一条。

### 4. 关闭

在启动窗口按 `Ctrl+C`，或直接关掉那个黑窗口即可。启动器会把 Celery worker 与 beat
一并终止。数据都在 `db.sqlite3` 里，下次启动还在。

### 5. 出问题了怎么办

| 现象 | 处理 |
|---|---|
| 窗口一闪就没了 | 说明没找到 Python，按上面第 1 步装好并勾选 `Add Python to PATH` |
| 提示依赖安装失败 | 检查网络；公司网络需要代理时先设置 `HTTP_PROXY` 环境变量 |
| 端口被占用 | 启动器会自动换端口（最多试 20 个），窗口里会打印实际地址 |
| 想确认环境是否正常 | 在项目目录运行 `python launcher.py --check` |
| 页面提示「轮换失败」 | 实例被停用（停用实例不参与轮换），或另一处正在轮换同一实例；失败原因看轮换历史里的 `error_message` |
| 定时任务没动静 | 看总览页「运行状态」卡片的调度模式；`SCHEDULER_MODE=celery` 而 worker 未起时页面会红字提示 |

### 6. 备份与恢复

只需要备份两个文件：

- `db.sqlite3` —— 全部业务数据
- `.env` —— 包含 `DJANGO_SECRET_KEY` 与 `FIELD_ENCRYPTION_KEY`

> **重要**：`.env` 里的 `FIELD_ENCRYPTION_KEY` 一旦丢失，已加密的密码将**永久无法解密**。
> 请把它和数据库分开、离线备份。`.env` 不要提交到代码仓库（已在 `.gitignore` 中排除）。

---

## 二、给开发者：项目结构与实现

### 2.1 目录结构

```
db_manager/
├── start.bat                 # Windows 双击入口（纯 ASCII + CRLF，中文提示交给 launcher.py）
├── launcher.py               # 一键启动器（仅标准库；--port / --no-browser / --check）
├── requirements.txt
├── .env.example              # 配置模板；.env 由启动器自动生成
├── pytest.ini
├── config/
│   ├── settings.py           # 配置分层；密钥缺失即拒绝启动
│   ├── celery.py             # Celery 应用（namespace="CELERY" + autodiscover）
│   ├── urls.py               # 页面 / API / admin / healthz 挂载
│   └── wsgi.py / asgi.py
├── common/
│   ├── env.py                # 极简 .env 解析 + secret_key_problem（与启动器共用同一套判定）
│   ├── api.py                # NinjaAPI 实例 + 统一异常处理 + 路由汇总
│   ├── auth.py               # django_session_auth（会话 + CSRF）
│   ├── auth_backends.py      # 登录后端
│   ├── auth_signals.py       # 登录成功/失败记账（喂给限流）
│   ├── permissions.py        # 三级权限点与角色（= Django Group）
│   ├── force_password_change.py  # 默认口令强制改密中间件
│   ├── middleware.py         # request_id + 慢请求计时
│   ├── throttle.py           # 登录失败限流
│   ├── scheduler.py          # 三模式调度：auto / celery / inprocess
│   ├── exceptions.py         # BusinessError / NotFoundError / ConflictError / PermissionDeniedError …
│   ├── pagination.py         # {count, page, page_size, results}
│   ├── search.py             # SEARCH_MODE=contains|prefix
│   ├── sqlite_writes.py      # SQLite 写锁重试
│   ├── timing.py             # Stopwatch（中间件与任务共用）
│   └── health.py             # /healthz
├── apps/
│   ├── runtime/              # 进程内调度器生命周期（须排在业务 app 之前）
│   ├── assets/               # 部门 / 集群 / 实例 / 轮换历史
│   │   ├── models.py         # Department / Cluster / Instance / PasswordRotationLog
│   │   ├── schemas.py        # In / Patch / Out / Paged 四类 Schema
│   │   ├── api.py            # departments / clusters / instances 三个 Router
│   │   ├── validators.py     # 别名规则
│   │   ├── replication.py    # 按架构自动生成复制关系
│   │   ├── topology.py       # build_topology（拓扑图数据）
│   │   ├── resolver.py       # 「集群.实例」「集群@主机」解析（控制台用）
│   │   ├── roles.py          # 角色与架构的对应关系
│   │   ├── admin.py
│   │   └── management/commands/ensure_admin.py
│   ├── probing/              # TCP 探测（services.probe_tcp + 1 个 API）
│   ├── credentials/          # 加密 / 改密 / 轮换策略
│   │   ├── crypto.py         # 加解密 / 密码生成 / 指纹
│   │   ├── changers.py       # mysql / postgresql / redis / mongodb 改密与回连验证
│   │   ├── services.py       # rotate_one / rotate_all
│   │   ├── models.py         # RotationPolicy（全局单例）
│   │   ├── schedule.py       # 到点判定（interval / daily）
│   │   ├── tasks.py          # Celery：按策略轮换、全量轮换
│   │   └── api.py            # 立即轮换 + 轮换历史
│   ├── dbconsole/            # 控制台执行
│   │   ├── services.py       # 连接、语句分类、破坏性判定、执行、审计
│   │   ├── models.py         # StatementAuditLog
│   │   └── api.py            # verify-credentials + execute
│   ├── metrics/              # 统计与观测
│   │   ├── models.py         # DailyInstanceStat / SlowRequestLog
│   │   ├── services.py       # aggregate_for_date
│   │   ├── slowlog.py        # 异步批量落库
│   │   ├── retention.py      # 日志保留期清理
│   │   ├── tasks.py          # Celery 包装
│   │   ├── api.py
│   │   └── management/commands/{aggregate_instance_stats,cleanup_expired_logs}.py
│   ├── monitor/              # 监控：采集 / 告警 / SQL 洞察 / 锁 / 巡检
│   │   ├── models.py         # MetricSample / CollectionRun / AlertRule / AlertEvent /
│   │   │                        SqlDigest / LockWait / DeadlockEvent
│   │   ├── collectors/       # base + mysql / postgresql / redis_collector
│   │   ├── services.py       # 采集编排、速率换算、保留期清理
│   │   ├── tasks.py          # 采集 / 清理 / 告警评估 / 锁采集
│   │   ├── queries.py        # 时序查询与降采样
│   │   ├── sqltext.py        # SQL 归一化 + sha1 指纹
│   │   ├── sql_collector.py  # 三引擎语句统计采集
│   │   ├── lock_collector.py # 锁等待与死锁采集
│   │   ├── alerting.py       # 阈值评估（持续时长 / 冷却 / 自动收敛）
│   │   ├── notify.py         # webhook 通知
│   │   ├── inspect.py        # 巡检评分（纯函数）
│   │   ├── reports.py        # 诊断报告取数
│   │   ├── catalog.py        # 指标目录
│   │   └── api.py            # 时序 / 最新值 / 采集状态 / 引擎目录
│   └── web/                  # 服务端渲染界面
│       ├── models.py         # DefaultPasswordWatch
│       ├── forms.py          # ModelForm 集合（含轮换策略）
│       ├── decorators.py     # login_required_page
│       ├── views.py          # 资产 / 统计 / 控制台 / 改密
│       ├── monitor_views.py  # 集群监控 / 大盘 / 画像 / SQL / 锁 / 巡检 / 报告
│       ├── monitor_instance_views.py  # 实例级五个监控页
│       ├── monitor_alert_views.py     # 事件中心与规则管理
│       └── urls.py
├── templates/                # base + 2 个局部模板 + 32 个页面模板
├── static/css/app.css        # 全站样式（纯本地，无 CDN）
└── tests/                    # pytest，45 个测试文件
```

### 2.2 核心算法实现说明

四个核心算法均已实现，对应测试见 `tests/test_algorithms.py`。

#### ① 加密与密码生成 —— `apps/credentials/crypto.py`

| 函数 | 实现要点 |
|---|---|
| `encrypt_secret` | Fernet 加密，返回 `v1:<token>`；**不加缓存**，保证相同明文得到不同密文 |
| `decrypt_secret` | 校验 `v1:` 版本前缀；`InvalidToken` / 格式错误转 `DecryptionError` |
| `generate_password` | `secrets` 生成，长度默认 32；四类字符各至少一个后整体洗牌 |
| `fingerprint` | `sha256` 前 16 位，供轮换审计比对 |

> 安全说明：`encrypt_secret` 特意不加 `lru_cache`。加了缓存会让相同密码在库里
> 留下相同密文（泄露"哪些实例共用同一密码"），还会把明文长期驻留在进程内存。
> 缓存的只有 `Fernet` 实例本身（`_build_fernet`），它不含任何敏感数据。

#### ② TCP 探测 —— `apps/probing/services.py`

`socket.create_connection` + `perf_counter` 计时，异常映射为
`timeout` / `refused` / `dns_failed` / `network_unreachable` / `unknown`。
**任何分支都不向外抛异常**——探测不到是业务结论，不是服务端错误。
瞬时类失败按 `PROBE_MAX_ATTEMPTS` / `PROBE_BACKOFF_BASE_MS` / `PROBE_TOTAL_BUDGET_MS`
退避重试，确定性失败（如 DNS 解析不到）不重试。
errno 常量用 `hasattr` 过滤，避免在部分平台上 import 即失败。

#### ③ 密码轮换 —— `apps/credentials/{services,changers,schedule}.py`

- **真的会连目标库改密**。`changers.py` 按引擎提供「改密 + 回连验证」一对函数：
  MySQL（`ALTER USER`）、PostgreSQL（`ALTER ROLE`）、Redis（`ACL SETUSER`）、
  MongoDB（`updateUser`）。改完立即用新密码回连一次，只有回连成功才算成功
  （`ROTATION_VERIFY_AFTER_CHANGE=True` 时生效，关掉可省一次连接，但会失去确认能力）。
- `rotate_one`：事务 + 乐观锁（更新条件带旧 `password_version`，因为 SQLite 会
  静默忽略 `select_for_update`）→ 旧密码指纹 → 改密 → 新密码加密落库 →
  `password_version += 1` → 写 `PasswordRotationLog`。失败历史在事务外写，
  避免随回滚一起丢失；`error` 字段记录 `auth_failed` / `change_failed` /
  `verify_failed` 等具体原因。
- `rotate_all`：按 id 游标分批（默认 100），单条失败不中断整批；
  并发度按后端自适应（`ROTATION_CONCURRENCY=0` 表示 SQLite 收敛、PostgreSQL 放开）。
  不用 `.iterator()` 是因为边读边写同一张表在 SQLite 上容易表锁。
- **调度**：`RotationPolicy` 是全局单例（`pk=1`），支持 `interval`（每 N 小时）
  与 `daily`（每日定点，逗号分隔多个时刻）两种口径。beat / 进程内调度**每分钟**
  戳一次，真正的「到点没有」由 `schedule.py` 读策略判断——因为「每 7 小时」
  这种严格间隔 cron 表达不了（`*/7` 每天 0 点会重置）。

#### ④ 每日统计 —— `apps/metrics/services.py`

`aggregate_for_date`：单条 SQL 按 `cluster__department` 聚合，
`update_or_create` 保证幂等，关键是**同时写 `department_name` / `cluster_name`
快照值**，避免日后改名篡改历史。

> 说明：`apps/credentials/tasks.py` 与 `apps/metrics/tasks.py` 是 Celery 包装层
> （日志、耗时、重试），不属于核心算法。整体性故障仍会向上抛，让 `autoretry`
> 按退避策略重试；单实例失败由 `rotate_all` 内部消化并计入 `failed`。

### 2.3 已完成的装配

- **调度三模式**：`SCHEDULER_MODE=auto|celery|inprocess`。`auto` 会在启动时探测
  broker **并向 worker 发 ping**（只有 broker 通、worker 也回应才判定为 Celery，
  否则 Redis 起着而 worker 没起时，定时任务会静默不执行），
  连不上就自动降级为进程内线程调度，**与 Celery 共用同一批任务函数**
- **Celery 任务路由**：轮换走 `rotation` 队列，其余走 `default`；
  `TASK_ACKS_LATE` + `REJECT_ON_WORKER_LOST`，worker 被杀不会丢任务
- **beat 计划**：轮换每分钟、统计 00:00、日志清理 03:30、指标清理 04:10、
  采集/告警/锁按各环境变量周期
- **配置分层**：`DB_ENGINE=sqlite|postgresql` 切换，无需改代码
- **密钥校验**：`DJANGO_SECRET_KEY` / `FIELD_ENCRYPTION_KEY` 缺失、占位符或格式非法
  → 启动即失败，不降级明文
- **统一错误体**：`{"code", "message", "detail"}`，业务异常 throw 即转换；
  校验失败 422、缺权限 403（带 `detail.required_permission`）
- **分页结构**：`{"count", "page", "page_size", "results"}`
- **数据库约束**：集群名部门内唯一、实例端点集群内唯一、别名唯一、端口 1–65535
- **request_id**：所有响应都带 `X-Request-ID`（含被耗时统计排除的路径）
- **界面零外部依赖**：CSS 全本地，目标机器无外网也能正常显示

### 2.4 开发环境手动运行

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
cp .env.example .env            # 填入 DJANGO_SECRET_KEY 与 FIELD_ENCRYPTION_KEY
python manage.py migrate
python manage.py ensure_admin
python manage.py runserver
```

生成两个密钥：

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`DEBUG` 默认是 **False**。此时 `runserver` 不再服务 `/static/`，静态文件由 whitenoise
接管（`collectstatic` 后从 `staticfiles/` 提供）。本地要看调试页才在 `.env` 里写
`DJANGO_DEBUG=True`。

#### Celery 由启动器自动托管

`python launcher.py`（或双击 `start.bat`）在起 Web 之前会**自动拉起
celery worker 与 beat**，无需手工开两个终端：

- `SCHEDULER_MODE=celery`：一定拉起，起不来会打印日志路径。
- `SCHEDULER_MODE=auto`：Redis 可达就拉起；不可达则跳过，由 Web 进程内调度兜底。
- `SCHEDULER_MODE=inprocess`：不拉起。

worker 与 beat 的输出分别写到 `celery-worker.log` / `celery-beat.log`
（已 gitignore）。关闭启动器时它们会被一并终止。

也可以手工管理（例如拆到不同机器上跑）：

```bash
celery -A config worker -l info -Q default,rotation -P threads -c 4
celery -A config beat -l info --schedule celerybeat-schedule
```

> Windows 上必须带 `-P threads`：celery 默认的 prefork 池在 Windows 不可用。
> `beat` 全局只能有**一个**实例，多开会重复投递任务。
> 注意 `apps/runtime/apps.py` 会识别 celery 进程（`is_celery_process`）并跳过
> 进程内调度器，否则 worker 里会再起一个调度线程、同一批任务被执行两遍。

### 2.5 多进程部署（重要约束）

`launcher.py` 用的是 **waitress 单进程多线程**，下面这些机制都靠这个前提成立。
如果你为了扛并发把它换成 gunicorn 多 worker、或起多个 waitress 进程 +
反向负载均衡，**下面三处会静默失效**——不会报错，只会表现成"偶发不对劲"：

| 组件 | 依赖的进程内状态 | 多进程时的表现 | 处理方式 |
|---|---|---|---|
| 登录失败限流（`common/throttle.py`） | `LocMemCache` | 每个进程各记一份计数，阈值等效放大 N 倍，爆破难度骤降 | 换成共享缓存（见下） |
| 进程内调度器（`SCHEDULER_MODE=inprocess`） | `threading.Thread` + 模块级单例 | **每个进程都会起一个调度线程**，轮换与每日统计被执行 N 次 | 改用 Celery（见下） |
| 慢请求日志队列（`apps/metrics/slowlog.py`） | `queue.Queue` + daemon 线程 | 每个进程各自缓冲与刷盘，正常；但进程被强杀时未刷盘的记录会丢 | 可接受；要求不丢则设 `SLOW_LOG_ASYNC=False` |

三者的共同点：**单进程是它们成立的前提，代码里没有跨进程锁**。

#### 改多进程时的正确做法

1. **限流计数换共享缓存。** `.env` 里填一行即可，不需要改代码：

   ```
   CACHE_URL=redis://127.0.0.1:6379/1
   ```

   填了 `redis://` 就会自动切到 `RedisCache`；留空则保持进程内缓存。
   注意这个缓存也会被其它缓存用途共用。

2. **定时任务交给 Celery。** 设 `SCHEDULER_MODE=celery`（缺 Redis 会直接报错，
   而不是悄悄降级成进程内模式）。`launcher.py` 会自动托管 worker 与 beat；
   若要把它们拆到独立机器，手工启动即可（命令见 §2.4）。

   > 配了 `celery` 但 worker 没起来时，总览页的「运行状态」卡片会显示红色的
   > 「Celery 未就绪」并明确说明定时任务不会执行——这类沉默停摆必须让人一眼看到。

3. **确认调度器没被意外拉起。** 多 worker 部署下可以用
   `DISABLE_SCHEDULER=1` 在 Web 进程里彻底关掉进程内调度器，把触发权完全交给
   `beat`。这是最不容易出错的做法：Web 进程只处理请求。

> 判断依据：**总览页**「运行状态」卡片里的「定时任务调度」一行会显示当前模式
> （`进程内调度` / `Celery`）。多 worker 部署时如果这里显示"进程内调度"，
> 就说明定时任务正在被每个进程重复执行。

#### 另一个前提：会话表

会话用的是 Django 默认的数据库后端（`django.contrib.sessions`），多进程共享同一个
数据库即可正常工作，不需要额外配置。但如果把 `DB_ENGINE` 保留为 SQLite 又跑多进程，
写锁会明显拖慢轮换与统计——**多进程部署请同时切到 PostgreSQL**。

### 2.6 验证

```bash
python manage.py check            # 装配检查
python -m pytest tests/ -q        # 全部测试
python launcher.py --check        # 启动器环境自检
```
