# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""实战演练服务层测试（FR-TR-003，SR-TR-003 §6.2/§6.3/§8；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类场景）：
- 等价类：作答提交主链路（submit→评分→差距分析）、校准判定（一致/偏差）；
- 边界：作答长度 2000/2001、维差 15/16、总分差恰 10、一致率阈值 85%；
- 异常：空作答/超长（TR-1002）、案例不可投（TR-1001）、结果缺失（TR-2003）、
  待人工结果不可校准（TR-2004）、评分分值非法；
- 并发：同 request_id 并发重放幂等（恰一次评分）、同 case 并发独立提交、会话隔离；
- 降级：LLM 评分失败 → status=pending_manual（不报错、不产出无依据分数），
  一致率 <85% 冻结投题（TR-2002）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest

from services.training.core.drill.casebank import (
    DIM_LABELS,
    DIM_PRODUCT,
    DIM_SCRIPT,
    DIM_STRATEGY,
    DrillCase,
    DrillCaseBank,
    build_demo_cases,
)
from services.training.core.drill.scoring import (
    DimScore,
    FakeDrillScorer,
    ScoreOutput,
    ScoringError,
)
from services.training.core.drill.service import (
    MAX_ANSWER_LENGTH,
    AnswerInvalidError,
    CalibrationFrozenError,
    CaseNotAvailableError,
    DrillService,
    ResultNotFoundError,
    ResultStateError,
)
from services.training.core.drill.store import InMemoryDrillStore

CASES = build_demo_cases(100)


def make_bank(min_published: int = 100, cases: int = 100) -> DrillCaseBank:
    from services.kb.core.pipeline.cleaning.masking import MaskingEngine, default_ruleset

    bank = DrillCaseBank(MaskingEngine(default_ruleset()), min_published=min_published)
    bank.import_cases(CASES[:cases])
    return bank


def make_service(
    scorer: Any | None = None,
    *,
    min_published: int = 100,
    cases: int = 100,
    **kwargs: Any,
) -> DrillService:
    return DrillService(
        make_bank(min_published=min_published, cases=cases),
        InMemoryDrillStore(),
        scorer if scorer is not None else FakeDrillScorer(),
        **kwargs,
    )


class StubScorer:
    """定分桩：恒返回 product=50 / script=60 / strategy=70 / total=60（校准判定基座）。"""

    calls = 0

    def score(self, case: DrillCase, answer: str) -> ScoreOutput:
        self.calls += 1
        dims = tuple(
            DimScore(dim=dim, label=DIM_LABELS[dim], score=score, evidence=(), missed_points=())
            for dim, score in ((DIM_PRODUCT, 50), (DIM_SCRIPT, 60), (DIM_STRATEGY, 70))
        )
        return ScoreOutput(dims=dims, total=60, comment="定分桩")


class BrokenScorer:
    """故障桩：恒抛 ScoringError（降级路径基座）。"""

    calls = 0

    def score(self, case: DrillCase, answer: str) -> ScoreOutput:
        self.calls += 1
        raise ScoringError("模拟 LLM 评分超时")


def sup_scores(product: int, script: int, strategy: int) -> dict[str, int]:
    return {DIM_PRODUCT: product, DIM_SCRIPT: script, DIM_STRATEGY: strategy}


