# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""零封号监测单元测试（NFR-SEC-10，SR-NFR-SEC-10 §6.2/§6.3/§9；迭代 2.3 TDD 阶段①用例）。

覆盖（监测段 + 应急段的内存/demo 口径）：
- 限权信号加权评分分级：观察（watch）/ 警告（warning）/ 紧急（critical）三级；
- 强信号直判：func_limit_hint 24h 内复现 ≥2 次直接紧急；
- 紧急自动停实例：kill 开关置位 + 事件状态机（open→stopped）+ 高级告警；
- 系统代理指标：询盘量骤降（较基线降 ≥50%）/ 操作被拒率（≥20%）自动入账；
- 信号 24h 回看窗口；恢复只能人工决策清除（无自动旁路）。

实现方案：docs/07-实现方案/NFR/SR-NFR-SEC-10-账号零封号.md §6.2、§6.3、§9
"""

from __future__ import annotations

from datetime import datetime, timedelta

from services.agent.core.engine import InMemoryAlerts
from services.agent.core.risk.account_guard import AccountHealthMonitor

NOW = datetime(2026, 10, 8, 10, 0, 0)


def make_monitor(**overrides):
    """标准装配：内存告警 + 事件记录。"""
    alerts = overrides.pop("alerts", None) or InMemoryAlerts()
    monitor = AccountHealthMonitor(alerts=alerts, clock=lambda: NOW, **overrides)
    return monitor, alerts


class TestGrading:
    """限权信号 → 三级分级（观察 <5 / 警告 5-7 / 紧急 ≥8 或强信号复现）。"""

    def test_watch_low_score(self) -> None:
        """单条 login_fail（score=2）→ watch，不开事件。"""
        monitor, _ = make_monitor()
        monitor.record(1, "login_fail", now=NOW)
        level, score = monitor.grade(1, now=NOW)
        assert level == "watch" and score == 2
        assert monitor.events(1) == []

    def test_warning_medium_score(self) -> None:
        """inquiry_drop(3)+traffic_drop(3)=6 → warning，开事件但不停车。"""
        monitor, _ = make_monitor()
        monitor.record(1, "inquiry_drop", now=NOW)
        monitor.record(1, "traffic_drop", now=NOW)
        level, score = monitor.grade(1, now=NOW)
        assert level == "warning" and score == 6
        assert monitor.events(1)[0].status == "open"
        assert not monitor.is_stopped(1)

    def test_critical_strong_signal_doubled(self) -> None:
        """func_limit_hint 24h 内 ×2 → 强信号直判 critical。"""
        monitor, _ = make_monitor()
        monitor.record(1, "func_limit_hint", now=NOW)
        monitor.record(1, "func_limit_hint", now=NOW + timedelta(hours=1))
        level, _ = monitor.grade(1, now=NOW + timedelta(hours=1))
        assert level == "critical"

    def test_critical_by_score(self) -> None:
        """累计分 ≥8 → critical：captcha_burst(4)+func_limit_hint(6)+login_fail(2)=12。"""
        monitor, _ = make_monitor()
        for code in ("captcha_burst", "func_limit_hint", "login_fail"):
            monitor.record(1, code, now=NOW)
        level, score = monitor.grade(1, now=NOW)
        assert level == "critical" and score == 12

    def test_signal_window_24h(self) -> None:
        """回看窗口 24h：窗口外信号不参与评分。"""
        monitor, _ = make_monitor()
        monitor.record(1, "captcha_burst", now=NOW)
        monitor.record(1, "func_limit_hint", now=NOW)
        level, _ = monitor.grade(1, now=NOW + timedelta(hours=25))
        assert level == "watch"


class TestEmergencyStop:
    """紧急分级 → 自动停实例（分钟级应急段的进程内形态）。"""

    def test_critical_auto_stops_instance(self) -> None:
        """critical → kill 开关置位 + 事件 stopped + 高级告警（自动停实例事件）。"""
        monitor, alerts = make_monitor()
        monitor.record(1, "func_limit_hint", now=NOW)
        monitor.record(1, "func_limit_hint", now=NOW + timedelta(minutes=5))
        monitor.grade(1, now=NOW + timedelta(minutes=5))
        assert monitor.is_stopped(1)
        event = monitor.events(1)[-1]
        assert event.level == "critical" and event.status == "stopped"
        assert alerts.last(level="high") is not None

    def test_recovery_manual_only(self) -> None:
        """恢复仅人工决策：manual_clear 前持续停发，决策后解除（无自动旁路）。"""
        monitor, _ = make_monitor()
        monitor.record(1, "func_limit_hint", now=NOW)
        monitor.record(1, "func_limit_hint", now=NOW)
        monitor.grade(1, now=NOW)
        assert monitor.is_stopped(1)
        monitor.manual_clear(1, decision="easy_restart", operator="supervisor")
        assert not monitor.is_stopped(1)

    def test_stop_blocks_admission_hook(self) -> None:
        """停实例钩子：is_stopped=True 时 action_allowed 返回 False（执行器每动作前读）。"""
        monitor, _ = make_monitor()
        assert monitor.action_allowed(1)
        monitor.emergency_stop(1, reason="演练")
        assert not monitor.action_allowed(1)


class TestProxyMetrics:
    """系统代理指标自动入账：询盘量骤降 / 操作被拒率。"""

    def test_inquiry_drop_detected(self) -> None:
        """基线 20 条/日、今日 8 条（降 60% ≥50%）→ inquiry_drop 信号入账。"""
        monitor, _ = make_monitor()
        fired = monitor.check_inquiry_drop(1, today_count=8, baseline_avg=20.0, now=NOW)
        assert fired
        assert monitor.recent_signals(1)[-1].signal_code == "inquiry_drop"

    def test_inquiry_drop_not_triggered(self) -> None:
        """今日 15 条（降 25% <50%）→ 不入账。"""
        monitor, _ = make_monitor()
        assert not monitor.check_inquiry_drop(1, today_count=15, baseline_avg=20.0, now=NOW)

    def test_reject_rate_detected(self) -> None:
        """近 1h 被拒 5/20=25% ≥20% → reject_rate_up 信号入账。"""
        monitor, _ = make_monitor()
        assert monitor.check_reject_rate(1, rejected=5, total=20, now=NOW)
        assert monitor.recent_signals(1)[-1].signal_code == "reject_rate_up"

    def test_reject_rate_not_triggered(self) -> None:
        """被拒 2/20=10% <20% → 不入账。"""
        monitor, _ = make_monitor()
        assert not monitor.check_reject_rate(1, rejected=2, total=20, now=NOW)

    def test_proxy_metrics_feed_grading(self) -> None:
        """代理指标联动分级：inquiry_drop+reject_rate_up+captcha_burst=11 → critical 停实例。"""
        monitor, _ = make_monitor()
        monitor.check_inquiry_drop(1, today_count=5, baseline_avg=20.0, now=NOW)
        monitor.check_reject_rate(1, rejected=6, total=20, now=NOW)
        monitor.record(1, "captcha_burst", now=NOW)
        level, _ = monitor.grade(1, now=NOW)
        assert level == "critical"
        assert monitor.is_stopped(1)
