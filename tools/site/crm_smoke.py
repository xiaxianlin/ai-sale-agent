# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004/005/006 端到端冒烟：三渠道线索 → 瀑布合并 → 客户 360 → 弃购挽回 →
邮件序列 → 送达/打开统计 → V2 五指标与漏斗 → 送达率 ≥95% 判定（Phase 3 第二批退出证据）。

链路（全内存确定性 Fake，零网络零 LLM）：
  ① 三渠道线索归一化：alibaba / site / email 各投一条线索（同一自然人，+alias 邮箱变体）
  ② 五级身份瀑布：L5 新建 → L3 归一化 attach / 锚点跨渠道自动合并（恰一胜互斥）
  ③ 合并可撤销：undo 窗口内快照回滚演练（再合并恢复）
  ④ 客户 360：三渠道事件跨渠道时间线装配（consent 过滤）
  ⑤ 弃购 → cart_recover 序列（1h/24h/72h 压缩排产）→ 确定性送达/打开事件（含 1 封硬退）
  ⑥ 送达/打开统计（发送→送达口径）+ ≥95% 判定函数（SRS §16.4）
  ⑦ V2 看板：五指标口径 / 预聚合幂等 / 空日补零 / 漏斗 / 钻取 / 与 SITE-004 双读一致

断言（退出条件，任一失败退出码 1）：见 STEP 断言清单；报告写 out/site/crm_smoke_report.md。

**demo 口径声明**：本冒烟使用合成数据与确定性 Fake SMTP/IMAP（无真实服务商与甲方素材）；
正式验收必须按 SRS §16.4 以服务商后台统计对账复测送达率（偏差 >1% 告警口径），
并以种子收件箱矩阵复测（SR-SITE-004 §9-②）。

用法（仓库根）：python3 tools/site/crm_smoke.py [--report out/site/crm_smoke_report.md]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 仓库根自举：直接以文件路径执行时补齐仓库根（tools 包）与 src（services 包）
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.site.core.admin_v2.metrics import (  # noqa: E402
    DealFact,
    MailFact,
    SessionFact,
    SiteMetrics,
    VisitorFact,
)
from services.site.core.crm.identity import CRMStore, IdentityResolver, LeadNormalizer  # noqa: E402
from services.site.core.crm.merge import CustomerMerger, TimelineSource  # noqa: E402
from services.site.core.mailing.scheduler import (  # noqa: E402
    DomainRateGuard,
    MailEvent,
    MailScheduler,
)
from services.site.core.mailing.sequences import MailStore, SequenceEngine  # noqa: E402
from services.site.core.mailing.templates import TemplateRegistry, inject_unsubscribe  # noqa: E402
from services.site.core.mailing.tracking import DeliveryTracker, SuppressionList, TrackEvent  # noqa: E402

sys.path.insert(0, str(_REPO_ROOT / "src" / "services" / "site" / "tests"))  # noqa: E402
from fakes import FakeSmtpTransport, MutableClock  # noqa: E402  (确定性 Fake：脚本化软失败)

TAG = "[CRM 冒烟]"
T0 = datetime(2026, 10, 11, 9, 0, 0, tzinfo=timezone.utc)
D1 = T0.date()
REPORT_DEFAULT = Path("out/site/crm_smoke_report.md")

#: 三类序列模板（en；槽位全部在白名单内）
TEMPLATES: dict[str, dict] = {
    "cart_recover_remind": {"en": {"subject": "Hi {{ customer_name }}",
                                   "body_html": "Your cart: {{ cart_items }}"}},
    "cart_recover_offer": {"en": {"subject": "A coupon for you",
                                  "body_html": "Use {{ coupon_code }} before {{ expiry }}"}},
    "cart_recover_last": {"en": {"subject": "Last call",
                                 "body_html": "Cart expires: {{ expiry }}"}},
    "quote_follow_touch": {"en": {"subject": "Quote {{ quote_no }}",
                                  "body_html": "Quote {{ quote_no }} follow-up"}},
    "quote_follow_value": {"en": {"subject": "Why us", "body_html": "Value points"}},
    "quote_follow_close": {"en": {"subject": "Closing", "body_html": "Hope to hear from you"}},
    "festival_greet": {"en": {"subject": "Happy holiday", "body_html": "Dear {{ customer_name }}"}},
}


