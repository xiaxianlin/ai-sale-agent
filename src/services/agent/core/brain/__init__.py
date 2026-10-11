# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""AI 大脑与兜底（FR-AG-003 / FR-AG-009 / NFR-PER-03 / NFR-PER-04；迭代 2.2）。

模块地图（生成链路 SR-AG-003 §2.1 → 兜底处置 SR-AG-009 → 观测统计 NFR-PER-03/04）：
- intent      意图识别（七意图 + other 兜底；红线词快检；可插拔 LLM 协议）
- generator   回复生成（检索→组装→生成→价格保护→引用校验→自评）
- confidence  置信度合成（检索/自评/事实三维加权，权重可配和=1）
- fallback    兜底判定（双阈值+硬校验 fail-safe）与转人工执行编排（无 send 能力）
- clock       询盘响应时钟（T0→T3 七段、45s 预算、中位/P90、>60s 告警）
- stats       日处理询盘量（同会话当日计 1、三终态、原子计数、快照对账、归因）
"""

from services.agent.core.brain.clock import (
    DEFAULT_BUDGETS_SEC,
    PHASES,
    ResponseClock,
    SINGLE_ALERT_SEC,
    TOTAL_BUDGET_SEC,
    TraceRecord,
    finish,
    summarize,
)
from services.agent.core.brain.confidence import (
    ConfidenceComposer,
    ConfidenceWeights,
    SelfEval,
    retrieval_score,
)
from services.agent.core.brain.fallback import (
    ACTION_AUTO_SEND,
    FallbackContext,
    FallbackDeps,
    FallbackEvidence,
    FallbackGate,
    FallbackVerdict,
    GateThresholds,
    InMemoryAckDedup,
    InMemoryTodoQueue,
    ReasonCode,
    execute_fallback,
    render_ack,
)
from services.agent.core.brain.generator import (
    Customer,
    FakeBrainLlm,
    PriceGuard,
    ReplyGenerator,
    ReplyRequest,
    ReplyResult,
)
from services.agent.core.brain.intent import (
    INTENT_ENUM,
    BrainLlmClient,
    DeepSeekBrainClient,
    FakeIntentLlm,
    IntentClassifier,
    IntentResult,
    LlmConfigError,
    LlmOutputError,
)
from services.agent.core.brain.stats import DailyInquiryStats, HandledEvent

__all__ = [
    # intent
    "INTENT_ENUM", "BrainLlmClient", "DeepSeekBrainClient", "FakeIntentLlm",
    "IntentClassifier", "IntentResult", "LlmConfigError", "LlmOutputError",
    # generator
    "Customer", "ReplyRequest", "ReplyResult", "ReplyGenerator", "FakeBrainLlm", "PriceGuard",
    # confidence
    "ConfidenceComposer", "ConfidenceWeights", "SelfEval", "retrieval_score",
    # fallback
    "ACTION_AUTO_SEND", "FallbackContext", "FallbackDeps", "FallbackEvidence", "FallbackGate",
    "FallbackVerdict", "GateThresholds", "InMemoryAckDedup", "InMemoryTodoQueue",
    "ReasonCode", "execute_fallback", "render_ack",
    # clock
    "DEFAULT_BUDGETS_SEC", "PHASES", "SINGLE_ALERT_SEC", "TOTAL_BUDGET_SEC",
    "ResponseClock", "TraceRecord", "finish", "summarize",
    # stats
    "DailyInquiryStats", "HandledEvent",
]
