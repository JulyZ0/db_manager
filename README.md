# 数据库实例管理系统

企业内部数据库实例的资产台账平台：登记部门 / 集群 / 实例，主动探测端口连通性，
每 12 小时自动轮换密码并以密文落库，每天 00:00 按「部门 × 集群」统计实例数量，
并用中间件记录每个请求的耗时。

**交付形态：双击 `start.bat` 即可运行，全程无需输入命令。**

---

## 一、给运维人员：怎么用

### 1. 启动

1. 确认电脑已安装 **Python 3.14 或更高版本**
   （下载地址 <https://www.python.org/downloads/>，安装时**务必勾选** `Add Python to PATH`）。
2. **双击 `start.bat`**。

首次启动会自动完成下面这些事，你只需要等（约 1～2 分钟）：

| 步骤 | 说明 |
|---|---|
| 检查 Python | 版本不够会在窗口里给出提示并停住 |
| 创建虚拟环境 | 在项目目录下生成 `.venv`，不影响系统 Python |
| 安装依赖 | 自动使用国内镜像，失败会回退官方源 |
| 生成配置 | 自动创建 `.env` 并填好加密密钥，无需手工配置 |
| 建表 | 初始化 SQLite 数据库 `db.sqlite3` |
| 启动服务 | 自动挑一个空闲端口，并帮你打开浏览器 |

第二次及以后启动会跳过已完成的步骤，通常几秒就能用。

### 2. 常用地址

启动窗口里会打印实际端口（默认 `8000`，被占用时自动顺延）：

| 页面 | 地址 |
|---|---|
| 管理界面 | `http://127.0.0.1:8000/` |
| 操作控制台 | `http://127.0.0.1:8000/console/` |
| 接口文档 | `http://127.0.0.1:8000/api/v1/docs` |
| Django 后台 | `http://127.0.0.1:8000/admin/` |
| 健康检查 | `http://127.0.0.1:8000/healthz` |

### 2.1 管理员账号

首次启动会自动创建后台管理员，账号密码固定如下：

| 用户名 | 密码 |
|---|---|
| `admin` | `Admin@123456` |

> **首次登录会被强制改密。** 这个初始密码写在代码和 README 里，等于公开信息，
> 所以系统把它当成「临时口令」处理：用 `admin` / `Admin@123456` 登录后，
> 除改密页与接口文档之外的**所有页面**都会自动跳到 `/account/password/`，
> 改完才能继续用。
>
> 这个判断不靠一个开关标记，而是比对「建号时的密码哈希」。因此不管你用哪种方式
> 改的密码——界面的改密页、后台右上角的「修改密码」、还是
> `manage.py changepassword`——只要密码变了，限制就自动解除，不会出现
> 「改了密码还被反复拦住」的情况。
>
> 接口（`/api/v1/`）不受此限制：调用方是脚本，没有界面可以改密，
> 拦住只会让既有自动化突然失败。

也可以用环境变量指定自己的账号，不写进代码：

```
set ADMIN_USERNAME=ops
set ADMIN_PASSWORD=你的强密码
python manage.py ensure_admin
```

用这种方式指定了**非默认**密码时，不会被要求强制改密——那已经不是公开信息了。

确实想恢复默认密码（例如忘记了自设的密码），用显式重置：

```bash
python manage.py ensure_admin --reset-password
```

重置回默认值后，下次登录会**重新**要求改密。

### 2.2 谁能做什么（鉴权口径）

本系统按「看得见」和「改得动」两档区分：

| 操作 | 是否需要登录 |
|---|---|
| 浏览总览、部门 / 集群 / 实例列表与详情、数量统计、慢请求 | **不需要**，内网随手可看 |
| 打开操作控制台、查看实例列表、验证凭据 | **不需要** |
| 在控制台里执行 SQL、通过 `/api/v1/instances/{id}/execute` 执行 SQL | **需要** |
| 新建 / 修改 / 删除部门、集群、实例 | **需要** |
| 立即探测、立即轮换密码 | **需要** |
| 重算某天统计 | **需要** |
| 通过 `/api/v1` 的 POST / PATCH / DELETE 改数据 | **需要**（复用 Django 会话，浏览器已登录即可） |

