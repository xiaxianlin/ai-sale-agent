# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""会话状态机（FR-AG-008，SR-AG-008 §6.1/§6.2/§6.4/§6.5；迭代 2.1 demo 口径）。

职责（05-核心模块详细设计 §6.2：agent-runner 执行器 owns 会话状态机）：
- 四态 ConvState（ai_managed / human_managed / pending_confirm / closed）与合法迁移表；
- transition() 迁移内核：快路径校验 → 乐观并发（state_version CAS）→ state_hist
  追加留痕（from/to/at/by/reason，与状态更新同"事务"）→ 审计钩子投递（NFR-SEC-03）；
- 发送闸门 AISendGate：物理阻断而非约定——SendToken 只能由闸门在 ai_managed 态签发，
  driver.send_reply 仅接受 SendToken 实例；发送前 verify 二次校验（版本失配/TTL 过期即拒）；
- pending_confirm 超时巡检：N 小时提醒恰一次、M 小时 system:timeout 转人工
  （SRS §7.4 红线：挂起超时绝不回 ai_managed 自动执行）；
- rebuild()：进程无状态化——权威在存储（生产 = PG conversation 行），新实例重建即恢复
  （NFR-REL-03 会话状态不丢失）。

与设计文档的偏差（显式标注）：SR-AG-008 §6.1 迁移表 9 条未含 pending→human_managed，
而 §6.4 挂起超时要求转人工；本实现按工程正确性取并集（10 条），该路径仅由
scan_pending_timeout 以 actor=system:timeout 触发（reason=pending_timeout_24h）。

demo 形态边界：InMemorySessionStore 以进程内存 + RLock 扮演 PG 行级锁 + 唯一权威；
生产替换为 PG 实现时 SessionStore 协议不变（乐观并发语义由 CAS 签名承载）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


# ─────────────────────────── 四态与迁移表 ───────────────────────────


class ConvState(StrEnum):
    """会话四态（SRS §7.2 FR-AG-008 原文：AI 接管/人工接管/等待确认/已结束）。"""

    AI_MANAGED = "ai_managed"            # AI 接管（唯一允许 AI 发送的态）
    HUMAN_MANAGED = "human_managed"      # 人工接管（AI 发送被物理阻断）
    PENDING_CONFIRM = "pending_confirm"  # 等待确认（敏感操作挂起，FR-AG-005②）
    CLOSED = "closed"                    # 已结束（可重开）


#: 合法迁移表 —— §6.1 九条 + §6.4 挂起超时 fail-safe（pending→human，见模块偏差标注）
LEGAL_TRANSITIONS: frozenset[tuple[ConvState, ConvState]] = frozenset({
    (ConvState.AI_MANAGED, ConvState.HUMAN_MANAGED),      # 人工接管
    (ConvState.HUMAN_MANAGED, ConvState.AI_MANAGED),      # 人工放回
    (ConvState.AI_MANAGED, ConvState.PENDING_CONFIRM),    # 敏感操作 → 挂起
    (ConvState.PENDING_CONFIRM, ConvState.AI_MANAGED),    # 确认执行 / 拒绝关闭草稿后放回
    (ConvState.AI_MANAGED, ConvState.CLOSED),             # 任意态 → closed
    (ConvState.HUMAN_MANAGED, ConvState.CLOSED),
    (ConvState.PENDING_CONFIRM, ConvState.CLOSED),
    (ConvState.CLOSED, ConvState.AI_MANAGED),             # 重开：默认回 AI
    (ConvState.CLOSED, ConvState.HUMAN_MANAGED),          # 重开：可指定人工
    (ConvState.PENDING_CONFIRM, ConvState.HUMAN_MANAGED),  # 挂起超时自动转人工（§6.4）
})

#: 超时迁移 actor/reason（§6.4 口径，日志与审计可对账）
TIMEOUT_ACTOR = "system:timeout"
TIMEOUT_REASON = "pending_timeout_24h"


# ─────────────────────────── 异常（中文上下文，对齐 §5 错误码） ───────────────────────────


class StateMachineError(Exception):
    """状态机基础异常：全部子类携带中文可读信息（日志/面板/HTTP detail 直用）。"""


class ConversationNotFoundError(StateMachineError):
    """会话不存在（→ HTTP 40401）。"""


class IllegalTransitionError(StateMachineError):
    """非法状态迁移：迁移表无此路径（→ HTTP 42201），不产生任何写库。"""


class VersionConflictError(StateMachineError):
    """乐观并发冲突：版本号已被并发方推进（→ HTTP 40901）；人工优先，AI 侧应放弃。"""


