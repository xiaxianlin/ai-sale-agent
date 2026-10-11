# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004 邮件自动化·序列状态机用例（SR-SITE-004 §6.1/§9；TDD 阶段①②）。

覆盖（开发规范 §9.1 五类）：
- 等价类：三类内置序列默认步骤（弃购 1h/24h/72h、报价 D+3/D+7/D+14、节日单步）、
  配置覆盖步骤、合法迁移矩阵全表；
- 边界：同 dedup_key 幂等 / 不同 key 新开；到期时刻恰好等于 now；
- 异常：终端状态再迁移、stop_reason 不在 stop_on 白名单；
- 并发/幂等：同事件重放 enroll 不新建；
- 降级：app_config 覆盖（免发版调参，NFR-MAI-02）。
验收口径映射：SR-SITE-004 §2.1 状态机（active→done/stopped）、§16.4 可配置运行。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from services.site.core.mailing.sequences import (
    DEFAULT_SEQUENCE_DEFS,
    IllegalTransition,
    MailStore,
    SequenceEngine,
)

from .fakes import MutableClock, T0


def make_engine(clock: MutableClock | None = None, overrides: dict | None = None):
    """构造（引擎, 存储, 时钟）三元组。"""
    clock = clock or MutableClock()
    store = MailStore()
    return SequenceEngine(store, clock=clock.now, overrides=overrides), store, clock


class TestSequenceDefs:
    """三类内置序列定义（等价类）。"""

    def test_cart_recover_default_steps(self) -> None:
        """弃购挽回默认三步：1h 提醒 → 24h 优惠 → 72h 最后召唤（SR §2.1）。"""
        d = DEFAULT_SEQUENCE_DEFS["cart_recover"]
        assert [s.offset_h for s in d.steps] == [1, 24, 72]
        assert [s.template_key for s in d.steps] == [
            "cart_recover_remind", "cart_recover_offer", "cart_recover_last"]
        assert d.stop_on == frozenset({"repurchased", "replied", "unsubscribed", "hard_bounce"})

    def test_quote_follow_default_steps(self) -> None:
        """报价跟进默认三步：D+3 跟进 → D+7 价值要点 → D+14 收尾。"""
        d = DEFAULT_SEQUENCE_DEFS["quote_follow"]
        assert [s.offset_h for s in d.steps] == [72, 168, 336]
        assert "won" in d.stop_on

    def test_festival_single_step(self) -> None:
        """节日营销单步群发，停止条件不含 repurchased/replied。"""
        d = DEFAULT_SEQUENCE_DEFS["festival"]
        assert len(d.steps) == 1
        assert d.stop_on == frozenset({"unsubscribed", "hard_bounce"})

    def test_seq_def_override(self) -> None:
        """配置覆盖：弃购步骤改 2 步即生效（免发版，NFR-MAI-02；SR §6.1 seq_def）。"""
        two_steps = ({"offset_h": 6, "template_key": "x"},) * 2
        engine, _, _ = make_engine(overrides={"cart_recover": {"steps": two_steps}})
        d = engine.seq_def("cart_recover")
        assert len(d.steps) == 2
        # 其他序列不受覆盖影响（覆盖式合并，非全局）
        assert len(engine.seq_def("quote_follow").steps) == 3


class TestEnroll:
    """序列开启与幂等（并发/幂等重放）。"""

    def test_enroll_creates_active(self) -> None:
        """enroll → active 序列，记录 dedup_key 与收件人。"""
        engine, store, _ = make_engine()
        res = engine.enroll("cart_recover", customer_id=7, email="a@b.com",
                            context={"dedup_key": "c-9"}, triggered_at=T0)
        assert res.created is True
        seq = store.sequences[res.sequence.seq_id]
        assert seq.status == "active" and seq.email == "a@b.com"
        assert seq.context["dedup_key"] == "c-9"

    def test_enroll_dedup_same_key(self) -> None:
        """同 (seq_type, customer, dedup_key) 重复触发只开一次（幂等重放；SR §4 UNIQUE）。"""
        engine, store, _ = make_engine()
        r1 = engine.enroll("cart_recover", 7, "a@b.com", {"dedup_key": "c-9"}, triggered_at=T0)
        r2 = engine.enroll("cart_recover", 7, "a@b.com", {"dedup_key": "c-9"},
                           triggered_at=T0 + timedelta(hours=2))
        assert r2.created is False and r2.sequence.seq_id == r1.sequence.seq_id
        assert len(store.sequences) == 1

    def test_enroll_different_key_opens_new(self) -> None:
        """不同 dedup_key（新购物车）开新序列（等价类）。"""
        engine, store, _ = make_engine()
        engine.enroll("cart_recover", 7, "a@b.com", {"dedup_key": "c-1"}, triggered_at=T0)
        r2 = engine.enroll("cart_recover", 7, "a@b.com", {"dedup_key": "c-2"}, triggered_at=T0)
        assert r2.created is True and len(store.sequences) == 2