未登录点写操作时不会看到报错页，而是被引导到登录页，登录成功后**自动回到你刚才所在的页面**，再点一次按钮即可。

> API 复用 Django 会话登录，因此跨站伪造请求会被 CSRF 校验拦住。
> 用脚本调写接口时，需先登录取得 `sessionid` 与 `csrftoken` 两个 Cookie。

`/admin/` 只在需要直接看数据库原始记录时用。

### 3. 日常操作

| 我想…… | 怎么做 |
|---|---|
| 登记一个部门 | 左侧「部门」→ 右上角「新增部门」 |
| 登记一个集群 | 「集群」→「新增集群」，选择所属部门、数据库类型、环境 |
| 登记一个数据库实例 | 「实例」→「新增实例」，填主机、端口、登录账号 |
| 看某个实例能不能连上 | 进实例详情页 → 点「立即探测」 |
| 立刻更换某个实例的密码 | 进实例详情页 → 点「立即轮换」 |
| 看密码换过几次 | 实例详情页下方的「轮换历史」 |
| 看昨天各部门有多少实例 | 「数量统计」→ 选日期 → 查询 |
| 找出慢的请求 | 「慢请求」 |
| 直接对某个库执行 SQL | 左侧「控制台」→ `list` 看实例 → `use <id>` → 输入 SQL 回车 |
| 看某台实例当前什么状况 | 「实例」→ 详情页 → 「监控」→ 实时性能 |
| 看某台实例最近几小时的曲线 | 实例详情 → 「监控」→ 性能趋势（可切 1 小时 ~ 30 天） |
| 看谁在等锁、有没有死锁 | 实例监控页 → 「锁分析」 |
| 找出最耗时的 SQL | 实例监控页 → 「SQL 洞察」，可切总耗时 / 单次平均 / 执行次数 |
| 一眼看全站有没有事 | 左侧「监控大盘」：只列有问题的实例 |
| 配一条「连接数超 80% 就通知我」 | 「告警」→「告警规则」→ 新增规则 |
| 今天该先修哪台机器 | 左侧「巡检评分」：按分数升序，最该修的排最前 |
| 要对某个库做整体体检 | 集群详情 → 「监控」→ 集群概览 → 诊断报告 |

### 3.2 监控与告警怎么用

监控数据来自**目标库自身**，因此实例必须已登记密码、且账号有权读取统计视图：

| 引擎 | 需要的权限 / 开关 |
|---|---|
| MySQL | 可读 `information_schema`、`performance_schema` |
| PostgreSQL | 可读 `pg_stat_activity`、`pg_database_size`；SQL 洞察需 `pg_stat_statements` 扩展 |
| Redis | 账号可执行 `INFO`；SQL 洞察走 `INFO commandstats` |

三件容易踩的事：

1. **采集周期最短 1 分钟**。写 0 会让任务永不触发（调度器按分钟去重）。
2. **页面显示「尚无采集记录」不是故障**，多半是刚加实例，等一轮即可。
   若长期如此，看「监控大盘」的「需要关注的实例」，那里直接给出错误码。
3. **告警不会因为「连不上」而响**。连不上目标库时没有指标可评，
   这类问题在「监控大盘」里以采集失败的形式列出，不走阈值规则。

可选的 webhook 通知（企业微信 / 钉钉 / 飞书群机器人）：

```ini
MONITOR_ALERT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
```

只推「新触发」与「恢复」两类事件，不重复推送——否则持续越界的指标会每分钟推一条。

### 3.1 控制台怎么用

「控制台」是一个黑底等宽的终端页面，适合习惯命令行的运维。打开后先输入 `help`：

| 命令 | 作用 |
|---|---|
| `list` | 列出全部实例，带 id、地址、账号、密码是否已设置 |
| `use 3` | 选中 id=3 的实例，之后所有 SQL 都发到它上面 |
| `status` | 看当前选中实例的信息 |
| `verify` | 真正连一次目标库，确认登记的账号密码能用 |
| `clear` | 清屏（也可以按 `Ctrl+L`） |
| 其它输入 | 当作 SQL 发给当前实例执行 |

几个要点：

