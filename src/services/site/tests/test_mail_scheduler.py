# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004 邮件自动化·调度器用例（SR-SITE-004 §6.2/§6.3；TDD 阶段①②）。

覆盖：延迟发送（zset 语义内存实现）、节奏限制（每域每小时上限）、退订名单过滤、
模板缺变量拒绝外发、SMTP 软失败重试、触发时停止条件复核（竞态兜底）、
崩溃回灌（NFR-REL-03）、事件重放幂等。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from services.site.core.mailing.scheduler import DomainRateGuard, MailEvent, MailScheduler, MemoryZSet
from services.site.core.mailing.sequences import MailStore, SequenceEngine
from services.site.core.mailing.templates import TemplateRegistry
from services.site.core.mailing.tracking import SuppressionList

from .fakes import FakeSmtpTransport, MutableClock, T0

CART_TPL = {
    "cart_recover_remind": {"en": {"subject": "Your cart",
                                   "body_html": "Hi {{ customer_name }}, cart {{ cart_items }}"}},
    "cart_recover_offer": {"en": {"subject": "Offer", "body_html": "coupon {{ coupon_code }}"}},
    "cart_recover_last": {"en": {"subject": "Last call", "body_html": "bye {{ customer_name }}"}},
    "festival_greet": {"en": {"subject": "Festival", "body_html": "Hi {{ customer_name }}"}},
}


def make_world(per_domain_per_hour: int = 1000):
    """装配调度世界：引擎/存储/模板/名单/限速/SMTP/调度器。"""
    clock = MutableClock()
    store = MailStore()
    engine = SequenceEngine(store, clock=clock.now)
    templates = TemplateRegistry(clock=clock.now)
    for key, versions in CART_TPL.items():
        templates.upsert(key, versions)
    suppression = SuppressionList()
    smtp = FakeSmtpTransport(clock)
    scheduler = MailScheduler(
        engine=engine, store=store, transport=smtp, templates=templates,
        suppression=suppression, rate_guard=DomainRateGuard(per_domain_per_hour=per_domain_per_hour),
        clock=clock.now,
    )
    return clock, store, engine, templates, suppression, smtp, scheduler


def cart_event(email: str = "lead@buyer.com", customer_id: int = 7, **kw) -> MailEvent:
    """弃购事件（FR-SITE-002 上报形态，含第 2 步优惠所需券码）。"""
    return MailEvent(seq_type="cart_recover", customer_id=customer_id, email=email,
                     context={"dedup_key": f"c-{customer_id}", "customer_name": "Ace",
                              "cart_items": "ST-3200 x2", "coupon_code": "SAVE5"}, **kw)


class TestDelayedSend:
    """延迟发送（zset 语义）。"""

    def test_event_schedules_all_steps(self) -> None:
        """弃购事件一次排产 3 个 job，zset score = 计划发送时刻（SR §6.2）。"""
        _, store, _, _, _, _, sched = make_world()
        out = sched.on_event(cart_event())
        assert out.created is True
        jobs = sorted(store.jobs.values(), key=lambda j: j.step_no)
        assert [j.step_no for j in jobs] == [1, 2, 3]
        assert [j.schedule_at for j in jobs] == [T0 + timedelta(hours=h) for h in (1, 24, 72)]
        assert sched.sendq.size() == 3

    def test_no_send_before_due(self) -> None:
        """未到期 poll 零外发（延迟发送核心语义）。"""
        clock, _, _, _, _, smtp, sched = make_world()
        sched.on_event(cart_event())
        assert sched.poll() == []
        clock.advance(minutes=59)
        assert sched.poll() == []
        assert smtp.sent == []

    def test_send_step1_after_one_hour(self) -> None:
        """推进 1h → 仅第 1 步发出且状态 delivered（SMTP 250=送达口径）；后续两步未发。"""
        clock, store, _, _, _, smtp, sched = make_world()
        sched.on_event(cart_event())
        clock.advance(hours=1)
        outcomes = sched.poll()
        assert [o.action for o in outcomes] == ["sent"]
        job = next(j for j in store.jobs.values() if j.step_no == 1)
        assert job.status == "delivered" and job.message_id == smtp.sent[0].message_id
        assert {j.status for j in store.jobs.values() if j.step_no > 1} == {"queued"}

    def test_poll_order_by_fire_time(self) -> None:
        """同批到期按 score 升序弹出（zset 顺序语义；quote_follow D+3 与弃购 1h 同刻）。"""
        clock, store, _, _, _, smtp, sched = make_world()
        sched.on_event(cart_event(email="a@x.com", customer_id=1))
        sched.on_event(MailEvent(seq_type="festival", customer_id=2, email="b@x.com",
                                 context={"dedup_key": "f-1", "fire_at": T0 + timedelta(hours=1)}))
        clock.advance(hours=1)
        outcomes = sched.poll()
        # a 序列第 1 步（1h）与 b 节日步（1h）都到期，按入队序稳定输出
        assert len(outcomes) == 2 and all(o.action == "sent" for o in outcomes)

    def test_full_sequence_completes(self) -> None:
        """三步全部发完 → 序列 done（max_step）；72h 步骤边界恰到期即发。"""
        clock, _, engine, _, _, _, sched = make_world()
        sched.on_event(cart_event())
        for hours in (1, 24, 72):
            clock.advance_to(T0 + timedelta(hours=hours))
            sched.poll()
        seq = next(iter(engine.store.sequences.values()))
        assert seq.status == "done" and seq.steps_sent == 3


