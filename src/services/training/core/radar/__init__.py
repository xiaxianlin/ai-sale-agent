# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""能力雷达评估子模块（FR-TR-004，SR-TR-004；迭代 1.3）。

对外暴露四维口径常量、聚合引擎、快照存储协议与校准模型（详见 engine 模块 docstring）。
"""
from services.training.core.radar.engine import (
    DEFAULT_CONSISTENCY_THRESHOLD,
    DEFAULT_DIM_TOLERANCE,
    DEFAULT_SOURCE_WEIGHTS,
    DEFAULT_TOTAL_TOLERANCE,
    DIMENSIONS,
    DIM_LABELS,
    EVIDENCE_SOURCES,
    TRIGGERS,
    CalibrationPair,
    CalibrationReport,
    DimResult,
    Evidence,
    EvidenceSource,
    FakeEvidenceSource,
    InMemoryRadarSnapshotStore,
    RadarConfig,
    RadarEngine,
    RadarSnapshot,
    RadarSnapshotStore,
)

__all__ = [
    "DEFAULT_CONSISTENCY_THRESHOLD",
    "DEFAULT_DIM_TOLERANCE",
    "DEFAULT_SOURCE_WEIGHTS",
    "DEFAULT_TOTAL_TOLERANCE",
    "DIMENSIONS",
    "DIM_LABELS",
    "EVIDENCE_SOURCES",
    "TRIGGERS",
    "CalibrationPair",
    "CalibrationReport",
    "DimResult",
    "Evidence",
    "EvidenceSource",
    "FakeEvidenceSource",
    "InMemoryRadarSnapshotStore",
    "RadarConfig",
    "RadarEngine",
    "RadarSnapshot",
    "RadarSnapshotStore",
]
