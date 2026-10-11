# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""主管看板统计层：每日预聚合 + 识别引擎 + 数据权限（FR-TR-006，SR-TR-006 §6，迭代 1.3）。

模块职责（读写分离，看板查询只走预聚合层）：
- 事件流数据源协议 :class:`BoardDataSource`——学习日志（SR-TR-005）/演练（SR-TR-003）/
  考试（SR-TR-007）/雷达快照（SR-TR-004）四族事件的注入接缝，demo 以 Fake 注入，
  生产替换 PG 查询形态，协议不变（不 import 各兄弟模块具体实现，避免并行迭代耦合）；
- :class:`TeamStatsAggregator` + :class:`DailyStatsStore`——trainee_daily 预聚合物化：
  按日聚合事件流（幂等：同日重算覆盖写，对齐 ON CONFLICT DO UPDATE 语义），
  空数据日为全员补零（主管打开看板永远有数）；
- 识别引擎 :class:`RuleEngine` / :class:`FlagEvaluator` / :class:`FlagStore`——
  高潜力（HP）/需干预（NI）规则注册制 + 参数外置热更新（NFR-MAI-02 改参不发版），
  命中携带规则编码 + 参数快照 + 数据证据（「识别规则可解释」验收口径）；
- 数据权限 :class:`TeamScope` / :func:`resolve_scope` / :func:`ensure_in_scope`——
  主管仅本团队学员集合（40301 + 审计 rbac_deny 留痕）；生产另以 PG RLS 兜底
  （SR-TR-006 §4 双防线），本模块承载第一防线（应用层注入）。

依据文档：docs/07-实现方案/TR/SR-TR-006-主管看板.md §4/§6/§7/§9；
         docs/03-技术文档/02-开发规范.md §3（中文注释 / 全量类型注解强制）。
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol


# --------------------------------------------------------------------------- 常量与事件模型

#: 雷达四维口径（与 trainee.radar 键一致；SR-TR-004 §4）
RADAR_DIMS: tuple[str, ...] = ("product", "communication", "negotiation", "platform")

#: 标记类型（SR-TR-006 §4 trainee_flag.flag_type CHECK 约束口径）
FLAG_HIGH_POTENTIAL: str = "high_potential"
FLAG_NEED_INTERVENTION: str = "need_intervention"


@dataclass(frozen=True)
class LearningEvent:
    """学习日志事件（源 train_learning_log 形态：当日时长/课时/累计路径完成率）。"""

    stat_date: date
    minutes: int
    lessons_done: int
    path_pct: float


@dataclass(frozen=True)
class DrillEvent:
    """演练事件（源 exercise 形态：当日一次演练及其得分）。"""

    stat_date: date
    score: float


@dataclass(frozen=True)
class ExamEvent:
    """考试事件（源 exam 形态：当日一次考试及通过与否）。"""

    stat_date: date
    passed: bool


@dataclass(frozen=True)
class RadarSnapshot:
    """雷达快照（源 trainee.radar / radar_snapshot 形态：四维分值）。"""

    stat_date: date
    radar: dict[str, float]


@dataclass(frozen=True)
class TraineeProfile:
    """学员档案（源 trainee 表形态：姓名/认证状态/累计路径/当前雷达）。"""

    trainee_id: int
    name: str
    cert_status: str = "uncertified"
    path_pct: float = 0.0
    radar_current: dict[str, float] = field(default_factory=dict)


class BoardDataSource(Protocol):
    """看板事件流数据源协议（鸭子类型注入接缝；生产为 PG 查询形态）。"""

    def list_trainee_ids(self) -> Sequence[int]:
        """全体学员 id（聚合任务全量口径）。"""
        ...

    def trainee_profile(self, trainee_id: int) -> TraineeProfile:
        """学员档案；不存在抛 KeyError（API 层转 40404）。"""
        ...

    def iter_learning(self, trainee_id: int) -> Iterator[LearningEvent]:
        """学习日志事件流（只读）。"""
        ...

    def iter_drills(self, trainee_id: int) -> Iterator[DrillEvent]:
        """演练事件流（只读）。"""
        ...

    def iter_exams(self, trainee_id: int) -> Iterator[ExamEvent]:
        """考试事件流（只读）。"""
        ...

    def iter_radars(self, trainee_id: int) -> Iterator[RadarSnapshot]:
        """雷达快照流（只读）。"""
        ...


