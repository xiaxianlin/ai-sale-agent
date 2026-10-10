# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""增量同步编排引擎（FR-KB-017 增量更新，Phase 0 / 迭代 0.2）。

IncrementalSyncEngine 执行事件驱动的增量索引闭环（SR-KB-017 §6.2 状态机
的同步内核形态）：

    知识条目事件(item_id + 新正文 + kb_type + 维度字段 + action)
      → ① 重分块（复用 FR-KB-012 SemanticChunker）
      → ② diff（diff.py 三分类：unchanged / to_upsert / to_delete）
      → ③ 块仓落库（PG 权威先行：新块 pending、失效块 removed）
      → ④ 变更块嵌入（FR-KB-013 EmbeddingClient，仅增量块，未变块零成本复用）
      → ⑤ 向量库写入（FR-KB-014 VectorStore：upsert by chunk_id + 失效 delete）
      → ⑥ vector_status 迁移（pending → indexed / failed）
      → ⑦ 检索可见性切换（draft 预索引不可见；publish 翻转；
          archived / 软删除 → 向量彻底清除）

分钟级时效设计（SR-KB-017 §6.1 时限预算表）：SyncTimeBudget 按
「分块+diff ≤10s / 嵌入 ≤30s / 入库 ≤15s / 可见性 ≤5s / 合计 ≤60s」
配置各段毫秒预算；任一段超限即**降级**——当次不再阻塞同步链路，
登记 CompensationEntry 交异步补偿（未完成块保持 pending，由下一次
事件重放或 reconcile 对账自愈，NFR-REL-01 断点重入）。

失败补偿（SR §6.5）：嵌入失败（EmbeddingError）→ 受影响块 vector_status
置 failed → 指数退避重试（base**attempt，sleep 可注入）→ 重试耗尽按块
进死信队列（复用 FR-KB-013 DeadLetterQueue / DlqEntry）并告警；failed
块在下一次同条目事件中被视为待自愈块自动重嵌（failed → pending → indexed）。

依赖注入（Protocol 鸭子类型，全部只读引用既有冻结接口）：
    chunker      Callable[[str], list[Chunk]]（make_text_chunk_fn 提供默认实现）
    chunk_store  KnowledgeChunkStore（PG knowledge_chunk 的端口，附内存实现）
    vector_store VectorStore（FR-KB-014 协议；可选实现 ItemStatusFlipper
                 以获得「发布仅翻标量」的秒级路径，未实现时降级为重嵌入重写）
    embedder     EmbeddingClient（FR-KB-013 协议）

依据文档：docs/07-实现方案/KB/SR-KB-017-增量更新.md §6.1、§6.2、§6.3、§6.5、§7
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from ..chunking.chunker import Chunk, ChunkConfig, SemanticChunker
from ..embedding.base import EmbeddingClient, EmbeddingError
from ..embedding.embed_orchestrator import (
    VECTOR_STATUS_FAILED,
    VECTOR_STATUS_INDEXED,
    VECTOR_STATUS_PENDING,
    VECTOR_STATUS_REMOVED,
    DeadLetterQueue,
    DlqEntry,
)
from ...vectorstore.base import (
    FILTER_VALUE_MAX_LENGTH,
    ITEM_STATUS_PUBLISHED,
    KB_TYPES,
    VectorRecord,
    VectorStore,
)
from .diff import IncrementalPlan, IndexedChunk, diff_chunks

# ── 事件动作枚举（SR-KB-017 §4 kb_index_job.action + 归档细化）───────────────
ACTION_SAVE = "save"                # 草稿保存：预索引 item_status=draft（检索不可见）
ACTION_PUBLISH = "publish"          # 发布：item_status=published（进入检索）
ACTION_ARCHIVE = "archive"          # 归档：退出检索（向量侧等同软删清理）
ACTION_SOFT_DELETE = "soft_delete"  # 软删除：块仓行保留（审计），向量彻底清除

#: 动作 → 向量库 item_status 目标值（archived/soft_delete 走删除路径，无目标态）
_ACTION_ITEM_STATUS: dict[str, str] = {
    ACTION_SAVE: "draft",
    ACTION_PUBLISH: ITEM_STATUS_PUBLISHED,
}

#: 告警钩子事件名（对接告警通道的约定枚举，与 FR-KB-013 EMBED_EVENT_* 同风格）
SYNC_EVENT_DLQ = "SYNC_DLQ"        # 嵌入重试耗尽进死信：需人工处置（critical）
SYNC_EVENT_DEGRADE = "SYNC_DEGRADE"  # 阶段超预算降级异步补偿（warning）

