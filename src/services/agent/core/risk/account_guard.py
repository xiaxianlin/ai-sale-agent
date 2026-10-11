# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""零封号监测（NFR-SEC-10，SR-NFR-SEC-10 §6.2/§6.3；迭代 2.3 demo 口径）。

职责（四段闭环中的**监测段 + 应急段**；预防段归 RiskGuard，举证段随生产台账）：
- 限权信号台账：人工观察清单 + 系统代理指标统一入账（platform_signal 语义）；
- 加权评分分级：24h 回看窗内信号加权求和 → 观察（watch）/ 警告（warning）/
  紧急（critical）三级；func_limit_hint 为强信号：24h 内复现 ≥2 次直接紧急；
- 紧急自动停实例：kill 开关置位（执行器每动作前读）+ 事件状态机 open→stopped
  + 高级告警——分钟级全停（SRS §17 R-01 的核心应对）；
- 系统代理指标：询盘量骤降（较基线降 ≥50%）/ 操作被拒率（≥20%）自动入账；
- 恢复仅人工决策（manual_clear），无任何自动旁路。

demo 形态边界：Redis kill 键 / PG 台账 / Docker pause 双保险由生产形态承载。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Protocol

#: 信号权重（app_config scope='guard' guard.signal.weights 默认值，SR §6.2）
SIGNAL_WEIGHTS: dict[str, int] = {
    "login_captcha": 3, "login_fail": 2, "captcha_burst": 4,       # 登录/人机校验类
    "func_limit_hint": 6,                                          # 平台明确限权提示（最强）
    "inquiry_drop": 3, "reject_rate_up": 4, "traffic_drop": 3,     # 系统代理指标类
}


class AlertSink(Protocol):
    """告警通道协议（生产 = FR-AG-007）。"""

    def send(self, level: str, type_: str, title: str, body: str) -> None: ...


@dataclass(frozen=True)
class SignalRecord:
    """限权信号台账一行（platform_signal 语义）。"""

    signal_code: str
    source: str                     # manual / system
    at: datetime
    detail: str = ""


@dataclass
class HealthEvent:
    """疑似限权事件（account_health_event 语义：应急 SOP 状态机载体）。"""

    uid: str
    account_id: int
    level: str                      # watch / warning / critical
    score: int
    status: str = "open"            # open / stopped / recovered / closed
    created_at: datetime | None = None
    stop_at: datetime | None = None
    decision: str | None = None
    decided_by: str | None = None


@dataclass
class _StopState:
    """Kill 开关状态（risk:kill:{account_id} 键语义：stop=全停 / 空=运行）。"""

    reason: str = ""
    since: datetime | None = None
    events: list[str] = field(default_factory=list)


