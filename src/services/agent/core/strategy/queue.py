# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""策略引擎 V1：秒回优先级队列 + 跟进延迟队列（FR-AG-004，SR-AG-004 §6.1/§6.2）。

职责边界（策略引擎是**节奏决策者**而非执行者）：本模块只决定「何时发、发什么模板」，
实际发送一律经 FR-AG-005 风控放行（send 回调由风控通道承载，demo 注入 Fake）。

- :class:`WorkWindow`——工作时段判定（活跃时段模型：夜间/窗外顺延 ±5min 噪声）；
- :class:`PriorityReplyQueue`——秒回优先级队列（zset score=应答截止时刻语义，
  deadline 临近者先出；inflight 90s 防重窗口幂等）；
- :class:`InstantReplyPolicy`——秒回编排：工作时段判定 → 入队 / 夜间缓发 / 重复丢弃
  三出口 + 45s 预算五段分解（NFR-PER-03 分段埋点口径）；
- :class:`FollowupScheduler`——跟进调度（zset 延迟队列内存实现）：轮次模板
  1/3/7/14d 配置化 → 客户回复摘除（幂等）→ N 轮上限静默转人工 → 渲染失败本轮
  跳过不外发残文 → 风控拒绝抖动重排。

demo 形态边界：Redis zset（Lua 原子弹出）/ PG followup_task 权威源 + 重启回灌由
生产形态承载；进程内堆结构保持「score 升序弹出」同语义。
"""

from __future__ import annotations

import heapq
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import random
from typing import Protocol


def _parse_hm(value: str) -> tuple[int, int]:
    """「HH:MM」→ (时, 分)。"""
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


class WorkWindow:
    """工作时段判定（SR-AG-004 §6.1②：客户时区/账号窗口，demo 仅小时窗）。

    夜间/窗外不自动打扰——秒回事件顺延至下一工作窗口起点 ±5min 噪声
    （对齐风控活跃时段模型；大客户仍走 FR-AG-007 告警，由调用方承载）。
    """

    def __init__(
        self, start: str = "08:30", end: str = "18:30", *,
        jitter_minutes: float = 5.0, rng: random.Random | None = None,
    ) -> None:
        self._start = _parse_hm(start)
        self._end = _parse_hm(end)
        self._jitter = jitter_minutes
        self._rng = rng if rng is not None else random.Random()

    def is_open(self, moment: datetime) -> bool:
        """窗口内判定（起点含、终点不含）。"""
        start_h, start_m = self._start
        end_h, end_m = self._end
        minutes = moment.hour * 60 + moment.minute
        return start_h * 60 + start_m <= minutes < end_h * 60 + end_m

    def next_open_after(self, moment: datetime) -> datetime:
        """下一开放时刻：早于今日起点→今日起点；已过终点→次日起点；开放中→当前。

        附 ±jitter_minutes 入场噪声（防整点聚集，M2）。
        """
        start_h, start_m = self._start
        end_h, end_m = self._end
        day_open = moment.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        day_close = moment.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if moment < day_open:
            base = day_open
        elif moment >= day_close:
            base = (moment + timedelta(days=1)).replace(
                hour=start_h, minute=start_m, second=0, microsecond=0)
        else:
            return moment                        # 窗口开放中：即刻可响应
        offset = timedelta(minutes=self._rng.uniform(-self._jitter, self._jitter))
        return base + offset


# ─────────────────────────── 秒回优先级队列 ───────────────────────────


class PriorityReplyQueue:
    """秒回优先级队列（t:{tenant}:strat:reply_pq:{aid} zset 语义的进程内等价）。

    score = 应答截止时刻（deadline 临近者优先被消费）；inflight 窗口（默认 90s）
    内同会话重复事件丢弃（幂等，SR-AG-004 §6.1① SETNX 语义）。
    """

    def __init__(self, *, budget_sec: float = 45.0, inflight_window_s: float = 90.0) -> None:
        self._budget = budget_sec
        self._inflight_window = inflight_window_s
        self._heap: list[tuple[float, int, str]] = []      # (deadline_ts, seq, conv_uid)
        self._inflight: dict[str, float] = {}              # conv_uid → 最近入队时刻
        self._seq = 0

    def deadline(self, arrived: datetime) -> datetime:
        """应答截止 = 到达时刻 + 45s 预算（NFR-PER-03）。"""
        return arrived + timedelta(seconds=self._budget)

    def enqueue(self, conv_uid: str, *, now_ts: float, deadline_ts: float | None = None) -> str:
        """入队（幂等）：在途窗口内重复事件返回 duplicate，不重复占位。"""
        last = self._inflight.get(conv_uid)
        if last is not None and now_ts - last < self._inflight_window:
            return "duplicate"
        self._inflight[conv_uid] = now_ts
        moment = deadline_ts if deadline_ts is not None else now_ts + self._budget
        heapq.heappush(self._heap, (moment, self._seq, conv_uid))
        self._seq += 1
        return "enqueued"

    def defer(self, conv_uid: str, *, until_ts: float) -> None:
        """夜间缓发：写入指定触发时刻（早班首响）。"""
        heapq.heappush(self._heap, (until_ts, self._seq, conv_uid))
        self._seq += 1

    def pop_due(self, *, now_ts: float) -> list[str]:
        """弹出全部到期会话（deadline 升序 = 截止临近者优先）。"""
        due: list[str] = []
        while self._heap and self._heap[0][0] <= now_ts:
            _, _, conv_uid = heapq.heappop(self._heap)
            due.append(conv_uid)
        return due

    def peek(self) -> list[tuple[str, float]]:
        """在途快照（观测面，不影响队列）。"""
        return [(conv_uid, moment) for moment, _, conv_uid in sorted(self._heap)]

    def __len__(self) -> int:
        return len(self._heap)


#: 秒回处置三出口
@dataclass(frozen=True)
class InstantOutcome:
    """秒回策略处置结果（enqueued / deferred_to_worktime / duplicate + 截止时刻）。"""

    result: str
    deadline: datetime | None = None


class InstantReplyPolicy:
    """秒回策略编排（SR-AG-004 §6.1）：工作时段判定 → 入队/缓发/去重。"""

    #: 45s 预算五段分解（秒；分段埋点口径，超段即告警由调用方承载）
    BUDGET_SEGMENTS: dict[str, float] = {
        "detect_enqueue": 5.0, "generate": 18.0, "risk_wait": 12.0,
        "typing_send": 9.0, "persist": 1.0,
    }

    def __init__(
        self,
        *,
        queue: PriorityReplyQueue | None = None,
        window: WorkWindow | None = None,
        budget_sec: float = 45.0,
    ) -> None:
        self.queue = queue or PriorityReplyQueue(budget_sec=budget_sec)
        self.window = window or WorkWindow()
        self.budget_sec = budget_sec
        self.budget_segments = dict(self.BUDGET_SEGMENTS)

    def on_new_inquiry(self, conv_uid: str, *, arrived: datetime) -> InstantOutcome:
        """新询盘事件入口：①夜间缓发 → ②入优先级队列（幂等）。"""
        deadline = arrived + timedelta(seconds=self.budget_sec)
        if not self.window.is_open(arrived):
            snap = self.window.next_open_after(arrived)
            if snap > deadline:                    # 窗口开启晚于预算截止：缓发早班首响
                self.queue.defer(conv_uid, until_ts=snap.timestamp())
                return InstantOutcome("deferred_to_worktime", snap)
        result = self.queue.enqueue(
            conv_uid, now_ts=arrived.timestamp(), deadline_ts=deadline.timestamp()
        )
        if result == "duplicate":
            return InstantOutcome("duplicate")
        return InstantOutcome("enqueued", deadline)


# ─────────────────────────── 跟进延迟队列与调度 ───────────────────────────


class RenderFn(Protocol):
    """模板渲染回调：轮次 → 渲染文本（None = 模板缺失，本轮跳过不外发）。"""

    def __call__(self, round_no: int) -> str | None: ...


class SendFn(Protocol):
    """发送回调（生产 = FR-AG-005 风控通道）：(conv_uid, text) → 是否受理。"""

    def __call__(self, conv_uid: str, text: str) -> bool: ...


@dataclass(frozen=True)
class FollowupTask:
    """跟进任务（followup_task 行语义；同会话同轮唯一 = 排程幂等约束）。"""

    task_uid: str
    conversation_uid: str
    round_no: int
    fire_at: datetime
    status: str = "pending"        # pending / sent / cancelled / skipped / rescheduled


@dataclass(frozen=True)
class FireOutcome:
    """单任务触发结果（sent / cancelled_replied / skipped_render / rescheduled）。"""

    task_uid: str
    conversation_uid: str
    round_no: int
    result: str
    text: str = ""


class FollowupScheduler:
    """跟进调度器（SR-AG-004 §6.2）：zset 延迟队列内存实现。

    - cadence 轮次间隔配置化（默认 [1,3,7,14] 天，app_config 即时生效语义）；
    - 客户回复摘除（幂等）/ 触发时状态复核（取消竞态）；
    - N 轮上限：最后一轮发完静默转人工（on_exhausted），不再排程；
    - 渲染失败本轮 skipped 不外发残文；风控拒绝 → 抖动重排而非丢失。
    """

    def __init__(
        self,
        *,
        cadence: tuple[float, ...] = (1.0, 3.0, 7.0, 14.0),
        max_rounds: int | None = None,
        render: RenderFn | None = None,
        send: SendFn | None = None,
        has_replied: Callable[[str], bool] | None = None,
        on_exhausted: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        reschedule_minutes: float = 15.0,
    ) -> None:
        self._cadence = tuple(float(days) for days in cadence)
        self._max_rounds = max_rounds if max_rounds is not None else len(self._cadence)
        self._render: RenderFn = render if render is not None else lambda round_no: None
        self._send: SendFn = send if send is not None else lambda uid, text: True
        self._has_replied = has_replied if has_replied is not None else lambda uid: False
        self._on_exhausted = on_exhausted if on_exhausted is not None else lambda uid: None
        self._clock = clock or (lambda: datetime.now())
        self._reschedule_minutes = reschedule_minutes
        self._tasks: dict[str, FollowupTask] = {}
        self._by_round: dict[tuple[str, int], str] = {}    # (conv, round) → task_uid 幂等
        self._removed: set[str] = set()

    # ---------------------------------------------------------------- 排程

    def on_reply_sent(self, conversation_uid: str, *, now: datetime | None = None) -> str:
        """发送成功回调：登记第 1 轮（触发时刻 = now + cadence[0] 天）。"""
        moment = now if now is not None else self._clock()
        return self.schedule(
            conversation_uid, round_no=1,
            fire_at=moment + timedelta(days=self._cadence[0]),
        )

    def schedule(
        self, conversation_uid: str, round_no: int, *, fire_at: datetime
    ) -> str:
        """排程一轮跟进：同会话同轮幂等；超上限轮次直接静默转人工登记。"""
        key = (conversation_uid, round_no)
        existing = self._by_round.get(key)
        if existing is not None:
            return existing
        if round_no > self._max_rounds:
            # 防骚扰上限：不排程，登记静默转人工（后台待办由告警通道承载）
            self._on_exhausted(conversation_uid)
            return ""
        task_uid = uuid.uuid4().hex[:12]
        task = FollowupTask(task_uid=task_uid, conversation_uid=conversation_uid,
                            round_no=round_no, fire_at=fire_at)
        self._tasks[task_uid] = task
        self._by_round[key] = task_uid
        return task_uid

    def remove(self, conversation_uid: str) -> int:
        """摘除该会话全部 pending 任务（客户回复/人工接管；幂等，返回摘除数）。"""
        removed = 0
        for task in self._tasks.values():
            if task.conversation_uid == conversation_uid and task.status == "pending":
                self._tasks[task.task_uid] = replace(task, status="cancelled")
                removed += 1
        self._removed.add(conversation_uid)
        return removed

    # ---------------------------------------------------------------- 消费

    def pop_due(self, *, now: datetime) -> list[FollowupTask]:
        """到期任务快照（fire_at 升序；已摘除会话不返回——zrem 语义）。"""
        due = [
            task for task in self._tasks.values()
            if task.status == "pending"
            and task.fire_at <= now
            and task.conversation_uid not in self._removed
        ]
        return sorted(due, key=lambda task: task.fire_at)

    def fire_due(self, *, now: datetime) -> list[FireOutcome]:
        """延迟队列主循环的单轮消费：逐任务触发（渲染→发送→排下一轮/转人工）。"""
        return [self._fire(task, now) for task in self.pop_due(now=now)]

    def peek(self) -> list[tuple[float, FollowupTask]]:
        """在途任务快照（观测面；(fire_at_ts, task) 升序）。"""
        pending = [task for task in self._tasks.values()
                   if task.status == "pending"
                   and task.conversation_uid not in self._removed]
        return sorted(((task.fire_at.timestamp(), task) for task in pending),
                      key=lambda pair: pair[0])

    # ---------------------------------------------------------------- 内部

    def _fire(self, task: FollowupTask, now: datetime) -> FireOutcome:
        """单任务触发（SR-AG-004 §6.2 _fire 流程的进程内形态）。"""
        # ① 状态复核：客户已回复/人工接管 → 全摘除，不发送
        if task.conversation_uid in self._removed or self._has_replied(task.conversation_uid):
            self._tasks[task.task_uid] = replace(task, status="cancelled")
            return FireOutcome(task.task_uid, task.conversation_uid, task.round_no,
                               "cancelled_replied")
        # ② 模板渲染：失败本轮 skipped 不外发残文（知识缺口回流由调用方承载）
        text = self._render(task.round_no)
        if text is None:
            self._tasks[task.task_uid] = replace(task, status="skipped")
            return FireOutcome(task.task_uid, task.conversation_uid, task.round_no,
                               "skipped_render")
        # ③ 过风控发送：拒绝 → 抖动重排（不丢失、不绕过）
        if not self._send(task.conversation_uid, text):
            self._reschedule(task, now + timedelta(minutes=self._reschedule_minutes))
            return FireOutcome(task.task_uid, task.conversation_uid, task.round_no,
                               "rescheduled", text)
        # ④ 发送成功：落账 + 排下一轮 / N 轮上限静默转人工
        self._tasks[task.task_uid] = replace(task, status="sent")
        if task.round_no >= self._max_rounds:
            self._on_exhausted(task.conversation_uid)
        else:
            self.schedule(
                task.conversation_uid, round_no=task.round_no + 1,
                fire_at=task.fire_at + timedelta(days=self._cadence[task.round_no]),
            )
        return FireOutcome(task.task_uid, task.conversation_uid, task.round_no,
                           "sent", text)

    def _reschedule(self, task: FollowupTask, fire_at: datetime) -> None:
        """抖动重排（风控拒绝后的重试位，保持同轮幂等键不变）。"""
        self._tasks[task.task_uid] = replace(task, fire_at=fire_at, status="pending")


__all__ = [
    "WorkWindow",
    "PriorityReplyQueue",
    "InstantOutcome",
    "InstantReplyPolicy",
    "FollowupTask",
    "FireOutcome",
    "FollowupScheduler",
]
