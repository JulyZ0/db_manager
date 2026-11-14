"""巡检评分：把散落的观测数据折算成一个可比的健康分。

为什么是"分段线性打分"而不是"通过/不通过"
------------------------------------------
运维要的不是"8 项里挂了 2 项"，而是"这台实例现在值多少分、比上周好没好"。
打分才能横向比较（哪台该先修）与纵向比较（改完之后有没有变好）。

每个检查项输出 0–100 的项分，按权重加权平均。分段而不是一刀切：
连接使用率 71% 与 99% 显然不该同罪，线性插值能把"轻微超标"与"即将打满"
区分开。

没有数据的项直接跳过（``score=None``），而不是记 0 分
------------------------------------------------------
新接入的实例可能还没采到复制延迟（无从库）、也可能没开缓存统计。
把"没数据"当 0 分会让一台完全健康的机器显示成 40 分，运维会去查一个
不存在的故障。跳过并计入 ``skipped``，页面单独说明"这项没有数据"。

本模块是纯函数：输入是已经取好的指标字典，不碰数据库、不发网络请求。
这样评分规则可以脱离环境测试，也便于以后换成"按历史均值"之类的口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "CheckResult",
    "InspectReport",
    "inspect_instance",
    "grade_of",
    "STATUS_OK",
    "STATUS_WARN",
    "STATUS_FAIL",
    "STATUS_UNKNOWN",
    "GRADE_INSUFFICIENT",
    "INSUFFICIENT_CAP",
]

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"

# 评级阈值。刻意把"优秀"门槛设到 90：巡检分数虚高会让运维不再看它。
GRADE_THRESHOLDS = ((90, "优秀"), (75, "良好"), (60, "一般"), (0, "较差"))

# 指标全缺失时的评级与分数上限。用独立评级而不是"较差"：
# "较差"意味着查出了问题，"数据不足"意味着还不能下结论，两者处置方式不同。
GRADE_INSUFFICIENT = "数据不足"
INSUFFICIENT_CAP = 59.0

# 指标类检查项：这些项决定"健康状况可判断性"。基础项（采集/凭据）与
# 环境项（锁/告警）即使正常，也不能代替它们说明机器健康。
_INDICATOR_KEYS = frozenset({"conn", "cache", "slow", "repl"})


@dataclass
class CheckResult:
    key: str
    label: str
    category: str
    weight: float
    score: float | None
    status: str
    detail: str

    @property
    def skipped(self) -> bool:
        return self.score is None


@dataclass
class InspectReport:
    instance_id: int
    instance_name: str
    engine: str
    score: float
    grade: str
    results: list[CheckResult] = field(default_factory=list)
    # 指标数据不足时为 True：此时分数不代表健康度，只表示"无从判断"
    insufficient: bool = False

    @property
    def counts(self) -> dict[str, int]:
        return {
            STATUS_OK: sum(1 for r in self.results if r.status == STATUS_OK),
            STATUS_WARN: sum(1 for r in self.results if r.status == STATUS_WARN),
            STATUS_FAIL: sum(1 for r in self.results if r.status == STATUS_FAIL),
            STATUS_UNKNOWN: sum(1 for r in self.results if r.status == STATUS_UNKNOWN),
        }


def grade_of(score: float) -> str:
    for threshold, label in GRADE_THRESHOLDS:
        if score >= threshold:
            return label
    return GRADE_THRESHOLDS[-1][1]


def _linear(value: float, good: float, bad: float) -> float:
    """把 value 从 [good, bad] 映射到 [100, 0]，超界截断。

    同时支持 good > bad（越大越好，如命中率）与 good < bad（越小越好，
    如延迟）两种方向，避免为每种方向各写一份。
    """
    if good == bad:
        return 100.0 if value == good else 0.0
    ratio = (value - good) / (bad - good)
    ratio = max(0.0, min(1.0, ratio))
    return round(100.0 * (1.0 - ratio), 1)


def _status_for(score: float) -> str:
    if score >= 85:
        return STATUS_OK
    if score >= 60:
        return STATUS_WARN
    return STATUS_FAIL


def _check(
    key: str,
    label: str,
    category: str,
    weight: float,
    score: float | None,
    detail: str,
) -> CheckResult:
    status = STATUS_UNKNOWN if score is None else _status_for(score)
    return CheckResult(
        key=key,
        label=label,
        category=category,
        weight=weight,
        score=score,
        status=status,
        detail=detail,
    )


# 各引擎的指标键映射：换引擎时只改这张表，不动评分逻辑
_ENGINE_KEYS = {
    "mysql": {
        "conn_usage": "mysql.connection_usage_percent",
        "cache_hit": "mysql.buffer_pool_hit_percent",
        "slow_rate": "mysql.slow_queries_per_sec",
        "repl_lag": "mysql.replication_delay_seconds",
        "active": "mysql.threads_running",
        "size": "mysql.total_size_bytes",
    },
    "postgresql": {
        "conn_usage": "pg.connection_usage_percent",
        "cache_hit": "pg.cache_hit_percent",
        "slow_rate": None,  # PG 慢查询靠 pg_stat_statements，不是采样指标
        "repl_lag": "pg.replication_lag_seconds",
        "active": "pg.connections_active",
        "size": "pg.total_size_bytes",
    },
    "redis": {
        "conn_usage": None,  # Redis 无 maxclients 指标，无从算使用率
        "cache_hit": "redis.cache_hit_percent",
        "slow_rate": None,
        "repl_lag": None,
        "active": "redis.connected_clients",
        "size": "redis.memory_used_bytes",
    },
}


def inspect_instance(
    *,
    instance_id: int,
    instance_name: str,
    engine: str,
    values: dict[str, float],
    collect_ok: bool | None,
    collect_message: str = "",
    has_password: bool = True,
    firing_alerts: int = 0,
    critical_alerts: int = 0,
    longest_lock_wait: float = 0.0,
) -> InspectReport:
    """对一台实例做一次巡检评分。

    参数全部是已取好的值：调用方负责查询，本函数只负责判定。
    ``values`` 是 ``{指标键: 值}``，缺失的键表示"没有数据"。
    """
    keys = _ENGINE_KEYS.get(engine, {})
    results: list[CheckResult] = []

    def value_of(name: str) -> float | None:
        metric = keys.get(name)
        if not metric:
            return None
        return values.get(metric)

    # 1. 采集是否正常——这是所有其它项可信的前提，权重最高
    if collect_ok is None:
        results.append(
            _check("collect", "采集状态", "基础", 3.0, None, "尚无采集记录")
        )
    else:
        results.append(
            _check(
                "collect",
                "采集状态",
                "基础",
                3.0,
                100.0 if collect_ok else 0.0,
                "最近一次采集成功"
                if collect_ok
                else f"最近一次采集失败：{collect_message or '原因未知'}",
            )
        )

    # 2. 凭据是否登记——没密码就采不到数据，但也不该直接判死
    results.append(
        _check(
            "credentials",
            "凭据登记",
            "基础",
            1.0,
            100.0 if has_password else 0.0,
            "已登记密码" if has_password else "未登记密码，采集无法进行",
        )
    )

    # 3. 连接使用率：70% 开始扣分，90% 归零
    usage = value_of("conn_usage")
    if usage is None:
        results.append(
            _check("conn", "连接使用率", "容量", 1.5, None, "该引擎无连接上限指标")
        )
    else:
        results.append(
            _check(
                "conn",
                "连接使用率",
                "容量",
                1.5,
                _linear(usage, 70.0, 90.0),
                f"当前 {usage:.1f}%（70% 起扣分，90% 计 0 分）",
            )
        )

    # 4. 缓存命中率：越大越好，99% 满分，90% 归零
    hit = value_of("cache_hit")
    if hit is None:
        results.append(
            _check("cache", "缓存命中率", "性能", 1.5, None, "尚无缓存统计")
        )
    else:
        results.append(
            _check(
                "cache",
                "缓存命中率",
                "性能",
                1.5,
                _linear(hit, 99.0, 90.0),
                f"当前 {hit:.2f}%（99% 满分，90% 计 0 分）",
            )
        )

    # 5. 慢查询速率：0 满分，每秒 5 次归零
    slow = value_of("slow_rate")
    if slow is None:
        results.append(
            _check("slow", "慢查询速率", "性能", 1.0, None, "该引擎未采此指标")
        )
    else:
        results.append(
            _check(
                "slow",
                "慢查询速率",
                "性能",
                1.0,
                _linear(slow, 0.0, 5.0),
                f"当前 {slow:.2f} 次/秒（0 满分，5 次/秒计 0 分）",
            )
        )

    # 6. 复制延迟：0 秒满分，30 秒归零
    lag = value_of("repl_lag")
    if lag is None:
        results.append(
            _check("repl", "复制延迟", "可用性", 2.0, None, "无从库或未采此指标")
        )
    else:
        results.append(
            _check(
                "repl",
                "复制延迟",
                "可用性",
                2.0,
                _linear(lag, 0.0, 30.0),
                f"当前 {lag:.1f} 秒（0 秒满分，30 秒计 0 分）",
            )
        )

    # 7. 锁等待：超过 30 秒开始扣分，300 秒归零
    lock_score = _linear(longest_lock_wait, 0.0, 300.0) if longest_lock_wait else 100.0
    results.append(
        _check(
            "lock",
            "锁等待",
            "可用性",
            1.5,
            lock_score,
            "当前无锁等待"
            if not longest_lock_wait
            else f"最长等待 {longest_lock_wait:.0f} 秒（0 秒满分，300 秒计 0 分）",
        )
    )

    # 8. 告警：有 critical 直接 0 分，仅 warning 扣到 50
    if critical_alerts:
        alert_score = 0.0
        alert_detail = f"有 {critical_alerts} 条严重告警正在触发"
    elif firing_alerts:
        # 70 分落在 warn 区间（>=60）：警告级告警是黄灯，不是事故。
        # 给 50 会掉进 fail 区间，页面上一片红，运维会开始忽略颜色。
        alert_score = 70.0
        alert_detail = f"有 {firing_alerts} 条警告级告警正在触发"
    else:
        alert_score = 100.0
        alert_detail = "没有触发中的告警"
    results.append(
        _check("alert", "告警状态", "可用性", 2.0, alert_score, alert_detail)
    )

    # 加权平均：只统计有分数的项。
    scored = [r for r in results if r.score is not None]
    if scored:
        total_weight = sum(r.weight for r in scored)
        score = sum((r.score or 0) * r.weight for r in scored) / total_weight
        score = round(score, 1)
    else:
        score = 0.0

    # 关键约束：**指标类检查全都没有数据时必须封顶**。
    #
    # 不这么做的话，"没有指标"会被采集/凭据/锁/告警四项（默认全 100 分）
    # 拉成满分 100 并评级"优秀"——这比给低分更危险：它让"监控实际上是
    # 瞎的"看起来像"一切正常"，运维因此不会去修采集。
    indicator_results = [r for r in results if r.key in _INDICATOR_KEYS]
    insufficient = bool(indicator_results) and all(
        r.score is None for r in indicator_results
    )
    if insufficient:
        score = min(score, INSUFFICIENT_CAP)

    return InspectReport(
        instance_id=instance_id,
        instance_name=instance_name,
        engine=engine,
        score=score,
        grade=GRADE_INSUFFICIENT if insufficient else grade_of(score),
        results=results,
        insufficient=insufficient,
    )