class AccountHealthMonitor:
    """账号健康监测：信号入账 → 三级分级 → 紧急自动停实例（SR §6.2/§6.3）。

    :param window_hours: 分级评分回看窗口（默认 24h）；
    :param critical_score / warning_score: 紧急/警告分数线（默认 8/5）；
    :param inquiry_drop_ratio: 询盘骤降阈值（较基线降幅，默认 0.5）；
    :param reject_rate: 操作被拒率阈值（默认 0.2）。
    """

    def __init__(
        self,
        *,
        alerts: AlertSink | None = None,
        clock: Callable[[], datetime] | None = None,
        window_hours: int = 24,
        critical_score: int = 8,
        warning_score: int = 5,
        inquiry_drop_ratio: float = 0.5,
        reject_rate: float = 0.2,
    ) -> None:
        self._alerts = alerts
        self._clock = clock or (lambda: datetime.now())
        self._window = timedelta(hours=window_hours)
        self._critical = critical_score
        self._warning = warning_score
        self._drop_ratio = inquiry_drop_ratio
        self._reject_rate = reject_rate
        self._signals: dict[int, list[SignalRecord]] = defaultdict(list)
        self._events: dict[int, list[HealthEvent]] = defaultdict(list)
        self._kill: dict[int, _StopState] = {}

    # ---------------------------------------------------------------- 信号入账

    def record(
        self, account_id: int, signal_code: str, *, now: datetime, source: str = "system",
        detail: str = "",
    ) -> SignalRecord:
        """登记限权信号（人工观察/系统采集统一入口）。"""
        entry = SignalRecord(signal_code=signal_code, source=source, at=now, detail=detail)
        self._signals[account_id].append(entry)
        return entry

    def recent_signals(self, account_id: int, *, now: datetime | None = None) -> list[SignalRecord]:
        """回看窗内信号（分级口径一致；now 缺省用注入时钟）。"""
        moment = now if now is not None else self._clock()
        cutoff = moment - self._window
        return [s for s in self._signals.get(account_id, []) if s.at > cutoff]

    # ---------------------------------------------------------------- 代理指标

    def check_inquiry_drop(
        self, account_id: int, *, today_count: int, baseline_avg: float, now: datetime
    ) -> bool:
        """询盘量骤降检测：今日 < 基线均值 ×(1−ratio) → inquiry_drop 信号入账。"""
        if baseline_avg > 0 and today_count < baseline_avg * (1.0 - self._drop_ratio):
            self.record(account_id, "inquiry_drop", now=now,
                        detail=f"today={today_count} baseline={baseline_avg}")
            return True
        return False

    def check_reject_rate(
        self, account_id: int, *, rejected: int, total: int, now: datetime
    ) -> bool:
        """操作被拒率检测：近窗被拒占比 ≥ 阈值 → reject_rate_up 信号入账。"""
        if total > 0 and rejected / total >= self._reject_rate:
            self.record(account_id, "reject_rate_up", now=now,
                        detail=f"rejected={rejected}/{total}")
            return True
        return False

    # ---------------------------------------------------------------- 分级与应急

    def grade(self, account_id: int, *, now: datetime) -> tuple[str, int]:
        """加权评分分级（SR §6.2）：观察 <5 / 警告 5-7 / 紧急 ≥8 或强信号复现。

        警告/紧急开事件；紧急自动全停实例（分钟级应急，kill 开关 + 高级告警）。
        """
        signals = self.recent_signals(account_id, now=now)
        score = sum(SIGNAL_WEIGHTS.get(s.signal_code, 0) for s in signals)
        strong_hits = sum(1 for s in signals if s.signal_code == "func_limit_hint")
        if score >= self._critical or strong_hits >= 2:
            level = "critical"
        elif score >= self._warning:
            level = "warning"
        else:
            return "watch", score
        self._open_or_escalate(account_id, level, score, now)
        if level == "critical":
            self.emergency_stop(account_id, reason=f"grade critical score={score}", now=now)
        return level, score

    def emergency_stop(self, account_id: int, *, reason: str, now: datetime | None = None) -> None:
        """紧急全停（人工/系统自动均可触发）：kill 置位 + 事件 stopped + 高级告警。"""
        moment = now if now is not None else self._clock()
        self._kill[account_id] = _StopState(reason=reason, since=moment)
        event = self._latest_open(account_id)
        if event is not None:
            event.status = "stopped"
            event.stop_at = moment
        self._alert("high", "account_risk",
                    f"账号{account_id} 疑似限权已全停（{reason}）",
                    f"kill=stop since={moment.isoformat()}")

    def manual_clear(
        self, account_id: int, *, decision: str, operator: str, now: datetime | None = None
    ) -> None:
        """人工处置决策清除全停（唯一恢复路径：easy_restart/disable/false_alarm）。"""
        moment = now if now is not None else self._clock()
        self._kill.pop(account_id, None)
        event = self._latest_open(account_id) or self._latest(account_id)
        if event is not None:
            event.status = "recovered" if decision == "easy_restart" else "closed"
            event.decision, event.decided_by = decision, operator
        self._alert("mid", "account_risk",
                    f"账号{account_id} 人工处置 {decision}（{operator}）",
                    f"at={moment.isoformat()}")

    def is_stopped(self, account_id: int) -> bool:
        """Kill 开关读取（执行器每动作前检查，≤30s 生效口径的进程内形态）。"""
        return account_id in self._kill

    def action_allowed(self, account_id: int) -> bool:
        """动作放行钩子：停实例期间拒绝一切平台动作。"""
        return not self.is_stopped(account_id)

    def events(self, account_id: int) -> list[HealthEvent]:
        """事件台账（应急 SOP 留痕，举证材料）。"""
        return list(self._events.get(account_id, []))

    # ---------------------------------------------------------------- 内部

    def _open_or_escalate(
        self, account_id: int, level: str, score: int, now: datetime
    ) -> HealthEvent:
        """开事件/升级：同账号已有同级 open 事件则更新评分，否则新开。"""
        event = self._latest_open(account_id)
        if event is not None and event.level == level:
            event.score = score
            return event
        event = HealthEvent(
            uid=uuid.uuid4().hex[:16], account_id=account_id, level=level, score=score,
            created_at=now,
        )
        self._events[account_id].append(event)
        return event

    def _latest_open(self, account_id: int) -> HealthEvent | None:
        for event in reversed(self._events.get(account_id, [])):
            if event.status == "open":
                return event
        return None

    def _latest(self, account_id: int) -> HealthEvent | None:
        events = self._events.get(account_id, [])
        return events[-1] if events else None

    def _alert(self, level: str, type_: str, title: str, body: str) -> None:
        """告警投递（best-effort）。"""
        if self._alerts is None:
            return
        try:
            self._alerts.send(level=level, type_=type_, title=title, body=body)
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "SIGNAL_WEIGHTS",
    "SignalRecord",
    "HealthEvent",
    "AccountHealthMonitor",
]
