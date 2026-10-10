# 知识产权归甲方所有（COM-IP-01）
"""kb-service 知识库语义分块包（FR-KB-012，Phase 0）。

模块分工（SR-KB-012 §6）：
    chunker   分块主循环 + 四级边界决策（标题 > 表格 > 语义 > 长度兜底）
    boundary  边界检测器族（可插拔，SimilarityBoundary 待向量服务接入后启用）
    metadata  heading_path 标题栈 + context_meta 定稿回填（父标题/相邻块指针）
    quality   账目对账（不重不漏 + 块文本拼接≈原文）与分布指标自检
"""
from .boundary import (
    REASON_HARD_LIMIT,
    REASON_HEADING,
    REASON_MERGE,
    REASON_SEMANTIC,
    REASON_TABLE,
    REASON_TABLE_SPLIT,
    BoundaryContext,
    BoundaryDetector,
    BoundaryHit,
    HeadingBoundary,
    LengthFallback,
    SimilarityBoundary,
    TableBoundary,
    TablePiece,
    cosine,
    rows_to_markdown,
    split_row_groups,
)
from .chunker import (
    BlockLike,
    BufferedSentence,
    Chunk,
    ChunkConfig,
    ChunkInputError,
    SemanticChunker,
    UnstructuredDocLike,
    default_token_counter,
    split_sentences,
)
from .metadata import HeadingStack, finalize_chunks, make_chunk_uid
from .quality import ChunkQuality, reconcile, reconcile_blocks, reconcile_text

__all__ = [
    "REASON_HARD_LIMIT", "REASON_HEADING", "REASON_MERGE", "REASON_SEMANTIC",
    "REASON_TABLE", "REASON_TABLE_SPLIT",
    "BoundaryContext", "BoundaryDetector", "BoundaryHit",
    "HeadingBoundary", "LengthFallback", "SimilarityBoundary",
    "TableBoundary", "TablePiece", "cosine", "rows_to_markdown", "split_row_groups",
    "BlockLike", "BufferedSentence", "Chunk", "ChunkConfig", "ChunkInputError",
    "SemanticChunker", "UnstructuredDocLike", "default_token_counter", "split_sentences",
    "HeadingStack", "finalize_chunks", "make_chunk_uid",
    "ChunkQuality", "reconcile", "reconcile_blocks", "reconcile_text",
]
