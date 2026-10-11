# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""复盘报告生成器单元测试（FR-TR-002，SR-TR-002 §6.3/§9；迭代 1.2 TDD 阶段①）。

五类场景覆盖（开发规范 §9.1）：
- 等价类：deal/breakdown/timeout 三种结局的复盘结构（scores 三维/建议三段/知识引用）；
- 边界：全命中/零命中评分极值；
- 异常：未知会话/未结束会话/LLM 故障降级报告；
- 并发与幂等由 engine/store/api 文件承载（本文件覆盖报告幂等覆盖写）。

验收 KPI 映射（测试即验收证据）：SRS §16.2「评分一致性 ≥85%」的落库明细结构
（dimension_scores → scores 三维 + goal_achievement 逐考核点证据 seq）。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.training.core.simulator.debrief import (
    DebriefGenerator,
    DebriefReport,
    FakeDebriefLlm,
    InMemoryDebriefStore,
)
from services.training.core.simulator.engine import (
    FakeSimulatorLlm,
    SimBadRequest,
    SimSessionNotFound,
    SimulatorEngine,
    SimulatorLlmError,
)
from services.training.core.simulator.persona import BUILT_IN_SCENARIOS, PersonaRegistry, PersonaSpec
from services.training.core.simulator.store import InMemorySimSessionStore

SC = BUILT_IN_SCENARIOS["sc-bargain-moq"]
VALUE_MSG = "请介绍一下你们的认证、交期和质量参数。"
BARGAIN_MSG = "太贵了，给我最低价，能便宜一点吗？"
NEUTRAL_MSG = "你好，请继续。"


def make_spec(persona_id: str = "C-91", state: tuple[float, int, int] = (0.70, 0, 100),
              points: tuple[str, ...] = ("价值传递", "信任建立", "专业度展示")) -> PersonaSpec:
    """构造测试 persona（C-9x 段；默认三考核点便于百分比断言）。"""
    return PersonaSpec(
        persona_id=persona_id,
        name="测试客户型",
        profile="用于单测的合成客户画像",
        behavior_traits=["特征甲", "特征乙"],
        hidden_needs="真实在意交期稳定性与返修率",
        difficulty_base=3,
        state_init={"price_stance": state[0], "emotion": state[1], "patience": state[2]},
        assessment_points=list(points),
    )


def make_engine(debrief_llm=None, *, max_rounds: int = 20) -> SimulatorEngine:
    """装配：注册 C-91（低立场起步易成交）/C-92（低耐心易谈崩）+ 复盘生成器。"""
    registry = PersonaRegistry.with_demo_extensions()
    registry.register(make_spec("C-91"))
    registry.register(make_spec("C-92", state=(0.15, 0, 13), points=("价值传递",)))
    generator = DebriefGenerator(
        debrief_llm if debrief_llm is not None else FakeDebriefLlm(), InMemoryDebriefStore()
    )
    return SimulatorEngine(
        llm=FakeSimulatorLlm(),
        store=InMemorySimSessionStore(),
        registry=registry,
        debrief=generator,
        max_rounds=max_rounds,
    )


