# 知识产权归甲方所有（COM-IP-01）
"""NFR-PER-02 基线工程单元测试（SR-NFR-PER-02 §9，迭代 0.2）。

覆盖：
- 中间件计时写入：X-KB-Latency-Ms 响应头、kb_search_request_ms histogram 观测、
  trace_id 关联（上游透传 / 本地生成）与轨迹记录、500 错误计数、非检索路径不计量；
- histogram 分位计算：已知延迟序列（1..100ms 等差）断言 P50/P95/P99（线性插值口径）
  与 Prometheus 兼容 bucket 累计计数；
- 告警阈值三态评估：≤400ms 无告警 / (400,500] 预警 / >500ms 告警（§6.7 从严口径）；
- /internal/metrics 端点：JSON 快照结构 + 告警段实时评估；
- 压测脚本冒烟：本地 FastAPI 假端点（TestClient 通道 + 真实多线程并发）跑通并 PASS，
  且中间件观测数与压测成功请求数一致（并发写入的线程安全互证）。
"""
from __future__ import annotations

# ── 标准库 platform 语义恢复（同 platform/tests/conftest.py 的既定做法）──
# 仓库顶层包目录名为 platform，pytest 从仓库根运行时先于标准库被解析；本文件导入
# fastapi/httpx 链路（rich→attrs→platform.python_implementation）会抛 AttributeError。
# 在导入任何第三方依赖之前回填标准库模块语义，保证本测试文件可独立收集执行。
import importlib.util
import os as _os
import sys as _sys
import sysconfig as _sysconfig


