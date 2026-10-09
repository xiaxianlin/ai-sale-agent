# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""批量向量化编排单元测试（FR-KB-013 §6.3/§9）。

覆盖（对应实现方案 §9 测试要点）：
- 状态机：pending -> indexed（先入库后回写状态、vector_id=chunk_id）；
- 幂等：已 indexed/removed 任务重投直接跳过（零 embed 调用）；
- 分批对齐：70 任务 -> 3 次调用（32/32/6），失败重推任务回到后续批次；
- 单块失败：转 failed 自动重推，重试耗尽进 DLQ + 告警钩子（EMB_DLQ）；
- TEI 不可用（EmbeddingError）：任务滞留队列零丢失，恢复后续跑全部入库；
- requeue：failed 人工重推回 pending。

实现方案：docs/07-实现方案/KB/SR-KB-013-向量化服务.md §6.3、§7、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import importlib.util
import os
import sys
import sysconfig
from types import ModuleType
from typing import Any


def _bootstrap_stdlib_platform() -> None:
    """恢复标准库 platform 语义（幂等，须先于 embedding 包导入执行）。

    同 test_embedding_client.py：仓库顶层 ``platform`` 包遮蔽标准库，
    embedding.client→httpx→rich→attrs 需要 ``platform.python_implementation()``；
    与 platform/tests/conftest.py 同法回填，本地包子模块导入不受影响。
    """
    cached: ModuleType | None = sys.modules.get("platform")
    if cached is not None and hasattr(cached, "python_implementation"):
        return
    std_path = os.path.join(sysconfig.get_paths()["stdlib"], "platform.py")
    spec = importlib.util.spec_from_file_location("platform", std_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法定位标准库 platform 模块（测试环境损坏）")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if cached is None:
        sys.modules["platform"] = module
        return
    for name, value in vars(module).items():
        if not name.startswith("__"):
            setattr(cached, name, value)


_bootstrap_stdlib_platform()

import pytest  # noqa: E402

from services.kb.core.pipeline.embedding.base import DENSE_DIM, EmbeddingError  # noqa: E402
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient
from services.kb.core.pipeline.embedding.embed_orchestrator import (
    EMBED_EVENT_DLQ,
    EMBED_EVENT_DOWN,
    EMBED_EVENT_FAILED,
    VECTOR_STATUS_FAILED,
    VECTOR_STATUS_INDEXED,
    VECTOR_STATUS_PENDING,
    VECTOR_STATUS_REMOVED,
    DeadLetterQueue,
    EmbedOrchestrator,
    InMemoryChunkStore,
)


class FlakySink:
    """可控向量入库桩：按 chunk_id 指定失败次数（倒计时耗尽后成功）。"""

    def __init__(self, fail_schedule: dict[int, int] | None = None) -> None:
        self.fail_schedule = dict(fail_schedule or {})
        self.upserts: list[tuple[int, int, int]] = []  # (chunk_id, dense 维度, sparse 非零项数)

    def upsert(self, chunk_id: int, dense: list[float], sparse: dict[int, float]) -> None:
        remaining = self.fail_schedule.get(chunk_id, 0)
        if remaining > 0:
            self.fail_schedule[chunk_id] = remaining - 1
            raise RuntimeError(f"Milvus upsert 模拟失败：chunk {chunk_id}")
        self.upserts.append((chunk_id, len(dense), len(sparse)))


class SwitchableClient:
    """可切换客户端：error 非空时模拟 TEI 整体不可用（抛 EmbeddingError）。"""

    def __init__(self) -> None:
        self.error: str | None = None
        self.inner = FakeEmbeddingClient()

    def embed_batch(self, texts: list[str]) -> Any:
        if self.error is not None:
            raise EmbeddingError(self.error)
        return self.inner.embed_batch(texts)


def _make_orch(
    client: Any | None = None,
    sink: FlakySink | None = None,
    store: InMemoryChunkStore | None = None,
    *,
    max_retries: int = 3,
    batch_size: int = 32,
    alerts: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[EmbedOrchestrator, InMemoryChunkStore, FlakySink]:
    """组装被测编排器：内存状态仓 + 可控入库桩 + 告警收集钩子。"""
    client = client if client is not None else FakeEmbeddingClient()
    sink = sink if sink is not None else FlakySink()
    store = store if store is not None else InMemoryChunkStore()
    collected: list[tuple[str, dict[str, Any]]] = alerts if alerts is not None else []

    def _alert(event: str, payload: dict[str, Any]) -> None:
        """告警收集钩子：把 (事件名, 载荷) 追加到测试列表（未传入时收集到丢弃列表）。"""
        collected.append((event, payload))
    orch = EmbedOrchestrator(
        client, store, sink, max_retries=max_retries, batch_size=batch_size, alert=_alert
    )
    return orch, store, sink


# ─────────────────────────── 状态机 happy path ───────────────────────────

class TestStateMachine:
    """vector_status：pending -> indexed，先入库后回写。"""

    def test_pending_to_indexed(self) -> None:
        """正常链路：全部 pending -> indexed，vector_id=chunk_id，先入库后回写状态。"""
        orch, store, sink = _make_orch()
        orch.enqueue_many([(i, f"知识块 {i}") for i in (1, 2, 3)])
        report = orch.process_queue()

        assert report.indexed == 3
        assert report.failed == 0
        assert report.service_unavailable is False
        assert orch.pending_count == 0
        for cid in (1, 2, 3):
            assert store.get_vector_status(cid) == VECTOR_STATUS_INDEXED
            assert store.get_vector_id(cid) == str(cid)  # Milvus 主键 = chunk_id
        # 先入库后回写：store.transitions 中 indexed 出现前已有 upsert 记录
        assert [t for t in store.transitions if t[0] == 1] == [
            (1, VECTOR_STATUS_PENDING),
            (1, VECTOR_STATUS_INDEXED),
        ]
        # 入库桩收到 1024 维稠密向量
        assert all(row[1] == DENSE_DIM for row in sink.upserts)
        assert sorted(row[0] for row in sink.upserts) == [1, 2, 3]

    def test_enqueue_sets_pending(self) -> None:
        """入队即置 pending（FR-KB-012 分块完成后的初始态）。"""
        orch, store, _ = _make_orch()
        assert orch.enqueue(7, "新知识块") is True
        assert store.get_vector_status(7) == VECTOR_STATUS_PENDING
        assert orch.pending_count == 1

    def test_enqueue_input_validation(self) -> None:
        """入参类型校验：chunk_id 非 int（含 bool）、text 非 str 均拒绝。"""
        orch, _, _ = _make_orch()
        for bad_id in ("1", 1.5, True):  # type: ignore[arg-type]
            with pytest.raises(TypeError):
                orch.enqueue(bad_id, "文本")
        with pytest.raises(TypeError):
            orch.enqueue(1, 123)  # type: ignore[arg-type]


# ─────────────────────────── 幂等与跳过 ───────────────────────────

class TestIdempotency:
    """已 indexed/removed 的任务重投：直接跳过、零 embed 调用。"""

    def test_enqueue_skips_indexed_and_removed(self) -> None:
        """入队侧幂等：indexed/removed 返回 False，不置 pending。"""
        orch, store, _ = _make_orch()
        store.set_vector_status(1, VECTOR_STATUS_INDEXED, vector_id="1")
        store.set_vector_status(2, VECTOR_STATUS_REMOVED)
        assert orch.enqueue(1, "已入库") is False
        assert orch.enqueue(2, "已下线") is False
        assert orch.enqueue(3, "正常") is True
        assert orch.pending_count == 1
        assert store.get_vector_status(1) == VECTOR_STATUS_INDEXED  # 未被覆盖
        assert store.get_vector_status(2) == VECTOR_STATUS_REMOVED

    def test_rerun_after_indexed_zero_embed_calls(self) -> None:
        """消费侧幂等：入库成功后重复投递同任务，直接 ACK、零 embed 调用。"""
        client = FakeEmbeddingClient()
        orch, store, _ = _make_orch(client=client)
        orch.enqueue(1, "知识块")
        assert orch.process_queue().indexed == 1
        assert client.call_count == 1

        orch.enqueue(1, "知识块（重投）")  # 已 indexed：入队即跳过
        report = orch.process_queue()
        assert report.indexed == 0 and report.skipped == 0
        assert client.call_count == 1  # 零新增 embed 调用

    def test_status_flipped_after_enqueue_skipped(self) -> None:
        """入队后、消费前状态被外部置 indexed：该任务跳过，其余正常处理。"""
        client = FakeEmbeddingClient()
        orch, store, _ = _make_orch(client=client)
        orch.enqueue_many([(1, "甲"), (2, "乙")])
        store.set_vector_status(1, VECTOR_STATUS_INDEXED, vector_id="1")  # 外部已完成
        report = orch.process_queue()
        assert report.skipped == 1
        assert report.indexed == 1
        assert client.calls == [["乙"]]  # 只嵌入了任务 2
        assert store.get_vector_status(2) == VECTOR_STATUS_INDEXED


# ─────────────────────────── 分批对齐 ───────────────────────────

class TestBatching:
    """任务按 batch=32 分批调用客户端。"""

    def test_70_tasks_three_calls(self) -> None:
        """70 任务 -> 3 次 embed_batch 调用（32/32/6），全部入库。"""
        client = FakeEmbeddingClient()
        orch, store, _ = _make_orch(client=client)
        orch.enqueue_many([(i, f"知识块 {i}") for i in range(70)])
        report = orch.process_queue()
        assert [len(c) for c in client.calls] == [32, 32, 6]
        assert report.indexed == 70
        assert all(
            store.get_vector_status(i) == VECTOR_STATUS_INDEXED for i in range(70)
        )

    def test_custom_batch_size(self) -> None:
        """批大小可配：batch_size=10 时 25 任务 -> 3 次调用（10/10/5）。"""
        client = FakeEmbeddingClient()
        orch, _, _ = _make_orch(client=client, batch_size=10)
        orch.enqueue_many([(i, f"知识块 {i}") for i in range(25)])
        orch.process_queue()
        assert [len(c) for c in client.calls] == [10, 10, 5]

    def test_retried_task_goes_to_later_batch(self) -> None:
        """失败任务回到队尾：同一次 process_queue 内由后续批次重试。"""
        client = FakeEmbeddingClient()
        sink = FlakySink(fail_schedule={2: 1})  # 任务 2 首次入库失败
        orch, store, _ = _make_orch(client=client, sink=sink)
        orch.enqueue_many([(1, "甲"), (2, "乙")])
        report = orch.process_queue()
        assert [len(c) for c in client.calls] == [2, 1]  # 第二批只含重推的任务 2
        assert report.indexed == 2 and report.failed == 1
        assert store.get_vector_status(2) == VECTOR_STATUS_INDEXED  # 重试后成功


# ─────────────────────────── 失败重试与 DLQ ───────────────────────────

class TestFailureAndDlq:
    """单块失败：转 failed -> 自动重推 N 次 -> 耗尽进 DLQ + 告警。"""

    def test_transient_failure_recovered(self) -> None:
        """瞬时失败（1 次）自动重推成功：最终 indexed，不进 DLQ。"""
        alerts: list[tuple[str, dict[str, Any]]] = []
        sink = FlakySink(fail_schedule={5: 1})
        orch, store, _ = _make_orch(sink=sink, alerts=alerts)
        orch.enqueue_many([(i, f"知识块 {i}") for i in (1, 5)])
        report = orch.process_queue()

        assert report.indexed == 2
        assert report.failed == 1 and report.dlq == 0
        assert len(orch.dlq) == 0
        assert store.get_vector_status(5) == VECTOR_STATUS_INDEXED
        # 失败重推时发出 EMB_FAILED 告警（携带次数与错误）
        failed_events = [p for name, p in alerts if name == EMBED_EVENT_FAILED]
        assert failed_events == [
            {"chunk_id": 5, "attempts": 1, "error": "Milvus upsert 模拟失败：chunk 5"}
        ]

    def test_retry_exhausted_to_dlq(self) -> None:
        """持续失败（max_retries=2）：1+2 次后进 DLQ，EMB_DLQ 告警，状态 failed。"""
        alerts: list[tuple[str, dict[str, Any]]] = []
        sink = FlakySink(fail_schedule={9: 99})  # 永远失败
        orch, store, _ = _make_orch(sink=sink, max_retries=2, alerts=alerts)
        orch.enqueue_many([(8, "正常块"), (9, "坏块")])
        report = orch.process_queue()

        assert store.get_vector_status(8) == VECTOR_STATUS_INDEXED  # 坏块不阻塞好块
        assert store.get_vector_status(9) == VECTOR_STATUS_FAILED
        assert report.dlq == 1 and report.failed == 3  # 1 次原生 + 2 次重推
        assert len(orch.dlq) == 1
        entry = orch.dlq.entries[0]
        assert entry.chunk_id == 9
        assert entry.attempts == 3
        assert "chunk 9" in entry.error
        assert entry.ts  # UTC ISO 留痕
        dlq_events = [p for name, p in alerts if name == EMBED_EVENT_DLQ]
        assert dlq_events == [
            {"chunk_id": 9, "attempts": 3, "error": "Milvus upsert 模拟失败：chunk 9"}
        ]
        assert orch.pending_count == 0  # 已终态入 DLQ，队列排空

    def test_dlq_manual_disposal_via_requeue(self) -> None:
        """DLQ 人工处置：取出死信 -> requeue 置回 pending -> 恢复后成功入库。"""
        sink = FlakySink(fail_schedule={9: 3})  # 前 3 次失败（恰好耗尽 max_retries=2）
        orch, store, _ = _make_orch(sink=sink, max_retries=2)
        orch.enqueue(9, "待重推块")
        first = orch.process_queue()
        assert first.dlq == 1 and store.get_vector_status(9) == VECTOR_STATUS_FAILED

        entry = orch.dlq.pop()  # 人工取出死信
        assert entry.chunk_id == 9
        assert orch.requeue(9, "待重推块（重推）") is True
        assert store.get_vector_status(9) == VECTOR_STATUS_PENDING
        second = orch.process_queue()  # 倒计时已耗尽：本次入库成功
        assert second.indexed == 1
        assert store.get_vector_status(9) == VECTOR_STATUS_INDEXED
        assert orch.requeue(9, "再推") is False  # 已 indexed：拒绝重复重推

    def test_external_dead_letter_queue_injected(self) -> None:
        """DLQ 可注入外部实例（生产对接 Redis kb:embed:dlq）。"""
        dlq = DeadLetterQueue()
        sink = FlakySink(fail_schedule={3: 99})
        client = FakeEmbeddingClient()
        store = InMemoryChunkStore()
        orch = EmbedOrchestrator(client, store, sink, max_retries=0, dlq=dlq)
        orch.enqueue(3, "直接进死信")
        report = orch.process_queue()
        assert report.dlq == 1
        assert dlq is orch.dlq and len(dlq) == 1  # 注入实例被原样使用


# ─────────────────────────── TEI 不可用（降级场景）───────────────────────────

class TestServiceUnavailable:
    """EmbeddingError：任务滞留队列零丢失，恢复后续跑（SR-KB-013 §6.3）。"""

    def test_tasks_retained_then_drained(self) -> None:
        """不可用期间任务留队且状态保持 pending；恢复后一次排空全部 indexed。"""
        alerts: list[tuple[str, dict[str, Any]]] = []
        client = SwitchableClient()
        orch, store, _ = _make_orch(client=client, alerts=alerts)
        orch.enqueue_many([(i, f"知识块 {i}") for i in (1, 2, 3)])

        client.error = "TEI 模拟不可用（超时重试耗尽）"
        report = orch.process_queue()
        assert report.service_unavailable is True
        assert report.indexed == 0 and report.failed == 0  # 不转 failed、不丢任务
        assert orch.pending_count == 3
        assert all(
            store.get_vector_status(i) == VECTOR_STATUS_PENDING for i in (1, 2, 3)
        )
        down_events = [p for name, p in alerts if name == EMBED_EVENT_DOWN]
        assert len(down_events) == 1 and "TEI 模拟不可用" in down_events[0]["error"]
        assert down_events[0]["pending"] == 3

        client.error = None  # TEI 恢复
        recovered = orch.process_queue()
        assert recovered.service_unavailable is False
        assert recovered.indexed == 3
        assert orch.pending_count == 0
        assert all(
            store.get_vector_status(i) == VECTOR_STATUS_INDEXED for i in (1, 2, 3)
        )

    def test_result_count_mismatch_treated_as_unavailable(self) -> None:
        """客户端返回条数与任务数不一致：同样按服务不可用留队（防错位入库）。"""

        class MismatchClient:
            def embed_batch(self, texts: list[str]) -> Any:
                result = FakeEmbeddingClient().embed_batch(texts[:-1])  # 少返回一条
                return result

        orch, store, _ = _make_orch(client=MismatchClient())
        orch.enqueue_many([(1, "甲"), (2, "乙")])
        report = orch.process_queue()
        assert report.service_unavailable is True
        assert orch.pending_count == 2  # 全部留队，未错位入库
        assert store.get_vector_status(1) == VECTOR_STATUS_PENDING


# ─────────────────────────── 配置校验 ───────────────────────────

class TestConfigValidation:
    """编排器构造参数校验。"""

    def test_invalid_batch_size(self) -> None:
        """batch_size < 1：构造即拒绝。"""
        with pytest.raises(ValueError):
            _make_orch(batch_size=0)

    def test_invalid_max_retries(self) -> None:
        """max_retries < 0：构造即拒绝。"""
        with pytest.raises(ValueError):
            _make_orch(max_retries=-1)
