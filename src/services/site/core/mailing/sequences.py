# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004 邮件自动化·三类序列状态机（SR-SITE-004 §6.1，Phase 3 第二批）。

模块职责：
- :class:`SequenceDef` / ``DEFAULT_SEQUENCE_DEFS``——三类内置序列（弃购挽回 1h/24h/72h、
  报价跟进 D+3/D+7/D+14、节日营销单步）的步骤/模板/停止条件定义，全配置化
  （overrides 覆盖式合并，对齐 app_config scope='channel' 免发版调参，NFR-MAI-02）；
- ``LEGAL_TRANSITIONS``——状态机合法迁移矩阵（唯一权威）：active→active（步骤推进）/
  active→done（末步已发）/ active→stopped（命中停止条件），终端态一律不可再迁移；
- :class:`SequenceEngine`——序列开启（dedup_key 幂等，对齐 mail_sequence UNIQUE 约束）、
  步骤排产时刻计算、停止条件执行（stop_reason 白名单校验 + 剩余任务批量取消）、
  成交归因口径的序列完成判定。

数据形态：源 mail_sequence / mail_job（02-数据库设计 §3.4 + SR-SITE-004 §4 增补），
demo 以 :class:`MailStore` 内存实现承载，生产替换 PG 形态、协议不变。
依据文档：docs/07-实现方案/SITE/SR-SITE-004-邮件自动化.md §2.1/§6.1。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

#: 序列状态（SR §4 mail_sequence.status CHECK 口径）
STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_STOPPED = "stopped"

#: 终态集合（进入后不可再迁移）
TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_STOPPED})

#: 合法迁移矩阵（唯一权威口径；终端态的任何迁出均非法）
LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    (STATUS_ACTIVE, STATUS_ACTIVE),   # 步骤逐封发送（步骤推进）
    (STATUS_ACTIVE, STATUS_DONE),     # 末步已发
    (STATUS_ACTIVE, STATUS_STOPPED),  # 命中停止条件
})

#: 合法停止原因全集（SR §4 stop_reason 注释口径）
ALL_STOP_REASONS = frozenset({"repurchased", "replied", "won", "unsubscribed", "hard_bounce"})


class IllegalTransition(Exception):
    """非法状态迁移（终端态再迁移 / stop_reason 不在 stop_on 白名单）。"""


@dataclass(frozen=True)
class Step:
    """序列单步定义：偏移小时数 + 模板键（festival 步以 at='calendar' 表示日历铺开）。"""

    offset_h: float | None
    template_key: str
    incentive: str | None = None
    at: str | None = None  # 'calendar' = 由触发事件的 context['fire_at'] 给定绝对时刻


@dataclass(frozen=True)
class SequenceDef:
    """序列定义：步骤表 + 停止条件白名单（配置覆盖的单位）。"""

    seq_type: str
    steps: tuple[Step, ...]
    stop_on: frozenset[str]

    def with_overrides(self, patch: dict) -> "SequenceDef":
        """配置覆盖（覆盖式合并：steps / stop_on 可整体替换，免发版，NFR-MAI-02）。"""
        steps = patch.get("steps", self.steps)
        parsed = tuple(Step(**s) if isinstance(s, dict) else s for s in steps)
        return SequenceDef(self.seq_type, parsed, frozenset(patch.get("stop_on", self.stop_on)))


def _cart_recover() -> SequenceDef:
    """弃购挽回：1h 提醒 → 24h 优惠（附券）→ 72h 最后召唤（SR §2.1）。"""
    return SequenceDef("cart_recover", (
        Step(1, "cart_recover_remind"),
        Step(24, "cart_recover_offer", incentive="coupon_5pct"),
        Step(72, "cart_recover_last", incentive="last_call"),
    ), frozenset({"repurchased", "replied", "unsubscribed", "hard_bounce"}))


def _quote_follow() -> SequenceDef:
    """报价跟进：D+3 跟进 → D+7 价值要点 → D+14 收尾。"""
    return SequenceDef("quote_follow", (
        Step(72, "quote_follow_touch"),
        Step(168, "quote_follow_value"),
        Step(336, "quote_follow_close"),
    ), frozenset({"replied", "won", "unsubscribed", "hard_bounce"}))


def _festival() -> SequenceDef:
    """节日营销：单步群发（触发时刻由节日日历按客户时区铺开给出）。"""
    return SequenceDef("festival", (Step(None, "festival_greet", at="calendar"),),
                       frozenset({"unsubscribed", "hard_bounce"}))


