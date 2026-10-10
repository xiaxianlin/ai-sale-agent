# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""评测管理端点集成测试（FR-KB-016 / SR-KB-016 §5，迭代 0.3）。

真实路由 + 确定性 search_fn 桩（无 mock 在链路中途截断）：
- POST /api/v1/knowledge/evaluate：触发异步任务（信封 + job_id + 初始状态）、
  评测集语义校验 40001（重复 case_id）、pydantic 校验 40001（非法 kb_type）；
- GET /api/v1/knowledge/evaluate/{job_id}：轮询至终态（done 携带 metrics/
  gate/报告下载引用；failed 携带 error）、未知任务 40401、trace_id 透传。

注：仓库根 conftest.py 已处理顶层 platform 包名冲突（TestClient 可用）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.kb.api.admin.evaluation import install_evaluation
from services.kb.api.routes import install_error_handlers
from services.kb.core.evaluation import AttributionContext

#: 被测端点
TRIGGER_PATH = "/api/v1/knowledge/evaluate"


class StubSearch:
    """确定性检索桩：query → 统一检索 API data 段形态（含改写文本）。"""

    def __init__(self, mapping: dict[str, list[int]]) -> None:
        self._mapping = mapping
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, query: str, kb_types: list[str] | None = None,
                 filters: dict[str, list[str]] | None = None, top_k: int = 5) -> dict[str, Any]:
        self.calls.append({"query": query, "kb_types": kb_types, "top_k": top_k})
        chunk_ids = self._mapping.get(query, [])
        return {
            "results": [{"chunk_id": cid, "score": round(1.0 - i * 0.1, 3)}
                        for i, cid in enumerate(chunk_ids)],
            "rewritten_query": f"{query}（改写）",
            "latency_ms": 8,
        }