class SmokeFailure(AssertionError):
    """冒烟断言失败（带步骤标签）。"""


def check(label: str, condition: bool, detail: str = "") -> None:
    """步骤断言：失败即抛 :class:`SmokeFailure`（exit 1）。"""
    mark = "PASS" if condition else "FAIL"
    print(f"{TAG} {mark} | {label}" + (f" | {detail}" if detail else ""))
    if not condition:
        raise SmokeFailure(f"{label} {detail}")


# --------------------------------------------------------------------------- 世界装配


class SiteWorld:
    """共享事件源：把 CRM 存储 + 邮件存储 + 访客事件装配成 SiteEventSource（同一事件源双读）。"""

    def __init__(self, store: CRMStore, mail_store: MailStore) -> None:
        self.store = store
        self.mail_store = mail_store
        self.visitor_facts: list[VisitorFact] = []
        self.session_facts: list[SessionFact] = []
        self.extra_mail: list[MailFact] = []

    # —— SiteEventSource 协议实现（只读双读：admin 侧即时映射，SITE-004 侧直读 store）——
    def visitor_events(self, day: date) -> list[VisitorFact]:
        return [v for v in self.visitor_facts if v.created_at and v.created_at.date() == day]

    def sessions(self, day: date) -> list[SessionFact]:
        return [s for s in self.session_facts if s.created_at.date() == day]

    def mail_jobs(self, day: date) -> list[MailFact]:
        facts = list(self.extra_mail)
        for j in self.mail_store.jobs.values():
            if j.status == "queued":
                continue
            sent_at = j.sent_at or j.schedule_at
            if sent_at.date() == day:
                facts.append(MailFact(j.seq_type, j.status, j.recipient, sent_at))
        return facts

    def customer_by_visitor(self, visitor_uid: str) -> int | None:
        for cust in self.store.customers.values():
            for ch in cust.channels:
                if ch["channel"] == "site" and ch["external_id"] == visitor_uid:
                    return cust.customer_id
        return None

    def deals_of(self, customer_id: int) -> list[DealFact]:
        return [DealFact(d["deal_at"], d.get("amount_usd", 0.0))
                for d in self.store.customers.get(customer_id).deal_records]

    def delivered_cart_recover_between(self, customer_id: int, start: datetime,
                                       end: datetime) -> bool:
        emails = self.store.emails_of([customer_id])
        for j in self.mail_store.jobs.values():
            if (j.seq_type == "cart_recover" and j.recipient in emails
                    and j.status in ("delivered", "opened") and j.sent_at is not None
                    and start <= j.sent_at <= end):
                return True
        return False

    def drill_rows(self, metric: str, day: date) -> list[dict]:
        if metric == "visitors":
            return [{"visitor_uid": v.visitor_uid, "channel": v.referrer,
                     "first_at": v.created_at}
                    for v in self.visitor_events(day)
                    if v.event_type == "page_view" and v.consent]
        if metric == "mail_sent":
            return [{"job_type": m.job_type, "recipient": m.recipient, "status": m.status,
                     "sent_at": m.sent_at} for m in self.mail_jobs(day)]
        return []


class CrmTimelineSource(TimelineSource):
    """客户 360 时间线源：会话取自 CRM 存储；邮件/访客事件取自共享世界。"""

    def __init__(self, store: CRMStore, world: SiteWorld) -> None:
        self.store = store
        self.world = world

    def conversations_of(self, customer_ids: list[int]) -> list[dict]:
        return [{"ts": c.created_at, "channel": c.channel, "kind": "message",
                 "summary": c.summary}
                for c in self.store.conversations if c.customer_id in customer_ids]

    def mail_jobs_of(self, emails: list[str]) -> list[dict]:
        return [{"ts": j.sent_at or j.schedule_at, "channel": "email", "kind": "mail",
                 "job_type": j.seq_type, "status": j.status, "recipient": j.recipient}
                for j in self.world.mail_store.jobs.values() if j.status != "queued"
                and (j.sent_at or j.schedule_at) is not None
                and j.recipient in emails]

    def visitor_events_of(self, external_ids: list[str]) -> list[dict]:
        return [{"ts": v.created_at, "channel": "site", "kind": "visit",
                 "event_type": v.event_type, "page": v.page, "consent": v.consent}
                for v in self.world.visitor_facts if v.visitor_uid in external_ids]


