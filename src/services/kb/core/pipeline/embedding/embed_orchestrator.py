# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""批量向量化任务编排（FR-KB-013 §6.3）：入队 -> 分批（32）-> 调用 -> 状态机 -> DLQ。

vector_status 状态机（与 03-数据库设计 §3.1 CHECK 枚举一致，仅 4 态）：

    pending --embed + 向量入库成功--> indexed
    pending --单块失败且重试耗尽----> failed --(人工/定时重推 requeue)--> pending
    任一态  --条目删除/下线---------> removed（跳过，不向量化）

失败处理两级策略（SR-KB-013 §6.3）：
- EmbeddingError（TEI 整体不可用）：本批任务回队滞留、状态不转 failed，
  恢复后续跑（零丢失）；同时触发 EMB_DOWN 告警（查询侧降级 BM25）；
- 单块失败（向量入库异常等）：状态转 failed 并自动重推 max_retries 次，
  耗尽后进死信队列 DLQ 并触发 EMB_DLQ 告警（人工处置）。

持久化依赖经端口注入（ChunkStore / VectorSink），生产实现分别对接
PostgreSQL 状态回写与 Milvus upsert（FR-KB-014/017），任务队列生产对应
Redis Stream（kb:embed:stream）；本模块内置内存实现，供单元测试与联调。

实现方案：docs/07-实现方案/KB/SR-KB-013-向量化服务.md §6.3、§7、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol

from .base import (
    DEFAULT_BATCH_SIZE,
    EmbeddingClient,
    EmbeddingError,
    EmbeddingResult,
)

# ── vector_status 状态枚举（03-数据库设计 §3.1 CHECK）───────────────────────
VECTOR_STATUS_PENDING = "pending"    # 待向量化（FR-KB-012 分块落库后的初始态）
VECTOR_STATUS_INDEXED = "indexed"    # 已向量化且向量可查（Milvus upsert 成功）
VECTOR_STATUS_FAILED = "failed"      # 向量化失败（等待重推或人工处置）
VECTOR_STATUS_REMOVED = "removed"    # 条目删除/下线（终态，跳过）

#: 单块失败自动重推上限（SR-KB-013 §7 KB_EMBED_MAX_AUTO_RETRIES 默认 3）
DEFAULT_MAX_AUTO_RETRIES = 3

# ── 告警钩子事件名（对接 FR-AG-007 告警通道的约定枚举）───────────────────────
EMBED_EVENT_DOWN = "EMB_DOWN"      # TEI 整体不可用：查询侧降级 BM25（warning）
EMBED_EVENT_FAILED = "EMB_FAILED"  # 单块失败：已自动重推（info）
EMBED_EVENT_DLQ = "EMB_DLQ"        # 重试耗尽进死信：需人工处置（critical）

#: 告警钩子类型：(事件名, 上下文载荷) -> None；缺省为静默 no-op
AlertHook = Callable[[str, dict[str, Any]], None]


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（毫秒精度），供 DLQ 留痕。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class EmbedTask:
    """队列任务：一次待向量化的知识块（chunk_id + 清洗后文本）。

    attempts 为累计失败次数（自动重推计数，超过 max_retries 进 DLQ）。
    """

    chunk_id: int
    text: str
    attempts: int = 0


@dataclass
class DlqEntry:
    """死信条目：重试耗尽任务的终态留痕（生产对应 Redis kb:embed:dlq）。"""

    chunk_id: int
    error: str      # 最后一次失败的错误描述
    attempts: int   # 累计失败次数
    ts: str         # 入队 UTC ISO 时间戳


class DeadLetterQueue:
    """向量化死信队列：超出自动重试上限的任务终态，供人工处置与统计。"""

    def __init__(self) -> None:
        self.entries: list[DlqEntry] = []

    def push(self, entry: DlqEntry) -> None:
        """追加一条死信。"""
        self.entries.append(entry)

    def pop(self, index: int = 0) -> DlqEntry:
        """取出一条死信（人工处置入口，配合 EmbedOrchestrator.requeue 使用）。"""
        return self.entries.pop(index)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterable[DlqEntry]:  # type: ignore[override]
        return iter(self.entries)


class ChunkStore(Protocol):
    """知识块状态仓端口：生产对接 PostgreSQL knowledge_chunk 回写（FR-KB-014）。"""

    def get_vector_status(self, chunk_id: int) -> str | None:
        """读取 vector_status；chunk 不存在返回 None。"""
        ...

    def set_vector_status(
        self, chunk_id: int, status: str, *, vector_id: str | None = None
    ) -> None:
        """回写 vector_status；成功时附带 vector_id（= str(chunk_id)）。"""
        ...


