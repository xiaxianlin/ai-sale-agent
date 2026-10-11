# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""学习路径规划器（FR-TR-005，SR-TR-005 §6.1 生成 / §6.2 进度状态机 / §6.3 重算；迭代 1.3）。

模块职责（SR-TR-005「编排与内容分离」关键决策的教学编排侧 demo 形态）：

- **课程映射注册制**：:class:`CourseCatalog` 维护 dimension → [course] 映射表
  （维度限雷达四维口径，编码唯一），课程内容本体归知识库（FR-KB-006），
  本文只做教学编排；
- **路径生成**：雷达快照 → 薄弱维排序（绝对分 < 阈值者优先按分升序，不足额从剩余
  有效维升序补齐；score=None 证据不足维不参与排序，由必修段覆盖并透传补证提示）
  → 三段组装（weak 薄弱突破置顶 / required 必修 / elective 选修，SRS §14.2 薄弱优先）
  → 前置依赖拓扑排程（Kahn，成环课时剔除并留诊断，不阻塞整条路径）；
- **进度状态机**：课时级 not_started / in_progress / completed / needs_retake 四态
  （``transition`` 纯函数，非法迁移显式拒绝）；测验结算事件 ``quiz_submitted`` 按
  通过线派生通过/失败（SR §6.2 口径）；乱序合并——测验结果先于完成事件到达时
  视同已完成学习再结算（取并集不丢单）；
- **完成事件幂等**：event_uid 唯一（重放返回首次结果），并发上报以进程锁串行化
  （生产为 DB 唯一约束 + Redis 锁，语义一致）；重修尝试超上限写 path_stuck
  主管干预标记（FR-TR-006 消费）；
- **评估联动重算**：每次重算 version+1、旧版本留档、active 切换；已完成课时进度按
  （学员 × 课时）唯一行保留并回填新路径（永不丢失——SR §6.3 核心口径）。

依据文档：docs/07-实现方案/TR/SR-TR-005-学习路径系统.md §2、§6.1、§6.2、§6.3、§7、§9；
        docs/07-实现方案/TR/SR-TR-004-能力评估系统.md §2（null 维不参与薄弱排序对齐）。
