# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""模拟客户对话引擎（FR-TR-002，SR-TR-002 §6.2 状态机与对话循环）。

架构规则（SR-TR-002 §2 决策 2）：**状态机外置**——议价立场/情绪/耐心由确定性
规则机维护（可解释、可回归测试），LLM 只负责「把状态演出来」，避免模型自由
发挥导致人设漂移。

一轮 reply 的顺序（与 SR-TR-002 §6.2 的偏差标注，demo 口径）：
    ① 状态机先按学员消息更新（update-then-generate）
    ② LLM 按更新后的状态演绎回复（谈崩话术可在耐心归零轮自然出现）
    ③ 结束判定（成交 / 谈崩 / 轮数耗尽）→ 转录落存储
实现方案采用「先生成后更新」源于 SSE 流式约束；本迭代为普通 JSON 非流式，
先更新后生成使回复与状态严格一致，回归断言更直接。

难度动态调节：每 DRIFT_ROUNDS 轮按最近窗口情绪均值在有效难度 ±1 内微调
（钳制 1~5），与 SR-TR-002 §6.2 _effective_difficulty 同口径。

降级（SR-TR-002 §8）：LLM 故障 → 重试策略随联调迭代接入，本迭代直接降级为
规则式短回复（FALLBACK_REPLY，"please send me your best price" 类口头禅），
degraded=true，状态机照常演化，会话不中断。

并发与幂等（SR-TR-002 §8）：引擎级锁串行化同进程并发回复（生产形态为 Redis
SETNX 锁）；request_id 幂等重放返回首次响应（重放不推进状态、不重复调 LLM）。

demo 口径：成交判定 price_stance≥0.75 等价于「学员报价 ≥ 客户底线」的客户
感知口径（SR-TR-002 §6.2 _check_ending 同阈值）——不做真实金额解析。
"""
from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from services.training.core.simulator.persona import (
    PersonaNotFoundError,
    PersonaRegistry,
    PersonaSpec,
    ScenarioCatalog,
    ScenarioNotFoundError,
    ScenarioSpec,
    build_system_prompt,
)

from services.training.core.simulator.store import START_REPLAY_SCOPE

if TYPE_CHECKING:  # store 协议仅类型期引用（运行期引擎仅依赖其常量，鸭子类型注入）
    from services.training.core.simulator.store import SimSessionStore

# --------------------------------------------------------------------------- 领域常量

#: 砍价词（学员消息命中 → 无价值铺垫直接让价 → 客户立场更硬、耐心额外扣减）
BARGAIN_WORDS: tuple[str, ...] = (
    "折扣", "便宜", "低价", "降价", "让价", "最低价", "太贵", "discount", "cheaper", "best price",
)
#: 价值词（命中隐藏诉求方向：认证/交期/质量/参数 → 立场抬升、情绪 +1）
VALUE_WORDS: tuple[str, ...] = (
    "认证", "证书", "交期", "质量", "参数", "品质", "工艺", "标准", "检测",
    "certificate", "quality", "delivery", "spec",
)
#: 激怒词（情绪 -1；情绪触底 -2 后耐心额外扣减，最终由耐心归零触发谈崩）
ANGER_WORDS: tuple[str, ...] = ("骗", "垃圾", "投诉", "敷衍", "失望", "滚", "scam", "rubbish")

#: 成交阈值（SR-TR-002 §6.2：stance≥0.75 且 emotion≥1）
DEAL_STANCE_THRESHOLD: float = 0.75
DEAL_EMOTION_THRESHOLD: int = 1
#: 议价立场三态阈值：≤0.35 坚持 / (0.35, 0.75) 松动 / ≥0.75 底线
STANCE_FIRM_MAX: float = 0.35

#: 单场最大轮数缺省（SR-TR-002 §7 sim.max_rounds）
DEFAULT_MAX_ROUNDS: int = 20
#: 难度动态调节评估周期（SR-TR-002 §7 sim.difficulty_drift_rounds）
DRIFT_ROUNDS: int = 5
#: 动态调节触发阈值（最近 DRIFT_ROUNDS 轮情绪均值）
DRIFT_UP_AVG: float = 0.5
DRIFT_DOWN_AVG: float = -0.5

#: 学员消息长度上限（与 FR-TR-001 40001 口径一致）
MAX_REPLY_LEN: int = 500

#: LLM 降级话术（SR-TR-002 §8："please send me your best price" 类口头禅）
FALLBACK_REPLY: str = "OK, please send me your best price and catalog first."

# --------------------------------------------------------------------------- Fake 演绎话术（确定性）

#: 谈崩话术（耐心尽 → 谈崩）
BREAKDOWN_LINE: str = "算了，这样谈下去没有意义，这单我不考虑了。"
#: 恼怒话术（情绪触底）
ANGRY_LINE: str = "你这说法让我很失望，我得重新考虑这次合作。"
#: 成交话术（立场≥0.75 且情绪≥1）
DEAL_LINE: str = "好，价格可以接受，把 PI 发过来，我们尽快安排定金。"
#: 接近成交（立场≥0.75 但情绪未达标）
NEAR_DEAL_LINE: str = "价格差不多能接受了，但交期和付款条款还要再确认一下。"
#: 松动（立场≥0.5）
FLEX_LINE: str = "你的方案有点意思，不过我还要再对比一下其他供应商。"
#: 难度语气档位（≥4 逼紧 / ≤2 宽松；Fake 下可断言行为差异）
TONE_HARD: str = "别绕弯子，直接说重点。"
TONE_SOFT: str = "朋友，我们慢慢聊，不着急。"


def stance_label(price_stance: float) -> str:
    """议价立场数值 → 三态标签（坚持/松动/底线）。"""
    if price_stance <= STANCE_FIRM_MAX:
        return "坚持"
    if price_stance < DEAL_STANCE_THRESHOLD:
        return "松动"
    return "底线"


def has_any_word(text: str, words: Sequence[str]) -> bool:
    """文本是否命中任一关键词（英文词统一小写比较；状态机与复盘共用口径）。"""
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


def _half_up(value: float) -> int:
    """四舍五入（half-up，规避 Python banker's rounding 的非预期取整）。"""
    return int(value + 0.5)


