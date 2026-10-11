# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""低置信度兜底：判定器 + 转人工执行编排（FR-AG-009，SR-AG-009 §6.1/§6.2/§6.3；迭代 2.2 demo 口径）。

职责分界（与 SR-AG-003 分工）：置信度**合成**归 brain/confidence.py；兜底**判定与执行**归本模块。
判定发生在生成之后、发送之前（生成链路末端门）：

- :class:`FallbackGate`——双阈值 + 硬校验一票否决 + fail-safe。判定序：
  SENSITIVE > ZERO_HIT（含 hit_floor 弱命中）> KEY_FIELD_MISSING > FACT_CHECK_FAIL > 双阈值分级；
  任何异常（配置非法/脏类型）一律 GATE_ERROR 转人工——宁误兜底，不漏兜底（R-03）；
- :func:`execute_fallback`——消费判定结果的执行编排：状态迁移 → 兜底话术（模板填充，
  不经 LLM——模板即全文，物理上无编造来源）→ 告警事件 → 待办入队（幂等键防重），
  全部依赖回调注入（state_transition/notify/pending_queue/ack_dedup/…）。

**架构红线（不得强行生成的物理保证，SR-AG-009 §6.3）**：本模块与执行编排**不持有任何
send 能力**——依赖面无发送回调，兜底路径唯一可发的文本是 :func:`render_ack` 的模板渲染
结果（由生产侧风控通道 FR-AG-005 投递，demo 仅渲染回传）；LLM 草稿只入待办 payload
供人工参考，代码路径上不存在草稿发送调用。测试以签名检查 + 行为侧捕获双断言钉死。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

# --------------------------------------------------------------------------- 判定器


class ReasonCode(StrEnum):
    """兜底原因码（六类）：驱动告警文案、待办分类与知识缺口回流。"""

    LOW_CONFIDENCE = "LOW_CONFIDENCE"        # conf 不足（含 T_low≤conf<T_high 待确认档）
    ZERO_HIT = "ZERO_HIT"                    # 知识库零命中/弱命中：无答案依据，禁止生成
    SENSITIVE = "SENSITIVE"                  # 敏感意图/敏感操作要素
    FACT_CHECK_FAIL = "FACT_CHECK_FAIL"      # 关键事实校验未过（价格残留/伪造引用）
    KEY_FIELD_MISSING = "KEY_FIELD_MISSING"  # 价格/交期等关键字段无结构化来源（占位符悬空）
    GATE_ERROR = "GATE_ERROR"                # 判定器自身异常 → fail-safe 兜底


#: 判定动作（action 三值）：自动发送 / 转人工（草稿供快速确认）/ 转人工+缺口记录
ACTION_AUTO_SEND = "auto_send"
ACTION_HUMAN_REVIEW = "human_review"
ACTION_HUMAN_REVIEW_GAP = "human_review_gap"


@dataclass(frozen=True)
class GateThresholds:
    """全局双阈值与弱命中底线（app_config scope='ag_fallback'，NFR-MAI-02 热更口径）。"""

    t_high: float = 0.75   # ≥ 且非敏感/全硬校验过 → 自动发送
    t_low: float = 0.45    # < → 转人工+缺口记录；[t_low, t_high) → 转人工（草稿确认）
    hit_floor: float = 0.30  # top_score 低于此值按零命中处置（弱命中=无依据）


@dataclass(frozen=True)
class FallbackEvidence:
    """判定输入：由生成链路（意图/检索/生成/校验/置信度）汇出。"""

    intent: str                 # 意图（七枚举或 other）
    hit_count: int              # 检索命中数
    top_score: float            # 最高检索得分（重排后 0~1）
    self_eval: float            # 生成自评 0~1
    fact_check_ok: bool         # 关键事实校验（引用合法、无价格残留）
    key_fields_complete: bool   # 关键字段结构化来源齐备（占位符全部解析）
    sensitive: bool             # 意图=敏感 或草稿命中敏感要素
    confidence: float           # confidence.py 合成的加权置信度


@dataclass(frozen=True)
class FallbackVerdict:
    """判定输出：need_human 结构化结果（need_human=true 时代码路径无草稿发送）。"""

    need_human: bool
    action: str            # auto_send / human_review / human_review_gap
    reason_code: str       # ReasonCode；自动发送为空串
    gap_record: bool       # 是否写知识缺口（knowledge_gap）
    confidence: float


