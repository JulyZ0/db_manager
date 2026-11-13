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

> **上线前必须改密码。** 这是方便演示的固定初始密码，任何知道它的人都能进后台。
> 改密码有两种方式：
>
> - 在后台右上角用户菜单里点「修改密码」；
> - 或运行 `python manage.py ensure_admin --reset-password --password 你的新密码`。
>
> 改过之后，启动器**不会**把它冲回默认密码——重跑只补齐权限，不动密码。
> 确实想恢复默认密码时，加 `--reset-password` 显式重置。

也可以用环境变量指定自己的账号，不写进代码：

```
set ADMIN_USERNAME=ops
set ADMIN_PASSWORD=你的强密码
python manage.py ensure_admin
```

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
│   └── web/                  # 服务端渲染界面
│       ├── forms.py          ✅ 3 个 ModelForm
│       ├── views.py          ✅ 13 个视图
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

需要 Celery 模式时（可选，需要 Redis）：

```bash
celery -A config worker -l info -Q default,rotation
celery -A config beat -l info
```

无 Redis 时保持 `SCHEDULER_MODE=auto` 即可，定时任务照常执行。

### 2.5 验证

```bash
python manage.py check         # 装配检查
python -m pytest tests/ -q     # 全部测试（277 项）
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
