# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略 V2 ②分级驱动跟进：cadence 分级表 + 复用 V1 队列语义（FR-SC-003②，SR-SC-003 §6.2）。

- :class:`GradeCadence`——分级节奏表（A[1,2,5]/B[1,3,7]/C[2,5,10]/D[3,7,14]，§7 配置）
  + 轮次上限 + A/B 实验变体解析（resolve 链：变体 > 分级键）+ 热更新（epoch 递增）；
- :class:`GradeDrivenFollowup`——挂在 V1 :class:`FollowupScheduler` 上的分级扩展：
  每个分级键持有一个 V1 调度器实例（构造时注入该级 cadence/max_rounds），排程/
  摘除/触发全部走 V1 队列语义（zset 内存形态），V1 零改动；
- 分级变更重排（reschedule_for_grade）：旧 pending 摘除、已发轮次不补发、
  下一轮按新级节奏排程；A 级任务主管可见（digest 钩子）；D 级低频防骚扰。

demo 形态：进程内堆结构（生产 = Redis zset + PG followup_task 权威，见 SR-AG-004）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from services.agent.core.strategy.queue import (
    FireOutcome,
    FollowupScheduler,
    RenderFn,
    SendFn,
)

#: 默认分级节奏（SR-SC-003 §7 strategy.followup.cadence.A/.B/.C/.D）
DEFAULT_CADENCE: dict[str, tuple[int, ...]] = {
    "A": (1, 2, 5), "B": (1, 3, 7), "C": (2, 5, 10), "D": (3, 7, 14),
}
#: 默认分级轮次上限（A 深挖、D 防骚扰；SR-SC-003 §7）
DEFAULT_MAX_ROUNDS: dict[str, int] = {"A": 5, "B": 4, "C": 4, "D": 3}
#: A 级分级键前缀（A/B 实验变体白名单键形态：strategy.followup.cadence.{GRADE}）
CADENCE_KEY_PREFIX = "strategy.followup.cadence"


class CadenceError(Exception):
    """分级节奏域异常（未知分级/越界轮次）。"""


@dataclass(frozen=True)
class ReschedulePlan:
    """分级变更重排计划（取消数/已发轮次/新节奏下一轮间隔）。"""

    cancelled: int
    done_rounds: int
    delay_days: int | None
    rescheduled_round: int | None


class GradeCadence:
    """分级节奏表：取值 / 变体解析 / 热更新（SR-SC-003 §6.2 cadence_for + §6.5 resolve 链）。

    :param cadence: 分级 → 天数数组（默认 DEFAULT_CADENCE）；
    :param max_rounds: 分级 → 轮次上限；
    :param variant_resolver: A/B 变体解析回调 ``(key, account_id) → 值|None``，
        仅 running 实验的处理组命中时返回覆盖值（实验域注入，§6.5）。
    """

    def __init__(
        self,
        *,
        cadence: dict[str, tuple[int, ...]] | None = None,
        max_rounds: dict[str, int] | None = None,
        variant_resolver: Callable[[str, int | None], tuple[int, ...] | None] | None = None,
    ) -> None:
        self._cadence: dict[str, tuple[int, ...]] = dict(cadence) if cadence else dict(DEFAULT_CADENCE)
        self._max_rounds: dict[str, int] = dict(max_rounds) if max_rounds else dict(DEFAULT_MAX_ROUNDS)
        self._variant_resolver = variant_resolver
        self._lock = threading.RLock()
        self._epoch = 0                                # 热更新纪元（配置版本）

    # ---------------------------------------------------------------- 取值

    def cadence_for(self, grade: str, *, account_id: int | None = None) -> tuple[int, ...]:
        """分级节奏取值链：A/B 变体 > 分级键（未知分级拒绝）。"""
        if grade not in self._cadence:
            raise CadenceError(f"未知分级 {grade}（仅 {'/'.join(self._cadence)}）")
        if self._variant_resolver is not None:
            override = self._variant_resolver(f"{CADENCE_KEY_PREFIX}.{grade}", account_id)
            if override:
                return tuple(override)
        with self._lock:
            return self._cadence[grade]

    def max_rounds_for(self, grade: str) -> int:
        """分级轮次上限。"""
        if grade not in self._max_rounds:
            raise CadenceError(f"未知分级 {grade}")
        with self._lock:
            return self._max_rounds[grade]

    def delay_days(self, grade: str, round_no: int, *, account_id: int | None = None) -> int:
        """第 round_no 轮（1 起）距上一轮的天数；越界拒绝（由 max_rounds 兜底转人工）。"""
        cadence = self.cadence_for(grade, account_id=account_id)
        if not 1 <= round_no <= len(cadence):
            raise CadenceError(f"{grade} 级第 {round_no} 轮超出节奏表（共 {len(cadence)} 轮）")
        return cadence[round_no - 1]

    def next_fire_at(self, grade: str, *, done_rounds: int, from_time: datetime,
                     account_id: int | None = None) -> datetime | None:
        """已发 done_rounds 轮后的下一轮触发时刻；节奏走完返回 None（静默归档）。"""
        cadence = self.cadence_for(grade, account_id=account_id)
        if done_rounds >= len(cadence) or done_rounds + 1 > self.max_rounds_for(grade):
            return None
        return from_time + timedelta(days=cadence[done_rounds])

    # ---------------------------------------------------------------- 热更新

    def update_cadence(self, grade: str, days: tuple[int, ...] | list[int]) -> None:
        """热更新分级节奏（app_config 改参免发版；epoch 递增使新排程取新值）。"""
        if grade not in self._cadence:
            raise CadenceError(f"未知分级 {grade}")
        with self._lock:
            self._cadence[grade] = tuple(int(d) for d in days)
            self._epoch += 1

    def update_max_rounds(self, grade: str, rounds: int) -> None:
        """热更新轮次上限。"""
        if grade not in self._max_rounds:
            raise CadenceError(f"未知分级 {grade}")
        with self._lock:
            self._max_rounds[grade] = int(rounds)
            self._epoch += 1

    @property
    def epoch(self) -> int:
        """当前配置纪元（GradeDrivenFollowup 调度器缓存失效依据）。"""
        with self._lock:
            return self._epoch


