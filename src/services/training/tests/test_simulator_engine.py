# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""模拟客户引擎单元测试（FR-TR-002，SR-TR-002 §6.2/§9；迭代 1.2 TDD 阶段①）。

五类场景覆盖（开发规范 §9.1）：
- 等价类：start/reply 正常路径、状态机三条规则（砍价/价值/激怒）、难度档位话术差异；
- 边界：立场三态阈值、耐心归零、成交阈值 0.75 两侧、轮数上限、难度动态调节钳制；
- 异常：未知会话/空内容/难度越界/结束后回复；
- 并发：双会话隔离 + 同会话并发回复串行化 + 幂等重放（request_id）；
- 降级：LLM 故障 → 规则式短回复 degraded=true，状态机照常演化。

验收 KPI 映射（测试即验收证据）：SRS §16.2「模拟客户逼真度 ≥80%（老销售盲测）」
在 demo 口径下以「结构逼真度检查」替代（状态演化/差异化/结束三分支覆盖）→
TestStructuralRealism 三用例。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from services.training.core.simulator.engine import (
    BREAKDOWN_LINE,
    DEAL_LINE,
    DEFAULT_MAX_ROUNDS,
    FALLBACK_REPLY,
    TONE_HARD,
    TONE_SOFT,
    CustomerState,
    CustomerStateMachine,
    FakeSimulatorLlm,
    SimBadRequest,
    SimSessionNotFound,
    SimulatorEngine,
    SimulatorLlmError,
    stance_label,
)
from services.training.core.simulator.persona import (
    BUILT_IN_SCENARIOS,
    PersonaRegistry,
    PersonaSpec,
)
from services.training.core.simulator.store import InMemorySimSessionStore

SC = BUILT_IN_SCENARIOS["sc-bargain-moq"]
VALUE_MSG = "请介绍一下你们的认证、交期和质量参数。"
BARGAIN_MSG = "太贵了，给我最低价，能便宜一点吗？"
ANGER_MSG = "你们这是骗人的垃圾报价。"
NEUTRAL_MSG = "你好，请继续。"
ALL_PERSONA_IDS = [f"C-0{i}" for i in range(1, 7)] + ["C-07", "C-08", "C-09", "C-10"]


def make_spec(persona_id: str = "C-90", state: tuple[float, int, int] = (0.15, 0, 100),
              difficulty: int = 3, points: tuple[str, ...] = ("价值传递",)) -> PersonaSpec:
    """构造测试 persona（C-90 段避开内置编号；state=(立场,情绪,耐心)）。"""
    return PersonaSpec(
        persona_id="C-90",
        name="测试客户型",
        profile="用于单测的合成客户画像",
        behavior_traits=["特征甲", "特征乙"],
        hidden_needs="真实在意交期稳定性与返修率",
        difficulty_base=difficulty,
        state_init={"price_stance": state[0], "emotion": state[1], "patience": state[2]},
        assessment_points=list(points),
    )


def make_engine(llm=None, *, max_rounds: int = DEFAULT_MAX_ROUNDS,
                registry: PersonaRegistry | None = None) -> SimulatorEngine:
    """标准装配：Fake LLM + 内存存储 + 全 10 类型注册表。"""
    return SimulatorEngine(
        llm=llm if llm is not None else FakeSimulatorLlm(),
        store=InMemorySimSessionStore(),
        registry=registry if registry is not None else PersonaRegistry.with_demo_extensions(),
        max_rounds=max_rounds,
    )