# --------------------------------------------------------------------------- 异常体系


class SimulatorError(RuntimeError):
    """模拟客户链路业务异常基类（未被特化的异常 → API 层映射 50002）。"""


class SimBadRequest(SimulatorError):
    """入参非法（空内容/超长/难度越界/已结束）→ API 层映射 40001。"""


class SimAlreadyEnded(SimBadRequest):
    """会话已结束仍尝试回复（40001 语义，message 携带结束原因）。"""


class SimNotFoundError(SimulatorError):
    """资源不存在基类 → API 层映射 40401。"""


class SimSessionNotFound(SimNotFoundError):
    """未知会话。"""


class SimulatorLlmError(SimulatorError):
    """LLM 下游故障（实现方须将底层异常包装为本类型）→ 引擎降级路径。"""


# --------------------------------------------------------------------------- 状态机


@dataclass
class CustomerState:
    """客户状态（SR-TR-002 §6.2 CustomerState；stance 为 price_stance 派生标签）。"""

    price_stance: float = 0.15
    emotion: int = 0
    patience: int = 100

    @classmethod
    def from_init(cls, init: Any) -> "CustomerState":
        """由 PersonaSpec.state_init（StateInit 模型）构造。"""
        return cls(price_stance=float(init.price_stance), emotion=int(init.emotion), patience=int(init.patience))

    def to_dict(self) -> dict[str, Any]:
        """快照形态（转录 state_snapshot / 响应 state 段共用）。"""
        return {
            "price_stance": self.price_stance,
            "emotion": self.emotion,
            "patience": self.patience,
            "stance": stance_label(self.price_stance),
        }


