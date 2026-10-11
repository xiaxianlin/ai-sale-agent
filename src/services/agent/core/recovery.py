# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""故障恢复协议（NFR-REL-03，SR-NFR-REL-03 §6.1/§6.3/§6.4；迭代 2.1 demo 口径）。

三层防线中本模块承载第①层（状态外置）与第③层（恢复协议 recover）：

- 进程无状态化：会话状态机零内存权威（state 模块）、游标外置（engine 模块）、
  回复任务外置（ReplyJobStore，生产 = PG reply_job）——进程可随时 kill；
- recover() 六步时序的 demo 编排：① 会话状态从存储重建（SR-AG-008 §6.5）→
  ② 中断任务重放：sending 不确定区以平台侧查证收敛（已发 CAS 补账 sent 不重发、
  未发回置 queued 重放），generating 无外发副作用回置 queued → ③ RTO 计时入报告；
- 发送幂等五层中本实现落地两层：幂等键 idem_key = sha256(会话 uid + 消息内容)
  （任务唯一性，PG UNIQUE 语义）+ 发送状态 CAS（mark/cas_status sending→sent 恰一）；
- 重放走正常链路：闸门 acquire → 拟人化计划 → 驱动发送，不为恢复开捷径；
  崩溃期间人工接管 → 闸门物理阻断 → 任务转 handover（绝不越权代发）；
- RTO：单实例 <2min（kill → ready），实测 rto_sec 入 RecoverReport（演练台账证据）。

demo 形态边界：advisory 单飞锁/风暴熔断/心跳探针随部署面落地（compose + 运维脚本），
本模块聚焦恢复语义与幂等正确性；KillDrill 演练见 tests/test_recovery.py（脚本化）。
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from services.agent.core.driver import BrowserDriver, TypingPlan
from services.agent.core.engine import AlertHook, HumanTyper, external_of
from services.agent.core.state import (
    AISendBlockedError,
    AISendGate,
    ConversationStateMachine,
    ConversationRecord,
    SessionStoreProtocol,
)

# ── 回复任务状态机（SR-NFR-REL-03 §4 reply_job.status 口径） ──
JOB_QUEUED = "queued"          # 待生成/待重放
JOB_GENERATING = "generating"  # 生成中（无外发副作用）
JOB_SENDING = "sending"        # 发送中（不确定区：崩溃恢复的查证对象）
JOB_SENT = "sent"              # 已发送（终态）
JOB_HANDOVER = "handover"      # 转人工（闸门阻断，终态）

#: 任务在途状态集合（重放候选）
INFLIGHT_STATUSES = (JOB_QUEUED, JOB_GENERATING, JOB_SENDING)


# ─────────────────────────── 回复任务与幂等键 ───────────────────────────


def idem_key(conversation_uid: str, content: str) -> str:
    """发送幂等键（幂等键②）：sha256(会话 uid + 换行 + 消息内容) 前 32 位十六进制。

    同会话同内容恒定 → 重放复用原键不新建；跨会话/跨内容互异 → 任务唯一。
    """
    digest = hashlib.sha256(f"{conversation_uid}\n{content}".encode("utf-8")).hexdigest()
    return digest[:32]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ReplyJob:
    """回复任务（生成-发送全周期落库：中断重放与幂等的载体）。

    生产 = PG reply_job（idem_key UNIQUE + status CHECK）；demo 进程内形态。
    """

    uid: str
    account_id: int
    conversation_uid: str
    content: str
    idem_key: str = ""
    status: str = JOB_QUEUED
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.idem_key:
            self.idem_key = idem_key(self.conversation_uid, self.content)
        now = _utc_now_iso()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    @classmethod
    def new(cls, *, account_id: int, conversation_uid: str, content: str) -> "ReplyJob":
        """构造新任务：uid 随机、幂等键自动派生。"""
        return cls(
            uid=uuid.uuid4().hex[:16],
            account_id=account_id,
            conversation_uid=conversation_uid,
            content=content,
        )


