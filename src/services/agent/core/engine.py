# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""任务主循环与拟人化输入器（FR-AG-002 + NFR-SEC-11，SR-AG-002 §6.1/§6.3/§6.5）。

职责（agent-runner 执行器业务面，demo 进程内形态）：
- AgentRunnerEngine.step()：单步可测的任务主循环——登录保持探测 → 询盘轮询 →
  水位过滤（is_new）→ 消息读取 → AI 大脑回调（process_reply，生产 = FR-AG-003）
  → 发送闸门 acquire/verify → 拟人化输入发送 → 审计留痕；
- WatermarkGate：处理水位 + 幂等去重双保险（SR-AG-002 §6.3）——(平台时间, 外部 ID)
  元组比较防旧消息重放，(会话, 时间) 去重键防列表重排二次进入；游标经
  PollCursorStore 持久化（生产 = PG agent_poll_cursor，进程可随时 kill）；
- HumanTyper：拟人化输入计划（NFR-SEC-11）——逐字 3-5 字符/秒，间隔按对数正态
  分布采样（右偏、拟人节奏），随机停顿 150-800ms；产出的 TypingPlan 交驱动执行；
- 降级通道：页面改版 → high 告警 + frozen:page_drift；验证码 → high 告警 +
  frozen:captcha（人工接管视口）；Cookie 失效 → frozen:cookie_expired（不暴力重登）；
  冻结后 step() 拒绝执行（宁停勿撞，NFR-SEC-10），人工恢复 resume() 放行。

demo 形态边界：同步单线程串行（同账号动作严格串行的结构化保证）；告警/审计为
进程内钩子（生产 = FR-AG-007 POST /api/v1/alerts + audit_log 哈希链）。
"""

from __future__ import annotations

import hashlib
import math
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from services.agent.core.driver import (
    CaptchaRequiredError,
    InquiryItem,
    NotLoggedInError,
    PageDriftError,
    PlatformMessage,
    TypingPlan,
)
from services.agent.core.state import AISendGate, ConversationStateMachine

#: 会话 uid 形态：channel:external（SR-AG-002 §4 会话定位规则）
UID_CHANNEL = "alibaba"


def conv_uid(external_id: str) -> str:
    """外部会话 ID → 系统会话 uid（channel:external）。"""
    return f"{UID_CHANNEL}:{external_id}"


def external_of(uid: str) -> str:
    """系统会话 uid → 外部会话 ID（恢复重放查证用）。"""
    return uid.split(":", 1)[1] if UID_CHANNEL + ":" in uid else uid


# ─────────────────────────── 处理水位（防重复处理核心） ───────────────────────────


@dataclass(frozen=True)
class PollCursor:
    """轮询水位（SR-AG-002 §4 agent_poll_cursor 行的进程内形态）。

    水位① last_seen_time：最后已处理消息的平台时间（epoch 秒）；
    水位② last_external_id：同秒消息的外部 ID 次级排序（元组比较）。
    """

    last_seen_time: int = 0
    last_external_id: str = ""

    def as_tuple(self) -> tuple[int, str]:
        """水位元组（比较语义：仅严格大于者视为新）。"""
        return (self.last_seen_time, self.last_external_id)


class PollCursorStore(Protocol):
    """游标存储协议（生产 = PG 主存 + Redis 热镜像；重启后从权威恢复）。"""

    def load(self, account_id: int) -> PollCursor: ...

    def save(self, account_id: int, cursor: PollCursor) -> None: ...


class InMemoryPollCursorStore:
    """内存游标存储（demo）：线程安全字典。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cursors: dict[int, PollCursor] = {}

    def load(self, account_id: int) -> PollCursor:
        with self._lock:
            return self._cursors.get(account_id, PollCursor())

    def save(self, account_id: int, cursor: PollCursor) -> None:
        with self._lock:
            self._cursors[account_id] = cursor


