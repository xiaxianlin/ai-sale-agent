# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""隐私合规（FR-SITE-007，SR-SITE-007 §6.1~§6.4；06-安全与合规设计 §9）。

- :class:`ConsentManager` —— consent 三态（granted/necessary/denied，§2.1 三态模型）：
  落证（仅追加，GDPR 举证）、10s 同状态去重幂等、门控出口（行为/邀聊仅 granted 放行，
  necessary 与 denied 数据处理等价）；
- :class:`DsrService` —— DSR 工单（GDPR Art.12(3)/15/17）：邮箱定位链
  （customer.channels[site] → visitor_uid → conversation）→ 跨表删除清单执行（计数审计）/
  JSON 导出打包；execute 幂等（completed 返回首次 result）；30 天时限跟踪与临期/超期告警分级。

向量库无关性：访客行为/画像/会话数据从不上向量（Milvus 仅存知识块，FR-KB-014），
DSR 删除不涉及向量库操作（本 docstring 即 §6.4 无关性说明的代码落点）。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol
from dataclasses import dataclass, field, replace
from typing import Any

from services.site.core.errors import SiteServiceError

#: consent 三态（SR-SITE-007 §2.1；necessary/denied 数据处理等价）
CONSENT_STATUSES: tuple[str, ...] = ("granted", "necessary", "denied")
#: 同状态去重窗（POST /site/consent 幂等）
CONSENT_DEDUPE_WINDOW_S: float = 10.0
#: DSR 处理时限（GDPR Art.12(3) 默认 30 天；privacy.dsr.deadline_days）
DSR_DEADLINE_DAYS_DEFAULT: int = 30
#: 导出包签名链接有效期（privacy.dsr.export_ttl_days）
DSR_EXPORT_TTL_DAYS_DEFAULT: int = 7
DAY_S: float = 86400.0
#: 临期告警阈值（剩 ≤3 天 mid）
DUE_SOON_DAYS: int = 3


class PrivacyError(SiteServiceError):
    """隐私域业务错误（40001/40401）。"""


# --------------------------------------------------------------------------- consent 三态


@dataclass(frozen=True)
class ConsentRecord:
    """consent 落证记录（site_consent_log 行，仅追加禁 UPDATE/DELETE）。"""

    visitor_uid: str
    status: str
    policy_ver: str
    sdk_version: str
    region: str
    ts: float
    recorded: bool = True  # False = 命中去重窗，未重复落证


class ConsentStore(Protocol):
    """consent 存储协议（生产 = PG site_consent_log，仅追加）。"""

    def append(self, record: ConsentRecord) -> None: ...

    def latest(self, uid: str) -> ConsentRecord | None: ...

    def history(self, uid: str) -> list[ConsentRecord]: ...

    def delete_uids(self, uids: tuple[str, ...] | list[str]) -> int: ...


class MemoryConsentStore:
    """内存 consent 存储（demo）。"""

    def __init__(self) -> None:
        self._rows: list[ConsentRecord] = []

    def append(self, record: ConsentRecord) -> None:
        self._rows.append(record)

    def latest(self, uid: str) -> ConsentRecord | None:
        found = [r for r in self._rows if r.visitor_uid == uid]
        return found[-1] if found else None

    def history(self, uid: str) -> list[ConsentRecord]:
        return [r for r in self._rows if r.visitor_uid == uid]

    def history_all(self) -> list[ConsentRecord]:
        return list(self._rows)

    def delete_uids(self, uids: tuple[str, ...] | list[str]) -> int:
        uid_set = set(uids)
        before = len(self._rows)
        self._rows = [r for r in self._rows if r.visitor_uid not in uid_set]
        return before - len(self._rows)


class PrivacyAudit(Protocol):
    """审计协议（生产 = audit_log，NFR-SEC-03）。"""

    def append(self, event: str, **fields: Any) -> None: ...


