# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""考试认证应用服务（FR-TR-007，SR-TR-007 §6.3~§6.5：评分编排 / 认证状态机 / 重考策略 / 审计）。

- **评分编排**（§6.3）：客观题答案集合精确比对（multi 全对才给分）；主观题经
  :class:`SubjectiveScorer` 协议评分（demo :class:`FakeRubricScorer` rubric 关键词覆盖，
  生产 DeepSeek-V3 rubric 评分）；评分失败降级 manual_pending 转人工队列，不产出无依据
  分数，人工分经 :meth:`ExamService.apply_manual_score` 回填后合卷；分维得分（FR-TR-004
  证据回流）与总分（客观/主观 6:4 配比）随交卷产出；
- **监考联动**（§6.2）：交卷时以 :class:`ProctorSession` 终判——嫌疑冻结
  status=review/passed=None 进人工复核；:meth:`apply_review` 主管裁决
  upheld（作废+冷却翻倍）/ overturned（撤销嫌疑并补授予）；
- **认证状态机**（§6.4）：cert_status 三态合法迁移矩阵
  （uncertified→certified→expired→certified），非法迁移拒绝；通过即授予
  （onboarded_at 覆盖式取最新）+ CERT_GRANT 审计；有效期届满 :meth:`expire_sweep` 置 expired；
- **重考策略**（§6.5）：冷却期 3 天（作弊 upheld 翻倍 6 天）+ 次数上限 3 次 +
  近 2 次已考题去重 + 认证覆盖式取最新；
- **并发口径**：demo 单进程以进程内锁实现交卷幂等——并发提交恰一胜（评分恰一次），
  生产为 advisory lock + uq_exam_ongoing 部分唯一索引（语义一致）；
- **上岗拦截**（§6.4 ★）：cert 状态查询 + 审计留痕由本服务提供，FastAPI 依赖
  ``require_certified`` 在 api 层（业务域共享，非 certified 一律 403 E_CERT_REQUIRED）。

依据文档：docs/07-实现方案/TR/SR-TR-007-考试认证系统.md §6.3~§6.5、§7、§8、§9。
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from services.training.core.exam.paper import (
    Paper,
    PaperGenerator,
    QuestionBank,
    QuestionBankItem,
    is_objective,
)
from services.training.core.exam.proctor import ProctorConfig, ProctorEvent, ProctorFlag, ProctorSession

# --------------------------------------------------------------------------- 认证状态机
# （SR §6.4，03-数据库设计 §3.3 CHECK 三态：uncertified/certified/expired）

CERT_UNCERTIFIED: str = "uncertified"
CERT_CERTIFIED: str = "certified"
CERT_EXPIRED: str = "expired"

#: 合法迁移矩阵：uncertified --通过--> certified --届满--> expired --重考通过--> certified
LEGAL_CERT_TRANSITIONS: dict[str, set[str]] = {
    CERT_UNCERTIFIED: {CERT_CERTIFIED},
    CERT_CERTIFIED: {CERT_EXPIRED},
    CERT_EXPIRED: {CERT_CERTIFIED},
}

#: 考试记录状态（SR §4 exam.status CHECK 七态的 demo 子集）
EXAM_ASSEMBLING: str = "assembling"
EXAM_ONGOING: str = "ongoing"
EXAM_SUBMITTED: str = "submitted"  # 待人工评分（主观降级挂起）
EXAM_REVIEW: str = "review"        # 监考嫌疑待复核
EXAM_COMPLETED: str = "completed"
EXAM_VOID: str = "void"


def transition_cert(current: str, target: str) -> str:
    """cert_status 合法迁移校验：非法迁移抛 :class:`CertTransitionError`（状态机硬约束）。"""
    if target not in LEGAL_CERT_TRANSITIONS.get(current, set()):
        raise CertTransitionError(
            f"非法认证状态迁移 {current!r} → {target!r}（合法迁移：{LEGAL_CERT_TRANSITIONS.get(current)}）"
        )
    return target


# --------------------------------------------------------------------------- 业务异常（SR §5 错误码）


