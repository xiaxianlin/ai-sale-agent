# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""风控 V1 安全框架（FR-AG-005，SR-AG-005 §6；迭代 2.3 demo 口径）。

职责（执行通道层的**前置门**——风控优先于效率，架构原则第 4 条）：
- :class:`RiskGuard`——判定总门，强制顺序：接管门禁（AI 静默）→ 红线内容匹配
  （BLOCK+高级告警）→ 敏感操作白名单拦截（INTERCEPT→草稿→pending_confirm 联动）
  → 频率门（PASS 签发 :class:`SendPermit` / 超限排队）；
- **fail-safe：宁停不险**（SR-AG-005 §6.6）——风控自身任何组件抛错一律 FAILSAFE
  停发并高级告警，绝不因故障放开（NFR-SEC-10 零封号第一优先级）；
- **强制路径无旁路**——:class:`SendPermit` 仅由风控签发（模块私有 bearer，
  外部构造即 TypeError）、一次性消费；:class:`PermitOnlySender` 仅接受 Permit 实例；
- 频率控制：滑动窗口（小时 ZSET + 日计数，Redis 语义的内存等价）+ 对数正态
  间隔随机化（NFR-SEC-11，夜间顺延次日工作窗口+入场噪声）；
- INTERCEPT/BLOCK 不消耗额度（草稿并未发送）；op_uid 幂等（重复判定回放首次结果）；
- 判定全量审计（含 PASS 双写，NFR-SEC-03「风控触发事件可审计」）。