class CustomerStateMachine:
    """确定性状态机：LLM 负责演绎，本机负责决策——保证回归可测。

    规则（难度系数 k = 0.6 + 0.25×difficulty；整数衰减取 half-up）：
    1. 砍价词 → price_stance -= 0.05k；耐心额外 -= round(4k)（被砍价加速消耗）；
    2. 价值词 → price_stance += 0.10/k；情绪 +1（上限 +2）；
    3. 激怒词 → 情绪 -1（下限 -2）；情绪触底 -2 后耐心额外 -= round(8k)；
    4. 每轮自然衰减：耐心 -= round(6k)（难度越高越快）；
    5. 钳制：stance∈[0,1]、patience∈[0,100]、emotion∈[-2,2]。
    """

    def __init__(self, state: CustomerState, difficulty: int) -> None:
        if not 1 <= difficulty <= 5:
            raise SimBadRequest(f"难度 {difficulty} 越界（1-5）")
        self.state = state
        self.difficulty = difficulty

    def k(self) -> float:
        """难度系数（1→0.85，3→1.35，5→1.85）。"""
        return 0.6 + self.difficulty * 0.25

    def update(self, trainee_msg: str) -> CustomerState:
        """按学员消息更新状态并返回（原地变更，供引擎持久化快照）。"""
        state, k = self.state, self.k()
        if has_any_word(trainee_msg, BARGAIN_WORDS):
            state.price_stance -= 0.05 * k
            state.patience -= _half_up(4 * k)
        if has_any_word(trainee_msg, VALUE_WORDS):
            state.price_stance += 0.10 / k
            state.emotion = min(2, state.emotion + 1)
        if has_any_word(trainee_msg, ANGER_WORDS):
            state.emotion = max(-2, state.emotion - 1)
            if state.emotion <= -2:
                state.patience -= _half_up(8 * k)
        state.patience -= _half_up(6 * k)  # 每轮自然衰减
        state.price_stance = max(0.0, min(1.0, state.price_stance))
        state.patience = max(0, min(100, state.patience))
        state.emotion = max(-2, min(2, state.emotion))
        return state


# --------------------------------------------------------------------------- 会话与转录


@dataclass
class SimSession:
    """模拟会话（SR-TR-002 §4 exercise 行的进程内形态：kind='simulated'）。

    persona/scenario 为开场时解析后的规格实例（支持直接传对象开场的调用形态，
    同时免去每轮重复查注册表）；persona_id/scenario_id 保留为落库键。
    """

    session_id: str
    persona_id: str
    scenario_id: str
    persona: PersonaSpec
    scenario: ScenarioSpec
    difficulty: int
    """起始难度（start_session 口径）。"""
    effective_difficulty: int
    """有效难度（动态调节后的当前值，终值回写 exercise.difficulty）。"""
    max_rounds: int
    state: CustomerState
    transcript: list[dict[str, Any]]
    """转录：{seq, role: customer|trainee, content, state_snapshot}；seq=1 为开场。"""
    rounds: int = 0
    """已完成对话轮数（一条学员消息为一轮）。"""
    emotion_history: list[int] = field(default_factory=list)
    """每轮更新后的情绪序列（难度动态调节窗口）。"""
    ended: bool = False
    ended_reason: str | None = None
    """deal | breakdown | timeout。"""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# --------------------------------------------------------------------------- LLM 协议与确定性桩


class SimulatorLlm(Protocol):
    """模拟客户 LLM 协议（可插拔；引擎仅依赖本协议，不绑定实现）。

    生产形态注入 DeepSeek-V3 客户端（RFP §3.2 指定栈，System Prompt 经
    build_system_prompt 组装后经 system 参数下发）；demo/测试注入
    :class:`FakeSimulatorLlm` 确定性桩。实现须把底层故障包装为
    :class:`SimulatorLlmError` 抛出（引擎据此走降级路径）。
    """

    def generate_reply(
        self,
        *,
        system: str,
        persona: PersonaSpec,
        scenario: ScenarioSpec,
        state: Mapping[str, Any],
        transcript: Sequence[Mapping[str, Any]],
        trainee_msg: str,
        difficulty: int,
    ) -> str:
        """按（persona 模板 + 状态 + 场景 + 历史）生成一条客户回复。"""
        ...


