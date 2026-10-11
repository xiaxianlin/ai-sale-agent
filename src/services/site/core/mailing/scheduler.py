# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004 邮件自动化·调度器：触发排产 → zset 延迟发送 → 前置门禁 → SMTP（SR §6.2/§6.3）。

模块职责：
- :class:`MemoryZSet`——Redis ZSet 语义的内存实现（zadd/pop_due 原子弹出语义），
  复用 SR-AG-004 延迟队列模式：权威源（MailStore）先行、索引（sendq）后写、
  :meth:`MailScheduler.rehydrate` 崩溃回灌兜底（NFR-REL-03 不丢不重）；
- :class:`DomainRateGuard`——发信节奏限制：每域每小时上限（送达率工程三件套①），
  超额任务顺延退避，不外发；
- :class:`MailScheduler`——触发入口 :meth:`on_event`（缺邮箱登记缺口 / dedup 幂等）；
  :meth:`poll` 到期触发，逐任务执行门禁链：终态复核（竞态兜底）→ 抑制名单 →
  停止条件探针 → 节奏限制 → 模板沙箱渲染（缺变量拒绝外发）→ SMTP 发送
  （250=送达口径；软失败退避重试）→ 状态推进/序列收口。

SMTP 以 :class:`MailTransport` 协议注入（demo 用确定性 Fake，生产 aiosmtpd/服务商 API）。
依据文档：docs/07-实现方案/SITE/SR-SITE-004-邮件自动化.md §6.2/§6.3。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from services.site.core.mailing.sequences import MailJob, MailStore, Sequence, SequenceEngine
from services.site.core.mailing.templates import TemplateError, TemplateRegistry, inject_unsubscribe
from services.site.core.mailing.tracking import SuppressionList


def domain_of(email: str) -> str:
    """收件人域名（节奏限制的限流维度，小写）。"""
    return email.rpartition("@")[2].lower()


class MemoryZSet:
    """ZSet 语义内存实现：member=job_id，score=计划发送 epoch；pop_due 为原子弹出语义。"""

    def __init__(self) -> None:
        self._items: dict[int, float] = {}

    def zadd(self, member: int, score: float) -> None:
        """加入/更新（同 member 覆盖 score，幂等）。"""
        self._items[member] = score

    def pop_due(self, now_ts: float) -> list[int]:
        """弹出全部 score <= now_ts 的成员（按 score 升序，同分按 member 序）。"""
        due = sorted((s, m) for m, s in self._items.items() if s <= now_ts)
        for _, m in due:
            self._items.pop(m, None)
        return [m for _, m in due]

    def remove(self, member: int) -> None:
        """移除成员（任务终态后清理索引）。"""
        self._items.pop(member, None)

    def size(self) -> int:
        """当前索引长度。"""
        return len(self._items)


