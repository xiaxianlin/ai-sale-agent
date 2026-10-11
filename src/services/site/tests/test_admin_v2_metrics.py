# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-006 管理后台 V2·指标口径/预聚合/漏斗/钻取用例（SR-SITE-006 §6.1-6.3/§9；TDD 阶段①②）。

覆盖：五指标口径（UV consent 过滤、留资率=留资÷会话、挽回率=挽回÷弃购、邮件状态映射）、
预聚合幂等（同日重算覆盖写）、空数据日补零、漏斗四层与层间转化率、
指标→明细下钻一致性、邮件回溯重算（backfill）、与 FR-SITE-004 统计口径双读一致
（同一事件源，重点验收项）、挽回归因双条件。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from services.site.core.admin_v2.metrics import (
    DealFact,
    DrillUnknown,
    MailFact,
    SessionFact,
    SiteMetrics,
    SiteEventSource,
    VisitorFact,
)
from services.site.core.mailing.sequences import MailStore, SequenceEngine
from services.site.core.mailing.tracking import DeliveryTracker, SuppressionList, TrackEvent

from .fakes import MutableClock, T0

D1 = date(2026, 10, 11)


class FakeSiteSource:
    """SiteEventSource 协议的内存实现（demo 注入形态；生产为 PG 查询形态）。"""

    def __init__(self) -> None:
        self.visitor_rows: list[VisitorFact] = []
        self.session_facts: list[SessionFact] = []
        self.mail_rows: list[MailFact] = []
        self.visitor_customer: dict[str, int] = {}
        self.deals: dict[int, list[DealFact]] = {}
        self.mail_probe_ok: set[int] = set()  # delivered_cart_recover_between 命中的客户
        self.drill_data: dict[str, list[dict]] = {}

    # —— 协议实现 ——
    def visitor_events(self, day: date) -> list[VisitorFact]:
        return [v for v in self.visitor_rows if v.created_at.date() == day]

    def sessions(self, day: date) -> list[SessionFact]:
        return [s for s in self.session_facts if s.created_at.date() == day]

    def mail_jobs(self, day: date) -> list[MailFact]:
        return [m for m in self.mail_rows if m.sent_at.date() == day]

    def customer_by_visitor(self, visitor_uid: str) -> int | None:
        return self.visitor_customer.get(visitor_uid)

    def deals_of(self, customer_id: int) -> list[DealFact]:
        return self.deals.get(customer_id, [])

    def delivered_cart_recover_between(self, customer_id: int, start: datetime, end: datetime) -> bool:
        return customer_id in self.mail_probe_ok

    def drill_rows(self, metric: str, day: date) -> list[dict]:
        return list(self.drill_data.get(metric, []))


def make_metrics(source: FakeSiteSource | None = None) -> tuple[SiteMetrics, FakeSiteSource]:
    """构造 SiteMetrics（预聚合窗口/归因窗口用小值便于测试）。"""
    clock = MutableClock()
    src = source or FakeSiteSource()
    return SiteMetrics(src, clock=clock.now, attribution_window_days=7, recalc_window_days=14), src


def day_at(day: date, hour: int = 10) -> datetime:
    """某日某点（UTC）。"""
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