class ExamError(RuntimeError):
    """考试业务异常基类：biz_code（E_*）/ numeric_code（数值信封码）/ http_status。"""

    biz_code: str = "E_EXAM_ERROR"
    numeric_code: int = 40001
    http_status: int = 400


class ExamNotFoundError(ExamError):
    """考试不存在（或非本人数据，E_EXAM_NOT_FOUND/40404/404）。"""

    biz_code = "E_EXAM_NOT_FOUND"
    numeric_code = 40404
    http_status = 404


class ExamOngoingError(ExamError):
    """同一学员已有进行中考试（E_EXAM_ONGOING/40902/409）。"""

    biz_code = "E_EXAM_ONGOING"
    numeric_code = 40902
    http_status = 409


class ExamNotOngoingError(ExamError):
    """考试未处于进行中（未开考交卷/状态不允许，E_EXAM_NOT_ONGOING/40902/409）。"""

    biz_code = "E_EXAM_NOT_ONGOING"
    numeric_code = 40902
    http_status = 409


class CertRequiredError(ExamError):
    """上岗拦截：cert_status != certified（E_CERT_REQUIRED/40301/403）——系统级强制。"""

    biz_code = "E_CERT_REQUIRED"
    numeric_code = 40301
    http_status = 403

    def __init__(self, *, cert_status: str = CERT_UNCERTIFIED, path: str = "") -> None:
        super().__init__(f"未通过上岗认证（当前 {cert_status}），考试认证通过后方可执行该操作")
        self.cert_status = cert_status
        self.path = path


class AttemptCooldownError(ExamError):
    """重考冷却期内禁考（E_ATTEMPT_COOLDOWN/42901/429）。"""

    biz_code = "E_ATTEMPT_COOLDOWN"
    numeric_code = 42901
    http_status = 429


class AttemptExceededError(ExamError):
    """重考次数超上限，需主管解锁（E_ATTEMPT_EXCEEDED/40302/403）。"""

    biz_code = "E_ATTEMPT_EXCEEDED"
    numeric_code = 40302
    http_status = 403


class ReviewNotPendingError(ExamError):
    """无待裁决监考嫌疑，复核操作冲突（E_REVIEW_NOT_PENDING/40902/409）。"""

    biz_code = "E_REVIEW_NOT_PENDING"
    numeric_code = 40902
    http_status = 409


class CertTransitionError(ExamError):
    """非法认证状态迁移（E_CERT_TRANSITION/40902/409）。"""

    biz_code = "E_CERT_TRANSITION"
    numeric_code = 40902
    http_status = 409


# --------------------------------------------------------------------------- 评分协议（SR §6.3）


class ScoringError(RuntimeError):
    """主观评分下游故障（LLM 超时/解析失败）：服务层降级 manual_pending，不产出无依据分数。"""


class SubjectiveScorer(Protocol):
    """主观题评分协议（demo FakeRubricScorer / 生产 DeepSeek rubric 评分器，同尺口径）。"""

    def score(self, question: QuestionBankItem, answer: str) -> int:
        """按 rubric 评分要点对作答打分（0 ≤ 分 ≤ question.score）。"""
        ...


class FakeRubricScorer:
    """确定性主观评分桩（demo 口径）：rubric 关键词覆盖度线性给分。

    与 SR-TR-004/SR-TR-003 校准同尺（rubric 锚点口径）；正式验收以
    DeepSeek-V3 rubric 评分 + 销售主管对比采样复测（§16.2 一致性 ≥85%）。
    """

    def score(self, question: QuestionBankItem, answer: str) -> int:
        """关键词覆盖度 → round(满分 × 命中数/要点数)。"""
        if not question.rubric:
            return 0
        text = (answer or "").lower()
        covered = sum(1 for kw in question.rubric if kw.lower() in text)
        return round(question.score * covered / len(question.rubric))


# --------------------------------------------------------------------------- 配置与数据模型


