# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""实战演练评分引擎测试（FR-TR-003，SR-TR-003 §6.2；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类场景）：
- 等价类：FakeDrillScorer 关键词覆盖度三档（全命中/部分命中/零命中）、差距分析结构；
- 异常：DeepSeek 评分器未配置报错（中文）、响应不可解析/维度残缺 → ScoringError；
- 可解释性：每维分数必须携带作答原文依据（evidence.quote 为作答子串，决策 D4）；
- 降级行为在 service 层验证（LLM 失败 → pending_manual），此处锁定错误类型契约。
"""
from __future__ import annotations

import json

import pytest

from services.training.core.drill.casebank import (
    DIM_PRODUCT,
    DIM_SCRIPT,
    DIM_STRATEGY,
    DrillCase,
    build_demo_cases,
)
from services.training.core.drill.scoring import (
    DeepSeekDrillScorer,
    DrillScorer,
    FakeDrillScorer,
    ScoringError,
    ScorerConfigError,
    build_gap_analysis,
)

CASE: DrillCase = build_demo_cases(100)[0]
GOOD_ANSWER = CASE.excellent_reply  # 全要点覆盖（锚点断言见 casebank 测试）
BAD_ANSWER = "你好，来信收到，我们会尽快回复。"  # 零关键词覆盖


def make_scorer() -> FakeDrillScorer:
    return FakeDrillScorer()


class TestFakeScorerEquivalence:
    """等价类：Fake 评分器确定性——按 rubric 评分点关键词覆盖度打 0-100 三维分。"""

    def test_full_coverage_scores_100(self) -> None:
        """优秀回复（全要点覆盖）→ 三维 100、总分 100，三维中文标签齐备。"""
        out = make_scorer().score(CASE, GOOD_ANSWER)
        assert [d.score for d in out.dims] == [100, 100, 100]
        assert out.total == 100
        assert [d.dim for d in out.dims] == [DIM_PRODUCT, DIM_SCRIPT, DIM_STRATEGY]
        assert [d.label for d in out.dims] == ["产品准确性", "话术", "商务策略"]

    def test_zero_coverage_scores_0(self) -> None:
        """零覆盖作答 → 三维 0、总分 0，全部评分点计入 missed_points。"""
        out = make_scorer().score(CASE, BAD_ANSWER)
        assert [d.score for d in out.dims] == [0, 0, 0]
        assert out.total == 0
        assert sum(len(d.missed_points) for d in out.dims) == 9

    def test_partial_coverage_proportional(self) -> None:
        """部分覆盖按比例：产品/话术满分、策略零分 → 总分 round(200/3)=67。"""
        answer = (
            "感谢您的来函。产品参数：承重 35 千克，已通过 CE 认证，报价单随函附上。"
            "第一，参数明细见附件；第二，报价有效期 15 天。期待您的回复。"
        )
        out = make_scorer().score(CASE, answer)
        by_dim = {d.dim: d.score for d in out.dims}
        assert by_dim[DIM_PRODUCT] == 100
        assert by_dim[DIM_SCRIPT] == 100
        assert by_dim[DIM_STRATEGY] == 0
        assert out.total == 67

    def test_deterministic_same_input(self) -> None:
        """确定性：同输入两次评分输出完全一致（demo 可断言口径）。"""
        scorer = make_scorer()
        assert scorer.score(CASE, GOOD_ANSWER) == scorer.score(CASE, GOOD_ANSWER)

    def test_satisfies_protocol(self) -> None:
        """Fake 与 DeepSeek 实现均满足 DrillScorer 协议（可插拔，LLM Protocol 模式）。"""
        assert isinstance(make_scorer(), DrillScorer)
        assert isinstance(DeepSeekDrillScorer(api_key="stub"), DrillScorer)


class TestEvidence:
    """可解释性（决策 D4）：每维分数必须附作答原文依据，严禁无依据分数。"""

    def test_evidence_quote_is_substring_of_answer(self) -> None:
        out = make_scorer().score(CASE, GOOD_ANSWER)
        for dim in out.dims:
            for ev in dim.evidence:
                assert ev.quote in GOOD_ANSWER  # 引用确为作答子串

    def test_each_hit_point_has_evidence(self) -> None:
        out = make_scorer().score(CASE, GOOD_ANSWER)
        evidence_points = {ev.point_id for dim in out.dims for ev in dim.evidence}
        rubric_points = {p.point_id for p in CASE.rubric}
        assert evidence_points == rubric_points  # 命中评分点逐一带依据

    def test_missed_points_subset_of_rubric(self) -> None:
        """missed_points 必须属于 rubric 点位（防编造）。"""
        out = make_scorer().score(CASE, BAD_ANSWER)
        rubric_points = {p.point_id for p in CASE.rubric}
        for dim in out.dims:
            assert set(dim.missed_points) <= rubric_points


class TestGapAnalysis:
    """等价类：差距分析（新人作答 vs 优秀回复逐维对比结构）。"""

    def test_zero_coverage_all_missing(self) -> None:
        gap = build_gap_analysis(CASE, BAD_ANSWER)
        assert gap.total_points == 9
        assert gap.covered_points == 0
        assert len(gap.missing_points) == 9
        for item in gap.missing_points:
            assert item.dim in (DIM_PRODUCT, DIM_SCRIPT, DIM_STRATEGY)
            assert item.point_id and item.desc
            assert item.excellent_quote in CASE.excellent_reply  # 优秀回复侧引用

    def test_full_coverage_no_gap(self) -> None:
        gap = build_gap_analysis(CASE, GOOD_ANSWER)
        assert gap.missing_points == ()
        assert gap.covered_points == gap.total_points == 9


class TestDeepSeekScorer:
    """异常：DeepSeek 形态评分器桩（未配置报错；离线 transport 验证解析路径）。"""

    def test_unconfigured_raises_chinese_error(self) -> None:
        scorer = DeepSeekDrillScorer()  # 未传 api_key 且环境变量未配置
        with pytest.raises(ScorerConfigError) as excinfo:
            scorer.score(CASE, GOOD_ANSWER)
        assert "TR_DRILL_DEEPSEEK_API_KEY" in str(excinfo.value)

    def test_configured_with_mock_transport_parses(self) -> None:
        """离线 transport 返回合法 JSON → 解析为 ScoreOutput（结构校验通过）。"""
        import httpx

        payload = {
            "dims": [
                {"dim": DIM_PRODUCT, "score": 80, "evidence": [], "missed_points": ["P1"]},
                {"dim": DIM_SCRIPT, "score": 75, "evidence": [], "missed_points": []},
                {"dim": DIM_STRATEGY, "score": 70, "evidence": [], "missed_points": []},
            ],
            "total": 75,
            "comment": "整体合格，产品参数可再具体。",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

        scorer = DeepSeekDrillScorer(api_key="stub", transport=httpx.MockTransport(handler))
        out = scorer.score(CASE, GOOD_ANSWER)
        assert out.total == 75
        assert [d.score for d in out.dims] == [80, 75, 70]

    def test_invalid_response_raises_scoring_error(self) -> None:
        """响应不可解析 → ScoringError（服务层据此降级 pending_manual）。"""
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": "不是 JSON"}}]})

        scorer = DeepSeekDrillScorer(api_key="stub", transport=httpx.MockTransport(handler))
        with pytest.raises(ScoringError):
            scorer.score(CASE, GOOD_ANSWER)

    def test_dim_mismatch_rejected(self) -> None:
        """返回维度残缺（少一维）→ 结构校验拒绝（不产出残缺分数）。"""
        import httpx

        payload = {
            "dims": [
                {"dim": DIM_PRODUCT, "score": 80, "evidence": [], "missed_points": []},
                {"dim": DIM_SCRIPT, "score": 75, "evidence": [], "missed_points": []},
            ],
            "total": 75,
            "comment": "",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

        scorer = DeepSeekDrillScorer(api_key="stub", transport=httpx.MockTransport(handler))
        with pytest.raises(ScoringError):
            scorer.score(CASE, GOOD_ANSWER)
