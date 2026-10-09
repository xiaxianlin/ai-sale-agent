"""知识产权归甲方所有（COM-IP-01）

出域治理基座包（NFR-SEC-06 数据不出域）：白名单引擎 + LLM 出域网关 + 月度审计报表。

对外入口统一从本包导入（内部模块路径不属于稳定契约）；
实现依据：docs/07-实现方案/NFR/SR-NFR-SEC-06-数据不出域.md、
docs/02-设计文档/06-安全与合规设计.md §6。
"""

from platform.core.net.egress import (
    EGRESS_NOT_CONFIRMED,
    EGRESS_PURPOSE_OVERFLOW,
    EGRESS_UNREGISTERED,
    CallVerdict,
    DestStatus,
    EgressCallRecord,
    EgressCheck,
    EgressDestination,
    EgressError,
    EgressGuard,
    EgressAuditHook,
    EgressRegistryError,
    EgressViolation,
    content_digest,
)
from platform.core.net.llm_gateway import (
    EGRESS_SENSITIVE_BLOCKED,
    REDACTED_MARK,
    ContentFilter,
    ContentSnapshot,
    FilterOutcome,
    InMemorySnapshotStore,
    LlmEgressResult,
    LlmGatewayConfig,
    LlmHttpClient,
    LLMEgressGateway,
    SensitiveWordFilter,
    SnapshotStore,
)
from platform.core.net.report import EgressReportGenerator, build_monthly_report

__all__ = [
    "EGRESS_NOT_CONFIRMED",
    "EGRESS_PURPOSE_OVERFLOW",
    "EGRESS_SENSITIVE_BLOCKED",
    "EGRESS_UNREGISTERED",
    "REDACTED_MARK",
    "CallVerdict",
    "ContentFilter",
    "ContentSnapshot",
    "DestStatus",
    "EgressCallRecord",
    "EgressCheck",
    "EgressDestination",
    "EgressError",
    "EgressGuard",
    "EgressAuditHook",
    "EgressRegistryError",
    "EgressViolation",
    "FilterOutcome",
    "InMemorySnapshotStore",
    "LlmEgressResult",
    "LlmGatewayConfig",
    "LlmHttpClient",
    "LLMEgressGateway",
    "SensitiveWordFilter",
    "SnapshotStore",
    "build_monthly_report",
    "content_digest",
    "EgressReportGenerator",
]