# --------------------------------------------------------------------------- 预聚合


@dataclass(frozen=True)
class DailyStat:
    """学员每日预聚合行（trainee_daily 形态；PK = (trainee_id, stat_date)）。"""

    stat_date: date
    study_minutes: int = 0
    lessons_done: int = 0
    path_pct: float = 0.0
    drill_count: int = 0
    drill_avg_score: float | None = None
    exam_count: int = 0
    exam_pass_count: int = 0
    radar: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """导出 dict（趋势序列 ECharts 数据源形态）。"""
        return {
            "stat_date": self.stat_date.isoformat(),
            "study_minutes": self.study_minutes,
            "lessons_done": self.lessons_done,
            "path_pct": self.path_pct,
            "drill_count": self.drill_count,
            "drill_avg_score": self.drill_avg_score,
            "exam_count": self.exam_count,
            "exam_pass_count": self.exam_pass_count,
            "radar": dict(self.radar),
        }


class DailyStatsStore:
    """每日预聚合存储（进程内线程安全；生产为 PG trainee_daily 表）。

    写入口径对齐 SR-TR-006 §6.1 ``ON CONFLICT (trainee_id, stat_date) DO UPDATE``：
    同键覆盖写（幂等），读侧 window/latest 服务识别引擎与看板查询。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[tuple[int, date], DailyStat] = {}

    def upsert(self, trainee_id: int, stat: DailyStat) -> None:
        """覆盖写一行（幂等：同 (trainee, day) 重算覆盖，不产生重复行）。"""
        with self._lock:
            self._rows[(trainee_id, stat.stat_date)] = stat

    def get(self, trainee_id: int, stat_date: date) -> DailyStat | None:
        """按键取行；缺失返回 None。"""
        with self._lock:
            return self._rows.get((trainee_id, stat_date))

    def latest(self, trainee_id: int, *, on_or_before: date) -> DailyStat | None:
        """不晚于指定日的最近一行（看板「当前值」口径）。"""
        with self._lock:
            candidates = [d for (tid, d) in self._rows if tid == trainee_id and d <= on_or_before]
            return self._rows[(trainee_id, max(candidates))] if candidates else None

    def window(self, trainee_id: int, *, end: date, days: int) -> list[DailyStat]:
        """[end-days+1, end] 窗口内已聚合行（升序；缺失日无行，识别引擎自补零）。"""
        start = end - timedelta(days=days - 1)
        with self._lock:
            return [self._rows[(trainee_id, d)] for d in sorted(
                d for (tid, d) in self._rows if tid == trainee_id and start <= d <= end
            )]

    def count(self) -> int:
        """当前行数（幂等重放断言用）。"""
        with self._lock:
            return len(self._rows)


class TeamStatsAggregator:
    """团队统计聚合器：事件流 → 每日预聚合物化（SR-TR-006 §6.1 工程级基线形态）。

    生产为单条聚合 SQL + ON CONFLICT；demo 以事件流遍历等价实现，幂等语义一致：
    同日重算覆盖写；空数据日为全员补零（读路径永不空窗）。
    """

    def __init__(self, source: BoardDataSource, store: DailyStatsStore | None = None) -> None:
        self._source = source
        self._store = store if store is not None else DailyStatsStore()

    @property
    def store(self) -> DailyStatsStore:
        """聚合结果存储（API 层与识别引擎共用）。"""
        return self._store

    def aggregate_day(self, day: date, trainee_ids: Sequence[int] | None = None) -> int:
        """聚合指定自然日：返回写入行数（= 学员数，含补零行）。

        来源映射（SR §6.1 AGG_SQL 口径）：学习时长/课时求和、路径完成率取累计最大、
        演练次数与均分、考试次数与通过数、雷达快照取当日或最近一次（≤ 当日）。
        """
        ids = list(trainee_ids) if trainee_ids is not None else list(self._source.list_trainee_ids())
        for tid in ids:
            self._store.upsert(tid, self._aggregate_trainee_day(tid, day))
        return len(ids)

    def aggregate_range(self, start: date, end: date) -> int:
        """区间聚合（含首尾两天），返回累计写入行数。"""
        written = 0
        day = start
        while day <= end:
            written += self.aggregate_day(day)
            day += timedelta(days=1)
        return written

    # ── 内部：单学员单日聚合 ──
    def _aggregate_trainee_day(self, trainee_id: int, day: date) -> DailyStat:
        minutes = lessons = 0
        path_pct = 0.0
        for event in self._source.iter_learning(trainee_id):
            if event.stat_date != day:
                continue
            minutes += event.minutes
            lessons += event.lessons_done
            path_pct = max(path_pct, event.path_pct)  # 累计完成率：当日口径取最大
        drills = [e.score for e in self._source.iter_drills(trainee_id) if e.stat_date == day]
        exams = [e for e in self._source.iter_exams(trainee_id) if e.stat_date == day]
        radar = self._latest_radar(trainee_id, day)
        return DailyStat(
            stat_date=day,
            study_minutes=minutes,
            lessons_done=lessons,
            path_pct=path_pct,
            drill_count=len(drills),
            drill_avg_score=(round(sum(drills) / len(drills), 2) if drills else None),
            exam_count=len(exams),
            exam_pass_count=sum(1 for e in exams if e.passed),
            radar=radar,
        )

    def _latest_radar(self, trainee_id: int, day: date) -> dict[str, float]:
        """当日或最近一次（≤ 当日）雷达快照；无任何快照回退档案当前值，再无则空。"""
        best: RadarSnapshot | None = None
        for snapshot in self._source.iter_radars(trainee_id):
            if snapshot.stat_date <= day and (best is None or snapshot.stat_date >= best.stat_date):
                best = snapshot
        if best is not None:
            return dict(best.radar)
        try:
            return dict(self._source.trainee_profile(trainee_id).radar_current)
        except KeyError:
            return {}


# --------------------------------------------------------------------------- 识别引擎


@dataclass(frozen=True)
class Hit:
    """一次规则命中（可解释性落点：规则编码 + 参数快照 + 数据证据）。"""

    flag_type: str
    rule_key: str
    rule_name: str
    rule_params: dict[str, Any]
    evidence: dict[str, Any]


@dataclass
class RuleContext:
    """单学员规则评估上下文（一次装载：预聚合窗口 + 原始得分/考试序列）。"""

    trainee_id: int
    profile: TraineeProfile
    rows: list[DailyStat]
    drills: list[DrillEvent]
    exams: list[ExamEvent]
    today: date
    window_days: int = 30

    # ── 规则视图助手（SR-TR-006 §6.3 DailyWindow 口径）──
    def row_at(self, day: date) -> DailyStat | None:
        """指定日的预聚合行；缺失返回 None。"""
        for row in self.rows:
            if row.stat_date == day:
                return row
        return None

    def radar_avg_at(self, day: date) -> float | None:
        """指定日雷达四维均分；当日无行或雷达为空返回 None（证据不足不判定）。"""
        row = self.row_at(day)
        if row is None or not row.radar:
            return None
        values = [row.radar[k] for k in RADAR_DIMS if k in row.radar]
        return sum(values) / len(values) if values else None

    def daily_lessons(self, days: int) -> list[dict[str, Any]]:
        """最近 days 天逐日课时（缺行按 0 计，日历口径连续）。"""
        result: list[dict[str, Any]] = []
        by_date = {row.stat_date: row for row in self.rows}
        for offset in range(days - 1, -1, -1):
            day = self.today - timedelta(days=offset)
            row = by_date.get(day)
            result.append({"date": day.isoformat(), "lessons_done": row.lessons_done if row else 0})
        return result

    def consecutive_zero_lesson_days(self) -> int:
        """自今日往前连续 0 课时天数（缺行视为无学习；窗口起点截断）。"""
        by_date = {row.stat_date: row for row in self.rows}
        earliest = min(by_date) if by_date else self.today + timedelta(days=1)
        streak = 0
        day = self.today
        while day >= earliest:
            row = by_date.get(day)
            if row is not None and row.lessons_done > 0:
                break
            streak += 1
            day -= timedelta(days=1)
        return streak

    def drill_scores(self, last_n: int) -> list[float]:
        """窗口内最近 last_n 次演练得分（按日升序取尾）。"""
        ordered = sorted(self.drills, key=lambda e: e.stat_date)
        return [e.score for e in ordered[-last_n:]] if last_n > 0 else []

    def exam_fails(self, window_days: int) -> list[ExamEvent]:
        """窗口内未通过的考试事件。"""
        start = self.today - timedelta(days=window_days)
        return [e for e in self.exams if not e.passed and e.stat_date > start]

    @property
    def path_pct(self) -> float:
        """当前路径完成率（最新预聚合行优先，回退档案）。"""
        if self.rows:
            return self.rows[-1].path_pct
        return self.profile.path_pct

    @property
    def cert_status(self) -> str:
        """认证状态（档案口径）。"""
        return self.profile.cert_status


#: 规则函数签名：(上下文, 参数) → 命中或 None
RuleFn = Callable[[RuleContext, Mapping[str, Any]], "Hit | None"]


def _hp1_radar_growth(ctx: RuleContext, p: Mapping[str, Any]) -> Hit | None:
    """HP-1 雷达增速：近 window_days 天四维均分周增速达标且当前分不低于下限。"""
    window = int(p["window_days"])
    cur = ctx.radar_avg_at(ctx.today)
    base = ctx.radar_avg_at(ctx.today - timedelta(days=window))
    if cur is None or base is None:
        return None
    growth = (cur - base) * 7.0 / window
    if growth >= float(p["growth_per_week"]) and cur >= float(p["score_floor"]):
        return Hit(
            FLAG_HIGH_POTENTIAL, "HP-1", "雷达增速领跑", dict(p),
            {"radar_then": round(base, 1), "radar_now": round(cur, 1),
             "growth_per_week": round(growth, 2)},
        )
    return None


def _hp2_drill_stable(ctx: RuleContext, p: Mapping[str, Any]) -> Hit | None:
    """HP-2 演练稳定高：最近 last_n 次演练均分达标且标准差受控（发挥稳定）。"""
    last_n = int(p["last_n"])
    scores = ctx.drill_scores(last_n)
    if len(scores) < last_n:
        return None
    avg = sum(scores) / len(scores)
    variance = sum((s - avg) ** 2 for s in scores) / len(scores)
    if avg >= float(p["min_avg"]) and variance ** 0.5 <= float(p["max_std"]):
        return Hit(
            FLAG_HIGH_POTENTIAL, "HP-2", "演练稳定高分", dict(p),
            {"scores": scores, "avg": round(avg, 1), "std": round(variance ** 0.5, 2)},
        )
    return None


def _ni1_progress_stall(ctx: RuleContext, p: Mapping[str, Any]) -> Hit | None:
    """NI-1 进度停滞：连续 stall_days 天完成课时为 0 且路径未结业。"""
    stall = ctx.consecutive_zero_lesson_days()
    if stall >= int(p["stall_days"]) and ctx.path_pct < 100.0:
        return Hit(
            FLAG_NEED_INTERVENTION, "NI-1", "学习进度停滞", dict(p),
            {"stall_days": stall, "threshold_days": int(p["stall_days"]),
             "daily_lessons": ctx.daily_lessons(int(p["stall_days"]) + 2)},
        )
    return None


def _ni2_consecutive_low(ctx: RuleContext, p: Mapping[str, Any]) -> Hit | None:
    """NI-2 连续低分：最近 consec 次演练得分全部低于阈值。"""
    consec = int(p["consec"])
    scores = ctx.drill_scores(consec)
    if len(scores) < consec or not all(s < float(p["threshold"]) for s in scores):
        return None
    return Hit(FLAG_NEED_INTERVENTION, "NI-2", "演练连续低分", dict(p), {"scores": scores})


def _ni3_exam_fail(ctx: RuleContext, p: Mapping[str, Any]) -> Hit | None:
    """NI-3 考试多次未过：窗口内未通过次数达标且尚未认证。"""
    fails = ctx.exam_fails(int(p["window_days"]))
    if len(fails) < int(p["times"]) or ctx.cert_status == "certified":
        return None
    return Hit(
        FLAG_NEED_INTERVENTION, "NI-3", "考试多次未过", dict(p),
        {"failed_exams": [{"date": e.stat_date.isoformat(), "passed": False} for e in fails]},
    )


@dataclass(frozen=True)
class RuleSpec:
    """规则注册项（注册制：新规则以 spec 注册即可参与每日评估）。"""

    key: str
    flag_type: str
    rule_name: str
    fn: RuleFn
    default_params: dict[str, Any]


#: 缺省规则参数（SR-TR-006 §7 app_config scope='strategy' 缺省值；demo 内置）
DEFAULT_RULE_PARAMS: dict[str, dict[str, Any]] = {
    "HP-1": {"window_days": 14, "growth_per_week": 1.0, "score_floor": 70.0},
    "HP-2": {"last_n": 10, "min_avg": 80.0, "max_std": 8.0},
    "NI-1": {"stall_days": 5},
    "NI-2": {"consec": 3, "threshold": 60.0},
    "NI-3": {"window_days": 30, "times": 2},
}

#: 缺省规则注册表（HP 两条 / NI 三条，编码与 SR §6.3 一致）
DEFAULT_RULES: tuple[RuleSpec, ...] = (
    RuleSpec("HP-1", FLAG_HIGH_POTENTIAL, "雷达增速领跑", _hp1_radar_growth, dict(DEFAULT_RULE_PARAMS["HP-1"])),
    RuleSpec("HP-2", FLAG_HIGH_POTENTIAL, "演练稳定高分", _hp2_drill_stable, dict(DEFAULT_RULE_PARAMS["HP-2"])),
    RuleSpec(
        "NI-1", FLAG_NEED_INTERVENTION, "学习进度停滞",
        _ni1_progress_stall, dict(DEFAULT_RULE_PARAMS["NI-1"]),
    ),
    RuleSpec(
        "NI-2", FLAG_NEED_INTERVENTION, "演练连续低分",
        _ni2_consecutive_low, dict(DEFAULT_RULE_PARAMS["NI-2"]),
    ),
    RuleSpec(
        "NI-3", FLAG_NEED_INTERVENTION, "考试多次未过",
        _ni3_exam_fail, dict(DEFAULT_RULE_PARAMS["NI-3"]),
    ),
)


class RuleEngine:
    """规则引擎：注册表 + 参数热更新（NFR-MAI-02 改参不发版）。

    参数合并语义：override 覆盖同名键、保留其余缺省键；每次命中的
    ``Hit.rule_params`` 为评估当时参数快照（历史标记可按当时参数解释）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._specs: dict[str, RuleSpec] = {}
        self._overrides: dict[str, dict[str, Any]] = {}

    @classmethod
    def default(cls) -> "RuleEngine":
        """装配五条缺省规则（HP-1/HP-2/NI-1/NI-2/NI-3）。"""
        engine = cls()
        for spec in DEFAULT_RULES:
            engine.register(spec)
        return engine

    def register(self, spec: RuleSpec) -> None:
        """注册/覆盖同键规则（幂等）。"""
        with self._lock:
            self._specs[spec.key] = spec

    def set_params(self, key: str, params: Mapping[str, Any]) -> None:
        """热更新规则参数（未注册的规则键拒绝，防配置漂移）。"""
        with self._lock:
            if key not in self._specs:
                raise KeyError(f"规则未注册，拒绝改参：{key}")
            self._overrides[key] = dict(params)

    def params(self, key: str) -> dict[str, Any]:
        """当前生效参数（缺省 + override 合并快照）。"""
        with self._lock:
            spec = self._specs[key]
            merged = dict(spec.default_params)
            merged.update(self._overrides.get(key, {}))
            return merged

    def specs(self) -> list[RuleSpec]:
        """全部已注册规则（评估遍历序稳定）。"""
        with self._lock:
            return [self._specs[key] for key in sorted(self._specs)]

    def evaluate(self, key: str, ctx: RuleContext) -> Hit | None:
        """以当前参数评估单条规则。"""
        with self._lock:
            spec = self._specs[key]
            params = self.params(key)
        return spec.fn(ctx, params)