class TestReportStructure:
    """等价类：复盘报告结构与 Fake 评分规则。"""

    def test_deal_report_structure(self) -> None:
        """deal 复盘：逐考核点判定、scores 三维、建议三段、知识引用非空、轮数正确。"""
        engine = make_engine()
        sid = engine.start_session("C-91", "sc-bargain-moq", difficulty=3)["session_id"]
        engine.reply(sid, VALUE_MSG)  # 0.70+0.074=0.774 且情绪 1 → deal
        report = engine.generate_debrief(sid)
        assert report.verdict == "deal" and report.session_id == sid
        assert report.persona_id == "C-91" and report.rounds == 1
        assert [p.point for p in report.goal_achievement] == ["价值传递", "信任建立", "专业度展示"]
        assert set(report.scores) == {"goal_achievement", "talk_skill", "strategy"}
        assert all(0 <= v <= 100 for v in report.scores.values())
        assert len(report.suggestions) == 3  # 改进建议三段
        assert report.knowledge_refs
        # System Prompt 携带考核点与结局（教练视角）
        assert "价值传递" in engine.debrief.llm.last_system
        assert "deal" in engine.debrief.llm.last_system

    def test_point_hit_with_keyword_evidence(self) -> None:
        """考核点证据：关键词命中点带 trainee 消息 seq；未命中点无证据。"""
        engine = make_engine()
        sid = engine.start_session("C-91", "sc-bargain-moq", difficulty=3)["session_id"]
        engine.reply(sid, VALUE_MSG)
        report = engine.generate_debrief(sid)
        by_point = {p.point: p for p in report.goal_achievement}
        assert by_point["价值传递"].hit is True
        assert by_point["价值传递"].evidence_seq == [2]  # trainee 消息 seq=2
        assert by_point["信任建立"].hit is False
        assert by_point["信任建立"].evidence_seq == []

    def test_scores_math(self) -> None:
        """评分规则：目标达成 2/3→67；话术 40+10*1 命中→50；策略零让价→90。"""
        engine = make_engine()
        sid = engine.start_session("C-91", "sc-bargain-moq", difficulty=3)["session_id"]
        engine.reply(sid, VALUE_MSG)
        report = engine.generate_debrief(sid)
        assert report.scores == {"goal_achievement": 67, "talk_skill": 50, "strategy": 90}

    def test_breakdown_suggestions_mention_recover(self) -> None:
        """breakdown 复盘：建议包含情绪安抚三步（危机处理口径）。"""
        engine = make_engine()
        sid = engine.start_session("C-92", "sc-bargain-moq", difficulty=3)["session_id"]
        engine.reply(sid, BARGAIN_MSG)  # 耐心 13-5-8=0 → breakdown
        report = engine.generate_debrief(sid)
        assert report.verdict == "breakdown"
        assert any("安抚" in s for s in report.suggestions)
        assert len(report.suggestions) == 3

    def test_timeout_low_scores(self) -> None:
        """边界：零价值/零让价的超时对话 → 话术 40、目标 0（评分下界）。"""
        engine = make_engine(max_rounds=2)
        sid = engine.start_session("C-91", "sc-bargain-moq", difficulty=3)["session_id"]
        engine.reply(sid, NEUTRAL_MSG)
        engine.reply(sid, NEUTRAL_MSG)
        report = engine.generate_debrief(sid)
        assert report.verdict == "timeout" and report.rounds == 2
        assert report.scores["talk_skill"] == 40
        assert report.scores["goal_achievement"] == 0

    def test_report_persisted_and_gettable(self) -> None:
        """落库：生成即写入复盘存储，get 返回同一报告（exercise.review_report 形态）。"""
        engine = make_engine()
        sid = engine.start_session("C-91", "sc-bargain-moq", difficulty=3)["session_id"]
        engine.reply(sid, VALUE_MSG)
        report = engine.generate_debrief(sid)
        assert engine.debrief.store.get(sid) == report


class TestDebriefErrors:
    """异常 + 降级：复盘生成失败路径。"""

    def test_unknown_session_and_not_ended(self) -> None:
        """未知会话 → SimSessionNotFound；未结束会话 → SimBadRequest。"""
        engine = make_engine()
        with pytest.raises(SimSessionNotFound):
            engine.generate_debrief("sim-not-exist")
        sid = engine.start_session("C-91", "sc-bargain-moq", difficulty=3)["session_id"]
        with pytest.raises(SimBadRequest):
            engine.generate_debrief(sid)  # 未结束

    def test_llm_failure_degraded_report(self) -> None:
        """降级：LLM 故障 → 规则兜底报告（degraded=true）仍落库可取。"""
        class BrokenDebriefLlm:
            def generate_debrief(self, **kwargs) -> dict:
                raise SimulatorLlmError("复盘模型超时（模拟故障）")

        engine = make_engine(debrief_llm=BrokenDebriefLlm())
        sid = engine.start_session("C-91", "sc-bargain-moq", difficulty=3)["session_id"]
        engine.reply(sid, VALUE_MSG)
        report = engine.generate_debrief(sid)
        assert report.degraded is True
        assert report.model == "rule-fallback"
        assert len(report.suggestions) == 3
        assert engine.debrief.store.get(sid) == report

    def test_report_overwrite_idempotent(self) -> None:
        """幂等：同会话重复生成 → 报告覆盖写，存储结果一致。"""
        engine = make_engine()
        sid = engine.start_session("C-91", "sc-bargain-moq", difficulty=3)["session_id"]
        engine.reply(sid, VALUE_MSG)
        first = engine.generate_debrief(sid)
        second = engine.generate_debrief(sid)
        # 覆盖写：存储以最新一次生成为准（created_at 之外的载荷确定性一致）
        assert first.model_dump(exclude={"created_at"}) == second.model_dump(exclude={"created_at"})
        assert engine.debrief.store.get(sid) == second

    def test_scores_range_validated(self) -> None:
        """异常：scores 越界（>100）在 DebriefReport 模型层拒绝。"""
        with pytest.raises(ValidationError):
            DebriefReport(
                session_id="sim-x", persona_id="C-01", scenario_id="sc-bargain-moq",
                verdict="deal", rounds=1, goal_achievement=[],
                scores={"goal_achievement": 101, "talk_skill": 0, "strategy": 0},
                strengths=[], weaknesses=[], suggestions=[], knowledge_refs=[],
            )