"""
from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from services.training.core.radar.engine import DIMENSIONS, DIM_LABELS, RadarSnapshot

# --------------------------------------------------------------------------- 三段与状态口径常量

#: 路径三段（weak 薄弱突破置顶 / required 必修基础 / elective 选修拓展，SR §4 path_item.section）
SECTION_WEAK: str = "weak"
SECTION_REQUIRED: str = "required"
SECTION_ELECTIVE: str = "elective"
SECTIONS: tuple[str, ...] = (SECTION_WEAK, SECTION_REQUIRED, SECTION_ELECTIVE)

#: 课时进度四态（落库值域，SR §4 lesson_progress.status CHECK）
PROGRESS_STATES: tuple[str, ...] = ("not_started", "in_progress", "completed", "needs_retake")

#: 进度状态机事件（quiz_submitted 为 SR §6.2 事件口径：按通过线派生结算）
EVENT_STARTED: str = "lesson_started"
EVENT_FINISHED: str = "lesson_finished"
EVENT_QUIZ_PASSED: str = "quiz_passed"
EVENT_QUIZ_FAILED: str = "quiz_failed"
EVENT_QUIZ_SUBMITTED: str = "quiz_submitted"
MACHINE_EVENTS: tuple[str, ...] = (EVENT_STARTED, EVENT_FINISHED, EVENT_QUIZ_PASSED, EVENT_QUIZ_FAILED)
PROGRESS_EVENTS: tuple[str, ...] = (*MACHINE_EVENTS, EVENT_QUIZ_SUBMITTED)
QUIZ_EVENTS: tuple[str, ...] = (EVENT_QUIZ_PASSED, EVENT_QUIZ_FAILED, EVENT_QUIZ_SUBMITTED)

#: 业务错误码（SR §5 错误口径的 TR-31xx 段；API 层映射 HTTP 状态族）
BIZ_NO_RADAR: str = "TR-3100"        # 无雷达快照（未完成诊断初评）
BIZ_NO_PATH: str = "TR-3101"         # 无进行中路径
BIZ_LESSON_NOT_IN_PATH: str = "TR-3102"
BIZ_LESSON_LOCKED: str = "TR-3103"
BIZ_ILLEGAL_TRANSITION: str = "TR-3104"


# --------------------------------------------------------------------------- 业务异常


class PathError(RuntimeError):
    """路径业务异常基类：携带 SR-TR-005 §5 业务码与 HTTP 状态（API 层映射信封）。"""

    biz_code: str = BIZ_NO_RADAR
    http_status: int = 404


class NoRadarSnapshotError(PathError):
    """无雷达快照可用（未完成诊断初评，TR-3100）。"""

    biz_code = BIZ_NO_RADAR
    http_status = 404


class NoActivePathError(PathError):
    """无进行中路径（未生成路径即查询/上报，TR-3101）。"""

    biz_code = BIZ_NO_PATH
    http_status = 404


class LessonNotInPathError(PathError):
    """课时不在当前路径（TR-3102）。"""

    biz_code = BIZ_LESSON_NOT_IN_PATH
    http_status = 404


class LessonLockedError(PathError):
    """前置课时未完成，课时锁定中（TR-3103）。"""

    biz_code = BIZ_LESSON_LOCKED
    http_status = 409


class IllegalTransitionError(PathError):
    """进度状态机非法迁移（TR-3104）。"""

    biz_code = BIZ_ILLEGAL_TRANSITION
    http_status = 409


# --------------------------------------------------------------------------- 配置与课程模型


@dataclass(frozen=True)
class PathConfig:
    """路径配置（SR §7 的进程内形态，构造期校验取值范围）。"""

    weak_dims_count: int = 2      # TR_PATH_WEAK_DIMS（1~4）
    weak_threshold: float = 70.0  # TR_PATH_WEAK_THRESHOLD（40~90，绝对薄弱线）
    quiz_pass_line: float = 60.0  # TR_PATH_QUIZ_PASS_LINE（40~90）
    max_attempts: int = 3         # TR_PATH_MAX_ATTEMPTS（1~10，超限转主管干预）

    def __post_init__(self) -> None:
        if isinstance(self.weak_dims_count, bool) or not isinstance(self.weak_dims_count, int):
            raise ValueError(f"weak_dims_count 须为整数，实为 {type(self.weak_dims_count).__name__}")
        if not 1 <= self.weak_dims_count <= 4:
            raise ValueError(f"weak_dims_count 超出取值范围 1~4（SR §7）：{self.weak_dims_count!r}")
        for name, value in (("weak_threshold", self.weak_threshold), ("quiz_pass_line", self.quiz_pass_line)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} 须为数值，实为 {type(value).__name__}")
            if not 40 <= float(value) <= 90:
                raise ValueError(f"{name} 超出取值范围 40~90（SR §7）：{value!r}")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError(f"max_attempts 须为整数，实为 {type(self.max_attempts).__name__}")
        if not 1 <= self.max_attempts <= 10:
            raise ValueError(f"max_attempts 超出取值范围 1~10（SR §7）：{self.max_attempts!r}")


@dataclass(frozen=True)
class Lesson:
    """课时（FR-KB-006 training_lesson 的编排侧投影）：内容本体不在本模块。"""

    lesson_uid: str
    title: str
    est_minutes: int = 10
    prereqs: tuple[str, ...] = ()  # 前置课时 uid（DAG；表内不存在的 uid 视为已满足并忽略边）
    has_quiz: bool = False


@dataclass(frozen=True)
class Course:
    """课程包（注册制映射单元）：所属能力维 + 课时序列；required 标记必修课程包。"""

    course_code: str
    title: str
    dim: str
    lessons: tuple[Lesson, ...]
    required: bool = False


class CourseCatalog:
    """课程映射注册表（dimension → [course]）：注册制配置的唯一事实来源。

    - 维度限雷达四维口径（与 FR-TR-004 强一致，越维注册拒绝）；
    - course_code 唯一（重复注册拒绝，防配置漂移）；
    - 线程安全（锁只护写路径）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_code: dict[str, Course] = {}
        self._by_dim: dict[str, list[Course]] = {dim: [] for dim in DIMENSIONS}

    def register(self, course: Course) -> Course:
        """注册一门课程：编码唯一 + 维度合法 + 课时非空校验。"""
        if not isinstance(course.course_code, str) or not course.course_code.strip():
            raise ValueError("课程编码 course_code 不能为空")
        if course.dim not in DIMENSIONS:
            raise ValueError(
                f"课程维度非法（须为雷达四维 {list(DIMENSIONS)}）：{course.dim!r}"
            )
        if not course.lessons:
            raise ValueError(f"课程 {course.course_code} 至少包含一个课时")
        for lesson in course.lessons:
            if not isinstance(lesson.lesson_uid, str) or not lesson.lesson_uid.strip():
                raise ValueError(f"课程 {course.course_code} 存在空 lesson_uid 的课时")
        with self._lock:
            if course.course_code in self._by_code:
                raise ValueError(f"课程编码重复注册：{course.course_code}（注册表唯一事实来源）")
            self._by_code[course.course_code] = course
            self._by_dim[course.dim].append(course)
        return course

    def courses_for(self, dim: str) -> tuple[Course, ...]:
        """按维度取已注册课程（注册序）。"""
        with self._lock:
            return tuple(self._by_dim.get(dim, ()))

    def required_courses(self) -> tuple[Course, ...]:
        """全部必修课程包（注册序）。"""
        with self._lock:
            return tuple(course for course in self._by_code.values() if course.required)


