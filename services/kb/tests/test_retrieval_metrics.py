# 知识产权归甲方所有（COM-IP-01）
"""轻量指标单元测试（SR-KB-015 §6.5）：注册与句柄复用 / 累加观测 / 快照确定性。"""
from __future__ import annotations

import pytest

from services.kb.core.retrieval.metrics import Counter, Histogram, MetricsRegistry


class TestCounter:
    """计数器：注册即用 / 同序列句柄复用 / 单调语义。"""

    def test_register_and_increment(self) -> None:
        counter = MetricsRegistry().counter("kb_search_total")
        assert counter.value == 0.0
        counter.inc()
        counter.inc(2.5)
        assert counter.value == 3.5

    def test_negative_increment_rejected(self) -> None:
        with pytest.raises(ValueError, match="非负"):
            Counter("x").inc(-1)

    def test_same_series_returns_same_handle(self) -> None:
        registry = MetricsRegistry()
        first = registry.counter("kb_search_degrade_total", reason="budget_skip_rerank")
        second = registry.counter("kb_search_degrade_total", reason="budget_skip_rerank")
        assert first is second  # 同名同标签合并序列
        first.inc()
        assert second.value == 1.0

    def test_different_labels_distinct_series(self) -> None:
        registry = MetricsRegistry()
        registry.counter("kb_search_degrade_total", reason="a").inc()
        registry.counter("kb_search_degrade_total", reason="b").inc(3)
        snap = registry.snapshot()["counters"]
        assert snap == {
            'kb_search_degrade_total{reason="a"}': 1.0,
            'kb_search_degrade_total{reason="b"}': 3.0,
        }


class TestHistogram:
    """直方图：观测统计 / 分位数 / 空序列。"""

    def test_observe_and_snapshot_stats(self) -> None:
        hist = Histogram("kb_search_stage_ms", {"stage": "recall"})
        for value in (10.0, 20.0, 30.0, 40.0, 100.0):
            hist.observe(value)
        snap = hist.snapshot()
        assert snap["count"] == 5
        assert snap["sum"] == pytest.approx(200.0)
        assert snap["min"] == 10.0 and snap["max"] == 100.0
        assert snap["avg"] == pytest.approx(40.0)
        assert snap["p50"] == 30.0  # 最近邻位：ceil(0.5·5)=3 → 第 3 个观测
        assert snap["p95"] == 100.0  # ceil(0.95·5)=5 → 第 5 个观测

    def test_empty_histogram_snapshot(self) -> None:
        snap = Histogram("x").snapshot()
        assert snap["count"] == 0 and snap["sum"] == 0.0 and snap["p95"] == 0.0

    def test_single_observation(self) -> None:
        hist = Histogram("kb_search_request_ms")
        hist.observe(123.4)
        snap = hist.snapshot()
        assert snap["count"] == 1
        assert snap["p50"] == snap["p95"] == snap["p99"] == pytest.approx(123.4)


class TestRegistry:
    """注册中心：快照确定性 / 跨类型名冲突 / reset。"""

    def test_snapshot_series_id_formats(self) -> None:
        registry = MetricsRegistry()
        registry.counter("kb_search_total").inc()
        registry.histogram("kb_search_stage_ms", stage="rewrite").observe(1.2)
        snap = registry.snapshot()
        assert snap["counters"] == {"kb_search_total": 1.0}
        assert list(snap["histograms"]) == ['kb_search_stage_ms{stage="rewrite"}']

    def test_cross_type_name_conflict_rejected(self) -> None:
        registry = MetricsRegistry()
        registry.counter("kb_search_total")
        with pytest.raises(ValueError, match="已注册为 counter"):
            registry.histogram("kb_search_total")

    def test_invalid_label_name_rejected(self) -> None:
        registry = MetricsRegistry()
        with pytest.raises(ValueError, match="标识符"):
            registry.counter("x", **{"bad-label": "v"})

    def test_reset_clears_all_series(self) -> None:
        registry = MetricsRegistry()
        registry.counter("kb_search_total").inc()
        registry.histogram("kb_search_request_ms").observe(1.0)
        registry.reset()
        assert registry.snapshot() == {"counters": {}, "histograms": {}}