@dataclass(frozen=True)
class ExamConfig:
    """考试与认证配置（SR §7 TR_EXAM_*/TR_CERT_*/TR_ATTEMPT_* 缺省口径）。"""

    pass_line: float = 80.0
    """认证通过分数线（百分制）。"""

    obj_ratio: float = 0.6
    """客观题分占比（余为主观）。"""

    cooldown_days: int = 3
    """重考冷却期（天）。"""

    violation_cooldown_days: int = 6
    """作弊成立的冷却期（翻倍）。"""

    max_attempts: int = 3
    """认证周期内次数上限（超限需主管解锁）。"""

    validity_days: int = 365
    """认证有效期（届满 expired 触发重考）。"""

    dedup_recent: int = 2
    """防背题：排除近 N 次已考题。"""

    total_questions: int = 20
    """全卷总题数（demo 规模）。"""


@dataclass
class Trainee:
    """学员认证档案（trainee 表认证列的内存形态）。"""

    trainee_id: int
    cert_status: str = CERT_UNCERTIFIED
    onboarded_at: datetime | None = None
    """最近一次授予时间（覆盖式取最新）。"""

    violation_count: int = 0
    """作弊成立次数（冷却翻倍依据）。"""


@dataclass
class ExamRecord:
    """考试记录（exam 表 + SR §4 扩展列的内存形态；试卷快照随行固化）。"""

    exam_uid: str
    trainee_id: int
    attempt_no: int
    paper: Paper
    status: str = EXAM_ASSEMBLING
    session_token: str = ""
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    answers: dict[str, Any] = field(default_factory=dict)
    score: float | None = None
    passed: bool | None = None
    section_scores: dict[str, float] = field(default_factory=dict)
    per_question: list[dict[str, Any]] = field(default_factory=list)
    cheat_score: float = 0.0
    cheat_suspect: bool = False
    review_status: str = "none"  # none/pending/upheld/overturned
    manual_pending: tuple[str, ...] = ()
    manual_scores: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ExamOutcome:
    """考试结果视图（交卷/查询/复核后对外口径，passed=None 表示冻结未决）。"""

    exam_uid: str
    trainee_id: int
    attempt_no: int
    status: str
    score: float | None
    passed: bool | None
    section_scores: dict[str, float]
    per_question: list[dict[str, Any]]
    manual_pending: tuple[str, ...]
    cheat_score: float
    cheat_suspect: bool
    review_status: str
    submitted_at: datetime | None
    cert_status: str

    def to_dict(self) -> dict[str, Any]:
        """序列化形态（API data 段）。"""
        return {
            "exam_uid": self.exam_uid,
            "trainee_id": self.trainee_id,
            "attempt_no": self.attempt_no,
            "status": self.status,
            "score": self.score,
            "passed": self.passed,
            "section_scores": dict(self.section_scores),
            "per_question": [dict(q) for q in self.per_question],
            "manual_pending": list(self.manual_pending),
            "cheat_score": self.cheat_score,
            "cheat_suspect": self.cheat_suspect,
            "review_status": self.review_status,
            "submitted_at": self.submitted_at.isoformat(timespec="seconds") if self.submitted_at else None,
            "cert_status": self.cert_status,
        }


# --------------------------------------------------------------------------- 存储


class ExamStore(Protocol):
    """考试存储协议（生产为 PG：uq_exam_ongoing 部分唯一索引 + advisory lock，协议不变）。"""

    def save_trainee(self, trainee: Trainee) -> None: ...

    def get_trainee(self, trainee_id: int) -> Trainee | None: ...

    def list_trainees(self) -> Sequence[Trainee]: ...

    def save_exam(self, record: ExamRecord) -> None: ...

    def get_exam(self, exam_uid: str) -> ExamRecord | None: ...

    def find_ongoing_by_trainee(self, trainee_id: int) -> ExamRecord | None: ...

    def list_exams_by_trainee(self, trainee_id: int) -> Sequence[ExamRecord]: ...