class WatermarkGate:
    """水位 + 幂等双保险（SR-AG-002 §6.3 Poller 的等价实现）。

    is_new = (time, ext_id) 严格大于游标 AND (ext_id, time) 未见过；
    advance = 游标取 max 元组 + 去重键登记。重启后从游标存储恢复（状态外置）。
    """

    def __init__(self, cursor_store: PollCursorStore, account_id: int) -> None:
        self._store = cursor_store
        self._account_id = account_id
        self._cursor = cursor_store.load(account_id)
        self._seen: set[tuple[str, int]] = set()  # demo 去重键（生产 = Redis SETNX 7d + PG UNIQUE）

    @property
    def cursor(self) -> PollCursor:
        """当前水位（只读快照）。"""
        return self._cursor

    def is_new(self, item: InquiryItem) -> bool:
        """新条目判定：水位元组严格递进且未在去重键集合中（防回拨/重排二次进入）。"""
        if (item.last_msg_time, item.external_conv_id) <= self._cursor.as_tuple():
            return False
        return (item.external_conv_id, item.last_msg_time) not in self._seen

    def advance(self, item: InquiryItem) -> None:
        """处理受理后推进：游标单调右移 + 去重键登记，双落存储。"""
        if item.last_msg_time > self._cursor.last_seen_time or (
            item.last_msg_time == self._cursor.last_seen_time
            and item.external_conv_id > self._cursor.last_external_id
        ):
            self._cursor = PollCursor(item.last_msg_time, item.external_conv_id)
        self._seen.add((item.external_conv_id, item.last_msg_time))
        self._store.save(self._account_id, self._cursor)


# ─────────────────────────── 告警钩子 ───────────────────────────


class AlertHook(Protocol):
    """告警通道协议（生产 = FR-AG-007 POST /api/v1/alerts）。"""

    def send(self, level: str, type_: str, title: str, body: str) -> None: ...


@dataclass(frozen=True)
class AlertMessage:
    """告警消息（进程内记录形态）。"""

    level: str
    type: str
    title: str
    body: str


class InMemoryAlerts:
    """内存告警记录（demo）：供测试断言告警触达与级别。"""

    def __init__(self) -> None:
        self.items: list[AlertMessage] = []

    def send(self, level: str, type_: str, title: str, body: str) -> None:
        self.items.append(AlertMessage(level=level, type=type_, title=title, body=body))

    def last(self, level: str | None = None, type_: str | None = None) -> AlertMessage | None:
        """最近一条匹配告警（无则 None）。"""
        for item in reversed(self.items):
            if (level is None or item.level == level) and (type_ is None or item.type == type_):
                return item
        return None


# ─────────────────────────── 拟人化输入器（NFR-SEC-11） ───────────────────────────


class HumanTyper:
    """拟人化输入计划器：逐字 3-5 字符/秒 + 随机停顿（SR-AG-002 §6.5）。

    采样模型：字符速度 cps ~ LogNormal(mu=ln(mid), sigma) 截断到 cps_range，
    逐字延迟 = 1000 / cps ∈ [200, 333.4]ms（3-5 字符/秒）；以 pause_prob 概率在
    字符后插入 LogNormal 停顿，截断到 pause_ms_range ∈ [150, 800]ms。
    分布右偏（mean > median）为对数正态签名，可由采样统计断言。
    """

    def __init__(
        self,
        rng: random.Random,
        *,
        cps_range: tuple[float, float] = (3.0, 5.0),
        pause_prob: float = 0.10,
        pause_ms_range: tuple[float, float] = (150.0, 800.0),
    ) -> None:
        if cps_range[0] <= 0 or cps_range[1] < cps_range[0]:
            raise ValueError("cps_range 必须为正且有序（字符/秒）")
        self._rng = rng
        self._cps_lo, self._cps_hi = cps_range
        self._pause_prob = pause_prob
        self._pause_lo, self._pause_hi = pause_ms_range
        mid = (self._cps_lo + self._cps_hi) / 2.0
        self._mu = math.log(mid)
        self._sigma = 0.2            # 对数正态形状参数（右偏拟人节奏）
        self._pause_mu = math.log((self._pause_lo + self._pause_hi) / 2.0)
        self._pause_sigma = 0.4

    def plan(self, text: str) -> TypingPlan:
        """生成输入计划：逐字延迟序列 + 停顿序列（文本保真，逐字输入语义）。"""
        delays: list[float] = []
        pauses: list[tuple[int, float]] = []
        for index in range(len(text)):
            cps = min(max(self._rng.lognormvariate(self._mu, self._sigma), self._cps_lo), self._cps_hi)
            delays.append(round(1000.0 / cps, 2))
            if self._rng.random() < self._pause_prob:
                duration = min(
                    max(self._rng.lognormvariate(self._pause_mu, self._pause_sigma),
                        self._pause_lo),
                    self._pause_hi,
                )
                pauses.append((index, round(duration, 2)))
        return TypingPlan(text=text, char_delays_ms=tuple(delays), pauses=tuple(pauses))


