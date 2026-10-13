# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V1 查询与控制面逻辑（FR-AG-006，SR-AG-006 §6.3；迭代 2.3 demo 口径）。

职责（后台 = **纯读 + 控制面**：不直接操作平台，一切执行经执行器/风控通道）：
- :class:`AdminViews`——会话中心（四态过滤/分页/消息流增量游标）、人工介入开关
  （接管/放回走状态机 + state_hist 留痕 + 审计）、敏感确认（对比视图 + 确认/驳回
  决策双人可见）、审计检索（多维筛选 + 哈希链完整性标识）；
- :class:`HashChainAuditLog`——审计日志哈希链（NFR-SEC-03：仅追加、row_hash =
  SHA256(prev_hash‖canonical)，篡改任一行即校验失败）；
- :class:`SensitiveBoard`——敏感操作草稿看板（sensitive_action 语义：AI 只起草，
  人工确认/驳回，决策人/时刻/理由双人可见）。

数据源全部注入（demo = 进程内结构；生产 = PG conversation/message/audit_log 读模型，
越权防护由服务端 scope 注入承载——前端传参无效，SR-AG-006 §2③）。
"""

from __future__ import annotations
import logging

_LOGGER = logging.getLogger(__name__)

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol

from services.agent.core.state import (
    ConversationNotFoundError,
    ConversationStateMachine,
    ConvState,
    IllegalTransitionError,
    SessionStoreProtocol,
)


# ─────────────────────────── 审计哈希链（NFR-SEC-03） ───────────────────────────


def _canonical(row: Mapping[str, object]) -> str:
    """规范化序列化（row_hash 与 prev_hash 不参与哈希）。"""
    payload = {key: row[key] for key in sorted(row) if key not in ("row_hash",)}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


class HashChainAuditLog:
    """审计日志（audit_log 哈希链语义的进程内等价：仅追加、逐行校验）。

    row_hash = SHA256(prev_hash ‖ canonical(row))——篡改任一行内容即链断裂，
    verify() 可发现（操作日志全量留痕、不可篡改、可检索）。
    """

    GENESIS = "GENESIS"

    def __init__(self) -> None:
        self._entries: list[dict[str, object]] = []
        self._prev_hash = self.GENESIS

    def append(
        self,
        event: Mapping[str, object],
        *,
        actor: str,
        source: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """追加一条审计（action_type/target 等事件字段 + seq/ts/actor/source + 链字段）。"""
        row: dict[str, object] = {
            "seq": len(self._entries) + 1,
            "ts": (now if now is not None else datetime.now()).isoformat(),
            "actor": actor,
            "source": source,
            **event,
            "prev_hash": self._prev_hash,
        }
        row["row_hash"] = hashlib.sha256(
            (str(self._prev_hash) + _canonical(row)).encode("utf-8")
        ).hexdigest()[:16]
        self._entries.append(row)
        self._prev_hash = str(row["row_hash"])
        return dict(row)

    def all(self) -> list[dict[str, object]]:
        """全量条目（只读副本）。"""
        return [dict(row) for row in self._entries]

    def verify(self) -> bool:
        """链完整性校验：逐行重算 row_hash 并核对前向指针（任一断裂即 False）。"""
        prev = self.GENESIS
        for row in self._entries:
            if row.get("prev_hash") != prev:
                return False
            expect = hashlib.sha256(
                (prev + _canonical(row)).encode("utf-8")
            ).hexdigest()[:16]
            if row.get("row_hash") != expect:
                return False
            prev = str(row["row_hash"])
        return True


# ─────────────────────────── 敏感操作草稿看板 ───────────────────────────


class SensitiveNotFoundError(LookupError):
    """敏感草稿不存在（API 层映射 40404）。"""


class SensitiveStateError(RuntimeError):
    """敏感草稿状态冲突（已决策不可再改；API 层映射 40901）。"""


@dataclass
class SensitiveActionRecord:
    """敏感操作草稿（sensitive_action 行语义；决策字段双人可见）。"""

    uid: str
    conversation_uid: str
    action_type: str
    draft_content: dict[str, object]                    # AI 起草内容（对比视图左栏）
    current_snapshot: dict[str, object] = field(default_factory=dict)  # 现状快照（右栏）
    status: str = "pending"                             # pending / confirmed / rejected
    created_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    reject_reason: str | None = None


class SensitiveBoard:
    """敏感草稿看板（AI 只起草，人工确认/驳回——§7.4 红线的人工决策面）。"""

    def __init__(self) -> None:
        self._items: dict[str, SensitiveActionRecord] = {}

    def create(
        self,
        *,
        conversation_uid: str,
        action_type: str,
        draft_content: Mapping[str, object],
        current_snapshot: Mapping[str, object] | None = None,
        now: datetime | None = None,
    ) -> SensitiveActionRecord:
        """登记待确认草稿。"""
        record = SensitiveActionRecord(
            uid=uuid.uuid4().hex[:16], conversation_uid=conversation_uid,
            action_type=action_type, draft_content=dict(draft_content),
            current_snapshot=dict(current_snapshot or {}), created_at=now,
        )
        self._items[record.uid] = record
        return record

    def get(self, uid: str) -> SensitiveActionRecord | None:
        """按 uid 读取草稿。"""
        return self._items.get(uid)

    def _require(self, uid: str) -> SensitiveActionRecord:
        record = self._items.get(uid)
        if record is None:
            raise SensitiveNotFoundError(f"敏感草稿 {uid} 不存在")
        return record

    def list(self, *, status: str | None = None) -> list[SensitiveActionRecord]:
        """草稿列表（默认按创建序；status 过滤）。"""
        rows = list(self._items.values())
        if status is not None:
            rows = [row for row in rows if row.status == status]
        return rows

    def confirm(self, uid: str, *, operator: str, now: datetime) -> SensitiveActionRecord:
        """确认执行（决策人/时刻留痕——操作者与主管双人可见）。"""
        record = self._require(uid)
        if record.status != "pending":
            raise SensitiveStateError(f"草稿 {uid} 已决策（{record.status}），不可重复决策")
        record.status = "confirmed"
        record.decided_by, record.decided_at = operator, now
        return record

    def reject(
        self, uid: str, *, operator: str, reason: str, now: datetime
    ) -> SensitiveActionRecord:
        """驳回（理由对操作者与主管双人可见）。"""
        record = self._require(uid)
        if record.status != "pending":
            raise SensitiveStateError(f"草稿 {uid} 已决策（{record.status}），不可重复决策")
        record.status = "rejected"
        record.decided_by, record.decided_at, record.reject_reason = operator, now, reason
        return record


# ─────────────────────────── 后台查询与控制面 ───────────────────────────


class ClockFn(Protocol):
    """时钟协议。"""

    def __call__(self) -> datetime: ...


class AdminViews:
    """管理后台查询与控制面（数据源注入；前端仅消费，越权防护在服务端）。

    :param store / state_machine: 会话状态机（接管/放回/敏感联动走迁移 + 留痕）；
    :param audit_log: 哈希链审计；
    :param sensitive: 敏感草稿看板（缺省自建）；
    :param messages: 消息流数据源（conv_uid → [{msg_id, sender, content,
        confidence, knowledge_refs, created_at}]；生产 = PG message 增量游标读）。
    """

    def __init__(
        self,
        *,
        store: SessionStoreProtocol,
        state_machine: ConversationStateMachine,
        audit_log: HashChainAuditLog,
        sensitive: SensitiveBoard | None = None,
        messages: dict[str, list[dict[str, object]]] | None = None,
        clock: ClockFn | None = None,
    ) -> None:
        self._store = store
        self._sm = state_machine
        self.audit_log = audit_log
        self.sensitive = sensitive if sensitive is not None else SensitiveBoard()
        self.messages: dict[str, list[dict[str, object]]] = (
            messages if messages is not None else {}
        )
        self._clock = clock or (lambda: datetime.now())

    # ---------------------------------------------------------------- 会话中心

    def list_sessions(
        self, *, state: str | None = None, page: int = 1, page_size: int = 20
    ) -> dict[str, object]:
        """会话列表：四态过滤（ai_managed/human_managed/pending_confirm/closed）
        + 服务端分页（§5 会话列表接口的模块内形态）。"""
        rows = self._store.list_all()
        if state is not None:
            rows = [row for row in rows if row.state.value == state]
        rows = sorted(rows, key=lambda row: row.uid)
        total = len(rows)
        start = max(0, (page - 1) * page_size)
        items = [
            {
                "uid": row.uid,
                "state": row.state.value,
                "state_version": row.state_version,
                "state_hist_len": len(row.state_hist),
                "pending_since": row.pending_since,
            }
            for row in rows[start:start + page_size]
        ]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def session_messages(self, uid: str, *, after_id: int = 0, limit: int = 100) -> dict[str, object]:
        """消息流：增量游标（after_id）轮询；AI 消息含置信度与知识来源（三色标识数据面）。"""
        self._require_conversation(uid)
        rows = [
            self._normalize_message(message)
            for message in self.messages.get(uid, [])
            if int(message.get("msg_id", 0)) > after_id
        ]
        rows = rows[:limit]
        next_after = max((int(row["msg_id"]) for row in rows), default=after_id)  # type: ignore[arg-type]
        return {"items": rows, "next_after_id": next_after}

    # ---------------------------------------------------------------- 人工介入开关

    def takeover(self, uid: str, *, operator: str, reason: str) -> dict[str, object]:
        """人工接管：ai_managed → human_managed（状态机迁移 + state_hist + 审计）。

        仅 ai_managed 可接管（SR-AG-006 §6.3 迁移规则）：closed 为已结束会话、
        pending_confirm 仅允许挂起超时转人工（§6.4）——其余状态前置拒绝，
        不借道状态机「重开」路径绕过。
        """
        self._require_conversation(uid)
        current, _version = self._sm.get_state(uid)
        if current is not ConvState.AI_MANAGED:
            raise IllegalTransitionError(
                f"状态 {current.value} 不可接管（仅 ai_managed 可人工接管，FR-AG-008 迁移规则）"
            )
        result = self._sm.transition(uid, ConvState.HUMAN_MANAGED, actor=operator,
                                     reason=reason)
        self._audit("human_takeover", uid, operator, "human", {"reason": reason})
        return self._session_view(uid, result.to_state)

    def release(self, uid: str, *, operator: str, reason: str) -> dict[str, object]:
        """人工放回：human_managed / pending_confirm → ai_managed（留痕 + 审计）。"""
        result = self._sm.transition(uid, ConvState.AI_MANAGED, actor=operator, reason=reason)
        self._audit("human_release", uid, operator, "human", {"reason": reason})
        return self._session_view(uid, result.to_state)

    # ---------------------------------------------------------------- 敏感确认

    def list_sensitive(self, *, status: str | None = None) -> list[dict[str, object]]:
        """草稿列表（默认全部；决策人/时刻/驳回理由列双人可见）。"""
        return [self._sensitive_row(row) for row in self.sensitive.list(status=status)]

    def sensitive_diff(self, uid: str) -> dict[str, object]:
        """对比视图：draft_content vs current_snapshot + 差异键（标红数据源）。"""
        record = self.sensitive.get(uid)
        if record is None:
            raise SensitiveNotFoundError(f"敏感草稿 {uid} 不存在")
        diff_keys = [
            key for key in record.draft_content
            if record.draft_content.get(key) != record.current_snapshot.get(key)
        ]
        return {
            "uid": record.uid,
            "conversation_uid": record.conversation_uid,
            "action_type": record.action_type,
            "status": record.status,
            "draft_content": dict(record.draft_content),
            "current_snapshot": dict(record.current_snapshot),
            "diff_keys": sorted(diff_keys),
        }

    def confirm_sensitive(
        self, uid: str, *, operator: str, now: datetime | None = None
    ) -> dict[str, object]:
        """确认执行：草稿 confirmed + 审计 sensitive_confirm + 会话放回 ai_managed。"""
        moment = now if now is not None else self._clock()
        record = self.sensitive.confirm(uid, operator=operator, now=moment)
        self._audit("sensitive_confirm", record.conversation_uid, operator, "human",
                    {"sensitive_uid": uid, "action_type": record.action_type})
        self._release_after_decision(record, operator)
        return self._sensitive_row(record)

    def reject_sensitive(
        self, uid: str, *, operator: str, reason: str, now: datetime | None = None
    ) -> dict[str, object]:
        """驳回：理由留痕（双人可见）+ 审计 sensitive_reject + 草稿关闭后会话放回。"""
        moment = now if now is not None else self._clock()
        record = self.sensitive.reject(uid, operator=operator, reason=reason, now=moment)
        self._audit("sensitive_reject", record.conversation_uid, operator, "human",
                    {"sensitive_uid": uid, "reason": reason})
        self._release_after_decision(record, operator)
        return self._sensitive_row(record)

    # ---------------------------------------------------------------- 审计检索

    def search_audit(
        self,
        *,
        ts_from: str | None = None,
        ts_to: str | None = None,
        actor: str | None = None,
        action_type: str | None = None,
        source: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, object]:
        """审计检索：时间/操作者/动作类型/来源多维筛选 + 哈希链完整性标识。"""
        rows = self.audit_log.all()
        if ts_from is not None:
            rows = [row for row in rows if str(row.get("ts", "")) >= ts_from]
        if ts_to is not None:
            rows = [row for row in rows if str(row.get("ts", "")) <= ts_to]
        if actor is not None:
            rows = [row for row in rows if row.get("actor") == actor]
        if action_type is not None:
            rows = [row for row in rows if row.get("action_type") == action_type]
        if source is not None:
            rows = [row for row in rows if row.get("source") == source]
        total = len(rows)
        start = max(0, (page - 1) * page_size)
        return {
            "items": rows[start:start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "chain_ok": self.audit_log.verify(),
        }

    # ---------------------------------------------------------------- 内部

    def _require_conversation(self, uid: str) -> None:
        if self._store.get(uid) is None:
            raise ConversationNotFoundError(f"会话 {uid} 不存在")

    def _session_view(self, uid: str, to_state: ConvState) -> dict[str, object]:
        """迁移结果视图：state + state_hist 全量留痕（SR-AG-006 §5 响应形态）。"""
        record = self._store.get(uid)
        return {
            "uid": uid,
            "state": to_state.value,
            "state_hist": [dict(entry) for entry in (record.state_hist if record else [])],
        }

    def _release_after_decision(self, record: SensitiveActionRecord, operator: str) -> None:
        """决策后会话放回：pending_confirm → ai_managed（草稿关闭后放回；
        其他状态幂等跳过——人工接管中不抢回）。"""
        try:
            self._sm.transition(record.conversation_uid, ConvState.AI_MANAGED,
                                actor=operator, reason=f"sensitive_{record.status}")
        except (IllegalTransitionError, ConversationNotFoundError) as exc:
            # 幂等跳过（如人工接管中不抢回）：留痕便于事后核对跳过原因（CR Q2 补日志）
            _LOGGER.info("确认后回迁 AI 态跳过（幂等, conv=%s）: %s", record.conversation_uid, type(exc).__name__)

    @staticmethod
    def _normalize_message(message: dict[str, object]) -> dict[str, object]:
        """消息行规范化（knowledge_refs 富化为 {chunk_id,kb_type,title,score}）。"""
        row = dict(message)
        row["knowledge_refs"] = [dict(ref) for ref in (message.get("knowledge_refs") or [])]  # type: ignore[union-attr]
        return row

    @staticmethod
    def _sensitive_row(record: SensitiveActionRecord) -> dict[str, object]:
        """草稿行（决策字段全量可见——双人可见口径）。"""
        return {
            "uid": record.uid,
            "conversation_uid": record.conversation_uid,
            "action_type": record.action_type,
            "status": record.status,
            "draft_content": dict(record.draft_content),
            "current_snapshot": dict(record.current_snapshot),
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "decided_by": record.decided_by,
            "decided_at": record.decided_at.isoformat() if record.decided_at else None,
            "reject_reason": record.reject_reason,
        }

    def _audit(self, action_type: str, target: str, actor: str, source: str,
               detail: Mapping[str, object]) -> None:
        """审计追加（哈希链；仅记 who/when/what，不记内容明文）。"""
        self.audit_log.append(
            {"action_type": action_type, "target": target, **detail},
            actor=actor, source=source, now=self._clock(),
        )
