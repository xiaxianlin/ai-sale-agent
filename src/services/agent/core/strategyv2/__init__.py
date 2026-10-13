# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略引擎 V2 包（FR-SC-003，SR-SC-003；Phase 4 第二批）。
# [CR A8 标注·生命周期] strategyv2 为 Phase 4 交付物：当前运行链接入形态为「按部署配置启用」——
# 生产装配迭代将按 NFR-SEC-11 差异化要求把 strategyv2 注入 AgentRunnerEngine/集群调度；
# demo 单账号形态沿用 V1（risk/strategy）即可满足 §16.3 验收口径。

四大策略能力 + A/B 实验（策略是决策者而非执行者，发送一律经风控）：
- :mod:`grader`——客户分级：RFM 变体四因子评分 → A/B/C/D + 人工改写留痕保护期；
- :mod:`cadence`——分级驱动跟进：cadence 分级表（热更新）+ 复用 V1 队列语义；
- :mod:`negotiation`——议价：底线校验（AI 不可见全量）+ 让步阶梯三段 + 赠品组合；
- :mod:`winback`——流失挽回：静默判定 → 唤醒序列（递进激励）+ 停止条件；
- :mod:`experiment`——策略 A/B：账号组分桶 + 双比例 z 检验效果统计。

依据文档：docs/07-实现方案/SC/SR-SC-003-销售策略引擎.md §6；SRS §9.2（FR-SC-003）。
"""

from services.agent.core.strategyv2.cadence import (
    DEFAULT_CADENCE,
    DEFAULT_MAX_ROUNDS,
    CadenceError,
    GradeCadence,
    GradeDrivenFollowup,
)
from services.agent.core.strategyv2.experiment import AbSplitter, ExperimentError
from services.agent.core.strategyv2.grader import (
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
    CustomerGrader,
    CustomerProfile,
    GradingError,
    GradeChange,
    InMemoryCustomerSource,
)
from services.agent.core.strategyv2.negotiation import (
    BargainSession,
    GiftRule,
    InMemoryFloorStore,
    NegotiationEngine,
    NegotiationError,
    PriceFloor,
    QuoteHint,
)
from services.agent.core.strategyv2.winback import (
    ConversationRef,
    InMemoryWinbackSource,
    WinbackEngine,
)

__all__ = [
    "DEFAULT_CADENCE", "DEFAULT_MAX_ROUNDS", "CadenceError", "GradeCadence",
    "GradeDrivenFollowup",
    "AbSplitter", "ExperimentError",
    "DEFAULT_THRESHOLDS", "DEFAULT_WEIGHTS", "CustomerGrader", "CustomerProfile",
    "GradingError", "GradeChange", "InMemoryCustomerSource",
    "BargainSession", "GiftRule", "InMemoryFloorStore", "NegotiationEngine",
    "NegotiationError", "PriceFloor", "QuoteHint",
    "ConversationRef", "InMemoryWinbackSource", "WinbackEngine",
]
