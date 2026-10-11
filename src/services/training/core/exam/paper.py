# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""AI 组卷引擎（FR-TR-007，SR-TR-007 §6.1：薄弱维蓝图 → 确定性校验修复 → 种子化抽题 → 快照）。

三段式（demo 口径，LLM 只出蓝图不直接选题）：
- **蓝图**：:func:`build_blueprint` 由雷达薄弱排序确定性导出节配额
  （quota_d = round(total × w_d)，第 1 薄弱 0.4/0.3/0.2/0.1 配置化，最大余数法补齐）、
  客观/主观配比与难度分布；非法蓝图（配比不守恒/未知维度）拒绝构造；
- **校验修复**：:func:`validate_and_repair` 对 LLM 产出的蓝图逐约束按确定性公式重排
  （分布不守恒/薄弱单调性被破坏均回配方额，不回问 LLM）；
- **抽题与降级**：:class:`PaperGenerator` 以 ``{trainee_id}:{attempt_no}:{paper_version}``
  为种子做确定性抽题（同参数重放可复现，审计口径）；题库不满足蓝图约束时逐级降级：
  规则模板卷（放宽难度与题型）→ 全卷兜底（仅保总量）→ :class:`BankInsufficientError`。

试卷快照（answer/model_answer/rubric 全量固化）保证题库后续改版不影响已开考试与判分复现；
考生视图 :meth:`Paper.questions_view` 剥离答案字段（开考下发面与判分快照分离）。

