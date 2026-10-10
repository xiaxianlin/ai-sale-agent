# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""Phase 0 端到端入库编排包（迭代 0.3 收口）。

- orchestrator：素材文件 → 解析 → 清洗 → 分块 → 条目化 → 嵌入 → 向量入库
  的端到端编排（IngestionOrchestrator + BatchManifest 批次模型）；
- coverage：材料清单（期望素材 × 六库分配）vs 实际入库结果的覆盖率核算
  （§16.1 知识库覆盖率 100% 口径的核算实现）。
"""
from services.kb.core.ingestion.coverage import (
    CoverageGap,
    CoverageReport,
    MaterialExpectation,
    compute_coverage,
    material_plan_from_sources,
)
from services.kb.core.ingestion.orchestrator import (
    BATCH_STATUS_DONE,
    FILE_STATUS_FAILED,
    FILE_STATUS_SUCCESS,
    BatchManifest,
    FileResult,
    IngestionError,
    IngestionOrchestrator,
    SourceMaterial,
    default_metadata_for,
    vector_dims_for,
)

__all__ = [
    "BATCH_STATUS_DONE",
    "FILE_STATUS_FAILED",
    "FILE_STATUS_SUCCESS",
    "BatchManifest",
    "CoverageGap",
    "CoverageReport",
    "FileResult",
    "IngestionError",
    "IngestionOrchestrator",
    "MaterialExpectation",
    "SourceMaterial",
    "compute_coverage",
    "default_metadata_for",
    "material_plan_from_sources",
    "vector_dims_for",
]