def _now_iso() -> str:
    """缺省时间戳（UTC ISO-8601；测试经 now 参数注入固定值）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TraineeFlag:
    """识别命中登记（trainee_flag 形态；UNIQUE (trainee_id, rule_key)）。"""

    trainee_id: int
    flag_type: str
    rule_key: str
    rule_name: str
    rule_params: dict[str, Any]
    evidence: dict[str, Any]
    status: str = "open"  # open | confirmed | dismissed
    first_hit_at: str = ""
    last_eval_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """导出 dict（详情 API 的 flags 元素形态）。"""
        return {
            "trainee_id": self.trainee_id,
            "flag_type": self.flag_type,
            "rule_key": self.rule_key,
            "rule_name": self.rule_name,
            "rule_params": dict(self.rule_params),
            "evidence": dict(self.evidence),
            "status": self.status,
            "first_hit_at": self.first_hit_at,
            "last_eval_at": self.last_eval_at,
        }


class FlagStore:
    """命中登记存储（进程内线程安全；生产为 PG trainee_flag 表 + RLS 兜底）。

    upsert 语义（SR §6.3 evaluate_all）：命中保留 first_hit_at、刷新 last_eval_at
    与证据快照；不再命中且 status=open → 撤销（主管未确认的标记不留陈旧告警）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._flags: dict[tuple[int, str], TraineeFlag] = {}

    def upsert_hit(self, trainee_id: int, hit: Hit, *, now: str | None = None) -> TraineeFlag:
        """登记/刷新一条命中（唯一键防重复，并发安全）。"""
        stamp = now or _now_iso()
        with self._lock:
            key = (trainee_id, hit.rule_key)
            existing = self._flags.get(key)
            if existing is None:
                flag = TraineeFlag(
                    trainee_id=trainee_id, flag_type=hit.flag_type, rule_key=hit.rule_key,
                    rule_name=hit.rule_name, rule_params=dict(hit.rule_params),
                    evidence=dict(hit.evidence), status="open",
                    first_hit_at=stamp, last_eval_at=stamp,
                )
            else:
                flag = replace(
                    existing, rule_name=hit.rule_name, rule_params=dict(hit.rule_params),
                    evidence=dict(hit.evidence), last_eval_at=stamp,
                )
            self._flags[key] = flag
            return flag

    def retract_if_open(self, trainee_id: int, rule_key: str, *, now: str | None = None) -> bool:
        """不再命中且状态为 open → 撤销登记；confirmed/dismissed 保留。返回是否撤销。"""
        del now  # 撤销即删除登记，无需时间戳（生产可改写 status=closed 留痕）
        with self._lock:
            flag = self._flags.get((trainee_id, rule_key))
            if flag is None or flag.status != "open":
                return False
            del self._flags[(trainee_id, rule_key)]
            return True

    def confirm(self, trainee_id: int, rule_key: str) -> None:
        """主管确认标记（open → confirmed，此后不再自动撤销）。"""
        with self._lock:
            flag = self._flags.get((trainee_id, rule_key))
            if flag is not None:
                self._flags[(trainee_id, rule_key)] = replace(flag, status="confirmed")

    def flags_of(self, trainee_id: int) -> list[TraineeFlag]:
        """某学员全部在档标记。"""
        with self._lock:
            return [f for (tid, _k), f in sorted(self._flags.items()) if tid == trainee_id]

    def list_flags(
        self,
        flag_type: str | None = None,
        trainee_ids: Iterable[int] | None = None,
    ) -> list[TraineeFlag]:
        """标记名单（可按类型与学员集合过滤；scope 过滤由调用方注入集合）。"""
        allowed = frozenset(trainee_ids) if trainee_ids is not None else None
        with self._lock:
            return [
                f for (_tid, _k), f in sorted(self._flags.items())
                if (flag_type is None or f.flag_type == flag_type)
                and (allowed is None or f.trainee_id in allowed)
            ]

    def count_by_type(self, trainee_ids: Iterable[int] | None = None) -> dict[str, int]:
        """按标记类型统计去重学员数（总览 KPI 卡口径）。"""
        allowed = frozenset(trainee_ids) if trainee_ids is not None else None
        with self._lock:
            result: dict[str, int] = {}
            seen: set[tuple[str, int]] = set()
            for f in self._flags.values():
                if allowed is not None and f.trainee_id not in allowed:
                    continue
                key = (f.flag_type, f.trainee_id)
                if key not in seen:
                    seen.add(key)
                    result[f.flag_type] = result.get(f.flag_type, 0) + 1
            return result


