# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
"""增量同步引擎单元测试（FR-KB-017 §9，任务口径：端到端 / 补偿 / 幂等）。

覆盖（SR-KB-017 §9 测试要点引擎部分）：
- 端到端增量：draft 保存预索引不可见 → publish 翻转可见（分钟级时效模拟，
  灌桩 VectorStore 断言各段耗时在预算内）；
- 部分修改：仅变更块重嵌入（未变块零成本复用），失效块向量删除；
- 幂等重放：同一事件重放零嵌入零写入；
- 失败补偿：嵌入失败 → failed + 指数退避重试 → 重试耗尽进 DLQ；服务
  恢复后 failed 块随下一次事件自愈重嵌；
- 软删除 / 归档：块仓行保留、向量彻底清除、检索不可见；
- 超时降级：分段超预算 → 降级登记异步补偿，断点重入自愈；
- 可见性后端无翻转能力时的重嵌入降级路径；
- 真实 SemanticChunker 重分块接入回归。

检索断言口径：FakeEmbeddingClient 的向量以文本哈希为种子，同文本查询
与记录向量完全一致（稠密余弦 = 稀疏点积 = 满分，双路排名均第一），
故「精确文本 → top-1 命中」确定性成立；跨文本相似度为随机量，
内容消失类断言一律用主键集合 / 成员关系表达。
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from typing import Any

import pytest

from services.kb.core.pipeline.chunking.chunker import Chunk
from services.kb.core.pipeline.embedding.base import EmbeddingError
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient
from services.kb.core.pipeline.embedding.embed_orchestrator import (
    VECTOR_STATUS_FAILED,
    VECTOR_STATUS_INDEXED,
    VECTOR_STATUS_PENDING,
    VECTOR_STATUS_REMOVED,
    DeadLetterQueue,
)
from services.kb.core.pipeline.sync import (
    ACTION_ARCHIVE,
    ACTION_PUBLISH,
    ACTION_SAVE,
    ACTION_SOFT_DELETE,
    SYNC_EVENT_DLQ,
    IncrementalSyncEngine,
    InMemoryKnowledgeChunkStore,
    KnowledgeItemEvent,
    SyncTimeBudget,
    make_text_chunk_fn,
)
from services.kb.core.vectorstore.inmemory_impl import InMemoryVectorStore

BODY = "第一段：产品概述。\n\n第二段：技术参数。\n\n第三段：交付说明。"
BODY_MODIFIED = "第一段：产品概述。\n\n第二段：技术参数（已更新）。\n\n第三段：交付说明。"
OTHER_BODY = "条目二甲。\n\n条目二乙。\n\n条目二丙。"


# ── 测试桩 ──────────────────────────────────────────────────────────────────


class FlippableVectorStore(InMemoryVectorStore):
    """灌桩向量库：在内存后端之上补齐 ItemStatusFlipper 能力（发布仅翻标量）。

    生产对位：SR-KB-017 §6.2 visibility.flip_item_status（Milvus 取回整行
    改 item_status 重写 upsert）；frozen 模型经 model_copy 覆盖字段。
    """

    def flip_item_status(self, chunk_ids: list[int], item_status: str) -> None:
        with self._lock:
            for chunk_id in chunk_ids:
                record = self._records.get(chunk_id)
                if record is not None:
                    self._records[chunk_id] = record.model_copy(
                        update={"item_status": item_status}
                    )

    def record_status(self, chunk_id: int) -> str | None:
        """读取记录 item_status（测试断言用，不属于检索协议）。"""
        with self._lock:
            record = self._records.get(chunk_id)
            return record.item_status if record is not None else None


class InspectableVectorStore(FlippableVectorStore):
    """附带主键清单的灌桩向量库（对账测试 VectorInventory 能力）。"""

    def all_chunk_ids(self) -> set[int]:
        with self._lock:
            return set(self._records)


class FakeClock:
    """模拟时钟：每次读数步进固定毫秒数（分钟级时效的确定性模拟）。"""

    def __init__(self, step_ms: float = 100.0) -> None:
        self._now = 0.0
        self._step = step_ms / 1000.0

    def __call__(self) -> float:
        self._now += self._step
        return self._now


class SwitchableEmbedder:
    """可切换嵌入端点：error 非空时抛 EmbeddingError（模拟 TEI 不可用）。

    attempts 统计含失败尝试（抛错不透传到内层假客户端，需自行计数）。
    """

    def __init__(self) -> None:
        self.error: str | None = None
        self.inner = FakeEmbeddingClient()
        self.attempts = 0  # embed_batch 调用总次数（含抛错失败）

    @property
    def call_count(self) -> int:
        return self.inner.call_count

    @property
    def calls(self) -> list[list[str]]:
        return self.inner.calls

    def embed_batch(self, texts: list[str]) -> Any:
        self.attempts += 1
        if self.error is not None:
            raise EmbeddingError(self.error, endpoint="http://tei.internal")
        return self.inner.embed_batch(texts)

    def query(self, text: str) -> tuple[list[float], dict[int, float]]:
        """按精确文本构造双路查询向量（与同文本记录向量逐分量一致）。"""
        return self.inner.dense_vector(text), self.inner.sparse_vector(text)


def paragraph_chunk_fn(body: str) -> list[Chunk]:
    """确定性重分块桩：按空行切段（与真实分块器同构的最小替身）。"""
    return [Chunk(content=p.strip()) for p in body.split("\n\n") if p.strip()]


def make_engine(
    embedder: SwitchableEmbedder | None = None,
    store: InMemoryKnowledgeChunkStore | None = None,
    vector_store: FlippableVectorStore | None = None,
    *,
    budget: SyncTimeBudget | None = None,
    clock: FakeClock | None = None,
    sleeps: list[float] | None = None,
    dlq: DeadLetterQueue | None = None,
    alerts: list[tuple[str, dict[str, Any]]] | None = None,
    max_retries: int = 3,
    chunk_fn=paragraph_chunk_fn,
) -> tuple[IncrementalSyncEngine, InMemoryKnowledgeChunkStore, FlippableVectorStore, SwitchableEmbedder]:
    """组装被测引擎：内存块仓 + 灌桩向量库 + 可切换嵌入端点。"""
    embedder = embedder if embedder is not None else SwitchableEmbedder()
    store = store if store is not None else InMemoryKnowledgeChunkStore()
    vector_store = vector_store if vector_store is not None else InspectableVectorStore()
    collected: list[tuple[str, dict[str, Any]]] = alerts if alerts is not None else []

    def _alert(event: str, payload: dict[str, Any]) -> None:
        collected.append((event, payload))

    engine = IncrementalSyncEngine(
        chunk_fn,
        store,
        vector_store,
        embedder,
        time_budget=budget,
        clock=clock if clock is not None else FakeClock(),
        sleep=(lambda seconds: sleeps.append(seconds)) if sleeps is not None else (lambda seconds: None),
        dlq=dlq,
        alert=_alert,
        max_retries=max_retries,
    )
    return engine, store, vector_store, embedder


def _event(body: str = BODY, action: str = ACTION_SAVE, **overrides: Any) -> KnowledgeItemEvent:
    """构造知识条目事件（默认产品库草稿保存）。"""
    fields: dict[str, Any] = {
        "item_id": 101,
        "body": body,
        "kb_type": "product",
        "action": action,
        "sku": "ST-3200",
    }
    fields.update(overrides)
    return KnowledgeItemEvent(**fields)


def _hit_ids(vector_store: Any, embedder: SwitchableEmbedder, text: str) -> list[int]:
    """按精确文本发起双路混合检索，返回命中主键列表（top_k=5）。"""
    dense, sparse = embedder.query(text)
    return [hit.chunk_id for hit in vector_store.hybrid_search(dense=dense, sparse=sparse, top_k=5)]


def _top1(vector_store: Any, embedder: SwitchableEmbedder, text: str) -> int | None:
    """精确文本检索的 top-1 主键（同文本双路满分，确定性第一）。"""
    hits = _hit_ids(vector_store, embedder, text)
    return hits[0] if hits else None


# ── 端到端：draft 预索引 → publish 翻转（分钟级时效模拟）────────────────────


def test_save_draft_then_publish_visible_within_minute_budget() -> None:
    """draft 保存预索引（不可见）→ 发布翻标量秒级可见；全链路在分钟级预算内。"""
    engine, store, vector_store, embedder = make_engine()

    # ① 草稿保存：全部块预索引为 draft，检索不可见（03-数据库设计 §4.2 仅索引 published）
    report = engine.handle_event(_event(action=ACTION_SAVE))
    assert report.ok
    assert report.embedded == 3
    assert len(vector_store) == 3
    assert all(store.get_chunk(cid).vector_status == VECTOR_STATUS_INDEXED for cid in (1, 2, 3))
    assert all(vector_store.record_status(cid) == "draft" for cid in (1, 2, 3))
    assert _hit_ids(vector_store, embedder, "第一段：产品概述。") == []  # 草稿不可见

    # 分钟级时效：模拟时钟每次读数 +100ms，全链路远小于 60s 合计预算（SR §6.1）
    assert report.elapsed_ms < 60_000.0
    assert set(report.stage_elapsed_ms) == {"chunk", "embed", "index", "publish"}
    assert all(ms <= limit for ms, limit in zip(
        (report.stage_elapsed_ms[k] for k in ("chunk", "embed", "index", "publish")),
        (10_000.0, 30_000.0, 15_000.0, 5_000.0),
    ))

    # ② 发布：diff 全命中 unchanged → 仅翻 item_status，零重嵌入
    calls_before = embedder.call_count
    publish_report = engine.handle_event(_event(action=ACTION_PUBLISH))
    assert publish_report.ok
    assert publish_report.reused == 3
    assert publish_report.embedded == 0
    assert publish_report.flipped == 3
    assert embedder.call_count == calls_before  # 发布零嵌入成本
    assert all(vector_store.record_status(cid) == "published" for cid in (1, 2, 3))
    assert _top1(vector_store, embedder, "第一段：产品概述。") == 1  # 发布即可检索（秒级生效）
    assert set(_hit_ids(vector_store, embedder, "第一段：产品概述。")) <= {1, 2, 3}

    # 块仓可见性镜像同步翻转（indexed_status=published）
    assert all(store.get_chunk(cid).indexed_status == "published" for cid in (1, 2, 3))


def test_publish_without_flip_capability_reembeds() -> None:
    """后端无 ItemStatusFlipper 能力 → 可见性切换降级为重嵌入重写（正确性优先）。"""
    embedder = SwitchableEmbedder()
    store = InMemoryKnowledgeChunkStore()
    vector_store = InMemoryVectorStore()  # 冻结协议最小实现：不含翻转能力
    engine = IncrementalSyncEngine(
        paragraph_chunk_fn, store, vector_store, embedder, clock=FakeClock(),
    )
    engine.handle_event(_event(action=ACTION_SAVE))

    calls_before = embedder.call_count
    report = engine.handle_event(_event(action=ACTION_PUBLISH))

    assert report.reembedded_for_visibility == 3
    assert report.flipped == 0
    assert "visibility_flip_unsupported_reembed" in report.degraded
    assert embedder.call_count == calls_before + 1  # 3 块一批重嵌入（1 次调用）
    assert len(vector_store) == 3
    assert _top1(vector_store, embedder, "第三段：交付说明。") == 3


def test_body_modification_reembeds_only_changed_chunk() -> None:
    """修改一段正文 → 仅该块重嵌入入库，旧块向量删除、其余两块零成本复用。"""
    engine, store, vector_store, embedder = make_engine()
    engine.handle_event(_event(action=ACTION_PUBLISH))
    calls_before = embedder.call_count

    report = engine.handle_event(_event(body=BODY_MODIFIED, action=ACTION_PUBLISH))

    assert report.reused == 2
    assert report.embedded == 1
    assert report.deleted_vectors == 1
    assert embedder.call_count == calls_before + 1
    assert embedder.calls[-1] == ["第二段：技术参数（已更新）。"]  # 仅嵌入变更块
    assert len(vector_store) == 3  # 失效块已删 + 新块已入，总数不变
    assert store.get_chunk(2).vector_status == VECTOR_STATUS_REMOVED  # 旧块仓标记保留（审计）
    assert store.get_chunk(4).vector_status == VECTOR_STATUS_INDEXED  # 新块主键顺延
    assert _top1(vector_store, embedder, "第二段：技术参数（已更新）。") == 4  # 新内容可命中
    assert 2 not in _hit_ids(vector_store, embedder, "第二段：技术参数。")  # 旧失效块不再命中
    assert _top1(vector_store, embedder, "第一段：产品概述。") == 1  # 未变块不受影响


def test_idempotent_replay_is_zero_cost() -> None:
    """幂等重放：同一发布事件重放两次，第二次零嵌入 / 零写入 / 零删除。"""
    engine, store, vector_store, embedder = make_engine()
    engine.handle_event(_event(action=ACTION_PUBLISH))
    calls_before = embedder.call_count
    len_before = len(vector_store)

    replay = engine.handle_event(_event(action=ACTION_PUBLISH))

    assert replay.ok
    assert replay.reused == 3
    assert replay.embedded == 0 and replay.upserted == 0
    assert replay.flipped == 0 and replay.deleted_vectors == 0
    assert embedder.call_count == calls_before
    assert len(vector_store) == len_before
    assert len(store.alive_chunks(101)) == 3  # 无重复块（diff 去重吸收重放）


# ── 失败补偿：嵌入失败 → failed → 重试 → DLQ ───────────────────────────────


def test_embed_failure_marks_failed_retries_then_dlq() -> None:
    """嵌入失败：块置 failed，指数退避重试耗尽后按块进死信并告警。"""
    sleeps: list[float] = []
    dlq = DeadLetterQueue()
    alerts: list[tuple[str, dict[str, Any]]] = []
    embedder = SwitchableEmbedder()
    embedder.error = "TEI 服务不可用"
    engine, store, vector_store, _ = make_engine(
        embedder, dlq=dlq, alerts=alerts, sleeps=sleeps, max_retries=2,
    )

    report = engine.handle_event(_event(action=ACTION_PUBLISH, body="段甲。\n\n段乙。"))

    # 重试账目：1 次原生 + 2 次重试 = 3 次尝试；退避 2^1、2^2（base=2）
    assert embedder.attempts == 3
    assert sleeps == [2.0, 4.0]
    assert not report.ok
    assert report.dlq_chunk_ids == [1, 2]
    assert report.failed_chunk_ids == [1, 2]
    assert len(dlq) == 2
    assert all(entry.attempts == 3 for entry in dlq)
    assert all(store.get_chunk(cid).vector_status == VECTOR_STATUS_FAILED for cid in (1, 2))
    assert len(vector_store) == 0  # 失败块不写向量
    assert alerts and alerts[0][0] == SYNC_EVENT_DLQ

    # 服务恢复：同一事件重放 → failed 块自愈重嵌 → indexed，死信不重复累积
    embedder.error = None
    healed = engine.handle_event(_event(action=ACTION_PUBLISH, body="段甲。\n\n段乙。"))
    assert healed.ok
    assert healed.embedded == 2
    assert all(store.get_chunk(cid).vector_status == VECTOR_STATUS_INDEXED for cid in (1, 2))
    assert len(vector_store) == 2
    assert _top1(vector_store, embedder, "段甲。") == 1
    assert len(dlq) == 2  # 死信留痕保留，不因自愈回抽


# ── 软删除 / 归档：向量清理 ─────────────────────────────────────────────────


def test_soft_delete_and_archive_clear_vectors() -> None:
    """软删除：块仓行保留（removed）、向量彻底清除、检索不可见；归档同路径。"""
    engine, store, vector_store, embedder = make_engine()
    engine.handle_event(_event(action=ACTION_PUBLISH))
    engine.handle_event(_event(item_id=202, body=OTHER_BODY, action=ACTION_PUBLISH))

    report = engine.handle_event(_event(action=ACTION_SOFT_DELETE))

    assert report.ok and report.deleted_vectors == 3
    assert store.alive_chunks(101) == []
    assert all(store.get_chunk(cid).vector_status == VECTOR_STATUS_REMOVED for cid in (1, 2, 3))
    assert all(store.get_chunk(cid).indexed_status == "" for cid in (1, 2, 3))
    assert vector_store.all_chunk_ids() == {4, 5, 6}  # 条目 101 向量清除，条目 202 保留
    assert 1 not in _hit_ids(vector_store, embedder, "第一段：产品概述。")  # 检索不可见

    archive_report = engine.handle_event(_event(item_id=202, body=OTHER_BODY, action=ACTION_ARCHIVE))
    assert archive_report.ok and archive_report.deleted_vectors == 3
    assert len(vector_store) == 0  # 全库向量清空


# ── 超时降级：分段预算 → 异步补偿 → 断点重入自愈 ────────────────────────────


def test_stage_overrun_degrades_to_async_compensation_then_heals() -> None:
    """分段超预算：降级中止同步链路、登记补偿，未完成块滞留 pending；
    预算恢复后的下一次事件断点重入自愈（SR §6.5 / §8 NFR-REL-01）。"""
    clock = FakeClock()
    embedder = SwitchableEmbedder()
    store = InMemoryKnowledgeChunkStore()
    vector_store = InspectableVectorStore()
    degraded_engine = IncrementalSyncEngine(
        paragraph_chunk_fn, store, vector_store, embedder,
        time_budget=SyncTimeBudget(chunk_ms=0.0),  # 分块段预算 0：任何耗时都超限
        clock=clock,
    )

    report = degraded_engine.handle_event(_event(action=ACTION_PUBLISH))

    assert not report.ok
    assert report.degraded and report.degraded[0].startswith("chunk_overrun")
    assert report.embedded == 0 and embedder.call_count == 0  # 嵌入段未执行
    assert len(vector_store) == 0                              # 入库段未执行
    alive = store.alive_chunks(101)
    assert len(alive) == 3  # 块仓已落库（chunk 段含 PG 先行落库）
    assert all(chunk.vector_status == VECTOR_STATUS_PENDING for chunk in alive)

    compensations = degraded_engine.drain_compensations()
    assert len(compensations) == 1
    assert compensations[0].item_id == 101 and compensations[0].action == ACTION_PUBLISH
    assert degraded_engine.drain_compensations() == []  # 取走即清

    # 预算恢复：同事件重放 → pending 块自愈嵌入入库，检索可见
    healed_engine = IncrementalSyncEngine(
        paragraph_chunk_fn, store, vector_store, embedder, clock=clock,
    )
    healed = healed_engine.handle_event(_event(action=ACTION_PUBLISH))
    assert healed.ok
    assert healed.embedded == 3
    assert all(store.get_chunk(cid).vector_status == VECTOR_STATUS_INDEXED for cid in (1, 2, 3))
    assert _top1(vector_store, embedder, "第二段：技术参数。") == 2


# ── 真实分块器接入回归 ──────────────────────────────────────────────────────


def test_real_semantic_chunker_roundtrip() -> None:
    """真实 SemanticChunker 重分块：分块确定性使同正文 diff 全命中，
    改一小节（标题改写改变 heading_path）仅重嵌该节。"""
    markdown_body = (
        "# 产品概述\n\n本产品是一款高端工业支架，适用于重型设备。\n\n"
        "## 技术参数\n\n材质为铝合金，承重 200 公斤。\n\n"
        "## 交付说明\n\n下单后 30 天内发货，含安装说明。"
    )
    embedder = SwitchableEmbedder()
    store = InMemoryKnowledgeChunkStore()
    vector_store = InspectableVectorStore()
    engine = IncrementalSyncEngine(
        make_text_chunk_fn(), store, vector_store, embedder, clock=FakeClock(),
    )

    first = engine.handle_event(_event(body=markdown_body, action=ACTION_PUBLISH))
    assert first.ok and first.embedded == 3  # 三个标题小节各一块

    calls_before = embedder.call_count
    replay = engine.handle_event(_event(body=markdown_body, action=ACTION_PUBLISH))
    assert replay.reused == 3 and replay.embedded == 0  # 同正文重分块结果一致 → 全复用
    assert embedder.call_count == calls_before

    modified = markdown_body.replace("## 技术参数", "## 技术规格").replace(
        "材质为铝合金", "材质为航空铝材"
    )
    partial = engine.handle_event(_event(body=modified, action=ACTION_PUBLISH))
    assert partial.reused == 2 and partial.embedded == 1  # 仅该小节重嵌
    assert _top1(vector_store, embedder, "材质为航空铝材，承重 200 公斤。") is not None


# ── 事件契约校验 ────────────────────────────────────────────────────────────


def test_event_validation_rejects_bad_payloads() -> None:
    """入口 fail-fast：非法 kb_type / action / item_id / 超长维度直接拒绝。"""
    engine, _, _, _ = make_engine()
    with pytest.raises(ValueError, match="kb_type"):
        engine.handle_event(_event(kb_type="unknown_kb"))
    with pytest.raises(ValueError, match="action"):
        engine.handle_event(_event(action="explode"))
    with pytest.raises(ValueError, match="item_id"):
        engine.handle_event(_event(item_id=0))
    with pytest.raises(ValueError, match="sku 超长"):
        engine.handle_event(_event(sku="X" * 65))
