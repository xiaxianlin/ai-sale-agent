# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""向量化服务（FR-KB-013 / SR-KB-013）TDD 补齐测试（五类场景补强，不改既有文件）。

按 TDD 新规五列审计既有 test_embedding_* 后补齐：
- 边界：编排器 max_retries=0（首次失败即死信）、batch_size=1（逐条分片）、
  FakeEmbeddingClient dense_dim=0 参数校验；
- 异常：TEI 响应非合法 JSON（200 + 脏响应体）——立即失败不重试（恰一次调用）；
- 并发：共享 FakeEmbeddingClient 跨线程确定性无串扰（calls 快照不丢）；
- 降级：TEI 整体不可用 → EmbeddingError（EmbeddingError 分支不重试直接上抛）、
  任务滞留队列不转 failed、EMB_DOWN 恰告警一次（与 DLQ 告警区分）。

KPI 映射：NFR-SEC-06「数据不出域」——出域断言既有测试已全覆盖（公网/https/
DNS 解析），本组补充聚焦向量化失败时的降级语义（滞留队列零丢失 = §16.1
入库链路可靠性）与维度假实现参数红线（1024 维冻结，COM-IP-03）。

依据文档：docs/07-实现方案/KB/SR-KB-013-向量化服务.md §6.1、§6.2、§6.3、§9。
"""
from __future__ import annotations

import threading

import httpx
import pytest

from services.kb.core.pipeline.embedding.base import EmbeddingError, EmbeddingResult
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient, TeiEmbeddingClient
from services.kb.core.pipeline.embedding.embed_orchestrator import (
    EMBED_EVENT_DOWN,
    EmbedOrchestrator,
    InMemoryChunkStore,
    VECTOR_STATUS_FAILED,
    VECTOR_STATUS_PENDING,
)


class _FailingSink:
    """恒失败向量入库桩：单块失败路径驱动。"""

    def __init__(self) -> None:
        self.calls = 0

    def upsert(self, chunk_id: int, dense: list[float], sparse: dict[int, float]) -> None:
        self.calls += 1
        raise RuntimeError("Milvus 写入失败（测试桩）")


class TestOrchestratorBoundaries:
    """边界：max_retries=0 与 batch_size=1。"""

    def test_max_retries_zero_goes_straight_to_dlq(self) -> None:
        """max_retries=0：单块失败不自动重推，首次失败即进死信（attempts=1）。"""
        store = InMemoryChunkStore()
        orchestrator = EmbedOrchestrator(
            FakeEmbeddingClient(), store, _FailingSink(), max_retries=0, batch_size=1
        )
        orchestrator.enqueue(1, "待向量化文本")
        report = orchestrator.process_queue()
        assert report.failed == 1 and report.dlq == 1 and report.indexed == 0
        assert len(orchestrator.dlq) == 1
        assert orchestrator.dlq.entries[0].attempts == 1
        assert store.get_vector_status(1) == VECTOR_STATUS_FAILED

    def test_batch_size_one_one_call_per_task(self) -> None:
        """batch_size=1：3 个任务 → 3 次客户端调用、每次恰好 1 条（分片下界）。"""
        embedder = FakeEmbeddingClient()
        orchestrator = EmbedOrchestrator(
            embedder, InMemoryChunkStore(), _MemorySink(), batch_size=1
        )
        orchestrator.enqueue_many([(i, f"文本{i}") for i in (1, 2, 3)])
        report = orchestrator.process_queue()
        assert report.indexed == 3
        assert embedder.call_count == 3
        assert all(len(batch) == 1 for batch in embedder.calls)

    def test_fake_client_rejects_zero_dense_dim(self) -> None:
        """假实现维度红线：dense_dim < 1 构造即 ValueError（1024 维冻结的参数防线）。"""
        with pytest.raises(ValueError, match="dense_dim"):
            FakeEmbeddingClient(dense_dim=0)


class TestTeiMalformedJson:
    """异常：200 + 非 JSON 响应体——立即失败不重试（与超时/5xx 重试路径区分）。"""

    def test_malformed_json_single_call_no_retry(self) -> None:
        """脏响应体：EmbeddingError 即刻上抛，HTTP 恰调用 1 次（重试耗尽路径不复用）。"""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, content=b"<html>not json</html>")

        client = TeiEmbeddingClient(
            "http://tei-embedding:8080",
            retries=3,
            transport=httpx.MockTransport(handler),
            sleep=lambda _s: None,
        )
        with pytest.raises(EmbeddingError) as exc_info:
            client.embed_batch(["文本"])
        assert "非合法 JSON" in str(exc_info.value)
        assert calls["n"] == 1  # 不重试：响应体损坏非瞬态故障

    def test_tei_unavailable_raises_embedding_error_not_generic(self) -> None:
        """TEI 不可用（连接拒绝）：重试耗尽 → EmbeddingError（供上层降级 BM25）。"""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("connection refused")

        client = TeiEmbeddingClient(
            "http://tei-embedding:8080",
            retries=1,
            transport=httpx.MockTransport(handler),
            sleep=lambda _s: None,
        )
        with pytest.raises(EmbeddingError) as exc_info:
            client.embed_batch(["文本"])
        assert calls["n"] == 2  # 原生 1 次 + 重试 1 次
        assert "连续失败" in str(exc_info.value)
        assert client.consecutive_failures == 1  # 重试耗尽计数 +1


class TestServiceUnavailableDegradation:
    """降级：TEI 整体不可用 → 任务滞留队列（零丢失），EMB_DOWN 恰告警一次。"""

    def test_unavailable_tasks_retained_and_alerted_once(self) -> None:
        """嵌入恒失败：状态保持 pending（不转 failed）、任务滞留、告警一次带积压量。"""
        store = InMemoryChunkStore()
        alerts: list[tuple[str, dict]] = []
        orchestrator = EmbedOrchestrator(
            _AlwaysDownEmbedder(), store, _MemorySink(), batch_size=2,
            alert=lambda event, payload: alerts.append((event, payload)),
        )
        orchestrator.enqueue_many([(i, f"文本{i}") for i in (1, 2, 3)])
        report = orchestrator.process_queue()
        assert report.service_unavailable is True
        assert report.processed == 0  # 未消费任何任务（不含滞留）
        assert orchestrator.pending_count == 3  # 批次原序回队滞留
        assert all(store.get_vector_status(i) == VECTOR_STATUS_PENDING for i in (1, 2, 3))
        assert [event for event, _payload in alerts] == [EMBED_EVENT_DOWN]  # 恰一次
        assert alerts[0][1]["pending"] == 3

    def test_recovery_after_unavailable_completes_remaining(self) -> None:
        """恢复后续跑：不可用滞留后换可用客户端（重注入编排器）→ 零丢失完成。"""
        store = InMemoryChunkStore()
        down = EmbedOrchestrator(
            _AlwaysDownEmbedder(), store, _MemorySink(), batch_size=2
        )
        down.enqueue_many([(i, f"文本{i}") for i in (1, 2, 3)])
        down.process_queue()
        healthy = EmbedOrchestrator(
            FakeEmbeddingClient(), store, _MemorySink(), batch_size=2
        )
        healthy._queue = down._queue  # 模拟同一队列续跑（生产为 Redis Stream 重投）
        report = healthy.process_queue()
        assert report.indexed == 3 and report.service_unavailable is False
        assert all(store.get_vector_status(i) == "indexed" for i in (1, 2, 3))


class TestSharedClientConcurrency:
    """并发：共享假客户端跨线程确定性无串扰（calls 快照恰好各线程一条）。"""

    def test_shared_fake_client_thread_safe_determinism(self) -> None:
        """8 线程共享一个 FakeEmbeddingClient：结果与单线程一致、calls 记录不丢。"""
        embedder = FakeEmbeddingClient()
        text = "共享客户端并发文本"
        reference = embedder.embed_batch([text]).dense[0]
        results: list[list[float]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            barrier.wait()
            dense = embedder.embed_batch([f"{text}{index}"]).dense[0]
            with lock:
                results.append(dense)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 8
        assert embedder.call_count == 9  # 基准 1 次 + 并发 8 次（快照无丢失/串写）
        same_text = embedder.embed_batch([text]).dense[0]
        assert same_text == reference  # 确定性：同文本跨线程同向量


class _MemorySink:
    """恒成功向量入库桩（内存丢弃）。"""

    def upsert(self, chunk_id: int, dense: list[float], sparse: dict[int, float]) -> None:
        return None


class _AlwaysDownEmbedder:
    """TEI 整体不可用桩：恒抛 EmbeddingError（非单块失败）。"""

    def embed_batch(self, texts: list[str]) -> EmbeddingResult:
        raise EmbeddingError("TEI 服务不可用（测试桩）")
