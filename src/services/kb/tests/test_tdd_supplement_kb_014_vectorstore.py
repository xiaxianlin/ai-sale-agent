# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""向量数据库（FR-KB-014 / SR-KB-014）TDD 补齐测试（五类场景补强，不改既有文件）。

按 TDD 新规五列审计既有 test_vectorstore_* 后补齐：
- 边界：零稠密查询向量（余弦 0 → 不召回，对齐 ANN「未命中即不返回」）、
  稀疏零重叠 / 空 dict 通道、top_k 大于候选数（全量返回）；
- 降级/幂等重放：draft → published → draft 的可见性翻转重放（FR-KB-017
  发布/下线的存储侧语义：未发布入库不可见、重放幂等）。

并发列由既有 test_concurrent_upsert_delete_search 承接（RLock 串行化已覆盖），
本文件不重复。

KPI 映射：SRS §16.1「检索准确率 / 数据完整性」——零分不蹭融合分保证
排序质量；可见性翻转幂等保证增量发布/下线后检索视图与库内状态一致。

依据文档：docs/07-实现方案/KB/SR-KB-014-向量数据库.md §6.3、§9；
         docs/02-设计文档/03-数据库设计.md §4.2。
"""
from __future__ import annotations

from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient
from services.kb.core.vectorstore import InMemoryVectorStore, VectorRecord


def _make_store(embedder: FakeEmbeddingClient) -> tuple[InMemoryVectorStore, FakeEmbeddingClient]:
    store = InMemoryVectorStore()
    store.upsert([
        VectorRecord(
            chunk_id=1,
            kb_type="product",
            dense=embedder.dense_vector("云台支架 最大承载 85kg"),
            sparse=embedder.sparse_vector("云台支架 最大承载 85kg"),
            sku="ST-3200",
        ),
        VectorRecord(
            chunk_id=2,
            kb_type="product",
            dense=embedder.dense_vector("摄影背包 防水 夹层"),
            sparse=embedder.sparse_vector("摄影背包 防水 夹层"),
            sku="GP-100",
        ),
    ])
    return store, embedder


class TestZeroSignalBoundaries:
    """边界：零信号查询不产生命中（杜绝零分记录蹭融合分）。"""

    def test_zero_dense_query_returns_empty(self) -> None:
        """零稠密向量：余弦无方向 → 全部 0 分 → 空结果（非报错、非全量返回）。"""
        store, _embedder = _make_store(FakeEmbeddingClient())
        assert store.hybrid_search(dense=[0.0] * 1024, top_k=5) == []

    def test_sparse_no_overlap_returns_empty(self) -> None:
        """稀疏零重叠：点积 0 → 该路无候选 → 空结果。"""
        store, _embedder = _make_store(FakeEmbeddingClient())
        assert store.hybrid_search(sparse={999_999: 3.0}, top_k=5) == []

    def test_empty_sparse_dict_is_valid_channel(self) -> None:
        """空 dict 稀疏通道（非 None）：合法请求，语义为该路零词元 → 空结果。"""
        store, _embedder = _make_store(FakeEmbeddingClient())
        assert store.hybrid_search(sparse={}, top_k=5) == []


class TestTopKBoundaries:
    """边界：top_k 与候选数量的相对关系。"""

    def test_top_k_exceeds_candidates_returns_all(self) -> None:
        """top_k=10 > 候选 2 条：全量返回且按融合分降序（不报错、不补空位）。

        稀疏查询同时携带两条记录各自的词元，保证两记录确定被该路召回
        （哈希稠密向量对无关文本的余弦可能 ≤ 0，属 ANN「未命中不返回」语义）。
        """
        store, embedder = _make_store(FakeEmbeddingClient())
        hits = store.hybrid_search(
            sparse=embedder.sparse_vector("云台支架 摄影背包"), top_k=10
        )
        assert len(hits) == 2
        assert hits[0].score >= hits[1].score
        assert {h.chunk_id for h in hits} == {1, 2}

    def test_top_k_one_returns_single_best(self) -> None:
        """top_k=1（下界）：恰返回融合分最高的一条。"""
        store, embedder = _make_store(FakeEmbeddingClient())
        hits = store.hybrid_search(
            dense=embedder.dense_vector("云台支架 最大承载 85kg"),
            sparse=embedder.sparse_vector("云台支架 最大承载 85kg"),
            top_k=1,
        )
        assert len(hits) == 1 and hits[0].chunk_id == 1  # 双路第一名的 RRF 分必最高


class TestVisibilityFlipReplay:
    """降级/幂等重放：发布可见性翻转（FR-KB-017 存储侧语义）。"""

    def test_draft_invisible_then_publish_flip_then_replay(self) -> None:
        """draft 入库不可见 → 同主键重写为 published 后可见 → draft 重放再隐匿。

        稀疏查询通道保证确定性（词元重叠恒 > 0 / 无重叠恒 = 0）。
        """
        embedder = FakeEmbeddingClient()
        store = InMemoryVectorStore()
        record = VectorRecord(
            chunk_id=7,
            kb_type="skill",
            dense=embedder.dense_vector("询价接待话术 开场白"),
            sparse=embedder.sparse_vector("询价接待话术 开场白"),
            item_status="draft",  # 草稿预索引：入库但不参与检索（SR-KB-017 §6.2）
        )
        visible_query = embedder.sparse_vector("开场白")      # 与记录词元重叠
        blind_query = embedder.sparse_vector("售后物流")      # 与记录词元零重叠
        store.upsert([record])
        assert store.hybrid_search(sparse=visible_query, top_k=5) == []

        published = record.model_copy(update={"item_status": "published"})
        store.upsert([published])  # 发布：仅翻标量重写（幂等 upsert 覆盖）
        assert [h.chunk_id for h in store.hybrid_search(sparse=visible_query, top_k=5)] == [7]

        store.upsert([published])  # 发布事件重放：幂等，无副作用
        assert [h.chunk_id for h in store.hybrid_search(
            sparse=visible_query, top_k=5)] == [7]
        assert store.hybrid_search(sparse=blind_query, top_k=5) == []  # 零重叠不召回

        store.upsert([record.model_copy(update={"item_status": "draft"})])  # 回退草稿
        assert store.hybrid_search(sparse=visible_query, top_k=5) == []
