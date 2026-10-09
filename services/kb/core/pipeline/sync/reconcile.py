# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""对账任务（FR-KB-017 增量更新，Phase 0 / 迭代 0.2）：PG ↔ 向量库一致性自愈。

以块仓（PG knowledge_chunk，权威侧）的「应索引集」与向量库实际「已索引集」
做双向差集（SR-KB-017 §6.5）：

    缺失（missing，漏入库）  块仓 vector_status=indexed 而向量库无此主键
                            → 修复：重嵌入 + upsert（自愈重索引）；
                              无嵌入端点时仅置回 pending 交异步补偿（deferred）。
    孤儿（orphan，漏删除）   向量库残留而块仓不应索引（软删除残留 / 中间崩溃）
                            → 修复：按 chunk_id 直接删除（PG 权威，向量可全量重建）。

库存探查经 VectorInventory 端口注入（VectorStore 冻结协议不含全量主键枚举；
生产实现对接 Milvus query 迭代主键，测试用内存实现）。

依据文档：docs/07-实现方案/KB/SR-KB-017-增量更新.md §6.5、§8（NFR-REL-03）
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from ..embedding.base import EmbeddingClient, EmbeddingError
from ..embedding.embed_orchestrator import VECTOR_STATUS_INDEXED, VECTOR_STATUS_PENDING
from ...vectorstore.base import ITEM_STATUS_PUBLISHED, VectorStore
from .engine import KnowledgeChunkStore, SyncAlertHook, build_vector_record

#: 对账修复告警事件名（与 engine SYNC_EVENT_* 同风格）
RECONCILE_EVENT_REPAIR = "RECONCILE_REPAIR"


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（毫秒精度），供对账报告留痕。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class VectorInventory(Protocol):
    """向量库主键清单端口：对账专用（迭代 0.2 内存实现，生产对接 Milvus 迭代器）。"""

    def all_chunk_ids(self) -> set[int]:
        """枚举向量库当前全部主键（已入库集，含未发布记录）。"""
        ...


@dataclass
class ReconcileReport:
    """一次对账的差集清单与修复账目（run_sample / run_full 共用产物）。"""

    expected_ids: list[int] = field(default_factory=list)   # 应索引集（块仓 indexed）
    actual_ids: list[int] = field(default_factory=list)     # 已索引集（向量库主键）
    missing: list[int] = field(default_factory=list)        # 漏入库（PG=indexed 而向量缺失）
    orphans: list[int] = field(default_factory=list)        # 漏删除（向量残留而 PG 不应索引）
    repaired_missing: list[int] = field(default_factory=list)   # 已补回向量的缺失块
    repaired_orphans: list[int] = field(default_factory=list)   # 已清除的孤儿向量
    deferred_missing: list[int] = field(default_factory=list)   # 未修复（置回 pending 交异步补偿）
    ts: str = field(default_factory=_now_iso)               # 对账时间

    @property
    def consistent(self) -> bool:
        """本轮对账后是否仍存在未愈合差异（True = 双侧一致或已全部修复）。"""
        return not (
            set(self.missing) - set(self.repaired_missing) - set(self.deferred_missing)
        ) and not (set(self.orphans) - set(self.repaired_orphans))


class ReconcileTask:
    """对账任务：差集发现 + 修复动作执行（幂等，可周期重放）。

    用法::

        task = ReconcileTask(chunk_store, vector_store, inventory, embedder=client)
        report = task.run()          # 发现并修复
        report = task.run(repair=False)  # 只盘点不修复（巡检口径）
    """

    def __init__(
        self,
        chunk_store: KnowledgeChunkStore,
        vector_store: VectorStore,
        inventory: VectorInventory,
        *,
        embedder: EmbeddingClient | None = None,
        alert: SyncAlertHook | None = None,
    ) -> None:
        """embedder 缺省 None：缺失块只置回 pending（deferred），不自行重嵌。"""
        self._chunk_store = chunk_store
        self._vector_store = vector_store
        self._inventory = inventory
        self._embedder = embedder
        self._alert = alert if alert is not None else (lambda event, payload: None)

    def run(self, *, repair: bool = True) -> ReconcileReport:
        """执行一次对账：差集计算 +（可选）修复动作。

        修复动作（SR-KB-017 §6.5 repair）：
        - missing：块仓行存在且有嵌入端点 → 重嵌入 upsert（自愈重索引）；
          无端点或嵌入失败 → 置回 pending 交异步补偿（deferred_missing）；
        - orphan：向量库按主键直接删除（含软删除残留一并清理）。
        """
        expected = {row.chunk_id for row in self._chunk_store.indexed_chunks()}
        actual = set(self._inventory.all_chunk_ids())
        report = ReconcileReport(
            expected_ids=sorted(expected),
            actual_ids=sorted(actual),
            missing=sorted(expected - actual),
            orphans=sorted(actual - expected),
        )
        if not repair:
            return report
        for chunk_id in report.missing:
            self._repair_missing(chunk_id, report)
        if report.orphans:
            self._vector_store.delete(report.orphans)  # 幂等：不存在的主键静默忽略
            report.repaired_orphans = list(report.orphans)
        if report.missing or report.orphans:
            self._alert(RECONCILE_EVENT_REPAIR, {
                "missing": report.missing,
                "orphans": report.orphans,
                "repaired_missing": report.repaired_missing,
                "repaired_orphans": report.repaired_orphans,
                "deferred_missing": report.deferred_missing,
            })
        return report

    def _repair_missing(self, chunk_id: int, report: ReconcileReport) -> None:
        """修复单个缺失块：重嵌入 + upsert + 状态回写（PG=indexed 权威不降级）。"""
        row = self._chunk_store.get_chunk(chunk_id)
        if row is None:
            return  # 块仓行已消失（并发软删）：该向量将由 orphan 侧清理兜底
        if self._embedder is None:
            # 无嵌入端点（巡检态部署）：置回 pending，交增量事件 / 补偿循环自愈
            self._chunk_store.set_vector_status(chunk_id, VECTOR_STATUS_PENDING)
            report.deferred_missing.append(chunk_id)
            return
        try:
            result = self._embedder.embed_batch([row.content])
        except EmbeddingError:
            # 嵌入服务不可用：保守置回 pending（零丢失），本轮记 deferred
            self._chunk_store.set_vector_status(chunk_id, VECTOR_STATUS_PENDING)
            report.deferred_missing.append(chunk_id)
            return
        item_status = row.indexed_status or ITEM_STATUS_PUBLISHED  # 未写过可见性时按已发布补齐
        record = build_vector_record(
            chunk_id, row.meta, result.dense[0], result.sparse[0], item_status
        )
        self._vector_store.upsert([record])
        self._chunk_store.set_vector_status(
            chunk_id, VECTOR_STATUS_INDEXED, vector_id=str(chunk_id)
        )
        report.repaired_missing.append(chunk_id)


__all__ = [
    "RECONCILE_EVENT_REPAIR",
    "VectorInventory",
    "ReconcileReport",
    "ReconcileTask",
]