# ─────────────────────────── 任务主循环 ───────────────────────────


@dataclass(frozen=True)
class ConvCtx:
    """会话上下文（AI 大脑回调入参：定位 + 买家提示）。"""

    uid: str
    external_conv_id: str
    buyer_name: str


#: AI 大脑回调（生产 = FR-AG-003 POST /api/v1/ai/reply）：返回草稿文本，None=不回复
ProcessReply = Callable[[ConvCtx, list[PlatformMessage]], "str | None"]


class EngineFrozenError(RuntimeError):
    """执行通道已冻结：宁停勿撞（NFR-SEC-10），等待人工 resume。"""


@dataclass(frozen=True)
class StepReport:
    """单步执行报告（可观测口径：轮询/新条目/读取/发送/跳过/终态）。"""

    polled: int          # 轮询条目数
    new_items: int       # 通过水位判定的新条目数
    read_msgs: int       # 读取消息数
    sent: int            # 发送数（经闸门）
    skipped_seen: int    # 去重键命中跳过数
    status: str          # ok / frozen:page_drift / frozen:captcha / frozen:cookie_expired


#: 冻结原因 → 告警类型映射（FR-AG-007 告警触点）
_FREEZE_ALERT_TYPE = {
    "frozen:page_drift": "page_drift",
    "frozen:captcha": "captcha",
    "frozen:cookie_expired": "cookie_expired",
}