class FakeSimulatorLlm:
    """确定性客户扮演桩（demo/测试，零网络请求）。

    生成规则（按 persona 模板 + 状态演绎中文回复）：
    - 耐心尽 → 谈崩话术；情绪触底 → 恼怒话术；立场≥0.75 且情绪≥1 → 成交话术；
      立场≥0.75 → 接近成交；立场≥0.5 → 松动；否则按考核点 + 行为特征轮转模板；
    - 难度档位语气：≥4 逼紧 / ≤2 宽松（难度对回复的调节在 Fake 下可断言）；
    - 记录 calls / last_system / last_state 供测试断言 Prompt 组装形态。
    """

    def __init__(self) -> None:
        self.calls: int = 0
        self.last_system: str = ""
        self.last_state: dict[str, Any] | None = None

    def generate_reply(
        self,
        *,
        system: str,
        persona: PersonaSpec,
        scenario: ScenarioSpec,
        state: Mapping[str, Any],
        transcript: Sequence[Mapping[str, Any]],
        trainee_msg: str,
        difficulty: int,
    ) -> str:
        """确定性生成（round 由转录长度推导：已完成客户回复数）。"""
        self.calls += 1
        self.last_system = system
        self.last_state = dict(state)
        patience = int(state["patience"])
        emotion = int(state["emotion"])
        stance = float(state["price_stance"])
        if patience <= 0:
            return BREAKDOWN_LINE
        if emotion <= -2:
            return ANGRY_LINE
        if stance >= DEAL_STANCE_THRESHOLD and emotion >= DEAL_EMOTION_THRESHOLD:
            return DEAL_LINE
        if stance >= DEAL_STANCE_THRESHOLD:
            return NEAR_DEAL_LINE
        if stance >= 0.5:
            return FLEX_LINE
        round_index = max(0, (len(transcript) - 1) // 2)
        point = persona.assessment_points[round_index % len(persona.assessment_points)]
        trait = persona.behavior_traits[round_index % len(persona.behavior_traits)]
        tone = TONE_HARD if difficulty >= 4 else (TONE_SOFT if difficulty <= 2 else "")
        return f"我最看重的是{point}这一块。尤其是{trait}这一点。{tone}"


# --------------------------------------------------------------------------- 引擎


class SimulatorEngine:
    """模拟客户对话引擎：start_session / reply / generate_debrief 唯一入口。

    组件鸭子类型注入（对齐 training-service app.py 装配形态）：llm（SimulatorLlm
    协议）、store（SimSessionStore 协议）、registry/scenario_catalog（目录）、
    debrief（复盘生成器，缺省 FakeDebriefLlm + 内存存储）。
    """

    def __init__(
        self,
        *,
        llm: SimulatorLlm,
        store: "SimSessionStore",
        registry: PersonaRegistry | None = None,
        scenario_catalog: ScenarioCatalog | None = None,
        debrief: Any | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ) -> None:
        if max_rounds < 1:
            raise SimBadRequest(f"max_rounds 必须 >= 1，收到 {max_rounds}")
        self.llm = llm
        self.store = store
        self.registry = registry if registry is not None else PersonaRegistry()
        self.scenarios = scenario_catalog if scenario_catalog is not None else ScenarioCatalog()
        if debrief is None:  # 惰性导入防循环（debrief 模块引用本模块常量）
            from services.training.core.simulator.debrief import (
                DebriefGenerator,
                FakeDebriefLlm,
                InMemoryDebriefStore,
            )

            debrief = DebriefGenerator(FakeDebriefLlm(), InMemoryDebriefStore())
        self.debrief = debrief
        self.max_rounds = max_rounds
        self._lock = threading.Lock()

    # --------------------------------------------------------------- 开场

    def start_session(
        self,
        persona: str | PersonaSpec,
        scenario: str | ScenarioSpec,
        difficulty: int | None = None,
        *,
        request_id: str = "",
    ) -> dict[str, Any]:
        """开场：解析类型/场景 → 初始状态 → 开场情境即首条 customer 消息。

        - difficulty 缺省取 persona.difficulty_base；越界抛 SimBadRequest；
        - 幂等：同 request_id 重放返回首次响应（不重复建会话）。
        """
        if request_id:
            cached = self.store.find_replay(START_REPLAY_SCOPE, request_id)
            if cached is not None:
                return dict(cached)
        spec = persona if isinstance(persona, PersonaSpec) else self.registry.get(str(persona))
        sc = scenario if isinstance(scenario, ScenarioSpec) else self.scenarios.get(str(scenario))
        effective = spec.difficulty_base if difficulty is None else int(difficulty)
        if not 1 <= effective <= 5:
            raise SimBadRequest(f"难度 {effective} 越界（1-5）")
        state = CustomerState.from_init(spec.state_init)
        session = SimSession(
            session_id=f"sim-{uuid4().hex[:12]}",
            persona_id=spec.persona_id,
            scenario_id=sc.scenario_id,
            persona=spec,
            scenario=sc,
            difficulty=effective,
            effective_difficulty=effective,
            max_rounds=self.max_rounds,
            state=state,
            transcript=[
                {"seq": 1, "role": "customer", "content": sc.opening, "state_snapshot": state.to_dict()}
            ],
        )
        self.store.save(session)
        response = {
            "session_id": session.session_id,
            "persona_id": spec.persona_id,
            "scenario_id": sc.scenario_id,
            "difficulty": effective,
            "max_rounds": self.max_rounds,
            "opening": sc.opening,
            "state": self._state_payload(state, effective),
        }
        if request_id:
            self.store.record_replay(START_REPLAY_SCOPE, request_id, dict(response))
        return response

    # --------------------------------------------------------------- 对话循环

    def reply(self, session_id: str, content: str, *, request_id: str = "") -> dict[str, Any]:
        """推进一轮对话：状态机更新 → LLM 演绎 → 结束判定 → 转录落存储。

        返回 {customer_reply, state, round, ended, ended_reason, degraded}；
        幂等：同 request_id 重放返回首次响应；会话已结束拒绝再回复。
        """
        if request_id:
            cached = self.store.find_replay(session_id, request_id)
            if cached is not None:
                return dict(cached)
        with self._lock:
            session = self.store.load(session_id)
            if session is None:
                raise SimSessionNotFound(f"未知会话：{session_id}")
            if session.ended:
                raise SimAlreadyEnded(f"会话 {session_id} 已结束（{session.ended_reason}），不能再回复")
            text = (content or "").strip()
            if not text:
                raise SimBadRequest("回复内容不能为空")
            if len(text) > MAX_REPLY_LEN:
                raise SimBadRequest(f"回复内容超过上限 {MAX_REPLY_LEN} 字符")

            # ① 状态机先更新（先更新后生成，回复与状态严格一致）
            machine = CustomerStateMachine(session.state, session.effective_difficulty)
            machine.update(text)

            # ② LLM 按更新后状态演绎（失败降级：规则式短回复 + degraded 标记）
            persona = session.persona
            scenario = session.scenario
            degraded = False
            try:
                reply_text = self.llm.generate_reply(
                    system=build_system_prompt(persona, scenario, session.effective_difficulty),
                    persona=persona,
                    scenario=scenario,
                    state=session.state.to_dict(),
                    transcript=session.transcript,
                    trainee_msg=text,
                    difficulty=session.effective_difficulty,
                )
            except SimulatorLlmError:
                degraded = True
                reply_text = FALLBACK_REPLY

            # ③ 转录追加（seq：trainee=2n，customer=2n+1；快照挂 customer 消息）
            session.rounds += 1
            round_no = session.rounds
            session.transcript.append(
                {"seq": 2 * round_no, "role": "trainee", "content": text, "state_snapshot": None}
            )
            session.transcript.append(
                {
                    "seq": 2 * round_no + 1,
                    "role": "customer",
                    "content": reply_text,
                    "state_snapshot": session.state.to_dict(),
                }
            )
            session.emotion_history.append(session.state.emotion)
            self._maybe_adjust_difficulty(session)
            verdict = self._check_ending(session)
            if verdict is not None:
                session.ended = True
                session.ended_reason = verdict
            self.store.save(session)
            response = {
                "customer_reply": reply_text,
                "state": self._state_payload(session.state, session.effective_difficulty),
                "round": round_no,
                "ended": session.ended,
                "ended_reason": session.ended_reason,
                "degraded": degraded,
            }
        if request_id:
            self.store.record_replay(session_id, request_id, dict(response))
        return response

    # --------------------------------------------------------------- 结束判定与难度调节

    @staticmethod
    def _check_ending(session: SimSession) -> str | None:
        """结束判定三分支，优先级：成交 > 谈崩 > 轮数耗尽（SR-TR-002 §6.2）。"""
        state = session.state
        if state.price_stance >= DEAL_STANCE_THRESHOLD and state.emotion >= DEAL_EMOTION_THRESHOLD:
            return "deal"
        if state.patience <= 0:
            return "breakdown"
        if session.rounds >= session.max_rounds:
            return "timeout"
        return None

    @staticmethod
    def _maybe_adjust_difficulty(session: SimSession) -> None:
        """难度动态调节：每 DRIFT_ROUNDS 轮按窗口情绪均值 ±1 微调（钳制 1~5）。"""
        if session.rounds % DRIFT_ROUNDS != 0 or not session.emotion_history:
            return
        window = session.emotion_history[-DRIFT_ROUNDS:]
        avg = sum(window) / len(window)
        if avg > DRIFT_UP_AVG:
            session.effective_difficulty = min(5, session.effective_difficulty + 1)
        elif avg < DRIFT_DOWN_AVG:
            session.effective_difficulty = max(1, session.effective_difficulty - 1)

    @staticmethod
    def _state_payload(state: CustomerState, difficulty: int) -> dict[str, Any]:
        """响应 state 段（快照四键 + 当前有效难度）。"""
        payload = state.to_dict()
        payload["difficulty"] = difficulty
        return payload

    # --------------------------------------------------------------- 复盘

    def generate_debrief(self, session_id: str) -> Any:
        """触发复盘生成（SR-TR-002 §6.3；API 层为 POST 语义）。

        会话未结束拒绝（40001）；未知会话 40401；LLM 故障由生成器降级兜底。
        """
        session = self.store.load(session_id)
        if session is None:
            raise SimSessionNotFound(f"未知会话：{session_id}")
        if not session.ended:
            raise SimBadRequest("会话尚未结束，暂不能生成复盘报告")
        return self.debrief.generate(
            persona=session.persona,
            session_id=session_id,
            scenario_id=session.scenario_id,
            verdict=str(session.ended_reason),
            transcript=session.transcript,
            final_state=session.state.to_dict(),
            difficulty=session.effective_difficulty,
        )

    def get_debrief(self, session_id: str) -> Any | None:
        """读取已生成的复盘报告；会话存在但未生成返回 None（40401 语义）。"""
        if self.store.load(session_id) is None:
            raise SimSessionNotFound(f"未知会话：{session_id}")
        return self.debrief.store.get(session_id)


# PersonaNotFoundError/ScenarioNotFoundError 透传（API 层映射 40401）
__all__ = [
    "BARGAIN_WORDS",
    "VALUE_WORDS",
    "ANGER_WORDS",
    "DEAL_STANCE_THRESHOLD",
    "DEAL_EMOTION_THRESHOLD",
    "STANCE_FIRM_MAX",
    "DEFAULT_MAX_ROUNDS",
    "DRIFT_ROUNDS",
    "MAX_REPLY_LEN",
    "FALLBACK_REPLY",
    "BREAKDOWN_LINE",
    "ANGRY_LINE",
    "DEAL_LINE",
    "NEAR_DEAL_LINE",
    "FLEX_LINE",
    "TONE_HARD",
    "TONE_SOFT",
    "stance_label",
    "has_any_word",
    "CustomerState",
    "CustomerStateMachine",
    "SimSession",
    "SimulatorLlm",
    "FakeSimulatorLlm",
    "SimulatorError",
    "SimBadRequest",
    "SimAlreadyEnded",
    "SimNotFoundError",
    "SimSessionNotFound",
    "SimulatorLlmError",
    "SimulatorEngine",
    "PersonaNotFoundError",
    "ScenarioNotFoundError",
]