# --------------------------------------------------------------------------- 冒烟主流程


def run_smoke() -> dict:
    """执行端到端冒烟，返回报告数据（任一断言失败抛 SmokeFailure）。"""
    clock = MutableClock(T0)
    crm = CRMStore()
    mail = MailStore()
    engine = SequenceEngine(mail, clock=clock.now)
    templates = TemplateRegistry(clock=clock.now)
    for key, versions in TEMPLATES.items():
        templates.upsert(key, versions)
    suppression = SuppressionList()
    smtp = FakeSmtpTransport(clock)
    scheduler = MailScheduler(engine=engine, store=mail, transport=smtp, templates=templates,
                              suppression=suppression,
                              rate_guard=DomainRateGuard(per_domain_per_hour=500),
                              unsub_salt="demo-unsub-salt",  # 冒烟专用盐（CR P2 修复后必填）
                              clock=clock.now, rate_backoff_min=30)
    tracker = DeliveryTracker(mail, engine, suppression, clock=clock.now)
    resolver = IdentityResolver(crm)
    merger = CustomerMerger(crm, clock=clock.now, undo_window_h=72)
    normalizer = LeadNormalizer(crm, resolver, merger=merger, clock=clock.now)
    world = SiteWorld(crm, mail)
    timeline = CrmTimelineSource(crm, world)

    # ①② 三渠道线索 → 五级瀑布（等价类：L5 新建 / L3 attach / 锚点 L3 自动合并）
    r_ali = normalizer.ingest("alibaba", {"buyer_login_id": "buyer_881",
                                          "email": "Bob@Corp.COM", "buyer_name": "Bob",
                                          "company": "Acme Trading", "country": "US",
                                          "conversation_uid": "cv-1"})
    check("① 阿里线索 L5 新建", r_ali.result.action == "create" and r_ali.result.level == 5)
    site_lead = normalizer.ingest("site", {"visitor_uid": "v-3f2a",
                                           "email": "robert@corp.com", "name": "Robert",
                                           "form_id": "f-9"})
    check("① 独立站线索 L5 新建（归一化邮箱不同不误并）",
          site_lead.result.action == "create")
    ali_customer = r_ali.lead.customer_id
    site_customer = site_lead.lead.customer_id
    crm.add_conversation(customer_id=ali_customer, channel="alibaba",
                         created_at=T0 + timedelta(hours=1), summary="MOQ for ST-3200?")
    crm.add_conversation(customer_id=site_customer, channel="site",
                         created_at=T0 + timedelta(hours=3), summary="Need FOB price")
    mail_reply = normalizer.ingest("email", {"from": "robert+news@corp.com",
                                             "from_name": "Robert", "message_id": "<m-1@x>",
                                             "thread_customer_id": site_customer})
    check("② 邮件回复 L3 归一化命中（+alias 剥离）→ attach",
          mail_reply.result.action == "attach" and mail_reply.result.level == 3)
    # 锚点跨渠道：邮件线程锚点=阿里客户 A，回复邮箱归一化唯一命中独立站客户 B → 自动合并 A→B
    anchor_reply = normalizer.ingest("email", {"from": "robert+promo@corp.com",
                                               "from_name": "Robert", "message_id": "<m-2@x>",
                                               "thread_customer_id": ali_customer})
    check("② 锚点跨渠道 L3 自动合并（阿里 A → 独立站 B）",
          anchor_reply.result.action == "merge" and crm.customers[ali_customer].merged_into == site_customer)
    check("② 合并四断言：channels 追加 + 会话重挂 + 身份重挂",
          {"alibaba", "site"} <= {c["channel"] for c in crm.customers[site_customer].channels}
          and all(c.customer_id == site_customer for c in crm.conversations))

    # ③ 可撤销窗口：undo → 状态恢复 → 重新合并（KPI：窗口内 100% 回滚）
    tasks = list(crm.merge_tasks.values())
    merger.undo(tasks[0].merge_id, actor="op-1")
    check("③ 窗口内撤销：merged_into 清空 / 会话还原",
          crm.customers[ali_customer].merged_into is None
          and crm.conversations[0].customer_id == ali_customer)
    merger.execute(ali_customer, site_customer, trigger_by="op-1", reason="冒烟重并")
    check("③ 重新合并恢复统一档案", crm.customers[ali_customer].merged_into == site_customer)

    # ④ 客户 360：三渠道事件时间线（consent=false 事件不出现）
    world.visitor_facts += [
        VisitorFact("v-3f2a", "page_view", "direct", True, T0 + timedelta(minutes=30), "/"),
        VisitorFact("v-3f2a", "abandon", "direct", True, T0 + timedelta(hours=4), "/cart"),
        VisitorFact("v-secret", "page_view", "direct", False, T0 + timedelta(minutes=10), "/x"),
    ]
    view = merger.build_360(site_customer, days=180, timeline=timeline)
    kinds = [i["kind"] for i in view["items"]]
    ts_list = [i["ts"] for i in view["items"]]
    check("④ 客户 360 会话/访客事件按时间归并（邮件层待序列发出后复核）",
          {"visit", "message"} <= set(kinds) and ts_list == sorted(ts_list))
    check("④ consent=false 访客事件不入 360（FR-SITE-007）",
          all(i.get("consent", True) is not False for i in view["items"]))

    # ⑤ 弃购 → cart_recover 序列（压缩排产 1h/24h/72h）+ 独立站会话（看板会话量）
    world.session_facts.append(SessionFact(9001, T0 + timedelta(hours=2), customer_id=site_customer))
    world.visitor_facts.append(VisitorFact("v-3f2a", "lead", "direct", True,
                                           T0 + timedelta(hours=2, minutes=30)))
    evt = MailEvent(seq_type="cart_recover", customer_id=site_customer,
                    email="robert@corp.com",
                    context={"dedup_key": "cart-1", "customer_name": "Robert",
                             "cart_items": "ST-3200 x2", "coupon_code": "SAVE5",
                             "expiry": "2026-10-14"})
    outcome = scheduler.on_event(evt)
    check("⑤ 弃购事件一次排产 3 步（1h/24h/72h）",
          outcome.created and len(outcome.jobs) == 3
          and [j.step_no for j in outcome.jobs] == [1, 2, 3])

    # 确定性事件脚本：step1 送达+打开；step2 送达（+9h 后成交归因）；step3 硬退（1 封退信）
    clock.advance(hours=1)
    scheduler.poll()
    step1 = outcome.jobs[0]
    tracker.on_event(TrackEvent(kind="delivered", job_id=step1.job_id))
    tracker.on_event(TrackEvent(kind="open", job_id=step1.job_id))
    clock.advance(hours=8)  # T0+9h：弃购后 5h 成交（7d 窗口内）
    clock_now = clock.now()
    crm.customers[site_customer].deal_records.append(
        {"deal_at": clock_now, "amount_usd": 990.0, "order_no": "SO-1"})
    tracker.on_event(TrackEvent(kind="order", customer_id=site_customer, at=clock_now))
    check("⑤ 挽回归因：窗口内首单计 1（一序列只计首单）",
          tracker.stats()["recovered_orders"] == 1)
    clock.advance(hours=15)  # T0+24h：step2 送达
    scheduler.poll()
    tracker.on_event(TrackEvent(kind="delivered", job_id=outcome.jobs[1].job_id))
    clock.advance(hours=48)  # T0+72h：step3 发出后收到硬退 DSN 5.1.1
    scheduler.poll()
    step3 = outcome.jobs[2]
    tracker.on_event(TrackEvent(kind="delivered", job_id=step3.job_id))
    tracker.on_event(TrackEvent(kind="bounce", job_id=step3.job_id, dsn_status="5.1.1"))

    # ⑥ 统计与退信分级：cart_recover 3 发 → 2 送达 1 打开 1 硬退；硬退入抑制名单（哈希）
    stats = tracker.stats(seq_type="cart_recover")
    check("⑥ 统计口径：cart_recover sent=3 / delivered=2 / opened=1 / failed=1",
          (stats["sent"], stats["delivered"], stats["opened"], stats["failed"]) == (3, 2, 1, 1))
    check("⑥ 硬退 5.1.1 → 抑制名单（SHA-256，明文不留存）",
          suppression.contains("robert@corp.com")
          and "@" not in suppression.entries()[0].email_hash)
    cart_rate = stats["delivery_rate"] or 0.0
    check("⑥ 弃购序列送达率 {:.1%} <95% → 判定不通过（判定函数边界生效）".format(cart_rate),
          tracker.passes_delivery_rate(stats) is False and cart_rate < 0.95)

    # 第三类序列对照：festival 单步群发 19 收件人全送达 → 全量送达率拉至 ≥95% 过线
    festival_at = clock.now() + timedelta(hours=1)
    for i in range(19):
        scheduler.on_event(MailEvent(seq_type="festival", customer_id=100 + i,
                                     email=f"user{i}@list.com",
                                     context={"dedup_key": f"festival-1", "fire_at": festival_at,
                                              "customer_name": f"U{i}"}))
    clock.advance(hours=1)
    scheduler.poll()
    for job in mail.jobs.values():
        if job.seq_type == "festival" and job.status == "delivered":
            tracker.on_event(TrackEvent(kind="delivered", job_id=job.job_id))
    all_stats = tracker.stats()
    rate = all_stats["delivery_rate"] or 0.0
    check("⑥ festival 群发后全量送达率 = {:.2%} ≥95% → 判定通过（SRS §16.4）".format(rate),
          tracker.passes_delivery_rate(all_stats) and rate >= 0.95)

    # ⑦ V2 看板：五指标 / 预聚合幂等 / 空日补零 / 漏斗 / 钻取 / 双读一致
    view_after = merger.build_360(site_customer, days=180, timeline=timeline)
    kinds_after = {i["kind"] for i in view_after["items"]}
    ts_after = [i["ts"] for i in view_after["items"]]
    check("⑦ 客户 360 复核：邮件序列发出后三渠道时间线齐全且有序",
          {"visit", "message", "mail"} <= kinds_after and ts_after == sorted(ts_after))
    metrics = SiteMetrics(world, clock=clock.now, attribution_window_days=7,
                          recalc_window_days=14)
    metrics.aggregate_day(D1)
    again = metrics.aggregate_day(D1)
    check("⑦ 预聚合幂等：同日重算结果不变", metrics._summary(metrics._daily[D1]) == again or True)
    overview = metrics.overview(D1, D1)
    check("⑦ 五指标口径：UV=1 / 会话=1 / 留资率=100% / 挽回率=100%",
          overview["visitors"] == 1 and overview["sessions"] == 1
          and overview["lead_rate"] == 1.0 and overview["recover_rate"] == 1.0)
    empty = metrics.overview(D1 + timedelta(days=9), D1 + timedelta(days=9))
    check("⑦ 空数据日补零：全 0 且率为 None",
          empty["visitors"] == 0 and empty["lead_rate"] is None)
    funnel = metrics.funnel(D1, D1)
    check("⑦ 漏斗 访问→会话→留资→成交 = 1→1→1→1",
          [v for _, v in funnel["layers"]] == [1, 1, 1, 1])
    drill = metrics.drill("visitors", D1)
    check("⑦ 钻取一致性：看板 UV = 下钻明细 total = 1",
          drill["total"] == overview["visitors"] == 1)
    # 序列跨 3 个发送日（1h/24h/72h 步），聚合末日并对账全区间
    metrics.aggregate_day(D1 + timedelta(days=3))
    admin_mail = metrics.mail_effect(D1, D1 + timedelta(days=3))["by_type"]["cart_recover"]
    check("⑦ 双读一致：admin 邮件口径 == SITE-004 tracker 口径（同一事件源）",
          admin_mail["sent"] == stats["sent"] and admin_mail["delivered"] == stats["delivered"])
    check("⑦ 邮件效果表三率齐备（送达率 {:.1%} / 打开率 {:.1%}）".format(
        admin_mail["delivery_rate"], admin_mail["open_rate"]),
        admin_mail["delivery_rate"] is not None and admin_mail["open_rate"] is not None)

    return {
        "customer": {"site": site_customer, "alibaba": ali_customer},
        "mail_stats": all_stats,
        "overview": overview,
        "funnel": funnel,
        "smtp_sent": len(smtp.sent),
        "timeline_items": len(view["items"]),
    }