class TestSubmitEquivalence:
    """等价类：作答提交主链路（幂等键 → 校验 → 评分 → 差距分析 → 落库）。"""

    def test_submit_scored_happy(self) -> None:
        service = make_service()
        case = CASES[0]
        result = service.submit(
            case_id=case.case_id, answer=case.excellent_reply, trainee_id=1, request_id="req-1"
        )
        assert result.status == "scored"
        assert [d.dim for d in result.dims] == [DIM_PRODUCT, DIM_SCRIPT, DIM_STRATEGY]
        assert result.total == 100
        assert result.gap_analysis is not None and result.gap_analysis.missing_points == ()
        assert all(dim.evidence for dim in result.dims)  # 逐维携带原文依据
        # 落库回环 + 本人列表可见
        assert service.get_result(result.result_id).result_id == result.result_id
        assert [r.result_id for r in service.list_results(1)] == [result.result_id]

    def test_submit_unknown_case_tr1001(self) -> None:
        service = make_service()
        with pytest.raises(CaseNotAvailableError) as excinfo:
            service.submit(case_id="DR-2026-9999", answer="收到。", trainee_id=1, request_id="r")
        assert excinfo.value.biz_code == "TR-1001"

    def test_submit_unpublished_case_tr1001(self) -> None:
        """未发布（draft）案例不可投题（SR §6.1 状态机口径）。"""
        bank = make_bank(min_published=1, cases=0)  # 空 bank，避免与预导入案例撞号
        draft = replace(CASES[0], status="draft")
        bank.add_case(draft)
        service = DrillService(bank, InMemoryDrillStore(), FakeDrillScorer())
        with pytest.raises(CaseNotAvailableError) as excinfo:
            service.submit(case_id=draft.case_id, answer="收到。", trainee_id=1, request_id="r")
        assert excinfo.value.biz_code == "TR-1001"

    def test_request_id_required(self) -> None:
        service = make_service()
        with pytest.raises(ValueError):
            service.submit(case_id=CASES[0].case_id, answer="收到。", trainee_id=1, request_id="  ")


class TestAnswerValidation:
    """边界/异常：作答校验（TR-1002：为空/超长）。"""

    @pytest.mark.parametrize("answer", ["", "   \n\t "])
    def test_empty_answer_rejected(self, answer: str) -> None:
        service = make_service()
        with pytest.raises(AnswerInvalidError) as excinfo:
            service.submit(case_id=CASES[0].case_id, answer=answer, trainee_id=1, request_id="r")
        assert excinfo.value.biz_code == "TR-1002"

    def test_overlong_answer_rejected(self) -> None:
        service = make_service()
        with pytest.raises(AnswerInvalidError):
            service.submit(
                case_id=CASES[0].case_id, answer="好" * (MAX_ANSWER_LENGTH + 1),
                trainee_id=1, request_id="r",
            )

    def test_answer_at_limit_ok(self) -> None:
        """边界：恰为 2000 字合法（零覆盖 → 0 分但正常评分入库）。"""
        service = make_service()
        result = service.submit(
            case_id=CASES[0].case_id, answer="好" * MAX_ANSWER_LENGTH,
            trainee_id=1, request_id="r",
        )
        assert result.status == "scored"
        assert result.total == 0


class TestIdempotency:
    """并发：幂等重放（同 (trainee, request_id) 恰一次评分，SR §8 可靠性口径）。"""

    def test_replay_same_request_same_result(self) -> None:
        scorer = FakeDrillScorer()
        service = make_service(scorer)
        case = CASES[0]
        first = service.submit(
            case_id=case.case_id, answer=case.excellent_reply, trainee_id=1, request_id="req-x"
        )
        replay = service.submit(
            case_id=case.case_id, answer=case.excellent_reply, trainee_id=1, request_id="req-x"
        )
        assert replay.result_id == first.result_id
        assert scorer.calls == 1  # 重放不再评分

    def test_replay_returns_first_answer(self) -> None:
        service = make_service()
        case = CASES[0]
        first = service.submit(
            case_id=case.case_id, answer=case.excellent_reply, trainee_id=1, request_id="req-x"
        )
        replay = service.submit(
            case_id=case.case_id, answer="换一个作答也不重算", trainee_id=1, request_id="req-x"
        )
        assert replay.answer == first.answer  # 以首次提交为准

    def test_same_request_id_different_trainee_isolated(self) -> None:
        """会话隔离：同 request_id 不同学员互相独立（幂等键作用域为本人）。"""
        scorer = FakeDrillScorer()
        service = make_service(scorer)
        case = CASES[0]
        a = service.submit(case_id=case.case_id, answer="收到。", trainee_id=1, request_id="req-x")
        b = service.submit(case_id=case.case_id, answer="收到。", trainee_id=2, request_id="req-x")
        assert a.result_id != b.result_id
        assert scorer.calls == 2