class AISendBlockedError(StateMachineError):
    """AI 发送被阻断：非 ai_managed 态闸门不签发任何可发送句柄（→ HTTP 40302）。"""


# ─────────────────────────── 会话记录与存储 ───────────────────────────


def _utc_now_iso() -> str:
    """默认时钟：ISO8601 UTC（留痕 at 字段口径）。"""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConversationRecord:
    """会话记录（生产 = PG conversation 行；demo 进程内形态，字段同名同义）。

    state_hist 条目结构（SR-AG-008 偏差①：{from,to,at,by} 扩展 reason）：
        {"from": "...", "to": "...", "at": "ISO8601", "by": "...", "reason": "..."}
    """

    uid: str
    state: ConvState = ConvState.AI_MANAGED
    state_version: int = 0                     # 乐观并发版本号，每次迁移 +1
    state_hist: list[dict[str, str]] = field(default_factory=list)
    pending_since: str | None = None           # 进入 pending_confirm 的时刻（超时巡检）
    pending_reminded: bool = False             # 挂起超时提醒是否已发（每会话恰一次）


#: CAS 迁移变更器：在存储锁内对记录施加变更（版本校验通过才执行）
Mutator = Callable[[ConversationRecord], None]


class SessionStoreProtocol:
    """会话存储协议（生产 = PG；demo = InMemorySessionStore）。

    cas_transition 是乐观并发的落点：锁内「读版本 → 比对 → 变更」原子执行，
    与调用方读到的版本不一致即返回 False（后到者 40901 重试或放弃）。
    """

    def get(self, uid: str) -> ConversationRecord | None: ...

    def put(self, record: ConversationRecord) -> None: ...

    def list_all(self) -> list[ConversationRecord]: ...

    def cas_transition(
        self,
        uid: str,
        expected_version: int,
        mutate: Mutator,
        *,
        on_locked: Callable[[], None] | None = None,
    ) -> bool:
        """锁内 CAS：版本匹配才执行 mutate 并返回 True；否则 False（不施加变更）。"""
        ...