class FlagEvaluator:
    """识别评估器：逐学员装载上下文 → 逐规则求值 → 登记或撤销。"""

    def __init__(
        self,
        source: BoardDataSource,
        store: DailyStatsStore,
        flag_store: FlagStore,
        *,
        engine: RuleEngine | None = None,
    ) -> None:
        self._source = source
        self._store = store
        self.flag_store = flag_store
        self.engine = engine if engine is not None else RuleEngine.default()

    def evaluate_trainee(
        self,
        trainee_id: int,
        *,
        today: date,
        window_days: int = 30,
        now: str | None = None,
    ) -> list[Hit]:
        """评估单学员：返回本次命中的全部标记（未命中规则同步撤销 open 登记）。"""
        profile = self._source.trainee_profile(trainee_id)
        start = today - timedelta(days=window_days - 1)
        ctx = RuleContext(
            trainee_id=trainee_id,
            profile=profile,
            rows=self._store.window(trainee_id, end=today, days=window_days),
            drills=sorted(
                (e for e in self._source.iter_drills(trainee_id) if e.stat_date >= start),
                key=lambda e: e.stat_date,
            ),
            exams=[e for e in self._source.iter_exams(trainee_id) if e.stat_date >= start],
            today=today,
            window_days=window_days,
        )
        hits: list[Hit] = []
        for spec in self.engine.specs():
            hit = self.engine.evaluate(spec.key, ctx)
            if hit is not None:
                self.flag_store.upsert_hit(trainee_id, hit, now=now)
                hits.append(hit)
            else:
                self.flag_store.retract_if_open(trainee_id, spec.key, now=now)
        return hits

    def evaluate_all(
        self,
        trainee_ids: Sequence[int] | None = None,
        *,
        today: date,
        window_days: int = 30,
        now: str | None = None,
    ) -> dict[int, list[Hit]]:
        """评估入口（每日全量 / 改参后重评；并发安全：存储层各自持锁）。"""
        ids = list(trainee_ids) if trainee_ids is not None else list(self._source.list_trainee_ids())
        return {tid: self.evaluate_trainee(tid, today=today, window_days=window_days, now=now) for tid in ids}


