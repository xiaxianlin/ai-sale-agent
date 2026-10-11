# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""考试认证服务单元测试（FR-TR-007，SR-TR-007 §6.3~§6.5；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类）：
- 评分：客观题精确判分（multi 全对才给分）/ 主观 Fake rubric 评分 / 主观失败待人工回填；
- 状态机：cert_status 三态合法迁移矩阵 / 有效期届满扫描；
- 边界：分数线恰好通过 / 未达线不授予；
- 异常：未开考交卷 / 未知试卷 / 冷却期 / 次数上限 / 复核状态冲突；
- 并发：同场考试并发提交恰一胜（评分器只调一次）；
- 降级：监考弱信号不拦截，强证据进人工复核（复核裁决 upheld/overturned 纠偏）。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from services.training.core.exam.paper import (
    PaperGenerator,
    QuestionBankItem,
    build_demo_questions,
    InMemoryQuestionBank,
)
from services.training.core.exam.proctor import ProctorEvent
from services.training.core.exam.service import (
    CERT_CERTIFIED,
    CERT_EXPIRED,
    CERT_UNCERTIFIED,
    AttemptCooldownError,
    AttemptExceededError,
    CertTransitionError,
    ExamNotOngoingError,
    ExamNotFoundError,
    ExamOngoingError,
    ExamService,
    InMemoryExamStore,
    ReviewNotPendingError,
    ScoringError,
    SubjectiveScorer,
    transition_cert,
)

# --------------------------------------------------------------------------- 夹具与助手

