# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""会话量约束门（NFR-SEC-12，SR-NFR-SEC-12 §6.1/§6.3；迭代 2.3 demo 口径）。

上下限并存语义（设计核心，两线不矛盾）：
- **下限 15 条/日 = 能力验收线**：只统计 + 低于告警提示核查，不拦截任何动作；
- **上限 60 条/日（默认，配置化）= 安全保护线（风控闸门）**：梯度软限制 →
  达上限新会话自动转人工 + 高级告警（强制执行）。

工程形态（Redis SET + Lua 的进程内等价）：
- 计数对象 = 当日 AI 自动接入新会话数（SET 去重：同会话当日仅占 1 额度，幂等）；
- 梯度：80% 预警（当日每阶段至多 1 条告警）→ 90% 仅 A 级自动（B 级/未知分级
  保守转人工）→ 100% 全转人工；
- 次日自动恢复：日期键换日计数自然清零，无需显式重置；
- fail-safe：内部任何异常 → 一律转人工（宁保守不冒险）+ system_error 高级告警。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol


class AlertSink(Protocol):
    """告警通道协议（生产 = FR-AG-007）。"""

    def send(self, level: str, type_: str, title: str, body: str) -> None: ...


#: 转人工执行回调：(conversation_uid, stage) —— 注入状态机迁移（human_managed 留痕）
TransferCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class QuotaDecision:
    """会话量判定出参（§5-1 admit 响应的进程内形态）。"""

    verdict: str                     # ADMIT / TRANSFER_HUMAN
    stage: str                       # ok / repeat / warn / vip_only / cap_reached / failsafe
    used: int                        # 当日已接入会话数（TRANSFER 时为占位回滚后的值）
    cap: int                         # 当日生效上限
    next_day_open: datetime | None = None   # 转人工时的次日恢复时刻