class TestStateMachineRules:
    """等价类：确定性状态机三条规则（SR-TR-002 §6.2，难度系数 k=0.6+0.25d）。"""

    def test_bargain_rule(self) -> None:
        """砍价消息（难度3，k=1.35）：立场 -0.0675；耐心 = 100-5(砍价)-8(自然) = 87。"""
        machine = CustomerStateMachine(CustomerState(0.15, 0, 100), 3)
        state = machine.update(BARGAIN_MSG)
        assert state.price_stance == pytest.approx(0.15 - 0.05 * 1.35)
        assert state.patience == 87
        assert state.emotion == 0

    def test_value_rule(self) -> None:
        """价值消息（难度3）：立场 +0.10/1.35；情绪 +1；耐心仅自然衰减 8。"""
        machine = CustomerStateMachine(CustomerState(0.15, 0, 100), 3)
        state = machine.update(VALUE_MSG)
        assert state.price_stance == pytest.approx(0.15 + 0.10 / 1.35)
        assert state.emotion == 1
        assert state.patience == 92

    def test_anger_rule(self) -> None:
        """激怒消息：情绪逐轮 -1；情绪触底 -2 后耐心额外扣 int(8k+0.5)=11。"""
        machine = CustomerStateMachine(CustomerState(0.15, 0, 100), 3)
        first = machine.update(ANGER_MSG)
        assert first.emotion == -1 and first.patience == 92  # 未触底无额外扣
        second = machine.update(ANGER_MSG)
        assert second.emotion == -2 and second.patience == 73  # 92-8-11

    def test_natural_decay_by_difficulty(self) -> None:
        """边界：难度系数决定自然衰减——d1 每轮 5，d5 每轮 11；立场不受中性消息影响。"""
        d1 = CustomerStateMachine(CustomerState(0.15, 0, 100), 1).update(NEUTRAL_MSG)
        d5 = CustomerStateMachine(CustomerState(0.15, 0, 100), 5).update(NEUTRAL_MSG)
        assert d1.patience == 95 and d5.patience == 89
        assert d1.price_stance == d5.price_stance == pytest.approx(0.15)

    def test_mixed_rule(self) -> None:
        """同一消息同时命中砍价+价值词：两条规则都生效（确定性叠加）。"""
        machine = CustomerStateMachine(CustomerState(0.15, 0, 100), 3)
        state = machine.update("价格太贵了，不过你们的质量认证怎么样？")
        assert state.price_stance == pytest.approx(0.15 - 0.05 * 1.35 + 0.10 / 1.35)
        assert state.emotion == 1 and state.patience == 87

    def test_clamp_bounds(self) -> None:
        """边界钳制：立场上限 1.0、耐心下限 0、情绪上限 +2。"""
        machine = CustomerStateMachine(CustomerState(0.98, 2, 3), 1)
        state = machine.update(VALUE_MSG)
        assert state.price_stance == 1.0
        assert state.emotion == 2
        assert state.patience == 0

    def test_stance_label_boundaries(self) -> None:
        """边界：议价立场三态阈值——≤0.35 坚持 / (0.35,0.75) 松动 / ≥0.75 底线。"""
        assert stance_label(0.0) == "坚持"
        assert stance_label(0.35) == "坚持"
        assert stance_label(0.36) == "松动"
        assert stance_label(0.74) == "松动"
        assert stance_label(0.75) == "底线"
        assert stance_label(1.0) == "底线"


class TestStartSession:
    """等价类：start_session 正常路径。"""

    def test_start_payload_and_opening(self) -> None:
        """开场情境即首条 customer 消息（seq=1，带初始状态快照），rounds=0。"""
        engine = make_engine()
        res = engine.start_session("C-01", "sc-bargain-moq")
        assert res["session_id"].startswith("sim-")
        assert res["persona_id"] == "C-01" and res["scenario_id"] == "sc-bargain-moq"
        assert res["opening"] == SC.opening
        assert res["difficulty"] == 2 and res["max_rounds"] == 20
        assert res["state"] == {
            "price_stance": 0.15, "emotion": 0, "patience": 100, "stance": "坚持", "difficulty": 2
        }
        session = engine.store.load(res["session_id"])
        assert session is not None and session.rounds == 0
        opening_msg = session.transcript[0]
        assert opening_msg["seq"] == 1 and opening_msg["role"] == "customer"
        assert opening_msg["content"] == SC.opening
        assert opening_msg["state_snapshot"]["patience"] == 100

    def test_start_difficulty_default_and_object_input(self) -> None:
        """难度缺省取 persona.difficulty_base；显式覆盖生效；可直接传 PersonaSpec 对象。"""
        engine = make_engine()
        assert engine.start_session("C-01", "sc-bargain-moq")["difficulty"] == 2
        assert engine.start_session("C-01", "sc-bargain-moq", difficulty=5)["difficulty"] == 5
        by_obj = engine.start_session(make_spec(difficulty=4), SC)
        assert by_obj["persona_id"] == "C-90" and by_obj["difficulty"] == 4


