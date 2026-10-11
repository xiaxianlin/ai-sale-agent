# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略 V2 ④流失挽回：静默判定 + 唤醒序列 + 停止条件（FR-SC-003④，SR-SC-003 §6.4）。

- 静默判定（每日扫描）：ai_managed 会话最近 inbound 超过静默天数（默认 14，
  可分级化）且无在途任务 → 建 round=1 任务（UNIQUE conv+round 幂等）；
- 唤醒序列：sequence_days（默认 [0,4,10]）多轮 + 递进激励 tier 0→1→2
  （0 情感唤起 / 1 赠品样品 / 2 阶梯让利）；
- 停止条件：①客户已回复（恢复正常跟进链路）②人工接管 ③明确拒绝意图
  ④序列走完（exhausted 静默归档）——触发时状态复核优先于发送；
- 门禁与保险：tier2 让利轮须主管批准（否则等待重排）；每账号日发送上限；
  发送一律过风控（拒绝 → 抖动重排不丢失）；渠道：阿里站内信 inmail / 其余 email。

demo 形态：内存任务表（生产 = PG winback_task 权威 + zset 索引 + 崩溃回灌，NFR-REL-03）。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

#: 挽回通道（inmail 走风控+拟人；email 走 site-service mail 通道复用，FR-SITE-004）
CHANNELS: tuple[str, ...] = ("inmail", "email")
#: 任务状态（winback_task CHECK 口径）
STATUSES: tuple[str, ...] = ("pending", "running", "sent", "cancelled", "failed", "skipped", "finished")


@dataclass(frozen=True)
class ConversationRef:
    """静默扫描的会话引用（conversation 表投影）。"""

    conversation_uid: str
    account_id: int
    customer_id: int
    channel: str = "alibaba"                      # 平台渠道：alibaba → inmail
    state: str = "ai_managed"                     # 会话状态机当前态
    last_inbound_at: datetime | None = None
    refusal_recently: bool = False                # 明确拒绝意图标签（客户画像）


class WinbackSource:
    """挽回数据源协议（注入接缝；生产 = PG 查询 + agent-core 意图标签）。"""

    def silent_conversations(self, *, older_than_days: int, now: datetime) -> Sequence[ConversationRef]:
        """静默会话扫描（含 ai_managed 且最近 inbound 超阈值）。"""
        raise NotImplementedError

    def inbound_after(self, conversation_uid: str, since: datetime) -> bool:
        """客户在 since 之后是否有入站（停止条件①）。"""
        raise NotImplementedError

    def state_of(self, conversation_uid: str) -> str:
        """会话状态机当前态（停止条件②：非 ai_managed 即人工侧）。"""
        raise NotImplementedError

    def refusal_recently(self, customer_id: int) -> bool:
        """客户近期是否明确拒绝（停止条件③）。"""
        raise NotImplementedError