#: 三类内置序列定义（只读；运行时经 SequenceEngine.overrides 覆盖）
DEFAULT_SEQUENCE_DEFS: dict[str, SequenceDef] = {
    "cart_recover": _cart_recover(),
    "quote_follow": _quote_follow(),
    "festival": _festival(),
}


# --------------------------------------------------------------------------- 存储形态


@dataclass
class Sequence:
    """序列实例（mail_sequence 形态；状态机权威源）。"""

    seq_id: int
    seq_type: str
    customer_id: int
    email: str
    lang: str = "en"
    context: dict = field(default_factory=dict)
    status: str = STATUS_ACTIVE
    stop_reason: str | None = None
    steps_sent: int = 0
    created_at: datetime | None = None

    @property
    def dedup_key(self) -> str | None:
        """防重开键（同购物车/报价单/战役只开一次）。"""
        return self.context.get("dedup_key")


@dataclass
class MailJob:
    """邮件任务（mail_job 形态：job_type/status CHECK、tracked_at 事件戳沿用 §3.4）。"""

    job_id: int
    sequence_id: int
    seq_type: str
    step_no: int
    template_key: str
    recipient: str
    schedule_at: datetime
    lang: str = "en"
    status: str = "queued"  # queued → delivered → opened；failed/cancelled 终态
    sent_at: datetime | None = None
    message_id: str | None = None
    retry_count: int = 0
    fail_reason: str | None = None
    tracked: dict = field(default_factory=lambda: {
        "opens": [], "bounces": [], "reply_at": None, "order_at": None})


class MailStore:
    """邮件域内存存储（demo 形态；生产替换 PG：mail_sequence/mail_job 两表）。"""

    def __init__(self) -> None:
        self.sequences: dict[int, Sequence] = {}
        self.jobs: dict[int, MailJob] = {}
        self.email_gaps: list = []  # 无留资邮箱事件缺口登记（SR §6.2 record_no_lead）
        self._seq_id = 0
        self._job_id = 0

    def add_sequence(self, seq: Sequence) -> Sequence:
        """新序列入库并分配 id。"""
        self._seq_id += 1
        seq.seq_id = self._seq_id
        self.sequences[seq.seq_id] = seq
        return seq

    def add_job(self, *, sequence_id: int, seq_type: str, step_no: int, template_key: str,
                recipient: str, schedule_at: datetime, lang: str = "en") -> MailJob:
        """新任务入库并分配 id（排产单位）。"""
        self._job_id += 1
        job = MailJob(job_id=self._job_id, sequence_id=sequence_id, seq_type=seq_type,
                      step_no=step_no, template_key=template_key, recipient=recipient,
                      schedule_at=schedule_at, lang=lang)
        self.jobs[job.job_id] = job
        return job

    def jobs_of(self, seq_id: int) -> list[MailJob]:
        """序列的全部任务（按步骤号排序）。"""
        return sorted((j for j in self.jobs.values() if j.sequence_id == seq_id),
                      key=lambda j: j.step_no)

    def pending_jobs(self, seq_id: int) -> list[MailJob]:
        """序列剩余 queued 任务（停止时批量取消的对象）。"""
        return [j for j in self.jobs_of(seq_id) if j.status == "queued"]

    def cancel_pending_jobs(self, seq_id: int, reason: str) -> int:
        """批量取消剩余任务（序列停止联动）；返回取消数。"""
        cancelled = 0
        for job in self.pending_jobs(seq_id):
            job.status = "cancelled"
            job.fail_reason = reason
            cancelled += 1
        return cancelled

    def sequences_for_email(self, email: str) -> list[Sequence]:
        """按收件人取全部序列（退信/退订停全量序列用）。"""
        return [s for s in self.sequences.values() if s.email.lower() == email.lower()]

    def job_by_message_id(self, message_id: str) -> MailJob | None:
        """按 Message-ID 反查任务（IMAP DSN/回复关联，SR §6.4）。"""
        return next((j for j in self.jobs.values() if j.message_id == message_id), None)

    def last_sent_job(self, email: str) -> MailJob | None:
        """收件人最近一封已发出（送达口径）的任务（回复归因落点）。"""
        sent = [j for j in self.jobs.values()
                if j.recipient.lower() == email.lower() and j.sent_at is not None]
        return max(sent, key=lambda j: (j.sent_at, j.job_id), default=None)


# --------------------------------------------------------------------------- 状态机引擎


@dataclass(frozen=True)
class EnrollResult:
    """开启结果：created=False 表示同 dedup_key 已开（幂等重放）。"""

    created: bool
    sequence: Sequence