- **执行 SQL 需要登录**。没登录时控制台会明确提示去登录，而不是报一个看不懂的错。
- 查询结果以等宽表格显示，并标出返回行数、是否被截断、耗时。
- 结果默认最多返回 500 行（`DB_MAX_ROWS` 可调），避免一条 `SELECT *` 把内存打满。
- **`DROP` / `TRUNCATE` 会要求二次确认**：第一次输入只给警告，把同一条语句再输一次才会真的执行。
- 一次只允许一条语句，`SELECT 1; DROP TABLE t` 会被直接拒绝。
- 每一条执行（含被拒绝的）都会写进审计表，记录操作人、语句前 500 字符与耗时。

> 反过来，**实例详情页**的「凭据验证」卡片和「立即探测」按钮是给不想碰命令行的人用的：
> 打开详情页即自动验证一次账号密码，点「立即探测」则只验证端口通不通。

### 4. 关闭

在启动窗口按 `Ctrl+C`，或直接关掉那个黑窗口即可。数据都在 `db.sqlite3` 里，下次启动还在。

### 5. 出问题了怎么办

| 现象 | 处理 |
|---|---|
| 窗口一闪就没了 | 说明没找到 Python，按上面第 1 步装好并勾选 `Add Python to PATH` |
| 提示依赖安装失败 | 检查网络；公司网络需要代理时先设置 `HTTP_PROXY` 环境变量 |
| 端口被占用 | 启动器会自动换端口，窗口里会打印实际地址 |
| 想确认环境是否正常 | 在项目目录运行 `python launcher.py --check` |
| 页面提示"轮换失败" | 实例被停用（停用实例不参与轮换），或另一处正在轮换同一实例 |

### 6. 备份与恢复

只需要备份两个文件：

- `db.sqlite3` —— 全部业务数据
- `.env` —— 包含加密密钥

> **重要**：`.env` 里的 `FIELD_ENCRYPTION_KEY` 一旦丢失，已加密的密码将**永久无法解密**。
> 请把它和数据库分开、离线备份。`.env` 不要提交到代码仓库（已在 `.gitignore` 中排除）。

---

## 二、给开发者：项目结构与实现状态

### 2.1 目录结构