class InMemoryExamStore:
    """内存考试存储（demo/测试）：线程安全，exam_uid 主键 + 学员索引。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._trainees: dict[int, Trainee] = {}
        self._exams: dict[str, ExamRecord] = {}
        self._seq = 0

    def save_trainee(self, trainee: Trainee) -> None:
        with self._lock:
            self._trainees[trainee.trainee_id] = trainee

    def get_trainee(self, trainee_id: int) -> Trainee | None:
        with self._lock:
            return self._trainees.get(trainee_id)

    def list_trainees(self) -> list[Trainee]:
        with self._lock:
            return list(self._trainees.values())

    def next_exam_uid(self) -> str:
        """考试单号（EX-000001 起自增）。"""
        with self._lock:
            self._seq += 1
            return f"EX-{self._seq:06d}"

    def save_exam(self, record: ExamRecord) -> None:
        with self._lock:
            self._exams[record.exam_uid] = record

    def get_exam(self, exam_uid: str) -> ExamRecord | None:
        with self._lock:
            return self._exams.get(exam_uid)

    def find_ongoing_by_trainee(self, trainee_id: int) -> ExamRecord | None:
        with self._lock:
            for record in self._exams.values():
                if record.trainee_id == trainee_id and record.status in (EXAM_ASSEMBLING, EXAM_ONGOING):
                    return record
        return None

    def list_exams_by_trainee(self, trainee_id: int) -> list[ExamRecord]:
        with self._lock:
            records = [r for r in self._exams.values() if r.trainee_id == trainee_id]
        return sorted(records, key=lambda r: (r.attempt_no, r.exam_uid))


# --------------------------------------------------------------------------- 服务


class ExamService:
    """考试认证应用服务：创建/开考/交卷评分/复核/过期扫描/认证查询 + 审计留痕。"""

    def __init__(
        self,
        bank: QuestionBank,
        store: ExamStore,
        scorer: SubjectiveScorer | None = None,
        *,
        generator: PaperGenerator | None = None,
        config: ExamConfig | None = None,
        proctor_config: ProctorConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        radar_fn: Callable[[int], dict[str, int]] | None = None,
    ) -> None:
        self._bank = bank
        self._store = store
        self._scorer = scorer if scorer is not None else FakeRubricScorer()
        self._config = config if config is not None else ExamConfig()
        self._proctor_config = proctor_config if proctor_config is not None else ProctorConfig()
        self._generator = generator if generator is not None else PaperGenerator(
            bank, total=self._config.total_questions
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._radar_fn = radar_fn
        self._submit_lock = threading.Lock()  # demo 单进程交卷幂等锁（生产 advisory lock）
        self._audit_lock = threading.Lock()
        self._audit: list[dict[str, Any]] = []
        self._sessions: dict[str, ProctorSession] = {}

    # ── 审计（NFR-SEC-03）──────────────────────────────────────────────────
    def audit(self, action: str, **detail: Any) -> dict[str, Any]:
        """写一条审计留痕（CERT_GRANT/CERT_BLOCKED/CERT_EXPIRE/REVIEW_* 等）。"""
        entry = {
            "action": action,
            "detail": dict(detail),
            "at": self._clock().isoformat(timespec="seconds"),
        }
        with self._audit_lock:
            self._audit.append(entry)
        return entry

    def audit_log(self) -> list[dict[str, Any]]:
        """审计流水快照（验收证据导出口）。"""
        with self._audit_lock:
            return [dict(e) for e in self._audit]

    # ── 学员与认证查询 ─────────────────────────────────────────────────────
    def _trainee(self, trainee_id: int) -> Trainee:
        """取或注册学员档案（首触自动建 uncertified 档）。"""
        trainee = self._store.get_trainee(trainee_id)
        if trainee is None:
            trainee = Trainee(trainee_id=trainee_id)
            self._store.save_trainee(trainee)
        return trainee

    def get_trainee(self, trainee_id: int) -> Trainee:
        """学员档案查询（缺档自动注册）。"""
        return self._trainee(trainee_id)

    def cert_status_of(self, trainee_id: int) -> str:
        """认证状态查询（require_certified 依赖的数据源；缓存策略见 SR §4 Redis 键）。"""
        trainee = self._store.get_trainee(trainee_id)
        return trainee.cert_status if trainee is not None else CERT_UNCERTIFIED

    def _grant(self, trainee_id: int, exam_uid: str) -> None:
        """授予认证（覆盖式取最新）：uncertified/expired → certified + CERT_GRANT 审计。

        生产为同事务 UPDATE + advisory lock 防并发双授予（SR §6.4），demo 单进程锁语义一致。
        """
        trainee = self._trainee(trainee_id)
        transition_cert(trainee.cert_status, CERT_CERTIFIED)
        trainee.cert_status = CERT_CERTIFIED
        trainee.onboarded_at = self._clock()  # 认证覆盖式取最新（重考再授予即刷新）
        self._store.save_trainee(trainee)
        self.audit("CERT_GRANT", trainee_id=trainee_id, exam_uid=exam_uid)

    def expire_sweep(self, now: datetime | None = None) -> list[int]:
        """认证过期扫描（每日兜底任务 demo 形态，幂等可重跑）：届满 → expired。"""
        moment = now or self._clock()
        expired: list[int] = []
        for trainee in self._store.list_trainees():
            if trainee.cert_status != CERT_CERTIFIED or trainee.onboarded_at is None:
                continue
            deadline = trainee.onboarded_at + timedelta(days=self._config.validity_days)
            if moment > deadline:  # 严格大于：恰好届满当日不判过期（SR §6.4 口径）
                transition_cert(trainee.cert_status, CERT_EXPIRED)
                trainee.cert_status = CERT_EXPIRED
                self._store.save_trainee(trainee)
                self.audit("CERT_EXPIRE", trainee_id=trainee.trainee_id)
                expired.append(trainee.trainee_id)
        return sorted(expired)

    # ── 创建 / 开考 / 交卷 ─────────────────────────────────────────────────
    def create_exam(self, trainee_id: int) -> ExamRecord:
        """创建考试（组卷）：进行中唯一 / 冷却期 / 次数上限 / 近 2 次去重（SR §6.5）。"""
        if not isinstance(trainee_id, int) or isinstance(trainee_id, bool) or trainee_id < 1:
            raise ValueError("trainee_id 必须为正整数")
        self._trainee(trainee_id)
        if self._store.find_ongoing_by_trainee(trainee_id) is not None:
            raise ExamOngoingError(f"学员 {trainee_id} 已有进行中考试，不可重复创建（E_EXAM_ONGOING）")
        history = self._store.list_exams_by_trainee(trainee_id)
        attempt_no = len(history) + 1
        if attempt_no > self._config.max_attempts:
            raise AttemptExceededError(
                f"认证周期内考试次数已达上限 {self._config.max_attempts}，需主管解锁（E_ATTEMPT_EXCEEDED）"
            )
        self._check_cooldown(history)
        exclude: set[str] = set()
        for record in history[-self._config.dedup_recent:]:
            exclude |= record.paper.question_ids()  # 防背题：排除近 N 次已考题
        radar = self._radar_fn(trainee_id) if self._radar_fn is not None else None
        paper = self._generator.generate(trainee_id, attempt_no, radar=radar, exclude=exclude)
        record = ExamRecord(
            exam_uid=self._store.next_exam_uid(), trainee_id=trainee_id,
            attempt_no=attempt_no, paper=paper,
        )
        self._store.save_exam(record)
        self.audit("EXAM_CREATED", trainee_id=trainee_id, exam_uid=record.exam_uid,
                   attempt_no=attempt_no, seed=paper.seed, degraded=paper.degraded)
        return record

    def _check_cooldown(self, history: Sequence[ExamRecord]) -> None:
        """冷却期校验：未过/作废后禁立即重考；作弊成立冷却翻倍（SR §6.5）。"""
        if not history:
            return
        last = max(
            history,
            key=lambda r: (r.submitted_at or datetime.min.replace(tzinfo=timezone.utc), r.exam_uid),
        )
        if last.status == EXAM_VOID:
            days = self._config.violation_cooldown_days
        elif last.status == EXAM_COMPLETED and last.passed is False:
            days = self._config.cooldown_days
        else:
            return
        ready_at = (last.submitted_at or self._clock()) + timedelta(days=days)
        if self._clock() < ready_at:
            raise AttemptCooldownError(
                f"重考冷却期内（{ready_at:%Y-%m-%d %H:%M} 前禁考，共 {days} 天）（E_ATTEMPT_COOLDOWN）"
            )

    def start(self, exam_uid: str) -> dict[str, Any]:
        """开考：assembling → ongoing，下发考生视图与会话令牌（幂等重启返回同令牌）。"""
        record = self._require(exam_uid)
        if record.status == EXAM_ONGOING:
            return self._start_view(record)
        if record.status != EXAM_ASSEMBLING:
            raise ExamNotOngoingError(f"考试 {exam_uid} 状态 {record.status} 不可开考（E_EXAM_NOT_ONGOING）")
        record.status = EXAM_ONGOING
        record.session_token = uuid.uuid4().hex[:16]
        record.started_at = self._clock()
        self._store.save_exam(record)
        return self._start_view(record)

    def _start_view(self, record: ExamRecord) -> dict[str, Any]:
        """开考下发面：考生视图（无答案）+ 会话令牌 + 时长。"""
        return {
            "exam_uid": record.exam_uid,
            "attempt_no": record.attempt_no,
            "session_token": record.session_token,
            "duration_minutes": record.paper.blueprint.duration_minutes,
            "questions": record.paper.questions_view(),
        }

    def submit(
        self,
        exam_uid: str,
        answers: Mapping[str, Any],
        *,
        events: Iterable[ProctorEvent] = (),
    ) -> ExamOutcome:
        """交卷评分（幂等，并发恰一胜）：评分 → 监考终判 → 认证联动（SR §6.3）。"""
        with self._submit_lock:  # 并发提交恰一胜：锁内状态复查，后至者重放首次结果
            record = self._require(exam_uid)
            if record.status in (EXAM_COMPLETED, EXAM_REVIEW, EXAM_VOID, EXAM_SUBMITTED):
                return self.get_outcome(exam_uid)  # 幂等重放
            if record.status != EXAM_ONGOING:
                raise ExamNotOngoingError(f"考试 {exam_uid} 未开考或已结束，不可交卷（E_EXAM_NOT_ONGOING）")
            record.answers = dict(answers)
            record.submitted_at = self._clock()
            self._grade(record)                      # ① 评分（客观精确 + 主观 rubric/降级）
            self._finalize_proctor(record, events)   # ② 监考终判（证据链 → 加权 → 阈值）
            if record.cheat_suspect:
                record.status = EXAM_REVIEW          # ③ 嫌疑冻结：passed=None 进人工复核
                record.review_status = "pending"
                self.audit("EXAM_REVIEW_PENDING", exam_uid=exam_uid, cheat_score=record.cheat_score)
            elif record.manual_pending:
                record.status = EXAM_SUBMITTED       # ④ 主观降级：待人工回填
            else:
                self._settle(record)                 # ⑤ 直通结算（判通过线 + 授予）
            self._store.save_exam(record)
            return self._outcome_of(record)

    def _grade(self, record: ExamRecord) -> None:
        """逐题判分：客观精确比对 + 主观 rubric 评分（失败降级 manual_pending）。"""
        per_question: list[dict[str, Any]] = []
        pending: list[str] = []
        for question in record.paper.questions:
            answer = record.answers.get(question.question_id, "")
            if is_objective(question.qtype):
                earned = question.score if self._objective_correct(question, answer) else 0
                per_question.append({
                    "question_id": question.question_id, "dim": question.dim,
                    "qtype": question.qtype, "max": question.score, "earned": earned,
                    "correct": earned == question.score,
                })
            else:
                try:
                    earned = max(0, min(question.score, int(self._scorer.score(question, str(answer)))))
                    pending_entry = False
                except ScoringError:
                    earned, pending_entry = 0, True
                    pending.append(question.question_id)
                per_question.append({
                    "question_id": question.question_id, "dim": question.dim,
                    "qtype": question.qtype, "max": question.score,
                    "earned": None if pending_entry else earned,
                    "manual_pending": pending_entry,
                })
        record.per_question = per_question
        record.manual_pending = tuple(pending)
        record.section_scores = self._section_scores(per_question)
        if not pending:
            record.score = self._total_score(per_question)

    @staticmethod
    def _objective_correct(question: QuestionBankItem, answer: Any) -> bool:
        """客观判分：single/judge 字符串精确比对；multi 集合全对才给分（无部分分）。"""
        if question.qtype == "multi":
            given = answer if isinstance(answer, (list, tuple, set)) else str(answer).split(",")
            return {str(item).strip() for item in given if str(item).strip()} == set(question.answer)
        return str(answer).strip() == str(question.answer).strip()

    @staticmethod
    def _section_scores(per_question: Sequence[Mapping[str, Any]]) -> dict[str, float]:
        """分维得分（FR-TR-004 证据回流）：各维已判题目的得分率均值（百分制）。"""
        buckets: dict[str, list[float]] = {}
        for item in per_question:
            if item["earned"] is None:
                continue
            pct = item["earned"] / item["max"] * 100 if item["max"] else 0.0
            buckets.setdefault(str(item["dim"]), []).append(pct)
        return {dim: round(sum(v) / len(v), 1) for dim, v in sorted(buckets.items())}

    def _total_score(self, per_question: Sequence[Mapping[str, Any]]) -> float:
        """合卷总分：客观/主观得分率按 obj_ratio 配比合成（默认 6:4，SR §6.3）。"""
        obj_earned = obj_max = subj_earned = subj_max = 0
        for item in per_question:
            if item["earned"] is None:
                continue
            if is_objective(str(item["qtype"])):
                obj_earned += int(item["earned"]); obj_max += int(item["max"])
            else:
                subj_earned += int(item["earned"]); subj_max += int(item["max"])
        obj_pct = obj_earned / obj_max * 100 if obj_max else 0.0
        subj_pct = subj_earned / subj_max * 100 if subj_max else 0.0
        return round(obj_pct * self._config.obj_ratio + subj_pct * (1 - self._config.obj_ratio), 1)

    def _finalize_proctor(self, record: ExamRecord, events: Iterable[ProctorEvent]) -> None:
        """监考终判：会话内事件（含逐题上报与交卷批次）→ 加权 → 阈值判定。"""
        session = self._sessions.setdefault(record.exam_uid, ProctorSession(self._proctor_config))
        session.ingest(list(events))
        record.cheat_score, record.cheat_suspect = session.finalize()

    def submit_proctor_events(self, exam_uid: str, events: Iterable[ProctorEvent]) -> list[ProctorFlag]:
        """考试中监考事件上报：服务端即时返回命中规则并累计证据链（SR §5 接口口径）。"""
        self._require(exam_uid)
        session = self._sessions.setdefault(exam_uid, ProctorSession(self._proctor_config))
        return session.ingest(list(events))

    # ── 结算 / 人工回填 / 复核 ─────────────────────────────────────────────
    def _settle(self, record: ExamRecord) -> None:
        """结算：过线 → passed=True + 授予认证；未过线 → passed=False（触发重考策略）。"""
        record.status = EXAM_COMPLETED
        record.passed = bool(record.score is not None and record.score >= self._config.pass_line)
        if record.passed:
            self._grant(record.trainee_id, record.exam_uid)

    def apply_manual_score(self, exam_uid: str, question_id: str, score: int) -> ExamOutcome:
        """人工评分回填：逐题补齐后合卷结算（SR §6.3 降级兜底；不阻塞其他题）。"""
        with self._submit_lock:
            record = self._require(exam_uid)
            if question_id not in record.manual_pending:
                raise ValueError(f"题目 {question_id} 不在待人工评分清单中")
            question = next(q for q in record.paper.questions if q.question_id == question_id)
            if not 0 <= score <= question.score:
                raise ValueError(f"人工分须在 [0, {question.score}] 内（实得 {score}）")
            record.manual_scores[question_id] = score
            remaining = tuple(qid for qid in record.manual_pending if qid not in record.manual_scores)
            record.manual_pending = remaining
            for item in record.per_question:
                if item["question_id"] == question_id:
                    item["earned"], item["manual_pending"] = score, False
            record.section_scores = self._section_scores(record.per_question)
            if not remaining:
                record.score = self._total_score(record.per_question)
                self._settle(record)
            self._store.save_exam(record)
            return self._outcome_of(record)

    def apply_review(self, exam_uid: str, verdict: str, *, reviewer: str, note: str = "") -> ExamOutcome:
        """主管复核裁决（防误判通道）：upheld 作废+冷却翻倍 / overturned 补授予（SR §6.4）。"""
        if verdict not in ("upheld", "overturned"):
            raise ValueError(f"复核裁决仅支持 upheld/overturned（实得 {verdict!r}）")
        with self._submit_lock:
            record = self._require(exam_uid)
            if record.review_status != "pending":
                raise ReviewNotPendingError(f"考试 {exam_uid} 无待裁决监考嫌疑（E_REVIEW_NOT_PENDING）")
            if verdict == "upheld":
                record.status = EXAM_VOID
                record.passed = False
                record.review_status = "upheld"
                trainee = self._trainee(record.trainee_id)
                trainee.violation_count += 1
                self._store.save_trainee(trainee)
                self.audit("REVIEW_UPHELD", exam_uid=exam_uid, reviewer=reviewer, note=note)
            else:
                record.review_status = "overturned"
                record.status = EXAM_COMPLETED
                record.passed = bool(record.score is not None and record.score >= self._config.pass_line)
                if record.passed:
                    self._grant(record.trainee_id, record.exam_uid)  # 撤销嫌疑并补授予
                self.audit("REVIEW_OVERTURNED", exam_uid=exam_uid, reviewer=reviewer, note=note)
            self._store.save_exam(record)
            return self._outcome_of(record)

    # ── 查询 ────────────────────────────────────────────────────────────────
    def get_paper(self, exam_uid: str) -> Paper:
        """试卷快照（判分复现/测试取全对作答；生产仅主管与管理员可及，NFR-SEC-02）。"""
        return self._require(exam_uid).paper

    def get_outcome(self, exam_uid: str) -> ExamOutcome:
        """考试结果视图（review 未决时 passed=None）。"""
        return self._outcome_of(self._require(exam_uid))

    def list_outcomes(self, trainee_id: int) -> list[ExamOutcome]:
        """学员历次考试结果（attempt_no 升序）。"""
        return [self._outcome_of(r) for r in self._store.list_exams_by_trainee(trainee_id)]

    def _require(self, exam_uid: str) -> ExamRecord:
        """取考试记录，不存在即 ExamNotFoundError（他人数据视同不存在）。"""
        record = self._store.get_exam(exam_uid)
        if record is None:
            raise ExamNotFoundError(f"考试不存在：{exam_uid}（E_EXAM_NOT_FOUND）")
        return record

    def _outcome_of(self, record: ExamRecord) -> ExamOutcome:
        """记录 → 结果视图（cert_status 实时取，授予/过期即时可见）。"""
        return ExamOutcome(
            exam_uid=record.exam_uid,
            trainee_id=record.trainee_id,
            attempt_no=record.attempt_no,
            status=record.status,
            score=record.score,
            passed=record.passed,
            section_scores=dict(record.section_scores),
            per_question=[dict(q) for q in record.per_question],
            manual_pending=tuple(record.manual_pending),
            cheat_score=record.cheat_score,
            cheat_suspect=record.cheat_suspect,
            review_status=record.review_status,
            submitted_at=record.submitted_at,
            cert_status=self.cert_status_of(record.trainee_id),
        )


__all__ = [
    "CERT_UNCERTIFIED",
    "CERT_CERTIFIED",
    "CERT_EXPIRED",
    "LEGAL_CERT_TRANSITIONS",
    "EXAM_ASSEMBLING",
    "EXAM_ONGOING",
    "EXAM_SUBMITTED",
    "EXAM_REVIEW",
    "EXAM_COMPLETED",
    "EXAM_VOID",
    "transition_cert",
    "ExamError",
    "ExamNotFoundError",
    "ExamOngoingError",
    "ExamNotOngoingError",
    "CertRequiredError",
    "AttemptCooldownError",
    "AttemptExceededError",
    "ReviewNotPendingError",
    "CertTransitionError",
    "ScoringError",
    "SubjectiveScorer",
    "FakeRubricScorer",
    "ExamConfig",
    "Trainee",
    "ExamRecord",
    "ExamOutcome",
    "ExamStore",
    "InMemoryExamStore",
    "ExamService",
]
