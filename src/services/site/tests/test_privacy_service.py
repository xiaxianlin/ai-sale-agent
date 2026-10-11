# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""隐私合规用例（FR-SITE-007 / SR-SITE-007 §9：consent 三态/DSR 删除导出/30 天时限）。

验收口径映射：SRS §16.4「访客数据隐私合规检查通过」（C2/C4/C5/C6/C7/C8 自检项）。
"""
from __future__ import annotations

import pytest

from services.site.core.conversation.service import (
    MemoryConversationRepo,
    MemoryCustomerWriter,
    MemoryInviteLogStore,
    MemoryLeadStore,
)
from services.site.core.privacy.service import (
    CONSENT_STATUSES,
    ConsentManager,
    ConsentRecord,
    DsrService,
    MemoryConsentStore,
    MemoryDsrTicketStore,
    MemoryMailJobStore,
    MemoryPrivacyData,
    PrivacyError,
    alert_level,
)
from services.site.core.session.service import MemorySessionStore, SiteSessionRecord
from services.site.core.tracking.service import (
    EventIngest,
    MemoryAuditTrail,
    MemoryEventStore,
    MemoryProfileStore,
)

from .conftest import FakeClock, StaticGate

CLOCK0 = 1_760_000_000.0
DAY = 86400.0
EMAIL = "buyer@x.com"


class World:
    """内存世界：装齐 DSR 视角的全部存储（app.py 装配的测试等价物）。"""

    def __init__(self) -> None:
        self.sessions = MemorySessionStore()
        self.events = MemoryEventStore()
        self.profiles = MemoryProfileStore()
        self.conversations = MemoryConversationRepo()
        self.customers = MemoryCustomerWriter()
        self.leads = MemoryLeadStore()
        self.mail_jobs = MemoryMailJobStore()
        self.consents = MemoryConsentStore()
        self.invites = MemoryInviteLogStore()
        self.audit = MemoryAuditTrail()

    def seed_visitor(self, uid: str = "v-1", email: str = EMAIL, with_conversation: bool = True) -> None:
        """造一个全链路访客：会话/事件/画像/会话消息/客户/线索/邮件任务/consent。"""
        self.sessions.save(SiteSessionRecord(
            uid=uid, tenant_id=1, sdk_key_id=1, lang="en",
            consent="granted", created_at=CLOCK0))
        ingest = EventIngest(self.events, StaticGate(True))
        ingest.ingest(uid, 1, True, [
            {"ev_id": f"{uid}-e1", "type": "page_view", "page": "/p/1", "ts": CLOCK0},
            {"ev_id": f"{uid}-e2", "type": "product_view", "page": "/p/1", "ts": CLOCK0 + 1,
             "payload": {"sku": "ST-3200"}},
        ])
        if with_conversation:
            conv = self.conversations.ensure_conversation(uid, "en")
            self.conversations.append(conv, "buyer", "buyer", "hello")
            self.conversations.append(conv, "ai", "ai", "hi [R1]")
        cid = self.customers.upsert(
            email, "+4915112345678", channel="site", external_id=uid,
            profile_tags=("interest:ST-3200",),
        )["customer_id"]
        self.leads.add({"lead_id": f"L-{uid}", "visitor_uid": uid, "customer_id": cid,
                        "email": email, "whatsapp": ""})
        self.mail_jobs.enqueue("quote_follow", email, at=CLOCK0 + DAY)
        self.consents.append(ConsentRecord(uid, "granted", "v1", "1.0.0", "", CLOCK0))


def make_consent(
    clock: FakeClock | None = None, **kw: object
) -> tuple[ConsentManager, MemoryConsentStore, MemoryAuditTrail]:
    """构造 consent 管理器。"""
    clock = clock or FakeClock()
    store = MemoryConsentStore()
    audit = MemoryAuditTrail()
    manager = ConsentManager(store, audit, clock=clock, **kw)  # type: ignore[arg-type]
    return manager, store, audit


def make_dsr(
    clock: FakeClock | None = None, world: World | None = None
) -> tuple[DsrService, World, MemoryAuditTrail]:
    """构造 DSR 服务（含已灌数据的内存世界）。"""
    clock = clock or FakeClock()
    world = world or World()
    tickets = MemoryDsrTicketStore()
    data = MemoryPrivacyData(
        sessions=world.sessions, events=world.events, profiles=world.profiles,
        conversations=world.conversations, customers=world.customers, leads=world.leads,
        mail_jobs=world.mail_jobs, consents=world.consents, invites=world.invites,
    )
    service = DsrService(data, tickets, world.audit, clock=clock)
    return service, world, world.audit


# --------------------------------------------------------------------------- consent 三态（等价类）


def test_three_states_accepted() -> None:
    """P1 三态设置：granted/necessary/denied 均可落证（等价类）。"""
    assert set(CONSENT_STATUSES) == {"granted", "necessary", "denied"}
    manager, store, _ = make_consent()
    for status in CONSENT_STATUSES:
        rec = manager.set_consent(f"v-{status}", status)
        assert rec.recorded is True and rec.status == status
    assert len(store.history_all()) == 3


def test_invalid_status_rejected_40001() -> None:
    """P2 非法状态 → 40001（异常类）。"""
    manager, _, _ = make_consent()
    with pytest.raises(PrivacyError) as ei:
        manager.set_consent("v-1", "maybe")
    assert ei.value.code == 40001


def test_denied_disables_tracking() -> None:
    """P3 denied → 行为采集禁用（C2 拒绝有效性，服务端门）。"""
    manager, _, _ = make_consent()
    manager.set_consent("v-1", "denied")
    assert manager.allows_tracking("v-1") is False


def test_necessary_equivalent_to_denied_for_processing() -> None:
    """P4 necessary 与 denied 数据处理等价：行为/邀聊均禁用（三态模型 §2.1）。"""
    manager, _, _ = make_consent()
    manager.set_consent("v-1", "necessary")
    assert manager.allows_tracking("v-1") is False
    assert manager.allows_invite("v-1") is False


def test_granted_enables_all() -> None:
    """P5 granted → 行为/邀聊启用（等价类）。"""
    manager, _, _ = make_consent()
    manager.set_consent("v-1", "granted")
    assert manager.allows_tracking("v-1") is True
    assert manager.allows_invite("v-1") is True


def test_unknown_initial_gate_closed() -> None:
    """P6 初始 unknown → 门控关闭（未同意前零采集，边界）。"""
    manager, _, _ = make_consent()
    assert manager.status_of("v-none") == "unknown"
    assert manager.allows_tracking("v-none") is False


def test_duplicate_report_deduped() -> None:
    """P7 去重幂等：10s 内同状态重复上报 recorded=False；窗外（11s）正常落证（并发/幂等+边界）。"""
    clock = FakeClock()
    manager, store, _ = make_consent(clock)
    first = manager.set_consent("v-1", "granted", now=clock())
    clock.advance(5)
    second = manager.set_consent("v-1", "granted")
    assert first.recorded is True and second.recorded is False
    assert len(store.history("v-1")) == 1
    clock.advance(6)  # 距上次落证 11s > 去重窗
    third = manager.set_consent("v-1", "granted")
    assert third.recorded is True and len(store.history("v-1")) == 2


def test_consent_audit_and_policy_version() -> None:
    """P8 落证审计与政策版本：uid/status 写审计；落证携带 policy_ver（NFR-SEC-03，等价类）。"""
    manager, store, audit = make_consent(policy_ver="v2")
    manager.set_consent("v-1", "denied")
    entry = audit.filter("consent_change")[-1]
    assert entry["visitor_uid"] == "v-1" and entry["status"] == "denied"
    assert store.history("v-1")[0].policy_ver == "v2"


# --------------------------------------------------------------------------- DSR（删除/导出/时限）


def test_open_ticket_due_in_30_days() -> None:
    """P9 工单时限与入参校验：due_at=创建+30 天；非法类型/空邮箱 → 40001（边界+异常）。"""
    clock = FakeClock()
    service, _, _ = make_dsr(clock)
    ticket = service.open_ticket(EMAIL, "delete")
    assert ticket.ticket_id.startswith("DSR-")
    assert ticket.due_at - ticket.created_at == pytest.approx(30 * DAY)
    assert ticket.status == "pending"
    with pytest.raises(PrivacyError) as ei:
        service.open_ticket(EMAIL, "anonymize")
    assert ei.value.code == 40001
    with pytest.raises(PrivacyError):
        service.open_ticket("", "delete")


def test_dsr_delete_across_tables() -> None:
    """P10 DSR 删除跨表清单执行：会话/事件/消息/会话容器/线索/邮件任务/客户 全链计数（等价类）。"""
    service, world, _ = make_dsr()
    world.seed_visitor("v-1")
    world.seed_visitor("v-2", email="other@x.com")  # 他人数据不误删
    ticket = service.open_ticket(EMAIL, "delete")
    result = service.execute(ticket.ticket_id)
    deleted = result["deleted"]
    assert deleted["visitor_event"] == 2
    assert deleted["site_session"] == 1
    assert deleted["message"] == 2
    assert deleted["conversation"] == 1
    assert deleted["site_lead"] == 1
    assert deleted["mail_job"] == 1
    assert deleted["customer"] == 1
    assert result["merged_main"] == "absent"  # 无跨渠道合并主档


def test_dsr_delete_zero_residue() -> None:
    """P11 删除后零残留（C6：各表 + 存储检索该邮箱/uid 零残留，等价类）。"""
    service, world, _ = make_dsr()
    world.seed_visitor("v-1")
    ticket = service.open_ticket(EMAIL, "delete")
    service.execute(ticket.ticket_id)
    assert world.events.events_of("v-1") == []
    assert world.sessions.get("v-1") is None
    assert world.customers.find_by_email(EMAIL) is None
    assert world.leads.leads_of("v-1") == []
    assert world.mail_jobs.jobs_of(EMAIL) == []
    assert world.conversations.conversation_ids_of("v-1") == []


def test_dsr_export_package_sections() -> None:
    """P12+P16b DSR 导出 JSON 打包：六段留存数据齐全；导出不删除数据（C7，等价类边界）。"""
    service, world, _ = make_dsr()
    world.seed_visitor("v-1")
    ticket = service.open_ticket(EMAIL, "export")
    result = service.execute(ticket.ticket_id)
    sections = set(result["export"]["sections"])
    assert {"profile", "events", "sessions", "conversations", "mails", "consents"} <= sections
    assert result["export"]["expires_in_days"] == 7
    assert world.events.events_of("v-1") != []  # 导出语义：仅打包不删除


def test_dsr_execute_idempotent() -> None:
    """P13 execute 幂等：completed 再执行返回首次 result、无二次删除（并发/幂等）。"""
    service, world, _ = make_dsr()
    world.seed_visitor("v-1")
    ticket = service.open_ticket(EMAIL, "delete")
    first = service.execute(ticket.ticket_id)
    second = service.execute(ticket.ticket_id)
    assert first == second
    assert world.events.events_of("v-1") == []  # 无异常二次删


def test_dsr_unknown_ticket_40401() -> None:
    """P14 未知工单 → 40401（异常类，增补码）。"""
    service, _, _ = make_dsr()
    with pytest.raises(PrivacyError) as ei:
        service.get("DSR-2099-9999")
    assert ei.value.code == 40401


def test_dsr_deadline_alerts() -> None:
    """P15 时限告警边界：剩 3 天 mid、超期 high、10 天 none（C8，边界）。"""
    clock = FakeClock()
    service, _, _ = make_dsr(clock)
    ticket = service.open_ticket(EMAIL, "delete")
    clock.advance(27 * DAY)
    rows = service.deadline_report()
    assert rows[0]["alert"] == "mid" and rows[0]["days_left"] == 3
    clock.advance(4 * DAY)  # 31 天：超期
    rows = service.deadline_report()
    assert rows[0]["alert"] == "high" and rows[0]["days_left"] < 0
    service.execute(ticket.ticket_id)  # 完成后不再出现在告警清单
    assert service.deadline_report() == []
    assert alert_level(10) == "none"


def test_dsr_email_locates_visitor_chain() -> None:
    """P16 定位链：邮箱 → customer(channels.site) → visitor_uid → 会话/事件（等价类）。"""
    service, world, _ = make_dsr()
    world.seed_visitor("v-1")
    scope = service.locate(EMAIL)
    assert scope.visitor_uids == ("v-1",)
    assert len(scope.conversation_ids) == 1
    assert scope.customer_ids