依据文档：docs/07-实现方案/TR/SR-TR-007-考试认证系统.md §6.1、§6.5、§7、§9。
"""
from __future__ import annotations

import random
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

# --------------------------------------------------------------------------- 常量（SR §6.1/§7 缺省口径）

#: 雷达四维（对齐 FR-TR-004 / services.training.core.radar.engine.DIMENSIONS，只读对齐不反向依赖）
DIMENSIONS: tuple[str, ...] = ("product", "communication", "negotiation", "platform")

#: 客观题型（自动精确判分）/ 主观题型（rubric LLM 评分，demo 为 Fake 桩）
OBJECTIVE_QTYPES: tuple[str, ...] = ("single", "multi", "judge")
SUBJECTIVE_QTYPES: tuple[str, ...] = ("short_answer", "case")

#: 组卷蓝图版本（进种子，蓝图规则升级即换版）
PAPER_VERSION: str = "tr.exam.paper@v1"

#: 薄弱排名 → 题量配额（第 1 薄弱 0.4 / 第 2 0.3 / 第 3 0.2 / 第 4 0.1，SR §7 配置化缺省）
DEFAULT_WEAK_QUOTA: tuple[float, ...] = (0.4, 0.3, 0.2, 0.1)

#: 客观题分占比缺省（SR §7 TR_EXAM_OBJ_SUBJ_RATIO=0.6，余为主观）
DEFAULT_OBJ_RATIO: float = 0.6

#: 全卷总题数缺省（demo 规模；SR §7 生产缺省 40，取值范围 20~80）
DEFAULT_TOTAL_QUESTIONS: int = 20

#: 考试时长（分钟，SR §7 TR_EXAM_DURATION_MIN）
DEFAULT_DURATION_MINUTES: int = 60

#: 难度分布缺省比例：易 0.5 / 中 0.3 / 难 0.2（余数归难档，保证难题位不被丢弃）
DIFFICULTY_RATIO: tuple[float, float, float] = (0.5, 0.3, 0.2)

#: 配比求和校验容差（浮点噪声防护）
_QUOTA_SUM_TOLERANCE: float = 1e-6

#: 难度档位标签（内部键）
_DIFF_KEYS: tuple[str, ...] = ("easy", "mid", "hard")


def is_objective(qtype: str) -> bool:
    """题型归类：single/multi/judge 为客观题（可自动精确判分）。"""
    return qtype in OBJECTIVE_QTYPES


# --------------------------------------------------------------------------- 异常


class BlueprintInvalidError(ValueError):
    """非法蓝图（薄弱配比不守恒 / 未知维度 / 总量与配额矛盾）：拒绝构造，不静默修复。"""


class BankInsufficientError(RuntimeError):
    """题库彻底不足（降级链走尽仍无法成卷）：透传 kb 抽题契约 42201 口径。"""

    biz_code = "E_BANK_INSUFFICIENT"
    numeric_code = 42201
    http_status = 409


# --------------------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class Blueprint:
    """组卷蓝图（LLM 产出的候选形态或确定性公式的重排结果）。

    硬约束在构造期校验：维度必须在四维口径内、各节配额之和等于总题数——
    违反即 :class:`BlueprintInvalidError`（非法蓝图拒绝，SR §9 用例）。
    """

    quotas: dict[str, int]
    """各维（节）题量配额 {dim: count}，之和 == total。"""

    obj_subj: dict[str, tuple[int, int]]
    """各节客观/主观题量 {(obj, subj)}，obj+subj == quota。"""

    difficulty_dist: dict[str, tuple[int, int]]
    """各节难度分布 {(easy, mid, hard)}，三元组和 == quota（可后置修复）。"""

    weak_rank: tuple[str, ...]
    """薄弱维排序（升序：越靠前越薄弱），决定配额权重取值位次。"""

    total: int
    """全卷总题数。"""

    duration_minutes: int = DEFAULT_DURATION_MINUTES
    """考试时长（分钟）。"""

    paper_version: str = PAPER_VERSION
    """蓝图规则版本（进组卷种子）。"""

    def __post_init__(self) -> None:
        unknown = [dim for dim in self.quotas if dim not in DIMENSIONS]
        if unknown:
            raise BlueprintInvalidError(f"未知维度 {unknown}（合法四维：{list(DIMENSIONS)}）")
        if sum(self.quotas.values()) != self.total:
            raise BlueprintInvalidError(
                f"各节配额之和 {sum(self.quotas.values())} != 总题数 {self.total}"
            )
        bad_rank = [dim for dim in self.weak_rank if dim not in DIMENSIONS]
        if bad_rank:
            raise BlueprintInvalidError(f"薄弱序含未知维度 {bad_rank}")


def _largest_remainder(total: int, weights: Sequence[float]) -> list[int]:
    """最大余数法：按权重把 total 拆成非负整数配额（和恰为 total，确定性）。"""
    raw = [total * w for w in weights]
    floors = [int(x) for x in raw]
    remaining = total - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[:remaining]:
        floors[i] += 1
    return floors


def _quota_by_weak_rank(weak_rank: Sequence[str], total: int, weak_quota: Sequence[float]) -> dict[str, int]:
    """薄弱序 + 权重 → 各节配额（quota_d = round(total × w_d)，最大余数法补齐）。"""
    counts = _largest_remainder(total, list(weak_quota))
    return dict(zip(weak_rank, counts))


def _dist_by_formula(quota: int) -> tuple[int, int, int]:
    """节配额 → 难度分布（易/中/难按 0.5/0.3/0.2 公式，余数归难档）。"""
    easy = round(quota * DIFFICULTY_RATIO[0])
    mid = round(quota * DIFFICULTY_RATIO[1])
    hard = quota - easy - mid
    return (easy, mid, hard)


def build_blueprint(
    radar: dict[str, int] | None,
    *,
    total: int = DEFAULT_TOTAL_QUESTIONS,
    weak_quota: Sequence[float] = DEFAULT_WEAK_QUOTA,
    obj_ratio: float = DEFAULT_OBJ_RATIO,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
) -> Blueprint:
    """由雷达薄弱排序构造蓝图（缺雷达 → 中性默认 60 分，按维度名稳定排序）。

    - 薄弱序：按雷达分升序（同分按维度名字典序，保证确定性）；
    - 配额：quota_d = total × weak_quota[位次]，最大余数法取整（和恰为 total）；
    - 客观/主观：obj = round(quota × obj_ratio)，subj 补齐；
    - 非法入参（薄弱配比和≠1 / 未知维度）→ :class:`BlueprintInvalidError`。
    """
    if len(weak_quota) != len(DIMENSIONS) or abs(sum(weak_quota) - 1.0) > _QUOTA_SUM_TOLERANCE:
        raise BlueprintInvalidError(f"薄弱配比之和必须为 1（实得 {sum(weak_quota)}）")
    if not 0.0 < obj_ratio < 1.0:
        raise BlueprintInvalidError(f"客观题占比须在 (0,1) 内（实得 {obj_ratio}）")
    scores = {dim: 60 for dim in DIMENSIONS} if radar is None else {
        dim: int(radar.get(dim, 60)) for dim in DIMENSIONS
    }
    weak_rank = tuple(sorted(DIMENSIONS, key=lambda d: (scores[d], d)))
    quotas = _quota_by_weak_rank(weak_rank, total, weak_quota)
    obj_subj = {dim: (round(q * obj_ratio), q - round(q * obj_ratio)) for dim, q in quotas.items()}
    difficulty_dist = {dim: _dist_by_formula(q) for dim, q in quotas.items()}
    return Blueprint(
        quotas=quotas,
        obj_subj=obj_subj,
        difficulty_dist=difficulty_dist,
        weak_rank=weak_rank,
        total=total,
        duration_minutes=duration_minutes,
    )


def validate_and_repair(
    bp: Blueprint,
    *,
    total: int | None = None,
    weak_quota: Sequence[float] = DEFAULT_WEAK_QUOTA,
) -> Blueprint:
    """蓝图确定性校验修复（SR §6.1-a/c）：违反约束不回问 LLM，按公式重排。

    - 配额与薄弱单调性：无论是否被打乱，一律按 weak_rank × weak_quota 重排
      （幂等：合法蓝图重排结果与原值一致）；total 可显式指定（缺省取蓝图原值）；
    - 难度分布 / 客观主观拆分：按确定性公式重导出（分布不守恒即被修复）。
    """
    if len(weak_quota) != len(DIMENSIONS) or abs(sum(weak_quota) - 1.0) > _QUOTA_SUM_TOLERANCE:
        raise BlueprintInvalidError(f"薄弱配比之和必须为 1（实得 {sum(weak_quota)}）")
    target_total = bp.total if total is None else total
    quotas = _quota_by_weak_rank(bp.weak_rank, target_total, weak_quota)
    obj_subj = {dim: (round(q * DEFAULT_OBJ_RATIO), q - round(q * DEFAULT_OBJ_RATIO))
                for dim, q in quotas.items()}
    difficulty_dist = {dim: _dist_by_formula(q) for dim, q in quotas.items()}
    return Blueprint(
        quotas=quotas,
        obj_subj=obj_subj,
        difficulty_dist=difficulty_dist,
        weak_rank=bp.weak_rank,
        total=target_total,
        duration_minutes=bp.duration_minutes,
        paper_version=bp.paper_version,
    )


# --------------------------------------------------------------------------- 题库


@dataclass(frozen=True)
class QuestionBankItem:
    """题库条目（FR-KB-006 question_bank_item 的 demo 内存形态，字段同名同义）。"""

    question_id: str
    """题目唯一编号（去重排除与判分定位键）。"""

    dim: str
    """所属雷达维度（四维口径）。"""

    qtype: str
    """题型：single/multi/judge（客观）或 short_answer/case（主观）。"""

    difficulty: int
    """难度档 1 易 / 2 中 / 3 难。"""

    stem: str
    """题干。"""

    answer: str | tuple[str, ...]
    """标准答案：客观题为选项值（multi 为选项元组）；主观题为参考答案文本。"""

    score: int
    """题分（主观题即满分锚点）。"""

    options: tuple[str, ...] = ()
    """选项（客观题）。"""

    model_answer: str = ""
    """主观题参考答案（判分依据，考生视图剥离）。"""

    rubric: tuple[str, ...] = ()
    """主观题评分要点关键词（Fake rubric 口径；生产为给分锚点结构）。"""

    kp: tuple[str, ...] = ()
    """知识点标注（FR-TR-008 考试错题归因的聚合键来源）。"""

    def to_dict(self) -> dict[str, Any]:
        """完整快照形态（含答案与 rubric，入试卷快照供判分复现）。"""
        return {
            "question_id": self.question_id,
            "dim": self.dim,
            "qtype": self.qtype,
            "difficulty": self.difficulty,
            "stem": self.stem,
            "options": list(self.options),
            "answer": list(self.answer) if isinstance(self.answer, tuple) else self.answer,
            "score": self.score,
            "model_answer": self.model_answer,
            "rubric": list(self.rubric),
            "kp": list(self.kp),
        }

    def view_dict(self) -> dict[str, Any]:
        """考生视图（剥离 answer/model_answer/rubric：开考下发面）。"""
        return {
            "question_id": self.question_id,
            "dim": self.dim,
            "qtype": self.qtype,
            "difficulty": self.difficulty,
            "stem": self.stem,
            "options": list(self.options),
            "score": self.score,
        }


class QuestionBank(Protocol):
    """题库协议（生产为 kb-service `/questions/select` 契约适配器，协议不变）。"""

    @property
    def items(self) -> Sequence[QuestionBankItem]:
        """全量题库条目（组卷引擎据此做约束抽题）。"""
        ...


class InMemoryQuestionBank:
    """内存题库（demo/测试）：条目元组不可变快照。"""

    def __init__(self, items: Sequence[QuestionBankItem]) -> None:
        self._items: tuple[QuestionBankItem, ...] = tuple(items)

    @property
    def items(self) -> tuple[QuestionBankItem, ...]:
        """题库条目快照。"""
        return self._items


def build_demo_questions(per_dim_obj: int = 11, per_dim_subj: int = 4) -> list[QuestionBankItem]:
    """确定性 demo 题库：每维客观 per_dim_obj 题（易/中/难 ≈ 4/4/3）+ 主观 per_dim_subj 题。

    - 客观答案分布确定性（单选 A/C 交替、判断对错交替、多选两组交替）；
    - 主观题 model_answer 内嵌全部 rubric 关键词（全对作答口径可复现）；
    - question_id 全库唯一（维度前缀 + 序号）。
    """
    items: list[QuestionBankItem] = []
    for dim in DIMENSIONS:
        seq = 0
        # 客观题：难度配比 易 max(1, round(n*0.36)) / 中 max(1, round(n*0.36)) / 难 余量
        easy = max(1, round(per_dim_obj * 0.36))
        mid = max(1, round(per_dim_obj * 0.36))
        hard = max(0, per_dim_obj - easy - mid)
        obj_diffs = [1] * easy + [2] * mid + [3] * hard
        for i, diff in enumerate(obj_diffs):
            seq += 1
            qtype = ("single", "judge", "multi")[i % 3]
            if qtype == "single":
                answer = "A" if i % 2 == 0 else "C"
                items.append(QuestionBankItem(
                    question_id=f"{dim[:2].upper()}-SIN-{seq:03d}", dim=dim, qtype="single",
                    difficulty=diff, stem=f"{dim} 维度单选题 {seq}：{dim} 知识点 {diff} 级考核",
                    answer=answer, score=2, options=("A", "B", "C", "D"),
                ))
            elif qtype == "judge":
                items.append(QuestionBankItem(
                    question_id=f"{dim[:2].upper()}-JUD-{seq:03d}", dim=dim, qtype="judge",
                    difficulty=diff, stem=f"{dim} 维度判断题 {seq}：陈述句正误判定（{diff} 级）",
                    answer="对" if i % 2 == 0 else "错", score=2, options=("对", "错"),
                ))
            else:
                answer = ("A", "C") if i % 2 == 0 else ("B", "D")
                items.append(QuestionBankItem(
                    question_id=f"{dim[:2].upper()}-MUL-{seq:03d}", dim=dim, qtype="multi",
                    difficulty=diff, stem=f"{dim} 维度多选题 {seq}：多选组合判定（{diff} 级）",
                    answer=answer, score=2, options=("A", "B", "C", "D"),
                ))
        # 主观题：难度配比 易 1 / 中 2 / 难 1（n<4 时按序截取）
        subj_diffs = ([1, 2, 2, 3] if per_dim_subj >= 4 else [1, 2, 3][:per_dim_subj])
        rubrics = (("报价", "交期"), ("认证", "产能"), ("付款", "物流"), ("售后", "质保"))
        for i, diff in enumerate(subj_diffs):
            seq += 1
            rubric = rubrics[i % len(rubrics)]
            model = f"参考答复：明确{rubric[0]}口径与{rubric[1]}承诺，并给出书面确认路径。"
            items.append(QuestionBankItem(
                question_id=f"{dim[:2].upper()}-CAS-{seq:03d}", dim=dim,
                qtype="case" if i == per_dim_subj - 1 else "short_answer",
                difficulty=diff, stem=f"{dim} 维度主观题 {seq}： scenario {diff} 级情景作答",
                answer=model, score=25, model_answer=model, rubric=rubric,
                kp=(f"{dim}-kp-{i + 1}",),
            ))
    return items


# --------------------------------------------------------------------------- 试卷与组卷引擎


@dataclass(frozen=True)
class Paper:
    """试卷快照（题库条目整体固化：题库改版不影响本场判分复现，SR §2）。"""

    seed: str
    """组卷种子（{trainee_id}:{attempt_no}:{paper_version}，审计复现键）。"""

    blueprint: Blueprint
    """本次组卷使用的蓝图（约束口径留痕）。"""

    questions: tuple[QuestionBankItem, ...]
    """快照题目序列（含答案与 rubric）。"""

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    """组卷时间。"""

    degraded: bool = False
    """是否降级成卷（题库不满足蓝图约束）。"""

    degrade_reason: str = ""
    """降级原因（degraded 时非空，审计口径）。"""

    def questions_view(self) -> list[dict[str, Any]]:
        """考生视图列表（剥离答案字段）。"""
        return [q.view_dict() for q in self.questions]

    def question_ids(self) -> set[str]:
        """题目编号集合（重考去重 exclude 口径）。"""
        return {q.question_id for q in self.questions}


class PaperGenerator:
    """组卷引擎：蓝图 → 约束抽题 → 逐级降级（SR §6.1 三段式 + SR-KB-006 降级链口径）。"""

    def __init__(
        self,
        bank: QuestionBank,
        *,
        total: int = DEFAULT_TOTAL_QUESTIONS,
        weak_quota: Sequence[float] = DEFAULT_WEAK_QUOTA,
        obj_ratio: float = DEFAULT_OBJ_RATIO,
        duration_minutes: int = DEFAULT_DURATION_MINUTES,
    ) -> None:
        self._bank = bank
        self._total = total
        self._weak_quota = tuple(weak_quota)
        self._obj_ratio = obj_ratio
        self._duration = duration_minutes

    def generate(
        self,
        trainee_id: int,
        attempt_no: int,
        *,
        radar: dict[str, int] | None = None,
        exclude: Collection[str] = (),
    ) -> Paper:
        """组卷：确定性种子抽题；约束不满足逐级降级，走尽仍不足则报错。"""
        seed = f"{trainee_id}:{attempt_no}:{PAPER_VERSION}"
        bp = build_blueprint(
            radar, total=self._total, weak_quota=self._weak_quota,
            obj_ratio=self._obj_ratio, duration_minutes=self._duration,
        )
        selected = self._select_by_blueprint(bp, exclude, seed)
        if selected is not None:
            return Paper(seed=seed, blueprint=bp, questions=tuple(selected))
        # 降级①：规则模板卷——保留节配额，放宽难度与题型约束
        template = self._select_relaxed(bp, exclude, seed, by_dim=True)
        if template is not None:
            return Paper(
                seed=seed, blueprint=bp, questions=tuple(template), degraded=True,
                degrade_reason="题库不满足蓝图难度/题型约束，降级规则模板卷（保留节配额）",
            )
        # 降级②：全卷兜底——仅保总题数
        fallback = self._select_relaxed(bp, exclude, seed, by_dim=False)
        if fallback is not None:
            return Paper(
                seed=seed, blueprint=bp, questions=tuple(fallback), degraded=True,
                degrade_reason="题库节配额亦不足，降级全卷兜底组卷（仅保总量）",
            )
        raise BankInsufficientError(
            f"题库可用题目不足 {bp.total} 题，无法成卷（E_BANK_INSUFFICIENT/42201）"
        )

    # ── 约束抽题 ────────────────────────────────────────────────────────────
    def _select_by_blueprint(
        self, bp: Blueprint, exclude: Collection[str], seed: str
    ) -> list[QuestionBankItem] | None:
        """按蓝图节配额 + 客观/主观拆分 + 难度分布确定性抽题；任一约束不可满足 → None。"""
        rng = random.Random(seed)
        picked: list[QuestionBankItem] = []
        for dim in bp.weak_rank:
            quota = bp.quotas[dim]
            obj_need, subj_need = bp.obj_subj[dim]
            easy, mid, hard = bp.difficulty_dist[dim]
            pool = [
                q for q in self._bank.items
                if q.dim == dim and q.question_id not in exclude
            ]
            dist_left = {"easy": easy, "mid": mid, "hard": hard}
            subj = self._take([q for q in pool if not is_objective(q.qtype)], subj_need, dist_left, rng)
            obj = self._take([q for q in pool if is_objective(q.qtype)], obj_need, dist_left, rng)
            if len(subj) < subj_need or len(obj) < obj_need or sum(dist_left.values()) != 0:
                return None  # 任一节不满足即整卷降级（约束完整性优先）
            picked.extend(subj)
            picked.extend(obj)
        return picked

    @staticmethod
    def _take(
        pool: list[QuestionBankItem],
        need: int,
        dist_left: dict[str, int],
        rng: random.Random,
    ) -> list[QuestionBankItem]:
        """从题池按剩余难度配额取题（自难档起取，确定性采样），返回已取清单。"""
        taken: list[QuestionBankItem] = []
        for diff, key in ((3, "hard"), (2, "mid"), (1, "easy")):
            bucket = sorted((q for q in pool if q.difficulty == diff), key=lambda q: q.question_id)
            take = min(need - len(taken), dist_left[key], len(bucket))
            if take > 0:
                taken.extend(rng.sample(bucket, take))
                dist_left[key] -= take
        return taken

    # ── 降级抽题 ────────────────────────────────────────────────────────────
    def _select_relaxed(
        self, bp: Blueprint, exclude: Collection[str], seed: str, *, by_dim: bool
    ) -> list[QuestionBankItem] | None:
        """放宽抽题：by_dim=True 保留节配额（模板卷）；False 仅保总量（兜底卷）。"""
        rng = random.Random(f"{seed}:relaxed:{by_dim}")
        pool = [q for q in self._bank.items if q.question_id not in exclude]
        picked: list[QuestionBankItem] = []
        if by_dim:
            for dim in bp.weak_rank:
                bucket = sorted((q for q in pool if q.dim == dim), key=lambda q: q.question_id)
                if len(bucket) < bp.quotas[dim]:
                    return None
                picked.extend(rng.sample(bucket, bp.quotas[dim]))
            return picked
        if len(pool) < bp.total:
            return None
        return rng.sample(sorted(pool, key=lambda q: q.question_id), bp.total)


__all__ = [
    "DIMENSIONS",
    "OBJECTIVE_QTYPES",
    "SUBJECTIVE_QTYPES",
    "PAPER_VERSION",
    "DEFAULT_WEAK_QUOTA",
    "DEFAULT_OBJ_RATIO",
    "DEFAULT_TOTAL_QUESTIONS",
    "DEFAULT_DURATION_MINUTES",
    "is_objective",
    "BlueprintInvalidError",
    "BankInsufficientError",
    "Blueprint",
    "build_blueprint",
    "validate_and_repair",
    "QuestionBankItem",
    "QuestionBank",
    "InMemoryQuestionBank",
    "build_demo_questions",
    "Paper",
    "PaperGenerator",
]