#: 告警钩子类型：(事件名, 上下文载荷) -> None；缺省静默 no-op
SyncAlertHook = Callable[[str, dict[str, Any]], None]

#: 重分块函数类型：新正文 -> 重分块结果（engine 的可插拔分块入口）
ChunkFn = Callable[[str], list[Chunk]]


def now_iso() -> str:
    """UTC ISO-8601 时间戳（毫秒精度），供补偿登记 / 死信留痕。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ── 时限预算表（SR-KB-017 §6.1：分钟级生效链路，P0 单机口径）─────────────────


@dataclass(frozen=True)
class SyncTimeBudget:
    """全链路时限预算（毫秒）：分阶段预算 + 总预算，超时降级异步补偿。

    默认值取 SR §6.1 预算表的保守整秒换算；job 级告警线（300s）对应
    kb.incr.job_timeout_seconds（SR §7），此处以 total_ms 表达 60s 合计线。
    """

    chunk_ms: float = 10_000.0    # 重分块 + diff + 块仓落库 ≤10s
    embed_ms: float = 30_000.0    # 增量向量化（batch=32，内网 TEI）≤30s
    index_ms: float = 15_000.0    # Milvus upsert + 失效 delete ≤15s
    publish_ms: float = 5_000.0   # 可见性切换 ≤5s
    total_ms: float = 60_000.0    # 合计 ≤60s（分钟级生效验收口径）


@dataclass(frozen=True)
class CompensationEntry:
    """异步补偿登记：阶段超预算后被降级的中断现场（引擎内存态，迭代 0.2）。

    生产形态对应 kb_index_job 滞留（state 未到 done）+ 消费者重投；
    本引擎以 drain_compensations() 暴露给补偿循环 / 对账任务消费。
    """

    item_id: int
    action: str
    reason: str    # 降级原因（如 chunk_overrun(52ms>10ms)）
    ts: str        # 登记时间（UTC ISO）


# ── 事件契约（任务口径：item_id + 新正文 + kb_type + 维度字段）───────────────


@dataclass(frozen=True)
class ItemVectorMeta:
    """向量维度元数据：kb_type + 五个高频过滤维度（03-数据库设计 §4.2 标量列）。"""

    kb_type: str
    sku: str = ""
    scene_tag: str = ""
    process_node: str = ""
    reason_type: str = ""
    customer_ref: str = ""


@dataclass(frozen=True)
class KnowledgeItemEvent:
    """知识条目修改事件（FR-KB-017 增量更新入口）。

    action 语义见模块头部枚举；维度字段随事件随行物化到块仓
    （块行携带 kb_type / 维度，供嵌入入库与对账修复时重建 VectorRecord）。
    """

    item_id: int
    body: str                 # 新正文（重分块输入；清空 = 全部块失效）
    kb_type: str
    action: str = ACTION_SAVE
    sku: str = ""
    scene_tag: str = ""
    process_node: str = ""
    reason_type: str = ""
    customer_ref: str = ""

    @property
    def meta(self) -> ItemVectorMeta:
        """事件的向量维度元数据投影。"""
        return ItemVectorMeta(
            kb_type=self.kb_type,
            sku=self.sku,
            scene_tag=self.scene_tag,
            process_node=self.process_node,
            reason_type=self.reason_type,
            customer_ref=self.customer_ref,
        )


@dataclass
class StoredChunk:
    """块仓行形态：chunk 身份 + 自愈重嵌所需元数据（维度随行物化）。"""

    chunk_id: int
    item_id: int
    chunk_index: int
    content: str
    heading_path: str
    content_hash: str
    vector_status: str                 # pending | indexed | failed | removed
    indexed_status: str = ""           # 最近一次写入向量库的 item_status（''=从未写入）
    kb_type: str = "product"
    sku: str = ""
    scene_tag: str = ""
    process_node: str = ""
    reason_type: str = ""
    customer_ref: str = ""

    @property
    def meta(self) -> ItemVectorMeta:
        """行内维度元数据投影（对账修复重建 VectorRecord 用）。"""
        return ItemVectorMeta(
            kb_type=self.kb_type,
            sku=self.sku,
            scene_tag=self.scene_tag,
            process_node=self.process_node,
            reason_type=self.reason_type,
            customer_ref=self.customer_ref,
        )


@dataclass
class SyncReport:
    """一次增量同步的运行账目（kb_index_job.stat 的内存形态 + 时效观测）。"""

    item_id: int
    action: str
    reused: int = 0                     # 零成本复用块数（hash 命中且向量已就位）
    embedded: int = 0                   # 成功嵌入并入库块数（含自愈重嵌）
    upserted: int = 0                   # 写入向量库记录数（含可见性降级重写）
    deleted_vectors: int = 0            # 从向量库删除的失效块数
    flipped: int = 0                    # 经翻标量完成可见性切换的块数（零重嵌）
    reembedded_for_visibility: int = 0  # 协议受限降级：为切可见性而重嵌的块数
    failed_chunk_ids: list[int] = field(default_factory=list)   # 终态 failed 的块
    dlq_chunk_ids: list[int] = field(default_factory=list)      # 进死信的块
    stage_elapsed_ms: dict[str, float] = field(default_factory=dict)  # 各段实际耗时
    elapsed_ms: float = 0.0             # 全链路耗时（分钟级时效观测）
    degraded: list[str] = field(default_factory=list)           # 降级原因清单

    @property
    def ok(self) -> bool:
        """本次同步是否完整成功：无降级、无 failed / 死信。"""
        return not self.degraded and not self.failed_chunk_ids and not self.dlq_chunk_ids


# ── 块仓端口（生产对接 PG knowledge_chunk；本文件附内存实现供测试/联调）──────


class KnowledgeChunkStore(Protocol):
    """知识块持久化端口：engine（增量写）与 reconcile（对账读）共同依赖。

    语义约定：
    - alive_chunks 仅返回 vector_status != removed 的行（removed 行保留供审计）；
    - insert_chunk 落库即 vector_status=pending（FR-KB-012 分块落库初始态）；
    - set_vector_status 幂等可重放（断点重入的状态机持久化基础）。
    """

    def alive_chunks(self, item_id: int) -> list[IndexedChunk]:
        """读取条目全部存活块（pending / indexed / failed），按 chunk_index 升序。"""
        ...

    def insert_chunk(
        self,
        item_id: int,
        chunk_index: int,
        content: str,
        heading_path: str,
        content_hash: str,
        meta: ItemVectorMeta,
    ) -> int:
        """落库新块（vector_status=pending，维度随行物化），返回分配的 chunk_id。"""
        ...

    def move_chunk_index(self, chunk_id: int, chunk_index: int) -> None:
        """校正不变块的顺序位移（头部插入段落场景，仅更新 chunk_index）。"""
        ...

    def mark_removed(self, chunk_ids: list[int]) -> None:
        """批量标记失效（vector_status → removed，PG 权威先行，向量随后删）。"""
        ...

    def set_vector_status(self, chunk_id: int, status: str, *, vector_id: str | None = None) -> None:
        """回写 vector_status；成功入库时附带 vector_id（= str(chunk_id)）。"""
        ...

    def set_indexed_status(self, chunk_id: int, item_status: str) -> None:
        """回写最近一次写入向量库的 item_status（'' 表示从未写入 / 已清除）。"""
        ...

    def get_chunk(self, chunk_id: int) -> StoredChunk | None:
        """读取单块行（不存在返回 None）；返回副本，调用方修改不影响仓内状态。"""
        ...

    def indexed_chunks(self) -> list[StoredChunk]:
        """读取全库应索引集（vector_status == indexed），供对账（reconcile）。"""
        ...


class InMemoryKnowledgeChunkStore:
    """KnowledgeChunkStore 内存实现：单元测试 / 联调默认替身（生产对接 PG）。

    自增主键从 1 起（对齐 PG IDENTITY）；removed 行保留在仓内
    （审计 / 回滚依据，FR-KB-022），只是不再出现在 alive / indexed 集合中。
    """

    def __init__(self) -> None:
        self._rows: dict[int, StoredChunk] = {}
        self._next_id: int = 1

    def alive_chunks(self, item_id: int) -> list[IndexedChunk]:
        rows = [
            row for row in self._rows.values()
            if row.item_id == item_id and row.vector_status != VECTOR_STATUS_REMOVED
        ]
        rows.sort(key=lambda row: row.chunk_index)
        return [
            IndexedChunk(
                chunk_id=row.chunk_id,
                content_hash=row.content_hash,
                chunk_index=row.chunk_index,
                vector_status=row.vector_status,
                indexed_status=row.indexed_status,
            )
            for row in rows
        ]

    def insert_chunk(
        self,
        item_id: int,
        chunk_index: int,
        content: str,
        heading_path: str,
        content_hash: str,
        meta: ItemVectorMeta,
    ) -> int:
        chunk_id = self._next_id
        self._next_id += 1
        self._rows[chunk_id] = StoredChunk(
            chunk_id=chunk_id,
            item_id=item_id,
            chunk_index=chunk_index,
            content=content,
            heading_path=heading_path,
            content_hash=content_hash,
            vector_status=VECTOR_STATUS_PENDING,
            indexed_status="",
            kb_type=meta.kb_type,
            sku=meta.sku,
            scene_tag=meta.scene_tag,
            process_node=meta.process_node,
            reason_type=meta.reason_type,
            customer_ref=meta.customer_ref,
        )
        return chunk_id

    def move_chunk_index(self, chunk_id: int, chunk_index: int) -> None:
        row = self._rows[chunk_id]
        row.chunk_index = chunk_index

    def mark_removed(self, chunk_ids: list[int]) -> None:
        for chunk_id in chunk_ids:
            self._rows[chunk_id].vector_status = VECTOR_STATUS_REMOVED

    def set_vector_status(self, chunk_id: int, status: str, *, vector_id: str | None = None) -> None:
        if chunk_id not in self._rows:
            raise KeyError(f"chunk {chunk_id} 不存在于块仓（状态回写拒绝幽灵块）")
        self._rows[chunk_id].vector_status = status

    def set_indexed_status(self, chunk_id: int, item_status: str) -> None:
        if chunk_id not in self._rows:
            raise KeyError(f"chunk {chunk_id} 不存在于块仓（可见性回写拒绝幽灵块）")
        self._rows[chunk_id].indexed_status = item_status

    def get_chunk(self, chunk_id: int) -> StoredChunk | None:
        row = self._rows.get(chunk_id)
        return replace(row) if row is not None else None

    def indexed_chunks(self) -> list[StoredChunk]:
        rows = [row for row in self._rows.values() if row.vector_status == VECTOR_STATUS_INDEXED]
        rows.sort(key=lambda row: row.chunk_id)
        return [replace(row) for row in rows]


# ── 可见性翻转让（可选能力：VectorStore 协议之外的增量扩展点）────────────────


@runtime_checkable
class ItemStatusFlipper(Protocol):
    """可见性翻转让（SR-KB-017 §6.2 visibility.py 的存储层落点）。

    VectorStore 冻结协议无「按主键读回整行」原语，翻标量（draft→published）
    需要存储侧支持；实现本协议的后端（生产 Milvus 封装 / 测试桩）可获得
    「发布仅翻 item_status、零重嵌入」的秒级路径，未实现时引擎降级为
    重嵌入重写（正确性优先，report 留痕）。
    """

    def flip_item_status(self, chunk_ids: list[int], item_status: str) -> None:
        """批量翻转记录 item_status（不存在的主键静默忽略，幂等）。"""
        ...


# ── 向量记录构造（engine 与 reconcile 共用的行投影）─────────────────────────


def build_vector_record(
    chunk_id: int,
    meta: ItemVectorMeta,
    dense: list[float],
    sparse: dict[int, float],
    item_status: str,
) -> VectorRecord:
    """组装向量入库行（字段与 03-数据库设计 §4.2 逐字段对齐）。

    构造即校验（VectorRecord 校验器）：kb_type 枚举、dense 1024 维、
    维度超长在此拦截——坏数据在入库前 fail-fast。
    """
    return VectorRecord(
        chunk_id=chunk_id,
        kb_type=meta.kb_type,
        dense=dense,
        sparse=sparse,
        sku=meta.sku,
        scene_tag=meta.scene_tag,
        process_node=meta.process_node,
        reason_type=meta.reason_type,
        customer_ref=meta.customer_ref,
        item_status=item_status,
    )


# ── 默认重分块适配（事件正文 -> SemanticChunker 输入）────────────────────────

_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


@dataclass
class _TextBlock:
    """事件正文的 block 投影（对齐 FR-KB-010 BlockLike 契约的最小形态）。"""

    type: str            # 'heading' | 'text'
    text: str
    heading_level: int = 1


@dataclass
class _SimpleDoc:
    """事件正文的文档投影（对齐 UnstructuredDocLike：blocks + source_file）。"""

    blocks: list[_TextBlock]
    source_file: str = ""
    title: str = ""


def split_body_blocks(body: str) -> list[_TextBlock]:
    """事件正文 -> block 序列：空行分段；单行 Markdown 标题（#..######）识别为标题块。

    生产链路的输入是 FR-KB-011 清洗产物（CleanedDoc），本适配仅服务于
    「事件携带纯正文」的迭代 0.2 口径；正文为空返回空列表（引擎按全部失效处理）。
    """
    blocks: list[_TextBlock] = []
    for paragraph in re.split(r"\n\s*\n", body.strip()):
        text = paragraph.strip()
        if not text:
            continue
        lines = text.splitlines()
        matched = _MARKDOWN_HEADING_RE.match(lines[0]) if len(lines) == 1 else None
        if matched is not None:
            blocks.append(_TextBlock(
                type="heading",
                text=matched.group(2).strip(),
                heading_level=len(matched.group(1)),
            ))
        else:
            blocks.append(_TextBlock(type="text", text=text))
    return blocks


def make_text_chunk_fn(config: ChunkConfig | None = None) -> ChunkFn:
    """构造默认重分块函数：复用 FR-KB-012 SemanticChunker（标题 > 表格 > 长度兜底）。

    分块确定性（同正文恒同块）是 content_hash diff 成立的前提；
    条目标题等 item 级元数据不进入块内容，改标题不触发重嵌。
    """
    chunker = SemanticChunker(config=config)

    def chunk_fn(body: str) -> list[Chunk]:
        blocks = split_body_blocks(body)
        if not blocks:
            return []
        chunks, _metrics = chunker.chunk(_SimpleDoc(blocks=blocks, source_file=""))
        return chunks

    return chunk_fn


# ── 增量同步引擎本体 ─────────────────────────────────────────────────────────


class IncrementalSyncEngine:
    """增量同步编排引擎：驱动「重分块 → diff → 嵌入 → 入库 → 可见性」闭环。

    幂等性（SR-KB-017 §6.5）：
    - 同一事件重放 → diff 全命中 unchanged 且向量已就位 → 零嵌入零写入；
    - upsert / delete 按 chunk_id 幂等；块仓状态迁移可重放；
    - 嵌入失败块保持 failed，下一次同条目事件自动自愈重嵌。

    线程模型：单线程事件消费（生产为 Stream 消费组单 worker），
    与 EmbedOrchestrator（FR-KB-013）一致，不做内部加锁。
    """

    def __init__(
        self,
        chunker: ChunkFn,
        chunk_store: KnowledgeChunkStore,
        vector_store: VectorStore,
        embedder: EmbeddingClient,
        *,
        time_budget: SyncTimeBudget | None = None,
        embed_batch_size: int = 32,
        max_retries: int = 3,
        retry_backoff_base_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        dlq: DeadLetterQueue | None = None,
        alert: SyncAlertHook | None = None,
    ) -> None:
        if embed_batch_size < 1:
            raise ValueError(f"embed_batch_size 必须 >= 1，当前 {embed_batch_size}")
        if max_retries < 0:
            raise ValueError(f"max_retries 必须 >= 0，当前 {max_retries}")
        if retry_backoff_base_seconds <= 0:
            raise ValueError(f"retry_backoff_base_seconds 必须为正数，当前 {retry_backoff_base_seconds}")
        self._chunker = chunker
        self._chunk_store = chunk_store
        self._vector_store = vector_store
        self._embedder = embedder
        self._budget = time_budget if time_budget is not None else SyncTimeBudget()
        self._embed_batch_size = embed_batch_size
        self._max_retries = max_retries
        self._backoff_base = retry_backoff_base_seconds
        self._clock = clock
        self._sleep = sleep
        # 注意：DeadLetterQueue 定义了 __len__（空队列 falsy），必须用 is None 判缺省
        self.dlq = dlq if dlq is not None else DeadLetterQueue()
        self._alert: SyncAlertHook = alert or (lambda event, payload: None)  # 缺省静默
        self._compensations: list[CompensationEntry] = []

    # ── 对外入口 ────────────────────────────────────────────────────────────

    @property
    def compensations(self) -> list[CompensationEntry]:
        """当前滞留的异步补偿登记（只读视图）。"""
        return list(self._compensations)

    def drain_compensations(self) -> list[CompensationEntry]:
        """取走全部补偿登记（补偿循环 / 对账任务消费入口）。"""
        entries, self._compensations = self._compensations, []
        return entries

    def handle_event(self, event: KnowledgeItemEvent) -> SyncReport:
        """处理知识条目事件：save / publish 走增量链路，archive / soft_delete 走清理链路。"""
        self._validate_event(event)
        report = SyncReport(item_id=event.item_id, action=event.action)
        started = self._clock()
        try:
            if event.action in (ACTION_ARCHIVE, ACTION_SOFT_DELETE):
                self._remove_item(event, report)
            else:
                self._sync_item(event, report)
        finally:
            report.elapsed_ms = (self._clock() - started) * 1000.0
            if report.elapsed_ms > self._budget.total_ms and not report.degraded:
                # 合计超线（60s 分钟级口径）：工作已完成的告警型降级，登记补偿供观测
                reason = f"total_overrun({report.elapsed_ms:.0f}ms>{self._budget.total_ms:.0f}ms)"
                report.degraded.append(reason)
                self._enqueue_compensation(event, reason)
        return report

    # ── 增量链路（save / publish）───────────────────────────────────────────

    def _sync_item(self, event: KnowledgeItemEvent, report: SyncReport) -> None:
        """增量索引主体：重分块 → diff → 块仓落库 → 嵌入 → 向量写入 → 可见性切换。"""
        target_status = _ACTION_ITEM_STATUS[event.action]

        # ①② 重分块 + diff（同一预算段：SR §6.1「消费 → 重分块 diff ≤10s」）
        stage_at = self._clock()
        new_chunks = self._chunker(event.body)
        plan = diff_chunks(self._chunk_store.alive_chunks(event.item_id), new_chunks)

        # ③ 块仓落库（PG 权威先行）：不变块序校正 / 新块 pending / 失效块 removed
        for unchanged in plan.unchanged:
            if unchanged.old_index != unchanged.new_index:
                self._chunk_store.move_chunk_index(unchanged.chunk_id, unchanged.new_index)
        for pending in plan.to_upsert:
            pending.chunk_id = self._chunk_store.insert_chunk(
                event.item_id,
                pending.new_index,
                pending.chunk.content,
                pending.chunk.heading_path,
                pending.content_hash,
                event.meta,
            )
        if plan.to_delete:
            self._chunk_store.mark_removed(plan.to_delete)
        if self._record_stage("chunk", stage_at, event, report):
            return  # 分段超预算：余下阶段降级交异步补偿（新块滞留 pending 自愈）

        # ④ 增量嵌入：to_upsert + unchanged 中向量未就位的自愈块（pending/failed）
        stage_at = self._clock()
        embed_pairs = self._collect_embed_pairs(plan, report)
        vectors = self._embed_stage(embed_pairs, report)
        if self._record_stage("embed", stage_at, event, report):
            return  # 嵌入超预算：本轮丢弃内存向量，块滞留原状态交补偿重放

        # ⑤ 向量写入：先 upsert 后回写状态（indexed <=> 向量可查），再删失效块
        stage_at = self._clock()
        if vectors:
            self._index_vectors(vectors, event.meta, target_status, report)
        if plan.to_delete:
            self._vector_store.delete(plan.to_delete)
            report.deleted_vectors += len(plan.to_delete)
        if self._record_stage("index", stage_at, event, report):
            return  # 入库超预算：已写部分有效，失效删除与可见性交补偿对账收尾

        # ⑥⑦ 可见性切换 + 复用计数（publish 段预算）
        stage_at = self._clock()
        self._apply_visibility(plan, event.meta, target_status, report)
        # 复用口径（SR §6.2 reuse_ids）：hash 命中且向量已就位的块，扣除为
        # 切换可见性而降级重嵌入的部分——翻标量路径不重嵌，仍计复用。
        zero_cost = sum(
            1 for unchanged in plan.unchanged
            if unchanged.vector_status == VECTOR_STATUS_INDEXED
        )
        report.reused = zero_cost - report.reembedded_for_visibility
        self._record_stage("publish", stage_at, event, report)

    def _collect_embed_pairs(
        self, plan: IncrementalPlan, report: SyncReport
    ) -> list[tuple[int, str]]:
        """汇总待嵌入块：(chunk_id, content) 对，to_upsert 优先、自愈块随后。

        自愈块 = unchanged 中 vector_status 非 indexed 的块（上轮降级滞留
        pending / 嵌入失败 failed）——hash 命中只说明内容没变，向量未就位
        仍需（重）嵌入，保证「不变块最终都有向量」的完整性口径；
        自愈成功的计数在实际入库（_index_vectors）时统一累计。
        """
        pairs: list[tuple[int, str]] = [
            (pending.chunk_id, pending.chunk.content)
            for pending in plan.to_upsert
            if pending.chunk_id is not None
        ]
        for unchanged in plan.unchanged:
            if unchanged.vector_status == VECTOR_STATUS_INDEXED:
                continue  # 向量已就位：零成本复用，不进入嵌入批次
            row = self._chunk_store.get_chunk(unchanged.chunk_id)
            if row is None:
                continue  # 行已被并发清理：交对账任务兜底
            pairs.append((unchanged.chunk_id, row.content))
        return pairs

    def _embed_stage(
        self, pairs: list[tuple[int, str]], report: SyncReport
    ) -> list[tuple[int, list[float], dict[int, float]]]:
        """嵌入阶段：按批调用 EmbeddingClient，成功产出 (chunk_id, dense, sparse)。

        失败补偿：EmbeddingError → 受影响块置 failed → 指数退避重试
        （base**attempt）→ 重试耗尽按块进死信并告警；DLQ 后继续后续批次
        （单批内容失败不扩大爆炸半径，SR §6.5）。
        """
        vectors: list[tuple[int, list[float], dict[int, float]]] = []
        for start in range(0, len(pairs), self._embed_batch_size):
            batch = pairs[start : start + self._embed_batch_size]
            outcome = self._embed_batch_with_retry(batch, report)
            if outcome is not None:
                vectors.extend(outcome)
        return vectors

    def _embed_batch_with_retry(
        self, batch: Sequence[tuple[int, str]], report: SyncReport
    ) -> list[tuple[int, list[float], dict[int, float]]] | None:
        """单批嵌入 + 重试闭环；重试耗尽返回 None（批次全部进死信）。"""
        attempt = 0
        while True:
            attempt += 1
            try:
                result = self._embedder.embed_batch([text for _, text in batch])
                if len(result) != len(batch):
                    raise EmbeddingError(
                        f"向量化结果条数（{len(result)}）与任务数（{len(batch)}）不一致"
                    )
                return [
                    (chunk_id, dense, sparse)
                    for (chunk_id, _), dense, sparse in zip(batch, result.dense, result.sparse)
                ]
            except EmbeddingError as exc:
                # 嵌入失败：受影响块置 failed（任务口径：失败补偿 chunk 级状态迁移）
                for chunk_id, _ in batch:
                    self._chunk_store.set_vector_status(chunk_id, VECTOR_STATUS_FAILED)
                if attempt > self._max_retries:
                    # 重试耗尽：按块进死信 + 告警，等待人工重放（下一次事件亦会自愈重嵌）
                    ts = now_iso()
                    for chunk_id, _ in batch:
                        self.dlq.push(DlqEntry(
                            chunk_id=chunk_id,
                            error=exc.message if hasattr(exc, "message") else str(exc),
                            attempts=attempt,
                            ts=ts,
                        ))
                        report.dlq_chunk_ids.append(chunk_id)
                        report.failed_chunk_ids.append(chunk_id)
                    self._alert(SYNC_EVENT_DLQ, {
                        "item_id": report.item_id,
                        "action": report.action,
                        "chunk_ids": [chunk_id for chunk_id, _ in batch],
                        "attempts": attempt,
                        "error": str(exc),
                    })
                    return None
                self._sleep(self._backoff_base ** attempt)  # 指数退避：2/4/8/…秒（可注入）

    def _index_vectors(
        self,
        vectors: list[tuple[int, list[float], dict[int, float]]],
        meta: ItemVectorMeta,
        target_status: str,
        report: SyncReport,
    ) -> None:
        """向量写入 + 状态回写：先入库（幂等 upsert）后迁移 indexed，再记可见性。"""
        records = [
            build_vector_record(chunk_id, meta, dense, sparse, target_status)
            for chunk_id, dense, sparse in vectors
        ]
        self._vector_store.upsert(records)
        for record in records:
            self._chunk_store.set_vector_status(
                record.chunk_id, VECTOR_STATUS_INDEXED, vector_id=str(record.chunk_id)
            )
            self._chunk_store.set_indexed_status(record.chunk_id, target_status)
        report.embedded += len(records)
        report.upserted += len(records)

    def _apply_visibility(
        self,
        plan: IncrementalPlan,
        meta: ItemVectorMeta,
        target_status: str,
        report: SyncReport,
    ) -> None:
        """可见性切换：向量已就位但 item_status 未达目标的 unchanged 块。

        优先走 ItemStatusFlipper（发布仅翻标量，秒级、零重嵌）；
        后端未实现该能力时降级为「重嵌入重写」（正确性优先，report 留痕）。
        """
        flip_ids = [
            unchanged.chunk_id for unchanged in plan.unchanged
            if unchanged.vector_status == VECTOR_STATUS_INDEXED
            and unchanged.indexed_status != target_status
        ]
        if not flip_ids:
            return
        if isinstance(self._vector_store, ItemStatusFlipper):
            self._vector_store.flip_item_status(flip_ids, target_status)
            for chunk_id in flip_ids:
                self._chunk_store.set_indexed_status(chunk_id, target_status)
            report.flipped = len(flip_ids)
            return
        # 降级路径：冻结 VectorStore 协议无读回原语，只能重嵌入重写整行
        report.degraded.append("visibility_flip_unsupported_reembed")
        pairs: list[tuple[int, str]] = []
        for chunk_id in flip_ids:
            row = self._chunk_store.get_chunk(chunk_id)
            if row is not None:
                pairs.append((chunk_id, row.content))
        vectors = self._embed_stage(pairs, report)
        if vectors:
            self._index_vectors(vectors, meta, target_status, report)
        report.reembedded_for_visibility = len(pairs)

    # ── 清理链路（archive / soft_delete，SR-KB-017 §6.3）────────────────────

    def _remove_item(self, event: KnowledgeItemEvent, report: SyncReport) -> None:
        """软删除 / 归档：块仓行保留（审计），向量侧彻底清除。

        先块仓标 removed（对账权威状态），再向量库按 chunk_id 删除；
        中间崩溃由对账任务（reconcile.py 孤儿清理）收尾。
        """
        stage_at = self._clock()
        alive = self._chunk_store.alive_chunks(event.item_id)
        chunk_ids = [chunk.chunk_id for chunk in alive]
        if chunk_ids:
            self._chunk_store.mark_removed(chunk_ids)
            self._vector_store.delete(chunk_ids)
            for chunk_id in chunk_ids:
                self._chunk_store.set_indexed_status(chunk_id, "")
        report.deleted_vectors = len(chunk_ids)
        self._record_stage("index", stage_at, event, report)

    # ── 时限预算与降级 ──────────────────────────────────────────────────────

    def _record_stage(
        self,
        stage: str,
        stage_at: float,
        event: KnowledgeItemEvent,
        report: SyncReport,
    ) -> bool:
        """记录分段耗时并判定超预算；超限登记降级 + 异步补偿，返回 True（应中止后续段）。"""
        elapsed_ms = (self._clock() - stage_at) * 1000.0
        report.stage_elapsed_ms[stage] = elapsed_ms
        budget_ms = getattr(self._budget, f"{stage}_ms")
        if elapsed_ms > budget_ms:
            reason = f"{stage}_overrun({elapsed_ms:.0f}ms>{budget_ms:.0f}ms)"
            report.degraded.append(reason)
            self._enqueue_compensation(event, reason)
            return True
        return False

    def _enqueue_compensation(self, event: KnowledgeItemEvent, reason: str) -> None:
        """登记异步补偿：滞留块保持既有状态（新块 pending），由补偿循环 / 对账自愈。"""
        self._compensations.append(CompensationEntry(
            item_id=event.item_id,
            action=event.action,
            reason=reason,
            ts=now_iso(),
        ))
        self._alert(SYNC_EVENT_DEGRADE, {
            "item_id": event.item_id,
            "action": event.action,
            "reason": reason,
        })

    # ── 事件校验 ────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_event(event: KnowledgeItemEvent) -> None:
        """入口校验（fail-fast，避免部分落库后因契约错误回滚困难）。"""
        if isinstance(event.item_id, bool) or not isinstance(event.item_id, int) or event.item_id < 1:
            raise ValueError(f"item_id 必须为正整数（=knowledge_item.id），收到：{event.item_id!r}")
        if not isinstance(event.body, str):
            raise ValueError(f"body 必须为 str，收到：{type(event.body).__name__}")
        if event.action not in (ACTION_SAVE, ACTION_PUBLISH, ACTION_ARCHIVE, ACTION_SOFT_DELETE):
            raise ValueError(f"action 必须为 {ACTION_SAVE}/{ACTION_PUBLISH}/{ACTION_ARCHIVE}/{ACTION_SOFT_DELETE}，收到：{event.action!r}")
        if event.kb_type not in KB_TYPES:
            raise ValueError(f"kb_type 必须为六大知识库枚举之一 {KB_TYPES}，收到：{event.kb_type!r}")
        for name in ("sku", "scene_tag", "process_node", "reason_type", "customer_ref"):
            value = getattr(event, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} 必须为 str，收到：{type(value).__name__}")
            if len(value) > FILTER_VALUE_MAX_LENGTH:
                raise ValueError(f"{name} 超长（上限 {FILTER_VALUE_MAX_LENGTH} 字符）：{value[:32]!r}…")


__all__ = [
    "ACTION_SAVE",
    "ACTION_PUBLISH",
    "ACTION_ARCHIVE",
    "ACTION_SOFT_DELETE",
    "SYNC_EVENT_DLQ",
    "SYNC_EVENT_DEGRADE",
    "SyncTimeBudget",
    "CompensationEntry",
    "ItemVectorMeta",
    "KnowledgeItemEvent",
    "StoredChunk",
    "SyncReport",
    "KnowledgeChunkStore",
    "InMemoryKnowledgeChunkStore",
    "ItemStatusFlipper",
    "build_vector_record",
    "split_body_blocks",
    "make_text_chunk_fn",
    "IncrementalSyncEngine",
]