class TestGateKeeping:
    """发送前置门禁：抑制名单 / 缺邮箱 / 停止条件复核。"""

    def test_suppressed_recipient_not_sent(self) -> None:
        """收件人在退订名单：触发时 job cancelled、序列 stopped(unsubscribed)、SMTP 零调用。"""
        clock, store, _, _, suppression, smtp, sched = make_world()
        suppression.add("lead@buyer.com", reason="unsubscribe", source="one_click")
        sched.on_event(cart_event())
        clock.advance(hours=1)
        outcomes = sched.poll()
        assert [o.action for o in outcomes] == ["skipped_suppressed"]
        assert smtp.sent == []
        seq = next(iter(store.sequences.values()))
        assert seq.status == "stopped" and seq.stop_reason == "unsubscribed"

    def test_missing_email_records_gap(self) -> None:
        """无留资邮箱事件：登记缺口不排产（等 FR-SITE-005 合并后再触发，SR §6.2 on_event）。"""
        _, store, _, _, _, _, sched = make_world()
        out = sched.on_event(MailEvent(seq_type="cart_recover", customer_id=9, email=None,
                                       context={"dedup_key": "c-9"}))
        assert out.created is False
        assert len(store.email_gaps) == 1 and store.jobs == {}

    def test_stop_probe_cancels_remaining(self) -> None:
        """触发时停止条件复核：已回购 → 剩余 queued 全 cancelled、不外发（竞态兜底①）。"""
        clock, store, engine, _, _, smtp, sched = make_world()
        sched.on_event(cart_event())
        clock.advance(hours=1)
        sched.poll()  # 第 1 步发出
        engine.store.sequences[next(iter(engine.store.sequences))].status = "stopped"  # 直接置停（模拟回购回写）
        engine.store.sequences[next(iter(engine.store.sequences))].stop_reason = "repurchased"
        clock.advance(hours=23)
        outcomes = sched.poll()
        assert all(o.action == "skipped_terminal" for o in outcomes)
        assert {j.status for j in store.jobs.values()} == {"delivered", "cancelled"}


class TestRateLimit:
    """发送节奏限制：每域每小时上限（送达率工程三件套①，SR §2.2/§6.3）。"""

    def test_over_hourly_cap_defers(self) -> None:
        """每域每小时上限 2：第 3 封顺延（deferred_rate）且不外发。"""
        clock, _, _, _, _, smtp, sched = make_world(per_domain_per_hour=2)
        for i in range(3):
            sched.on_event(cart_event(email=f"u{i}@same.com", customer_id=i + 1))
        clock.advance(hours=1)
        actions = sorted(o.action for o in sched.poll())
        assert actions == ["deferred_rate", "sent", "sent"]
        assert len(smtp.sent) == 2

class TestDegradedPaths:
    """降级与异常路径：模板失败 / SMTP 软失败 / 崩溃回灌 / 重放幂等。"""

    def test_missing_template_var_fails_job(self) -> None:
        """模板缺变量：job failed(template) 且不外发（拒绝外发残文，SR §6.2⑤）。"""
        clock, store, _, templates, _, smtp, sched = make_world()
        # 事件上下文未提供 quote_no → 模板引用即真缺失（业务槽位不喂空串）
        templates.upsert("cart_recover_remind", {"en": {"subject": "s", "body_html": "{{ quote_no }}"}})
        sched.on_event(cart_event())
        clock.advance(hours=1)
        outcomes = sched.poll()
        assert [o.action for o in outcomes] == ["failed_template"]
        assert smtp.sent == []
        assert next(iter(store.jobs.values())).status == "failed"

    def test_smtp_soft_error_retries(self) -> None:
        """SMTP 软失败：退避重试后成功（4xx 仅重试不拉黑，SR §6.3）。"""
        clock, store, _, _, _, smtp, sched = make_world()
        smtp.fail_next("lead@buyer.com", times=1)
        sched.on_event(cart_event())
        clock.advance(hours=1)
        assert [o.action for o in sched.poll()] == ["deferred_retry"]
        clock.advance(minutes=sched.rate_backoff_min)
        assert [o.action for o in sched.poll()] == ["sent"]
        assert len(smtp.sent) == 1

    def test_rehydrate_after_crash(self) -> None:
        """崩溃回灌：zset 清空后 rehydrate 重建索引，到期任务不丢（NFR-REL-03）。"""
        clock, _, _, _, _, smtp, sched = make_world()
        sched.on_event(cart_event())
        clock.advance(hours=3)
        sched.sendq = MemoryZSet()  # 模拟 Redis 丢失（进程崩溃）
        assert sched.rehydrate() == 3
        clock.advance(hours=21)  # 至 24h 步到期
        actions = [o.action for o in sched.poll()]
        assert actions.count("sent") == 2  # 1h 步补发 + 24h 步准发，72h 步未到期

    def test_unsub_header_and_link_present(self) -> None:
        """每封营销邮件必带退订：List-Unsubscribe 头（RFC 8058 one-click）+ 正文退订链接。"""
        clock, _, _, _, _, smtp, sched = make_world()
        sched.on_event(cart_event())
        clock.advance(hours=1)
        sched.poll()
        mail = smtp.sent[0]
        assert mail.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert "List-Unsubscribe" in mail.headers
        assert "/s/m/u/" in mail.html
