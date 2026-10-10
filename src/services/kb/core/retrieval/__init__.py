# 知识产权归甲方所有（COM-IP-01）
"""知识库检索三段式链路（FR-KB-015 / SR-KB-015，迭代 0.2）。

模块组成：
    rewriter  ① 查询改写：纠错/别名扩展/意图对齐规则层 + 可选 LLM 钩子；
    recall    ② 多路召回：向量路 + 关键词路 + 结构化路 → RRF 融合（k=60）；
    reranker  ③ 重排：Reranker Protocol + 本地 TEI 客户端 + 直通兜底；
    pipeline  三段编排：预算守卫（500ms 可配）+ 降级阶梯 + 分段计时；
    metrics   轻量指标（counter/histogram 内存实现，Prometheus 后接）；
    base      共享内核：Hit / 鸭子类型兼容器 / 50002 语义异常。

外部依赖（向量库 store.hybrid_search、向量化 embedder.embed_batch）由并行
任务建设、接口冻结，一律 Protocol 鸭子类型注入，本包不导入其具体实现。
"""
from .base import Hit, RetrievalDownstreamError, maybe_await
from .metrics import Counter, Histogram, MetricsRegistry
from .pipeline import (
    DEGRADE_OVER_TOTAL,
    DEGRADE_RERANK_FAILED,
    DEGRADE_SKIP_LLM_REWRITE,
    DEGRADE_SKIP_RERANK,
    STAGE_NAMES,
    BudgetGuard,
    RetrievalPipeline,
    RetrievalResult,
    StageBudgets,
)
from .recall import (
    RECALL_DEPTH_DEFAULT,
    RRF_K_DEFAULT,
    ROUTE_DENSE,
    ROUTE_SPARSE,
    ROUTE_STRUCTURED,
    EmbedderLike,
    EmbeddingResultLike,
    MultiRouteRecall,
    RecallResult,
    VectorStoreLike,
    build_expr,
    build_structured_expr,
    escape_expr_value,
    local_sparse_fallback,
    rrf_fuse,
)
from .reranker import BgeRerankerClient, PassthroughReranker, Reranker
from .rewriter import (
    REWRITE_LLM_SYSTEM_PROMPT,
    QueryRewriter,
    RewriteResult,
)

__all__ = [
    "BudgetGuard",
    "BgeRerankerClient",
    "Counter",
    "DEGRADE_OVER_TOTAL",
    "DEGRADE_RERANK_FAILED",
    "DEGRADE_SKIP_LLM_REWRITE",
    "DEGRADE_SKIP_RERANK",
    "EmbedderLike",
    "EmbeddingResultLike",
    "Hit",
    "Histogram",
    "MetricsRegistry",
    "MultiRouteRecall",
    "PassthroughReranker",
    "QueryRewriter",
    "RECALL_DEPTH_DEFAULT",
    "RRF_K_DEFAULT",
    "ROUTE_DENSE",
    "ROUTE_SPARSE",
    "ROUTE_STRUCTURED",
    "REWRITE_LLM_SYSTEM_PROMPT",
    "RecallResult",
    "Reranker",
    "RetrievalDownstreamError",
    "RetrievalPipeline",
    "RetrievalResult",
    "STAGE_NAMES",
    "StageBudgets",
    "VectorStoreLike",
    "build_expr",
    "build_structured_expr",
    "escape_expr_value",
    "local_sparse_fallback",
    "maybe_await",
    "rrf_fuse",
]
