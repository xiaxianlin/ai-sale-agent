# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""kb-service 向量化包（FR-KB-013，Phase 0）。

模块分工（SR-KB-013 §5/§6）：
    base               对外稳定契约：EmbeddingResult / EmbeddingClient 协议 / EmbeddingError
    guard              出域防护：端点内网断言（NFR-SEC-06 代码层，fail-fast）
    client             TeiEmbeddingClient（内网 TEI 真实客户端）+ FakeEmbeddingClient（确定性测试桩）
    embed_orchestrator 批量编排：入队 -> 分批 -> vector_status 状态机 -> DLQ + 告警钩子

下游（FR-KB-014 入库 / FR-KB-017 增量 / FR-KB-030 查询向量化）只从本包
__init__ 导入，不触碰内部模块路径。
"""
# 知识产权归甲方所有（COM-IP-01）

from .base import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    DENSE_DIM,
    EmbeddingClient,
    EmbeddingError,
    EmbeddingResult,
)
from .client import FakeEmbeddingClient, TeiEmbeddingClient
from .embed_orchestrator import (
    DEFAULT_MAX_AUTO_RETRIES,
    EMBED_EVENT_DLQ,
    EMBED_EVENT_DOWN,
    EMBED_EVENT_FAILED,
    VECTOR_STATUS_FAILED,
    VECTOR_STATUS_INDEXED,
    VECTOR_STATUS_PENDING,
    VECTOR_STATUS_REMOVED,
    AlertHook,
    ChunkStore,
    DeadLetterQueue,
    DlqEntry,
    EmbedOrchestrator,
    EmbedRunReport,
    EmbedTask,
    InMemoryChunkStore,
    VectorSink,
)
from .guard import (
    DEFAULT_ALLOWED_CIDRS,
    DEFAULT_ALLOWED_HOSTS,
    EmbeddingEgressError,
    assert_allowed_embedding_endpoint,
    default_resolve_host,
    is_allowed_embedding_endpoint,
)

__all__ = [
    # base 契约
    "DEFAULT_BATCH_SIZE", "DEFAULT_RETRIES", "DEFAULT_TIMEOUT_SECONDS", "DENSE_DIM",
    "EmbeddingClient", "EmbeddingError", "EmbeddingResult",
    # 客户端实现
    "FakeEmbeddingClient", "TeiEmbeddingClient",
    # 出域防护
    "DEFAULT_ALLOWED_CIDRS", "DEFAULT_ALLOWED_HOSTS", "EmbeddingEgressError",
    "assert_allowed_embedding_endpoint", "default_resolve_host", "is_allowed_embedding_endpoint",
    # 批量编排
    "DEFAULT_MAX_AUTO_RETRIES", "EMBED_EVENT_DLQ", "EMBED_EVENT_DOWN", "EMBED_EVENT_FAILED",
    "VECTOR_STATUS_FAILED", "VECTOR_STATUS_INDEXED", "VECTOR_STATUS_PENDING", "VECTOR_STATUS_REMOVED",
    "AlertHook", "ChunkStore", "DeadLetterQueue", "DlqEntry",
    "EmbedOrchestrator", "EmbedRunReport", "EmbedTask", "InMemoryChunkStore", "VectorSink",
]
