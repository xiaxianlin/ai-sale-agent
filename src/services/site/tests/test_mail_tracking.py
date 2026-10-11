# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004 邮件自动化·状态追踪与统计用例（SR-SITE-004 §6.4/§9；TDD 阶段①②）。

覆盖：送达回执→状态推进、打开像素去重、退信分级（硬退移出名单 / 连续软退降级）、
退订与回复停止、统计口径（发送→送达）、送达率 ≥95% 判定边界（SRS §16.4）、
成交归因窗口。SMTP/IMAP 以确定性事件注入（Fake，webhook 形态同构）。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from services.site.core.mailing.sequences import MailStore, SequenceEngine
from services.site.core.mailing.tracking import DeliveryTracker, SuppressionList, TrackEvent

from .fakes import MutableClock, T0


def make_world():
    """构造（追踪器, 存储, 引擎, 名单, 时钟），预置一条已送达的弃购序列。"""
    clock = MutableClock()
    store = MailStore()
    engine = SequenceEngine(store, clock=clock.now)
    suppression = SuppressionList()
    tracker = DeliveryTracker(store, engine, suppression, clock=clock.now)
    res = engine.enroll("cart_recover", customer_id=7, email="lead@buyer.com",
                        context={"dedup_key": "c-9"}, triggered_at=T0)
    job = store.add_job(sequence_id=res.sequence.seq_id, seq_type="cart_recover", step_no=1,
                        template_key="cart_recover_remind", recipient="lead@buyer.com",
                        schedule_at=T0 + timedelta(hours=1))
    return tracker, store, engine, suppression, clock, res.sequence, job


class TestDeliveryAndOpens:
    """送达回执与打开埋点。"""

    def test_delivered_advances_status(self) -> None:
        """送达回执（SMTP 250/服务商 webhook）：queued → delivered（状态单调推进）。"""
        tracker, _, _, _, clock, _, job = make_world()
        tracker.on_event(TrackEvent(kind="delivered", job_id=job.job_id, at=clock.now()))
        assert job.status == "delivered"

    def test_open_counts_once(self) -> None:
        """同 job 30 天内两次像素请求仅首次计入（打开去重；SR §6.4 open 像素 nx 去重）。"""
        tracker, _, _, _, clock, _, job = make_world()
        tracker.on_event(TrackEvent(kind="delivered", job_id=job.job_id))
        o1 = tracker.on_event(TrackEvent(kind="open", job_id=job.job_id, at=clock.now()))
        clock.advance(days=2)
        o2 = tracker.on_event(TrackEvent(kind="open", job_id=job.job_id, at=clock.now()))
        assert o1["counted"] is True and o2["counted"] is False
        assert job.status == "opened" and len(job.tracked["opens"]) == 1

class TestBounceClassification:
    """退信分级（送达率工程三件套③，SR §2.2/§6.4）。"""

    def test_hard_bounce_suppresses_and_stops(self) -> None:
        """DSN 5.1.1（硬退）：job failed、抑制名单写入（哈希）、该收件人全部序列 stopped(hard_bounce)。"""
        tracker, store, engine, suppression, clock, seq, job = make_world()
        # 同收件人再开一条 festival 序列，验证「停全部序列」
        engine.enroll("festival", 7, "lead@buyer.com", {"dedup_key": "f-1"}, triggered_at=T0)
        out = tracker.on_event(TrackEvent(kind="bounce", job_id=job.job_id,
                                          dsn_status="5.1.1", at=clock.now()))
        assert out["klass"] == "hard"
        assert job.status == "failed"
        assert suppression.contains("lead@buyer.com")
        assert {s.status for s in store.sequences.values()} == {"stopped"}
        assert {s.stop_reason for s in store.sequences.values()} == {"hard_bounce"}

    def test_soft_bounce_third_time_suppresses(self) -> None:
        """连续软退（4.x.x）：第 1/2 次仅记失败，第 3 次降级入名单（边界：阈值 3 两侧）。"""
        tracker, store, engine, suppression, clock, seq, job = make_world()
        for i in range(1, 4):
            j = store.add_job(sequence_id=seq.seq_id, seq_type="cart_recover", step_no=i + 1,
                              template_key="cart_recover_offer", recipient="lead@buyer.com",
                              schedule_at=T0)
            out = tracker.on_event(TrackEvent(kind="bounce", job_id=j.job_id, dsn_status="4.2.1"))
            if i < 3:
                assert not suppression.contains("lead@buyer.com")
        assert out["klass"] == "soft"
        assert suppression.contains("lead@buyer.com")  # 第 3 次入名单
        assert seq.status == "stopped" and seq.stop_reason == "hard_bounce"  # 降级按硬退口径停序列

    def test_suppression_stores_hash_only(self) -> None:
        """抑制名单仅存 SHA-256(小写邮箱)，明文不落存储（NFR-SEC-01）。"""
        tracker, _, _, suppression, _, _, _ = make_world()
        tracker.on_event(TrackEvent(kind="unsubscribe", email="Lead@Buyer.com"))
        entry = suppression.entries()[0]
        assert "lead@buyer.com" not in repr(entry.email_hash) or "@" not in entry.email_hash
        assert len(entry.email_hash) == 64  # sha-256 hex
        import hashlib
        assert entry.email_hash == hashlib.sha256(b"lead@buyer.com").hexdigest()