class TestReplyLoop:
    """等价类：reply 对话循环与 Fake LLM 状态演绎。"""

    def test_reply_shape_and_transcript(self) -> None:
        """回复六键齐全；一轮追加 trainee+customer 两条消息（seq 2/3，快照挂 customer）。"""
        engine = make_engine()
        sid = engine.start_session("C-01", "sc-bargain-moq", difficulty=3)["session_id"]
        rep = engine.reply(sid, VALUE_MSG)
        assert set(rep) == {"customer_reply", "state", "round", "ended", "ended_reason", "degraded"}
        assert set(rep["state"]) == {"price_stance", "emotion", "patience", "stance", "difficulty"}
        assert rep["round"] == 1 and rep["ended"] is False
        assert rep["ended_reason"] is None and rep["degraded"] is False
        session = engine.store.load(sid)
        assert session is not None and len(session.transcript) == 3
        assert session.transcript[1]["role"] == "trainee" and session.transcript[1]["seq"] == 2
        assert session.transcript[2]["role"] == "customer" and session.transcript[2]["seq"] == 3
        assert session.transcript[2]["state_snapshot"]["patience"] == 92

    def test_deal_detection_and_reply_line(self) -> None:
        """成交判定：立场≥0.75 且情绪≥1 → ended=deal，Fake 演绎成交话术。"""
        engine = make_engine()
        sid = engine.start_session(make_spec(state=(0.70, 0, 100)), SC, difficulty=3)["session_id"]
        rep = engine.reply(sid, VALUE_MSG)  # 立场 0.70+0.074=0.774，情绪 1
        assert rep["ended"] is True and rep["ended_reason"] == "deal"
        assert rep["customer_reply"] == DEAL_LINE

    def test_breakdown_detection_and_reply_line(self) -> None:
        """谈崩判定：耐心归零 → ended=breakdown，Fake 演绎谈崩话术（耐心尽→谈崩话术）。"""
        engine = make_engine()
        sid = engine.start_session(make_spec(state=(0.15, 0, 13)), SC, difficulty=3)["session_id"]
        rep = engine.reply(sid, BARGAIN_MSG)  # 13-5-8=0
        assert rep["ended"] is True and rep["ended_reason"] == "breakdown"
        assert rep["state"]["patience"] == 0
        assert rep["customer_reply"] == BREAKDOWN_LINE

    def test_timeout_detection(self) -> None:
        """轮数上限：max_rounds=2 时第 2 轮结束 reason=timeout，第 1 轮未结束。"""
        engine = make_engine(max_rounds=2)
        sid = engine.start_session("C-01", "sc-bargain-moq")["session_id"]
        first = engine.reply(sid, NEUTRAL_MSG)
        assert first["ended"] is False
        second = engine.reply(sid, NEUTRAL_MSG)
        assert second["ended"] is True and second["ended_reason"] == "timeout"

    def test_ending_priority(self) -> None:
        """边界：结束判定优先级 成交 > 谈崩 > 超时。"""
        engine = make_engine(max_rounds=1)
        # 成交与谈崩同轮满足 → deal 优先（耐心归零但立场/情绪已达标）
        deal_sid = engine.start_session(make_spec("C-91", state=(0.90, 2, 5)), SC, difficulty=3)["session_id"]
        assert engine.reply(deal_sid, VALUE_MSG)["ended_reason"] == "deal"
        # 谈崩与超时同轮满足（rounds==max_rounds==1 且耐心归零）→ breakdown 优先
        brk_sid = engine.start_session(make_spec("C-92", state=(0.15, 0, 13)), SC, difficulty=3)["session_id"]
        assert engine.reply(brk_sid, BARGAIN_MSG)["ended_reason"] == "breakdown"

    def test_reply_after_ended_rejected(self) -> None:
        """异常：已结束会话再回复 → SimBadRequest（SimAlreadyEnded）。"""
        engine = make_engine(max_rounds=1)
        sid = engine.start_session("C-01", "sc-bargain-moq")["session_id"]
        engine.reply(sid, NEUTRAL_MSG)
        with pytest.raises(SimBadRequest):
            engine.reply(sid, NEUTRAL_MSG)

    def test_reply_unknown_session_and_empty_content(self) -> None:
        """异常：未知会话 → SimSessionNotFound；空/超长内容 → SimBadRequest。"""
        engine = make_engine()
        with pytest.raises(SimSessionNotFound):
            engine.reply("sim-not-exist", VALUE_MSG)
        sid = engine.start_session("C-01", "sc-bargain-moq")["session_id"]
        with pytest.raises(SimBadRequest):
            engine.reply(sid, "   ")
        with pytest.raises(SimBadRequest):
            engine.reply(sid, "超" * 501)