class InMemorySessionStore(SessionStoreProtocol):
    """内存会话存储（demo）：RLock 串行化 + on_locked 测试钩子注入锁内交错。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()         # 可重入：race 测试在同线程内模拟并发写者
        self._records: dict[str, ConversationRecord] = {}

    def get(self, uid: str) -> ConversationRecord | None:
        with self._lock:
            found = self._records.get(uid)
            return None if found is None else _clone(found)

    def put(self, record: ConversationRecord) -> None:
        with self._lock:
            self._records[record.uid] = _clone(record)

    def list_all(self) -> list[ConversationRecord]:
        with self._lock:
            return [_clone(r) for r in self._records.values()]

    def cas_transition(
        self,
        uid: str,
        expected_version: int,
        mutate: Mutator,
        *,
        on_locked: Callable[[], None] | None = None,
    ) -> bool:
        with self._lock:
            if on_locked is not None:          # 锁内钩子：并发写者可在此抢先推进版本
                on_locked()
            record = self._records.get(uid)
            if record is None or record.state_version != expected_version:
                return False                   # 权威态二次校验失败：后到者冲突
            mutate(record)
            return True


def _clone(record: ConversationRecord) -> ConversationRecord:
    """深拷贝记录（hist 列表隔离，防外部引用绕过存储锁）。"""
    return ConversationRecord(
        uid=record.uid,
        state=record.state,
        state_version=record.state_version,
        state_hist=[dict(entry) for entry in record.state_hist],
        pending_since=record.pending_since,
        pending_reminded=record.pending_reminded,
    )


# ─────────────────────────── 迁移内核 ───────────────────────────


@dataclass(frozen=True)
class TransitionResult:
    """迁移结果：to_state 为目标态、version 为迁移后版本；idempotent=幂等命中。"""

    uid: str
    from_state: ConvState
    to_state: ConvState
    version: int
    idempotent: bool = False


#: 审计钩子：每次真实迁移投递一条（生产 = audit_log 哈希链，NFR-SEC-03）
AuditHook = Callable[[dict[str, object]], None]
Clock = Callable[[], datetime]


class ConversationStateMachine:
    """会话状态机内核：迁移表 + 乐观并发 + 留痕 + 审计钩子（SR-AG-008 §6.1）。

    权威 = 存储会话记录（生产 PG）；本类进程内零状态，可随时重建（rebuild）。
    """

    def __init__(
        self,
        store: SessionStoreProtocol,
        *,
        audit: AuditHook | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._audit = audit
        self._clock = clock or _utc_now_iso

    # ---- 查询 ----

    def get_state(self, uid: str) -> tuple[ConvState, int]:
        """读当前态与版本（不存在抛 ConversationNotFoundError）。"""
        record = self._require(uid)
        return record.state, record.state_version

    def rebuild(self, uid: str) -> ConversationRecord:
        """崩溃恢复前置（NFR-REL-03）：从存储重建会话（state/version/hist 全量）。"""
        return _clone(self._require(uid))

    def ensure_conversation(self, uid: str) -> ConversationRecord:
        """会话定位（SR-AG-002 §4）：未命中既有会话则新建，默认 ai_managed。"""
        record = self._store.get(uid)
        if record is None:
            record = ConversationRecord(uid=uid)
            self._store.put(record)
            return _clone(record)
        return record

    def send_allowed(self, state: ConvState) -> bool:
        """发送资格：仅 ai_managed 态允许 AI 发送（四态唯一）。"""
        return state is ConvState.AI_MANAGED

    # ---- 迁移内核 ----

    def transition(
        self,
        uid: str,
        to_state: ConvState,
        actor: str,
        reason: str,
        *,
        expect_version: int | None = None,
        _test_on_locked: Callable[[], None] | None = None,
    ) -> TransitionResult:
        """迁移内核：①快路径校验 → ②乐观并发 CAS → ③锁内留痕 → ④审计投递。

        - 幂等：cur == to_state 直接返回（无迁移不写痕、不审计）；
        - 非法迁移：抛 IllegalTransitionError（不碰存储）；
        - 乐观并发：expect_version 与当前版本不符，或 CAS 期间被并发方推进
          （锁内 on_locked 钩子可注入交错）→ VersionConflictError；
        - 留痕：state_hist 追加 {from,to,at,by,reason}，与状态同"事务"（存储 CAS 内）。
        """
        to_state = ConvState(to_state)         # 防御式收窄：接受 ConvState 或其字符串值
        record = self._require(uid)
        cur = record.state
        if cur is to_state:                    # 幂等：目标态已达，无迁移不写痕
            return TransitionResult(uid, cur, to_state, record.state_version, idempotent=True)
        if (cur, to_state) not in LEGAL_TRANSITIONS:
            raise IllegalTransitionError(
                f"非法状态迁移：{cur.value} → {to_state.value}（四态迁移表无此路径，FR-AG-008）"
            )
        if (cur, to_state) == (ConvState.PENDING_CONFIRM, ConvState.HUMAN_MANAGED) \
                and actor != TIMEOUT_ACTOR:
            # §6.1 迁移表未含 pending→human；该路径仅为 §6.4 挂起超时 fail-safe 保留
            # （system:timeout 专用），人工侧不得借此绕过确认链路直接接管。
            raise IllegalTransitionError(
                "非法状态迁移：pending_confirm → human_managed 仅允许挂起超时"
                f"（actor={TIMEOUT_ACTOR}）触发，人工请走确认链路"
            )
        if expect_version is not None and record.state_version != expect_version:
            raise VersionConflictError(
                f"乐观并发冲突：期望版本 {expect_version}，当前版本 {record.state_version}"
                "（请重读状态后重试或放弃；人工已接管则 AI 一律放弃）"
            )
        at = self._clock()
        entry = {
            "from": cur.value,
            "to": to_state.value,
            "at": at,
            "by": actor,
            "reason": reason,
        }
        new_version = record.state_version + 1

        def mutate(target: ConversationRecord) -> None:
            target.state = to_state
            target.state_version += 1
            target.state_hist.append(entry)    # 留痕与状态同事务（存储 CAS 内原子）
            if to_state is ConvState.PENDING_CONFIRM:
                target.pending_since = at
                target.pending_reminded = False  # 新一轮挂起重置提醒标记
            else:
                target.pending_since = None

        ok = self._store.cas_transition(
            uid, record.state_version, mutate, on_locked=_test_on_locked
        )
        if not ok:
            raise VersionConflictError(
                "乐观并发冲突：迁移期间状态已被并发方推进（人工优先，AI 侧应放弃重试）"
            )
        self._emit_audit(uid, cur, to_state, actor, reason, at, new_version)
        return TransitionResult(uid, cur, to_state, new_version)

    # ---- 内部 ----

    def _require(self, uid: str) -> ConversationRecord:
        record = self._store.get(uid)
        if record is None:
            raise ConversationNotFoundError(f"会话 {uid} 不存在，无法执行状态操作")
        return record

    def _emit_audit(
        self,
        uid: str,
        from_state: ConvState,
        to_state: ConvState,
        actor: str,
        reason: str,
        at: str,
        version: int,
    ) -> None:
        """审计投递（NFR-SEC-03）：只记 who/when/what，不记任何内容明文。"""
        if self._audit is None:
            return
        self._audit({
            "event": "conv_state_transition",
            "conv_uid": uid,
            "from": from_state.value,
            "to": to_state.value,
            "at": at,
            "by": actor,
            "reason": reason,
            "version": version,
        })


# ─────────────────────────── pending_confirm 超时巡检 ───────────────────────────


def scan_pending_timeout(
    store: SessionStoreProtocol,
    sm: ConversationStateMachine,
    *,
    now: datetime,
    remind_hours: float = 2.0,
    timeout_hours: float = 24.0,
    remind: Callable[[str, float], None] | None = None,
) -> list[TransitionResult]:
    """挂起超时巡检（SR-AG-008 §6.4，jobs/pending_timeout.py 的进程内形态）。

    - 挂起 ≥ remind_hours：提醒恰一次（pending_reminded 去重，复用 FR-AG-007 告警通道）；
    - 挂起 ≥ timeout_hours：system:timeout 转 human_managed——挂起超时只提醒、只转人工，
      绝不回 ai_managed 自动执行（SRS §7.4 红线）；
    - 幂等可重入：重复扫描同一快照不产生第二次提醒/迁移。
    """
    moved: list[TransitionResult] = []
    for snapshot in store.list_all():
        if snapshot.state is not ConvState.PENDING_CONFIRM or snapshot.pending_since is None:
            continue
        since = datetime.fromisoformat(snapshot.pending_since)
        age_hours = (now - since).total_seconds() / 3600.0
        if age_hours >= timeout_hours:
            moved.append(sm.transition(
                snapshot.uid, ConvState.HUMAN_MANAGED,
                actor=TIMEOUT_ACTOR, reason=TIMEOUT_REASON,
            ))
        elif age_hours >= remind_hours and not snapshot.pending_reminded:
            if remind is not None:
                remind(snapshot.uid, age_hours)   # 提醒去重由调用方事件通道+本标记双保险
            fresh = store.get(snapshot.uid)
            if fresh is not None:
                fresh.pending_reminded = True
                store.put(fresh)
    return moved


# ─────────────────────────── 发送闸门（物理阻断） ───────────────────────────


@dataclass(frozen=True)
class SendToken:
    """发送令牌：闸门在 ai_managed 态签发的唯一可发送凭证（含签发时版本）。"""

    uid: str
    version: int
    issued_at: float


def _default_clock() -> float:
    import time

    return time.monotonic()


class AISendGate:
    """AI 发送闸门（SR-AG-008 §6.2）：物理阻断而非约定。

    物理性三重保证：
    ① acquire 仅在 ai_managed 态返回 SendToken——其他态抛 AISendBlockedError，
       不产生任何可发送句柄；
    ② SendToken 只能由闸门构造（driver.send_reply 对非 SendToken 参数 TypeError，
       见 driver 模块——字符串/None 伪造一律拒收）；
    ③ verify 在发送提交前最后一瞬校验：期间任何迁移（版本 +1）或 TTL 过期即失配，
       调用方必须中止发送。
    """

    def __init__(
        self,
        store: SessionStoreProtocol,
        *,
        token_ttl_sec: float = 30.0,
        clock: Callable[[], float] = _default_clock,
    ) -> None:
        self._store = store
        self._ttl = token_ttl_sec
        self._clock = clock

    def acquire(self, uid: str) -> SendToken:
        """签发令牌：非 ai_managed 态抛 AISendBlockedError（40302，无令牌产生）。"""
        record = self._store.get(uid)
        if record is None:
            raise ConversationNotFoundError(f"会话 {uid} 不存在，无法签发发送令牌")
        if record.state is not ConvState.AI_MANAGED:
            raise AISendBlockedError(
                f"AI 发送被物理阻断：会话 {uid} 处于 {record.state.value} 态"
                "（仅 ai_managed 可发送，FR-AG-008 发送闸门）"
            )
        return SendToken(uid=uid, version=record.state_version, issued_at=self._clock())

    def verify(self, uid: str, token: SendToken) -> bool:
        """发送前最后一瞬校验：状态仍 ai_managed、版本未变、未过 TTL 三者齐备才放行。"""
        record = self._store.get(uid)
        if record is None or token.uid != uid:
            return False
        if record.state is not ConvState.AI_MANAGED:
            return False
        if record.state_version != token.version:
            return False                       # 期间发生迁移：版本必 +1
        return (self._clock() - token.issued_at) <= self._ttl
