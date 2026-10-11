# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004 邮件自动化·状态追踪/退信分级/抑制名单/统计口径（SR §6.4，Phase 3 第二批）。

模块职责：
- :class:`SuppressionList`——退订与抑制名单（mail_suppression 形态）：仅存
  SHA-256(小写邮箱) 哈希，明文不留存（NFR-SEC-01）；发送前强制过滤的权威数据源；
- :class:`DeliveryTracker`——事件驱动追踪（送达回执/打开像素/DSN 退信/客户回复/退订/订单），
  对齐「服务商 webhook + IMAP 轮询」两类入口的统一处理内核：
  送达→状态推进；打开→30 天去重只计首次；硬退（5.x.x）→ 即时入名单 + 停全部序列；
  软退（4.x.x）→ 连续 3 次降级入名单；回复/退订 → 停序列 + 归因回写；
- :meth:`DeliveryTracker.stats` —— 统计口径（发送→送达）：sent = 已离开队列
  （delivered+opened+failed，failed 计入分母并单列失败率）；delivered = {delivered, opened}
  （状态单调推进不重复计数）；queued/cancelled 不入任何分子分母；
- :meth:`DeliveryTracker.passes_delivery_rate` —— 送达率 ≥95% 判定函数（SRS §16.4 验收 KPI）。

SMTP/IMAP 以确定性事件注入（Fake/webhook 形态），断言不依赖真实网络。
依据文档：docs/07-实现方案/SITE/SR-SITE-004-邮件自动化.md §2.2/§6.4。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from services.site.core.mailing.sequences import MailJob, MailStore, SequenceEngine


@dataclass(frozen=True)
class SuppressionEntry:
    """抑制名单条目：email_hash=SHA-256(小写邮箱)（NFR-SEC-01，明文不落存储）。"""

    email_hash: str
    reason: str  # unsubscribe / hard_bounce / complaint / expired_consent
    source: str
    created_at: datetime


class SuppressionList:
    """退订与抑制名单（发送前强制过滤；退订链接/DSN/FR-SITE-007 同治）。"""

    def __init__(self) -> None:
        self._by_hash: dict[str, SuppressionEntry] = {}

    @staticmethod
    def hash_email(email: str) -> str:
        """小写化后 SHA-256（与 §4 mail_suppression.email_hash 口径一致）。"""
        return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()

    def add(self, email: str, reason: str, source: str = "mail",
            now: datetime | None = None) -> SuppressionEntry:
        """入名单（同邮箱幂等：UNIQUE (tenant, email_hash) 语义）。"""
        entry = SuppressionEntry(email_hash=self.hash_email(email), reason=reason,
                                 source=source, created_at=now or _default_now())
        self._by_hash[entry.email_hash] = entry
        return entry

    def contains(self, email: str) -> bool:
        """是否在名单（发送门禁查询口径）。"""
        return self.hash_email(email) in self._by_hash

    def entries(self) -> list[SuppressionEntry]:
        """全部条目（审计/看板输出）。"""
        return list(self._by_hash.values())


def _default_now() -> datetime:
    """缺省时钟（未注入 clock 时使用）。"""
    from datetime import timezone
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TrackEvent:
    """追踪事件（服务商 webhook / IMAP 轮询 / 公开像素三类入口的统一形态）。"""

    kind: str  # delivered / open / bounce / reply / unsubscribe / order
    job_id: int | None = None
    message_id: str | None = None
    email: str | None = None
    dsn_status: str | None = None  # DSN 状态码：5.x.x=硬退，4.x.x=软退
    customer_id: int | None = None  # order 归因事件必填
    at: datetime | None = None