class VectorSink(Protocol):
    """向量入库端口：生产对接 Milvus upsert by chunk_id（FR-KB-014/017）。"""

    def upsert(self, chunk_id: int, dense: list[float], sparse: dict[int, float]) -> None:
        """写入/覆盖单条向量（幂等，重复写无副作用）。"""
        ...


class InMemoryChunkStore:
    """内存版状态仓：单元测试与联调默认实现（状态迁移留痕供断言）。"""

    def __init__(self) -> None:
        self._status: dict[int, str] = {}
        self._vector_id: dict[int, str] = {}
        self.transitions: list[tuple[int, str]] = []  # (chunk_id, status) 迁移留痕

    def get_vector_status(self, chunk_id: int) -> str | None:
        return self._status.get(chunk_id)

    def set_vector_status(
        self, chunk_id: int, status: str, *, vector_id: str | None = None
    ) -> None:
        self._status[chunk_id] = status
        if vector_id is not None:
            self._vector_id[chunk_id] = vector_id
        self.transitions.append((chunk_id, status))

    def get_vector_id(self, chunk_id: int) -> str | None:
        """读取成功入库后的向量主键（= str(chunk_id)）。"""
        return self._vector_id.get(chunk_id)


@dataclass
class EmbedRunReport:
    """一次 process_queue 的运行账目（stats 端点数据来源，SR-KB-013 §5）。

    failed 计失败「次数」（同块多次重推多次计数），dlq 计最终进死信的块数。
    """

    indexed: int = 0                # 成功置 indexed 的次数
    failed: int = 0                  # 单块失败次数（含重推后再失败）
    skipped: int = 0                 # 幂等跳过（已 indexed/removed/不存在）
    dlq: int = 0                     # 进死信的块数
    service_unavailable: bool = False  # TEI 不可用：剩余任务滞留队列（未完成）

    @property
    def processed(self) -> int:
        """本次处理过的任务次数合计（不含滞留队列的部分）。"""
        return self.indexed + self.failed + self.skipped


