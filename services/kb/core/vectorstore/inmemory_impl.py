# 知识产权归甲方所有（COM-IP-01）
"""内存向量存储实现（FR-KB-014 向量数据库的内存后端，迭代 0.2）。

定位：单元测试 / 联调对拍 / 无 Milvus 环境下的开发替身（SR-KB-014 §9）。
检索语义与 MilvusStore 严格对齐（SR-KB-014 §6.3），保证「换后端不换行为」：
- 稠密路：余弦相似度——BGE-M3 向量已归一化时与 Milvus HNSW+IP 度量等价；
- 稀疏路：稀疏点积——与 SPARSE_INVERTED_INDEX + IP 语义一致；
- 标量过滤：base.parse_filter_expr 解析简化 DSL 后逐条求值（字段白名单 fail-closed）；
- 可见性：仅 item_status == published 的记录参与检索（03-数据库设计 §4.2
  「仅索引 published 状态」；未发布条目允许入库但不召回）；
- 融合：RRF（k 默认 60 可配，SR-KB-014 §7 KB_VEC_RRF_K）。单路相似度严格大于 0
  才视为被该路召回——对齐 ANN「未命中即不返回」语义，杜绝零分记录蹭融合分；
  融合同分按 chunk_id 升序，保证结果确定可测；
- 线程安全：单把 RLock 串行化 upsert / delete / hybrid_search。VectorRecord 为
  frozen 模型、入库后不可变，读侧无部分写风险；开发态规模（万级以下）下串行足够，
  生产态并发由 Milvus 承担。
"""
from __future__ import annotations

import math
import threading
from collections.abc import Mapping, Sequence

from .base import (
    DENSE_DIM,
    ITEM_STATUS_PUBLISHED,
    FilterCondition,
    SearchHit,
    VectorRecord,
    parse_filter_expr,
)
from .collection_schema import DEFAULT_RRF_K


def _cosine_similarity(query: Sequence[float], doc: Sequence[float]) -> float:
    """稠密余弦相似度；任一侧为零向量时返回 0.0（无方向，视为未召回，避免除零）。"""
    dot = 0.0
    norm_q = 0.0
    norm_d = 0.0
    for q, d in zip(query, doc):
        dot += q * d
        norm_q += q * q
        norm_d += d * d
    if norm_q == 0.0 or norm_d == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_q) * math.sqrt(norm_d))


def _sparse_dot(query: Mapping[int, float], doc: Mapping[int, float]) -> float:
    """稀疏点积：在较短一侧迭代，重叠下标累乘求和。"""
    if len(query) <= len(doc):
        return sum(weight * doc.get(index, 0.0) for index, weight in query.items())
    return sum(query.get(index, 0.0) * weight for index, weight in doc.items())


class InMemoryVectorStore:
    """VectorStore 协议的内存实现（chunk_id → VectorRecord 字典为主存）。

    用法：单测与联调对拍。生产环境请使用 MilvusStore（milvus_impl.py）。
    """

    def __init__(self, *, rrf_k: int = DEFAULT_RRF_K) -> None:
        """rrf_k：RRF 融合常数（SR-KB-014 §7 取值范围 10~200，默认 60）。"""
        if rrf_k < 1:
            raise ValueError(f"rrf_k 必须 >= 1，收到：{rrf_k!r}")
        self._rrf_k = rrf_k
        self._records: dict[int, VectorRecord] = {}  # chunk_id → 记录（upsert 幂等覆盖）
        self._lock = threading.RLock()  # 读写共用一把锁：小数据量下简单且足够

    def __len__(self) -> int:
        """当前存储记录总数（含未发布；仅测试与运维观测用，不属于检索协议）。"""
        with self._lock:
            return len(self._records)

    def upsert(self, records: list[VectorRecord]) -> None:
        """批量写入/覆盖：按 chunk_id 幂等（同主键重投只保留最后一条，FR-KB-017）。"""
        with self._lock:
            for record in records:
                self._records[record.chunk_id] = record

    def delete(self, chunk_ids: list[int]) -> None:
        """按主键批量删除；不存在的主键静默忽略（幂等，条目下线同步删除）。"""
        with self._lock:
            for chunk_id in chunk_ids:
                self._records.pop(chunk_id, None)

    def hybrid_search(
        self,
        dense: Sequence[float] | None = None,
        sparse: Mapping[int, float] | None = None,
        expr: str | None = None,
        top_k: int = 5,
    ) -> list[SearchHit]:
        """混合检索：published 过滤 + expr 标量过滤 → 双路召回 → RRF 融合 → 截断 top_k。

        - dense / sparse 至少提供一路，否则 ValueError；
        - expr 非法（白名单外字段 / 不支持语法）立即 ValueError（fail-closed）；
        - 单路相似度 <= 0 的记录不进入该路排序（对齐 ANN 未命中不返回语义）；
        - 返回按融合得分降序、同分按 chunk_id 升序的稳定排序。
        """
        if not isinstance(top_k, int) or top_k < 1:
            raise ValueError(f"top_k 必须为 >= 1 的整数，收到：{top_k!r}")
        if dense is None and sparse is None:
            raise ValueError("dense 与 sparse 查询向量至少提供一路")
        if dense is not None and len(dense) != DENSE_DIM:
            raise ValueError(f"dense 查询向量维度必须为 {DENSE_DIM}，收到 {len(dense)} 维")
        conditions: list[FilterCondition] = parse_filter_expr(expr)  # 非法 expr 在此抛错

        with self._lock:  # 全程持锁：过滤 + 双路打分 + 融合对一个一致快照求值
            candidates = [
                record
                for record in self._records.values()
                if record.item_status == ITEM_STATUS_PUBLISHED
                and all(condition.matches(record) for condition in conditions)
            ]
            rrf_scores: dict[int, float] = {}
            if dense is not None:
                self._fuse_channel(rrf_scores, candidates, dense, is_dense=True)
            if sparse is not None:
                self._fuse_channel(rrf_scores, candidates, sparse, is_dense=False)

        ranked = sorted(rrf_scores.items(), key=lambda item: (-item[1], item[0]))
        return [SearchHit(chunk_id=cid, score=score) for cid, score in ranked[:top_k]]

    def _fuse_channel(
        self,
        rrf_scores: dict[int, float],
        candidates: list[VectorRecord],
        query: Sequence[float] | Mapping[int, float],
        *,
        is_dense: bool,
    ) -> None:
        """单路召回 + RRF 计分：按相似度降序（同分按 chunk_id 升序）排名后累加 1/(k+名次)。

        相似度 <= 0 的候选视为未被该路召回，不参与排名（见模块 docstring）。
        """
        scored: list[tuple[float, int]] = []
        for record in candidates:
            if is_dense:
                similarity = _cosine_similarity(query, record.dense)  # type: ignore[arg-type]
            else:
                similarity = _sparse_dot(query, record.sparse)  # type: ignore[arg-type]
            if similarity > 0.0:
                scored.append((similarity, record.chunk_id))
        scored.sort(key=lambda item: (-item[0], item[1]))  # 名次稳定：同分小 chunk_id 在前
        for rank, (_similarity, chunk_id) in enumerate(scored, start=1):
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + 1.0 / (self._rrf_k + rank)


__all__ = ["InMemoryVectorStore"]
