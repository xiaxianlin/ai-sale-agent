# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""增量更新（FR-KB-017 / SR-KB-017）TDD 补齐测试（五类场景补强，不改既有文件）。

按 TDD 新规五列审计既有 test_sync_* 后补齐：
- 边界：空正文事件 = 全部块失效（diff 空新集的引擎级贯通）+ 软删重放幂等；
  嵌入恒失败 max_retries=2 的退避序列（2s/4s）与死信 attempts 口径；
- 异常：非法 action / 非法 kb_type 在任何写库前被拒绝（fail-fast，无半写状态）；
- 并发（幂等重放）：同一 publish 事件重放 → 零嵌入零写入、reused 全量；
- 降级：单次嵌入失败后恢复 → 指数退避重试自愈 indexed（无死信）。

KPI 映射：SRS §16.1「知识更新分钟级生效」——重放幂等与断点自愈保证
「更新可见」不因重放/瞬态故障产生重复向量或丢失（向量一致性 = 检索准确率前提）。

依据文档：docs/07-实现方案/KB/SR-KB-017-增量更新.md §6.1、§6.2、§6.5、§9。
"""
from __future__ import annotations

from typing import Any

import pytest

from services.kb.core.pipeline.embedding.base import EmbeddingError, EmbeddingResult
from services.kb.core.pipeline.sync.engine import (
    ACTION_PUBLISH,
    ACTION_SOFT_DELETE,
    InMemoryKnowledgeChunkStore,
    IncrementalSyncEngine,
    KnowledgeItemEvent,
    VECTOR_STATUS_FAILED,
    VECTOR_STATUS_INDEXED,
    make_text_chunk_fn,
)
from services.kb.core.vectorstore import InMemoryVectorStore

BODY = (
    "# 产品参数\n\n支架最大承载八十五公斤。\n\n"
    "# 售后政策\n\n整机保修十二个月。\n\n"
    "# 报价流程\n\n询价后两个工作日报价。"
)  # 三个 Markdown 小节标题 → 三条独立 chunk（跨小节不合并）


class _CountingEmbedder:
    """可计数假嵌入器：恒成功，记录调用次数与入参快照。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_batch(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        return EmbeddingResult(
            dense=[[0.1] * 1024 for _ in texts],
            sparse=[{1: 1.0} for _ in texts],
        )


class _FlakyEmbedder:
    """前 fail_times 次调用抛 EmbeddingError，其后成功（瞬态故障桩）。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def embed_batch(self, texts: list[str]) -> EmbeddingResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise EmbeddingError("TEI 瞬态不可用（测试桩）")
        return EmbeddingResult(
            dense=[[0.2] * 1024 for _ in texts],
            sparse=[{2: 1.0} for _ in texts],
        )


class _DeadEmbedder:
    """恒失败嵌入桩（死信路径）。"""

    def embed_batch(self, texts: list[str]) -> EmbeddingResult:
        raise EmbeddingError("TEI 持续不可用（测试桩）")


def _engine(
    embedder: Any = None,
    *,
    store: InMemoryKnowledgeChunkStore | None = None,
    vector_store: InMemoryVectorStore | None = None,
    **kwargs: Any,
) -> tuple[IncrementalSyncEngine, InMemoryKnowledgeChunkStore, InMemoryVectorStore]:
    store = store if store is not None else InMemoryKnowledgeChunkStore()
    vector_store = vector_store if vector_store is not None else InMemoryVectorStore()
    engine = IncrementalSyncEngine(
        make_text_chunk_fn(), store, vector_store,
        embedder if embedder is not None else _CountingEmbedder(), **kwargs,
    )
    return engine, store, vector_store


class TestEmptyBodyAndReplay:
    """边界 + 幂等重放：空正文全失效、软删重放幂等、发布重放零成本。"""

    def test_empty_body_event_removes_all_vectors_then_replay_idempotent(self) -> None:
        """空正文（清空内容）：全部块 removed + 向量删除；重放第二次零删除且 ok。"""
        engine, store, vector_store = _engine()
        engine.handle_event(KnowledgeItemEvent(
            item_id=7, body=BODY, kb_type="product", action=ACTION_PUBLISH))
        assert len(vector_store) == 3

        cleared = engine.handle_event(KnowledgeItemEvent(
            item_id=7, body="", kb_type="product", action=ACTION_SOFT_DELETE))
        assert cleared.ok and cleared.deleted_vectors == 3
        assert len(vector_store) == 0
        assert store.alive_chunks(7) == []  # 全部 removed（行保留审计，alive 不含）

        replay = engine.handle_event(KnowledgeItemEvent(
            item_id=7, body="", kb_type="product", action=ACTION_SOFT_DELETE))
        assert replay.ok and replay.deleted_vectors == 0  # 幂等重放：无向量可删

    def test_double_publish_replay_zero_cost(self) -> None:
        """同一 publish 事件重放：diff 全 unchanged、零嵌入零写入、reused 恰为块数。"""
        embedder = _CountingEmbedder()
        engine, store, vector_store = _engine(embedder)
        first = engine.handle_event(KnowledgeItemEvent(
            item_id=8, body=BODY, kb_type="product", action=ACTION_PUBLISH))
        assert first.ok and first.embedded == 3
        calls_after_first = len(embedder.calls)

        replay = engine.handle_event(KnowledgeItemEvent(
            item_id=8, body=BODY, kb_type="product", action=ACTION_PUBLISH))
        assert replay.ok
        assert replay.reused == 3 and replay.embedded == 0
        assert replay.upserted == 0 and replay.flipped == 0  # 已 published：无可翻块
        assert len(embedder.calls) == calls_after_first  # 零嵌入调用
        assert len(vector_store) == 3  # 向量库无重复写入
        assert all(c.vector_status == VECTOR_STATUS_INDEXED for c in store.alive_chunks(8))


class TestEmbedRetryAndDlqBoundaries:
    """降级/异常：退避重试序列、自愈与死信 attempts 口径。"""

    def test_transient_failure_recovers_with_backoff(self) -> None:
        """首批失败后恢复：置 failed → 退避 2s 重试 → indexed，无死信、恰告警链无 DLQ。"""
        sleeps: list[float] = []
        alerts: list[str] = []
        engine, store, _vs = _engine(
            _FlakyEmbedder(fail_times=1),
            sleep=sleeps.append,
            alert=lambda event, _payload: alerts.append(event),
        )
        report = engine.handle_event(KnowledgeItemEvent(
            item_id=9, body=BODY, kb_type="product", action=ACTION_PUBLISH))
        assert report.ok  # 自愈成功：无降级无死信
        assert report.embedded == 3
        assert sleeps == [2.0]  # 指数退避 base**attempt：首次重试 2^1
        assert len(engine.dlq) == 0
        assert alerts == []  # 未耗尽重试不进死信 → 无 SYNC_DLQ 告警
        assert all(c.vector_status == VECTOR_STATUS_INDEXED for c in store.alive_chunks(9))

    def test_persistent_failure_backoff_sequence_then_dlq(self) -> None:
        """持续失败（max_retries=2）：退避 2s→4s 后耗尽，死信 attempts=3、SYNC_DLQ 恰一次。"""
        sleeps: list[float] = []
        alerts: list[tuple[str, dict]] = []
        engine, store, _vs = _engine(
            _DeadEmbedder(), max_retries=2,
            sleep=sleeps.append,
            alert=lambda event, payload: alerts.append((event, payload)),
        )
        report = engine.handle_event(KnowledgeItemEvent(
            item_id=10, body=BODY, kb_type="product", action=ACTION_PUBLISH))
        assert not report.ok
        assert sleeps == [2.0, 4.0]  # base**attempt：2^1、2^2（重试 2 次）
        assert len(engine.dlq) == 3  # 按块进死信
        assert all(entry.attempts == 3 for entry in engine.dlq)  # 1 原生 + 2 重试
        assert sorted(report.dlq_chunk_ids) == sorted(
            c.chunk_id for c in store.alive_chunks(10))
        assert all(c.vector_status == VECTOR_STATUS_FAILED for c in store.alive_chunks(10))
        assert [event for event, _payload in alerts] == ["SYNC_DLQ"]


class TestEventValidationFailFast:
    """异常：入口校验在任何落库前拒绝（无半写状态）。"""

    @pytest.mark.parametrize(
        "event",
        [
            KnowledgeItemEvent(item_id=1, body="x", kb_type="product", action="publish_now"),
            KnowledgeItemEvent(item_id=1, body="x", kb_type="unknown_kb", action=ACTION_PUBLISH),
        ],
        ids=["bad_action", "bad_kb_type"],
    )
    def test_invalid_event_rejected_before_any_write(self, event: KnowledgeItemEvent) -> None:
        engine, store, vector_store = _engine()
        with pytest.raises(ValueError):
            engine.handle_event(event)
        assert store.alive_chunks(1) == []  # 零落库
        assert len(vector_store) == 0  # 零写向量库