class TestComplianceStops:
    """退订 / 回复驱动的停止条件。"""

    def test_unsubscribe_stops_and_suppresses(self) -> None:
        """退订事件：入名单（reason=unsubscribe）+ 全部序列 stopped(unsubscribed)。"""
        tracker, store, _, suppression, _, seq, _ = make_world()
        tracker.on_event(TrackEvent(kind="unsubscribe", email="lead@buyer.com"))
        assert suppression.contains("lead@buyer.com")
        assert seq.status == "stopped" and seq.stop_reason == "unsubscribed"

    def test_reply_stops_and_marks(self) -> None:
        """客户回复（IMAP 识别）：序列 stopped(replied) + 最近 job 回写 reply_at。"""
        tracker, store, _, _, clock, seq, job = make_world()
        tracker.on_event(TrackEvent(kind="delivered", job_id=job.job_id))
        tracker.on_event(TrackEvent(kind="reply", email="lead@buyer.com", at=clock.now()))
        assert seq.status == "stopped" and seq.stop_reason == "replied"
        assert job.tracked["reply_at"] == clock.now()


class TestStats:
    """统计口径（发送→送达）与 ≥95% 判定（SRS §16.4：送达率 ≥95%）。"""

    @staticmethod
    def seed_jobs(store: MailStore, engine: SequenceEngine, specs: list[str]) -> None:
        """按状态种子造 job：spec ∈ delivered/opened/failed/cancelled/queued。"""
        seq = engine.enroll("cart_recover", 1, "s@x.com", {"dedup_key": "k"}, triggered_at=T0).sequence
        for i, spec in enumerate(specs, start=1):
            j = store.add_job(sequence_id=seq.seq_id, seq_type="cart_recover", step_no=i,
                              template_key="cart_recover_remind", recipient="s@x.com", schedule_at=T0)
            if spec == "delivered":
                tracker_probe = DeliveryTracker(store, engine, SuppressionList(), clock=lambda: T0)
                tracker_probe.on_event(TrackEvent(kind="delivered", job_id=j.job_id))
            elif spec == "opened":
                tp = DeliveryTracker(store, engine, SuppressionList(), clock=lambda: T0)
                tp.on_event(TrackEvent(kind="delivered", job_id=j.job_id))
                tp.on_event(TrackEvent(kind="open", job_id=j.job_id))
            elif spec == "failed":
                j.status = "failed"

    def test_stats_counts_by_status(self) -> None:
        """口径：sent=已离开队列（delivered+opened+failed）；queued/cancelled 不入分母。"""
        tracker, store, engine, _, _, _, _ = make_world()
        self.seed_jobs(store, engine, ["delivered", "opened", "failed", "cancelled", "queued"])
        s = tracker.stats()
        assert s["sent"] == 3 and s["delivered"] == 2 and s["opened"] == 1 and s["failed"] == 1

    def test_pass_rate_boundary_at_95(self) -> None:
        """恰好 95%（19/20）判定通过（≥ 边界；SRS §16.4 验收 KPI）。"""
        tracker, store, engine, _, _, _, _ = make_world()
        self.seed_jobs(store, engine, ["delivered"] * 19 + ["failed"])
        s = tracker.stats()
        assert s["delivery_rate"] == pytest.approx(0.95)
        assert tracker.passes_delivery_rate(s) is True

    def test_pass_rate_below_95_fails(self) -> None:
        """94%（低于阈值）不通过。"""
        tracker, store, engine, _, _, _, _ = make_world()
        self.seed_jobs(store, engine, ["delivered"] * 47 + ["failed"] * 3)
        assert tracker.passes_delivery_rate(tracker.stats()) is False

    def test_pass_rate_empty_sends(self) -> None:
        """零发送不判定达标（空集保护，避免 0/0 误报通过）。"""
        tracker, _, _, _, _, _, _ = make_world()
        s = tracker.stats()
        assert s["sent"] == 0 and s["delivery_rate"] is None
        assert tracker.passes_delivery_rate(s) is False

    def test_stats_filter_by_seq_type(self) -> None:
        """按序列类型过滤统计（quote_follow 与 cart_recover 分列）。"""
        tracker, store, engine, _, _, _, _ = make_world()
        seq = engine.enroll("quote_follow", 2, "q@x.com", {"dedup_key": "q-1"}, triggered_at=T0).sequence
        j = store.add_job(sequence_id=seq.seq_id, seq_type="quote_follow", step_no=1,
                          template_key="quote_follow_touch", recipient="q@x.com", schedule_at=T0)
        tracker.on_event(TrackEvent(kind="delivered", job_id=j.job_id))
        assert tracker.stats(seq_type="quote_follow")["sent"] == 1
        assert tracker.stats(seq_type="cart_recover")["sent"] == 0

    def test_order_attribution_window(self) -> None:
        """挽回归因：发送后 7d 窗口内首单计 1（一序列只计首单），窗口外不计。"""
        tracker, store, engine, _, clock, seq, job = make_world()
        tracker.on_event(TrackEvent(kind="delivered", job_id=job.job_id, at=clock.now()))
        out = tracker.on_event(TrackEvent(kind="order", customer_id=7, at=clock.now() + timedelta(days=3)))
        assert out["attributed"] is True
        out2 = tracker.on_event(TrackEvent(kind="order", customer_id=7, at=clock.now() + timedelta(days=5)))
        assert out2["attributed"] is False  # 一序列只计首单
        assert tracker.stats()["recovered_orders"] == 1
        clock.advance(days=3)  # 距发送已超 7d
        new_seq = engine.enroll("cart_recover", 7, "lead@buyer.com", {"dedup_key": "c-10"},
                                triggered_at=clock.now()).sequence
        tracker.on_event(TrackEvent(kind="order", customer_id=7, at=clock.now() + timedelta(days=9)))
        assert tracker.stats()["recovered_orders"] == 1  # 窗口外不计