class TestMetricDefinitions:
    """指标口径（唯一权威口径，SR §6.1）。"""

    def test_uv_consent_filter(self) -> None:
        """UV 口径：仅 consent=true 计数——3 访客 1 人未同意 → UV=2（FR-SITE-007）。"""
        m, src = make_metrics()
        for i, consent in enumerate([True, True, False]):
            src.visitor_rows.append(VisitorFact(visitor_uid=f"v{i}", event_type="page_view",
                                            referrer="direct", consent=consent,
                                            created_at=day_at(D1, 10 + i)))
        m.aggregate_day(D1)
        assert m.overview(D1, D1)["visitors"] == 2

    def test_lead_rate_definition(self) -> None:
        """留资率 = 留资数 ÷ 会话数：2 会话 1 留资 → 0.5（任务口径）。"""
        m, src = make_metrics()
        src.visitor_rows.append(VisitorFact("v1", "lead", "direct", True, day_at(D1, 10)))
        src.session_facts.append(SessionFact(1, day_at(D1, 10), customer_id=7))
        src.session_facts.append(SessionFact(2, day_at(D1, 11), customer_id=8))
        m.aggregate_day(D1)
        assert m.overview(D1, D1)["lead_rate"] == 0.5

    def test_lead_rate_zero_sessions_null(self) -> None:
        """分母 0 → None（前端显示「—」，不显示 0%；SR §6.1）。"""
        m, src = make_metrics()
        src.visitor_rows.append(VisitorFact("v1", "lead", "direct", True, day_at(D1, 10)))
        m.aggregate_day(D1)
        assert m.overview(D1, D1)["lead_rate"] is None

    def test_recover_rate_definition(self) -> None:
        """挽回率 = 挽回成交数 ÷ 弃购数：2 弃购 1 挽回 → 0.5（任务口径）。"""
        m, src = make_metrics()
        src.visitor_customer = {"v1": 7, "v2": 8}
        src.mail_probe_ok = {7}
        for uid, hour in (("v1", 10), ("v2", 11)):
            src.visitor_rows.append(VisitorFact(uid, "abandon", "direct", True, day_at(D1, hour)))
        ab_at = day_at(D1, 10)
        src.deals[7] = [DealFact(deal_at=ab_at + timedelta(days=2), amount_usd=990.0)]
        m.aggregate_day(D1)
        ov = m.overview(D1, D1)
        assert ov["abandons"] == 2 and ov["recovered"] == 1 and ov["recover_rate"] == 0.5

    def test_mail_status_map(self) -> None:
        """邮件状态映射（与 FR-SITE-004 同源口径）：10 封 → sent=7、delivered=5、opened=1、failed=2。

        sent = status∈{delivered,opened,failed}（已离开队列，failed 计入分母单列失败率）；
        delivered = ∈{delivered,opened}；queued/cancelled 不入任何分子分母（SR §6.1④）。
        """
        m, src = make_metrics()
        statuses = ["queued", "queued", "cancelled", "delivered", "delivered",
                    "delivered", "delivered", "opened", "failed", "failed"]
        for i, st in enumerate(statuses):
            src.mail_rows.append(MailFact(job_type="cart_recover", status=st, recipient=f"u{i}@x.com",
                                     sent_at=day_at(D1, 9)))
        m.aggregate_day(D1)
        row = m.mail_effect(D1, D1)["by_type"]["cart_recover"]
        assert (row["sent"], row["delivered"], row["opened"], row["failed"]) == (7, 5, 1, 2)
        assert row["delivery_rate"] == 5 / 7


class TestAggregation:
    """预聚合：幂等 / 补零 / 回溯重算。"""

    def test_aggregate_idempotent(self) -> None:
        """同日重算两次：各渠道行值不变（覆盖写 + 先清零，ON CONFLICT 语义）。"""
        m, src = make_metrics()
        src.visitor_rows.append(VisitorFact("v1", "page_view", "referral", True, day_at(D1, 10)))
        first = m.aggregate_day(D1)
        second = m.aggregate_day(D1)
        assert first == second

    def test_empty_day_zero_fill(self) -> None:
        """空数据日补零：overview 全 0、率 None；邮件三序列行齐备为 0（主管永远有数）。"""
        m, src = make_metrics()
        m.aggregate_day(D1)
        ov = m.overview(D1, D1)
        assert ov["visitors"] == 0 and ov["sessions"] == 0 and ov["orders"] == 0
        assert ov["lead_rate"] is None and ov["recover_rate"] is None
        row = m.mail_effect(D1, D1)["by_type"]["festival"]
        assert row["sent"] == 0 and row["delivery_rate"] is None

    def test_mail_backfill_recompute(self) -> None:
        """送达/打开滞后回填：D1 发 4 封 delivered，D3 其中 2 封 opened →
        回溯窗口内重算后 D1 行 opened=2、打开率 0.5（SR §6.1③）。"""
        m, src = make_metrics()
        for i in range(4):
            src.mail_rows.append(MailFact("cart_recover", "delivered", f"u{i}@x.com", day_at(D1, 9)))
        m.aggregate_day(D1)
        assert m.mail_effect(D1, D1)["by_type"]["cart_recover"]["opened"] == 0
        # D3：2 封打开回执回填（状态单调 delivered → opened）
        for i, f in enumerate(src.mail_rows):
            if i < 2:
                src.mail_rows[i] = MailFact(f.job_type, "opened", f.recipient, f.sent_at)
        m.aggregate_day(D1 + timedelta(days=2))
        row = m.mail_effect(D1, D1)["by_type"]["cart_recover"]
        assert row["opened"] == 2 and row["open_rate"] == 0.5
        assert m.mail_effect(D1, D1)["mail_backfill_until"] < D1 + timedelta(days=2)


