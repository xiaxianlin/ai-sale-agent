# 知识产权归甲方所有（COM-IP-01）
"""检索接口压测脚本（NFR-PER-02 验收跑分，纯 httpx 线程版，迭代 0.2）。

设计（SR-NFR-PER-02 §6.6「压测即验收」的基线形态）：
- 并发 N 线程 × 持续时长，压 POST /api/v1/knowledge/search（04-接口设计 §2）；
- 输出 P50/P95/P99/QPS/错误率，并按验收线判定：P95≤500ms 且 错误率<0.1% → PASS；
- 分位采用线性插值法，与 services/kb/api/metrics.py 的 percentile 同口径（工具脚本
  须独立于服务包运行——直接以文件路径执行时仓库根不在 sys.path——故不反向导入）；
- locust 编排版见同目录 retrieval_locust.py（CI/联调），判定线一致。

用法（仓库根；凭证经环境变量注入、不入 Git——NFR-SEC-06）：
  KB_LOAD_API_KEY=*** python3 tools/loadtest/retrieval_load.py \
      --url http://127.0.0.1:8000/api/v1/knowledge/search \
      --concurrency 10 --duration 30 --report out/bench_p95.json
退出码：0=PASS；1=FAIL（或参数错误）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
import sysconfig
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence


import httpx  # noqa: E402  (须在标准库 platform 语义恢复之后导入)

# ── 默认参数（口径来源：SRS §11.6 并发基线 / SR-NFR-PER-02 §6.6、§1 验收 KPI）──
DEFAULT_URL = "http://127.0.0.1:8000/api/v1/knowledge/search"
DEFAULT_QUERY = "stainless steel pipe 304 price and MOQ"
P95_LIMIT_MS = 500.0  # NFR-PER-02 硬线
ERROR_RATE_LIMIT = 0.001  # 错误率上限 0.1%
# 携带 JSON 请求体的方法（GET/HEAD 等不携带）
_JSON_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
# 客户端工厂：每工作线程独立创建一个（httpx.Client 及其子类均可注入，测试用 ASGI 通道）
ClientFactory = Callable[[], httpx.Client]


def _percentile(ordered: Sequence[float], q: float) -> float:
    """线性插值分位（与 services/kb/api/metrics.percentile 同法；入参须已升序）。"""
    if not ordered:
        raise ValueError("分位计算需要非空样本序列")
    if len(ordered) == 1:
        return float(ordered[0])
    idx = (len(ordered) - 1) * q / 100.0
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return float(ordered[lo])
    frac = idx - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


@dataclass(frozen=True)
class LoadReport:
    """压测报告：时延分位 + 吞吐 + 错误率 + PASS/FAIL 判定（NFR-PER-02 验收口径）。"""

    url: str
    method: str
    concurrency: int
    duration_s: float
    elapsed_s: float  # 实际墙钟时长（含收尾在途请求）
    total: int  # 总请求数（成功 + 失败）
    ok: int  # 成功请求数（2xx/3xx）
    failed: int  # 失败请求数（HTTP≥400 或传输异常）
    error_rate: float  # failed/total；无请求时为 1.0（无样本即失败）
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    qps: float
    p95_limit_ms: float
    error_rate_limit: float
    errors: tuple[str, ...] = field(default=())  # 去重失败原因样本（前若干条，含计数）

    @property
    def passed(self) -> bool:
        """验收判定：P95≤500ms 且 错误率<0.1% 且 至少有成功样本。"""
        return (
            self.total > 0
            and self.ok > 0
            and self.p95_ms <= self.p95_limit_ms
            and self.error_rate < self.error_rate_limit
        )

    def as_dict(self) -> dict[str, Any]:
        """导出 dict（--report JSON 落盘形态，验收留档）。"""
        return {
            "url": self.url,
            "method": self.method,
            "concurrency": self.concurrency,
            "duration_s": self.duration_s,
            "elapsed_s": round(self.elapsed_s, 3),
            "total": self.total,
            "ok": self.ok,
            "failed": self.failed,
            "error_rate": round(self.error_rate, 6),
            "p50_ms": round(self.p50_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "min_ms": round(self.min_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "qps": round(self.qps, 3),
            "p95_limit_ms": self.p95_limit_ms,
            "error_rate_limit": self.error_rate_limit,
            "passed": self.passed,
            "errors": list(self.errors),
        }

    def summary_lines(self) -> list[str]:
        """人读摘要行（stdout 输出，中文）。"""
        verdict = "PASS" if self.passed else "FAIL"
        return [
            f"[压测] {self.method} {self.url}  并发 {self.concurrency}  "
            f"时长 {self.duration_s:.1f}s（实际 {self.elapsed_s:.1f}s）",
            f"[请求] 总 {self.total}  成功 {self.ok}  失败 {self.failed}  "
            f"错误率 {self.error_rate:.3%}  QPS {self.qps:.1f}",
            f"[时延] P50 {self.p50_ms:.1f}ms  P95 {self.p95_ms:.1f}ms  P99 {self.p99_ms:.1f}ms  "
            f"min {self.min_ms:.1f}ms  max {self.max_ms:.1f}ms",
            f"[判定] {verdict}（要求 P95≤{self.p95_limit_ms:.0f}ms 且 "
            f"错误率<{self.error_rate_limit:.2%}，NFR-PER-02）",
        ]


def run_load_test(
    url: str,
    *,
    concurrency: int = 10,
    duration_s: float = 10.0,
    method: str = "POST",
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
    p95_limit_ms: float = P95_LIMIT_MS,
    error_rate_limit: float = ERROR_RATE_LIMIT,
    client_factory: ClientFactory | None = None,
) -> LoadReport:
    """并发 N 线程 × 持续时长打目标端点，返回 P50/P95/P99/QPS/错误率与判定。

    - 每工作线程独立 httpx.Client（连接复用且免跨线程共享；client_factory 可注入
      自定义通道——测试以 ASGI/TestClient 假端点替代真实端口）；
    - 失败口径：HTTP 状态 ≥400 或传输/协议异常；时延分位仅统计成功请求（失败请求
      只入错误率，避免异常路径拉偏 P95 验收读数）；
    - 线程以 Barrier 对齐起步，墙钟自统一起步点计，含收尾在途请求。
    """
    if concurrency < 1:
        raise ValueError(f"并发线程数须 ≥1：{concurrency}")
    if duration_s <= 0:
        raise ValueError(f"持续时长须 >0：{duration_s}")
    factory: ClientFactory = client_factory or (lambda: httpx.Client(timeout=timeout_s))
    body = json_body if method.upper() in _JSON_BODY_METHODS else None

    start_gate = threading.Barrier(concurrency + 1)
    started_at = [0.0]
    results: list[tuple[list[float], list[str]]] = [([], []) for _ in range(concurrency)]

    def worker(slot: int) -> None:
        lats, errs = results[slot]
        client = factory()
        try:
            start_gate.wait()
            deadline = started_at[0] + duration_s
            while time.perf_counter() < deadline:
                t0 = time.perf_counter()
                try:
                    resp = client.request(method, url, json=body, headers=headers)
                except httpx.HTTPError as exc:  # 超时/传输异常：计入失败，压测不中断
                    errs.append(f"传输异常 {type(exc).__name__}")
                    continue
                except Exception as exc:  # 防御：非预期异常同样只计失败
                    errs.append(f"异常 {type(exc).__name__}")
                    continue
                latency_ms = (time.perf_counter() - t0) * 1000.0
                if 200 <= resp.status_code < 400:
                    lats.append(latency_ms)
                else:
                    errs.append(f"HTTP {resp.status_code}")
        finally:
            client.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=False) for i in range(concurrency)]
    for t in threads:
        t.start()
    started_at[0] = time.perf_counter()  # 与 Barrier 释放点同步：全体线程同一起跑线
    start_gate.wait()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started_at[0]

    lats = [x for lat_list, _ in results for x in lat_list]
    errs = [e for _, err_list in results for e in err_list]
    ok, failed = len(lats), len(errs)
    total = ok + failed
    error_rate = failed / total if total else 1.0
    ordered = sorted(lats)
    error_summary = tuple(f"{msg} ×{n}" for msg, n in Counter(errs).most_common(5))
    return LoadReport(
        url=url,
        method=method.upper(),
        concurrency=concurrency,
        duration_s=duration_s,
        elapsed_s=elapsed,
        total=total,
        ok=ok,
        failed=failed,
        error_rate=error_rate,
        p50_ms=_percentile(ordered, 50) if ordered else 0.0,
        p95_ms=_percentile(ordered, 95) if ordered else 0.0,
        p99_ms=_percentile(ordered, 99) if ordered else 0.0,
        min_ms=ordered[0] if ordered else 0.0,
        max_ms=ordered[-1] if ordered else 0.0,
        qps=total / elapsed if elapsed > 0 else 0.0,
        p95_limit_ms=p95_limit_ms,
        error_rate_limit=error_rate_limit,
        errors=error_summary,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """命令行参数定义（默认值即 NFR-PER-02 验收口径）。"""
    parser = argparse.ArgumentParser(
        prog="retrieval_load",
        description="NFR-PER-02 检索接口压测（纯 httpx 线程版，无需 locust）",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="目标端点（默认 %(default)s）")
    parser.add_argument("--method", default="POST", help="HTTP 方法（默认 POST）")
    parser.add_argument("--concurrency", type=int, default=10, help="并发线程数（默认 10，SRS §11.6 口径）")
    parser.add_argument("--duration", type=float, default=10.0, help="持续时长秒（默认 10）")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="检索问询（拼装默认请求体）")
    parser.add_argument("--body-json", default=None, help="完整请求体 JSON 字符串（覆盖 --query）")
    parser.add_argument("--timeout", type=float, default=10.0, help="单请求超时秒（默认 10）")
    parser.add_argument("--p95-limit", type=float, default=P95_LIMIT_MS, help="P95 验收线 ms（默认 %(default)s）")
    parser.add_argument(
        "--error-rate-limit", type=float, default=ERROR_RATE_LIMIT, help="错误率上限（默认 0.001 即 0.1%%）"
    )
    parser.add_argument("--report", default=None, help="可选：JSON 报告落盘路径（验收留档）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口：解析参数 → 跑压测 → 输出摘要 → 可选落盘报告；退出码 0=PASS / 1=FAIL。"""
    args = build_arg_parser().parse_args(argv)
    if args.body_json is not None:
        try:
            json_body = json.loads(args.body_json)
        except json.JSONDecodeError as exc:
            print(f"[参数错误] --body-json 不是合法 JSON：{exc}", file=sys.stderr)
            return 1
    else:
        json_body = {"query": args.query, "kb_types": ["product"], "top_k": 5, "rerank": True}
    headers: dict[str, str] = {}
    api_key = os.environ.get("KB_LOAD_API_KEY", "").strip()
    if api_key:  # 凭证经环境变量注入，不入命令行历史与 Git（NFR-SEC-06）
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        report = run_load_test(
            args.url,
            concurrency=args.concurrency,
            duration_s=args.duration,
            method=args.method,
            json_body=json_body,
            headers=headers or None,
            timeout_s=args.timeout,
            p95_limit_ms=args.p95_limit,
            error_rate_limit=args.error_rate_limit,
        )
    except ValueError as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        return 1
    for line in report.summary_lines():
        print(line)
    for line in report.errors:
        print(f"[失败样本] {line}")
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[归档] 报告已写入 {report_path}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
