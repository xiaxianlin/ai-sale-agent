# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
"""对账任务单元测试（FR-KB-017 §6.5 / §9 T4：PG ↔ 向量库一致性自愈）。

覆盖：
- 无漂移：双侧一致时差集为空，修复零动作（幂等巡检）；
- 缺失（漏入库）：手动删向量模拟丢块 → 对账自动补回（重嵌入 + upsert），
  块仓 vector_status 保持 indexed（PG 权威不降级）；
- 孤儿（漏删除）：软删除中间崩溃残留 + 完全未知主键 → 按主键清除；
- 无嵌入端点的保守修复：缺失块置回 pending（deferred），交异步补偿；
- repair=False 巡检口径：只盘点不修复。

实现方案：docs/07-实现方案/KB/SR-KB-017-增量更新.md §6.5、§8（NFR-REL-03）
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from services.kb.core.pipeline.chunking.chunker import Chunk
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient
from services.kb.core.pipeline.embedding.embed_orchestrator import (
    VECTOR_STATUS_INDEXED,
    VECTOR_STATUS_PENDING,
    VECTOR_STATUS_REMOVED,
)
from services.kb.core.pipeline.sync import (
    ACTION_PUBLISH,
    IncrementalSyncEngine,
    InMemoryKnowledgeChunkStore,
    KnowledgeItemEvent,
    ReconcileTask,
)
from services.kb.core.vectorstore.base import VectorRecord
from services.kb.core.vectorstore.inmemory_impl import InMemoryVectorStore

BODY = "第一段：产品概述。\n\n第二段：技术参数。\n\n第三段：交付说明。"


class InspectableVectorStore(InMemoryVectorStore):
    """灌桩向量库：内存后端 + 主键清单（VectorInventory 能力）。"""

    def all_chunk_ids(self) -> set[int]:
        with self._lock:
            return set(self._records)


def paragraph_chunk_fn(body: str) -> list[Chunk]:
    """确定性重分块桩：按空行切段。"""
    return [Chunk(content=p.strip()) for p in body.split("\n\n") if p.strip()]


def _seed_published_item() -> tuple[IncrementalSyncEngine, InMemoryKnowledgeChunkStore, InspectableVectorStore, FakeEmbeddingClient]:
    """铺底：发布一个三块条目（块仓 indexed + 向量库三记录，双侧一致）。"""
    embedder = FakeEmbeddingClient()
    store = InMemoryKnowledgeChunkStore()
    vector_store = InspectableVectorStore()
    engine = IncrementalSyncEngine(
        paragraph_chunk_fn, store, vector_store, embedder,
    )
    report = engine.handle_event(KnowledgeItemEvent(
        item_id=101, body=BODY, kb_type="product", action=ACTION_PUBLISH,
    ))
    assert report.ok
    return engine, store, vector_store, embedder


def _make_task(
    store: InMemoryKnowledgeChunkStore,
    vector_store: InspectableVectorStore,
    embedder: FakeEmbeddingClient | None,
) -> ReconcileTask:
    return ReconcileTask(store, vector_store, vector_store, embedder=embedder)


# ── 差集发现与修复 ──────────────────────────────────────────────────────────


def test_reconcile_clean_state_reports_consistent() -> None:
    """无漂移：双侧一致 → 差集为空、consistent=True、修复零动作。"""
    _, store, vector_store, embedder = _seed_published_item()

    report = _make_task(store, vector_store, embedder).run()

    assert report.expected_ids == [1, 2, 3]
    assert report.actual_ids == [1, 2, 3]
    assert report.missing == [] and report.orphans == []
    assert report.repaired_missing == [] and report.repaired_orphans == []
    assert report.consistent


def test_reconcile_repairs_missing_chunk() -> None:
    """缺失（漏入库）：PG=indexed 而向量缺失 → 重嵌入补回，权威状态不降级。"""
    _, store, vector_store, embedder = _seed_published_item()
    vector_store.delete([2])  # 模拟人工误删 / Milvus 写入丢失
    assert vector_store.all_chunk_ids() == {1, 3}

    report = _make_task(store, vector_store, embedder).run()

    assert report.missing == [2]
    assert report.repaired_missing == [2]
    assert report.orphans == []
    assert report.consistent
    assert vector_store.all_chunk_ids() == {1, 2, 3}
    assert store.get_chunk(2).vector_status == VECTOR_STATUS_INDEXED
    # 补回记录带完整维度元数据与既有可见性（对齐 SR §6.5 自愈重嵌）
    hits = vector_store.hybrid_search(dense=embedder.dense_vector("第二段：技术参数。"), top_k=5)
    assert 2 in [hit.chunk_id for hit in hits]


def test_reconcile_repairs_missing_with_correct_item_status() -> None:
    """补回记录沿用块仓记录的可见性镜像（draft 条目补回仍不可检索）。"""
    embedder = FakeEmbeddingClient()
    store = InMemoryKnowledgeChunkStore()
    vector_store = InspectableVectorStore()
    engine = IncrementalSyncEngine(paragraph_chunk_fn, store, vector_store, embedder)
    engine.handle_event(KnowledgeItemEvent(
        item_id=303, body=BODY, kb_type="training", action="save",  # draft 预索引
    ))
    vector_store.delete([1, 2, 3])  # 三块全丢（模拟批量丢失）

    report = ReconcileTask(store, vector_store, vector_store, embedder=embedder).run()

    assert report.repaired_missing == [1, 2, 3]
    assert vector_store.all_chunk_ids() == {1, 2, 3}
    # draft 条目补回后仍不可检索（可见性口径不因对账改变）
    assert vector_store.hybrid_search(dense=embedder.dense_vector("第一段：产品概述。")) == []


def test_reconcile_cleans_orphans() -> None:
    """孤儿（漏删除）：软删除残留 + 未知主键 → 按主键清除（PG 权威）。"""
    _, store, vector_store, embedder = _seed_published_item()

    # ① 软删除中间崩溃残留：块仓已标 removed 而向量未删（对账权威差集场景）
    store.mark_removed([1])
    # ② 完全未知主键：块仓从未见过的向量（脏写 / 恢复演练残留）
    vector_store.upsert([VectorRecord(
        chunk_id=999, kb_type="product",
        dense=embedder.dense_vector("幽灵向量"),
        sparse=embedder.sparse_vector("幽灵向量"),
    )])

    report = _make_task(store, vector_store, embedder).run()

    assert report.missing == []          # indexed 块（2,3）向量俱在
    assert report.orphans == [1, 999]
    assert report.repaired_orphans == [1, 999]
    assert report.consistent
    assert vector_store.all_chunk_ids() == {2, 3}
    assert store.get_chunk(1).vector_status == VECTOR_STATUS_REMOVED  # 块仓行不动


def test_reconcile_defers_missing_without_embedder() -> None:
    """无嵌入端点（巡检态部署）：缺失块置回 pending 交异步补偿，不自行重嵌。"""
    _, store, vector_store, _ = _seed_published_item()
    vector_store.delete([3])

    report = _make_task(store, vector_store, None).run()

    assert report.missing == [3]
    assert report.deferred_missing == [3]
    assert report.repaired_missing == []
    assert report.consistent  # 已进入补偿通道（pending），本轮无未处理差异
    assert store.get_chunk(3).vector_status == VECTOR_STATUS_PENDING
    assert vector_store.all_chunk_ids() == {1, 2}  # 向量侧未动


def test_reconcile_inspect_only_without_repair() -> None:
    """repair=False 巡检口径：只盘点差集，不执行任何修复动作。"""
    _, store, vector_store, embedder = _seed_published_item()
    store.mark_removed([1, 2])     # 块仓侧 1、2 已 removed（应索引集只剩 3）
    vector_store.delete([1])       # 向量侧 1 已删、2 残留（模拟软删中间崩溃）

    report = _make_task(store, vector_store, embedder).run(repair=False)

    assert report.expected_ids == [3]
    assert report.missing == [] and report.orphans == [2]
    assert report.repaired_missing == [] and report.repaired_orphans == []
    assert not report.consistent
    assert vector_store.all_chunk_ids() == {2, 3}  # 未修复：向量原样


def test_reconcile_run_is_idempotent() -> None:
    """对账幂等：修复后立即重跑 → 差集为空，无重复动作。"""
    _, store, vector_store, embedder = _seed_published_item()
    vector_store.delete([2])
    vector_store.upsert([VectorRecord(
        chunk_id=777, kb_type="process",
        dense=embedder.dense_vector("孤儿"),
        sparse=embedder.sparse_vector("孤儿"),
    )])
    first = _make_task(store, vector_store, embedder).run()
    assert first.missing == [2] and first.orphans == [777]

    second = _make_task(store, vector_store, embedder).run()

    assert second.missing == [] and second.orphans == []
    assert second.consistent
    assert vector_store.all_chunk_ids() == {1, 2, 3}