class DeliveryTracker:
    """送达/打开/退信追踪与统计（mail_job.status 流转的唯一写入口）。"""

    def __init__(self, store: MailStore, engine: SequenceEngine, suppression: SuppressionList, *,
                 clock: Callable[[], datetime] | None = None,
                 soft_bounce_max: int = 3, attribution_days: int = 7) -> None:
        self.store = store
        self.engine = engine
        self.suppression = suppression
        self.clock = clock or _default_now
        self.soft_bounce_max = soft_bounce_max
        self.attribution_days = attribution_days
        self._soft_counts: dict[str, int] = {}  # 收件人连续软退计数（降级入名单依据）
        self._attributed_seqs: set[int] = set()  # 已计挽回的序列（一序列只计首单）

    # ---------------------------------------------------------------- 事件入口

    def on_event(self, evt: TrackEvent) -> dict:
        """统一事件处理（幂等：重复事件不重复计数、状态不回跳）。"""
        handler = {
            "delivered": self._on_delivered, "open": self._on_open, "bounce": self._on_bounce,
            "reply": self._on_reply, "unsubscribe": self._on_unsubscribe, "order": self._on_order,
        }[evt.kind]
        return handler(evt)

    def _job_of(self, evt: TrackEvent) -> MailJob | None:
        """按 job_id 或 message_id 定位任务（IMAP DSN 经 Message-ID 关联）。"""
        if evt.job_id is not None:
            return self.store.jobs.get(evt.job_id)
        if evt.message_id:
            return self.store.job_by_message_id(evt.message_id)
        return None

    def _on_delivered(self, evt: TrackEvent) -> dict:
        """送达回执（SMTP 250 / 服务商 webhook）：queued → delivered（单调，不回跳）。"""
        job = self._job_of(evt)
        if job is None:
            return {"ok": False, "reason": "job_not_found"}
        if job.status == "queued":
            job.status = "delivered"
            job.sent_at = evt.at or self.clock()
        return {"ok": True, "status": job.status}

    def _on_open(self, evt: TrackEvent) -> dict:
        """打开像素：仅 delivered 可推进；同 job 只计首次（30 天去重口径）。"""
        job = self._job_of(evt)
        if job is None or job.status not in ("delivered", "opened"):
            return {"counted": False, "reason": "not_delivered"}
        if job.status == "opened":  # 已计过：像素重复请求不重复计数
            return {"counted": False, "reason": "already_counted"}
        job.status = "opened"
        job.tracked["opens"].append((evt.at or self.clock()).isoformat())
        return {"counted": True}

    def _on_bounce(self, evt: TrackEvent) -> dict:
        """DSN 退信分级（三件套③）：硬退即时入名单停全序列；软退连续 3 次降级入名单。"""
        job = self._job_of(evt)
        if job is None:
            return {"ok": False, "reason": "job_not_found"}
        dsn = evt.dsn_status or ""
        klass = "hard" if dsn.startswith("5") else "soft"
        job.tracked["bounces"].append({"dsn": dsn, "klass": klass,
                                       "at": (evt.at or self.clock()).isoformat()})
        if klass == "hard":  # 5.x.x 永久失败：即时抑制 + 停该收件人全部序列
            self.suppression.add(job.recipient, "hard_bounce", source="dsn", now=evt.at or self.clock())
            self._stop_sequences_of(job.recipient, "hard_bounce")
        else:  # 4.x.x 临时失败：计数，达阈值降级按硬退口径处理
            count = self._soft_counts.get(job.recipient.lower(), 0) + 1
            self._soft_counts[job.recipient.lower()] = count
            if count >= self.soft_bounce_max:
                self.suppression.add(job.recipient, "hard_bounce", source="soft_x3",
                                     now=evt.at or self.clock())
                self._stop_sequences_of(job.recipient, "hard_bounce")
        job.status = "failed"
        job.fail_reason = f"bounce:{klass}:{dsn}"
        return {"ok": True, "klass": klass}

    def _on_reply(self, evt: TrackEvent) -> dict:
        """客户回复（IMAP 识别）：停全部序列（replied）+ 最近任务回写 reply_at。"""
        email = evt.email or (self._job_of(evt).recipient if self._job_of(evt) else "")
        if not email:
            return {"ok": False, "reason": "no_recipient"}
        self._stop_sequences_of(email, "replied")
        job = self.store.last_sent_job(email)
        if job is not None:
            job.tracked["reply_at"] = evt.at or self.clock()  # 原始 datetime（序列化在 API 层）
        return {"ok": True}

    def _on_unsubscribe(self, evt: TrackEvent) -> dict:
        """退订（RFC 8058 one-click / 落地页）：入名单 + 停全部序列（unsubscribed）。"""
        if not evt.email:
            return {"ok": False, "reason": "no_email"}
        self.suppression.add(evt.email, "unsubscribe", source="one_click", now=evt.at or self.clock())
        self._stop_sequences_of(evt.email, "unsubscribed")
        return {"ok": True}

    def _on_order(self, evt: TrackEvent) -> dict:
        """成交归因：窗口内该客户最近一条已发送 cart_recover 序列计 1（一序列只计首单）。"""
        if evt.customer_id is None:
            return {"attributed": False, "reason": "no_customer"}
        at = evt.at or self.clock()
        best: tuple[int, datetime, MailJob] | None = None
        for seq in self.store.sequences.values():
            if seq.customer_id != evt.customer_id or seq.seq_type != "cart_recover":
                continue
            if seq.seq_id in self._attributed_seqs:
                continue
            sent_jobs = [j for j in self.store.jobs_of(seq.seq_id) if j.sent_at is not None]
            if not sent_jobs:
                continue
            latest = max(sent_jobs, key=lambda j: (j.sent_at, j.job_id))
            window = timedelta(days=self.attribution_days)
            if latest.sent_at is not None and latest.sent_at <= at <= latest.sent_at + window:
                if best is None or (latest.sent_at, latest.job_id) > (best[1], best[2].job_id):
                    best = (seq.seq_id, latest.sent_at, latest)
        if best is None:
            return {"attributed": False, "reason": "out_of_window"}
        self._attributed_seqs.add(best[0])
        best[2].tracked["order_at"] = at.isoformat()
        return {"attributed": True}

    def _stop_sequences_of(self, email: str, reason: str) -> int:
        """停某收件人全部序列（apply_event 白名单外原因自动 no-op）。"""
        return sum(1 for s in self.store.sequences_for_email(email)
                   if self.engine.apply_event(s.seq_id, reason))

    # ---------------------------------------------------------------- 统计口径

    def stats(self, seq_type: str | None = None,
              date_from: date | None = None, date_to: date | None = None) -> dict:
        """统计看板口径（与 FR-SITE-006 同源）：状态终值计数 + 送达/打开率 + 挽回归因。"""
        jobs = list(self.store.jobs.values())
        if seq_type is not None:
            jobs = [j for j in jobs if j.seq_type == seq_type]
        if date_from is not None or date_to is not None:
            jobs = [j for j in jobs if j.sent_at is not None
                    and (date_from is None or j.sent_at.date() >= date_from)
                    and (date_to is None or j.sent_at.date() <= date_to)]
        sent = sum(1 for j in jobs if j.status in ("delivered", "opened", "failed"))
        delivered = sum(1 for j in jobs if j.status in ("delivered", "opened"))
        opened = sum(1 for j in jobs if j.status == "opened")
        failed = sum(1 for j in jobs if j.status == "failed")
        seqs = list(self.store.sequences.values())
        if seq_type is not None:
            seqs = [s for s in seqs if s.seq_type == seq_type]
        stops: dict[str, int] = {}
        for s in seqs:
            if s.status == "stopped" and s.stop_reason:
                stops[s.stop_reason] = stops.get(s.stop_reason, 0) + 1
        attributed = sum(1 for sid in self._attributed_seqs
                         if seq_type is None or self.store.sequences[sid].seq_type == seq_type)
        return {
            "sent": sent,
            "delivered": delivered,
            "opened": opened,
            "failed": failed,
            "cancelled": sum(1 for j in jobs if j.status == "cancelled"),
            "replied": stops.get("replied", 0),
            "unsubscribed": stops.get("unsubscribed", 0),
            "hard_bounce": stops.get("hard_bounce", 0),
            "recovered_orders": attributed,
            "delivery_rate": (delivered / sent) if sent else None,
            "open_rate": (opened / delivered) if delivered else None,
        }

    def passes_delivery_rate(self, stats: dict, threshold: float = 0.95) -> bool:
        """≥95% 判定函数（SRS §16.4）：零发送不判定通过（空集保护）。"""
        rate = stats.get("delivery_rate")
        return bool(stats.get("sent") and rate is not None and rate >= threshold)
