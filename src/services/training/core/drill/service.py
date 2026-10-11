# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""实战演练服务层（FR-TR-003，SR-TR-003 §6.2 评分编排 / §6.3 一致性校准回路 / §8 可靠性）。

- :class:`DrillService.submit`——作答提交：入参校验（TR-1002）→ 案例可用性
  （TR-1001）→ 幂等重放（同 (trainee_id, request_id) 以首次提交为准，恰一次评分）
  → 评分（LLM 失败降级 status=pending_manual，不报错、不产出无依据分数，决策 D4）
  → 差距分析（vs excellent_reply 缺失要点清单）；
- :meth:`DrillService.record_supervisor_score`——主管校准采样：AI 评分 vs 主管评分
  逐维对比，判定口径（SR §6.3/§7）：各维差 ≤15 且总分差 ≤10 判一致；
- :meth:`DrillService.calibration_report`——滚动一致率（SRS §16.2 KPI ≥85%），
  一致率 <85% 置 frozen 冻结标志 → :meth:`assign` 拒绝新投题（TR-2002，存量评分不受影响）；
- 会话隔离（NFR-SEC-02）：学员仅本人演练数据可见，他人视角视同不存在。

并发口径：demo 单进程以进程内锁实现评分幂等（生产为 SR §4 Redis 幂等锁
``tr:drill:score:lock:{exercise_id}``，语义一致：at-least-once 投递下不重复计分）。