# --------------------------------------------------------------------------- 路径与进度模型


@dataclass(frozen=True)
class PathItem:
    """路径条目（路径 × 课时）：结构信息；status/locked 由视图读取时按实时进度合并。"""

    section: str
    order_no: int
    lesson_uid: str
    course_code: str
    title: str
    weak_dim: str | None  # weak 段命中的薄弱维（呼应雷达图徽标）
    est_minutes: int
    has_quiz: bool
    prereqs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "order_no": self.order_no,
            "lesson_uid": self.lesson_uid,
            "course_code": self.course_code,
            "title": self.title,
            "weak_dim": self.weak_dim,
            "weak_dim_label": DIM_LABELS.get(self.weak_dim) if self.weak_dim else None,
            "est_minutes": self.est_minutes,
            "has_quiz": self.has_quiz,
            "prereqs": list(self.prereqs),
        }


@dataclass(frozen=True)
class PlanDiagnostics:
    """生成诊断：成环剔除 / 证据不足维补证提示 / 本次重算保留的已完成课时数。"""

    cycle_dropped: tuple[str, ...] = ()
    uncovered_dims: tuple[str, ...] = ()
    kept_completed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_dropped": list(self.cycle_dropped),
            "uncovered_dims": list(self.uncovered_dims),
            "kept_completed": self.kept_completed,
        }


@dataclass(frozen=True)
class PathPlan:
    """一版学习路径（version 递增，同一学员至多一条 active）。"""

    plan_id: str
    trainee_id: int
    version: int
    radar_snapshot_id: str
    weak_dims: tuple[str, ...]
    items: tuple[PathItem, ...]
    diagnostics: PlanDiagnostics
    created_at: datetime
    status: str = "active"

    @property
    def lesson_uids(self) -> set[str]:
        """本版路径全部课时 uid 集合。"""
        return {item.lesson_uid for item in self.items}

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "trainee_id": self.trainee_id,
            "version": self.version,
            "radar_snapshot_id": self.radar_snapshot_id,
            "weak_dims": list(self.weak_dims),
            "items": [item.to_dict() for item in self.items],
            "diagnostics": self.diagnostics.to_dict(),
            "created_at": self.created_at.isoformat(),
            "status": self.status,
        }


@dataclass(frozen=True)
class LessonProgress:
    """课时进度（学员 × 课时全局唯一行，跨路径版本保留——重算不丢已完成）。"""

    trainee_id: int
    lesson_uid: str
    status: str = "not_started"
    attempts: int = 0              # 测验/重修尝试次数
    quiz_best: float | None = None  # 课后测验最高分（百分制）
    first_completed_at: datetime | None = None  # 首次完成时间（完成度计次锚点）
    updated_at: datetime | None = None


@dataclass(frozen=True)
class PathEvent:
    """路径事件流水（event_uid 幂等键，支持重放对账；含结算结果快照）。"""

    event_uid: str
    trainee_id: int
    kind: str
    lesson_uid: str
    score: float | None
    status: str  # 结算后的课时状态
    attempts: int
    stuck: bool
    occurred_at: datetime


@dataclass(frozen=True)
class ApplyResult:
    """进度事件应用结果（replay=True 表示幂等重放返回首次结果）。"""

    event_uid: str
    lesson_uid: str
    status: str
    completion_pct: float
    attempts: int
    stuck: bool
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_uid": self.event_uid,
            "lesson_uid": self.lesson_uid,
            "status": self.status,
            "completion_pct": self.completion_pct,
            "attempts": self.attempts,
            "stuck": self.stuck,
            "replayed": self.replayed,
        }


# --------------------------------------------------------------------------- 存储协议与内存实现


class PathPlanStore(Protocol):
    """路径存储协议（demo 内存实现；生产为 PG learning_path 表形态）。"""

    def next_plan_id(self) -> str:
        """分配下一个路径 id（线程安全递增）。"""
        ...

    def save(self, plan: PathPlan) -> None:
        """落库一版路径并将其置为该学员的 active（旧版本留档）。"""
        ...

    def active(self, trainee_id: int) -> PathPlan | None:
        """当前 active 路径；无则 None。"""
        ...

    def versions(self, trainee_id: int) -> list[PathPlan]:
        """该学员全部版本（按 version 升序）。"""
        ...