class TestConcurrency:
    """并发：多线程提交同 case（内存存储线程安全 + 幂等锁语义，SR §4 幂等锁）。"""

    def test_concurrent_same_request_single_scoring(self) -> None:
        scorer = FakeDrillScorer()
        service = make_service(scorer)
        case = CASES[0]

        def submit(_: int) -> Any:
            return service.submit(
                case_id=case.case_id, answer=case.excellent_reply, trainee_id=1, request_id="req-x"
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        assert len({r.result_id for r in results}) == 1  # 幂等重放收敛同一结果
        assert scorer.calls == 1  # 恰一次评分

    def test_concurrent_same_case_different_requests(self) -> None:
        service = make_service()
        case = CASES[0]

        def submit(i: int) -> Any:
            return service.submit(
                case_id=case.case_id, answer=f"作答 {i}", trainee_id=i + 1, request_id=f"req-{i}"
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        assert len({r.result_id for r in results}) == 8  # 各自独立结果
        assert all(r.status == "scored" for r in results)


class TestDegradation:
    """降级：LLM 评分失败 → pending_manual（不报错、不产出无依据分数，决策 D4）。"""

    def test_scorer_failure_pending_manual(self) -> None:
        broken = BrokenScorer()
        service = make_service(broken)
        result = service.submit(
            case_id=CASES[0].case_id, answer="收到。", trainee_id=1, request_id="r"
        )
        assert result.status == "pending_manual"
        assert result.dims == ()  # 不产出无依据分数
        assert result.total is None
        assert result.gap_analysis is None
        assert "LLM" in result.scoring_error

    def test_pending_manual_not_calibratable(self) -> None:
        """待人工结果无 AI 评分快照，不可校准（TR-2004）。"""
        service = make_service(BrokenScorer())
        result = service.submit(
            case_id=CASES[0].case_id, answer="收到。", trainee_id=1, request_id="r"
        )
        with pytest.raises(ResultStateError) as excinfo:
            service.record_supervisor_score(
                result_id=result.result_id, supervisor="主管甲",
                scores=sup_scores(60, 60, 60),
            )
        assert excinfo.value.biz_code == "TR-2004"


class TestIsolation:
    """会话隔离：学员仅本人演练数据可见（NFR-SEC-02）。"""

    def test_get_result_wrong_trainee_denied(self) -> None:
        service = make_service()
        result = service.submit(
            case_id=CASES[0].case_id, answer="收到。", trainee_id=1, request_id="r"
        )
        with pytest.raises(ResultNotFoundError):
            service.get_result(result.result_id, trainee_id=2)  # 他人视角视同不存在

    def test_list_results_only_own(self) -> None:
        service = make_service()
        for i in range(3):
            service.submit(case_id=CASES[i].case_id, answer="收到。", trainee_id=1, request_id=f"r{i}")
        service.submit(case_id=CASES[3].case_id, answer="收到。", trainee_id=2, request_id="r9")
        assert len(service.list_results(1)) == 3
        assert all(r.trainee_id == 1 for r in service.list_results(1))


class TestCalibration:
    """等价类/边界：校准判定（总分差≤10 且维差≤15 判一致，SR §6.3/§9 表）。"""

    def make_scored(self) -> tuple[DrillService, str]:
        service = make_service(StubScorer())
        result = service.submit(
            case_id=CASES[0].case_id, answer="收到。", trainee_id=1, request_id="r"
        )
        return service, result.result_id

    def test_calibration_consistent(self) -> None:
        """维差 12（≤15）且总分差 7.3（≤10）→ 一致。"""
        service, rid = self.make_scored()
        sample = service.record_supervisor_score(
            result_id=rid, supervisor="主管甲", scores=sup_scores(62, 72, 68)
        )
        assert sample.consistent is True
        assert sample.deviation == {DIM_PRODUCT: 12, DIM_SCRIPT: 12, DIM_STRATEGY: 2}
        assert sample.total_deviation == 7.3

    def test_calibration_deviation_recorded(self) -> None:
        """维差 18（>15）→ 偏差，deviation 留痕供 rubric/Prompt 回流。"""
        service, rid = self.make_scored()
        sample = service.record_supervisor_score(
            result_id=rid, supervisor="主管甲", scores=sup_scores(68, 78, 88)
        )
        assert sample.consistent is False
        assert sample.deviation[DIM_PRODUCT] == 18
        assert sample.total_deviation == 18.0

    def test_calibration_boundary_dim15_total10(self) -> None:
        """边界：维差恰 15、总分差恰 10 → 判一致（阈值取等）。"""
        service, rid = self.make_scored()
        sample = service.record_supervisor_score(
            result_id=rid, supervisor="主管甲", scores=sup_scores(65, 75, 70)
        )
        assert sample.consistent is True

    def test_calibration_boundary_dim16_inconsistent(self) -> None:
        """边界：维差 16（>15）→ 判偏差。"""
        service, rid = self.make_scored()
        sample = service.record_supervisor_score(
            result_id=rid, supervisor="主管甲", scores=sup_scores(66, 75, 70)
        )
        assert sample.consistent is False

    def test_calibration_against_fake_scorer_e2e(self) -> None:
        """端到端：Fake 满分作答（AI=100）对主管 90 分 → 三维差 10、总分差 10 → 一致。"""
        service = make_service()
        result = service.submit(
            case_id=CASES[0].case_id, answer=CASES[0].excellent_reply,
            trainee_id=1, request_id="r",
        )
        sample = service.record_supervisor_score(
            result_id=result.result_id, supervisor="主管甲", scores=sup_scores(90, 90, 90)
        )
        assert sample.consistent is True

    def test_calibration_unknown_result(self) -> None:
        service = make_service(StubScorer())
        with pytest.raises(ResultNotFoundError):
            service.record_supervisor_score(
                result_id="RES-404", supervisor="主管甲", scores=sup_scores(60, 60, 60)
            )

    @pytest.mark.parametrize(
        "scores",
        [
            {DIM_PRODUCT: 60, DIM_SCRIPT: 60},  # 缺维
            {DIM_PRODUCT: 60, DIM_SCRIPT: 60, DIM_STRATEGY: 101},  # 越上界
        ],
    )
    def test_calibration_invalid_scores(self, scores: dict[str, int]) -> None:
        service, rid = self.make_scored()
        with pytest.raises(ValueError):
            service.record_supervisor_score(result_id=rid, supervisor="主管甲", scores=scores)


class TestCalibrationReport:
    """降级/边界：一致率报表与 <85% 冻结投题（§16.2 KPI）。"""

    def submit_n(self, service: DrillService, n: int, bad_from: int) -> None:
        """提交 n 条并主管评分：前 bad_from 条与 AI 一致，其余偏差（90/90/90 对定分桩 60）。"""
        for i in range(n):
            result = service.submit(
                case_id=CASES[i].case_id, answer="收到。", trainee_id=1, request_id=f"r{i}"
            )
            scores = sup_scores(50, 60, 70) if i < bad_from else sup_scores(90, 90, 90)
            service.record_supervisor_score(result_id=result.result_id, supervisor="主管甲", scores=scores)

    def test_report_rate_90_not_frozen(self) -> None:
        """9 组一致 + 1 组偏差 → 一致率 90.0%，不低于 85% 不冻结。"""
        service = make_service(StubScorer(), min_published=1)
        self.submit_n(service, 10, bad_from=9)
        report = service.calibration_report()
        assert report.samples == 10
        assert report.consistent == 9
        assert report.consistency_rate == 90.0
        assert report.frozen is False

    def test_report_frozen_below_85(self) -> None:
        """8 组一致 + 2 组偏差 → 80.0% < 85% → 冻结标志置位。"""
        service = make_service(StubScorer(), min_published=1)
        self.submit_n(service, 10, bad_from=8)
        assert service.calibration_report().frozen is True

    def test_report_no_samples_rate_none_not_frozen(self) -> None:
        """无样本：一致率不定义（None），不冻结（无证据不做保守降级）。"""
        report = make_service().calibration_report()
        assert report.consistency_rate is None
        assert report.frozen is False

    def test_frozen_blocks_assign(self) -> None:
        """降级：冻结后新投题被拒（TR-2002），存量评分不受影响。"""
        service = make_service(StubScorer(), min_published=1, cases=3)
        self.submit_n(service, 3, bad_from=0)  # 0% 一致 → 冻结
        assert service.calibration_report().frozen is True
        with pytest.raises(CalibrationFrozenError) as excinfo:
            service.assign(page=1, page_size=10)
        assert excinfo.value.biz_code == "TR-2002"