依据文档：docs/07-实现方案/TR/SR-TR-003-实战演练系统.md §6.2、§6.3、§7、§8、§9。
"""
from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from services.training.core.drill.casebank import (
    DRILL_DIMS,
    DrillCase,
    DrillCaseBank,
    ImportReport,
)
from services.training.core.drill.scoring import (
    DimScore,
    DrillScorer,
    GapAnalysis,
    ScoreOutput,
    ScoringError,
    build_gap_analysis,
)
from services.training.core.drill.store import DrillStore

# --------------------------------------------------------------------------- 配置常量（SR §7 缺省值）

#: 作答最大长度（字符；超出 → TR-1002）
MAX_ANSWER_LENGTH: int = 2000
#: 一致判定：总分差阈值（SR §7 tr.drill.consistency.total_tolerance）
DEFAULT_TOTAL_TOLERANCE: int = 10
#: 一致判定：单维差阈值（SR §7 tr.drill.consistency.dim_tolerance）
DEFAULT_DIM_TOLERANCE: int = 15
#: 一致率冻结阈值（SRS §16.2 KPI ≥85%）
DEFAULT_CONSISTENCY_THRESHOLD: float = 85.0

# --------------------------------------------------------------------------- 业务异常（TR-xxxx 码，SR §5）


class DrillError(RuntimeError):
    """演练业务异常基类：携带 SR-TR-003 §5 业务码与 HTTP 状态（API 层映射信封）。"""

    biz_code: str = "TR-0000"
    http_status: int = 400


class AnswerInvalidError(DrillError):
    """作答为空/超长（TR-1002）。"""

    biz_code = "TR-1002"
    http_status = 400


class CaseNotAvailableError(DrillError):
    """案例不存在或未发布，不可投题（TR-1001）。"""

    biz_code = "TR-1001"
    http_status = 404


class CalibrationFrozenError(DrillError):
    """评分一致率低于阈值，冻结新投题（TR-2002）。"""

    biz_code = "TR-2002"
    http_status = 409


class ResultNotFoundError(DrillError):
    """评分结果不存在（或非本人数据，TR-2003）。"""

    biz_code = "TR-2003"
    http_status = 404


class ResultStateError(DrillError):
    """结果状态不允许该操作（如待人工评分时做校准，TR-2004）。"""

    biz_code = "TR-2004"
    http_status = 409


# --------------------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class DrillResult:
    """演练评分结果：三维分 + 总分 + 依据 + 差距分析；降级时 status=pending_manual。"""

    result_id: str
    request_id: str
    case_id: str
    trainee_id: int
    answer: str
    status: str  # scored | pending_manual
    dims: tuple[DimScore, ...]
    total: int | None
    comment: str
    gap_analysis: GapAnalysis | None
    scoring_error: str  # 降级原因（pending_manual 时非空，scored 时为空串）

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "request_id": self.request_id,
            "case_id": self.case_id,
            "trainee_id": self.trainee_id,
            "answer": self.answer,
            "status": self.status,
            "dims": [dim.to_dict() for dim in self.dims],
            "total": self.total,
            "comment": self.comment,
            "gap_analysis": self.gap_analysis.to_dict() if self.gap_analysis is not None else None,
            "scoring_error": self.scoring_error,
        }


@dataclass(frozen=True)
class CalibrationSample:
    """校准采样：AI 评分快照 vs 主管评分，含一致判定与偏差明细（回流 rubric/Prompt 优化）。"""

    calibration_id: str
    result_id: str
    supervisor: str
    ai_scores: dict[str, int]
    supervisor_scores: dict[str, int]
    consistent: bool
    deviation: dict[str, int]
    total_deviation: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "calibration_id": self.calibration_id,
            "result_id": self.result_id,
            "supervisor": self.supervisor,
            "ai_scores": dict(self.ai_scores),
            "supervisor_scores": dict(self.supervisor_scores),
            "consistent": self.consistent,
            "deviation": dict(self.deviation),
            "total_deviation": self.total_deviation,
        }


@dataclass(frozen=True)
class CalibrationReport:
    """一致率报表（SRS §16.2 KPI 证据）：滚动一致率 + 冻结标志。"""

    samples: int
    consistent: int
    consistency_rate: float | None  # 无样本时不定义（None，不冻结）
    threshold: float
    frozen: bool
    total_tolerance: int
    dim_tolerance: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "consistent": self.consistent,
            "consistency_rate": self.consistency_rate,
            "threshold": self.threshold,
            "frozen": self.frozen,
            "total_tolerance": self.total_tolerance,
            "dim_tolerance": self.dim_tolerance,
        }


# --------------------------------------------------------------------------- 服务


class DrillService:
    """实战演练应用服务：作答提交 → 评分/降级 → 差距分析 → 一致性校准回路。"""

    def __init__(
        self,
        bank: DrillCaseBank,
        store: DrillStore,
        scorer: DrillScorer,
        *,
        total_tolerance: int = DEFAULT_TOTAL_TOLERANCE,
        dim_tolerance: int = DEFAULT_DIM_TOLERANCE,
        threshold: float = DEFAULT_CONSISTENCY_THRESHOLD,
    ) -> None:
        self._bank = bank
        self._store = store
        self._scorer = scorer
        self._total_tolerance = total_tolerance
        self._dim_tolerance = dim_tolerance
        self._threshold = threshold
        # demo 单进程评分幂等锁（生产为 Redis lock，SR §4；语义：重放不重复计分）
        self._submit_lock = threading.Lock()

    # ── 案例批量导入 ────────────────────────────────────────────────────────
    def import_cases(self, cases: Sequence[DrillCase]) -> ImportReport:
        """批量导入案例（透传案例库脱敏闸门：PII 命中拒绝入台账，不整批失败）。"""
        return self._bank.import_cases(cases)

    # ── 作答提交 ────────────────────────────────────────────────────────────
    def submit(self, *, case_id: str, answer: str, trainee_id: int, request_id: str) -> DrillResult:
        """提交作答并评分（幂等）：重放同 (trainee_id, request_id) 返回首次结果。"""
        if not isinstance(answer, str) or not answer.strip():
            raise AnswerInvalidError("作答不能为空（TR-1002）")
        if len(answer) > MAX_ANSWER_LENGTH:
            raise AnswerInvalidError(f"作答超长：{len(answer)} > {MAX_ANSWER_LENGTH}（TR-1002）")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id 不能为空（幂等键必填）")
        if not isinstance(trainee_id, int) or isinstance(trainee_id, bool) or trainee_id < 1:
            raise ValueError("trainee_id 必须为正整数")
        case = self._bank.get_case(case_id)
        if case is None or case.status != "published":
            raise CaseNotAvailableError(f"案例不存在或未发布，不可投题：{case_id}（TR-1001）")
        with self._submit_lock:  # 幂等临界区（生产为分布式锁，SR §4）
            replayed = self._store.find_result_by_request(trainee_id, request_id)
            if replayed is not None:
                return replayed
            result = self._score_or_degrade(case, answer, trainee_id=trainee_id, request_id=request_id)
            self._store.save_result(result)
            return result

    def _score_or_degrade(
        self, case: DrillCase, answer: str, *, trainee_id: int, request_id: str
    ) -> DrillResult:
        """评分，LLM 失败降级 pending_manual（TR-2001）：不报错、不产出无依据分数。"""
        result_id = self._store.next_result_id()
        try:
            output: ScoreOutput = self._scorer.score(case, answer)
        except ScoringError as exc:
            return DrillResult(
                result_id=result_id,
                request_id=request_id,
                case_id=case.case_id,
                trainee_id=trainee_id,
                answer=answer,
                status="pending_manual",
                dims=(),
                total=None,
                comment="",
                gap_analysis=None,
                scoring_error=f"评分降级待人工（TR-2001）：{exc}",
            )
        return DrillResult(
            result_id=result_id,
            request_id=request_id,
            case_id=case.case_id,
            trainee_id=trainee_id,
            answer=answer,
            status="scored",
            dims=output.dims,
            total=output.total,
            comment=output.comment,
            gap_analysis=build_gap_analysis(case, answer),
            scoring_error="",
        )

    # ── 结果查询（会话隔离，NFR-SEC-02）────────────────────────────────────
    def get_result(self, result_id: str, *, trainee_id: int | None = None) -> DrillResult:
        """按 id 取结果；携带 trainee_id 时校验归属（他人数据视同不存在）。"""
        result = self._store.get_result(result_id)
        if result is None or (trainee_id is not None and result.trainee_id != trainee_id):
            raise ResultNotFoundError(f"评分结果不存在：{result_id}（TR-2003）")
        return result

    def list_results(self, trainee_id: int) -> list[DrillResult]:
        """本人演练结果清单（仅本人数据）。"""
        return self._store.list_results(trainee_id)

    # ── 一致性校准回路（SR §6.3）──────────────────────────────────────────
    def record_supervisor_score(
        self, *, result_id: str, supervisor: str, scores: dict[str, int]
    ) -> CalibrationSample:
        """主管校准评分：与 AI 评分逐维对比 → 一致判定 → 采样入台账。"""
        result = self._store.get_result(result_id)
        if result is None:
            raise ResultNotFoundError(f"评分结果不存在：{result_id}（TR-2003）")
        if result.status != "scored" or result.total is None:
            raise ResultStateError("结果待人工评分，无 AI 评分快照可校准（TR-2004）")
        validated = self._validate_scores(scores)
        deviation = {dim: abs(validated[dim] - self._ai_score(result, dim)) for dim in DRILL_DIMS}
        supervisor_total = sum(validated.values()) / len(DRILL_DIMS)
        total_deviation = round(abs(supervisor_total - result.total), 1)
        consistent = (
            max(deviation.values()) <= self._dim_tolerance
            and total_deviation <= self._total_tolerance
        )
        sample = CalibrationSample(
            calibration_id=self._store.next_calibration_id(),
            result_id=result_id,
            supervisor=supervisor,
            ai_scores={dim: self._ai_score(result, dim) for dim in DRILL_DIMS},
            supervisor_scores=dict(validated),
            consistent=consistent,
            deviation=deviation,
            total_deviation=total_deviation,
        )
        self._store.save_calibration(sample)
        return sample

    @staticmethod
    def _ai_score(result: DrillResult, dim: str) -> int:
        """取结果中指定维度的 AI 分（scored 结果三维必齐）。"""
        for dim_score in result.dims:
            if dim_score.dim == dim:
                return dim_score.score
        raise ResultStateError(f"结果缺少维度评分：{dim}（TR-2004）")

    def _validate_scores(self, scores: dict[str, int]) -> dict[str, int]:
        """主管分值校验：三维齐备、0-100；非法即 ValueError（API 层映射 40001）。"""
        if not isinstance(scores, dict) or set(scores) != set(DRILL_DIMS):
            raise ValueError(f"主管评分维度必须齐备：{sorted(DRILL_DIMS)}")
        validated: dict[str, int] = {}
        for dim, value in scores.items():
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
                raise ValueError(f"维度 {dim} 分值非法（须为 0-100 整数）：{value!r}")
            validated[dim] = value
        return validated

    def calibration_report(self) -> CalibrationReport:
        """滚动一致率报表：一致率 <85% 置冻结标志（§16.2 KPI；SR §6.3 降级口径）。"""
        samples = self._store.list_calibrations()
        consistent = sum(1 for sample in samples if sample.consistent)
        if samples:
            rate = round(consistent / len(samples) * 100, 1)
            frozen = rate < self._threshold
        else:
            rate, frozen = None, False  # 无样本不定义一致率，不做保守降级
        return CalibrationReport(
            samples=len(samples),
            consistent=consistent,
            consistency_rate=rate,
            threshold=self._threshold,
            frozen=frozen,
            total_tolerance=self._total_tolerance,
            dim_tolerance=self._dim_tolerance,
        )

    def is_frozen(self) -> bool:
        """当前是否处于「一致率 <85% 冻结投题」状态。"""
        return self.calibration_report().frozen

    # ── 投题 ────────────────────────────────────────────────────────────────
    def assign(self, *, page: int = 1, page_size: int = 10, weak_dim: str | None = None) -> dict[str, Any]:
        """分页投题：先校准冻结闸门（TR-2002），再案例库启用闸门与分页/薄弱维过滤。"""
        if self.is_frozen():
            raise CalibrationFrozenError(
                f"评分一致率低于 {self._threshold}%，已冻结新投题直至完成一轮 rubric/Prompt 优化（TR-2002）"
            )
        return self._bank.pick(page=page, page_size=page_size, weak_dim=weak_dim)


__all__ = [
    "MAX_ANSWER_LENGTH",
    "DEFAULT_TOTAL_TOLERANCE",
    "DEFAULT_DIM_TOLERANCE",
    "DEFAULT_CONSISTENCY_THRESHOLD",
    "DrillError",
    "AnswerInvalidError",
    "CaseNotAvailableError",
    "CalibrationFrozenError",
    "ResultNotFoundError",
    "ResultStateError",
    "DrillResult",
    "CalibrationSample",
    "CalibrationReport",
    "DrillService",
]