class InMemoryPathPlanStore:
    """进程内内存实现：plan_id 取号 + active 指针 + 版本留档。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plans: dict[str, PathPlan] = {}
        self._active: dict[int, str] = {}
        self._seq = 0

    def next_plan_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"LP-{self._seq:04d}"

    def save(self, plan: PathPlan) -> None:
        with self._lock:
            self._plans[plan.plan_id] = plan
            self._active[plan.trainee_id] = plan.plan_id

    def active(self, trainee_id: int) -> PathPlan | None:
        with self._lock:
            plan_id = self._active.get(trainee_id)
            return self._plans.get(plan_id) if plan_id else None

    def versions(self, trainee_id: int) -> list[PathPlan]:
        with self._lock:
            rows = [plan for plan in self._plans.values() if plan.trainee_id == trainee_id]
        return sorted(rows, key=lambda plan: plan.version)


class PathProgressStore(Protocol):
    """课时进度存储协议（学员 × 课时唯一行）。"""

    def get(self, trainee_id: int, lesson_uid: str) -> LessonProgress | None:
        """取单条进度；缺失返回 None。"""
        ...

    def set(self, progress: LessonProgress) -> None:
        """写入/覆盖单条进度（upsert 语义）。"""
        ...

    def list_by_trainee(self, trainee_id: int) -> list[LessonProgress]:
        """该学员全部进度行（按 lesson_uid 稳定排序）。"""
        ...


class InMemoryPathProgressStore:
    """进程内内存实现：(trainee_id, lesson_uid) 唯一键。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[int, str], LessonProgress] = {}

    def get(self, trainee_id: int, lesson_uid: str) -> LessonProgress | None:
        with self._lock:
            return self._rows.get((trainee_id, lesson_uid))

    def set(self, progress: LessonProgress) -> None:
        with self._lock:
            self._rows[(progress.trainee_id, progress.lesson_uid)] = progress

    def list_by_trainee(self, trainee_id: int) -> list[LessonProgress]:
        with self._lock:
            rows = [row for key, row in self._rows.items() if key[0] == trainee_id]
        return sorted(rows, key=lambda row: row.lesson_uid)


class PathEventStore(Protocol):
    """路径事件流水协议（event_uid 幂等查重）。"""

    def find(self, event_uid: str) -> PathEvent | None:
        """按幂等键查事件；不存在返回 None。"""
        ...

    def append(self, event: PathEvent) -> None:
        """追加事件（重复 uid 视为编程错误，拒绝落库）。"""
        ...


