# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""实战演练系统核心包（FR-TR-003，迭代 1.2；SR-TR-003 §6 工程实现）。

模块划分：
- :mod:`casebank`——案例库：结构校验 / 脱敏闸门（MaskingGate 协议注入）/
  ≥100 启用闸门 / demo 合成案例生成器；
- :mod:`scoring`——评分引擎：Scorer 协议（Fake 确定性桩 / DeepSeek 生产形态）+
  差距分析（vs 优秀回复）；
- :mod:`service`——应用服务：作答提交（幂等/会话隔离/评分降级）+ 一致性校准回路；
- :mod:`store`——存储协议 + 内存实现（生产替换 PG 形态，协议不变）。

与并行交付的 simulator（FR-TR-002 陪练）互不依赖；本包为 drill_api 独占后端。
"""
from services.training.core.drill.casebank import (
    DIM_LABELS,
    DIM_PRODUCT,
    DIM_SCRIPT,
    DIM_STRATEGY,
    DRILL_DIMS,
    CaseCapacityError,
    CaseValidationError,
    DrillCase,
    DrillCaseBank,
    GateRejection,
    ImportReport,
    MaskingGate,
    PiiRejectedError,
    RubricPoint,
    build_demo_cases,
)
from services.training.core.drill.scoring import (
    DeepSeekDrillScorer,
    DimScore,
    DrillScorer,
    Evidence,
    FakeDrillScorer,
    GapAnalysis,
    GapItem,
    ScoreOutput,
    ScoringError,
    ScorerConfigError,
    build_gap_analysis,
)
from services.training.core.drill.service import (
    AnswerInvalidError,
    CalibrationFrozenError,
    CalibrationReport,
    CalibrationSample,
    CaseNotAvailableError,
    DrillResult,
    DrillService,
    ResultNotFoundError,
    ResultStateError,
)
from services.training.core.drill.store import DrillStore, InMemoryDrillStore

__all__ = [
    # casebank
    "DIM_LABELS",
    "DIM_PRODUCT",
    "DIM_SCRIPT",
    "DIM_STRATEGY",
    "DRILL_DIMS",
    "CaseCapacityError",
    "CaseValidationError",
    "DrillCase",
    "DrillCaseBank",
    "GateRejection",
    "ImportReport",
    "MaskingGate",
    "PiiRejectedError",
    "RubricPoint",
    "build_demo_cases",
    # scoring
    "DeepSeekDrillScorer",
    "DimScore",
    "DrillScorer",
    "Evidence",
    "FakeDrillScorer",
    "GapAnalysis",
    "GapItem",
    "ScoreOutput",
    "ScoringError",
    "ScorerConfigError",
    "build_gap_analysis",
    # service
    "AnswerInvalidError",
    "CalibrationFrozenError",
    "CalibrationReport",
    "CalibrationSample",
    "CaseNotAvailableError",
    "DrillResult",
    "DrillService",
    "ResultNotFoundError",
    "ResultStateError",
    # store
    "DrillStore",
    "InMemoryDrillStore",
]