def _days(n: int) -> timedelta:
    """天数 → timedelta（排程偏移量）。"""
    return timedelta(days=n)


class GradeDrivenFollowup:
    """分级驱动跟进调度（复用 V1 FollowupScheduler 队列语义，SR-SC-003 §6.2）。

    - 每个分级键一个 V1 调度器实例（注入该级 cadence/max_rounds/render/send）；
    - 热更新后新会话落新纪元调度器（新节奏），在途任务留在旧调度器按旧节奏消费
      （不丢任务）；分级变更走 :meth:`reschedule_for_grade` 显式重排；
    - A 级任务主管可见：排程即时触发 ``on_a_visible`` 钩子（告警摘要承载）。
    """

    def __init__(
        self,
        cadence: GradeCadence,
        grade_of: Callable[[str], str],
        *,
        render: RenderFn | None = None,
        send: SendFn | None = None,
        has_replied: Callable[[str], bool] | None = None,
        on_exhausted: Callable[[str], None] | None = None,
        on_a_visible: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.cadence = cadence
        self._grade_of = grade_of
        self._render = render
        self._send = send
        self._has_replied = has_replied
        self._on_exhausted = on_exhausted
        self._on_a_visible = on_a_visible or (lambda uid: None)
        self._clock = clock or (lambda: datetime.now())
        self._lock = threading.RLock()
        # (grade, epoch) → V1 调度器（旧纪元调度器保留消费在途任务）
        self._schedulers: dict[tuple[str, int], FollowupScheduler] = {}
        self._conv_scheduler: dict[str, FollowupScheduler] = {}   # 会话 → 所属调度器
        self._sent_rounds: dict[str, set[int]] = {}               # 会话 → 已发轮次集合

    # ---------------------------------------------------------------- 调度器供给

    def _scheduler_for(self, grade: str) -> FollowupScheduler:
        """按（分级, 当前纪元）取/建 V1 调度器（注入该级 cadence 与上限）。"""
        key = (grade, self.cadence.epoch)
        with self._lock:
            scheduler = self._schedulers.get(key)
            if scheduler is None:
                # V1 队列不变式：cadence 数组长度即轮次上限上界（超出部分取末段间隔无依据），
                # 故 V2 上限取 min(分级轮次上限, 节奏表长度)——A 深挖/D 防骚扰均在此收敛。
                cadence = self.cadence.cadence_for(grade)
                scheduler = FollowupScheduler(
                    cadence=cadence,
                    max_rounds=min(self.cadence.max_rounds_for(grade), len(cadence)),
                    render=self._render, send=self._send,
                    has_replied=self._has_replied,
                    on_exhausted=self._on_exhausted,
                    clock=self._clock,
                )
                self._schedulers[key] = scheduler
            return scheduler

    @property
    def schedulers(self) -> list[FollowupScheduler]:
        """全部在册 V1 调度器（结构性断言/观测面）。"""
        with self._lock:
            return list(self._schedulers.values())

    # ---------------------------------------------------------------- 排程入口

    def schedule_first(self, conversation_uid: str, *, now: datetime | None = None,
                       grade: str | None = None) -> str:
        """登记首轮跟进（发送成功回调语义）：按分级路由，触发时刻 = now + cadence[0]。"""
        moment = now if now is not None else self._clock()
        resolved = grade or self._grade_of(conversation_uid)
        scheduler = self._scheduler_for(resolved)
        task_uid = scheduler.on_reply_sent(conversation_uid, now=moment)
        with self._lock:
            self._conv_scheduler[conversation_uid] = scheduler
        if resolved == "A":
            self._on_a_visible(conversation_uid)       # A 级主管可见（看板/摘要告警）
        return task_uid

    def reschedule_for_grade(self, conversation_uid: str, new_grade: str, *,
                             now: datetime | None = None) -> ReschedulePlan:
        """分级变更重排（SR-SC-003 §6.2）：旧 pending 摘除；已发轮次不补发；
        下一轮按新级节奏（cadence[done]）排入新分级调度器。"""
        moment = now if now is not None else self._clock()
        with self._lock:
            old = self._conv_scheduler.get(conversation_uid)
            cancelled = old.remove(conversation_uid) if old is not None else 0
            done = len(self._sent_rounds.get(conversation_uid, ()))
        new_scheduler = self._scheduler_for(new_grade)
        cadence = self.cadence.cadence_for(new_grade)
        if done < len(cadence) and done + 1 <= self.cadence.max_rounds_for(new_grade):
            new_scheduler.schedule(
                conversation_uid, round_no=done + 1,
                fire_at=moment + timedelta(days=cadence[done]),
            )
            with self._lock:
                self._conv_scheduler[conversation_uid] = new_scheduler
            if new_grade == "A":
                self._on_a_visible(conversation_uid)
            return ReschedulePlan(cancelled, done, cadence[done], done + 1)
        with self._lock:
            self._conv_scheduler[conversation_uid] = new_scheduler
        return ReschedulePlan(cancelled, done, None, None)

    # ---------------------------------------------------------------- 消费与观测

    def fire_due(self, *, now: datetime | None = None) -> list[FireOutcome]:
        """到期任务触发（跨全部在册调度器汇总；V1 _fire 语义：复核/渲染/发送/续排）。"""
        moment = now if now is not None else self._clock()
        with self._lock:
            schedulers = list(self._schedulers.values())
        outcomes: list[FireOutcome] = []
        for scheduler in schedulers:
            outcomes.extend(scheduler.fire_due(now=moment))
        for outcome in outcomes:
            if outcome.result == "sent":
                with self._lock:
                    self._sent_rounds.setdefault(outcome.conversation_uid, set()).add(outcome.round_no)
        return sorted(outcomes, key=lambda o: (o.conversation_uid, o.round_no))

    def pending(self, conversation_uid: str) -> int:
        """该会话在途 pending 任务数（观测面）。"""
        with self._lock:
            scheduler = self._conv_scheduler.get(conversation_uid)
        if scheduler is None:
            return 0
        return sum(1 for _, task in scheduler.peek() if task.conversation_uid == conversation_uid)

    def sent_rounds(self, conversation_uid: str) -> tuple[int, ...]:
        """该会话已发送轮次（升序）。"""
        with self._lock:
            return tuple(sorted(self._sent_rounds.get(conversation_uid, ())))


__all__ = [
    "DEFAULT_CADENCE", "DEFAULT_MAX_ROUNDS", "CADENCE_KEY_PREFIX",
    "CadenceError", "GradeCadence", "ReschedulePlan", "GradeDrivenFollowup",
]
