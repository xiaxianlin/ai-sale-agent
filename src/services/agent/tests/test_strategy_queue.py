# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略 V1 单元测试（FR-AG-004，SR-AG-004 §6.1/§6.2/§9；迭代 2.3 TDD 阶段①用例）。

覆盖（策略引擎 = 节奏决策者而非执行者）：
- 秒回策略：工作时段判定（夜间顺延 ± 噪声）+ 优先级队列（deadline 最早先出）
  + 45s 预算 deadline 分解 + 在途防重（inflight 窗口幂等）；
- 跟进调度：zset 语义延迟队列（内存实现）→ 轮次模板 1/3/7/14d 配置化 →
  客户回复摘除（幂等）→ N 轮上限静默转人工 → 渲染失败本轮跳过不外发 →
  风控拒绝抖动重排；
- 模板槽位填充见 test_strategy_templates.py。

实现方案：docs/07-实现方案/AG/SR-AG-004-销售策略V1.md §6.1、§6.2、§9
"""

from __future__ import annotations

from datetime import datetime, timedelta

from services.agent.core.strategy.queue import (
    FollowupScheduler,
    InstantReplyPolicy,
    PriorityReplyQueue,
    WorkWindow,
)

NOW = datetime(2026, 10, 8, 10, 0, 0)


class TestWorkWindow:
    """工作时段判定（活跃时段模型，SR-AG-004 §6.1②）。"""

    def test_open_hours(self) -> None:
        """窗口内（09:00/08:30 整点）开放。"""
        window = WorkWindow("08:30", "18:30")
        assert window.is_open(datetime(2026, 10, 8, 9, 0, 0))
        assert window.is_open(datetime(2026, 10, 8, 8, 30, 0))

    def test_closed_hours(self) -> None:
        """窗口外（清晨/夜间）关闭。"""
        window = WorkWindow("08:30", "18:30")
        assert not window.is_open(datetime(2026, 10, 8, 8, 29, 0))
        assert not window.is_open(datetime(2026, 10, 8, 19, 0, 0))
        assert not window.is_open(datetime(2026, 10, 8, 2, 0, 0))

    def test_next_open_after_night(self) -> None:
        """02:00 询盘 → 顺延当日 08:30 窗口起点（±5min 噪声域内）。"""
        window = WorkWindow("08:30", "18:30")
        snap = window.next_open_after(datetime(2026, 10, 8, 2, 0, 0))
        assert snap.date() == datetime(2026, 10, 8).date()
        assert datetime(2026, 10, 8, 8, 25, 0) <= snap <= datetime(2026, 10, 8, 8, 35, 0)

    def test_next_open_after_window_closed_same_day(self) -> None:
        """当日窗口已过（19:30）→ 次日窗口起点。"""
        window = WorkWindow("08:30", "18:30")
        snap = window.next_open_after(datetime(2026, 10, 8, 19, 30, 0))
        assert snap.date() == datetime(2026, 10, 9).date()


class TestPriorityReplyQueue:
    """秒回优先级队列（zset score=应答截止时刻；SR-AG-004 §6.1③）。"""

    def test_budget_deadline_45s(self) -> None:
        """45s 预算：应答截止 = 到达时刻 + budget_sec。"""
        queue = PriorityReplyQueue()
        assert queue.deadline(NOW) == NOW + timedelta(seconds=45)

    def test_enqueue_and_pop_due(self) -> None:
        """到期弹出：deadline 后 pop_due 返回会话并移除。"""
        queue = PriorityReplyQueue()
        assert queue.enqueue("c-1", now_ts=NOW.timestamp()) == "enqueued"
        due = queue.pop_due(now_ts=(NOW + timedelta(seconds=46)).timestamp())
        assert due == ["c-1"]
        assert queue.pop_due(now_ts=(NOW + timedelta(seconds=47)).timestamp()) == []

    def test_pop_due_not_before_deadline(self) -> None:
        """未到截止不弹出（预算内不提前消费）。"""
        queue = PriorityReplyQueue()
        queue.enqueue("c-1", now_ts=NOW.timestamp())
        assert queue.pop_due(now_ts=(NOW + timedelta(seconds=10)).timestamp()) == []

    def test_pop_order_by_deadline(self) -> None:
        """截止临近者优先（score 升序弹出）。"""
        queue = PriorityReplyQueue()
        queue.enqueue("c-late", now_ts=NOW.timestamp())
        queue.enqueue("c-early", now_ts=(NOW - timedelta(seconds=30)).timestamp())
        due = queue.pop_due(now_ts=(NOW + timedelta(seconds=60)).timestamp())
        assert due == ["c-early", "c-late"]

    def test_duplicate_within_inflight_window(self) -> None:
        """同会话 90s 内重复事件丢弃（幂等，SR-AG-004 §6.1①）。"""
        queue = PriorityReplyQueue()
        queue.enqueue("c-1", now_ts=NOW.timestamp())
        assert queue.enqueue("c-1", now_ts=(NOW + timedelta(seconds=30)).timestamp()) == "duplicate"

    def test_inflight_window_expiry(self) -> None:
        """在途窗口过期后允许再次入队。"""
        queue = PriorityReplyQueue()
        queue.enqueue("c-1", now_ts=NOW.timestamp())
        assert queue.enqueue("c-1", now_ts=(NOW + timedelta(seconds=91)).timestamp()) == "enqueued"

    def test_defer_to_worktime(self) -> None:
        """夜间缓发：defer 写入指定时刻，到期后可弹出（早班首响）。"""
        queue = PriorityReplyQueue()
        queue.defer("c-night", until_ts=datetime(2026, 10, 8, 8, 35, 0).timestamp())
        assert queue.pop_due(now_ts=datetime(2026, 10, 8, 8, 34, 0).timestamp()) == []
        assert queue.pop_due(now_ts=datetime(2026, 10, 8, 8, 36, 0).timestamp()) == ["c-night"]


class TestInstantReplyPolicy:
    """秒回策略编排：工作时段判定 + 入队/缓发/去重三出口。"""

    def test_daytime_enqueues(self) -> None:
        """工作时段新询盘 → 入优先级队列，deadline=到达+45s。"""
        policy = InstantReplyPolicy()
        outcome = policy.on_new_inquiry("c-1", arrived=NOW)
        assert outcome.result == "enqueued"
        assert outcome.deadline == NOW + timedelta(seconds=45)

    def test_night_defers_to_worktime(self) -> None:
        """夜间询盘不发送 → 缓发至下一工作窗口（告警不阻断由调用方承载）。"""
        policy = InstantReplyPolicy()
        outcome = policy.on_new_inquiry("c-n", arrived=datetime(2026, 10, 8, 2, 0, 0))
        assert outcome.result == "deferred_to_worktime"
        assert outcome.deadline.hour == 8

    def test_duplicate_event(self) -> None:
        """同会话在途重复事件 → duplicate（零重复入队）。"""
        policy = InstantReplyPolicy()
        policy.on_new_inquiry("c-1", arrived=NOW)
        outcome = policy.on_new_inquiry("c-1", arrived=NOW + timedelta(seconds=5))
        assert outcome.result == "duplicate"

    def test_budget_segments_sum(self) -> None:
        """45s 预算五段分解：各段之和 ≤ 总预算（分段埋点口径）。"""
        policy = InstantReplyPolicy()
        assert sum(policy.budget_segments.values()) <= policy.budget_sec


class TestFollowupScheduler:
    """跟进调度器（zset 延迟队列内存实现；SR-AG-004 §6.2）。"""

    def make_scheduler(self, **overrides):
        """标准装配：记录发送/摘除/转人工回调（可注入覆盖）。"""
        sent: list[tuple[str, str]] = []
        exhausted: list[str] = []
        scheduler = FollowupScheduler(
            render=overrides.pop("render", None)
            or (lambda round_no: f"跟进模板第 {round_no} 轮"),
            send=overrides.pop("send", None)
            or (lambda uid, text: sent.append((uid, text)) or True),
            has_replied=overrides.pop("has_replied", None) or (lambda uid: False),
            on_exhausted=overrides.pop("on_exhausted", None) or exhausted.append,
            clock=overrides.pop("clock", None) or (lambda: NOW),
            **overrides,
        )
        return scheduler, sent, exhausted

    def test_reply_sent_schedules_round1(self) -> None:
        """发送成功回调 → 登记第 1 轮，触发时刻 = now + cadence[0] 天。"""
        scheduler, _, _ = self.make_scheduler()
        scheduler.on_reply_sent("c-1")
        tasks = scheduler.pop_due(now=NOW + timedelta(days=1, minutes=1))
        assert len(tasks) == 1
        assert tasks[0].round_no == 1
        assert tasks[0].conversation_uid == "c-1"

    def test_round1_not_due_early(self) -> None:
        """1 天内未到期不触发。"""
        scheduler, sent, _ = self.make_scheduler()
        scheduler.on_reply_sent("c-1")
        assert scheduler.pop_due(now=NOW + timedelta(hours=23)) == []
        assert sent == []

    def test_fire_sends_template_and_schedules_next(self) -> None:
        """触发第 1 轮：渲染发送 + 排程第 2 轮（+3d）。"""
        scheduler, sent, _ = self.make_scheduler()
        scheduler.on_reply_sent("c-1")
        outcomes = scheduler.fire_due(now=NOW + timedelta(days=1, minutes=1))
        assert outcomes[0].result == "sent"
        assert sent == [("c-1", "跟进模板第 1 轮")]
        nxt = scheduler.peek()
        assert len(nxt) == 1 and nxt[0][1].round_no == 2

    def test_cadence_configurable(self) -> None:
        """节奏配置化：cadence=[2,5] 时第 1 轮 +2 天触发。"""
        scheduler, _, _ = self.make_scheduler(cadence=(2, 5))
        scheduler.on_reply_sent("c-1")
        assert scheduler.pop_due(now=NOW + timedelta(days=1)) == []
        assert len(scheduler.pop_due(now=NOW + timedelta(days=2, minutes=1))) == 1

    def test_customer_reply_removes_pending(self) -> None:
        """客户回复摘除：pending 任务全部摘除且不再触发。"""
        scheduler, sent, _ = self.make_scheduler()
        scheduler.on_reply_sent("c-1")
        assert scheduler.remove("c-1") == 1
        assert scheduler.remove("c-1") == 0                       # 幂等
        assert scheduler.pop_due(now=NOW + timedelta(days=2)) == []
        assert sent == []

    def test_fire_checks_replied_and_cancels(self) -> None:
        """触发时状态复核：客户已回复 → cancelled_replied，不发送。"""
        scheduler, sent, _ = self.make_scheduler(has_replied=lambda uid: True)
        scheduler.on_reply_sent("c-1")
        outcomes = scheduler.fire_due(now=NOW + timedelta(days=1, minutes=1))
        assert outcomes[0].result == "cancelled_replied"
        assert sent == []

    def test_max_rounds_exhausted_silent_handover(self) -> None:
        """N 轮上限：最后一轮发完 → 静默转人工（on_exhausted），不再排程。"""
        scheduler, sent, exhausted = self.make_scheduler(cadence=(1, 3), max_rounds=2)
        scheduler.on_reply_sent("c-1")
        scheduler.fire_due(now=NOW + timedelta(days=1, minutes=1))     # 第 1 轮 → 排第 2 轮
        outcomes = scheduler.fire_due(now=NOW + timedelta(days=4, minutes=1))  # 第 2 轮
        assert outcomes[0].result == "sent"
        assert exhausted == ["c-1"]
        assert scheduler.peek() == []                                  # 无第 3 轮

    def test_schedule_beyond_max_rounds_rejected(self) -> None:
        """超上限排程被拒：直接触发静默转人工登记。"""
        scheduler, _, exhausted = self.make_scheduler(max_rounds=2)
        scheduler.schedule("c-9", round_no=3, fire_at=NOW + timedelta(days=1))
        assert exhausted == ["c-9"]
        assert scheduler.peek() == []

    def test_render_failure_skips_without_send(self) -> None:
        """渲染失败 → 本轮 skipped 不外发残文（None 模板）。"""
        scheduler, sent, _ = self.make_scheduler(render=lambda round_no: None)
        scheduler.on_reply_sent("c-1")
        outcomes = scheduler.fire_due(now=NOW + timedelta(days=1, minutes=1))
        assert outcomes[0].result == "skipped_render"
        assert sent == []

    def test_risk_reject_reschedules_with_jitter(self) -> None:
        """风控拒绝（send=False）→ 抖动重排而非丢失。"""
        scheduler, _, _ = self.make_scheduler(send=lambda uid, text: False,
                                              reschedule_minutes=15)
        scheduler.on_reply_sent("c-1")
        outcomes = scheduler.fire_due(now=NOW + timedelta(days=1, minutes=1))
        assert outcomes[0].result == "rescheduled"
        nxt = scheduler.peek()
        assert len(nxt) == 1
        assert nxt[0][0] >= (NOW + timedelta(days=1, minutes=16)).timestamp()

    def test_schedule_idempotent_per_round(self) -> None:
        """同会话同轮幂等（UNIQUE(conv, round) 语义）：重复排程只保留一个任务。"""
        scheduler, _, _ = self.make_scheduler()
        first = scheduler.schedule("c-1", round_no=1, fire_at=NOW + timedelta(days=1))
        second = scheduler.schedule("c-1", round_no=1, fire_at=NOW + timedelta(days=2))
        assert first == second
        assert len(scheduler.peek()) == 1
