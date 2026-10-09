# 知识产权归甲方所有（COM-IP-01）
"""检索链路可观测指标（FR-KB-015 §6.5 / SR-NFR-PER-02 §6.2）：轻量内存实现。

设计口径：
    - counter / histogram 两类序列，注册即返回同一句柄（同名同标签合并序列）；
    - snapshot() 输出 Prometheus 文本风格序列键（name{k="v",...}），便于断言
      与后续直迁 prometheus_client（届时本接口保持不变，仅换后端）；
    - 线程模型：面向 asyncio 单线程事件循环，不加锁（跨线程场景由后接的
      Prometheus 后端承担）。

三段式打点约定（pipeline 消费）：
    - kb_search_total                          counter   检索请求数
    - kb_search_stage_ms{stage}                histogram 各段耗时（rewrite/recall/rerank/assemble）
    - kb_search_request_ms                     histogram 全链路耗时（P95 验收数据源）
    - kb_search_degrade_total{reason}          counter   降级触发计数（判定点 a2/b/d 等）
    - kb_search_zero_hit_total                 counter   零命中计数（知识缺口回流，§6.5）
"""
from __future__ import annotations

import math
from typing import Mapping


def _series_id(name: str, labels: Mapping[str, str]) -> str:
    """序列键：无标签返回裸名，有标签按标签名排序渲染（保证快照确定性）。"""
    if not labels:
        return name
    rendered = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return f"{name}{{{rendered}}}"


def _check_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """标签规整：值统一转字符串；键必须为合法标识符（Prometheus 标签语法）。"""
    normalized: dict[str, str] = {}
    for key, value in labels.items():
        if not key.isidentifier():
            raise ValueError(f"指标标签名必须为合法标识符: {key!r}")
        normalized[key] = str(value)
    return normalized


class Counter:
    """单调计数器（Prometheus 语义：只增不减）。"""

    def __init__(self, name: str, labels: Mapping[str, str] | None = None) -> None:
        self.name = name
        self.labels: dict[str, str] = _check_labels(labels or {})
        self._value = 0.0

    @property
    def series_id(self) -> str:
        """该序列的 Prometheus 文本风格键。"""
        return _series_id(self.name, self.labels)

    @property
    def value(self) -> float:
        """当前累计值。"""
        return self._value

    def inc(self, amount: float = 1.0) -> None:
        """累加增量；负增量违反单调语义，直接拒绝。"""
        if amount < 0:
            raise ValueError("计数器只允许非负增量（Prometheus 单调语义）")
        self._value += amount


class Histogram:
    """观测值直方图：本迭代保留原始观测，快照输出分位数（P95 验收口径）。"""

    def __init__(self, name: str, labels: Mapping[str, str] | None = None) -> None:
        self.name = name
        self.labels: dict[str, str] = _check_labels(labels or {})
        self._observations: list[float] = []

    @property
    def series_id(self) -> str:
        return _series_id(self.name, self.labels)

    @property
    def count(self) -> int:
        """观测次数。"""
        return len(self._observations)

    @property
    def sum(self) -> float:  # noqa: A003 —— 与 Prometheus 指标语义对齐保留命名
        """观测值总和。"""
        return sum(self._observations)

    def observe(self, value: float) -> None:
        """记一次观测（耗时 ms 等）。"""
        self._observations.append(float(value))

    def snapshot(self) -> dict[str, float]:
        """单序列统计快照：count/sum/min/max/avg/p50/p95/p99。"""
        obs = self._observations
        if not obs:
            return {"count": 0, "sum": 0.0, "min": 0.0, "max": 0.0, "avg": 0.0,
                    "p50": 0.0, "p95": 0.0, "p99": 0.0}
        ordered = sorted(obs)
        total = sum(ordered)
        return {
            "count": len(ordered),
            "sum": total,
            "min": ordered[0],
            "max": ordered[-1],
            "avg": total / len(ordered),
            "p50": _percentile(ordered, 50),
            "p95": _percentile(ordered, 95),
            "p99": _percentile(ordered, 99),
        }


def _percentile(ordered: list[float], pct: float) -> float:
    """最近邻位分位数（观测已升序）：pct 分位对应第 ceil(p%·N) 个观测值。"""
    if not ordered:
        return 0.0
    idx = max(0, math.ceil(pct / 100.0 * len(ordered)) - 1)
    return ordered[min(idx, len(ordered) - 1)]


class MetricsRegistry:
    """指标注册中心：counter/histogram 的注册、句柄复用与全量快照。

    同一 (name, labels) 重复注册返回既有句柄（序列合并）；同一 name 跨类型
    使用（既作 counter 又作 histogram）视为配置错误，立即失败。
    """

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}

    def counter(self, name: str, **labels: str) -> Counter:
        """注册（或复用）计数器序列。"""
        key = _series_id(name, _check_labels(labels))
        if key in self._histograms:
            raise ValueError(f"指标名冲突：{name} 已注册为 histogram")
        if key not in self._counters:
            self._counters[key] = Counter(name, labels)
        return self._counters[key]

    def histogram(self, name: str, **labels: str) -> Histogram:
        """注册（或复用）直方图序列。"""
        key = _series_id(name, _check_labels(labels))
        if key in self._counters:
            raise ValueError(f"指标名冲突：{name} 已注册为 counter")
        if key not in self._histograms:
            self._histograms[key] = Histogram(name, labels)
        return self._histograms[key]

    def snapshot(self) -> dict[str, dict[str, float]]:
        """全量快照：{"counters": {series_id: value}, "histograms": {series_id: stats}}。"""
        return {
            "counters": {key: c.value for key, c in sorted(self._counters.items())},
            "histograms": {key: h.snapshot() for key, h in sorted(self._histograms.items())},
        }

    def reset(self) -> None:
        """清空全部序列（测试隔离用；既有句柄此后写入不再进入快照）。"""
        self._counters.clear()
        self._histograms.clear()