class AgentRunnerEngine:
    """单账号任务循环（SR-AG-002 §6.1 AgentTaskLoop 的单步可测形态）。

    同账号动作严格串行（同步单步，从结构上杜绝并发触发平台）；所有对外动作
    前经登录探测/页面指纹检查；发送必经状态机门禁 + AISendGate 物理闸门。
    """

    def __init__(
        self,
        *,
        account_id: int,
        driver: BrowserDriver,
        state_machine: ConversationStateMachine,
        gate: AISendGate,
        cursor_store: PollCursorStore,
        typer: HumanTyper,
        process_reply: ProcessReply,
        alerts: AlertHook | None = None,
        audit: Callable[[dict[str, object]], None] | None = None,
        risk_guard: "RiskGuard | None" = None,
    ) -> None:
        # CR R1 接线：风控引擎可选注入——None 仅限无风控演示形态（既有装配/测试兼容）；
        # 生产必配（FR-AG-005：发送前必须过 check，敏感 INTERCEPT/红线 BLOCK 拦截）
        self._risk_guard = risk_guard
        self._account_id = account_id
        self._driver = driver
        self._sm = state_machine
        self._gate = gate
        self._typer = typer
        self._process_reply = process_reply
        self._alerts = alerts
        self._audit = audit
        self.watermark = WatermarkGate(cursor_store, account_id)
        self.login_state: str = "alive"

    # ---- 对外 ----

    @property
    def frozen(self) -> bool:
        """是否处于冻结（业务静默等人工）。"""
        return self.login_state != "alive"

    def resume(self) -> None:
        """人工恢复（接管视口完成验证/受控重登/选择器更新后调用）。"""
        self.login_state = "alive"

    def ensure_conversation(self, external_id: str, buyer_hint: str = "") -> ConvCtx:
        """会话定位（SR-AG-002 §4）：未命中则新建（默认 ai_managed，FR-AG-008）。"""
        uid = conv_uid(external_id)
        self._sm.ensure_conversation(uid)
        return ConvCtx(uid=uid, external_conv_id=external_id, buyer_name=buyer_hint)

    def step(self) -> StepReport:
        """单步主循环：poll → read → process → gate → send（任一冻结即降级返回）。"""
        if self.frozen:
            raise EngineFrozenError(
                f"执行通道已冻结（{self.login_state}）：等待人工恢复，拒绝执行任何平台动作"
            )
        try:
            self.login_state = self._driver.ensure_login()   # ① 登录保持：不暴力重登
        except Exception as exc:                             # noqa: BLE001 统一降级分类
            return self._freeze_from(exc)
        try:
            items = self._driver.poll_inquiries()            # ② 询盘轮询
        except Exception as exc:                             # noqa: BLE001
            return self._freeze_from(exc)
        report = StepReport(polled=len(items), new_items=0, read_msgs=0,
                            sent=0, skipped_seen=0, status="ok")
        for item in items:
            if not self.watermark.is_new(item):              # ③ 水位+幂等双保险
                report = self._bump(report, skipped_seen=1)
                continue
            conv = self.ensure_conversation(item.external_conv_id, item.buyer_name)
            messages = self._driver.read_messages(item.external_conv_id)  # ④ 消息读取
            self.watermark.advance(item)                     # 处理受理即推进水位
            report = self._bump(report, new_items=1, read_msgs=len(messages))
            draft = self._process_reply(conv, messages)      # ⑤ AI 大脑回调
            if draft is None:
                continue
            if self._send(conv, draft):                      # ⑥ 门禁 + 闸门 + 发送
                report = self._bump(report, sent=1)
        return report

    # ---- 内部 ----

    @staticmethod
    def _bump(report: StepReport, **deltas: int) -> StepReport:
        """报告累加（frozen dataclass 字段替换，不可变口径）。"""
        return replace(report, **deltas)

    def _send(self, conv: ConvCtx, draft: str) -> bool:
        """发送链：状态机门禁 → **风控判定（CR R1 接线，FR-AG-005）** → 闸门 → 发送。"""
        state, _version = self._sm.get_state(conv.uid)
        if not self._sm.send_allowed(state):
            return False                                     # 人工接管/挂起/关闭：AI 静默
        if self._risk_guard is not None:
            from datetime import datetime, timezone

            from services.agent.core.risk.guard import (
                RiskAction,
                Verdict,
            )

            decision = self._risk_guard.check(
                RiskAction(
                    op_uid=f"engine:{conv.uid}:{hash(draft) & 0xFFFFFFFF}",
                    account_id=self._account_id,
                    conversation_uid=conv.uid,
                    action_type="send_message",
                    content=draft,
                ),
                now=datetime.now(timezone.utc),
            )
            if decision.verdict is not Verdict.PASS:
                # INTERCEPT（敏感草稿待确认）/ BLOCK（红线）/ QUEUE_*（限额）一律不发送；
                # INTERCEPT 额外迁移会话至 pending_confirm（人工确认链路入口）
                if decision.verdict is Verdict.INTERCEPT:
                    self._sm.transition(conv.uid, "pending_confirm", actor="risk_guard",
                                        reason=f"sensitive:{decision.reason}")
                if self._alerts is not None:
                    self._alerts.send("mid", "risk_hit",
                                       f"风控拦截 {decision.verdict.value}", decision.reason)
                return False
        token = self._gate.acquire(conv.uid)                 # 非法态抛 AISendBlockedError
        plan = self._typer.plan(draft)                       # 拟人化输入计划
        if not self._gate.verify(conv.uid, token):           # 发送前最后一瞬校验
            return False                                     # 竞态（如生成中接管）：放弃
        receipt = self._driver.send_reply(token, plan)
        if self._audit is not None:
            self._audit({
                "event": "send_reply",
                "conv_uid": conv.uid,
                "receipt_id": receipt.external_msg_id,
                "content_len": receipt.content_len,          # 只记长度不记内容（NFR-SEC-01）
                "content_fp": hashlib.sha256(draft.encode("utf-8")).hexdigest()[:8],
            })
        return True

    def _freeze_from(self, exc: Exception) -> StepReport:
        """异常 → 冻结分类：验证码/Cookie 失效/页面改版 → high 告警 + 业务静默。"""
        if isinstance(exc, CaptchaRequiredError):
            reason = "frozen:captcha"
        elif isinstance(exc, NotLoggedInError):
            reason = "frozen:cookie_expired"
        elif isinstance(exc, PageDriftError):
            reason = "frozen:page_drift"
        else:                                                # 未知异常：保守冻结待巡检
            reason = "frozen:unknown_error"
        self.login_state = reason
        if self._alerts is not None:
            self._alerts.send(
                level="high",
                type_=_FREEZE_ALERT_TYPE.get(reason, "system_error"),
                title=f"账号{self._account_id} 执行通道冻结（{reason}）",
                body=str(exc),
            )
        return StepReport(polled=0, new_items=0, read_msgs=0, sent=0,
                          skipped_seen=0, status=reason)
