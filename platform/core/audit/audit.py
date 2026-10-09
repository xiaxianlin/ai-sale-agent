"""知识产权归甲方所有（COM-IP-01）

NFR-SEC-03 审计日志（SHA-256 哈希链）——平台基座实现。

依据：docs/07-实现方案/NFR/SR-NFR-SEC-03-审计日志.md
- §4 数据模型（audit_log 仅追加 + prev_hash/row_hash 哈希链 + 对账检查点）；
- §6.2 哈希计算（canonical_payload 规范化序列化 / compute_row_hash / compute_content_digest）；
- §6.3 写入器（链尾「读-算-写」串行化防链分叉 + event_id 幂等去重；SR §5 的统一写入器
  audit() 在本基座的进程内形态即 AuditWriter.append，tx 事务通道随 PG 实现落地）；
- §6.4 对账（gap_or_delete / chain_break / row_tampered 三类问题检测 + 对账结果自身入链）。

平台基座范围：事件模型、哈希链、内存存储（InMemoryAuditStorage）与对账；
PostgreSQL 实现（advisory lock 串行化 + 仅追加表 + 权限回收/触发器硬阻断，SR §6.5）
随服务侧迁移脚本落地，本文件仅留 PostgresAuditStorage 接口占位。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterator, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

AuditResult = Literal["success", "denied", "failed"]
"""审计结果：success=成功；denied=越权/拦截尝试（NFR-SEC-02 联动）；failed=业务失败（SR §4）。"""


class AuditEventType(str, Enum):
    """审计事件类型（SR-NFR-SEC-03 §4 CHECK 约束的八类闭枚举）。

    口径说明：SR-NFR-SEC-02 §6.4 的 'rbac_deny'/'rbac_change' 在本基座统一映射为
    PERM_CHANGE（result=denied/success，detail.kind='rbac_deny'/'rbac_change'），
    以保持与审计表闭枚举约束一致（两份 SR 文档的枚举差异在此收敛）。
    """

    LOGIN = "login"                          # 登录（成功与失败尝试均留痕）
    HUMAN_TAKEOVER = "human_takeover"        # 人工介入/接管及会话状态迁移（FR-AG-008）
    SENSITIVE_CONFIRM = "sensitive_confirm"  # 改价/订单等敏感操作人工确认（FR-AG-005）
    KB_CHANGE = "kb_change"                  # 知识修改：增删改/导入/回滚/发布（FR-KB-022）
    CONFIG_CHANGE = "config_change"          # 配置变更（app_config，NFR-MAI-02）
    PERM_CHANGE = "perm_change"              # 权限变更与越权尝试留痕（NFR-SEC-02）
    ALERT_HANDLE = "alert_handle"            # 告警处置：确认/忽略/升级（FR-AG-007）
    AUDIT_RECONCILE = "audit_reconcile"      # 对账结果自身留痕（SR §2 偏差①，防篡改闭环）


GENESIS_HASH: str = "0" * 64
"""创世 prev_hash（链首前驱，SR §6.3）。"""

CANONICAL_FIELDS: tuple[str, ...] = (
    "tenant_id",
    "actor",
    "action_type",
    "target",
    "result",
    "detail",
    "trace_id",
    "ip",
    "created_at",
)
"""参与哈希计算的字段集（写入与对账两侧逐字节一致的前提，SR §6.2）。