class ReplyJobStoreProtocol:
    """回复任务存储协议（生产 = PG；demo = InMemoryReplyJobStore）。"""

    def upsert(self, job: ReplyJob) -> ReplyJob:
        """按 idem_key 幂等写入：已存在同键任务则原样返回（UNIQUE 语义）。"""
        ...

    def get(self, job_uid: str) -> ReplyJob | None: ...

    def get_by_idem(self, key: str) -> ReplyJob | None: ...

    def list_inflight(self, account_id: int) -> list[ReplyJob]:
        """在途任务（queued/generating/sending）——重放候选。"""
        ...

    def cas_status(self, job_uid: str, from_status: str, to_status: str) -> bool:
        """发送状态 CAS（幂等键③）：条件更新恰一成功，败者放弃绝不二次外发。"""
        ...


class InMemoryReplyJobStore(ReplyJobStoreProtocol):
    """内存回复任务库（demo）：RLock 串行化 + idem_key 唯一 + CAS 条件更新。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, ReplyJob] = {}

    def upsert(self, job: ReplyJob) -> ReplyJob:
        with self._lock:
            existing = self._jobs_by_idem(job.idem_key)
            if existing is not None:
                return existing                      # 幂等：同键不新建
            self._jobs[job.uid] = job
            return job

    def get(self, job_uid: str) -> ReplyJob | None:
        with self._lock:
            return self._jobs.get(job_uid)

    def get_by_idem(self, key: str) -> ReplyJob | None:
        with self._lock:
            return self._jobs_by_idem(key)

    def list_inflight(self, account_id: int) -> list[ReplyJob]:
        with self._lock:
            return [
                job for job in self._jobs.values()
                if job.account_id == account_id and job.status in INFLIGHT_STATUSES
            ]

    def cas_status(self, job_uid: str, from_status: str, to_status: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_uid)
            if job is None or job.status != from_status:
                return False                         # 败者：已被并发方推进，立即放弃
            job.status = to_status
            job.updated_at = _utc_now_iso()
            return True

    def _jobs_by_idem(self, key: str) -> ReplyJob | None:
        return next((j for j in self._jobs.values() if j.idem_key == key), None)


# ─────────────────────────── 恢复协议 ───────────────────────────


@dataclass(frozen=True)
class RecoverReport:
    """恢复报告（演练台账证据：RTO 实测 + 各步计数）。"""

    ready: bool               # 恢复完成且可拉起轮询（闸门/存储全部就绪）
    rto_sec: float            # kill/异常 → ready 实测时长（验收口径 <2min）
    rebuilt_sessions: int     # 第①步重建会话数
    replayed: int             # 第②步重放任务数（含回置 queued 的重放）
    sent_now: int             # 本次恢复实际发送数（重放产生）
    marked_sent: int          # 平台查证已发 → CAS 补账数（零重发证据）
    handed_over: int          # 闸门阻断转人工数


class RecoveryManager:
    """恢复协议编排（SR-NFR-REL-03 §6.3 六步时序的 demo 核心）。

    幂等可重入：恢复中再崩溃 → 从头再跑结果一致（CAS + 幂等键兜底）。
    """

    def __init__(
        self,
        *,
        store: SessionStoreProtocol,
        state_machine: ConversationStateMachine,
        gate: AISendGate,
        driver: BrowserDriver,
        job_store: ReplyJobStoreProtocol,
        alerts: AlertHook | None = None,
        audit: Callable[[dict[str, object]], None] | None = None,
        typer: HumanTyper | None = None,
        rto_target_s: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._sm = state_machine
        self._gate = gate
        self._driver = driver
        self._jobs = job_store
        self._alerts = alerts
        self._audit = audit
        self._typer = typer
        self._rto_target = rto_target_s
        self._clock = clock

    def recover(self, account_id: int, *, unclean: bool = True) -> RecoverReport:
        """恢复主时序：告警取证 → 会话重建 → 中断任务重放（幂等）→ RTO 计时。"""
        t0 = self._clock()
        # ── 第 1 步 崩溃上下文与告警：计划外重启先告警后恢复（FR-AG-007 通道）
        if unclean and self._alerts is not None:
            self._alerts.send(
                level="mid",
                type_="restart",
                title=f"账号{account_id} Agent 进程异常重启，恢复协议执行中",
                body="崩溃上下文：最后处理会话与错误栈摘要见 spool 卷（脱敏后发送）",
            )
        # ── 第 2 步 会话状态重建（SR-AG-008 §6.5）：进程无状态化，权威在存储
        records: list[ConversationRecord] = self._store.list_all()
        for record in records:
            self._sm.rebuild(record.uid)     # 运行时实例重建（缓存语义，demo 显式化）
        # 任务所属会话兜底重建（回复任务必然关联既有会话；demo 防御式口径）
        inflight = self._jobs.list_inflight(account_id)
        for job in inflight:
            self._sm.ensure_conversation(job.conversation_uid)
        # ── 第 3 步 中断任务重放（幂等核心，防重启后重复发送）
        replayed = sent_now = marked = handed = 0
        for job in inflight:
            if job.status == JOB_SENDING:
                # 发送不确定区收敛：平台侧查证（读会话末条比对），不猜测不盲发
                if self._driver.verify_sent(external_of(job.conversation_uid), job.content):
                    if self._jobs.cas_status(job.uid, JOB_SENDING, JOB_SENT):
                        marked += 1          # 已发 → 补账，绝不重发
                    continue
                self._jobs.cas_status(job.uid, JOB_SENDING, JOB_QUEUED)  # 未发 → 回置重放
            elif job.status == JOB_GENERATING:
                self._jobs.cas_status(job.uid, JOB_GENERATING, JOB_QUEUED)  # 无外发副作用
            if not self._jobs.cas_status(job.uid, JOB_QUEUED, JOB_SENDING):
                continue                     # 恰一重放（CAS 败者放弃）
            replayed += 1
            outcome = self._replay_send(job)
            if outcome == "sent":
                sent_now += 1
            elif outcome == "handover":
                handed += 1
        # ── 第 4 步 恢复完成标记：RTO 实测 + 审计（风暴熔断/登录失效随部署面落地）
        rto = self._clock() - t0
        if self._audit is not None:
            self._audit({
                "event": "instance_recovered",
                "account_id": account_id,
                "rto_sec": round(rto, 3),
                "rebuilt": len(records),
                "replayed": replayed,
            })
        return RecoverReport(
            ready=True,
            rto_sec=rto,
            rebuilt_sessions=len(records),
            replayed=replayed,
            sent_now=sent_now,
            marked_sent=marked,
            handed_over=handed,
        )

    def _replay_send(self, job: ReplyJob) -> str:
        """重放发送（正常链路：闸门 → 拟人化计划 → 驱动发送 → CAS 补账）。

        返回 "sent" / "handover" / "skipped"：
        闸门物理阻断（崩溃期间人工接管）→ 任务转 handover，绝不越权代发。
        """
        try:
            token = self._gate.acquire(job.conversation_uid)
        except AISendBlockedError:
            self._jobs.cas_status(job.uid, JOB_SENDING, JOB_HANDOVER)
            return "handover"
        if not self._gate.verify(job.conversation_uid, token):
            return "skipped"                 # 竞态：放弃（人工优先）
        plan = (
            self._typer.plan(job.content)
            if self._typer is not None
            else TypingPlan(text=job.content, char_delays_ms=(), pauses=())
        )
        self._driver.send_reply(token, plan)
        self._jobs.cas_status(job.uid, JOB_SENDING, JOB_SENT)   # 幂等键③ CAS 补账
        if self._audit is not None:
            self._audit({
                "event": "replay_send",
                "conv_uid": job.conversation_uid,
                "idem_key": job.idem_key,
                "content_len": len(job.content),
            })
        return "sent"