class EmbedOrchestrator:
    """批量向量化编排器：驱动 pending -> indexed/failed 状态机闭环。

    用法（典型流水线片段）::

        orch = EmbedOrchestrator(client, store, sink, alert=dingtalk_alert)
        orch.enqueue_many([(chunk.id, chunk.content) for chunk in chunks])
        report = orch.process_queue()

    线程安全性：单线程消费模型（生产为 Stream 消费组 worker），不做内部加锁。
    """

    def __init__(
        self,
        client: EmbeddingClient,
        store: ChunkStore,
        sink: VectorSink,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_retries: int = DEFAULT_MAX_AUTO_RETRIES,
        alert: AlertHook | None = None,
        dlq: DeadLetterQueue | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size 必须 >= 1，当前 {batch_size}")
        if max_retries < 0:
            raise ValueError(f"max_retries 必须 >= 0，当前 {max_retries}")
        self._client = client
        self._store = store
        self._sink = sink
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._alert: AlertHook = alert or (lambda event, payload: None)  # 缺省静默
        # 注意：DeadLetterQueue 定义了 __len__（空队列 falsy），必须用 is None 判缺省
        self.dlq = dlq if dlq is not None else DeadLetterQueue()
        self._queue: deque[EmbedTask] = deque()

    # ── 入队（producer 侧）──────────────────────────────────────────────────

    @property
    def pending_count(self) -> int:
        """当前滞留队列的任务数（队列积压监控，stats 端点口径）。"""
        return len(self._queue)

    def enqueue(self, chunk_id: int, text: str) -> bool:
        """任务入队：置 pending 并排队；indexed/removed 幂等跳过（返回 False）。"""
        _validate_task(chunk_id, text)
        status = self._store.get_vector_status(chunk_id)
        if status in (VECTOR_STATUS_INDEXED, VECTOR_STATUS_REMOVED):
            return False  # 已入库/已下线：重复入队直接跳过（幂等）
        self._store.set_vector_status(chunk_id, VECTOR_STATUS_PENDING)
        self._queue.append(EmbedTask(chunk_id=chunk_id, text=text))
        return True

    def enqueue_many(self, items: Iterable[tuple[int, str]]) -> int:
        """批量入队（FR-KB-012 分块完成后的 producer 入口），返回实际入队条数。"""
        count = 0
        for chunk_id, text in items:
            if self.enqueue(chunk_id, text):
                count += 1
        return count

    def requeue(self, chunk_id: int, text: str) -> bool:
        """failed 重推（人工/定时，SR-KB-013 §5 retry 端点内核）：置回 pending 入队。"""
        _validate_task(chunk_id, text)
        status = self._store.get_vector_status(chunk_id)
        if status in (VECTOR_STATUS_INDEXED, VECTOR_STATUS_REMOVED):
            return False
        self._store.set_vector_status(chunk_id, VECTOR_STATUS_PENDING)
        self._queue.append(EmbedTask(chunk_id=chunk_id, text=text))
        return True

    # ── 消费（worker 侧）────────────────────────────────────────────────────

    def process_queue(self) -> EmbedRunReport:
        """排空队列：按 batch 分批调用客户端，驱动状态机直至队列清空或服务不可用。"""
        report = EmbedRunReport()
        while self._queue:
            batch = [self._queue.popleft() for _ in range(min(self._batch_size, len(self._queue)))]
            # 幂等过滤：已 indexed/removed/已删除的任务直接跳过（零 embed 调用）
            live = [t for t in batch if self._needs_embedding(t.chunk_id)]
            report.skipped += len(batch) - len(live)
            if not live:
                continue
            try:
                result = self._client.embed_batch([t.text for t in live])
                if len(result) != len(live):
                    raise EmbeddingError(
                        f"向量化结果条数（{len(result)}）与任务数（{len(live)}）不一致"
                    )
            except EmbeddingError as exc:
                # TEI 整体不可用：本批任务原序回队滞留（不 ACK、不转 failed），恢复后续跑
                self._queue.extendleft(reversed(live))
                report.service_unavailable = True
                self._alert(
                    EMBED_EVENT_DOWN,
                    {"error": exc.message, "pending": len(self._queue)},
                )
                break
            for task, dense, sparse in zip(live, result.dense, result.sparse):
                self._process_task(task, dense, sparse, report)
        return report

    # ── 内部实现 ────────────────────────────────────────────────────────────

    def _needs_embedding(self, chunk_id: int) -> bool:
        """任务是否仍需向量化：indexed/removed/不存在（已删除）均视为无需处理。"""
        status = self._store.get_vector_status(chunk_id)
        return status not in (None, VECTOR_STATUS_INDEXED, VECTOR_STATUS_REMOVED)

    def _process_task(
        self,
        task: EmbedTask,
        dense: list[float],
        sparse: dict[int, float],
        report: EmbedRunReport,
    ) -> None:
        """单任务落库：先向量入库、后回写状态，保证 indexed <=> 向量可查。"""
        try:
            self._sink.upsert(task.chunk_id, dense, sparse)
        except Exception as exc:  # noqa: BLE001 —— 单块失败不阻塞队列：转 failed 重推
            self._handle_failure(task, exc, report)
            return
        self._store.set_vector_status(
            task.chunk_id, VECTOR_STATUS_INDEXED, vector_id=str(task.chunk_id)
        )
        report.indexed += 1

    def _handle_failure(self, task: EmbedTask, exc: Exception, report: EmbedRunReport) -> None:
        """单块失败处理：转 failed -> 自动重推 / 重试耗尽进 DLQ 并告警。"""
        self._store.set_vector_status(task.chunk_id, VECTOR_STATUS_FAILED)
        task.attempts += 1
        report.failed += 1
        if task.attempts > self._max_retries:
            # 自动重推耗尽：进死信 + 告警，等待人工处置（requeue）
            self.dlq.push(
                DlqEntry(
                    chunk_id=task.chunk_id,
                    error=str(exc),
                    attempts=task.attempts,
                    ts=_now_iso(),
                )
            )
            report.dlq += 1
            self._alert(
                EMBED_EVENT_DLQ,
                {"chunk_id": task.chunk_id, "attempts": task.attempts, "error": str(exc)},
            )
        else:
            self._queue.append(task)  # 自动重推：回到队尾由后续批次重试
            self._alert(
                EMBED_EVENT_FAILED,
                {"chunk_id": task.chunk_id, "attempts": task.attempts, "error": str(exc)},
            )


def _validate_task(chunk_id: int, text: str) -> None:
    """入参校验：chunk_id 必须为 int（排除 bool），text 必须为 str。"""
    if isinstance(chunk_id, bool) or not isinstance(chunk_id, int):
        raise TypeError(f"chunk_id 必须为 int，当前 {type(chunk_id).__name__}")
    if not isinstance(text, str):
        raise TypeError(f"text 必须为 str，当前 {type(text).__name__}")
