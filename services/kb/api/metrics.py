# 知识产权归甲方所有（COM-IP-01）
"""检索 API 层性能指标（NFR-PER-02 基线工程，迭代 0.2）。

模块职责（SR-NFR-PER-02 §6.2/§6.7 的 API 层与汇总层基线实现）：
- TimingMiddleware / timing_middleware：纯 ASGI 计时中间件——拦截检索端点，请求级
  总耗时观测入 kb_search_request_ms histogram，trace_id 关联（上游透传或本地生成），
  回填 X-Trace-Id / X-KB-Latency-Ms 响应头（与 latency_ms、kb_search_log 三口径同源）；
- MetricsRegistry：counter / gauge / histogram 内存线程安全注册表，histogram 含
  P50/P95/P99 分位计算与 Prometheus 兼容 bucket，snapshot() 导出 dict；
- MetricsRegistry.evaluate_thresholds：P95>400ms 预警 / >500ms 告警（从严口径，
  告警名对齐 §6.7 Prometheus 规则 KbSearchP95Warning / KbSearchP95Critical）；
- mount_metrics_endpoint：挂载 GET /internal/metrics（JSON 快照，供监控采集）。

口径边界：检索管道内各段打点（rewrite/embed/recall/rerank/assemble 的 stage() 计时）
由 SR-KB-015 §6.5 承载，本模块不重复实现，只做 API 层（请求级总量）与汇总层；
生产环境接 Prometheus 后（§6.7 监控栈），本模块快照为同构的过渡采集数据源。

依据文档：
- docs/07-实现方案/NFR/SR-NFR-PER-02-检索响应速度.md §6.2、§6.6、§6.7、§9
- docs/07-实现方案/KB/SR-KB-015-检索优化三段式.md §6.5（三段指标口径对齐）
- docs/03-技术文档/02-开发规范.md §3（中文注释 / 全量类型注解强制）
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ── 指标与告警常量（命名与 SR-NFR-PER-02 §4 指标表 / §6.7 告警规则保持一致）──
SEARCH_PATH = "/api/v1/knowledge/search"  # FR-KB-030 统一检索端点（04-接口设计 §2）
REQUEST_MS_METRIC = "kb_search_request_ms"  # 请求级全链耗时 histogram（P95 验收口径）
REQUESTS_TOTAL_METRIC = "kb_api_requests_total"  # 请求计数 counter（method/path/status 维度）
ERRORS_TOTAL_METRIC = "kb_api_errors_total"  # 服务端错误计数（status≥500）
INFLIGHT_GAUGE = "kb_api_requests_inflight"  # 在途并发 gauge（容量观测）
TRACE_HEADER = "X-Trace-Id"  # 上游链路追踪头（缺失时本地生成）
LATENCY_HEADER = "X-KB-Latency-Ms"  # 请求级耗时响应头（§6.2 细化标注）
ALERT_WARN_NAME = "KbSearchP95Warning"  # §6.7 预警规则名（P95>400ms）
ALERT_CRITICAL_NAME = "KbSearchP95Critical"  # §6.7 告警规则名（P95>500ms，违反 NFR-PER-02）
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
P95_WARN_MS = 400.0  # 预警阈值（SR-NFR-PER-02 偏差标注②：从严口径）
P95_CRITICAL_MS = 500.0  # 告警阈值（SRS §11.1「≤500ms」硬线）
# Prometheus bucket 基线（SR-NFR-PER-02 §4）：快照导出的兼容口径，不参与分位计算
DEFAULT_BUCKETS: tuple[float, ...] = (25.0, 50.0, 100.0, 200.0, 300.0, 400.0, 500.0, 750.0, 1000.0)


def now_iso() -> str:
    """UTC ISO-8601 时间戳（毫秒精度）：快照 generated_at 与轨迹 ts 的统一口径。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def percentile(values: Sequence[float], q: float) -> float:
    """分位值（线性插值法，与 numpy.percentile 默认 linear 口径一致）。

    values 无序亦可（内部取排序副本）；q 取 (0, 100]；空序列无定义，抛 ValueError
    ——调用方（evaluate_thresholds / 快照导出）须先保证样本非空。
    """
    if not values:
        raise ValueError("分位计算需要非空样本序列")
    if not 0 < q <= 100:
        raise ValueError(f"分位数 q 须在 (0, 100] 区间：{q}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = (len(ordered) - 1) * q / 100.0
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return float(ordered[lo])
    frac = idx - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _labels_key(labels: Mapping[str, str] | None) -> str:
    """标签序列化为序列键：按 key 排序的 Prometheus 展示序 ``{k="v",...}``。"""
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"


class Counter:
    """单调递增计数器（线程安全）：仅允许 inc，与 Prometheus counter 语义一致。"""

    __slots__ = ("_name", "_key", "_lock", "_value")

    def __init__(self, name: str, key: str) -> None:
        self._name = name  # 指标名（诊断用）
        self._key = key  # 标签串（快照导出键的后缀）
        self._lock = threading.Lock()
        self._value = 0.0

    def inc(self, delta: float = 1.0) -> None:
        """累加非负增量；负增量属于口径误用，显式拒绝。"""
        if delta < 0:
            raise ValueError("counter 只允许非负增量")
        with self._lock:
            self._value += delta

    @property
    def value(self) -> float:
        """当前累计值。"""
        with self._lock:
            return self._value


class Gauge:
    """瞬时值仪表（线程安全）：set/inc/dec，用于在途并发数等容量观测。"""

    __slots__ = ("_name", "_key", "_lock", "_value")

    def __init__(self, name: str, key: str) -> None:
        self._name = name
        self._key = key
        self._lock = threading.Lock()
        self._value = 0.0

    def set(self, value: float) -> None:
        """设定瞬时值。"""
        with self._lock:
            self._value = value

    def inc(self, delta: float = 1.0) -> None:
        """上调瞬时值。"""
        with self._lock:
            self._value += delta

    def dec(self, delta: float = 1.0) -> None:
        """下调瞬时值。"""
        with self._lock:
            self._value -= delta

    @property
    def value(self) -> float:
        """当前瞬时值。"""
        with self._lock:
            return self._value


class Histogram:
    """耗时直方图（线程安全）：保留原始样本以计算精确分位（P50/P95/P99）。

    内存口径（基线工程定位）：样本数超过 max_samples 时丢弃最旧一半——count/sum
    仍为全量累计值，仅分位反映较近时段；生产接 Prometheus（§6.7）后由服务端
    滑动窗口接管。buckets 为 Prometheus 兼容导出（le 累计计数），不参与分位计算。
    """

    __slots__ = ("_name", "_key", "_lock", "_buckets", "_max_samples", "_values", "_count", "_sum", "_max", "_min")

    def __init__(
        self,
        name: str,
        key: str,
        buckets: Sequence[float] = DEFAULT_BUCKETS,
        max_samples: int = 100_000,
    ) -> None:
        self._name = name
        self._key = key
        self._lock = threading.Lock()
        self._buckets = tuple(buckets)
        self._max_samples = max_samples
        self._values: list[float] = []  # 原始观测样本（超限裁剪最旧一半）
        self._count = 0  # 全量观测次数（不受裁剪影响）
        self._sum = 0.0  # 全量观测总和（不受裁剪影响）
        self._max = 0.0
        self._min = 0.0

    def observe(self, value: float) -> None:
        """记一次观测（毫秒）：耗时恒非负，负值属口径误用，显式拒绝。"""
        if value < 0:
            raise ValueError("histogram 只接受非负观测值")
        with self._lock:
            self._values.append(value)
            self._count += 1
            self._sum += value
            self._max = max(self._max, value)
            self._min = value if self._count == 1 else min(self._min, value)
            if len(self._values) > self._max_samples:
                discard = len(self._values) - self._max_samples // 2
                del self._values[:discard]

    @property
    def count(self) -> int:
        """全量观测次数。"""
        with self._lock:
            return self._count

    @property
    def sum(self) -> float:
        """全量观测总和（毫秒）。"""
        with self._lock:
            return self._sum

    def percentile(self, q: float) -> float:
        """当前留存样本的 q 分位（线性插值，见模块级 percentile 说明）。"""
        with self._lock:
            return percentile(self._values, q)

    def stats(self) -> dict[str, Any]:
        """导出统计 dict：count/sum/min/max + p50/p95/p99 + Prometheus 兼容 bucket。"""
        with self._lock:
            ordered = sorted(self._values)
            count, total, mn, mx = self._count, self._sum, self._min, self._max
        buckets: dict[str, int] = {("%g" % edge): bisect_right(ordered, edge) for edge in self._buckets}
        buckets["+Inf"] = len(ordered)
        if ordered:
            p50, p95, p99 = (percentile(ordered, q) for q in (50, 95, 99))
        else:
            p50 = p95 = p99 = 0.0
        return {
            "count": count,
            "sum": round(total, 3),
            "min": mn if ordered else 0.0,
            "max": mx if ordered else 0.0,
            "p50": round(p50, 3),
            "p95": round(p95, 3),
            "p99": round(p99, 3),
            "buckets": buckets,
        }


@dataclass(frozen=True)
class Alert:
    """阈值告警（/internal/metrics 与告警通道的导出形态）。"""

    name: str  # 告警规则名（对齐 §6.7 Prometheus 规则）
    severity: str  # warning | critical
    metric: str  # 来源指标名
    value_ms: float  # 观测到的 P95 值（毫秒）
    threshold_ms: float  # 触发阈值（毫秒）
    message: str  # 中文告警文案（通道直发口径）

    def as_dict(self) -> dict[str, Any]:
        """导出 dict（JSON 快照内嵌形态）。"""
        return {
            "name": self.name,
            "severity": self.severity,
            "metric": self.metric,
            "value_ms": self.value_ms,
            "threshold_ms": self.threshold_ms,
            "message": self.message,
        }


class MetricsRegistry:
    """内存线程安全指标注册表：counter/gauge/histogram 三类 + 快照导出 + 阈值评估。

    序列键 = (指标名, 标签串)；snapshot() 按类型分组、以 ``指标名{标签}`` 为键导出
    dict，供 /internal/metrics 采集端直接 JSON 化。基线工程定位：不依赖
    prometheus_client，阈值口径与 §6.7 告警规则一致（400ms 预警 / 500ms 告警）。
    """

    def __init__(self, *, max_trace_history: int = 64) -> None:
        self._lock = threading.RLock()
        self._counters: dict[tuple[str, str], Counter] = {}
        self._gauges: dict[tuple[str, str], Gauge] = {}
        self._histograms: dict[tuple[str, str], Histogram] = {}
        # 最近请求轨迹（有界环形）：P95 超标时回溯具体慢请求的 trace_id
        self._traces: deque[dict[str, Any]] = deque(maxlen=max_trace_history)

    # ── 序列获取（不存在则创建；注册表锁保证同键唯一）──
    def counter(self, name: str, labels: Mapping[str, str] | None = None) -> Counter:
        """获取/创建 counter 序列。"""
        key = (name, _labels_key(labels))
        with self._lock:
            if key not in self._counters:
                self._counters[key] = Counter(name, key[1])
            return self._counters[key]

    def gauge(self, name: str, labels: Mapping[str, str] | None = None) -> Gauge:
        """获取/创建 gauge 序列。"""
        key = (name, _labels_key(labels))
        with self._lock:
            if key not in self._gauges:
                self._gauges[key] = Gauge(name, key[1])
            return self._gauges[key]

    def histogram(
        self,
        name: str,
        labels: Mapping[str, str] | None = None,
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> Histogram:
        """获取/创建 histogram 序列（bucket 口径以首次创建为准）。"""
        key = (name, _labels_key(labels))
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = Histogram(name, key[1], buckets=buckets)
            return self._histograms[key]

    # ── 便捷写入口（中间件/业务埋点一行调用）──
    def inc(self, name: str, delta: float = 1.0, labels: Mapping[str, str] | None = None) -> None:
        """counter 便捷累加。"""
        self.counter(name, labels).inc(delta)

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """histogram 便捷观测。"""
        self.histogram(name, labels).observe(value)

    def record_trace(
        self,
        trace_id: str,
        *,
        method: str,
        path: str,
        status: int,
        latency_ms: float,
        slow_ms: float = P95_WARN_MS,
    ) -> None:
        """记录最近请求轨迹（有界）：P95 超标回溯具体慢请求，trace_id 与 kb_search_log 串联。"""
        self._traces.append(
            {
                "trace_id": trace_id,
                "method": method,
                "path": path,
                "status": status,
                "latency_ms": round(latency_ms, 3),
                "slow": latency_ms > slow_ms,  # 超预警线标记：慢请求筛选用
                "ts": now_iso(),
            }
        )

    def snapshot(self) -> dict[str, Any]:
        """导出全量指标快照（dict）：结构同 /internal/metrics 响应体（不含告警段）。"""
        with self._lock:
            counters = {f"{n}{k}": c.value for (n, k), c in sorted(self._counters.items())}
            gauges = {f"{n}{k}": g.value for (n, k), g in sorted(self._gauges.items())}
            histograms = {f"{n}{k}": h.stats() for (n, k), h in sorted(self._histograms.items())}
            traces = list(self._traces)
        return {
            "generated_at": now_iso(),
            "counters": counters,
            "gauges": gauges,
            "histograms": histograms,
            "traces": traces,
            "thresholds": {"p95_warn_ms": P95_WARN_MS, "p95_critical_ms": P95_CRITICAL_MS},
        }

    def evaluate_thresholds(
        self,
        *,
        metric: str = REQUEST_MS_METRIC,
        labels: Mapping[str, str] | None = None,
        warn_ms: float = P95_WARN_MS,
        critical_ms: float = P95_CRITICAL_MS,
    ) -> list[Alert]:
        """评估 P95 阈值（三态互斥，取最高级）：≤400ms 无告警 / (400,500] 预警 / >500ms 告警。

        命名与 §6.7 Prometheus 规则同名（KbSearchP95Warning / KbSearchP95Critical）；
        critical 即已违反 NFR-PER-02（≤500ms 硬线），message 供告警通道直发。
        无样本（指标不存在或零观测）时不告警——监控空窗不是性能劣化。
        """
        with self._lock:
            hist = self._histograms.get((metric, _labels_key(labels)))
        if hist is None or hist.count == 0:
            return []
        p95 = hist.percentile(95)
        if p95 > critical_ms:
            return [
                Alert(
                    name=ALERT_CRITICAL_NAME,
                    severity=SEVERITY_CRITICAL,
                    metric=metric,
                    value_ms=round(p95, 3),
                    threshold_ms=critical_ms,
                    message=(
                        f"检索 P95={p95:.0f}ms 超过 {critical_ms:.0f}ms，已违反 NFR-PER-02（≤500ms），"
                        "请立即处置（容量/降级/慢段归因，SR-NFR-PER-02 §6.7）"
                    ),
                )
            ]
        if p95 > warn_ms:
            return [
                Alert(
                    name=ALERT_WARN_NAME,
                    severity=SEVERITY_WARNING,
                    metric=metric,
                    value_ms=round(p95, 3),
                    threshold_ms=warn_ms,
                    message=(
                        f"检索 P95={p95:.0f}ms 超过预警线 {warn_ms:.0f}ms，逼近 NFR-PER-02 上限，"
                        "需关注容量水位与降级触发率（SR-NFR-PER-02 §6.7）"
                    ),
                )
            ]
        return []


def _extract_trace_id(scope: Scope, header_name: str) -> str:
    """取上游 trace 头（ASGI 小写键比较），缺失或为空则本地生成 16 位 hex。"""
    target = header_name.lower().encode("latin-1")
    for raw_key, raw_value in scope.get("headers") or []:
        if raw_key == target:
            value = raw_value.decode("latin-1").strip()
            if value:
                return value
    return uuid.uuid4().hex[:16]


class TimingMiddleware:
    """请求级计时中间件（纯 ASGI，SR-NFR-PER-02 §6.2 口径）。

    仅拦截 paths 内端点（默认检索端点 /api/v1/knowledge/search），避免健康检查等
    噪声计入 P95 验收口径：
    - 总耗时观测入 kb_search_request_ms histogram（响应首字节口径，与 §6.2 示例
      一致；异常未发出响应时以 finally 时点兜底观测、按 500 记数）；
    - 响应头回填 X-KB-Latency-Ms（毫秒取整）与 X-Trace-Id（透传/生成）；
    - 状态分类计数：requests_total{method,path,status}、errors_total（status≥500）；
    - 在途并发 gauge（inflight）供容量观测；轨迹写入 registry（trace_id 关联）。
    """

    def __init__(
        self,
        app: ASGIApp,
        registry: MetricsRegistry | None = None,
        *,
        paths: Sequence[str] = (SEARCH_PATH,),
        trace_header: str = TRACE_HEADER,
        slow_ms: float = P95_WARN_MS,
    ) -> None:
        self.app = app
        self.registry = registry if registry is not None else get_default_registry()
        self.paths = tuple(paths)
        self.trace_header = trace_header
        self.slow_ms = slow_ms

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        trace_id = _extract_trace_id(scope, self.trace_header)
        self.registry.gauge(INFLIGHT_GAUGE).inc(1.0)
        state = {"status": 0, "observed": False}
        t0 = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                ms = (time.perf_counter() - t0) * 1000.0
                state["status"] = int(message["status"])
                state["observed"] = True
                self.registry.observe(REQUEST_MS_METRIC, ms)  # 首字节口径（§6.2）
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                headers.append(self.trace_header, trace_id)
                headers.append(LATENCY_HEADER, f"{ms:.0f}")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            ms = (time.perf_counter() - t0) * 1000.0
            status = state["status"] or 500  # 未发出响应首字节即异常终止，按 500 计
            if not state["observed"]:
                self.registry.observe(REQUEST_MS_METRIC, ms)
            labels = {"method": method, "path": path, "status": str(status)}
            self.registry.inc(REQUESTS_TOTAL_METRIC, 1.0, labels=labels)
            if status >= 500:
                self.registry.inc(ERRORS_TOTAL_METRIC, 1.0, labels=labels)
            self.registry.gauge(INFLIGHT_GAUGE).dec(1.0)
            self.registry.record_trace(
                trace_id, method=method, path=path, status=status, latency_ms=ms, slow_ms=self.slow_ms
            )


def timing_middleware(
    app: ASGIApp,
    registry: MetricsRegistry | None = None,
    **options: Any,
) -> TimingMiddleware:
    """计时中间件工厂：包裹任意 ASGI 应用（FastAPI / Starlette 均可）。

    用法一（直接包裹启动）：uvicorn.Config(timing_middleware(app, registry), ...)；
    用法二（FastAPI 注册）：app.add_middleware(TimingMiddleware, registry=registry)。
    """
    return TimingMiddleware(app, registry, **options)


def mount_metrics_endpoint(
    app: FastAPI,
    registry: MetricsRegistry | None = None,
    *,
    path: str = "/internal/metrics",
    include_alerts: bool = True,
) -> FastAPI:
    """挂载 GET /internal/metrics：JSON 快照（+ 当前 P95 告警评估），供监控采集。

    与 Prometheus /metrics 抓取并存不冲突：本端点是零依赖的基线采集通道（部署与
    运维方案 §4 监控栈未就绪阶段的过渡口径），字段结构与 snapshot() 同构。
    """
    reg = registry if registry is not None else get_default_registry()

    @app.get(path, include_in_schema=False, tags=["internal"])
    def internal_metrics() -> dict[str, Any]:
        """指标快照端点：快照体 + 可选告警段（evaluate_thresholds 实时评估）。"""
        payload = reg.snapshot()
        if include_alerts:
            payload["alerts"] = [alert.as_dict() for alert in reg.evaluate_thresholds()]
        return payload

    return app


_default_registry: MetricsRegistry | None = None
_default_registry_lock = threading.Lock()


def get_default_registry() -> MetricsRegistry:
    """进程级默认注册表（懒加载）：单体接入零配置；需要隔离（如测试）时显式传入。"""
    global _default_registry
    with _default_registry_lock:
        if _default_registry is None:
            _default_registry = MetricsRegistry()
        return _default_registry