class TestFunnelAndDrill:
    """漏斗视图与指标下钻。"""

    def _seed_funnel(self, src: FakeSiteSource) -> None:
        """访问 10 → 会话 4 → 留资 2 → 成交 1。"""
        for i in range(10):
            src.visitor_rows.append(VisitorFact(f"v{i}", "page_view", "direct", True, day_at(D1, 8)))
        for i in range(4):
            src.session_facts.append(SessionFact(i + 1, day_at(D1, 9), customer_id=100 + i))
        for i in range(2):
            src.visitor_rows.append(VisitorFact(f"v{i}", "lead", "direct", True, day_at(D1, 10)))
        src.visitor_customer = {"v0": 100}
        src.deals[100] = [DealFact(deal_at=day_at(D1, 12), amount_usd=500.0)]
        src.mail_probe_ok = {100}

    def test_funnel_layers_and_rates(self) -> None:
        """漏斗四层 访问→会话→留资→成交 与层间转化率（SR §6.3 funnel；读路径只扫预聚合）。"""
        m, src = make_metrics()
        self._seed_funnel(src)
        m.aggregate_day(D1)
        f = m.funnel(D1, D1)
        assert [v for _, v in f["layers"]] == [10, 4, 2, 1]
        assert f["rates"]["会话率"] == 0.4 and f["rates"]["留资率"] == 0.5 and f["rates"]["成交率"] == 0.5

    def test_drill_consistent_with_overview(self) -> None:
        """钻取一致性：看板 UV 与同日下钻明细 total 相等（看板值=明细聚合值，验收口径）。"""
        m, src = make_metrics()
        for i in range(5):
            src.visitor_rows.append(VisitorFact(f"v{i}", "page_view", "direct", True, day_at(D1, 9)))
        src.drill_data["visitors"] = [{"visitor_uid": f"v{i}", "channel": "direct"} for i in range(5)]
        m.aggregate_day(D1)
        assert m.overview(D1, D1)["visitors"] == 5
        out = m.drill("visitors", D1)
        assert out["total"] == 5 and out["columns"]

class TestCrossModuleConsistency:
    """与 FR-SITE-004 统计口径一致性（同一事件源双读一致，重点验收）。"""

    @staticmethod
    def store_backed_source() -> tuple[FakeSiteSource, MailStore, SequenceEngine, DeliveryTracker]:
        """以 MailStore 为唯一事件源：admin 侧 source 即时映射，SITE-004 侧 tracker 直读。"""
        clock = MutableClock()
        store = MailStore()
        engine = SequenceEngine(store, clock=clock.now)
        tracker = DeliveryTracker(store, engine, SuppressionList(), clock=clock.now)
        src = FakeSiteSource()

        def sync_mail_jobs(day: date) -> list[MailFact]:
            facts = []
            for j in store.jobs.values():
                sent_at = j.sent_at or j.schedule_at
                if sent_at.date() == day and j.status != "queued":
                    facts.append(MailFact(j.seq_type, j.status, j.recipient, sent_at))
            return facts

        src.mail_jobs = sync_mail_jobs  # 同一 store 双读：admin 只读映射，不改状态
        return src, store, engine, tracker

    def test_same_event_source_double_read(self) -> None:
        """双读一致：admin mail_effect 的 sent/delivered/送达率 == tracker.stats 同期口径。"""
        src, store, engine, tracker = self.store_backed_source()
        m, _ = make_metrics(src)
        res = engine.enroll("cart_recover", 1, "a@x.com", {"dedup_key": "k1"}, triggered_at=T0)
        for step in range(1, 4):
            j = store.add_job(sequence_id=res.sequence.seq_id, seq_type="cart_recover", step_no=step,
                              template_key="cart_recover_remind", recipient="a@x.com",
                              schedule_at=T0 + timedelta(hours=step))
            tracker.on_event(TrackEvent(kind="delivered", job_id=j.job_id))
        j4 = store.add_job(sequence_id=res.sequence.seq_id, seq_type="cart_recover", step_no=4,
                           template_key="x", recipient="a@x.com", schedule_at=T0 + timedelta(hours=4))
        j4.status = "failed"  # 一封硬退
        m.aggregate_day(D1)
        admin = m.mail_effect(D1, D1)["by_type"]["cart_recover"]
        t = tracker.stats()
        assert (admin["sent"], admin["delivered"]) == (t["sent"], t["delivered"]) == (4, 3)
        assert admin["delivery_rate"] == t["delivery_rate"] == 0.75

    def test_attribution_requires_mail_and_deal(self) -> None:
        """挽回归因双条件：仅成交未收到送达挽回邮件 → 不计 recovered（SR §6.2③）。"""
        m, src = make_metrics()
        src.visitor_customer = {"v1": 7}
        src.mail_probe_ok = set()  # 无送达挽回邮件
        src.visitor_rows.append(VisitorFact("v1", "abandon", "direct", True, day_at(D1, 10)))
        src.deals[7] = [DealFact(deal_at=day_at(D1, 11), amount_usd=100.0)]
        m.aggregate_day(D1)
        assert m.overview(D1, D1)["recovered"] == 0