id / event_id / prev_hash / row_hash / content_digest 不参与内容哈希：
prev_hash 单独作为链输入，其余为链元数据。"""


class AuditEvent(BaseModel):
    """审计事件（写入器入参）：由中间件通道或业务埋点构造（SR §6.1）。"""

    model_config = ConfigDict(frozen=True)

    actor: str                                        # 操作者：用户名 / 'ai' / 'system'
    action_type: AuditEventType
    target: str                                       # 操作对象，如 'knowledge_item:1024'
    result: AuditResult = "success"
    detail: dict[str, Any] = Field(default_factory=dict)  # 变更摘要（须先过脱敏，NFR-SEC-01/04）
    trace_id: str = ""                                # 链路追踪（贯穿一次请求的全部事件）
    ip: str | None = None                             # 来源 IP（统一 str 表示，SR §6.2）
    tenant_id: int = 1                                # P0-P4 恒为 1
    event_id: str | None = None                       # 幂等去重键（at-least-once 消费防重复入链，SR §6.3）


class AuditRow(BaseModel):
    """审计链行（audit_log 表行的内存形态）。

    id 与三类哈希由写入器/存储分配计算；行对象可变——仅限内存实现与测试构造
    篡改场景，生产 PG 形态由权限回收 + 触发器保证不可变（SR §6.5）。
    """

    id: int = 0                                 # 链上序号（存储分配，等价 PG IDENTITY）
    event_id: str | None = None
    tenant_id: int = 1
    actor: str
    action_type: str                            # AuditEventType 的 value（哈希输入用纯字符串）
    target: str
    result: str
    detail: dict[str, Any] = Field(default_factory=dict)
    trace_id: str = ""
    ip: str | None = None
    created_at: str                             # 应用显式生成（UTC ISO8601 微秒，与哈希输入严格一致）
    content_digest: str = ""                    # 本条规范化序列化的 SHA-256（内容指纹）
    prev_hash: str = ""                         # 前一条 row_hash；首条为 GENESIS_HASH
    row_hash: str = ""                          # SHA256(prev_hash || 规范化序列化)


# --------------------------------------------------------------------------- 哈希链计算（SR §6.2）


def _stringize(value: Any) -> Any:
    """detail 规范化：数值统一字符串化（规避 jsonb 数值重解析差异，SR §6.2）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _stringize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringize(v) for v in value]
    return value


def canonical_payload(row: Mapping[str, Any]) -> str:
    """字段规范化序列化：固定字段集 + sort_keys + 紧凑分隔符（SR §6.2）。

    写入与对账两侧调用同一函数，保证逐字节一致——哈希链可比对的前提。
    """
    payload = {k: _stringize(row[k]) for k in CANONICAL_FIELDS}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_row_hash(prev_hash: str, row: Mapping[str, Any]) -> str:
    """row_hash = SHA256(前条 row_hash + '|' + 本条规范化序列化)（SR §6.2）。"""
    return hashlib.sha256(f"{prev_hash}|{canonical_payload(row)}".encode("utf-8")).hexdigest()