class DomainRateGuard:
    """发信节奏限制：单域每小时发送上限（桶 = UTC 整点窗口）。"""

    def __init__(self, per_domain_per_hour: int = 500) -> None:
        self.per_domain_per_hour = per_domain_per_hour
        self._counters: dict[tuple[str, int], int] = {}

    def try_acquire(self, domain: str, now: datetime) -> bool:
        """申请一个发送额度：桶内计数未达上限才放行（超额由调度器顺延退避）。"""
        bucket = (domain.lower(), int(now.timestamp() // 3600))
        used = self._counters.get(bucket, 0)
        if used >= self.per_domain_per_hour:
            return False
        self._counters[bucket] = used + 1
        return True


class MailTransport(Protocol):
    """SMTP 发送协议（demo 以 FakeSmtpTransport 注入；生产为 aiosmtpd/服务商 SDK 形态）。"""

    def send(self, to: str, subject: str, html: str, headers: dict[str, str]) -> str:
        """发送并返回 Message-ID；软失败抛 SmtpSoftError（非 250 一律软失败，SR §6.3）。"""
        ...


class SmtpSoftError(Exception):
    """SMTP 软失败（4xx/超时）：仅重试，不拉黑不视为送达。"""


@dataclass(frozen=True)
class MailEvent:
    """触发事件（FR-SITE-002 弃购上报 / 报价创建 / 节日日历批次的统一入口形态）。"""

    seq_type: str
    customer_id: int
    email: str | None
    lang: str = "en"
    triggered_at: datetime | None = None
    context: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EnrollOutcome:
    """事件受理结果：created=False 时 sequence 可为已存在序列（幂等重放）。"""

    created: bool
    sequence: Sequence | None
    jobs: list[MailJob] = field(default_factory=list)


@dataclass(frozen=True)
class FiredOutcome:
    """单任务触发结果（action 口径见各分支）。"""

    job_id: int
    # action: sent / skipped_terminal / skipped_suppressed / skipped_stop /
    #         deferred_rate / deferred_retry / failed_template / failed_retry
    action: str
    detail: str | None = None


# 扩展点（SR §6.3 三件套②）：域名预热档表在 RateGuard 之上按日套用 current_cap，
# 接入时以装饰器包装 DomainRateGuard，协议不变（try_acquire 语义）。
class MailScheduler:
    """邮件调度器：事件 → 全步骤排产 → zset 延迟触发 → 门禁链 → SMTP → 状态推进。"""

    def __init__(self, *, engine: SequenceEngine, store: MailStore, transport: MailTransport,
                 templates: TemplateRegistry, suppression: SuppressionList,
                 rate_guard: DomainRateGuard, clock: Callable[[], datetime],
                 rate_backoff_min: int = 30, store_url: str = "https://shop.example.com",
                 unsub_base_url: str = "https://shop.example.com/s/m/u",
                 stop_lookup: Callable[[Sequence], str | None] | None = None) -> None:
        self.engine = engine
        self.store = store
        self.transport = transport
        self.templates = templates
        self.suppression = suppression
        self.rate_guard = rate_guard
        self.clock = clock
        self.rate_backoff_min = rate_backoff_min
        self.store_url = store_url
        self.unsub_base_url = unsub_base_url
        self.stop_lookup = stop_lookup
        self.sendq = MemoryZSet()
        self.alerts: list[dict] = []  # 告警登记（模板失效等，SR §6.2 alert.push 占位）

    # ---------------------------------------------------------------- 事件入口

    def on_event(self, evt: MailEvent) -> EnrollOutcome:
        """触发入口：无邮箱登记缺口；dedup 幂等；全步骤一次排产（权威源先行、索引后写）。"""
        if not evt.email:
            self.store.email_gaps.append(evt)  # 等 FR-SITE-005 合并出邮箱后再触发
            return EnrollOutcome(False, None)
        res = self.engine.enroll(evt.seq_type, evt.customer_id, evt.email, evt.context,
                                 lang=evt.lang, triggered_at=evt.triggered_at or self.clock())
        if not res.created:
            return EnrollOutcome(False, res.sequence)
        jobs: list[MailJob] = []
        for step_no, step, fire_at in self.engine.fire_times(
                evt.seq_type, res.sequence.created_at or self.clock(), evt.context):
            job = self.store.add_job(sequence_id=res.sequence.seq_id, seq_type=evt.seq_type,
                                     step_no=step_no, template_key=step.template_key,
                                     recipient=evt.email, lang=evt.lang, schedule_at=fire_at)
            self.sendq.zadd(job.job_id, fire_at.timestamp())
            jobs.append(job)
        return EnrollOutcome(True, res.sequence, jobs)

    # ---------------------------------------------------------------- 触发循环

    def poll(self, now: datetime | None = None) -> list[FiredOutcome]:
        """到期触发（等价 run_loop 单拍）：原子弹出保证单副本消费语义。"""
        moment = now or self.clock()
        return [self._fire(job_id) for job_id in self.sendq.pop_due(moment.timestamp())]

    def rehydrate(self) -> int:
        """崩溃回灌：全部 queued 任务重建 zset 索引（不丢不重，NFR-REL-03）。"""
        count = 0
        for job in self.store.jobs.values():
            if job.status == "queued":
                self.sendq.zadd(job.job_id, job.schedule_at.timestamp())
                count += 1
        return count

    # ---------------------------------------------------------------- 单任务门禁链

    def _fire(self, job_id: int) -> FiredOutcome:
        """单任务触发：终态复核 → 抑制名单 → 停止探针 → 节奏 → 渲染 → 发送 → 推进。"""
        job = self.store.jobs.get(job_id)
        seq = self.store.sequences.get(job.sequence_id) if job else None
        if job is None or seq is None or job.status != "queued" or seq.status != "active":
            # 触发时复核（竞态兜底①）：序列已终态 → 剩余任务批量取消（SR §6.2 _stop 语义）
            if seq is not None and seq.status != "active":
                self.store.cancel_pending_jobs(seq.seq_id, seq.stop_reason or "terminal")
            return FiredOutcome(job_id, "skipped_terminal", "触发时复核：任务非 queued 或序列已终态")
        now = self.clock()
        # ② 抑制名单（FR-SITE-007：退订/硬退信后不再外发，序列按退订口径停止）
        if self.suppression.contains(job.recipient):
            self.engine.apply_event(seq.seq_id, "unsubscribed")
            return FiredOutcome(job_id, "skipped_suppressed", "收件人在抑制名单")
        # ③ 停止条件探针（已回购/已回复等竞态兜底，事件回写主路径在 DeliveryTracker）
        if self.stop_lookup is not None and (reason := self.stop_lookup(seq)):
            self.engine.apply_event(seq.seq_id, reason)
            return FiredOutcome(job_id, "skipped_terminal", f"停止条件命中：{reason}")
        # ④ 节奏限制：超额顺延退避，不外发
        if not self.rate_guard.try_acquire(domain_of(job.recipient), now):
            self.sendq.zadd(job_id, (now + timedelta(minutes=self.rate_backoff_min)).timestamp())
            return FiredOutcome(job_id, "deferred_rate", "每域每小时配额用尽，顺延")
        # ⑤ 模板沙箱渲染：缺失变量/非法模板 → 任务失败 + 告警，拒绝外发残文
        try:
            subject, html = self.templates.render(job.template_key, job.lang, self._vars(seq, job))
            html = inject_unsubscribe(html, self._unsub_url(job))
        except TemplateError as exc:
            self.store.jobs[job_id].status = "failed"
            self.store.jobs[job_id].fail_reason = f"template:{exc}"
            self.alerts.append({"kind": "mail_tpl_invalid", "job_id": job_id, "detail": str(exc)})
            return FiredOutcome(job_id, "failed_template", str(exc))
        # ⑥ SMTP 发送：软失败退避重试（仅一次，再次失败判失败）
        try:
            message_id = self.transport.send(job.recipient, subject, html, self._headers(job))
        except SmtpSoftError as exc:
            job.retry_count += 1
            if job.retry_count > 1:
                job.status = "failed"
                job.fail_reason = f"smtp_soft:{exc}"
                return FiredOutcome(job_id, "failed_retry", str(exc))
            self.sendq.zadd(job_id, (now + timedelta(minutes=self.rate_backoff_min)).timestamp())
            return FiredOutcome(job_id, "deferred_retry", str(exc))
        # ⑦ 250=服务商接收（送达口径）→ 状态推进 + 序列步骤收口
        job.status = "delivered"
        job.sent_at = now
        job.message_id = message_id
        self.engine.mark_step_sent(seq)
        return FiredOutcome(job_id, "sent")

    # ---------------------------------------------------------------- 组装辅助

    def _vars(self, seq: Sequence, job: MailJob) -> dict:
        """渲染变量组装：
        - 恒可用槽位：email/store_url（系统侧必有）、customer_name（客户号兜底，非业务缺失）；
        - 业务槽位（cart_items/coupon_code/quote_no/expiry）：context 提供才注入——
          缺席即「真缺失」，模板引用时经沙箱拒绝外发（不喂空串残文，SR §6.5）。"""
        variables: dict = {"email": seq.email, "store_url": self.store_url,
                           "customer_name": seq.context.get("customer_name", f"客户#{seq.customer_id}")}
        for key in ("cart_items", "coupon_code", "quote_no", "expiry"):
            if key in seq.context:
                variables[key] = seq.context[key]
        return variables

    def _unsub_url(self, job: MailJob) -> str:
        """退订链接（token 形态，URL 不含明文 PII；demo token=job 摘要）。"""
        return f"{self.unsub_base_url}/{job.job_id:08d}"

    def _headers(self, job: MailJob) -> dict[str, str]:
        """合规头域：List-Unsubscribe（RFC 8058 one-click）+ bulk 标记（SR §6.3）。"""
        unsub = self._unsub_url(job)
        return {
            "List-Unsubscribe": f"<{unsub}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            "Precedence": "bulk",
        }