class ConsentManager:
    """consent 三态管理（SR-SITE-007 §6.1/§6.2）：落证 + 门控出口。

    作为 :class:`~services.site.core.tracking.service.ConsentGate` 与邀聊门控的实现方，
    向 FR-SITE-002/003 输出统一的放行判定（仅 granted 放行）。
    """

    def __init__(
        self,
        store: ConsentStore,
        audit: PrivacyAudit | None = None,
        *,
        policy_ver: str = "v1",
        dedupe_window_s: float = CONSENT_DEDUPE_WINDOW_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._audit = audit
        self._policy_ver = policy_ver
        self._window = dedupe_window_s
        self._clock = clock

    def set_consent(
        self,
        visitor_uid: str,
        status: str,
        *,
        sdk_version: str = "",
        region: str = "",
        policy_ver: str | None = None,
        now: float | None = None,
    ) -> ConsentRecord:
        """三态落证：非法状态 40001；去重窗内同状态幂等（recorded=False）。"""
        if status not in CONSENT_STATUSES:
            raise PrivacyError(40001, f"consent 状态非法：{status!r}（三态 {CONSENT_STATUSES}）")
        timestamp = self._clock() if now is None else now
        latest = self._store.latest(visitor_uid)
        if (
            latest is not None
            and latest.status == status
            and timestamp - latest.ts < self._window
        ):
            return replace(latest, recorded=False)  # 幂等：不重复落证
        record = ConsentRecord(
            visitor_uid=visitor_uid, status=status,
            policy_ver=policy_ver or self._policy_ver,
            sdk_version=sdk_version, region=region, ts=timestamp,
        )
        self._store.append(record)
        if self._audit is not None:
            self._audit.append(
                "consent_change", visitor_uid=visitor_uid, status=status,
                policy_ver=record.policy_ver,
            )
        return record

    def status_of(self, visitor_uid: str) -> str:
        """当前状态（无落证 = unknown，门控关闭）。"""
        latest = self._store.latest(visitor_uid)
        return latest.status if latest is not None else "unknown"

    def allows_tracking(self, visitor_uid: str) -> bool:
        """行为采集门（FR-SITE-002 服务端兜底）：仅 granted。"""
        return self.status_of(visitor_uid) == "granted"

    def allows_invite(self, visitor_uid: str) -> bool:
        """邀聊门（FR-SITE-003 短路条件）：仅 granted。"""
        return self.status_of(visitor_uid) == "granted"


# --------------------------------------------------------------------------- DSR 工单


@dataclass(frozen=True)
class DsrTicket:
    """DSR 工单（dsr_request 行的进程内形态）。"""

    ticket_id: str
    req_type: str  # delete | export
    email: str
    status: str  # pending | processing | completed | rejected
    created_at: float
    due_at: float
    result: dict[str, Any] = field(default_factory=dict)
    completed_at: float | None = None


@dataclass(frozen=True)
class DsrScope:
    """邮箱定位出的数据范围（跨表删除/导出的键集）。"""

    visitor_uids: tuple[str, ...] = ()
    conversation_ids: tuple[str, ...] = ()
    customer_ids: tuple[int, ...] = ()
    emails: tuple[str, ...] = ()  # 数据主体身份键（邮件任务删除/导出的键）


class MemoryDsrTicketStore:
    """内存工单存储（demo）。"""

    def __init__(self) -> None:
        self._rows: dict[str, DsrTicket] = {}
        self._seq = 1
        self._year = "2026"

    def add(self, email: str, req_type: str, created_at: float, due_at: float) -> DsrTicket:
        """建单：工单号 DSR-<年>-<四位序号>。"""
        ticket_id = f"DSR-{self._year}-{self._seq:04d}"
        self._seq += 1
        ticket = DsrTicket(ticket_id, req_type, email, "pending", created_at, due_at)
        self._rows[ticket_id] = ticket
        return ticket

    def get(self, ticket_id: str) -> DsrTicket | None:
        return self._rows.get(ticket_id)

    def update(self, ticket: DsrTicket) -> None:
        self._rows[ticket.ticket_id] = ticket

    def open_tickets(self) -> list[DsrTicket]:
        """未完结工单（时限跟踪口径）。"""
        return [t for t in self._rows.values() if t.status in ("pending", "verifying", "processing")]


class DsrDataPort(Protocol):
    """DSR 数据面协议（生产 = PG 跨表事务 + Redis 连带 + OSS 导出包）。"""

    def locate(self, email: str) -> DsrScope: ...

    def delete_scope(self, scope: DsrScope) -> dict[str, int]: ...

    def export_package(self, scope: DsrScope) -> dict[str, Any]: ...


class DsrAudit(Protocol):
    """审计协议。"""

    def append(self, event: str, **fields: Any) -> None: ...


class MemoryPrivacyData:
    """DSR 内存数据面（demo）：装配各内存存储实现定位/跨表删除/导出打包。"""

    def __init__(
        self,
        *,
        sessions: Any,
        events: Any,
        profiles: Any,
        conversations: Any,
        customers: Any,
        leads: Any,
        mail_jobs: Any,
        consents: Any,
        invites: Any | None = None,
    ) -> None:
        self._sessions = sessions
        self._events = events
        self._profiles = profiles
        self._conversations = conversations
        self._customers = customers
        self._leads = leads
        self._mail_jobs = mail_jobs
        self._consents = consents
        self._invites = invites

    def locate(self, email: str) -> DsrScope:
        """定位链：customer(channels.site external_id) → visitor_uid → conversation 集合。"""
        record = self._customers.find_by_email(email)
        if record is None:
            return DsrScope(emails=(email,))
        uids: list[str] = []
        for channel_id in record["channels"].values():
            if channel_id not in uids:
                uids.append(str(channel_id))
        conv_ids: list[str] = []
        for uid in uids:
            conv_ids.extend(self._conversations.conversation_ids_of(uid))
        return DsrScope(
            visitor_uids=tuple(uids),
            conversation_ids=tuple(conv_ids),
            customer_ids=(int(record["customer_id"]),),
            emails=(email,),
        )

    def delete_scope(self, scope: DsrScope) -> dict[str, int]:
        """跨表删除清单执行（生产为单事务；返回逐表计数供审计）。"""
        uids = scope.visitor_uids
        out: dict[str, int] = {}
        out["visitor_event"] = self._events.delete_uids(uids)
        out["site_session"] = self._sessions.delete_uids(uids)
        out["visitor_profile"] = self._profiles.delete_uids(uids)
        conv_counts = self._conversations.delete_by_visitors(uids)
        out["conversation"] = conv_counts.get("conversation", 0)
        out["message"] = conv_counts.get("message", 0)
        out["site_lead"] = self._leads.delete_by_visitors(uids)
        out["customer"] = self._customers.delete_ids(list(scope.customer_ids))
        out["mail_job"] = sum(self._mail_jobs.delete_by_email(e) for e in scope.emails)
        if self._invites is not None and hasattr(self._invites, "delete_by_visitors"):
            out["site_invite"] = self._invites.delete_by_visitors(uids)
        self._consents.delete_uids(uids)  # consent 记录连带（演示口径；生产按举证期策略保留）
        return out

    def export_package(self, scope: DsrScope) -> dict[str, Any]:
        """导出打包：JSON 覆盖全部留存数据段（GDPR Art.15 可携权）。"""
        uids = scope.visitor_uids
        uid_set = set(uids)
        return {
            "profile": [p.__dict__ for p in (self._profiles.load(u) for u in uids) if p is not None],
            "events": [e.__dict__ for u in uids for e in self._events.events_of(u)],
            "sessions": [s.__dict__ for s in self._sessions.all() if s.uid in uid_set],
            "conversations": [
                {"conversation_id": cid, "messages": self._conversations.messages_of(cid)}
                for cid in scope.conversation_ids
            ],
            "mails": [j for e in scope.emails for j in self._mail_jobs.jobs_of(e)],
            "consents": [r.__dict__ for r in self._consents.history_all() if r.visitor_uid in uid_set],
        }


class MemoryMailJobStore:
    """内存邮件任务存储（mail_job，demo；DSR 删除连带含打开追踪）。"""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._seq = 1

    def enqueue(self, job_type: str, recipient: str, *, at: float) -> dict[str, Any]:
        job = {"job_id": f"J-{self._seq:04d}", "job_type": job_type,
               "recipient": recipient, "at": at}
        self._seq += 1
        self._rows.append(job)
        return job

    def jobs_of(self, email: str) -> list[dict[str, Any]]:
        return [j for j in self._rows if j["recipient"] == email]

    def delete_by_email(self, email: str) -> int:
        before = len(self._rows)
        self._rows = [j for j in self._rows if j["recipient"] != email]
        return before - len(self._rows)

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class DsrService:
    """DSR 工单引擎（SR-SITE-007 §6.4）：定位 → 删除/导出 → 30 天时限跟踪。"""

    def __init__(
        self,
        data: DsrDataPort,
        tickets: MemoryDsrTicketStore,
        audit: DsrAudit | None = None,
        *,
        deadline_days: int = DSR_DEADLINE_DAYS_DEFAULT,
        export_ttl_days: int = DSR_EXPORT_TTL_DAYS_DEFAULT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._data = data
        self._tickets = tickets
        self._audit = audit
        self._deadline_days = deadline_days
        self._export_ttl_days = export_ttl_days
        self._clock = clock

    # ---- 工单生命周期 ----

    def open_ticket(self, email: str, req_type: str) -> DsrTicket:
        """建单：req_type ∈ {delete, export}、邮箱非空，否则 40001；due_at = created + 时限。"""
        if req_type not in ("delete", "export"):
            raise PrivacyError(40001, f"req_type 非法：{req_type!r}")
        if not (email or "").strip():
            raise PrivacyError(40001, "email 不能为空")
        now = self._clock()
        return self._tickets.add(email.strip(), req_type, now, now + self._deadline_days * DAY_S)

    def execute(self, ticket_id: str) -> dict[str, Any]:
        """执行工单：删除（跨表计数）或导出（JSON 打包）；幂等——completed 返回首次 result。"""
        ticket = self._get(ticket_id)
        if ticket.status == "completed":
            return dict(ticket.result)  # 幂等：不二次删除
        self._tickets.update(replace(ticket, status="processing"))
        scope = self._data.locate(ticket.email)
        if ticket.req_type == "delete":
            result: dict[str, Any] = {
                "deleted": self._data.delete_scope(scope),
                "merged_main": "kept" if len(scope.customer_ids) > 1 else "absent",
            }
        else:
            package = self._data.export_package(scope)
            result = {
                "export": {
                    "sections": sorted(package.keys()),
                    "visitor_uids": len(scope.visitor_uids),
                    "expires_in_days": self._export_ttl_days,
                    "expires_at": self._clock() + self._export_ttl_days * DAY_S,
                }
            }
        completed = replace(
            ticket, status="completed", result=result,
            completed_at=self._clock(),
        )
        self._tickets.update(completed)
        if self._audit is not None:
            self._audit.append("dsr_execute", ticket_id=ticket_id, req_type=ticket.req_type,
                               sections=sorted(result.keys()))
        return result

    def get(self, ticket_id: str) -> DsrTicket:
        """查询工单（未知 → 40401，本服务增补码）。"""
        return self._get(ticket_id)

    def locate(self, email: str) -> DsrScope:
        """定位链透出（验收走查用）。"""
        return self._data.locate(email)

    def deadline_report(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """30 天时限跟踪：未完结工单剩余天数与告警分级（mid=临期 ≤3 天，high=超期）。"""
        timestamp = self._clock() if now is None else now
        rows: list[dict[str, Any]] = []
        for ticket in sorted(self._tickets.open_tickets(), key=lambda t: t.due_at):
            days_left = int((ticket.due_at - timestamp) // DAY_S)
            rows.append({
                "ticket_id": ticket.ticket_id, "req_type": ticket.req_type,
                "email": ticket.email, "due_at": ticket.due_at,
                "days_left": days_left, "alert": alert_level(days_left),
            })
        return rows

    def _get(self, ticket_id: str) -> DsrTicket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise PrivacyError(40401, f"DSR 工单不存在：{ticket_id}")
        return ticket


def alert_level(days_left: int) -> str:
    """告警分级：超期 high（高优）/ 临期 mid（≤3 天）/ 正常 none（SR-SITE-007 §6.4 deadline_watch）。"""
    if days_left < 0:
        return "high"
    if days_left <= DUE_SOON_DAYS:
        return "mid"
    return "none"


__all__ = [
    "CONSENT_DEDUPE_WINDOW_S",
    "CONSENT_STATUSES",
    "ConsentManager",
    "ConsentRecord",
    "ConsentStore",
    "DSR_DEADLINE_DAYS_DEFAULT",
    "DSR_EXPORT_TTL_DAYS_DEFAULT",
    "DsrAudit",
    "DsrDataPort",
    "DsrScope",
    "DsrService",
    "DsrTicket",
    "MemoryConsentStore",
    "MemoryDsrTicketStore",
    "MemoryMailJobStore",
    "MemoryPrivacyData",
    "PrivacyAudit",
    "PrivacyError",
    "alert_level",
]
