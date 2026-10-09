# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""kb-service 增量更新包（FR-KB-017，Phase 0 / 迭代 0.2）。

模块分工（SR-KB-017 §3 代码落点中 incremental/differ/visibility/reconcile
四件套在本仓库的统一落位——迭代 0.2 按任务口径收敛为 sync/ 三模块，
visibility 并入 engine 的发布段）：
    diff       增量 diff 引擎：content_hash 三分类（unchanged / to_upsert / to_delete）
    engine     IncrementalSyncEngine 执行编排：事件 → 重分块 → diff → 嵌入 →
               入库 → vector_status 迁移 → 可见性切换；时限预算与超时降级异步补偿
    reconcile  对账任务：块仓应索引集 vs 向量库已索引集，缺失 / 孤儿清单 + 修复

复用的冻结接口（只读引用，按 Protocol 鸭子类型注入）：
FR-KB-012 SemanticChunker/Chunk、FR-KB-013 EmbeddingClient（及
embed_orchestrator 的 vector_status 枚举与死信队列）、FR-KB-014 VectorStore。

依据文档：docs/07-实现方案/KB/SR-KB-017-增量更新.md
"""
# 知识产权归甲方所有（COM-IP-01）

from .diff import (
    IncrementalPlan,
    IndexedChunk,
    PendingUpsert,
    UnchangedChunk,
    compute_content_hash,
    diff_chunks,
)
from .engine import (
    ACTION_ARCHIVE,
    ACTION_PUBLISH,
    ACTION_SAVE,
    ACTION_SOFT_DELETE,
    SYNC_EVENT_DEGRADE,
    SYNC_EVENT_DLQ,
    ChunkFn,
    CompensationEntry,
    IncrementalSyncEngine,
    InMemoryKnowledgeChunkStore,
    ItemStatusFlipper,
    ItemVectorMeta,
    KnowledgeChunkStore,
    KnowledgeItemEvent,
    StoredChunk,
    SyncAlertHook,
    SyncReport,
    SyncTimeBudget,
    build_vector_record,
    make_text_chunk_fn,
    split_body_blocks,
)
from .reconcile import (
    RECONCILE_EVENT_REPAIR,
    ReconcileReport,
    ReconcileTask,
    VectorInventory,
)

__all__ = [
    # diff 增量 diff 引擎
    "compute_content_hash",
    "IndexedChunk",
    "UnchangedChunk",
    "PendingUpsert",
    "IncrementalPlan",
    "diff_chunks",
    # engine 增量同步编排
    "ACTION_SAVE", "ACTION_PUBLISH", "ACTION_ARCHIVE", "ACTION_SOFT_DELETE",
    "SYNC_EVENT_DLQ", "SYNC_EVENT_DEGRADE",
    "ChunkFn", "SyncAlertHook", "SyncTimeBudget", "CompensationEntry",
    "ItemVectorMeta", "KnowledgeItemEvent", "StoredChunk", "SyncReport",
    "KnowledgeChunkStore", "InMemoryKnowledgeChunkStore", "ItemStatusFlipper",
    "build_vector_record", "split_body_blocks", "make_text_chunk_fn",
    "IncrementalSyncEngine",
    # reconcile 对账任务
    "RECONCILE_EVENT_REPAIR", "VectorInventory", "ReconcileReport", "ReconcileTask",
]
