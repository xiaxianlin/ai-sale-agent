"""知识产权归甲方所有（COM-IP-01）

审计基座包（NFR-SEC-03 审计日志·哈希链）：事件模型、哈希链写入器与对账。

对外入口统一从本包导入（内部模块路径不属于稳定契约）；
实现依据：docs/07-实现方案/NFR/SR-NFR-SEC-03-审计日志.md。
"""

from foundation.audit.audit import (
    GENESIS_HASH,
    CANONICAL_FIELDS,
    AuditEvent,
    AuditEventType,
    AuditResult,
    AuditRow,
    AuditStorage,
    AuditWriter,
    InMemoryAuditStorage,
    PostgresAuditStorage,
    ReconcileReport,
    canonical_payload,
    compute_content_digest,
    compute_row_hash,
)

__all__ = [
    "CANONICAL_FIELDS",
    "GENESIS_HASH",
    "AuditEvent",
    "AuditEventType",
    "AuditResult",
    "AuditRow",
    "AuditStorage",
    "AuditWriter",
    "InMemoryAuditStorage",
    "PostgresAuditStorage",
    "ReconcileReport",
    "canonical_payload",
    "compute_content_digest",
    "compute_row_hash",
]