class GateConfigError(ValueError):
    """门控阈值配置非法（t_low≥t_high 等）：fail-safe 前的显式信号。"""


class FallbackGate:
    """兜底判定器：双阈值 + 硬校验 + fail-safe（SR-AG-009 §6.1）。

    :param thresholds: 全局阈值（默认 0.75/0.45/0.30）；
    :param by_intent: 按意图差异化阈值——询价/技术类从严（报错代价高）、闲聊类从宽；
        形态 ``{"询价": {"t_high": 0.80, "t_low": 0.50}}``，未配置意图回退全局默认。
    """

    def __init__(
        self,
        thresholds: GateThresholds | None = None,
        by_intent: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self._thresholds = thresholds or GateThresholds()
        self._by_intent = by_intent or {}

    def _thresholds_for(self, intent: str) -> tuple[float, float, float]:
        """解析意图级阈值并校验（非法抛 GateConfigError → decide 捕获转 GATE_ERROR）。"""
        overrides = self._by_intent.get(intent) or {}
        t_high = float(overrides.get("t_high", self._thresholds.t_high))
        t_low = float(overrides.get("t_low", self._thresholds.t_low))
        hit_floor = float(overrides.get("hit_floor", self._thresholds.hit_floor))
        if not (0.0 < t_low < t_high < 1.0) or not (0.0 < hit_floor < 1.0):
            raise GateConfigError(f"阈值非法：t_low={t_low}, t_high={t_high}, hit_floor={hit_floor}")
        return t_high, t_low, hit_floor

    def decide(self, ev: FallbackEvidence) -> FallbackVerdict:
        """判定主流程：硬校验（一票否决，与阈值无关）→ 双阈值分级。

        fail-safe：任何异常（配置缺失/类型脏/计算错误）一律按转人工处置，
        reason=GATE_ERROR，不向调用方抛出——宁误兜底，不漏兜底。
        """
        try:
            if ev.sensitive:  # ① 敏感最优先：不生成不发送，交 FR-AG-005/009
                return FallbackVerdict(True, ACTION_HUMAN_REVIEW, ReasonCode.SENSITIVE,
                                       False, ev.confidence)
            _, _, hit_floor = self._thresholds_for(ev.intent)
            if ev.hit_count == 0 or ev.top_score < hit_floor:  # ② 零命中/弱命中：物理无依据（R-03）
                return FallbackVerdict(True, ACTION_HUMAN_REVIEW_GAP, ReasonCode.ZERO_HIT,
                                       True, ev.confidence)
            if not ev.key_fields_complete:  # ③ 关键字段无结构化来源：防占位符悬空/编造
                return FallbackVerdict(True, ACTION_HUMAN_REVIEW, ReasonCode.KEY_FIELD_MISSING,
                                       ev.hit_count <= 1, ev.confidence)
            if not ev.fact_check_ok:  # ④ 关键事实与产品库不符/引用伪造：疑似幻觉
                return FallbackVerdict(True, ACTION_HUMAN_REVIEW, ReasonCode.FACT_CHECK_FAIL,
                                       False, ev.confidence)
            t_high, t_low, _ = self._thresholds_for(ev.intent)
            if ev.confidence >= t_high:  # ⑤ 高置信：唯一自动发送出口
                return FallbackVerdict(False, ACTION_AUTO_SEND, "", False, ev.confidence)
            if ev.confidence >= t_low:  # ⑥ 灰区：转人工，草稿附待办供快速确认
                return FallbackVerdict(True, ACTION_HUMAN_REVIEW, ReasonCode.LOW_CONFIDENCE,
                                       False, ev.confidence)
            return FallbackVerdict(True, ACTION_HUMAN_REVIEW_GAP,  # ⑦ 低置信：转人工+缺口
                                   ReasonCode.LOW_CONFIDENCE, True, ev.confidence)
        except Exception:  # noqa: BLE001 —— fail-safe：判定器任何异常都不得漏兜底
            return FallbackVerdict(True, ACTION_HUMAN_REVIEW, ReasonCode.GATE_ERROR,
                                   False, ev.confidence)


# --------------------------------------------------------------------------- 执行编排（回调注入）

#: 兜底话术模板（FR-KB-003 kb003_fallback_ack_v1 的 demo 内联形态；占位符仅 {promise_hours}）
ACK_TEMPLATE_DEFAULT = (
    "Thanks for your inquiry. Our colleague will follow up with a detailed answer "
    "within {promise_hours} hours. 感谢您的询盘，人工同事将在 {promise_hours} 小时内为您详细答复。"
)
#: 状态迁移 actor（审计/留痕可对账口径）
FALLBACK_ACTOR = "system:fr_ag_009"
#: 待办来源标识（SR-AG-009 §4 human_todo.source 枚举之一）
FALLBACK_SOURCE = "conf_fallback"


@dataclass(frozen=True)
class FallbackContext:
    """执行编排上下文：一次兜底涉及的会话/消息/草稿要素。"""

    conversation_uid: str
    message_uid: str
    account_id: int
    intent: str
    query: str
    reply_draft: str          # LLM 草稿（仅入待办供人工参考；任何回调不外发）
    confidence: float


class InMemoryTodoQueue:
    """内存待办队列（demo 形态；生产 = PG human_todo，幂等键唯一约束同语义）。"""

    def __init__(self) -> None:
        self._by_key: dict[str, int] = {}
        self._items: list[dict[str, Any]] = []

    def enqueue(self, payload: dict[str, Any]) -> int:
        """幂等入队：同 idempotent_key 返回既有 ID（重复投递/重试不产生重复待办）。"""
        key = str(payload["idempotent_key"])
        if key in self._by_key:
            return self._by_key[key]
        todo_id = len(self._items) + 1
        self._items.append(dict(payload))
        self._by_key[key] = todo_id
        return todo_id

    @property
    def items(self) -> list[dict[str, Any]]:
        """全部待办（只读副本）。"""
        return [dict(item) for item in self._items]


class InMemoryAckDedup:
    """内存去抖窗口（demo 形态；生产 = Redis SET NX EX，TTL 由配置承载）。"""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def first_seen(self, conversation_uid: str) -> bool:
        """首次出现返回 True 并登记；窗口内重复返回 False。"""
        if conversation_uid in self._seen:
            return False
        self._seen.add(conversation_uid)
        return True


@dataclass
class FallbackDeps:
    """执行编排依赖面（全部回调注入；**无任何 send 能力字段**——架构红线见模块 docstring）。

    - state_transition(uid, to_state, actor, reason)：会话状态迁移（FR-AG-008 状态机）；
    - notify(payload)：告警事件（FR-AG-007；level=high 即时推送由生产通道承载）；
    - pending_queue.enqueue(payload)：人工待办入队（幂等键防重）；
    - ack_dedup(uid)：兜底话术去抖窗口（同会话 30min 一次，防噪声）；
    - gap_recorder(query_digest, query_text, intent)：知识缺口记录（可选）；
    - monitor(reason_code)：兜底率监控计数（可选，知识库退化信号）。
    """

    state_transition: Callable[[str, str, str, str], Any]
    notify: Callable[[dict[str, Any]], Any]
    pending_queue: Any                    # 具备 enqueue(payload) -> int 的队列协议
    ack_dedup: Callable[[str], bool]
    gap_recorder: Callable[[str, str, str], None] | None = None
    monitor: Callable[[str], None] | None = None
    promise_hours: int = 4                # 承诺人工答复时限（话术 + 待办 SLA）
    template: str = ACK_TEMPLATE_DEFAULT


@dataclass
class FallbackOutcome:
    """执行编排结果：API 响应 fallback 段与审计对账的数据源。"""

    todo_id: int | None
    state: str
    ack_sent: bool
    ack_text: str
    alert_emitted: bool
    gap_recorded: bool = False
    errors: tuple[str, ...] = field(default_factory=tuple)


def render_ack(template: str, *, promise_hours: int) -> str:
    """模板占位符填充（模板即全文，不经 LLM——物理上无编造来源）。"""
    return template.replace("{promise_hours}", str(promise_hours))


def execute_fallback(verdict: FallbackVerdict, ctx: FallbackContext, deps: FallbackDeps) -> FallbackOutcome:
    """转人工执行编排：状态迁移 → 兜底话术 → 告警 → 待办（→ 缺口/监控）。

    步骤级容错（SR-AG-009 §6.6）：单步失败记入 errors 继续后续步骤，
    会话停留在 pending_confirm 即安全态——不回滚、不阻断主链路；
    各步幂等（状态机幂等迁移 / 去抖窗口 / 待办幂等键），重试不产生副作用。
    **本函数不发送任何内容**：话术仅渲染交回调用方（生产经 FR-AG-005 风控通道投递）。
    """
    errors: list[str] = []
    # ① 会话状态迁移（ai_managed → pending_confirm；FR-AG-008 留痕+审计由状态机承载）
    state = "pending_confirm"
    try:
        deps.state_transition(ctx.conversation_uid, state, FALLBACK_ACTOR, verdict.reason_code)
    except Exception as exc:  # noqa: BLE001 —— 步骤级容错
        errors.append(f"state_transition: {exc}")
    # ② 兜底话术：模板填充 + 去抖（话术不经 LLM；demo 只渲染，不投递）
    ack_text = ""
    ack_sent = False
    try:
        if deps.ack_dedup(ctx.conversation_uid):
            ack_text = render_ack(deps.template, promise_hours=deps.promise_hours)
            ack_sent = True  # 生产：此处交 FR-AG-005 风控通道 send_templated（频率/节奏约束）
    except Exception as exc:  # noqa: BLE001
        errors.append(f"ack_dedup: {exc}")
    # ③ 告警事件（level=high；同会话合并去重由生产告警通道的 dedup_key 承载）
    alert_emitted = False
    try:
        deps.notify({
            "level": "high",
            "type": "conf_fallback",
            "title": f"[兜底] {ctx.intent} 会话需人工处理",
            "body": f"原因={verdict.reason_code} conf={verdict.confidence:.2f} 账号={ctx.account_id}",
            "link": f"/console/todos?conversation={ctx.conversation_uid}",
        })
        alert_emitted = True
    except Exception as exc:  # noqa: BLE001
        errors.append(f"notify: {exc}")
    # ④ 人工待办入队（幂等键 {conv}:{msg}:{source}；草稿仅入待办供人工参考）
    todo_id: int | None = None
    try:
        todo_id = deps.pending_queue.enqueue({
            "source": FALLBACK_SOURCE,
            "reason_code": verdict.reason_code,
            "conversation_uid": ctx.conversation_uid,
            "message_uid": ctx.message_uid,
            "account_id": ctx.account_id,
            "intent": ctx.intent,
            "draft": ctx.reply_draft,
            "confidence": verdict.confidence,
            "sla_hours": deps.promise_hours,
            "idempotent_key": f"{ctx.conversation_uid}:{ctx.message_uid}:{FALLBACK_SOURCE}",
        })
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pending_queue: {exc}")
    # ⑤ 知识缺口记录（ZERO_HIT / conf<T_low；供缺口清单回流 FR-TR-008）
    gap_recorded = False
    if verdict.gap_record and deps.gap_recorder is not None:
        try:
            deps.gap_recorder(_query_digest(ctx.query), ctx.query, ctx.intent)
            gap_recorded = True
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gap_recorder: {exc}")
    # ⑥ 兜底率监控计数（突增=知识库退化信号，联动 FR-KB-016）
    if deps.monitor is not None:
        try:
            deps.monitor(verdict.reason_code)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"monitor: {exc}")
    return FallbackOutcome(
        todo_id=todo_id, state=state, ack_sent=ack_sent, ack_text=ack_text,
        alert_emitted=alert_emitted, gap_recorded=gap_recorded, errors=tuple(errors),
    )


def _query_digest(query: str) -> str:
    """归一化查询指纹（聚合键）：小写化 + 压缩空白，取 SHA1 前 16 位（knowledge_gap 口径）。"""
    import hashlib
    import re as _re

    normalized = _re.sub(r"\s+", " ", query.strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "ReasonCode",
    "ACTION_AUTO_SEND",
    "ACTION_HUMAN_REVIEW",
    "ACTION_HUMAN_REVIEW_GAP",
    "GateThresholds",
    "FallbackEvidence",
    "FallbackVerdict",
    "GateConfigError",
    "FallbackGate",
    "ACK_TEMPLATE_DEFAULT",
    "FALLBACK_ACTOR",
    "FALLBACK_SOURCE",
    "FallbackContext",
    "FallbackDeps",
    "FallbackOutcome",
    "InMemoryTodoQueue",
    "InMemoryAckDedup",
    "render_ack",
    "execute_fallback",
]