```
db_manager/
├── start.bat                 # Windows 双击入口
├── launcher.py               # 一键启动器（仅标准库，可在无依赖环境运行）
├── requirements.txt
├── .env.example              # 配置模板；.env 由启动器自动生成
├── config/
│   ├── settings.py           # 配置分层，加密密钥缺失即拒绝启动
│   ├── celery.py             # Celery 应用
│   ├── urls.py               # 页面 / API / admin 挂载
│   └── wsgi.py / asgi.py
├── common/
│   ├── env.py                # 极简 .env 解析（不引入额外依赖）
│   ├── api.py                # NinjaAPI 实例 + 统一异常处理 + 路由汇总
│   ├── exceptions.py         # BusinessError / NotFoundError / ConflictError
│   ├── pagination.py         # {count, page, page_size, results}
│   ├── scheduler.py          # 三模式调度：auto / celery / inprocess
│   ├── middleware.py         # 请求耗时中间件 ✅
│   └── health.py             # /healthz
├── apps/
│   ├── runtime/              # 调度器生命周期（置于业务 app 之前）
│   ├── assets/               # 部门 / 集群 / 实例 / 轮换历史
│   │   ├── models.py         ✅
│   │   ├── schemas.py        ✅
│   │   ├── api.py            ✅ 15 个 CRUD 端点
│   │   └── admin.py          ✅
│   ├── probing/              # TCP 探测
│   │   ├── api.py            ✅
│   │   └── services.py       ✅ probe_tcp
│   ├── credentials/          # 加密与轮换
│   │   ├── crypto.py         ✅ 加解密 / 密码生成 / 指纹
│   │   ├── services.py       ✅ rotate_one / rotate_all
│   │   ├── tasks.py          ✅ Celery 包装层
│   │   └── api.py            ✅
│   ├── metrics/              # 统计与观测
│   │   ├── models.py         ✅
│   │   ├── services.py       ✅ aggregate_for_date
│   │   ├── tasks.py          ✅ Celery 包装层
│   │   ├── api.py            ✅
│   │   └── management/commands/aggregate_instance_stats.py  ✅
│   ├── monitor/              # 监控：采集 / 告警 / SQL 洞察 / 锁 / 巡检
│   │   ├── models.py         ✅ MetricSample / CollectionRun / AlertRule /
│   │   │                        AlertEvent / SqlDigest / LockWait / DeadlockEvent
│   │   ├── collectors/       ✅ base + mysql / postgresql / redis_collector
│   │   ├── services.py       ✅ 采集编排、速率换算、保留期清理
│   │   ├── tasks.py          ✅ 采集 / 清理 / 告警评估 / 锁采集
│   │   ├── queries.py        ✅ 时序查询与降采样
│   │   ├── sqltext.py        ✅ SQL 归一化 + sha1 指纹
│   │   ├── sql_collector.py  ✅ 三引擎语句统计采集
│   │   ├── lock_collector.py ✅ 锁等待与死锁采集
│   │   ├── alerting.py       ✅ 阈值评估（持续时长 / 冷却 / 自动收敛）
│   │   ├── notify.py         ✅ webhook 通知
│   │   ├── inspect.py        ✅ 巡检评分（纯函数）
│   │   ├── reports.py        ✅ 诊断报告取数
│   │   ├── catalog.py        ✅ 指标目录
│   │   └── api.py            ✅ 时序 / 最新值 / 采集状态
│   └── web/                  # 服务端渲染界面
│       ├── forms.py          ✅ 4 个 ModelForm
│       ├── views.py          ✅ 资产管理视图
│       ├── monitor_views.py  ✅ 集群监控 / 大盘 / 画像 / SQL / 锁 / 巡检
│       ├── monitor_instance_views.py  ✅ 实例级五个监控页
│       ├── monitor_alert_views.py     ✅ 事件中心与规则管理
│       └── urls.py           ✅
├── common/timing.py          ✅ 计时组件（中间件与任务共用）
├── templates/                # base + 各类页面模板
├── static/css/app.css        # 全站样式（纯本地，无 CDN）
└── tests/                    ✅ 全部通过
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
errno 常量用 `hasattr` 过滤，避免在部分平台上 import 即失败。

#### ③ 密码轮换 —— `apps/credentials/services.py`

- `rotate_one`：事务 + `select_for_update` → 旧密码指纹 → 新密码加密落库 →
  `password_version += 1` → 写 `PasswordRotationLog`。
  并发用**乐观锁**兜底（更新条件带旧 `password_version`），因为 SQLite 会
  静默忽略 `select_for_update`。失败历史在事务外写，避免随回滚一起丢失。
- `rotate_all`：按 id 游标分批（默认 100），单条失败不中断整批。
  不用 `.iterator()` 是因为边读边写同一张表在 SQLite 上容易表锁。

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
  否则 Redis 起着而 worker 没起时，定时任务会静默不执行）
  broker（0.3 秒超时），连不上就自动降级为进程内线程调度，**与 Celery 共用同一批任务函数**
- **配置分层**：`DB_ENGINE=sqlite|postgresql` 切换，无需改代码
- **密钥校验**：`FIELD_ENCRYPTION_KEY` 缺失或格式非法 → 启动即失败，不降级明文
- **统一错误体**：`{"code", "message", "detail"}`，业务异常 throw 即转换
- **分页结构**：`{"count", "page", "page_size", "results"}`
- **数据库约束**：集群名部门内唯一、实例端点集群内唯一、端口 1–65535（Schema 层校验）
- **request_id**：所有响应都带 `X-Request-ID`（含被耗时统计排除的路径）
- **界面零外部依赖**：CSS 全本地，目标机器无外网也能正常显示

### 2.4 开发环境手动运行

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
cp .env.example .env            # 填入 FIELD_ENCRYPTION_KEY
python manage.py migrate
python manage.py runserver
```

