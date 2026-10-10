# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""知识版本控制包（FR-KB-022 版本控制，迭代 0.3）：store / diff / service 公开门面。

    store   VersionStore 端口 + 内存实现（线性版本链 / UNIQUE 幂等 / CAS）
    diff    字段级分类对比引擎（text / tree / list 三类 + DiffReport UI 契约）
    service VersioningService（save 自动快照 / 两版对比 / 引用式回滚 + 钩子）
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from .diff import (
    KIND_LIST,
    KIND_SCALAR,
    KIND_TEXT,
    KIND_TREE,
    DiffReport,
    FieldChange,
    classify_kind,
    diff_snapshots,
    list_diff,
    text_diff,
    tree_diff,
)
from .service import (
    AUDIT_ROLLBACK,
    AUDIT_SNAPSHOT,
    AuditHook,
    InvalidItemPayload,
    InvalidVersion,
    ItemNotFound,
    ResyncHook,
    SnapshotNotFound,
    VersionConflict,
    VersioningError,
    VersioningService,
    digest_of,
)
from .store import (
    InMemoryVersionStore,
    ItemRecord,
    VersionSnapshot,
    VersionStore,
    utc_now,
)

__all__ = [
    "AUDIT_ROLLBACK",
    "AUDIT_SNAPSHOT",
    "AuditHook",
    "DiffReport",
    "FieldChange",
    "InMemoryVersionStore",
    "InvalidItemPayload",
    "InvalidVersion",
    "ItemNotFound",
    "KIND_LIST",
    "KIND_SCALAR",
    "KIND_TEXT",
    "KIND_TREE",
    "ItemRecord",
    "ResyncHook",
    "SnapshotNotFound",
    "VersionConflict",
    "VersioningError",
    "VersioningService",
    "VersionSnapshot",
    "VersionStore",
    "classify_kind",
    "diff_snapshots",
    "digest_of",
    "list_diff",
    "text_diff",
    "tree_diff",
    "utc_now",
]