class InMemoryWinbackSource(WinbackSource):
    """内存数据源（demo Fake：扫描/入站/状态/拒绝标签可注入）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._convs: dict[str, ConversationRef] = {}
        self._inbound: dict[str, list[datetime]] = {}

    def upsert(self, ref: ConversationRef) -> None:
        """登记/覆盖会话引用（测试夹具入口）。"""
        with self._lock:
            self._convs[ref.conversation_uid] = ref

    def record_inbound(self, conversation_uid: str, *, at: datetime) -> None:
        """登记客户入站（竞态注入：模拟唤醒期间客户回复）。"""
        with self._lock:
            self._inbound.setdefault(conversation_uid, []).append(at)

    def set_state(self, conversation_uid: str, state: str) -> None:
        """改写会话状态（模拟人工接管）。"""
        with self._lock:
            ref = self._convs[conversation_uid]
            self._convs[conversation_uid] = replace(ref, state=state)

    def set_refusal(self, customer_id: int, refused: bool) -> None:
        """改写客户拒绝标签（模拟明确拒绝意图）。"""
        with self._lock:
            for uid, ref in self._convs.items():
                if ref.customer_id == customer_id:
                    self._convs[uid] = replace(ref, refusal_recently=refused)

    def silent_conversations(self, *, older_than_days: int, now: datetime) -> list[ConversationRef]:
        with self._lock:
            found = []
            for ref in self._convs.values():
                last = ref.last_inbound_at
                # 「超过」静默天数才判定（恰等于阈值不判，边界语义）
                silent = last is not None and (now - last).days > older_than_days
                if silent and ref.state == "ai_managed" and not ref.refusal_recently:
                    found.append(ref)
            return found

    def inbound_after(self, conversation_uid: str, since: datetime) -> bool:
        with self._lock:
            return any(at > since for at in self._inbound.get(conversation_uid, ()))

    def state_of(self, conversation_uid: str) -> str:
        with self._lock:
            return self._convs[conversation_uid].state

    def refusal_recently(self, customer_id: int) -> bool:
        with self._lock:
            return any(ref.customer_id == customer_id and ref.refusal_recently
                       for ref in self._convs.values())


@dataclass(frozen=True)
class WinbackTask:
    """挽回任务（winback_task 行语义；UNIQUE(conversation_id, round) 排程幂等）。"""

    task_uid: str
    account_id: int
    customer_id: int
    conversation_uid: str
    round: int                                    # 1 起
    incentive_tier: int                           # 0 无激励 / 1 赠品样品 / 2 阶梯让利
    channel: str                                  # inmail / email
    schedule_at: datetime
    status: str = "pending"
    sent_at: datetime | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class WinbackOutcome:
    """单任务触发结果。"""

    task_uid: str
    conversation_uid: str
    round: int
    result: str                                   # sent / cancelled_* / rescheduled_* / skipped_*
    text: str = ""


class WinbackEngine:
    """流失挽回引擎（SR-SC-003 §6.4 工程级形态）。

    :param source: 挽回数据源（注入）；
    :param silent_days: 静默判定天数（默认 14；可分级化后缀由调用方解析）；
    :param sequence_days: 唤醒序列轮次基准偏移（默认 [0,4,10]）；
    :param approval_check: 主管批准校验回调（tier2 让利轮门禁，FR-AG-005 链路）；
    :param render: 模板渲染 ``(round, tier) → 文本|None``（None = 缺模板本轮跳过）；
    :param send: 发送回调（生产 = 风控放行通道；False = 拒绝 → 抖动重排）；
    :param daily_cap: 每账号日唤醒发送上限（叠加风控日配额，双保险防骚扰）；
    :param approval_retry_minutes: tier2 无批准的等待重排间隔；
    :param risk_retry_minutes: 风控拒绝的抖动重排间隔；
    :param clock: 时钟注入。
    """

    def __init__(
        self,
        source: WinbackSource,
        *,
        silent_days: int = 14,
        sequence_days: tuple[int, ...] = (0, 4, 10),
        approval_check: Callable[[str], bool] | None = None,
        render: Callable[[int, int], str | None] | None = None,
        send: Callable[[str, str], bool] | None = None,
        daily_cap: int = 10,
        approval_retry_minutes: float = 60.0,
        risk_retry_minutes: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.source = source
        self._silent_days = silent_days
        self._sequence_days = tuple(sequence_days)
        self._approval_check = approval_check or (lambda token: True)
        self._render = render or (lambda round_no, tier: f"唤醒第{round_no}轮")
        self._send = send or (lambda uid, text: True)
        self._daily_cap = daily_cap
        self._approval_retry = timedelta(minutes=approval_retry_minutes)
        self._risk_retry = timedelta(minutes=risk_retry_minutes)
        self._clock = clock or (lambda: datetime.now())
        self._lock = threading.RLock()
        self._tasks: dict[str, WinbackTask] = {}
        self._by_round: dict[tuple[str, int], str] = {}   # (conv, round) → uid 幂等

    # ---------------------------------------------------------------- 扫描建单

    def scan_silent(self, *, now: datetime | None = None, account_id: int | None = None) -> int:
        """每日静默扫描：建 round=1 任务（渠道按平台映射；幂等返回新建数）。"""
        moment = now if now is not None else self._clock()
        created = 0
        for ref in self.source.silent_conversations(
                older_than_days=self._silent_days, now=moment):
            if account_id is not None and ref.account_id != account_id:
                continue
            channel = "inmail" if ref.channel == "alibaba" else "email"
            if self._schedule(ref, round_no=1, tier=0, channel=channel,
                              schedule_at=moment) is not None:
                created += 1
        return created

    # ---------------------------------------------------------------- 序列触发

    def fire_due(self, *, now: datetime | None = None) -> list[WinbackOutcome]:
        """延迟队列主循环单轮消费：到期任务逐个触发（停止条件复核优先）。"""
        moment = now if now is not None else self._clock()
        with self._lock:
            due = sorted(
                (t for t in self._tasks.values() if t.status == "pending" and t.schedule_at <= moment),
                key=lambda t: t.schedule_at)
        return [self._fire(task, moment) for task in due]

    def pending_due(self, *, now: datetime | None = None) -> list[WinbackTask]:
        """到期 pending 任务快照（观测面）。"""
        moment = now if now is not None else self._clock()
        with self._lock:
            return sorted(
                (t for t in self._tasks.values() if t.status == "pending" and t.schedule_at <= moment),
                key=lambda t: t.schedule_at)

    def tasks_of(self, conversation_uid: str) -> tuple[WinbackTask, ...]:
        """该会话全部任务（按轮次升序）。"""
        with self._lock:
            tasks = [t for t in self._tasks.values() if t.conversation_uid == conversation_uid]
        return tuple(sorted(tasks, key=lambda t: t.round))

    def stop_reason_of(self, conversation_uid: str) -> str | None:
        """该会话序列停止原因（None = 序列仍在进行/等待）。"""
        with self._lock:
            reasons = [t.stop_reason for t in self._tasks.values()
                       if t.conversation_uid == conversation_uid and t.stop_reason]
            finished = [t for t in self._tasks.values()
                        if t.conversation_uid == conversation_uid and t.status == "finished"]
            if reasons:
                return str(reasons[0])
            if finished:
                return str(finished[-1].stop_reason)
            return None

    # ---------------------------------------------------------------- 内部

    def _schedule(self, ref: ConversationRef, *, round_no: int, tier: int, channel: str,
                  schedule_at: datetime) -> WinbackTask | None:
        """建单（UNIQUE conv+round 幂等；已停止序列不再排程）。"""
        with self._lock:
            key = (ref.conversation_uid, round_no)
            if key in self._by_round:
                return None
            if self.stop_reason_of(ref.conversation_uid) is not None:
                return None
            task = WinbackTask(
                task_uid=uuid.uuid4().hex[:12], account_id=ref.account_id,
                customer_id=ref.customer_id, conversation_uid=ref.conversation_uid,
                round=round_no, incentive_tier=tier, channel=channel, schedule_at=schedule_at)
            self._tasks[task.task_uid] = task
            self._by_round[key] = task.task_uid
            return task

    def _finish(self, conversation_uid: str, stop_reason: str, *,
                except_uid: str | None = None) -> None:
        """序列终止：全部 pending 任务取消并标记停止原因。"""
        with self._lock:
            for task in self._tasks.values():
                if task.conversation_uid != conversation_uid or task.status != "pending":
                    continue
                if except_uid is not None and task.task_uid == except_uid:
                    continue
                self._tasks[task.task_uid] = replace(task, status="cancelled",
                                                     stop_reason=stop_reason)

    def _fire(self, task: WinbackTask, now: datetime) -> WinbackOutcome:
        """单任务触发：停止条件 → 门禁/上限 → 渲染 → 发送 → 续排/归档。"""
        # ① 停止条件（复核优先于发送；全序列取消）——基准 = 序列首轮排程时刻
        base = self._sequence_base(task.conversation_uid) or task.schedule_at
        if self.source.inbound_after(task.conversation_uid, since=base):
            self._finish(task.conversation_uid, "replied")
            return self._mark(task, "cancelled_replied", stop_reason="replied")
        if self.source.state_of(task.conversation_uid) != "ai_managed":
            self._finish(task.conversation_uid, "human")
            return self._mark(task, "cancelled_human", stop_reason="human")
        if self.source.refusal_recently(task.customer_id):
            self._finish(task.conversation_uid, "refused")
            return self._mark(task, "cancelled_refused", stop_reason="refused")
        # ② tier2 让利轮须主管批准：等待重排而非直接让利
        tier = min(task.round - 1, 2)
        if tier >= 2 and not self._approval_check(f"winback:{task.task_uid}"):
            self._reschedule(task, now + self._approval_retry)
            return self._mark(task, "rescheduled_approval")
        # ③ 日发送上限：顺延次日（不丢失、不硬发）
        if self._sent_today(task.account_id, now) >= self._daily_cap:
            self._reschedule(task, now + timedelta(days=1))
            return self._mark(task, "skipped_cap")
        # ④ 模板渲染：失败本轮跳过不外发残文
        text = self._render(task.round, tier)
        if text is None:
            with self._lock:
                self._tasks[task.task_uid] = replace(task, status="skipped", stop_reason="render_missing")
            return WinbackOutcome(task.task_uid, task.conversation_uid, task.round,
                                  "skipped_render")
        # ⑤ 过风控发送：拒绝 → 抖动重排（不丢失、不绕过）
        if not self._send(task.conversation_uid, text):
            self._reschedule(task, now + self._risk_retry)
            return self._mark(task, "rescheduled_risk", text=text)
        # ⑥ 发送成功：落账 + 续排 / 序列走完归档（停止条件④）
        with self._lock:
            self._tasks[task.task_uid] = replace(task, status="sent", sent_at=now)
        if task.round < len(self._sequence_days):
            base = self._sequence_base(task.conversation_uid)
            if base is not None:
                ref = self._ref_of(task)
                if ref is not None:
                    offset = self._sequence_days[task.round]
                    self._schedule(ref, round_no=task.round + 1,
                                   tier=min(task.round, 2), channel=task.channel,
                                   schedule_at=base + timedelta(days=offset))
                return WinbackOutcome(task.task_uid, task.conversation_uid, task.round,
                                      "sent", text)
        with self._lock:
            current = self._tasks[task.task_uid]
            self._tasks[task.task_uid] = replace(current, status="finished", stop_reason="exhausted")
        return WinbackOutcome(task.task_uid, task.conversation_uid, task.round,
                              "finished_exhausted", text)

    def _sent_today(self, account_id: int, now: datetime) -> int:
        """该账号当日已发送唤醒数（日上限判定）。"""
        with self._lock:
            return sum(1 for t in self._tasks.values()
                       if t.account_id == account_id and t.status == "sent"
                       and t.sent_at is not None and t.sent_at.date() == now.date())

    def _sequence_base(self, conversation_uid: str) -> datetime | None:
        """序列基准时刻（round=1 的 schedule_at）。"""
        with self._lock:
            for task in self._tasks.values():
                if task.conversation_uid == conversation_uid and task.round == 1:
                    return task.schedule_at
            return None

    def _ref_of(self, task: WinbackTask) -> ConversationRef | None:
        """由任务重建会话引用（续排建单参数）。"""
        return ConversationRef(
            conversation_uid=task.conversation_uid, account_id=task.account_id,
            customer_id=task.customer_id,
            channel="alibaba" if task.channel == "inmail" else "site")

    def _reschedule(self, task: WinbackTask, fire_at: datetime) -> None:
        """重排（审批等待/风控抖动/日上限顺延；保持同轮幂等键不变）。"""
        with self._lock:
            self._tasks[task.task_uid] = replace(task, schedule_at=fire_at, status="pending")

    def _mark(self, task: WinbackTask, result: str, *, stop_reason: str | None = None,
              text: str = "") -> WinbackOutcome:
        """取消终态落账 + 结果构造。"""
        if stop_reason is not None:
            with self._lock:
                self._tasks[task.task_uid] = replace(task, status="finished",
                                                     stop_reason=stop_reason)
        return WinbackOutcome(task.task_uid, task.conversation_uid, task.round, result, text)


__all__ = [
    "CHANNELS", "STATUSES", "ConversationRef", "WinbackSource", "InMemoryWinbackSource",
    "WinbackTask", "WinbackOutcome", "WinbackEngine",
]