生成加密密钥：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

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
   若要把它们拆到独立机器，手工启动即可：

   ```bash
   celery -A config worker -l info -Q default,rotation -P threads -c 4
   celery -A config beat -l info --schedule celerybeat-schedule
   ```

   `beat` 全局只能有**一个**实例，多开会重复投递任务。

   > 配了 `celery` 但 worker 没起来时，仪表盘会显示红色的「Celery 未就绪」
   > 并明确说明定时任务不会执行——这类沉默停摆必须让人一眼看到。

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
python manage.py check         # 装配检查
python -m pytest tests/ -q     # 全部测试
python launcher.py --check     # 启动器环境自检
```

---

## 三、接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/v1/departments` | 部门列表 / 新建 |
| GET/PATCH/DELETE | `/api/v1/departments/{id}` | 部门详情 / 更新 / 删除 |
| GET/POST | `/api/v1/clusters` | 集群列表 / 新建 |
| GET/PATCH/DELETE | `/api/v1/clusters/{id}` | 集群详情 / 更新 / 删除 |
| GET/POST | `/api/v1/instances` | 实例列表 / 新建 |
| GET/PATCH/DELETE | `/api/v1/instances/{id}` | 实例详情 / 更新 / 删除 |
| POST | `/api/v1/instances/{id}/probe` | TCP 端口探测 |
| POST | `/api/v1/instances/{id}/rotate-password` | 手动轮换密码 |
| GET | `/api/v1/instances/{id}/rotations` | 轮换历史 |
| GET | `/api/v1/stats/daily` | 按日期查询统计快照 |
| POST | `/api/v1/stats/daily/rebuild` | 重算某日统计（幂等） |
| GET | `/api/v1/monitor/series` | 指标时序（自动按跨度降采样） |
| GET | `/api/v1/monitor/latest` | 指标最新值 |
| GET | `/api/v1/monitor/collect-status` | 最近一次采集状态 |
| GET | `/api/v1/monitor/engines` | 指标目录 |

实例相关的**任何响应都不包含密码或密文**，只暴露 `password_set` 与 `password_version`。

完整契约见 PRD 第 7 节，或启动后访问 `/api/v1/docs` 在线调试。

---

## 四、设计取舍

**为什么用 django-ninja 而不是 DRF**：写法贴近 FastAPI（装饰器 + Pydantic Schema），
省掉 Serializer 样板，内置 OpenAPI。代价是过滤 / 权限要自己写——本项目规模下可接受。
若评审方要求 DRF，模型层可完全复用，只需重写路由与 Schema 层。

**为什么「轮换」不连目标库改密**：真实改密需要多数据库方言适配与失败回滚。
本期只做应用侧：生成强随机密码 → 加密落库 → 版本号递增 → 写审计历史。
生产演进方向见 PRD §9.4.3（`PasswordChanger` 策略类 + 双密码过渡窗口）。

**为什么调度要自动降级**：目标机器是运维的办公电脑，不一定装 Redis。
`auto` 模式下有 Redis 就走 Celery，没有就退化为进程内线程调度，两者调用**同一批任务函数**，
行为一致，运维无需理解差异。

**为什么统计表冗余名称字段**：集群改名或删除后，历史快照若走外键会失真。
存快照值 + 外键 `SET_NULL` 是趋势类数据的标准做法。

**为什么界面用服务端渲染而非 SPA**：交付对象是不熟悉命令行的运维人员，
SSR 没有构建步骤、没有前端依赖、断网也能用，双击即开。

**为什么启动器只用标准库**：它要能在**还没装任何依赖**的机器上跑起来，
所以 .env 生成、密钥生成（`os.urandom` + base64）都自己实现，不 import cryptography。

**为什么 `start.bat` 必须保持纯 ASCII + CRLF**：cmd.exe 在 `chcp 65001` 生效**之前**
就已经按系统代码页（GBK）解析整个批处理文件。文件里只要有中文，就会被读成乱码，
而且乱码行会被当作命令执行，窗口里会刷出一堆
`'舵壘涓嶅埌椤圭洰鏂囦欢' 不是内部或外部命令`。
因此：批处理只做流程控制与英文提示，所有中文提示交给 `launcher.py` 输出
（它会把 stdout 强制设为 UTF-8）。另外批处理必须是 CRLF 行尾，
LF-only 会让 `goto :label` 定位失败。
修改 `start.bat` 后请确认这两点。