class TestDifficulty:
    """等价类 + 边界：难度参数对回复与状态演化的调节。"""

    def test_difficulty_tone_changes_reply(self) -> None:
        """同一 persona/消息下，难度 1/3/5 的 Fake 回复可断言行为差异（语气档位）。"""
        engine = make_engine()
        r1 = engine.reply(
            engine.start_session("C-01", "sc-bargain-moq", difficulty=1)["session_id"], NEUTRAL_MSG
        )["customer_reply"]
        r3 = engine.reply(
            engine.start_session("C-01", "sc-bargain-moq", difficulty=3)["session_id"], NEUTRAL_MSG
        )["customer_reply"]
        r5 = engine.reply(
            engine.start_session("C-01", "sc-bargain-moq", difficulty=5)["session_id"], NEUTRAL_MSG
        )["customer_reply"]
        assert r1.endswith(TONE_SOFT)
        assert r5.endswith(TONE_HARD)
        assert TONE_SOFT not in r3 and TONE_HARD not in r3
        assert len({r1, r3, r5}) == 3

    def test_start_difficulty_out_of_range(self) -> None:
        """异常：难度 0/6 越界 → SimBadRequest。"""
        engine = make_engine()
        for bad in (0, 6):
            with pytest.raises(SimBadRequest):
                engine.start_session("C-01", "sc-bargain-moq", difficulty=bad)

    def test_drift_up_after_five_good_rounds(self) -> None:
        """难度动态调节：连续 5 轮高情绪（均值>0.5）→ 有效难度 3→4（SR-TR-002 §6.2）。"""
        engine = make_engine()
        sid = engine.start_session("C-01", "sc-bargain-moq", difficulty=3)["session_id"]
        for _ in range(4):
            last = engine.reply(sid, VALUE_MSG)
        assert last["state"]["difficulty"] == 3
        last = engine.reply(sid, VALUE_MSG)
        assert last["state"]["difficulty"] == 4

    def test_drift_clamp_at_five(self) -> None:
        """边界：难度 5 时动态调节不越上界（钳制在 5）。"""
        engine = make_engine()
        sid = engine.start_session("C-01", "sc-bargain-moq", difficulty=5)["session_id"]
        for _ in range(5):
            last = engine.reply(sid, VALUE_MSG)
        assert last["state"]["difficulty"] == 5

    def test_drift_down_after_five_angry_rounds(self) -> None:
        """难度动态调节：连续 5 轮低情绪（均值<-0.5）→ 有效难度 3→2。"""
        engine = make_engine()
        sid = engine.start_session("C-01", "sc-bargain-moq", difficulty=3)["session_id"]
        for _ in range(5):
            last = engine.reply(sid, ANGER_MSG)
        assert last["state"]["difficulty"] == 2
        assert last["ended"] is False  # 耐心 16 未归零，会话存活


class TestDegradation:
    """降级：LLM 故障回退路径（SR-TR-002 §8：规则式短回复 + degraded 标记）。"""

    def test_llm_failure_degrades_reply(self) -> None:
        """LLM 抛 SimulatorLlmError → 规则式短回复、degraded=true、状态机照常演化。"""
        class BrokenLlm:
            def __init__(self) -> None:
                self.calls = 0

            def generate_reply(self, **kwargs) -> str:
                self.calls += 1
                raise SimulatorLlmError("模型超时（模拟故障）")

        broken = BrokenLlm()
        engine = make_engine(llm=broken)
        sid = engine.start_session("C-01", "sc-bargain-moq", difficulty=3)["session_id"]
        rep = engine.reply(sid, VALUE_MSG)
        assert rep["customer_reply"] == FALLBACK_REPLY
        assert rep["degraded"] is True
        assert rep["state"]["patience"] == 92  # 状态机不受 LLM 故障影响
        assert rep["ended"] is False
        assert broken.calls == 1