# --------------------------------------------------------------------------- 数据权限


class ScopeDenied(Exception):
    """数据范围越权（40301；语义对齐 foundation RBAC 码表，04-接口设计 §1.3）。"""

    def __init__(self, message: str, *, code: int = 40301) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TeamScope:
    """主管数据范围值对象（团队集合 + 本团队学员集合 + 管理员全量标志）。"""

    supervisor: str
    team_ids: tuple[int, ...]
    trainee_ids: frozenset[int]
    is_admin: bool = False


class TeamDirectory(Protocol):
    """团队名录协议（生产由 train_team/train_team_member 查询承担）。"""

    def trainee_ids_of_teams(self, team_ids: Sequence[int]) -> frozenset[int]:
        """多团队学员并集。"""
        ...

    def all_trainee_ids(self) -> frozenset[int]:
        """全体学员（管理员全量口径）。"""
        ...


#: 越权审计回调（action_type, detail）；生产写 audit_log（NFR-SEC-03）
AuditCallback = Callable[[str, str], None]


def resolve_scope(claims: Mapping[str, Any], directory: TeamDirectory) -> TeamScope:
    """JWT claims → 数据范围（SR-TR-006 §6.2）。

    - admin：全量学员（BYPASSRLS 语义）；
    - supervisor：claims.data_scope.team_ids → 本团队学员集合；空集合直接拒绝
      （绝不退化为全量）；
    - 其余角色：40301。
    """
    roles = {str(r) for r in (claims.get("roles") or [])}
    sub = str(claims.get("sub", ""))
    if "admin" in roles:
        return TeamScope(supervisor=sub, team_ids=(), trainee_ids=directory.all_trainee_ids(), is_admin=True)
    if "supervisor" not in roles:
        raise ScopeDenied("角色无权访问主管看板")
    data_scope = claims.get("data_scope") or {}
    team_ids = tuple(int(t) for t in (data_scope.get("team_ids") or []))
    trainee_ids = directory.trainee_ids_of_teams(team_ids)
    if not trainee_ids:
        raise ScopeDenied("名下无团队学员")
    return TeamScope(supervisor=sub, team_ids=team_ids, trainee_ids=trainee_ids)


def ensure_in_scope(scope: TeamScope, trainee_id: int, *, audit_cb: AuditCallback | None = None) -> None:
    """详情/名单类接口的越权校验：不在范围 → 审计 rbac_deny 留痕 + 40301。"""
    if scope.is_admin or trainee_id in scope.trainee_ids:
        return
    if audit_cb is not None:
        audit_cb("rbac_deny", f"supervisor={scope.supervisor} trainee_id={trainee_id}")
    raise ScopeDenied("无权查看该学员")