class ConversationQuotaGate:
    """会话量门：新会话首动作判定额度（SR-NFR-SEC-12 §6.1）。

    :param daily_cap: 全局默认日上限（默认 60，risk.limits.daily_conversation_cap）；
    :param floor: 日处理下限（默认 15，验收线——只提示核查不拦截）；
    :param warn_ratio / vip_ratio: 梯度阈值（80% / 90%）；
    :param vip_grades: 高优先级客户分级集合（未知分级按非 A 保守转人工）；
    :param on_transfer: 转人工执行回调（状态机迁移注入点）。
    """

    def __init__(
        self,
        *,
        daily_cap: int = 60,
        floor: int = 15,
        warn_ratio: float = 0.8,
        vip_ratio: float = 0.9,
        vip_grades: tuple[str, ...] = ("A",),
        alerts: AlertSink | None = None,
        on_transfer: TransferCallback | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not (0.0 < warn_ratio < vip_ratio <= 1.0):
            raise ValueError("梯度阈值非法：须 0 < warn_ratio < vip_ratio <= 1")
        # 注：「下限须 < 上限」的配置联动校验属配置治理面（SR §6.5，配置中心保存时校验）；
        # 门运行时不重复强校验——demo 小上限（cap=10）与验收下限（15）并存属合法演练口径。
        self._daily_cap = daily_cap
        self._floor = floor
        self._warn_ratio = warn_ratio
        self._vip_ratio = vip_ratio
        self._vip_grades = frozenset(vip_grades)
        self._alerts = alerts
        self._on_transfer = on_transfer
        self._clock = clock or (lambda: datetime.now())
        # 当日 AI 接入会话集合（risk:conv:{acct}:d:{date} SET 键语义，日期键换日即恢复）
        self._admitted: dict[tuple[int, str], set[str]] = {}
        # 梯度告警当日去重（risk:conv:alerted HASH 键语义：每阶段至多 1 条）
        self._alerted: dict[tuple[int, str], set[str]] = {}
        # 下限提示当日去重
        self._floor_hint_date: dict[int, str] = {}

    # ---------------------------------------------------------------- 判定入口

    def admit(
        self, account_id: int, conversation_uid: str, grade: str, *,
        now: datetime | None = None,
    ) -> QuotaDecision:
        """会话量判定（SR §6.1 Lua「占位→判梯度→不合格回滚」的进程内等价）。

        :param now: 判定时刻（None = 注入时钟；时钟/内部故障走 fail-safe 转人工）。
        """
        try:
            moment = now if now is not None else self._clock()
            return self._admit_inner(account_id, conversation_uid, grade, moment)
        except Exception:  # noqa: BLE001 —— fail-safe：计数/时钟故障一律保守转人工
            fallback_now = datetime.now()
            self._alert("high", "system_error", "会话量门内部异常，新会话全量转人工",
                        f"account={account_id} conv={conversation_uid}")
            return QuotaDecision(verdict="TRANSFER_HUMAN", stage="failsafe",
                                 used=self._safe_used(account_id, fallback_now),
                                 cap=self._daily_cap,
                                 next_day_open=self._next_day_open(fallback_now))

    def _admit_inner(
        self, account_id: int, conversation_uid: str, grade: str, now: datetime
    ) -> QuotaDecision:
        bucket = self._admitted.setdefault((account_id, now.date().isoformat()), set())
        # ① SET 去重：同会话重复 admit 幂等秒放行，不重复计费
        if conversation_uid in bucket:
            return QuotaDecision("ADMIT", "repeat", len(bucket), self._daily_cap)
        # ② 原子占位
        bucket.add(conversation_uid)
        used = len(bucket)
        # ③ 100% 全转人工（第 cap+1 个：回滚占位）
        if used > self._daily_cap:
            bucket.discard(conversation_uid)
            return self._transfer(account_id, conversation_uid, "cap_reached",
                                  used - 1, now)
        # ④ 90% 仅 A 级自动（B 级/未知分级保守转人工，回滚占位）
        if used > int(self._daily_cap * self._vip_ratio) and grade not in self._vip_grades:
            bucket.discard(conversation_uid)
            return self._transfer(account_id, conversation_uid, "vip_only",
                                  used - 1, now)
        # ⑤ 80% 预警：放行 + 主管资源调度提示（当日去重）
        if used >= int(self._daily_cap * self._warn_ratio):
            self._alert_stage_once(account_id, "warn", used, now)
            return QuotaDecision("ADMIT", "warn", used, self._daily_cap)
        return QuotaDecision("ADMIT", "ok", used, self._daily_cap)

    def _transfer(
        self, account_id: int, conversation_uid: str, stage: str, used: int, now: datetime
    ) -> QuotaDecision:
        """转人工执行链：告警（100% 高级 / 90% 中级）→ 迁移回调 → 次日恢复时刻。"""
        level = "high" if stage == "cap_reached" else "mid"
        type_ = "conv_cap_reached" if stage == "cap_reached" else "conv_cap_vip_only"
        self._alert_stage_once(account_id, stage, used, now, level=level, type_=type_)
        if self._on_transfer is not None:
            self._on_transfer(conversation_uid, stage)
        return QuotaDecision(verdict="TRANSFER_HUMAN", stage=stage, used=used,
                             cap=self._daily_cap, next_day_open=self._next_day_open(now))

    # ---------------------------------------------------------------- 查询面

    def used(self, account_id: int, *, now: datetime) -> int:
        """当日已接入会话数（看板实时值）。"""
        return self._safe_used(account_id, now)

    def daily_floor_check(
        self, account_id: int, *, processed: int, now: datetime
    ) -> dict[str, object]:
        """下限核查（能力验收线，只提示不拦截）：processed < floor → 提示核查告警。

        零流量（processed=0）不告警——归因平台侧流量不足，非系统核查项
        （NFR-PER-04 归因二分口径的联动简化）。
        """
        meet = processed >= self._floor
        if not meet and processed > 0:
            date_key = now.date().isoformat()
            if self._floor_hint_date.get(account_id) != date_key:
                self._floor_hint_date[account_id] = date_key
                self._alert("mid", "quota_floor_hint",
                            f"账号{account_id} 日处理量 {processed} 未达下限 {self._floor}，"
                            "请核查（流量/风控/兜底归因）",
                            f"processed={processed} floor={self._floor}")
        return {"meet": meet, "processed": processed, "floor": self._floor}

    # ---------------------------------------------------------------- 内部

    def _safe_used(self, account_id: int, now: datetime) -> int:
        """used 查询（fail-safe 包裹：任何异常按 0 处理，不向上抛）。"""
        try:
            return len(self._admitted.get((account_id, now.date().isoformat()), set()))
        except Exception:  # noqa: BLE001
            return 0

    def _alert_stage_once(
        self, account_id: int, stage: str, used: int, now: datetime,
        *, level: str = "mid", type_: str = "conv_cap_warn",
    ) -> None:
        """梯度告警当日去重（每阶段至多 1 条，HSETNX 语义）。"""
        key = (account_id, now.date().isoformat())
        alerted = self._alerted.setdefault(key, set())
        if stage in alerted:
            return
        alerted.add(stage)
        self._alert(level, type_,
                    f"账号{account_id} 会话量梯度 {stage}（{used}/{self._daily_cap}）",
                    f"stage={stage} used={used} cap={self._daily_cap}")

    def _alert(self, level: str, type_: str, title: str, body: str) -> None:
        """告警投递（best-effort：告警故障不影响判定）。"""
        if self._alerts is None:
            return
        try:
            self._alerts.send(level=level, type_=type_, title=title, body=body)
        except Exception:  # noqa: BLE001
            pass

    def _next_day_open(self, now: datetime) -> datetime:
        """次日恢复时刻（工作窗口起点口径；换日即恢复额度的展示值）。"""
        return (now + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)


__all__ = ["QuotaDecision", "ConversationQuotaGate"]