class TestConcurrencyAndIdempotency:
    """并发 + 幂等：会话隔离、串行化、request_id 重放（SR-TR-002 §8）。"""

    def test_two_sessions_isolated(self) -> None:
        """并发会话隔离：两会话交替推进，状态互不串扰。"""
        engine = make_engine()
        s1 = engine.start_session("C-01", "sc-bargain-moq", difficulty=3)["session_id"]
        s2 = engine.start_session("C-01", "sc-bargain-moq", difficulty=3)["session_id"]
        assert s1 != s2
        for _ in range(3):
            engine.reply(s1, VALUE_MSG)
        engine.reply(s2, NEUTRAL_MSG)
        st1 = engine.store.load(s1).state
        st2 = engine.store.load(s2).state
        assert st1.patience == 76 and st1.price_stance == pytest.approx(0.15 + 3 * 0.10 / 1.35)
        assert st2.patience == 92 and st2.price_stance == pytest.approx(0.15)

    def test_parallel_replies_serialized(self) -> None:
        """并发：同会话多线程回复被引擎锁串行化——轮数/转录/状态一致（结果与顺序无关）。"""
        engine = make_engine()
        sid = engine.start_session("C-01", "sc-bargain-moq", difficulty=3)["session_id"]
        msgs = [VALUE_MSG, BARGAIN_MSG, VALUE_MSG, BARGAIN_MSG]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda m: engine.reply(sid, m), msgs))
        session = engine.store.load(sid)
        assert session.rounds == 4 and len(session.transcript) == 9
        # 4 轮自然衰减 4*8 + 2 次砍价 2*5 = 42（加减线性，与提交顺序无关）
        assert session.state.patience == 58

    def test_start_idempotent_same_request_id(self) -> None:
        """幂等：start 同 request_id 重放返回原会话，不重复建会话。"""
        engine = make_engine()
        first = engine.start_session("C-01", "sc-bargain-moq", request_id="req-start-1")
        second = engine.start_session("C-01", "sc-bargain-moq", request_id="req-start-1")
        assert first == second
        assert engine.store.count() == 1

    def test_reply_idempotent_replay(self) -> None:
        """幂等：reply 同 request_id 重放返回原响应，LLM 只调一次、状态只推进一轮。"""
        engine = make_engine()
        sid = engine.start_session("C-01", "sc-bargain-moq", difficulty=3)["session_id"]
        first = engine.reply(sid, VALUE_MSG, request_id="msg-1")
        second = engine.reply(sid, VALUE_MSG, request_id="msg-1")
        assert first == second
        assert engine.llm.calls == 1
        session = engine.store.load(sid)
        assert session.rounds == 1 and len(session.transcript) == 3


class TestStructuralRealism:
    """结构逼真度检查（SRS §16.2 逼真度 KPI 的 demo 替代口径：正式盲测属人评）。

    demo 下以可自动断言的结构指标替代：①状态随轮次演化；②10 类型回复差异化；
    ③成交/谈崩/超时三分支全部可达。
    """

    def test_realism_state_evolution(self) -> None:
        """①状态演化：5 轮价值消息下耐心严格递减、立场上升、情绪抬升。"""
        engine = make_engine()
        sid = engine.start_session("C-01", "sc-bargain-moq", difficulty=3)["session_id"]
        patience_path = [100]
        for _ in range(5):
            rep = engine.reply(sid, VALUE_MSG)
            patience_path.append(rep["state"]["patience"])
        assert patience_path == [100, 92, 84, 76, 68, 60]
        session = engine.store.load(sid)
        assert session.state.price_stance > 0.15
        assert session.state.emotion == 2

    def test_realism_persona_differentiation(self) -> None:
        """②差异化：10 类型（6 内置 + 4 扩展注册）对同一消息回复两两不同。"""
        engine = make_engine()
        replies = []
        for persona_id in ALL_PERSONA_IDS:
            sid = engine.start_session(persona_id, "sc-inquiry-first", difficulty=3)["session_id"]
            replies.append(engine.reply(sid, NEUTRAL_MSG)["customer_reply"])
        assert len(set(replies)) == 10

    def test_realism_three_endings_covered(self) -> None:
        """③三分支：成交/谈崩/超时三种结束判定全部可达且结论正确。"""
        engine = make_engine()
        deal_sid = engine.start_session(make_spec("C-91", state=(0.70, 0, 100)), SC, difficulty=3)["session_id"]
        engine.reply(deal_sid, VALUE_MSG)
        brk_sid = engine.start_session(make_spec("C-92", state=(0.15, 0, 13)), SC, difficulty=3)["session_id"]
        engine.reply(brk_sid, BARGAIN_MSG)
        short_engine = make_engine(max_rounds=2)
        timeout_sid = short_engine.start_session("C-01", "sc-bargain-moq")["session_id"]
        short_engine.reply(timeout_sid, NEUTRAL_MSG)
        short_engine.reply(timeout_sid, NEUTRAL_MSG)
        reasons = {
            engine.store.load(deal_sid).ended_reason,
            engine.store.load(brk_sid).ended_reason,
            short_engine.store.load(timeout_sid).ended_reason,
        }
        assert reasons == {"deal", "breakdown", "timeout"}