class SequenceEngine:
    """序列状态机引擎：定义解析 + 开启幂等 + 迁移执行（纯逻辑，不含 IO）。"""

    def __init__(self, store: MailStore, *, clock: Callable[[], datetime],
                 overrides: dict[str, dict] | None = None) -> None:
        self.store = store
        self.clock = clock
        self.overrides = overrides or {}

    def seq_def(self, seq_type: str) -> SequenceDef:
        """生效定义 = 内置默认 + overrides 覆盖（对齐 seq_def 的 app_config 合并语义）。"""
        base = DEFAULT_SEQUENCE_DEFS[seq_type]
        patch = self.overrides.get(seq_type)
        return base.with_overrides(patch) if patch else base

    def enroll(self, seq_type: str, customer_id: int, email: str, context: dict | None = None,
               *, lang: str = "en", triggered_at: datetime | None = None) -> EnrollResult:
        """开启序列：UNIQUE (seq_type, customer, dedup_key) 幂等；重复触发返回已有序列。"""
        context = context or {}
        dedup = context.get("dedup_key")
        for seq in self.store.sequences.values():
            if (seq.seq_type == seq_type and seq.customer_id == customer_id
                    and seq.context.get("dedup_key") == dedup):
                return EnrollResult(False, seq)
        seq = self.store.add_sequence(Sequence(
            seq_id=0, seq_type=seq_type, customer_id=customer_id, email=email, lang=lang,
            context=dict(context), created_at=triggered_at or self.clock()))
        return EnrollResult(True, seq)

    def fire_times(self, seq_type: str, triggered_at: datetime,
                   context: dict | None = None) -> list[tuple[int, Step, datetime]]:
        """全步骤一次排产的触发时刻：triggered_at + offset（festival 步取 context['fire_at']）。"""
        context = context or {}
        out: list[tuple[int, Step, datetime]] = []
        for no, step in enumerate(self.seq_def(seq_type).steps, start=1):
            if step.at == "calendar":
                fire_at = context.get("fire_at") or triggered_at
            else:
                fire_at = triggered_at + timedelta(hours=float(step.offset_h or 0))
            out.append((no, step, fire_at))
        return out

    def due_jobs(self, seq: Sequence, now: datetime | None = None) -> list[MailJob]:
        """到期待发任务（schedule_at <= now，按步骤号序）。"""
        moment = now or self.clock()
        return [j for j in self.store.pending_jobs(seq.seq_id) if j.schedule_at <= moment]

    def _assert_transition(self, src: str, dst: str) -> None:
        """迁移矩阵校验：非法迁移一律 IllegalTransition（先于任何副作用）。"""
        if src in TERMINAL_STATUSES or (src, dst) not in LEGAL_TRANSITIONS:
            raise IllegalTransition(f"非法状态迁移：{src} → {dst}")

    def stop(self, seq_id: int, reason: str) -> Sequence:
        """停止序列：reason 须在该序列 stop_on 白名单内；剩余任务批量取消（SR §6.2 _stop）。"""
        seq = self.store.sequences[seq_id]
        if reason not in ALL_STOP_REASONS:
            raise IllegalTransition(f"未知停止原因：{reason}")
        self._assert_transition(seq.status, STATUS_STOPPED)
        if reason not in self.seq_def(seq.seq_type).stop_on:
            raise IllegalTransition(f"{seq.seq_type} 序列不允许以 {reason} 停止（stop_on 白名单外）")
        seq.status = STATUS_STOPPED
        seq.stop_reason = reason
        self.store.cancel_pending_jobs(seq_id, reason)
        return seq

    def apply_event(self, seq_id: int, reason: str) -> bool:
        """便捷入口：命中停止条件才停，否则 no-op（不抛错，供事件回写循环调用）。"""
        seq = self.store.sequences[seq_id]
        if seq.status != STATUS_ACTIVE or reason not in self.seq_def(seq.seq_type).stop_on:
            return False
        self.stop(seq_id, reason)
        return True

    def mark_step_sent(self, seq: Sequence) -> None:
        """步骤已发出：steps_sent+1；末步已发 → done（max_step 语义，归因窗口仍开）。"""
        self._assert_transition(seq.status, STATUS_ACTIVE)
        seq.steps_sent += 1
        if seq.steps_sent >= len(self.seq_def(seq.seq_type).steps):
            self._assert_transition(seq.status, STATUS_DONE)
            seq.status = STATUS_DONE