_START = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """假时钟：测试手动推进（冷却期/有效期断言不依赖睡眠）。"""

    def __init__(self, start: datetime = _START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now += timedelta(**kwargs)


class CountingScorer:
    """主观评分桩包装：透传内层并统计调用次数（并发恰一胜断言用）。"""

    def __init__(self, inner: SubjectiveScorer) -> None:
        self._inner = inner
        self.calls = 0
        self._lock = threading.Lock()

    def score(self, question: QuestionBankItem, answer: str) -> int:
        with self._lock:
            self.calls += 1
        return self._inner.score(question, answer)


class BrokenScorer:
    """主观评分故障桩：恒抛 ScoringError（降级待人工路径）。"""

    def score(self, question: QuestionBankItem, answer: str) -> int:
        raise ScoringError("LLM 评分不可用")


def make_service(*, scorer: SubjectiveScorer | None = None, clock: FakeClock | None = None) -> ExamService:
    """标准装配：demo 题库（每维 15 题）+ 内存存储 + Fake rubric 评分器 + 假时钟。"""
    bank = InMemoryQuestionBank(build_demo_questions(per_dim_obj=11, per_dim_subj=4))
    return ExamService(
        bank,
        InMemoryExamStore(),
        scorer if scorer is not None else _default_scorer(),
        clock=clock or FakeClock(),
    )


def _default_scorer() -> SubjectiveScorer:
    from services.training.core.exam.service import FakeRubricScorer

    return FakeRubricScorer()


def correct_answers(service: ExamService, exam_uid: str) -> dict[str, Any]:
    """从试卷快照取全对作答（multi 转列表形态，模拟前端提交）。"""
    return {
        q.question_id: list(q.answer) if q.qtype == "multi" else q.answer
        for q in service.get_paper(exam_uid).questions
    }


def wrong_answers(service: ExamService, exam_uid: str) -> dict[str, Any]:
    """全错作答：客观题填不存在选项，主观题留空（未作答得 0 分）。"""
    subjective = ("short_answer", "case")
    return {
        q.question_id: (["ZZ"] if q.qtype == "multi" else "" if q.qtype in subjective else "ZZ")
        for q in service.get_paper(exam_uid).questions
    }


def run_full_exam(
    service: ExamService, trainee_id: int = 101, *, events: list[ProctorEvent] | None = None
) -> str:
    """开一场考试并以全对作答交卷，返回 exam_uid。"""
    exam = service.create_exam(trainee_id)
    service.start(exam.exam_uid)
    service.submit(exam.exam_uid, correct_answers(service, exam.exam_uid), events=events or [])
    return exam.exam_uid


# --------------------------------------------------------------------------- 评分


class TestGrading:
    """自动评分：客观精确 / 主观 rubric / 降级待人工。"""

    def test_objective_exact_scoring(self) -> None:
        """客观题精确比对：multi 集合全对才给分（部分命中 0 分），全对卷总分 100。"""
        service = make_service()
        run_full_exam(service)
        outcome = service.list_outcomes(101)[0]
        assert outcome.score == 100.0
        assert outcome.passed is True

    def test_multi_partial_answer_scores_zero(self) -> None:
        """multi 只答一半 → 该题 0 分（集合精确比对，不给部分分）。"""
        service = make_service()
        exam = service.create_exam(101)
        service.start(exam.exam_uid)
        answers = correct_answers(service, exam.exam_uid)
        paper = service.get_paper(exam.exam_uid)
        for q in paper.questions:
            if q.qtype == "multi":
                answers[q.question_id] = list(q.answer)[:1]  # 只答一个正确项
        service.submit(exam.exam_uid, answers)
        outcome = service.list_outcomes(101)[0]
        assert outcome.score < 100.0
        assert any(r["qtype"] == "multi" and r["earned"] == 0 for r in outcome.per_question)

    def test_subjective_fake_rubric_scoring(self) -> None:
        """主观题 Fake rubric：关键词全命中满分、零命中零分（demo 口径确定性）。"""
        from services.training.core.exam.service import FakeRubricScorer

        scorer = FakeRubricScorer()
        bank = InMemoryQuestionBank(build_demo_questions(per_dim_obj=11, per_dim_subj=4))
        subjective = next(q for q in bank.items if q.qtype == "case")
        assert scorer.score(subjective, " ".join(subjective.rubric)) == subjective.score
        assert scorer.score(subjective, "完全无关的回答") == 0

    def test_subjective_failure_manual_pending(self) -> None:
        """降级：主观评分器故障 → 该题 manual_pending、passed=None，客观题正常合卷。"""
        service = make_service(scorer=BrokenScorer())
        exam = service.create_exam(101)
        service.start(exam.exam_uid)
        outcome = service.submit(exam.exam_uid, correct_answers(service, exam.exam_uid))
        assert outcome.passed is None
        assert len(outcome.manual_pending) >= 1
        assert service.cert_status_of(101) == CERT_UNCERTIFIED

    def test_manual_score_backfill_completes(self) -> None:
        """人工分回填：逐题补齐后合卷出总分，≥ 分数线授予认证（SR §6.3 不阻塞其他题）。

        附带校验：回填不存在的题目直接 ValueError（防错题号静默吞掉）。
        """
        service = make_service(scorer=BrokenScorer())
        exam = service.create_exam(101)
        service.start(exam.exam_uid)
        outcome = service.submit(exam.exam_uid, correct_answers(service, exam.exam_uid))
        for qid in list(outcome.manual_pending):
            with pytest.raises(ValueError):
                service.apply_manual_score(exam.exam_uid, "GHOST-Q", 25)
            outcome = service.apply_manual_score(exam.exam_uid, qid, 25)
        assert outcome.passed is True
        assert outcome.manual_pending == ()
        assert service.cert_status_of(101) == CERT_CERTIFIED

    def test_section_scores_cover_four_dims(self) -> None:
        """分维得分（FR-TR-004 证据回流）：四维齐备、百分制。"""
        service = make_service()
        run_full_exam(service)
        outcome = service.list_outcomes(101)[0]
        assert set(outcome.section_scores) == {"product", "communication", "negotiation", "platform"}
        assert all(0.0 <= v <= 100.0 for v in outcome.section_scores.values())

    def test_pass_line_boundary(self) -> None:
        """边界：全对恰 100 ≥ 80 通过；全错 0 < 80 不通过且不授予（SR §7 TR_EXAM_PASS_LINE）。"""
        service = make_service()
        run_full_exam(service, 201)
        service2 = make_service()
        exam = service2.create_exam(202)
        service2.start(exam.exam_uid)
        service2.submit(exam.exam_uid, wrong_answers(service2, exam.exam_uid))
        outcome = service2.list_outcomes(202)[0]
        assert outcome.score == 0.0
        assert outcome.passed is False
        assert service2.cert_status_of(202) == CERT_UNCERTIFIED


# --------------------------------------------------------------------------- 监考联动与复核


class TestProctorIntegration:
    """监考终判与人工复核联动（证据链 → 加权 → 复核裁决）。"""

    def test_weak_signal_does_not_block_grading(self) -> None:
        """弱信号（权重 1）随卷提交 → 正常判分与授予（宁漏勿误）。"""
        service = make_service()
        events = [
            ProctorEvent(t=i * 100, type="answer", detail={"elapsed_ms": 500, "correct": True})
            for i in range(5)
        ]
        run_full_exam(service, 301, events=events)
        outcome = service.list_outcomes(301)[0]
        assert outcome.cheat_suspect is False
        assert outcome.passed is True

    def test_strong_evidence_freezes_review(self) -> None:
        """强证据（cheat_score=5）→ status=review、passed=None、cert 不授予。"""
        service = make_service()
        events = [ProctorEvent(t=i * 1000, type="blur") for i in range(3)]
        events.append(ProctorEvent(t=9999, type="paste", detail={"len": 80}))
        run_full_exam(service, 301, events=events)
        outcome = service.list_outcomes(301)[0]
        assert outcome.cheat_score == 5
        assert outcome.cheat_suspect is True
        assert outcome.review_status == "pending"
        assert outcome.passed is None
        assert service.cert_status_of(301) == CERT_UNCERTIFIED

    def test_review_upheld_voids_and_doubles_cooldown(self) -> None:
        """复核 upheld：本卷作废 passed=false，违规冷却期翻倍为 6 天。"""
        clock = FakeClock()
        service = make_service(clock=clock)
        exam_uid = run_full_exam(
            service, 301,
            events=[ProctorEvent(t=i * 1000, type="blur") for i in range(3)]
            + [ProctorEvent(t=9999, type="paste", detail={"len": 80})],
        )
        service.apply_review(exam_uid, "upheld", reviewer="主管甲", note="证据成立")
        outcome = service.list_outcomes(301)[0]
        assert outcome.status == "void"
        assert outcome.passed is False
        # 冷却翻倍：4 天后仍禁考，7 天后放行
        clock.advance(days=4)
        with pytest.raises(AttemptCooldownError):
            service.create_exam(301)
        clock.advance(days=3)
        assert service.create_exam(301) is not None

    def test_review_overturned_grants_when_pass(self) -> None:
        """复核 overturned：撤销嫌疑，分数达线 → 补授予 certified（纠偏通道）。"""
        service = make_service()
        exam_uid = run_full_exam(
            service, 302,
            events=[ProctorEvent(t=i * 1000, type="blur") for i in range(3)]
            + [ProctorEvent(t=9999, type="paste", detail={"len": 80})],
        )
        outcome = service.apply_review(exam_uid, "overturned", reviewer="主管乙", note="误判")
        assert outcome.review_status == "overturned"
        assert outcome.passed is True
        assert service.cert_status_of(302) == CERT_CERTIFIED

    def test_review_not_pending_conflict(self) -> None:
        """异常：无待裁决嫌疑时复核 → ReviewNotPendingError（409）。"""
        service = make_service()
        exam_uid = run_full_exam(service, 303)
        with pytest.raises(ReviewNotPendingError):
            service.apply_review(exam_uid, "upheld", reviewer="主管丙")

    def test_proctor_events_endpoint_accumulates(self) -> None:
        """考试中事件上报：服务端即时返回命中规则并累计证据。"""
        service = make_service()
        exam = service.create_exam(304)
        service.start(exam.exam_uid)
        flags = service.submit_proctor_events(
            exam.exam_uid, [ProctorEvent(t=1000, type="blur"), ProctorEvent(t=2000, type="focus")]
        )
        assert flags == []  # 1 次切屏：留痕不命中
        flags = service.submit_proctor_events(
            exam.exam_uid,
            [ProctorEvent(t=3000, type="blur"), ProctorEvent(t=4000, type="focus"),
             ProctorEvent(t=5000, type="blur"), ProctorEvent(t=6000, type="focus")],
        )
        assert [f.rule for f in flags] == ["switch_window"]


# --------------------------------------------------------------------------- 认证状态机与有效期


class TestCertStateMachine:
    """cert_status 三态状态机（uncertified/certified/expired 合法迁移矩阵）。"""

    def test_legal_transition_matrix(self) -> None:
        """合法迁移：uncertified→certified / certified→expired / expired→certified。"""
        assert transition_cert(CERT_UNCERTIFIED, CERT_CERTIFIED) == CERT_CERTIFIED
        assert transition_cert(CERT_CERTIFIED, CERT_EXPIRED) == CERT_EXPIRED
        assert transition_cert(CERT_EXPIRED, CERT_CERTIFIED) == CERT_CERTIFIED

    def test_illegal_transition_rejected(self) -> None:
        """非法迁移：uncertified→expired / certified→uncertified / 同态 → 拒绝。"""
        for cur, tgt in ((CERT_UNCERTIFIED, CERT_EXPIRED), (CERT_CERTIFIED, CERT_UNCERTIFIED),
                         (CERT_UNCERTIFIED, CERT_UNCERTIFIED)):
            with pytest.raises(CertTransitionError):
                transition_cert(cur, tgt)

    def test_grant_on_pass_and_regrant_after_expired(self) -> None:
        """通过授予 certified；有效期届满 expired；重考通过再授予（覆盖式取最新）。"""
        clock = FakeClock()
        service = make_service(clock=clock)
        run_full_exam(service, 401)
        assert service.cert_status_of(401) == CERT_CERTIFIED
        first_onboarded = service.get_trainee(401).onboarded_at
        assert first_onboarded is not None
        clock.advance(days=366)
        expired = service.expire_sweep()
        assert 401 in expired
        assert service.cert_status_of(401) == CERT_EXPIRED
        run_full_exam(service, 401)
        assert service.cert_status_of(401) == CERT_CERTIFIED
        assert service.get_trainee(401).onboarded_at >= first_onboarded

    def test_expire_sweep_idempotent_before_deadline(self) -> None:
        """有效期边界：365 天整未届满不过期；扫描幂等可重跑。"""
        clock = FakeClock()
        service = make_service(clock=clock)
        run_full_exam(service, 402)
        clock.advance(days=365)
        assert service.expire_sweep() == []
        clock.advance(days=1)
        assert service.expire_sweep() == [402]
        assert service.expire_sweep() == []  # 已过期不再重复出列


# --------------------------------------------------------------------------- 考试生命周期异常


class TestLifecycleErrors:
    """创建/开考/交卷的状态与参数异常。"""

    def test_duplicate_ongoing_rejected(self) -> None:
        """同一学员同时只有一场进行中考试 → ExamOngoingError（409）。"""
        service = make_service()
        service.create_exam(501)
        with pytest.raises(ExamOngoingError):
            service.create_exam(501)

    def test_submit_before_start_rejected(self) -> None:
        """未开考交卷 → ExamNotOngoingError（409）。"""
        service = make_service()
        exam = service.create_exam(502)
        with pytest.raises(ExamNotOngoingError):
            service.submit(exam.exam_uid, {})

    def test_unknown_exam_not_found(self) -> None:
        """未知试卷：开考/交卷/查询均 → ExamNotFoundError（404）。"""
        service = make_service()
        with pytest.raises(ExamNotFoundError):
            service.start("EX-999999")
        with pytest.raises(ExamNotFoundError):
            service.submit("EX-999999", {})
        with pytest.raises(ExamNotFoundError):
            service.get_outcome("EX-999999")

    def test_double_submit_replays_first_result(self) -> None:
        """重复交卷幂等：返回首次结果（分数一致，不重复评分）。"""
        service = make_service()
        exam = service.create_exam(503)
        service.start(exam.exam_uid)
        first = service.submit(exam.exam_uid, correct_answers(service, exam.exam_uid))
        second = service.submit(exam.exam_uid, correct_answers(service, exam.exam_uid))
        assert first.score == second.score
        assert first.submitted_at == second.submitted_at


# --------------------------------------------------------------------------- 重考策略


class TestRetryPolicy:
    """重考：冷却期 / 次数上限 / 近 2 次题目去重（SR §6.5）。"""

    def test_cooldown_blocks_immediate_retake(self) -> None:
        """未通过立即重考 → AttemptCooldownError（429）；冷却期满放行。"""
        clock = FakeClock()
        service = make_service(clock=clock)
        exam = service.create_exam(601)
        service.start(exam.exam_uid)
        service.submit(exam.exam_uid, wrong_answers(service, exam.exam_uid))
        with pytest.raises(AttemptCooldownError):
            service.create_exam(601)
        clock.advance(days=3)
        assert service.create_exam(601).attempt_no == 2

    def test_max_attempts_exceeded(self) -> None:
        """第 4 次创建 → AttemptExceededError（403，需主管解锁）。"""
        clock = FakeClock()
        service = make_service(clock=clock)
        for i in range(3):
            exam = service.create_exam(602)
            service.start(exam.exam_uid)
            service.submit(exam.exam_uid, wrong_answers(service, exam.exam_uid))
            clock.advance(days=4)
        with pytest.raises(AttemptExceededError):
            service.create_exam(602)

    def test_reattempt_excludes_recent_questions(self) -> None:
        """防背题：重考试卷与近 2 次已考题零交集。"""
        clock = FakeClock()
        service = make_service(clock=clock)
        exam1 = service.create_exam(603)
        service.start(exam1.exam_uid)
        service.submit(exam1.exam_uid, wrong_answers(service, exam1.exam_uid))
        first_ids = {q.question_id for q in service.get_paper(exam1.exam_uid).questions}
        clock.advance(days=4)
        exam2 = service.create_exam(603)
        service.start(exam2.exam_uid)
        service.submit(exam2.exam_uid, wrong_answers(service, exam2.exam_uid))
        second_ids = {q.question_id for q in service.get_paper(exam2.exam_uid).questions}
        clock.advance(days=4)
        exam3 = service.create_exam(603)
        assert first_ids.isdisjoint(second_ids)
        third_ids = {q.question_id for q in service.get_paper(exam3.exam_uid).questions}
        assert first_ids.isdisjoint(third_ids)
        assert second_ids.isdisjoint(third_ids)


# --------------------------------------------------------------------------- 并发


class TestConcurrency:
    """并发口径：并发交卷恰一胜（评分恰一次，结果一致）。"""

    def test_concurrent_submit_single_grading(self) -> None:
        """两线程同时交卷：主观评分恰一批（每题一次，无重复评分），双方拿到同一结果。"""
        from services.training.core.exam.paper import is_objective

        bank = InMemoryQuestionBank(build_demo_questions(per_dim_obj=11, per_dim_subj=4))
        counting = CountingScorer(_default_scorer())
        clock = FakeClock()
        service = ExamService(bank, InMemoryExamStore(), counting, clock=clock)
        exam = service.create_exam(701)
        service.start(exam.exam_uid)
        answers = correct_answers(service, exam.exam_uid)
        subj_count = sum(
            1 for q in service.get_paper(exam.exam_uid).questions if not is_objective(q.qtype)
        )
        barrier = threading.Barrier(2)
        results: list[Any] = []

        def worker() -> None:
            barrier.wait()
            results.append(service.submit(exam.exam_uid, answers))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            for f in futures:
                f.result(timeout=10)
        assert counting.calls == subj_count  # 恰一胜：并发重放不产生第二次评分副作用
        assert results[0].score == results[1].score
        assert results[0].submitted_at == results[1].submitted_at
        assert service.cert_status_of(701) == CERT_CERTIFIED