demo 形态边界：Redis Lua / PG risk_event 由生产形态承载；存储替换时模块协议不变。
"""

from __future__ import annotations

import random
import re
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Callable, Iterable, Protocol


# ─────────────────────────── 判定结果与配置 ───────────────────────────


class Verdict(StrEnum):
    """风控判定（SR-AG-005 §4 risk_event.verdict 枚举 + FAILSAFE 降级档）。"""

    PASS = "PASS"                        # 放行（签发 SendPermit）
    QUEUE_HOUR = "QUEUE_HOUR"            # 小时超限/间隔未到：排队下一窗口
    QUEUE_NEXT_DAY = "QUEUE_NEXT_DAY"    # 日超限：排队次日工作窗口
    INTERCEPT = "INTERCEPT"              # 敏感操作：转草稿等人工确认（§7.4 红线）
    BLOCK = "BLOCK"                      # 平台红线命中：永不执行
    DENY = "DENY"                        # 接管/待确认：AI 静默（正常业务，不告警）
    FAILSAFE = "FAILSAFE"                # 风控自身故障：一律 BLOCK（宁可停发）


@dataclass(frozen=True)
class RiskConfig:
    """风控参数（app_config scope='risk' 的冻结快照形态，NFR-MAI-02 配置外置）。"""

    max_ops_per_hour: int = 20            # 单账号每小时操作量上限
    max_ops_per_day: int = 150            # 单账号每日操作量上限（超限排队次日）
    min_gap_s: float = 20.0               # 间隔采样夹取下限（防机械高频，M1）
    max_gap_s: float = 600.0              # 间隔采样夹取上限
    ln_mu: float = 11.0                   # 间隔对数正态 μ（ms 域 ≈ 中位 60s）
    ln_sigma: float = 0.6                 # 间隔对数正态 σ（右偏拟人节奏）
    hold_minutes: int = 30                # 小时超限排队保持时长
    work_start: str = "08:30"             # 活跃时段模型：工作窗口起点
    work_end: str = "18:30"               # 工作窗口终点（夜间不操作）
    confirm_expire_hours: int = 24        # 敏感草稿确认时限
    auto_executable: frozenset[str] = frozenset({
        "poll_inquiry", "read_message", "mark_read", "send_reply",
    })
    sensitive_types: frozenset[str] = frozenset({
        "price_change", "order_op", "payment", "shipping_addr",   # §7.4 红线，不可移出
    })


def _parse_hm(value: str) -> tuple[int, int]:
    """「HH:MM」→ (时, 分)。"""
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def classify_action(action_type: str, config: RiskConfig | None = None) -> str:
    """动作白名单制分类：白名单→auto；敏感清单→intercept；**未知→intercept**。

    未知动作默认拦截是 fail-safe：新增动作类型未登记前不可能绕过风控自动执行。
    """
    cfg = config or RiskConfig()
    if action_type in cfg.sensitive_types:
        return "intercept"
    if action_type in cfg.auto_executable:
        return "auto"
    return "intercept"


@dataclass(frozen=True)
class RiskAction:
    """判定入参（SR-AG-005 §5-1 check 请求体的进程内形态）。"""

    op_uid: str                    # 幂等键（执行器生成）
    account_id: int
    conversation_uid: str
    action_type: str
    content: str = ""              # 外发内容草稿（红线内容匹配对象）


@dataclass(frozen=True)
class RiskDecision:
    """判定出参：verdict + 原因码 + 排队时刻 / Permit / 敏感草稿 uid。"""

    verdict: Verdict
    reason: str
    rule_code: str | None = None
    next_allowed_at: datetime | None = None
    permit: "SendPermit | None" = None
    sensitive_uid: str | None = None


# ─────────────────────────── SendPermit（无旁路结构保证） ───────────────────────────

#: 模块私有签发凭据——外部拿不到该对象即无法构造合法 Permit（对齐 state.SendToken 模式）
_PERMIT_BEARER = object()


@dataclass(frozen=True)
class SendPermit:
    """发送许可（一次性令牌）：执行通道唯一放行凭证，仅 RiskGuard 可签发。

    外部构造（bearer 缺失/非法）在 ``__post_init__`` 即 TypeError——
    「发送器仅接受风控签发的 SendPermit」是类型层保证而非约定。
    """

    permit_uid: str
    account_id: int
    conversation_uid: str
    action_type: str
    op_uid: str
    issued_at: datetime
    next_allowed_at: datetime
    _bearer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._bearer is not _PERMIT_BEARER:
            raise TypeError(
                "SendPermit 仅可由 RiskGuard 签发（外部构造被拒绝：强制路径无旁路）"
            )


class PermitAlreadyUsedError(RuntimeError):
    """SendPermit 重复消费（一次性语义被违反）。"""


# ─────────────────────────── 红线规则引擎（§6.4） ───────────────────────────


@dataclass(frozen=True)
class RiskRule:
    """红线拦截规则（risk_rule 行的进程内形态；源头 = FR-KB-004 结构化导出）。"""

    rule_code: str
    name: str
    pattern_type: str              # regex | keyword
    pattern: str                   # 正则 或 竖线分隔关键词


class RedlineEngine:
    """平台红线清单引擎：发送前内容匹配，命中即 BLOCK+告警+审计。

    reload 原子替换进程内规则引用（判定读旧表不锁，热加载瞬时生效）。
    """

    def __init__(self, rules: Iterable[RiskRule] = ()) -> None:
        self._rules: tuple[RiskRule, ...] = tuple(rules)

    @property
    def rules(self) -> tuple[RiskRule, ...]:
        """当前生效规则（只读）。"""
        return self._rules

    def reload(self, rules: Iterable[RiskRule]) -> None:
        """规则热替换（rule_sync_job 同步后调用）。"""
        self._rules = tuple(rules)

    def match(self, content: str) -> RiskRule | None:
        """内容匹配：regex 忽略大小写搜索；keyword 竖线词表包含匹配。"""
        if not content:
            return None
        lowered = content.lower()
        for rule in self._rules:
            if rule.pattern_type == "regex":
                if re.search(rule.pattern, content, re.IGNORECASE):
                    return rule
            elif rule.pattern_type == "keyword":
                keywords = (k.strip() for k in rule.pattern.lower().split("|"))
                if any(k and k in lowered for k in keywords):
                    return rule
        return None


# ─────────────────────────── 频率门：滑动窗口（§6.1） ───────────────────────────


class SlidingWindow:
    """滑动窗口频率记录（Redis ZSET/STRING 语义的进程内等价，RLock 串行化）。

    - 小时窗：每账号 (op_uid, ts) 队列，读时清出窗外记录（ZREMRANGEBYSCORE 语义）；
    - 日计数：按自然日键计数（TTL 换键语义由日期键自然承载）；
    - next_allowed：签发的下次允许时刻（PASS 才登记计数/签发间隔）。
    """

    def __init__(self) -> None:
        self._hour: dict[int, deque[tuple[str, float]]] = defaultdict(deque)
        self._day: dict[tuple[int, str], int] = defaultdict(int)
        self._next: dict[int, datetime] = {}

    def hour_count(self, account_id: int, now: datetime) -> int:
        """清窗后的小时窗计数（1h 滑动）。"""
        cutoff = now.timestamp() - 3600.0
        queue = self._hour[account_id]
        while queue and queue[0][1] <= cutoff:
            queue.popleft()
        return len(queue)

    def day_count(self, account_id: int, now: datetime) -> int:
        """当日操作计数（自然日键）。"""
        return self._day[(account_id, now.date().isoformat())]

    def next_allowed(self, account_id: int) -> datetime | None:
        """已签发的下次允许时刻（未签发 None）。"""
        return self._next.get(account_id)

    def register(self, account_id: int, op_uid: str, now: datetime) -> None:
        """登记一笔 PASS 操作（仅 PASS 调用：INTERCEPT/BLOCK 不消耗额度）。"""
        self._hour[account_id].append((op_uid, now.timestamp()))
        self._day[(account_id, now.date().isoformat())] += 1

    def set_next(self, account_id: int, at: datetime) -> None:
        """签发下次允许时刻（risk:gate:{aid}:next 键语义）。"""
        self._next[account_id] = at


# ─────────────────────────── 敏感操作草稿（§6.3） ───────────────────────────


@dataclass
class SensitiveDraft:
    """敏感操作草稿（sensitive_action 行：AI 只起草，人工确认后才能执行）。"""

    uid: str
    conversation_uid: str
    action_type: str
    payload: dict[str, object]
    status: str = "pending_confirm"    # pending_confirm / confirmed / rejected / expired
    created_at: datetime | None = None
    expires_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    reject_reason: str | None = None


class SensitiveDraftStore:
    """敏感草稿存储（demo 内存形态；生产 = PG sensitive_action 唯一权威）。"""

    def create(
        self,
        *,
        action_type: str,
        conversation_uid: str,
        payload: dict[str, object],
        now: datetime,
        expire_hours: int = 24,
    ) -> SensitiveDraft:
        """创建待确认草稿（expires_at = now + 时限，超时由巡检转 expired）。"""
        draft = SensitiveDraft(
            uid=uuid.uuid4().hex[:16],
            conversation_uid=conversation_uid,
            action_type=action_type,
            payload=dict(payload),
            created_at=now,
            expires_at=now + timedelta(hours=expire_hours),
        )
        self._items[draft.uid] = draft
        return draft

    def __init__(self) -> None:
        self._items: dict[str, SensitiveDraft] = {}

    def get(self, uid: str) -> SensitiveDraft | None:
        """按 uid 读取草稿。"""
        return self._items.get(uid)

    def _require(self, uid: str) -> SensitiveDraft:
        draft = self._items.get(uid)
        if draft is None:
            raise ValueError(f"敏感草稿 {uid} 不存在")
        return draft

    def confirm(self, uid: str, *, operator: str, now: datetime) -> SensitiveDraft:
        """人工确认（写决策人/时刻，双人可见留痕）。"""
        draft = self._require(uid)
        if draft.status != "pending_confirm":
            raise ValueError(f"草稿状态 {draft.status} 不可确认（仅 pending_confirm 可决策）")
        draft.status = "confirmed"
        draft.decided_by, draft.decided_at = operator, now
        return draft

    def reject(self, uid: str, *, operator: str, reason: str, now: datetime) -> SensitiveDraft:
        """人工驳回（理由留痕）。"""
        draft = self._require(uid)
        if draft.status != "pending_confirm":
            raise ValueError(f"草稿状态 {draft.status} 不可驳回（仅 pending_confirm 可决策）")
        draft.status = "rejected"
        draft.decided_by, draft.decided_at, draft.reject_reason = operator, now, reason
        return draft


# ─────────────────────────── 判定总门（§6.2） ───────────────────────────


class AlertSink(Protocol):
    """告警通道协议（生产 = FR-AG-007 POST /api/v1/alerts；demo = engine.InMemoryAlerts）。"""

    def send(self, level: str, type_: str, title: str, body: str) -> None: ...


#: 接管门禁：会话 uid → 状态（非 ai_managed 一律 DENY）
StateLookup = Callable[[str], str]
#: 敏感拦截联动回调：注入状态机迁移（pending_confirm），FR-AG-008 留痕
InterceptCallback = Callable[[str, SensitiveDraft], None]
AuditSink = Callable[[dict[str, object]], None]


class RiskGuard:
    """风控判定总门：全部对外动作必经，全部判定落审计，任何故障 fail-safe 停发。

    :param config: 风控参数（默认 SR-AG-005 §7 默认值）；
    :param rules: 红线规则集（注入注入——知识源 FR-KB-004 的结构化导出）；
    :param alerts: 告警通道（None 则静默，测试可注入 InMemoryAlerts）；
    :param audit: 审计钩子（全部判定含 PASS 双写）；
    :param state_lookup: 接管门禁（None = 全部视为 ai_managed）；
    :param on_intercept: 敏感拦截时的状态机迁移回调（pending_confirm 联动）；
    :param rng: 随机源注入（确定性种子供测试/演练复现）。
    """

    def __init__(
        self,
        *,
        config: RiskConfig | None = None,
        rules: Iterable[RiskRule] = (),
        redline: RedlineEngine | None = None,
        alerts: AlertSink | None = None,
        audit: AuditSink | None = None,
        state_lookup: StateLookup | None = None,
        on_intercept: InterceptCallback | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._config = config or RiskConfig()
        self._redline = redline if redline is not None else RedlineEngine(rules)
        self._alerts = alerts
        self._audit = audit
        self._state_lookup = state_lookup
        self._on_intercept = on_intercept
        self._rng = rng if rng is not None else random.Random()
        self._window = SlidingWindow()
        self.drafts = SensitiveDraftStore()
        self._seen: dict[str, RiskDecision] = {}
        self._issued_permits: set[str] = set()
        self._consumed_permits: set[str] = set()

    # ---- 对外：判定 ----

    def check(self, action: RiskAction, *, now: datetime) -> RiskDecision:
        """判定总门（SR-AG-005 §6.2 强制顺序）。

        op_uid 幂等：重复判定回放首次结果（risk_event 唯一约束语义）；
        fail-safe：_check_inner 任何异常 → FAILSAFE（一律 BLOCK，宁可停发）。
        """
        seen = self._seen.get(action.op_uid)
        if seen is not None:
            return seen
        try:
            decision = self._check_inner(action, now)
        except Exception as exc:  # noqa: BLE001 —— fail-safe：组件故障绝不放行
            decision = self._failsafe(action, now, exc)
        self._seen[action.op_uid] = decision
        return decision

    def next_allowed_at(self, account_id: int, base: datetime | None = None) -> datetime:
        """对数正态采样下次允许时刻（拟人化间隔，NFR-SEC-11）。

        gap = exp(N(μ,σ)) 夹取 [min_gap, max_gap]；落在夜间/工作窗外顺延
        次日窗口起点 + 入场噪声 U(60,900)s（活跃时段模型，防 M2/M4）。
        """
        now = base if base is not None else datetime.now()
        gap_ms = self._rng.lognormvariate(self._config.ln_mu, self._config.ln_sigma)
        gap_s = min(max(gap_ms / 1000.0, self._config.min_gap_s), self._config.max_gap_s)
        moment = now + timedelta(seconds=gap_s)
        if not self._is_open(moment):
            moment = self._next_work_open(moment)
        self._window.set_next(account_id, moment)
        return moment

    # ---- 对外：敏感草稿决策 ----

    def confirm_sensitive(self, uid: str, *, operator: str, now: datetime) -> SensitiveDraft:
        """人工确认敏感草稿（确认后经 execute_confirmed 放行，仍过红线+频率门）。"""
        return self.drafts.confirm(uid, operator=operator, now=now)

    def reject_sensitive(
        self, uid: str, *, operator: str, reason: str, now: datetime
    ) -> SensitiveDraft:
        """人工驳回敏感草稿（永不执行）。"""
        return self.drafts.reject(uid, operator=operator, reason=reason, now=now)

    def execute_confirmed(self, uid: str, action: RiskAction, *, now: datetime) -> RiskDecision:
        """确认后放行：复跑红线 + 频率门（不再二次敏感拦截——人工已决策）。"""
        draft = self.drafts.get(uid)
        if draft is None:
            raise ValueError(f"敏感草稿 {uid} 不存在")
        if draft.status == "rejected":
            raise ValueError("sensitive draft rejected（已驳回草稿不可放行，§7.4）")
        if draft.status != "confirmed":
            raise ValueError("sensitive draft pending_confirm（需人工确认后方可放行）")
        try:
            rule = self._redline.match(action.content)
            if rule is not None:
                self._alert("high", "risk_hit", f"红线拦截 {rule.rule_code}", action)
                return self._emit(action, Verdict.BLOCK, f"redline:{rule.rule_code}",
                                  rule_code=rule.rule_code, now=now)
            verdict, next_at, rate_reason = self._admit(action, now)
            if verdict is not Verdict.PASS:
                return self._emit(action, verdict, rate_reason,
                                  next_allowed_at=next_at, now=now)
            return self._emit(action, Verdict.PASS, "confirmed_executed",
                              next_allowed_at=self.next_allowed_at(action.account_id, base=now),
                              permit=self._issue(action, now), now=now)
        except Exception as exc:  # noqa: BLE001
            return self._failsafe(action, now, exc)

    # ---- 对外：Permit 消费（无旁路） ----

    def consume(self, permit: SendPermit) -> None:
        """Permit 一次性消费登记：重复消费抛 PermitAlreadyUsedError。"""
        if permit.permit_uid not in self._issued_permits:
            raise ValueError(f"SendPermit {permit.permit_uid} 非本风控实例签发，拒绝执行")
        if permit.permit_uid in self._consumed_permits:
            raise PermitAlreadyUsedError(f"SendPermit {permit.permit_uid} 已消费（一次性令牌）")
        self._consumed_permits.add(permit.permit_uid)

    # ---- 内部：判定序 ----

    def _check_inner(self, action: RiskAction, now: datetime) -> RiskDecision:
        """判定序（SR-AG-005 §6.2）：接管 → 红线 → 敏感 → 频率。"""
        # ① 接管门禁：人工接管/等待确认会话 AI 一律静默（不告警，属正常业务）
        if self._state_lookup is not None:
            state = self._state_lookup(action.conversation_uid)
            if state != "ai_managed":
                return self._emit(action, Verdict.DENY, "session_not_ai_managed", now=now)
        # ② 平台红线内容匹配：命中即 BLOCK + 高级告警（永不执行）
        rule = self._redline.match(action.content)
        if rule is not None:
            self._alert("high", "risk_hit", f"红线拦截 {rule.rule_code}", action)
            return self._emit(action, Verdict.BLOCK, f"redline:{rule.rule_code}",
                              rule_code=rule.rule_code, now=now)
        # ③ 敏感操作白名单拦截：INTERCEPT→草稿→pending_confirm 联动（迁移回调注入）
        if classify_action(action.action_type, self._config) == "intercept":
            draft = self.drafts.create(
                action_type=action.action_type, conversation_uid=action.conversation_uid,
                payload={"content": action.content}, now=now,
                expire_hours=self._config.confirm_expire_hours,
            )
            if self._on_intercept is not None:
                self._on_intercept(action.conversation_uid, draft)
            self._alert("high", "risk_hit", f"敏感操作待确认 {action.action_type}", action)
            return self._emit(action, Verdict.INTERCEPT, "sensitive_action_draft",
                              sensitive_uid=draft.uid, now=now)
        # ④ 频率门：超限排队（小时→下一窗口；日→次日工作窗口）
        verdict, next_at, rate_reason = self._admit(action, now)
        if verdict is not Verdict.PASS:
            self._alert("mid", "risk_hit", f"频率超限 {verdict.value}", action)
            return self._emit(action, verdict, rate_reason, next_allowed_at=next_at, now=now)
        # ⑤ PASS：签发拟人间隔 + SendPermit（唯一放行出口）
        next_at = self.next_allowed_at(action.account_id, base=now)
        return self._emit(action, Verdict.PASS, "ok", next_allowed_at=next_at,
                          permit=self._issue(action, now), now=now)

    def _admit(self, action: RiskAction, now: datetime) -> tuple[Verdict, datetime | None, str]:
        """频率门：间隔 → 日上限 → 小时上限（PASS 才登记计数）。"""
        cfg = self._config
        issued = self._window.next_allowed(action.account_id)
        if issued is not None and now < issued:               # 间隔未到：等待签发时刻
            return Verdict.QUEUE_HOUR, issued, "interval_wait"
        if self._window.day_count(action.account_id, now) >= cfg.max_ops_per_day:
            return Verdict.QUEUE_NEXT_DAY, self._next_work_open(now), \
                "rate_limit:daily_cap"
        if self._window.hour_count(action.account_id, now) >= cfg.max_ops_per_hour:
            return Verdict.QUEUE_HOUR, now + timedelta(minutes=cfg.hold_minutes), \
                "rate_limit:hourly_cap"
        self._window.register(action.account_id, action.op_uid, now)
        return Verdict.PASS, None, "ok"

    def _issue(self, action: RiskAction, now: datetime) -> SendPermit:
        """签发一次性 Permit（模块私有 bearer，外部无法伪造）。"""
        permit = SendPermit(
            permit_uid=uuid.uuid4().hex[:16], account_id=action.account_id,
            conversation_uid=action.conversation_uid, action_type=action.action_type,
            op_uid=action.op_uid, issued_at=now,
            next_allowed_at=self._window.next_allowed(action.account_id) or now,
            _bearer=_PERMIT_BEARER,
        )
        self._issued_permits.add(permit.permit_uid)
        return permit

    def _failsafe(self, action: RiskAction, now: datetime, exc: Exception) -> RiskDecision:
        """fail-safe：风控组件任何异常 → 一律 BLOCK（宁可停发损失当班询盘）。"""
        self._alert("high", "system_error", "风控组件异常，fail-safe 停发", action, body=str(exc))
        decision = RiskDecision(
            verdict=Verdict.FAILSAFE,
            reason=f"failsafe:blocked（{type(exc).__name__}: {exc}）——宁停不险",
        )
        if self._audit is not None:
            self._audit(self._audit_row(action, decision, now))
        return decision

    # ---- 内部：辅助 ----

    def _is_open(self, moment: datetime) -> bool:
        """活跃时段判定（工作窗口内；demo 仅小时窗，周末档随生产配置扩展）。"""
        start_h, start_m = _parse_hm(self._config.work_start)
        end_h, end_m = _parse_hm(self._config.work_end)
        minutes = moment.hour * 60 + moment.minute
        return start_h * 60 + start_m <= minutes < end_h * 60 + end_m

    def _next_work_open(self, moment: datetime) -> datetime:
        """次日工作窗口起点 + 入场噪声 U(60,900)s（防整点聚集，M2）。"""
        start_h, start_m = _parse_hm(self._config.work_start)
        day_start = (moment + timedelta(days=1)).replace(
            hour=start_h, minute=start_m, second=0, microsecond=0)
        return day_start + timedelta(seconds=self._rng.uniform(60.0, 900.0))

    def _alert(self, level: str, type_: str, title: str, action: RiskAction,
               body: str = "") -> None:
        """告警投递（best-effort：告警故障不影响判定主流程）。"""
        if self._alerts is None:
            return
        try:
            self._alerts.send(level=level, type_=type_, title=title,
                              body=body or f"account={action.account_id} op={action.op_uid}")
        except Exception:  # noqa: BLE001
            pass

    def _emit(self, action: RiskAction, verdict: Verdict, reason: str, *, now: datetime,
              rule_code: str | None = None, next_allowed_at: datetime | None = None,
              permit: SendPermit | None = None, sensitive_uid: str | None = None) -> RiskDecision:
        """判定出口：构造决策 + 全量审计（含 PASS，NFR-SEC-03）。"""
        decision = RiskDecision(
            verdict=verdict, reason=reason, rule_code=rule_code,
            next_allowed_at=next_allowed_at, permit=permit, sensitive_uid=sensitive_uid,
        )
        if self._audit is not None:
            self._audit(self._audit_row(action, decision, now))
        return decision

    def _audit_row(self, action: RiskAction, decision: RiskDecision,
                   now: datetime) -> dict[str, object]:
        """审计行：verdict/op_uid/原因/规则码/排队时刻（全判定留痕可查询）。"""
        return {
            "event": "risk_decision",
            "op_uid": action.op_uid,
            "account_id": action.account_id,
            "conversation_uid": action.conversation_uid,
            "action_type": action.action_type,
            "verdict": decision.verdict.value,
            "reason": decision.reason,
            "rule_code": decision.rule_code,
            "next_allowed_at": decision.next_allowed_at.isoformat()
            if decision.next_allowed_at else None,
            "at": now.isoformat(),
        }


class PermitOnlySender:
    """发送器唯一入口（无旁路结构断言的 demo 形态）。

    仅接受 :class:`SendPermit` 实例（TypeError 拒绝其他类型）、仅接受本风控实例
    签发的 Permit（ValueError 拒绝伪造）、一次性消费（PermitAlreadyUsedError）。
    生产 = agent-runner RiskControlledChannel 私有持有 OpenClaw 句柄（SR-AG-005 §6.6）。
    """

    def __init__(self, guard: RiskGuard) -> None:
        self._guard = guard

    def send(self, permit: SendPermit, text: str) -> str:
        """唯一发送入口：校验 Permit 类型 → 归属 → 一次性消费 → 交执行通道。"""
        if not isinstance(permit, SendPermit):
            raise TypeError("发送器仅接受风控签发的 SendPermit（强制路径无旁路）")
        self._guard.consume(permit)
        return text


__all__ = [
    "Verdict",
    "RiskConfig",
    "RiskAction",
    "RiskDecision",
    "RiskRule",
    "RedlineEngine",
    "SlidingWindow",
    "SensitiveDraft",
    "SensitiveDraftStore",
    "classify_action",
    "SendPermit",
    "PermitAlreadyUsedError",
    "PermitOnlySender",
    "RiskGuard",
]