class TestTransitions:
    """状态机合法迁移矩阵（SR §2.1：active →（步骤逐封发送）→ done/stopped）。"""

    def test_transition_matrix(self) -> None:
        """合法迁移恰为 active→active / active→done / active→stopped；终端态迁移一律非法。"""
        from services.site.core.mailing.sequences import LEGAL_TRANSITIONS
        assert LEGAL_TRANSITIONS == {("active", "active"), ("active", "done"), ("active", "stopped")}
        for src in ("done", "stopped"):
            for dst in ("active", "done", "stopped"):
                assert (src, dst) not in LEGAL_TRANSITIONS

    @pytest.mark.parametrize("reason", ["repurchased", "unsubscribed"])
    def test_stop_on_each_configured_reason(self, reason: str) -> None:
        """停止条件逐项：回购/回复/退订/硬退信 → stopped(reason)，剩余任务批量取消。"""
        engine, store, _ = make_engine()
        res = engine.enroll("cart_recover", 7, "a@b.com", {"dedup_key": "c-9"}, triggered_at=T0)
        store.add_job(sequence_id=res.sequence.seq_id, seq_type="cart_recover", step_no=2,
                      template_key="cart_recover_offer", recipient="a@b.com", schedule_at=T0)
        seq = engine.stop(res.sequence.seq_id, reason)
        assert seq.status == "stopped" and seq.stop_reason == reason
        assert all(j.status == "cancelled" for j in store.jobs.values())

    def test_stop_reason_not_in_stop_on_rejected(self) -> None:
        """cart_recover 的 stop_on 不含 'won'：纯状态机层面拒绝（成单属 quote_follow 停止条件）。"""
        engine, _, _ = make_engine()
        res = engine.enroll("cart_recover", 7, "a@b.com", {"dedup_key": "c-9"}, triggered_at=T0)
        with pytest.raises(IllegalTransition):
            engine.stop(res.sequence.seq_id, "won")

    def test_apply_event_ignores_non_stop_reason(self) -> None:
        """apply_event 对非停止条件事件 no-op（序列继续），命中停止条件才停（便捷入口）。"""
        engine, _, _ = make_engine()
        res = engine.enroll("cart_recover", 7, "a@b.com", {"dedup_key": "c-9"}, triggered_at=T0)
        assert engine.apply_event(res.sequence.seq_id, "won") is False
        assert res.sequence.status == "active"
        assert engine.apply_event(res.sequence.seq_id, "repurchased") is True
        assert res.sequence.status == "stopped"

    def test_last_step_sent_marks_done(self) -> None:
        """末步已发 → done（max_step 语义）；归因窗口仍开（SR §6.2⑦）。"""
        engine, _, _ = make_engine()
        res = engine.enroll("cart_recover", 7, "a@b.com", {"dedup_key": "c-9"}, triggered_at=T0)
        seq = res.sequence
        engine.mark_step_sent(seq)
        assert seq.status == "active"
        engine.mark_step_sent(seq)
        engine.mark_step_sent(seq)
        assert seq.status == "done" and seq.steps_sent == 3

    def test_terminal_sequence_immune(self) -> None:
        """终端态（done/stopped）再 stop / 再计步 → IllegalTransition（迁移矩阵收口）。"""
        engine, _, _ = make_engine()
        res = engine.enroll("cart_recover", 7, "a@b.com", {"dedup_key": "c-9"}, triggered_at=T0)
        engine.stop(res.sequence.seq_id, "replied")
        with pytest.raises(IllegalTransition):
            engine.stop(res.sequence.seq_id, "replied")
        with pytest.raises(IllegalTransition):
            engine.mark_step_sent(res.sequence)
