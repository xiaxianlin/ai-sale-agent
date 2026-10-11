# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""账号关联风险监控单元测试（FR-SC-004-②/§6.4，SR-SC-004 §6.4/§9；Phase 4 TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：完全同步操作对 → ρ≈1 超阈告警（含账号对明细）、独立操作对 → 低于阈值不告警、
  内容相似率达标/超阈两分支；
- 边界：相似率恰等阈值（=5% 不告警）与恰过（6% 告警）、零流量（total=0）不除零；
- 异常：零方差序列 Pearson 守卫（返回 0.0 而非 NaN/异常）；
- 并发：多线程 record_op/record_send 计数无丢失；
- 降级：evaluate 汇总输出结构稳定（仪表盘契约），审计留痕可查。

验收口径映射：SRS §17 R-06（多账号关联风险）、§16.5（账号级行为差异化抽检指标）。
实现方案：docs/07-实现方案/SC/SR-SC-004-风控V2.md §6.4、§9
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from services.agent.core.riskv2.correlation import CorrelationMonitor, pearson
from services.agent.core.engine import InMemoryAlerts

T0 = datetime(2026, 10, 12, 8, 0, 0)


def make_monitor(**kw) -> tuple[CorrelationMonitor, InMemoryAlerts]:
    """标准装配：内存告警 + 审计记录。"""
    alerts = InMemoryAlerts()
    audit: list[dict] = []
    monitor = CorrelationMonitor(alerts=alerts, audit=audit.append, **kw)
    monitor.audit_ref = audit
    return monitor, alerts


def feed_synchronized(monitor: CorrelationMonitor) -> None:
    """构造两账号完全同步的 7 天操作序列（同 10min 槽同计数）。"""
    for day in range(7):
        for slot in range(6):  # 每日 6 个活跃槽
            ts = T0 + timedelta(days=day, minutes=10 * slot)
            for _ in range(slot + 1):  # 槽计数 1..6，两账号完全一致
                monitor.record_op(7001, ts)
                monitor.record_op(7002, ts)


def feed_independent(monitor: CorrelationMonitor) -> None:
    """构造两账号节奏错开的操作序列（错峰后形态，槽位互补）。"""
    for day in range(7):
        for slot in range(6):
            ts_a = T0 + timedelta(days=day, minutes=10 * slot)
            ts_b = T0 + timedelta(days=day, minutes=10 * (5 - slot))  # 反相
            for _ in range(slot + 1):
                monitor.record_op(7101, ts_a)
                monitor.record_op(7102, ts_b)


# ───────────────────────── Pearson 基元 ─────────────────────────


class TestPearson:
    """纯 Python Pearson 相关系数（无 numpy 本地依赖口径，SR §8 NFR-SEC-06）。"""

    def test_perfect_positive(self) -> None:
        """等价类：完全同向 → +1。"""
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert pearson(xs, xs) == pytest.approx(1.0)

    def test_perfect_negative(self) -> None:
        """反相序列 → -1。"""
        xs = [1.0, 2.0, 3.0]
        assert pearson(xs, [3.0, 2.0, 1.0]) == pytest.approx(-1.0)

    def test_zero_variance_guarded(self) -> None:
        """异常：零方差序列 → 0.0（不抛 NaN/ZeroDivisionError）。"""
        assert pearson([2.0, 2.0, 2.0], [1.0, 5.0, 9.0]) == 0.0


# ───────────────────────── 时间相关系数监控 ─────────────────────────


class TestPairCorrelation:
    """账号对操作时间相关系数（SR-SC-004 §6.4 pair_corr_max，阈值 0.6）。"""

    def test_synchronized_pair_alerts_with_detail(self) -> None:
        """等价类：完全同步两账号 → ρ≈1 > 0.6 → 高级告警且含账号对明细。"""
        monitor, alerts = make_monitor()
        feed_synchronized(monitor)
        value, top_pairs = monitor.pair_corr_max()
        assert value > 0.99
        assert top_pairs and {top_pairs[0][0], top_pairs[0][1]} == {7001, 7002}
        report = monitor.evaluate()
        assert report["pair_corr_max"]["status"] == "alert"
        assert alerts.last(level="high", type_="risk_correlation") is not None

    def test_staggered_pair_no_alert(self) -> None:
        """等价类：错峰账号对 → 相关系数低于阈值、无告警（错峰机制有效性证明）。"""
        monitor, alerts = make_monitor()
        feed_independent(monitor)
        value, _ = monitor.pair_corr_max()
        assert value < 0.6
        assert monitor.evaluate()["pair_corr_max"]["status"] == "ok"
        assert alerts.last(type_="risk_correlation") is None

    def test_single_account_or_boundary_no_false_alert(self) -> None:
        """边界/降级：仅 1 账号无量测对 → 0.0 且 ok（不误报；超阈为严格大于）。"""
        monitor, _ = make_monitor()
        monitor.record_op(7001, T0)
        value, pairs = monitor.pair_corr_max()
        assert value == 0.0 and pairs == []
        assert monitor.evaluate()["pair_corr_max"]["status"] == "ok"


# ───────────────────────── 内容相似率监控 ─────────────────────────


class TestContentSimilarRate:
    """跨账号内容相似率（SR-SC-004 §6.4 content_similar_rate，阈值 5%）。"""

    def test_boundary_exactly_threshold_not_alert(self) -> None:
        """边界：5/100 = 恰 5% → 不告警（≤5% 验收线内含）。"""
        monitor, alerts = make_monitor()
        for i in range(100):
            monitor.record_send(similar_hit=(i < 5))
        assert monitor.content_similar_rate() == pytest.approx(0.05)
        assert monitor.evaluate()["content_similar_rate"]["status"] == "ok"
        assert alerts.last(type_="risk_correlation") is None

    def test_over_threshold_alerts(self) -> None:
        """等价类：6/100 = 6% > 5% → 告警（含分子分母明细）。"""
        monitor, alerts = make_monitor()
        for i in range(100):
            monitor.record_send(similar_hit=(i < 6))
        report = monitor.evaluate()
        assert report["content_similar_rate"]["status"] == "alert"
        assert report["content_similar_rate"]["hits"] == 6
        assert alerts.last(level="high") is not None

    def test_zero_traffic_guarded(self) -> None:
        """异常/降级：零流量 → 0.0 且 ok（不除零、不误报）。"""
        monitor, _ = make_monitor()
        assert monitor.content_similar_rate() == 0.0
        assert monitor.evaluate()["content_similar_rate"]["status"] == "ok"


# ───────────────────────── 并发与审计 ─────────────────────────


class TestConcurrencyAndAudit:
    """并发计数与审计留痕（NFR-SEC-03）。"""

    def test_concurrent_send_counts_lossless(self) -> None:
        """并发：8 线程 × 50 笔发送计数 → total 恰 400（无丢失竞态）。"""
        monitor, _ = make_monitor()

        def worker(w: int) -> None:
            for i in range(50):
                monitor.record_send(similar_hit=((w + i) % 25 == 0))

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert monitor.total_sends == 400

    def test_evaluate_writes_audit(self) -> None:
        """等价类：evaluate 对每项指标写审计行（仪表盘数据 + 审计双写，NFR-SEC-03）。"""
        monitor, _ = make_monitor()
        monitor.record_send(similar_hit=False)
        report = monitor.evaluate()
        assert set(report) == {"pair_corr_max", "content_similar_rate"}
        events = [r["event"] for r in monitor.audit_ref]
        assert events.count("correlation_metric") >= 2