def compute_content_digest(row: Mapping[str, Any]) -> str:
    """content_digest = SHA256(本条规范化序列化)：与 prev_hash 无关的内容指纹（SR §6.2）。"""
    return hashlib.sha256(canonical_payload(row).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- 存储抽象（SR §6.3：仅追加/只读）


class AuditStorage(ABC):
    """审计存储抽象：仅追加 / 只读（repo 层无任何 UPDATE/DELETE 方法）。

    PG 形态另有两层不可变强制（业务角色权限回收 + 触发器硬阻断，SR §6.5 迁移脚本），
    与本接口共同构成防篡改纵深；哈希链对账（verify_chain）是最终兜底。
    """

    @abstractmethod
    def append_row(self, row: AuditRow) -> int:
        """追加一行（分配并写回 id），返回链上 id。"""

    @abstractmethod
    def tail(self) -> tuple[int, str] | None:
        """读链尾 (id, row_hash)；空链返回 None。"""

    @abstractmethod
    def iter_rows(self) -> Iterator[AuditRow]:
        """按 id 升序迭代全部行（对账用；PG 形态为服务端游标流式扫描，SR §6.4）。"""

    @abstractmethod
    def find_by_event_id(self, event_id: str) -> AuditRow | None:
        """幂等去重查询（event_id 唯一索引）。"""


class InMemoryAuditStorage(AuditStorage):
    """内存存储实现（单元测试与本地开发用；线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[AuditRow] = []
        self._max_id = 0
        self._by_event_id: dict[str, AuditRow] = {}

    @property
    def rows(self) -> list[AuditRow]:
        """内部行列表的活引用：仅供测试构造篡改/删除场景与只读检索，生产代码勿用。"""
        return self._rows

    def append_row(self, row: AuditRow) -> int:
        with self._lock:
            self._max_id += 1
            row.id = self._max_id
            self._rows.append(row)
            if row.event_id is not None:
                self._by_event_id[row.event_id] = row
            return row.id

    def tail(self) -> tuple[int, str] | None:
        with self._lock:
            if not self._rows:
                return None
            last = self._rows[-1]
            return (last.id, last.row_hash)

    def iter_rows(self) -> Iterator[AuditRow]:
        with self._lock:
            return iter(list(self._rows))  # 快照迭代，避免对账期间并发追加干扰

    def find_by_event_id(self, event_id: str) -> AuditRow | None:
        with self._lock:
            return self._by_event_id.get(event_id)


class PostgresAuditStorage(AuditStorage):
    """PostgreSQL 存储实现占位（接口留白，随服务侧落地）。

    落地要点（SR §4/§6.5，迁移脚本交付）：
    - audit_log 仅追加表（IDENTITY 主键 + 八类 action_type CHECK + result CHECK）；
    - 业务角色仅 INSERT/SELECT；UPDATE/DELETE/TRUNCATE 由触发器硬阻断；
    - 写入路径 pg_advisory_xact_lock(AUDIT_LOCK_KEY) 串行化（本基座以线程锁等价实现）。
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def append_row(self, row: AuditRow) -> int:
        raise NotImplementedError("PG 实现随服务侧落地（平台基座仅提供内存实现与接口）")

    def tail(self) -> tuple[int, str] | None:
        raise NotImplementedError("PG 实现随服务侧落地（平台基座仅提供内存实现与接口）")

    def iter_rows(self) -> Iterator[AuditRow]:
        raise NotImplementedError("PG 实现随服务侧落地（平台基座仅提供内存实现与接口）")

    def find_by_event_id(self, event_id: str) -> AuditRow | None:
        raise NotImplementedError("PG 实现随服务侧落地（平台基座仅提供内存实现与接口）")


# --------------------------------------------------------------------------- 对账结果（SR §4 audit_reconcile_log）


@dataclass(frozen=True)
class ReconcileReport:
    """对账结果（audit_reconcile_log 检查点的内存形态，SR §4/§6.4）。

    problems 元素结构（kind 三类，SR §6.4）：
    - {"kind": "row_tampered", "id": ...}                  行内容被篡改（重算不一致）；
    - {"kind": "chain_break", "id": ..., ...}              链指针断裂（插入/重排伪造行）；
    - {"kind": "gap_or_delete", "expect_id": ..., "found_id": ...}  id 跳变（中间行被删除）。
    """

    verdict: Literal["pass", "fail"]
    checked_until_id: int                          # 本次校验覆盖到的链尾 id（增量对账锚点）
    chain_head_hash: str                           # 校验时的链尾 row_hash
    problems: tuple[dict[str, Any], ...] = field(default=())

    @property
    def ok(self) -> bool:
        """校验是否通过（verdict == pass）。"""
        return self.verdict == "pass"


# --------------------------------------------------------------------------- 哈希链写入器（SR §6.3/§6.4）


class AuditWriter:
    """哈希链写入器：append() 串行化 + 幂等；verify_chain() 全链对账。

    并发串行化：进程内 threading.Lock 保证「读链尾 → 计算哈希 → 写入」原子执行，
    防止并发追加争抢同一 prev_hash 造成链分叉（PG 形态等价物为事务级
    pg_advisory_xact_lock，SR §6.3）。
    """

    def __init__(self, storage: AuditStorage) -> None:
        self._storage = storage
        self._lock = threading.Lock()

    @property
    def storage(self) -> AuditStorage:
        """底层存储（对账/导出/检索用只读入口）。"""
        return self._storage

    def append(self, event: AuditEvent) -> int:
        """链尾追加事件，返回链上 id（SR §6.3 append_event 的进程内形态）。

        - 幂等：event_id 非空且已入链 → 直接返回既有 id，不重复入链；
        - 串行化：全程持锁（读链尾 → 算哈希 → 写入），链不分叉；
        - created_at 由应用显式生成并参与哈希（禁用 DB 默认值，保证两侧严格一致）。
        """
        with self._lock:
            if event.event_id is not None:
                existing = self._storage.find_by_event_id(event.event_id)
                if existing is not None:
                    return existing.id
            tail = self._storage.tail()
            prev_hash = tail[1] if tail is not None else GENESIS_HASH
            row = AuditRow(
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                actor=event.actor,
                action_type=event.action_type.value,
                target=event.target,
                result=event.result,
                detail=dict(event.detail),
                trace_id=event.trace_id,
                ip=event.ip,
                created_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            )
            row.content_digest = compute_content_digest(row.model_dump())
            row.row_hash = compute_row_hash(prev_hash, row.model_dump())
            row.prev_hash = prev_hash
            return self._storage.append_row(row)

    def verify_chain(self) -> ReconcileReport:
        """全链对账（每日定时 + 手动触发，SR §6.4）：

        - id 跳变 → gap_or_delete（中间行被删除或归档后人为清表）；
        - prev_hash 与前条 row_hash 不符 → chain_break（插入/重排伪造行）；
        - 重算 row_hash 不一致 → row_tampered（行内容被篡改）。

        注意：若攻击者篡改行并重算后续全部哈希，本链校验不报警——该场景由
        OSS 异地归档比对兜底（SR §6.4 compare_with_oss_archive，部署侧实现）；
        链尾截断同样检不出（无后续 id），依赖对账检查点锚点与归档 manifest。
        """
        problems: list[dict[str, Any]] = []
        prev_hash = GENESIS_HASH
        expected_id = 1
        head_hash = GENESIS_HASH
        last_id = 0
        for row in self._storage.iter_rows():
            if row.id != expected_id:
                problems.append({"kind": "gap_or_delete", "expect_id": expected_id, "found_id": row.id})
            if row.prev_hash != prev_hash:
                problems.append(
                    {"kind": "chain_break", "id": row.id,
                     "expect_prev": prev_hash, "actual_prev": row.prev_hash}
                )
            elif compute_row_hash(prev_hash, row.model_dump()) != row.row_hash:
                problems.append({"kind": "row_tampered", "id": row.id})
            prev_hash = row.row_hash
            head_hash = row.row_hash
            last_id = row.id
            expected_id = row.id + 1
        verdict: Literal["pass", "fail"] = "fail" if problems else "pass"
        return ReconcileReport(
            verdict=verdict, checked_until_id=last_id, chain_head_hash=head_hash, problems=tuple(problems)
        )

    def run_reconcile(self, alerter: Callable[[ReconcileReport], None] | None = None) -> ReconcileReport:
        """对账入口（SR §6.4 run_reconcile 的进程内形态）：

        校验 → 对账结果以 audit_reconcile 事件写入链尾（防「改对账报告掩盖问题」，
        篡改对账结果同样会在下一次对账中被哈希链检出）→ 断链高级告警
        （alerter 由部署侧接入钉钉/微信 + 后台红色横幅，FR-AG-007）。
        """
        report = self.verify_chain()
        self.append(
            AuditEvent(
                actor="system",
                action_type=AuditEventType.AUDIT_RECONCILE,
                target="audit_log",
                result="failed" if not report.ok else "success",
                detail={
                    "checked_until_id": report.checked_until_id,
                    "problems": [dict(p) for p in report.problems[:50]],
                },
                event_id=f"reconcile-{uuid.uuid4().hex}",  # 非幂等：每次对账独立留痕
            )
        )
        if not report.ok and alerter is not None:
            alerter(report)
        return report