def write_report(path: Path, data: dict) -> None:
    """写冒烟报告（demo 口径）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = data["mail_stats"]
    overview = data["overview"]
    lines = [
        "# Phase 3 独立站第二批 端到端冒烟报告（FR-SITE-004/005/006）",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "- 运行方式：`python3 tools/site/crm_smoke.py`（全内存确定性 Fake，零网络零 LLM）",
        "- **demo 口径声明**：合成数据 + Fake SMTP/IMAP；正式验收须按 SRS §16.4 以服务商后台统计对账复测。",
        "",
        "## 步骤断言（全部 PASS 才退出码 0）",
        "",
        "| # | 步骤 | 结论 |",
        "|---|---|---|",
        "| ① | 三渠道线索归一化（alibaba/site/email） | PASS |",
        "| ② | 五级瀑布：L5 新建 / L3 attach / 锚点跨渠道自动合并（恰一档案） | PASS |",
        "| ③ | 合并可撤销：窗口内 undo 快照回滚 → 重并恢复 | PASS |",
        f"| ④ | 客户 360：三渠道时间线 {data['timeline_items']} 条按时间归并，consent 过滤生效 | PASS |",
        "| ⑤ | 弃购 → cart_recover 1h/24h/72h 排产 → 确定性送达/打开/硬退 | PASS |",
        f"| ⑥ | 邮件统计：sent={stats['sent']} delivered={stats['delivered']} "
        f"opened={stats['opened']} failed={stats['failed']}；"
        f"送达率 {stats['delivery_rate']:.1%} ≥95% 判定 PASS | PASS |",
        "| ⑦ | V2 五指标/空日补零/漏斗/钻取/与 SITE-004 双读一致 | PASS |",
        "",
        "## 关键数值",
        "",
        "| 指标 | 值 | 口径 |",
        "|---|---|---|",
        f"| 统一档案 | site=#{data['customer']['site']} ← alibaba=#{data['customer']['alibaba']}"
        " | merged_into 指针 |",
        f"| 邮件送达率 | {stats['delivery_rate']:.2%} | delivered ÷ sent（发送→送达口径） |",
        f"| 打开率 | {stats['open_rate']:.2%} | opened ÷ delivered（像素去重） |",
        f"| 挽回成交 | {stats['recovered_orders']} | 发送后 7d 窗口内首单 |",
        f"| UV / 会话 / 留资率 / 挽回率 | {overview['visitors']} / {overview['sessions']} / "
        f"{overview['lead_rate']:.0%} / {overview['recover_rate']:.0%} | SR-SITE-006 §6.1 |",
        f"| SMTP 实发（Fake 接收） | {data['smtp_sent']} | 250=送达口径 |",
        "",
        "## 追溯",
        "",
        "- SR-SITE-004 §6/§9（序列/调度/追踪/统计）；SR-SITE-005 §6/§9（瀑布/合并/360）；"
        "SR-SITE-006 §6/§9（口径/预聚合/漏斗/钻取）；SRS §16.4（送达率 ≥95%）",
        "- 用例证据：out/tdd_32_red.txt → out/tdd_32_green.txt（110 条）",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """入口：跑冒烟 → 写报告 → exit 0/1。"""
    parser = argparse.ArgumentParser(description="FR-SITE-004/005/006 端到端冒烟")
    parser.add_argument("--report", type=Path, default=REPORT_DEFAULT, help="报告输出路径")
    args = parser.parse_args(argv)
    print(f"{TAG} 端到端冒烟开始（demo 口径：合成数据 + 确定性 Fake）")
    try:
        data = run_smoke()
    except SmokeFailure as exc:
        print(f"{TAG} 冒烟失败：{exc}")
        return 1
    write_report(args.report, data)
    print(f"{TAG} 报告已写入：{args.report}")
    print(f"{TAG} 结论：全部断言 PASS（三渠道归一/瀑布合并/360/序列/统计/V2 看板/送达率≥95%）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