class BrokenSearch:
    """故障桩：恒抛错 → 任务进入 failed 终态。"""

    def __call__(self, *, query: str, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("检索链路不可用（测试桩）")


def _client(stub: Any, tmp_path: Any) -> tuple[TestClient, Any]:
    """组装被测应用：统一信封错误处理器 + 评测路由 + 桩注入。"""
    app = FastAPI(title="eval-test")
    install_error_handlers(app)
    admin = install_evaluation(
        app, stub, report_dir=tmp_path / "reports",
        attribution_context=AttributionContext(indexed_chunk_ids=frozenset({101, 102, 201})),
    )
    return TestClient(app), admin


def _cases_payload(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """标准触发请求体（内联数据集）。"""
    return {
        "dataset": {"name": "smoke", "version": 1, "cases": cases},
        "top_k": 5,
        "concurrency": 2,
        "threshold_pp": 2.0,
        "trigger": "ci",
    }


def _wait_terminal(client: TestClient, job_id: str, timeout_s: float = 5.0) -> dict[str, Any]:
    """轮询任务至终态（done/failed）；超时判失败。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"{TRIGGER_PATH}/{job_id}").json()
        if body["data"]["status"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    pytest.fail(f"评测任务 {job_id} 未在 {timeout_s}s 内进入终态")


SMOKE_CASES = [
    {"case_id": "C1", "query": "ST-3200 支架最大承载", "expected_chunk_ids": [101], "kb_type": "product",
     "dimensions": {"scene": "询价"}},
    {"case_id": "C2", "query": "交期如何确认", "expected_chunk_ids": [201], "kb_type": "process"},
    {"case_id": "C3", "query": "询价接待话术", "expected_chunk_ids": [102], "kb_type": "skill"},
]


def _envelope_ok(body: dict[str, Any]) -> None:
    """统一信封断言（04-接口设计 §1.3：四键齐全）。"""
    assert set(body) == {"code", "message", "data", "trace_id"}
    assert body["code"] == 0 and body["message"] == "ok" and body["trace_id"]


# ─────────────────────────────── 触发 → 轮询 → 终态 ───────────────────────────────


def test_触发并轮询至完成(tmp_path: Any) -> None:
    """C1/C3 命中、C2 未命中 → done；metrics 手算对照 + 报告引用齐备。"""
    stub = StubSearch({
        "ST-3200 支架最大承载": [101, 900, 901],
        "询价接待话术": [902, 102],
        "交期如何确认": [903, 904],
    })
    client, _admin = _client(stub, tmp_path)

    response = client.post(TRIGGER_PATH, json=_cases_payload(SMOKE_CASES),
                           headers={"X-Trace-Id": "trace-eval-0001"})
    assert response.status_code == 200
    body = response.json()
    _envelope_ok(body)
    assert body["trace_id"] == "trace-eval-0001"  # 透传优先
    job_id = body["data"]["job_id"]
    assert body["data"]["status"] in ("pending", "running")
    assert body["data"]["case_count"] == 3

    final = _wait_terminal(client, job_id)
    _envelope_ok(final)
    data = final["data"]
    assert data["status"] == "done" and data["case_count"] == 3
    metrics = data["metrics"]
    assert metrics["n"] == 3 and metrics["hit_count"] == 2
    assert metrics["top5_acc"] == pytest.approx(2 / 3)
    assert metrics["per_dimension"]["kb_type=product"]["top5_acc"] == pytest.approx(1.0)
    assert set(data["gate"]) == {"passed", "reasons"}
    assert data["gate"]["passed"] is True  # 无基线 → 相对门槛跳过

    report = data["report"]
    assert report["markdown_path"].endswith(f"{job_id}.md")
    assert report["xlsx_path"].endswith(f"{job_id}.xlsx")
    assert report["download_markdown"] == f"{TRIGGER_PATH}/{job_id}/report?fmt=md"
    assert Path(report["markdown_path"]).exists() and Path(report["xlsx_path"]).exists()
    assert stub.calls and all(call["top_k"] == 5 for call in stub.calls)


def test_基线回落门禁阻断态(tmp_path: Any) -> None:
    """带基线触发：回落超 2pp → 终态 gate.passed=False 且 reasons 非空。"""
    stub = StubSearch({"ST-3200 支架最大承载": [101], "询价接待话术": [102]})
    client, _admin = _client(stub, tmp_path)
    payload = _cases_payload(SMOKE_CASES)
    payload["baseline"] = {
        "n": 3, "hit_count": 3, "top5_acc": 1.0, "mrr": 0.9,
        "per_dimension": {"kb_type=process": {"n": 1, "hit_count": 1, "top5_acc": 1.0, "mrr": 1.0}},
    }
    job_id = client.post(TRIGGER_PATH, json=payload).json()["data"]["job_id"]
    data = _wait_terminal(client, job_id)["data"]
    assert data["status"] == "done"
    assert data["gate"]["passed"] is False
    assert any("kb_type=process" in reason for reason in data["gate"]["reasons"])


def test_执行体故障按未命中计入口径(tmp_path: Any) -> None:
    """search_fn 恒抛错 → 单条降级为未命中（保守口径）：任务仍 done，命中率 0。"""
    client, _admin = _client(BrokenSearch(), tmp_path)
    job_id = client.post(TRIGGER_PATH, json=_cases_payload(SMOKE_CASES)).json()["data"]["job_id"]
    data = _wait_terminal(client, job_id)["data"]
    assert data["status"] == "done"
    assert data["metrics"]["n"] == 3 and data["metrics"]["hit_count"] == 0
    assert data["metrics"]["top5_acc"] == 0.0


def test_报告落盘故障进入failed终态(tmp_path: Any) -> None:
    """报告目录不可写（被同名文件占位）→ 任务级兜底 failed 终态 + error 留痕。"""
    blocker = tmp_path / "reports"
    blocker.write_text("占位文件：使报告目录不可创建", encoding="utf-8")
    client, _admin = _client(StubSearch({"ST-3200 支架最大承载": [101]}), tmp_path)
    job_id = client.post(TRIGGER_PATH, json=_cases_payload(SMOKE_CASES)).json()["data"]["job_id"]
    data = _wait_terminal(client, job_id)["data"]
    assert data["status"] == "failed"
    assert data["error"]


# ─────────────────────────────── 校验失败路径 ───────────────────────────────


def test_重复case_id语义校验40001(tmp_path: Any) -> None:
    """内联数据集 case_id 重复 → HTTP 400，code 40001（语义校验层）。"""
    client, _admin = _client(StubSearch({}), tmp_path)
    cases = [dict(SMOKE_CASES[0]), dict(SMOKE_CASES[0])]
    response = client.post(TRIGGER_PATH, json=_cases_payload(cases))
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == 40001
    assert "重复 case_id" in body["message"]


def test_非法kb_type契约校验40001(tmp_path: Any) -> None:
    """kb_type 非枚举值 → RequestValidationError → 统一信封 40001。"""
    client, _admin = _client(StubSearch({}), tmp_path)
    cases = [dict(SMOKE_CASES[0], kb_type="catalog")]
    response = client.post(TRIGGER_PATH, json=_cases_payload(cases))
    assert response.status_code == 400
    assert response.json()["code"] == 40001


def test_未知任务40401(tmp_path: Any) -> None:
    """查询不存在的 job_id → HTTP 404，code 40401。"""
    client, _admin = _client(StubSearch({}), tmp_path)
    response = client.get(f"{TRIGGER_PATH}/no-such-job")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 40401 and body["data"] is None