class InMemoryPathEventStore:
    """进程内内存实现：event_uid → PathEvent。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, PathEvent] = {}

    def find(self, event_uid: str) -> PathEvent | None:
        with self._lock:
            return self._events.get(event_uid)

    def append(self, event: PathEvent) -> None:
        with self._lock:
            if event.event_uid in self._events:
                raise ValueError(f"路径事件重复落库：{event.event_uid}（幂等键必须唯一）")
            self._events[event.event_uid] = event


# --------------------------------------------------------------------------- 状态机与薄弱排序（纯函数）


def transition(state: str, event: str, *, has_quiz: bool) -> str:
    """课时进度状态机（纯函数）：非法迁移显式拒绝（TR-3104）。

    合法迁移（SR-TR-005 §6.2，落库四态；``awaiting_quiz`` 为内存瞬态不落库——
    有测验课时 ``lesson_finished`` 后保持 ``in_progress`` 直至测验结算）：
    - not_started --lesson_started--> in_progress
    - not_started/in_progress --lesson_finished--> completed（无测验）/ in_progress（有测验）
    - in_progress --quiz_passed--> completed；--quiz_failed--> needs_retake
    - needs_retake --lesson_started--> in_progress（重修先开始）；
      --quiz_passed--> completed；--quiz_failed--> needs_retake
    - completed --quiz_passed--> completed（重复交卷幂等：已完成不回退）
    其余组合（如 completed 回退、needs_retake 未开始即学完、未开始直接出测验结果）
    一律抛 :class:`IllegalTransitionError`（未开始的测验乱序合并由服务层先推 ``lesson_finished``）。
    """
    if state not in PROGRESS_STATES:
        raise ValueError(f"进度状态非法：{state!r}（须为 {list(PROGRESS_STATES)}）")
    if event not in MACHINE_EVENTS:
        raise ValueError(f"进度事件非法：{event!r}（状态机口径为 {list(MACHINE_EVENTS)}）")
    if event == EVENT_FINISHED:
        if state in ("not_started", "in_progress"):
            return "completed" if not has_quiz else "in_progress"
        raise IllegalTransitionError(f"非法进度迁移：{state} --lesson_finished-->（已完成/需重修须先重新开始）")
    table: dict[tuple[str, str], str] = {
        ("not_started", EVENT_STARTED): "in_progress",
        ("in_progress", EVENT_QUIZ_PASSED): "completed",
        ("in_progress", EVENT_QUIZ_FAILED): "needs_retake",
        ("needs_retake", EVENT_STARTED): "in_progress",
        ("needs_retake", EVENT_QUIZ_PASSED): "completed",
        ("needs_retake", EVENT_QUIZ_FAILED): "needs_retake",
        ("completed", EVENT_QUIZ_PASSED): "completed",
    }
    target = table.get((state, event))
    if target is None:
        raise IllegalTransitionError(f"非法进度迁移：{state} --{event}-->（状态机未定义该迁移）")
    return target


def rank_weak_dims(
    dims: Mapping[str, Mapping[str, Any]], *, threshold: float, count: int
) -> list[str]:
    """薄弱维排序（SR §6.1）：绝对分 < 阈值者优先按分升序，不足额从剩余有效维升序补齐。

    score=None（证据不足）维度不参与排序（对齐 SR-TR-004 §2 决策），由必修段覆盖。
    同分按四维口径顺序稳定排序（可回放）。
    """
    scored = [
        (dim, dims[dim]["score"])
        for dim in DIMENSIONS
        if dims.get(dim, {}).get("score") is not None
    ]
    scored.sort(key=lambda pair: (pair[1], DIMENSIONS.index(pair[0])))
    weak = [dim for dim, score in scored if score < threshold]
    rest = [dim for dim, _ in scored if dim not in weak]
    return (weak + rest)[: max(0, count)]


@dataclass(frozen=True)
class _Entry:
    """拓扑排程内部条目：课时 + 所属段 + 薄弱维标注 + 课程编码（生成期临时结构）。"""

    lesson: Lesson
    section: str
    weak_dim: str | None
    course_code: str


def _topo_order(entries: list[_Entry]) -> tuple[list[_Entry], tuple[str, ...]]:
    """前置依赖拓扑排程（Kahn，稳定序）：成环课时剔除并返回剔除清单（不阻塞整条路径）。

    - 边仅在建于「本次入选课时」之间；表内不存在的 prereq 视为已满足（忽略边）；
    - 同一批次按入选顺序弹出（保证 order_no 可回放）。
    """
    by_uid = {entry.lesson.lesson_uid: entry for entry in entries}
    indegree = {
        uid: sum(1 for pre in entry.lesson.prereqs if pre in by_uid) for uid, entry in by_uid.items()
    }
    ordered: list[_Entry] = []
    emitted: set[str] = set()
    remaining = list(entries)
    while remaining:
        progressed = False
        for entry in list(remaining):
            uid = entry.lesson.lesson_uid
            if indegree[uid] == 0:
                ordered.append(entry)
                emitted.add(uid)
                remaining.remove(entry)
                for other_uid, other in by_uid.items():
                    if uid in other.lesson.prereqs and other_uid not in emitted:
                        indegree[other_uid] -= 1
                progressed = True
        if not progressed:  # 剩余全部处于环上：剔除并告警（SR §6.1 on_cycle="drop_and_alert"）
            break
    dropped = tuple(entry.lesson.lesson_uid for entry in remaining)
    return ordered, dropped


# --------------------------------------------------------------------------- 规划器


class PathPlanner:
    """学习路径规划器：雷达快照 → 薄弱排序 → 课程映射 → 三段路径 → 进度状态机 → 重算保留。"""

    def __init__(
        self,
        catalog: CourseCatalog,
        plan_store: PathPlanStore,
        progress_store: PathProgressStore,
        event_store: PathEventStore,
        *,
        config: PathConfig | None = None,
        radar_store: Any | None = None,
    ) -> None:
        self.catalog = catalog
        self.plan_store = plan_store
        self.progress_store = progress_store
        self.event_store = event_store
        self.config = config if config is not None else PathConfig()
        self.radar_store = radar_store  # 只读消费雷达快照（SR §2：上游只读）；None 则无法自动重算
        self.interventions: list[dict[str, Any]] = []  # path_stuck 主管干预标记（FR-TR-006 消费）
        self._lock = threading.RLock()

    # ── 路径生成与重算 ──────────────────────────────────────────────────────
    def plan(self, trainee_id: int, snapshot: RadarSnapshot, *, now: datetime | None = None) -> PathPlan:
        """由雷达快照生成一版路径：version+1、旧版留档、active 切换、已完成进度回填。"""
        missing = [dim for dim in DIMENSIONS if dim not in snapshot.dims]
        if missing:
            raise ValueError(f"雷达快照维度残缺，拒绝生成路径：缺少 {missing}（与 FR-TR-004 口径强一致）")
        at = now if now is not None else datetime.now(timezone.utc)
        with self._lock:  # 重算串行化（生产为 pg_advisory_xact_lock(88051+tid)，SR §6.3）
            weak = rank_weak_dims(
                snapshot.dims, threshold=self.config.weak_threshold, count=self.config.weak_dims_count
            )
            covered_before = {
                row.lesson_uid
                for row in self.progress_store.list_by_trainee(trainee_id)
                if row.status == "completed"
            }
            entries = self._collect_entries(weak)
            ordered, dropped = _topo_order(entries)
            items = self._build_items(ordered)
            kept = len(covered_before & items.keys())  # 本次重算保留的已完成课时（前端横幅口径）
            plan = PathPlan(
                plan_id=self.plan_store.next_plan_id(),
                trainee_id=trainee_id,
                version=self._next_version(trainee_id),
                radar_snapshot_id=snapshot.snapshot_id,
                weak_dims=tuple(weak),
                items=tuple(items.values()),
                diagnostics=PlanDiagnostics(
                    cycle_dropped=dropped,
                    uncovered_dims=tuple(
                        dim for dim in DIMENSIONS if snapshot.dims[dim].get("score") is None
                    ),
                    kept_completed=kept,
                ),
                created_at=at,
            )
            self.plan_store.save(plan)  # 存储内置旧版留档 + active 切换
        return plan

    def replan(
        self,
        trainee_id: int,
        *,
        now: datetime | None = None,
        snapshot: RadarSnapshot | None = None,
    ) -> PathPlan:
        """评估更新后重算（SR §6.3）：取雷达最新快照重新生成一版路径并返回 :class:`PathPlan`。

        - ``snapshot`` 显式传入时优先生效（测试/手动指定快照）；否则读雷达存储最新快照；
        - 两者皆无 → :class:`NoRadarSnapshotError`（TR-3100，未完成诊断初评）；
        - 保留的已完成课时数见 ``plan.diagnostics.kept_completed``（前端横幅口径），
          实时视图（状态/完成度）经 :meth:`current_view` 读取。
        """
        snap = snapshot
        if snap is None:
            if self.radar_store is None:
                raise NoRadarSnapshotError("雷达快照存储未装配，无法自动重算路径（TR-3100）")
            history = self.radar_store.list_by_trainee(trainee_id)
            snap = history[-1] if history else None
        if snap is None:
            raise NoRadarSnapshotError("未完成诊断初评，无雷达快照可用（TR-3100）")
        self.plan(trainee_id, snap, now=now)
        plan = self.active(trainee_id)
        assert plan is not None  # plan() 刚落库，active 必存在；断言仅为类型收窄
        return plan

    def _collect_entries(self, weak: Sequence[str]) -> list[_Entry]:
        """三段课时入选（注册制映射）：weak 置顶 → required → elective，跨段按课时去重。

        - weak 段：薄弱维的非必修课程（必修课程包固定走 required 段，不参与薄弱映射）；
        - elective 段：非薄弱维的非必修课程（已掌握内容不重复编排为薄弱突破）。
        """
        entries: list[_Entry] = []
        seen: set[str] = set()
        weak_set = set(weak)

        def _push(courses: Sequence[Course], section: str) -> None:
            for course in courses:
                for lesson in course.lessons:
                    if lesson.lesson_uid in seen:
                        continue
                    seen.add(lesson.lesson_uid)
                    weak_dim = course.dim if section == SECTION_WEAK and course.dim in weak_set else None
                    entries.append(
                        _Entry(
                            lesson=lesson,
                            section=section,
                            weak_dim=weak_dim,
                            course_code=course.course_code,
                        )
                    )

        _push(
            [course for dim in weak for course in self.catalog.courses_for(dim) if not course.required],
            SECTION_WEAK,
        )
        _push(self.catalog.required_courses(), SECTION_REQUIRED)
        _push(
            [
                course
                for dim in DIMENSIONS
                if dim not in weak_set
                for course in self.catalog.courses_for(dim)
                if not course.required
            ],
            SECTION_ELECTIVE,
        )
        return entries

    @staticmethod
    def _build_items(ordered: Sequence[_Entry]) -> dict[str, PathItem]:
        """拓扑序 → 段内 order_no 编号（weak/required/elective 各自从 1 起）。"""
        counters: dict[str, int] = {section: 0 for section in SECTIONS}
        items: dict[str, PathItem] = {}
        for entry in ordered:
            counters[entry.section] += 1
            items[entry.lesson.lesson_uid] = PathItem(
                section=entry.section,
                order_no=counters[entry.section],
                lesson_uid=entry.lesson.lesson_uid,
                course_code=entry.course_code,
                title=entry.lesson.title,
                weak_dim=entry.weak_dim,
                est_minutes=entry.lesson.est_minutes,
                has_quiz=entry.lesson.has_quiz,
                prereqs=entry.lesson.prereqs,
            )
        return items

    def _next_version(self, trainee_id: int) -> int:
        """下一版本号：历史最高版本 +1（首版为 1）。"""
        versions = self.plan_store.versions(trainee_id)
        return versions[-1].version + 1 if versions else 1

    # ── 当前路径视图（读时合并实时进度）────────────────────────────────────
    def active(self, trainee_id: int) -> PathPlan | None:
        """当前 active 路径。"""
        return self.plan_store.active(trainee_id)

    def current_view(self, trainee_id: int) -> dict[str, Any] | None:
        """当前路径视图：结构 + 实时进度合并（status/locked/attempts）+ 完成度统计。"""
        plan = self.plan_store.active(trainee_id)
        if plan is None:
            return None
        progress = {row.lesson_uid: row for row in self.progress_store.list_by_trainee(trainee_id)}
        plan_uids = plan.lesson_uids
        items: list[dict[str, Any]] = []
        completed = 0
        for item in plan.items:
            row = progress.get(item.lesson_uid)
            status = row.status if row is not None else "not_started"
            completed += 1 if status == "completed" else 0
            locked = any(
                prereq in plan_uids
                and (progress.get(prereq) is None or progress[prereq].status != "completed")
                for prereq in item.prereqs
            )
            view_item = item.to_dict()
            view_item.update(
                {
                    "status": status,
                    "locked": locked,
                    "attempts": row.attempts if row else 0,
                    "quiz_best": row.quiz_best if row else None,
                }
            )
            items.append(view_item)
        total = len(plan.items)
        by_section = {
            section: {
                "total": sum(1 for item in plan.items if item.section == section),
                "completed": sum(
                    1
                    for item in plan.items
                    if item.section == section
                    and (
                        progress.get(item.lesson_uid) is not None
                        and progress[item.lesson_uid].status == "completed"
                    )
                ),
            }
            for section in SECTIONS
        }
        return {
            "plan_id": plan.plan_id,
            "version": plan.version,
            "trainee_id": trainee_id,
            "radar_snapshot_id": plan.radar_snapshot_id,
            "weak_dims": list(plan.weak_dims),
            "items": items,
            "stats": {
                "total": total,
                "completed": completed,
                "completion_pct": round(completed / total, 4) if total else 0.0,
                "by_section": by_section,
            },
            "diagnostics": plan.diagnostics.to_dict(),
            "kept_completed": plan.diagnostics.kept_completed,  # 顶层透出（前端「已完成 N 课时已保留」横幅）
            "created_at": plan.created_at.isoformat(),
        }

    # ── 进度事件应用（状态机 + 幂等 + 锁定 + 干预标记）──────────────────────
    def apply_progress_event(
        self,
        trainee_id: int,
        *,
        event_uid: str,
        kind: str,
        lesson_uid: str,
        score: float | None = None,
        now: datetime | None = None,
    ) -> ApplyResult:
        """完成事件应用：幂等（event_uid）→ 路径归属 → 前置锁定 → 状态机迁移 → 落账。

        - ``quiz_submitted``：按通过线派生 quiz_passed/quiz_failed（SR §6.2 结算口径）；
        - 乱序合并：测验结果先于完成事件到达（state=not_started）时，视同已完成学习
          再结算（取并集，不丢单）；
        - 重修尝试达 ``max_attempts`` 仍不过 → path_stuck 主管干预标记（FR-TR-006 消费）。
        """
        if not isinstance(event_uid, str) or not event_uid.strip():
            raise ValueError("event_uid 不能为空（幂等键必填）")
        if kind not in PROGRESS_EVENTS:
            raise ValueError(f"进度事件类型非法（须为 {list(PROGRESS_EVENTS)}）：{kind!r}")
        if not isinstance(lesson_uid, str) or not lesson_uid.strip():
            raise ValueError("lesson_uid 不能为空")
        if kind in QUIZ_EVENTS:
            if score is None or isinstance(score, bool) or not isinstance(score, (int, float)):
                raise ValueError(f"测验事件 {kind} 必须携带 score（0-100 分）")
            if not 0 <= float(score) <= 100:
                raise ValueError(f"score 超出 0-100：{score!r}")
        at = now if now is not None else datetime.now(timezone.utc)
        with self._lock:  # 事件应用串行化（生产为 DB 唯一约束 + Redis 锁，SR §4/§8）
            prior = self.event_store.find(event_uid)
            if prior is not None:  # 幂等重放：返回首次结果
                return ApplyResult(
                    event_uid=prior.event_uid,
                    lesson_uid=prior.lesson_uid,
                    status=prior.status,
                    completion_pct=self._completion_pct_of(trainee_id, prior.lesson_uid),
                    attempts=prior.attempts,
                    stuck=prior.stuck,
                    replayed=True,
                )
            plan = self.plan_store.active(trainee_id)
            if plan is None:
                raise NoActivePathError("学员当前无进行中路径，无法上报进度（TR-3101）")
            item = next((entry for entry in plan.items if entry.lesson_uid == lesson_uid), None)
            if item is None:
                raise LessonNotInPathError(f"课时不在当前路径：{lesson_uid}（TR-3102）")
            progress_map = {row.lesson_uid: row for row in self.progress_store.list_by_trainee(trainee_id)}
            locked_prereqs = [
                prereq
                for prereq in item.prereqs
                if prereq in plan.lesson_uids
                and (prereq not in progress_map or progress_map[prereq].status != "completed")
            ]
            if locked_prereqs:
                raise LessonLockedError(
                    f"前置课时未完成，课时锁定中：{lesson_uid}（待完成 {locked_prereqs}，TR-3103）"
                )
            row = progress_map.get(lesson_uid) or LessonProgress(trainee_id=trainee_id, lesson_uid=lesson_uid)
            effective = kind
            if kind == EVENT_QUIZ_SUBMITTED:  # 按通过线派生结算（SR §6.2）
                effective = (
                    EVENT_QUIZ_PASSED if float(score) >= self.config.quiz_pass_line else EVENT_QUIZ_FAILED
                )
            if effective in (EVENT_QUIZ_PASSED, EVENT_QUIZ_FAILED) and row.status == "not_started":
                # 乱序合并：测验结果先到 → 视同已完成学习（有测验课时为待结算瞬态，取并集不丢单）
                row = replace(row, status=transition("not_started", EVENT_FINISHED, has_quiz=item.has_quiz))
            new_status = transition(row.status, effective, has_quiz=item.has_quiz)
            attempts = row.attempts + (1 if effective in (EVENT_QUIZ_PASSED, EVENT_QUIZ_FAILED) else 0)
            quiz_best = (
                max(row.quiz_best or 0.0, float(score))
                if effective in (EVENT_QUIZ_PASSED, EVENT_QUIZ_FAILED)
                else row.quiz_best
            )
            first_completed_at = row.first_completed_at or (at if new_status == "completed" else None)
            stuck = new_status == "needs_retake" and attempts >= self.config.max_attempts
            self.progress_store.set(
                replace(
                    row,
                    status=new_status,
                    attempts=attempts,
                    quiz_best=quiz_best,
                    first_completed_at=first_completed_at,
                    updated_at=at,
                )
            )
            self.event_store.append(
                PathEvent(
                    event_uid=event_uid,
                    trainee_id=trainee_id,
                    kind=kind,
                    lesson_uid=lesson_uid,
                    score=score,
                    status=new_status,
                    attempts=attempts,
                    stuck=stuck,
                    occurred_at=at,
                )
            )
            if stuck:
                self.interventions.append(
                    {
                        "trainee_id": trainee_id,
                        "kind": "path_stuck",
                        "lesson_uid": lesson_uid,
                        "attempts": attempts,
                    }
                )
            return ApplyResult(
                event_uid=event_uid,
                lesson_uid=lesson_uid,
                status=new_status,
                completion_pct=self._completion_pct_of(trainee_id, lesson_uid),
                attempts=attempts,
                stuck=stuck,
                replayed=False,
            )

    def _completion_pct_of(self, trainee_id: int, _lesson_uid: str) -> float:
        """当前路径完成度（事件结算后即时口径；无路径时 0.0）。"""
        plan = self.plan_store.active(trainee_id)
        if plan is None or not plan.items:
            return 0.0
        completed = sum(
            1
            for item in plan.items
            if (row := self.progress_store.get(trainee_id, item.lesson_uid)) is not None
            and row.status == "completed"
        )
        return round(completed / len(plan.items), 4)


__all__ = [
    "SECTION_WEAK",
    "SECTION_REQUIRED",
    "SECTION_ELECTIVE",
    "SECTIONS",
    "PROGRESS_STATES",
    "MACHINE_EVENTS",
    "PROGRESS_EVENTS",
    "BIZ_NO_RADAR",
    "BIZ_NO_PATH",
    "BIZ_LESSON_NOT_IN_PATH",
    "BIZ_LESSON_LOCKED",
    "BIZ_ILLEGAL_TRANSITION",
    "PathError",
    "NoRadarSnapshotError",
    "NoActivePathError",
    "LessonNotInPathError",
    "LessonLockedError",
    "IllegalTransitionError",
    "PathConfig",
    "Lesson",
    "Course",
    "CourseCatalog",
    "PathItem",
    "PlanDiagnostics",
    "PathPlan",
    "LessonProgress",
    "PathEvent",
    "ApplyResult",
    "PathPlanStore",
    "InMemoryPathPlanStore",
    "PathProgressStore",
    "InMemoryPathProgressStore",
    "PathEventStore",
    "InMemoryPathEventStore",
    "transition",
    "rank_weak_dims",
    "PathPlanner",
]