def _restore_stdlib_platform() -> None:
    """把标准库 platform 的公开属性回填到已被缓存的本仓库同名包对象上（幂等）。"""
    cached = _sys.modules.get("platform")
    if cached is not None and hasattr(cached, "python_implementation"):
        return
    std_path = _os.path.join(_sysconfig.get_paths()["stdlib"], "platform.py")
    spec = importlib.util.spec_from_file_location("platform", std_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法定位标准库 platform 模块（测试环境损坏）")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if cached is None:
        _sys.modules["platform"] = module
        return
    for name, value in vars(module).items():
        if not name.startswith("__"):
            setattr(cached, name, value)


_restore_stdlib_platform()

import asyncio  # noqa: E402  (须在 platform 自愈之后导入第三方链路)
import threading  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from services.kb.api.metrics import (  # noqa: E402
    ALERT_CRITICAL_NAME,
    ALERT_WARN_NAME,
    ERRORS_TOTAL_METRIC,
    INFLIGHT_GAUGE,
    REQUEST_MS_METRIC,
    REQUESTS_TOTAL_METRIC,
    SEARCH_PATH,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    MetricsRegistry,
    TimingMiddleware,
    mount_metrics_endpoint,
)
from tools.loadtest.retrieval_load import run_load_test  # noqa: E402


def _requests_key(status: str) -> str:
    """构造 requests_total 计数器在快照中的导出键（标签按 key 排序的展示序）。"""
    return f'{REQUESTS_TOTAL_METRIC}{{method="POST",path="{SEARCH_PATH}",status="{status}"}}'


class TestTimingMiddleware:
    """中间件计时写入：histogram 观测 / 响应头 / trace 关联 / 路径过滤 / 错误计数。"""

    def _build_app(self, registry: MetricsRegistry) -> FastAPI:
        """构建带计时中间件的假检索应用：delay_ms 控制端点耗时，fail=1 返回 500。"""
        app = FastAPI()
        app.add_middleware(TimingMiddleware, registry=registry)

        @app.post(SEARCH_PATH)
        async def search(fail: int = 0, delay_ms: float = 20.0) -> dict[str, object]:
            await asyncio.sleep(delay_ms / 1000.0)
            if fail:
                # 模拟 50002（下游超时）类服务端错误：走标准异常通道返回 500
                raise HTTPException(status_code=500, detail="50002 mock 下游超时")
            return {"results": [], "latency_ms": delay_ms}

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        return app

    def test_records_latency_histogram_headers_and_trace(self) -> None:
        registry = MetricsRegistry()
        client = TestClient(self._build_app(registry))
        resp = client.post(SEARCH_PATH, headers={"X-Trace-Id": "trace-fixed-0001"})
        assert resp.status_code == 200
        # 上游 trace_id 透传并回显（与 kb_search_log 的 trace 串联口径一致）
        assert resp.headers["x-trace-id"] == "trace-fixed-0001"
        # 响应头耗时（毫秒取整）不小于端点模拟耗时 20ms
        assert float(resp.headers["x-kb-latency-ms"]) >= 20.0
        snap = registry.snapshot()
        hist = snap["histograms"][REQUEST_MS_METRIC]
        assert hist["count"] == 1
        assert hist["sum"] >= 20.0
        assert hist["max"] >= 20.0
        assert snap["counters"][_requests_key("200")] == 1.0
        assert snap["gauges"][INFLIGHT_GAUGE] == 0.0  # 请求结束后在途并发归零
        traces = snap["traces"]
        assert len(traces) == 1
        assert traces[0]["trace_id"] == "trace-fixed-0001"
        assert traces[0]["status"] == 200
        assert traces[0]["latency_ms"] >= 20.0
        assert traces[0]["slow"] is False  # 20ms 远低于 400ms 预警线
        assert registry.evaluate_thresholds() == []  # 单次快请求：无告警

    def test_error_status_counted_and_trace_generated(self) -> None:
        registry = MetricsRegistry()
        client = TestClient(self._build_app(registry))
        resp = client.post(SEARCH_PATH, params={"fail": 1, "delay_ms": 5})
        assert resp.status_code == 500
        snap = registry.snapshot()
        hist = snap["histograms"][REQUEST_MS_METRIC]
        assert hist["count"] == 1  # 500 响应同样计入 P95 观测（总量口径不挑状态）
        assert snap["counters"][_requests_key("500")] == 1.0
        error_key = (
            f'{ERRORS_TOTAL_METRIC}{{method="POST",path="{SEARCH_PATH}",status="500"}}'
        )
        assert snap["counters"][error_key] == 1.0
        trace = snap["traces"][0]
        assert trace["status"] == 500
        assert len(trace["trace_id"]) == 16  # 未提供上游 trace → 本地生成 16 位 hex

    def test_non_search_path_not_instrumented(self) -> None:
        registry = MetricsRegistry()
        client = TestClient(self._build_app(registry))
        assert client.get("/healthz").status_code == 200
        snap = registry.snapshot()
        assert snap["histograms"] == {}
        assert snap["counters"] == {}
        assert snap["traces"] == []


class TestHistogramQuantiles:
    """histogram 分位计算：线性插值口径（构造已知延迟序列断言 P50/P95/P99）。"""

    def test_known_sequence_percentiles(self) -> None:
        registry = MetricsRegistry()
        for ms in range(1, 101):  # 1..100ms 等差序列，共 100 个样本
            registry.observe(REQUEST_MS_METRIC, float(ms))
        hist = registry.snapshot()["histograms"][REQUEST_MS_METRIC]
        assert hist["count"] == 100
        assert hist["sum"] == pytest.approx(5050.0)
        # 线性插值：idx=(n-1)*q/100 —— 50 → 49.5 → 50/51 插值 50.5；95 → 94.05；99 → 98.01
        assert hist["p50"] == pytest.approx(50.5)
        assert hist["p95"] == pytest.approx(95.05)
        assert hist["p99"] == pytest.approx(99.01)
        assert hist["min"] == pytest.approx(1.0)
        assert hist["max"] == pytest.approx(100.0)

    def test_bucket_cumulative_counts(self) -> None:
        registry = MetricsRegistry()
        for ms in (10.0, 30.0, 100.0):
            registry.observe(REQUEST_MS_METRIC, ms)
        hist = registry.snapshot()["histograms"][REQUEST_MS_METRIC]
        buckets = hist["buckets"]
        assert buckets["25"] == 1  # ≤25：仅 10
        assert buckets["50"] == 2  # ≤50：10、30
        assert buckets["100"] == 3  # ≤100：全量（le 含边界，Prometheus 口径）
        assert buckets["+Inf"] == 3
        # 3 样本 P95：idx=1.9 → 30×0.1 + 100×0.9 = 93（插值口径的显式断言）
        assert hist["p95"] == pytest.approx(93.0)

    def test_empty_histogram_zeroed_stats(self) -> None:
        registry = MetricsRegistry()
        registry.histogram(REQUEST_MS_METRIC)  # 仅创建未观测
        hist = registry.snapshot()["histograms"][REQUEST_MS_METRIC]
        assert hist["count"] == 0
        assert hist["p95"] == 0.0
        assert hist["buckets"]["500"] == 0


class TestThresholdEvaluation:
    """P95 阈值三态（§6.7 从严口径：400ms 预警 / 500ms 告警，互斥取最高级）。"""

    def _seed(self, registry: MetricsRegistry, base_ms: float, tail_ms: float) -> None:
        """94×base + 6×tail 共 100 样本：P95 恰落在 tail 值（第 95/96 个样本均为 tail）。"""
        for _ in range(94):
            registry.observe(REQUEST_MS_METRIC, base_ms)
        for _ in range(6):
            registry.observe(REQUEST_MS_METRIC, tail_ms)

    def test_no_alert_under_threshold(self) -> None:
        registry = MetricsRegistry()
        self._seed(registry, base_ms=300.0, tail_ms=320.0)  # P95=320 ≤ 400
        assert registry.evaluate_thresholds() == []

    def test_warning_between_400_and_500(self) -> None:
        registry = MetricsRegistry()
        self._seed(registry, base_ms=100.0, tail_ms=450.0)  # P95=450 ∈ (400,500]
        alerts = registry.evaluate_thresholds()
        assert [a.name for a in alerts] == [ALERT_WARN_NAME]
        assert alerts[0].severity == SEVERITY_WARNING
        assert alerts[0].value_ms == pytest.approx(450.0)
        assert alerts[0].threshold_ms == pytest.approx(400.0)

    def test_critical_above_500(self) -> None:
        registry = MetricsRegistry()
        self._seed(registry, base_ms=100.0, tail_ms=620.0)  # P95=620 > 500
        alerts = registry.evaluate_thresholds()
        assert [a.name for a in alerts] == [ALERT_CRITICAL_NAME]
        assert alerts[0].severity == SEVERITY_CRITICAL
        assert alerts[0].value_ms == pytest.approx(620.0)
        assert "NFR-PER-02" in alerts[0].message  # 告警文案锚定需求编号

    def test_empty_or_missing_metric_no_alerts(self) -> None:
        assert MetricsRegistry().evaluate_thresholds() == []  # 空注册表不告警
        registry = MetricsRegistry()
        registry.observe(REQUEST_MS_METRIC, 10.0)
        assert registry.evaluate_thresholds(metric="不存在的指标") == []


class TestMetricsEndpoint:
    """GET /internal/metrics：JSON 快照（供监控采集）+ 告警段实时评估。"""

    def test_endpoint_returns_snapshot_and_alerts(self) -> None:
        registry = MetricsRegistry()
        app = FastAPI()
        app.add_middleware(TimingMiddleware, registry=registry)

        @app.post(SEARCH_PATH)
        async def search() -> dict[str, object]:
            await asyncio.sleep(0.005)
            return {"results": [], "latency_ms": 5}

        mount_metrics_endpoint(app, registry)
        client = TestClient(app)
        assert client.post(SEARCH_PATH).status_code == 200

        resp = client.get("/internal/metrics")
        assert resp.status_code == 200
        payload = resp.json()
        # 快照结构完整（采集端契约）：指标三类 + 轨迹 + 阈值 + 告警段
        assert set(payload) >= {"generated_at", "counters", "gauges", "histograms", "traces", "thresholds", "alerts"}
        assert payload["thresholds"]["p95_warn_ms"] == 400
        assert payload["thresholds"]["p95_critical_ms"] == 500
        assert payload["histograms"][REQUEST_MS_METRIC]["count"] == 1
        assert payload["counters"][_requests_key("200")] == 1.0
        assert payload["alerts"] == []  # 5ms 级快请求：无告警

        # /internal/metrics 自身不进计时口径（路径过滤）：直采不产生新观测
        assert client.get("/internal/metrics").json()["histograms"][REQUEST_MS_METRIC]["count"] == 1


class TestRegistryThreadSafety:
    """并发写入互证：多线程 observe/inc 后计数精确一致（内存线程安全实现）。"""

    def test_concurrent_observe_and_inc(self) -> None:
        registry = MetricsRegistry()
        labels = {"method": "POST", "path": "/x", "status": "200"}

        def worker() -> None:
            for _ in range(50):
                registry.observe(REQUEST_MS_METRIC, 1.0)
                registry.inc(REQUESTS_TOTAL_METRIC, 1.0, labels=labels)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = registry.snapshot()
        assert snap["histograms"][REQUEST_MS_METRIC]["count"] == 400  # 8 线程 × 50 次
        assert snap["histograms"][REQUEST_MS_METRIC]["sum"] == pytest.approx(400.0)
        assert snap["counters"][f'{REQUESTS_TOTAL_METRIC}{{method="POST",path="/x",status="200"}}'] == 400.0


class TestLoadScriptSmoke:
    """压测脚本冒烟：本地 FastAPI 假端点（每线程独立 TestClient 通道 + 真实多线程）。"""

    def test_smoke_pass_against_fake_endpoint(self) -> None:
        registry = MetricsRegistry()
        app = FastAPI()
        app.add_middleware(TimingMiddleware, registry=registry)

        @app.post(SEARCH_PATH)
        async def search() -> dict[str, object]:
            await asyncio.sleep(0.005)  # 5ms 模拟检索底噪
            return {"results": [], "latency_ms": 5}

        def client_factory() -> TestClient:
            # TestClient 是 httpx.Client 子类：以 ASGI 通道替代真实端口，满足工厂契约
            return TestClient(app)

        report = run_load_test(
            f"http://testserver{SEARCH_PATH}",
            concurrency=3,
            duration_s=0.5,
            json_body={"query": "smoke test", "top_k": 5},
            client_factory=client_factory,
        )
        assert report.total >= 3  # 3 线程 × 0.5s 至少各完成一次请求
        assert report.ok == report.total
        assert report.failed == 0
        assert report.error_rate == 0.0
        assert report.qps > 0
        assert 0.0 < report.p50_ms <= report.p95_ms <= report.p99_ms  # 分位单调性
        assert report.passed is True  # 判定口径：P95≤500ms 且 错误率<0.1%
        # 双口径互校：中间件观测数 == 压测端成功请求数（并发路径的线程安全）
        assert registry.snapshot()["histograms"][REQUEST_MS_METRIC]["count"] == report.ok
