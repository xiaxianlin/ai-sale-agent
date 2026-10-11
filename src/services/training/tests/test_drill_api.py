# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""实战演练 API 集成测试（FR-TR-003，SR-TR-003 §5/§9；TDD 阶段①用例设计，TestClient 通道）。

覆盖：
- POST /api/v1/training/drill/cases/import：批量导入（干净+PII 混合 → 部分拒绝）、
  rubric 维度非法 → 40001 信封（data.errors 定位字段）；
- GET  /api/v1/training/drill/cases：分页抽题、weak_dim 薄弱维参数预留、
  容量闸门（TR-1003）、一致率冻结闸门（TR-2002）；
- POST /api/v1/training/drill/submit：评分成功 / LLM 失败降级 pending_manual（200 非报错）、
  空作答/超长（TR-1002）、未知案例（TR-1001）、幂等重放同 result_id；
- POST /api/v1/training/drill/results/{id}/supervisor-score + GET .../calibration/report：
  校准采样与一致率报表、结果缺失（TR-2003）、待人工不可校准（TR-2004）。

统一信封（04-接口设计 §1.3）：code/message/data/trace_id 四键；drill 业务错误在
data.biz_code 承载 SR-TR-003 §5 的 TR-xxxx 码（数值码沿用仓库 40001/40404/40902 口径）。
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.training.api.drill_api import (
    CALIBRATION_REPORT_PATH,
    CASES_IMPORT_PATH,
    CASES_PATH,
    DRILL_PREFIX,
    SUBMIT_PATH,
    create_drill_app,
)
from services.training.core.drill.casebank import DIM_STRATEGY, build_demo_cases
from services.training.tests.test_drill_service import (  # 复用装配助手与桩
    BrokenScorer,
    make_service,
    sup_scores,
)

CASES = build_demo_cases(100)
PII_CASE: dict[str, Any] = {
    "case_id": "DR-2026-8001",
    "inquiry_text": "请把目录发到 buyer@example.com 或致电 13800138000。",
    "context": "客户区域：测试区。",
    "excellent_reply": "感谢您的来函，资料已随邮件附上，期待您的回复。",
    "rubric": [
        {"point_id": "P1", "dim": "product_accuracy", "desc": "回应参数", "keywords": ["参数"]},
        {"point_id": "S1", "dim": "script", "desc": "感谢来函", "keywords": ["感谢"]},
        {"point_id": "G1", "dim": "strategy", "desc": "探询用途", "keywords": ["采购"]},
    ],
    "kb_type": "training",
    "difficulty": 2,
    "primary_dim": "product_accuracy",
}


def import_payload(case_dicts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"cases": case_dicts}


@pytest.fixture()
def small_client() -> TestClient:
    """小库应用（min_published=1，1 条案例）：信封/提交/校准形态测试基座。"""
    return TestClient(create_drill_app(make_service(min_published=1, cases=1)))


@pytest.fixture()
def import_client() -> TestClient:
    """空库应用（min_published=1，0 条案例）：批量导入测试基座（无预置案例撞号）。"""
    return TestClient(create_drill_app(make_service(min_published=1, cases=0)))


@pytest.fixture()
def full_client() -> TestClient:
    """满库应用（默认 100 闸门 + 100 条 demo 案例）：分页/投题闸门测试基座。"""
    return TestClient(create_drill_app(make_service(min_published=100, cases=100)))


@pytest.fixture()
def broken_client() -> TestClient:
    """LLM 故障应用：评分降级 pending_manual 路径基座。"""
    return TestClient(create_drill_app(make_service(BrokenScorer(), min_published=1, cases=2)))


class TestEnvelope:
    """统一信封（04-接口设计 §1.3）：code/message/data/trace_id 四键齐全。"""

    def test_envelope_keys(self, small_client: TestClient) -> None:
        resp = small_client.get(CASES_PATH)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"code", "message", "data", "trace_id"}
        assert body["code"] == 0 and body["trace_id"]


class TestImport:
    """POST /cases/import：批量导入 + 脱敏闸门部分拒绝。"""

    def test_import_mixed(self, import_client: TestClient) -> None:
        clean = [CASES[0].to_dict(), CASES[1].to_dict()]
        dirty = dict(PII_CASE)
        resp = import_client.post(CASES_IMPORT_PATH, json=import_payload([*clean, dirty]))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["imported_count"] == 2
        assert data["rejected_count"] == 1
        assert data["rejected"][0]["index"] == 2
        assert "mask.email" in data["rejected"][0]["rule_ids"]

    def test_import_invalid_dim_rejected_40001(self, import_client: TestClient) -> None:
        """rubric 维度非法（"product" 不在三维口径）→ 40001 信封（data.errors 定位）。"""
        bad = CASES[0].to_dict()
        bad["rubric"][0]["dim"] = "product"
        resp = import_client.post(CASES_IMPORT_PATH, json=import_payload([bad]))
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 40001
        assert body["data"]["errors"]


class TestCases:
    """GET /cases：分页抽题 + 薄弱维参数预留 + 启用/冻结闸门。"""

    def test_cases_pagination(self, full_client: TestClient) -> None:
        first = full_client.get(CASES_PATH, params={"page": 1, "page_size": 10}).json()["data"]
        second = full_client.get(CASES_PATH, params={"page": 2, "page_size": 10}).json()["data"]
        assert (first["total"], len(first["items"])) == (100, 10)
        assert first["items"][0]["case_id"] != second["items"][0]["case_id"]

    def test_cases_weak_dim_filter(self, full_client: TestClient) -> None:
        """薄弱维参数预留：weak_dim 过滤 primary_dim（strategy 共 33 条）。"""
        data = full_client.get(
            CASES_PATH, params={"page": 1, "page_size": 50, "weak_dim": DIM_STRATEGY}
        ).json()["data"]
        assert data["total"] == 33
        assert all(item["primary_dim"] == DIM_STRATEGY for item in data["items"])

    def test_cases_capacity_gate_tr1003(self) -> None:
        """边界：published <100 → 启用闸门拒绝（TR-1003，HTTP 409）。"""
        client = TestClient(create_drill_app(make_service(min_published=100, cases=2)))
        resp = client.get(CASES_PATH)
        assert resp.status_code == 409
        assert resp.json()["data"]["biz_code"] == "TR-1003"

    def test_cases_frozen_gate_tr2002(self) -> None:
        """降级：一致率 <85%（3 组全偏差）→ 冻结投题（TR-2002，HTTP 409）。"""
        service: Any = make_service(min_published=1, cases=3)
        for i in range(3):
            result = service.submit(
                case_id=CASES[i].case_id, answer="收到。", trainee_id=1, request_id=f"r{i}"
            )
            service.record_supervisor_score(
                result_id=result.result_id, supervisor="主管甲", scores=sup_scores(90, 90, 90)
            )
        resp = TestClient(create_drill_app(service)).get(CASES_PATH)
        assert resp.status_code == 409
        assert resp.json()["data"]["biz_code"] == "TR-2002"


class TestSubmitApi:
    """POST /submit：评分成功、降级待人工、参数校验、幂等重放。"""

    def test_submit_happy(self, full_client: TestClient) -> None:
        case = CASES[0]
        resp = full_client.post(
            SUBMIT_PATH,
            json={
                "case_id": case.case_id,
                "answer": case.excellent_reply,
                "trainee_id": 1,
                "request_id": "req-1",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "scored"
        assert len(data["dims"]) == 3 and data["total"] == 100
        assert data["gap_analysis"]["missing_points"] == []

    def test_submit_pending_manual(self, broken_client: TestClient) -> None:
        """降级：LLM 评分失败 → 200 + status=pending_manual（TR-2001 语义，非报错）。"""
        resp = broken_client.post(
            SUBMIT_PATH,
            json={"case_id": CASES[0].case_id, "answer": "收到。", "trainee_id": 1, "request_id": "r"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "pending_manual"
        assert data["scoring_error"]
        assert data["total"] is None and data["dims"] == []

    def test_submit_errors(self, small_client: TestClient) -> None:
        """异常：空作答/超长（TR-1002）、未知案例（TR-1001）。"""
        cases = [
            ({"case_id": CASES[0].case_id, "answer": " ", "trainee_id": 1, "request_id": "r"}, 400, "TR-1002"),
            (
                {"case_id": CASES[0].case_id, "answer": "好" * 2001, "trainee_id": 1, "request_id": "r"},
                400,
                "TR-1002",
            ),
            ({"case_id": "DR-2026-9999", "answer": "收到。", "trainee_id": 1, "request_id": "r"}, 404, "TR-1001"),
        ]
        for payload, status, biz in cases:
            resp = small_client.post(SUBMIT_PATH, json=payload)
            assert resp.status_code == status, payload
            assert resp.json()["data"]["biz_code"] == biz

    def test_submit_idempotent_replay(self, small_client: TestClient) -> None:
        payload = {
            "case_id": CASES[0].case_id,
            "answer": CASES[0].excellent_reply,
            "trainee_id": 1,
            "request_id": "req-x",
        }
        first = small_client.post(SUBMIT_PATH, json=payload).json()["data"]["result_id"]
        replay = small_client.post(SUBMIT_PATH, json=payload).json()["data"]["result_id"]
        assert first == replay  # 幂等重放返回同一结果


class TestSupervisorApi:
    """POST /results/{id}/supervisor-score + GET /calibration/report。"""

    def test_supervisor_score_and_report(self, small_client: TestClient) -> None:
        submit = small_client.post(
            SUBMIT_PATH,
            json={
                "case_id": CASES[0].case_id,
                "answer": CASES[0].excellent_reply,
                "trainee_id": 1,
                "request_id": "r",
            },
        ).json()["data"]
        resp = small_client.post(
            f"{DRILL_PREFIX}/results/{submit['result_id']}/supervisor-score",
            json={"supervisor": "主管甲", "scores": {"product_accuracy": 90, "script": 90, "strategy": 90}},
        )
        assert resp.status_code == 200
        cal = resp.json()["data"]["calibration"]
        assert cal["consistent"] is True  # 维差 10 ≤15 且总分差 10 ≤10
        report = small_client.get(CALIBRATION_REPORT_PATH).json()["data"]
        assert report["samples"] == 1 and report["consistency_rate"] == 100.0
        assert report["frozen"] is False and report["threshold"] == 85.0

    def test_supervisor_errors(self, small_client: TestClient, broken_client: TestClient) -> None:
        """异常：结果缺失（TR-2003）、待人工不可校准（TR-2004）、分值非法（40001）。"""
        missing = small_client.post(
            f"{DRILL_PREFIX}/results/RES-404/supervisor-score",
            json={"supervisor": "主管甲", "scores": {"product_accuracy": 90, "script": 90, "strategy": 90}},
        )
        assert missing.status_code == 404
        assert missing.json()["data"]["biz_code"] == "TR-2003"

        pending = broken_client.post(
            SUBMIT_PATH,
            json={"case_id": CASES[0].case_id, "answer": "收到。", "trainee_id": 1, "request_id": "r"},
        ).json()["data"]
        on_pending = broken_client.post(
            f"{DRILL_PREFIX}/results/{pending['result_id']}/supervisor-score",
            json={"supervisor": "主管甲", "scores": {"product_accuracy": 90, "script": 90, "strategy": 90}},
        )
        assert on_pending.status_code == 409
        assert on_pending.json()["data"]["biz_code"] == "TR-2004"

        submit = small_client.post(
            SUBMIT_PATH,
            json={"case_id": CASES[0].case_id, "answer": "收到。", "trainee_id": 2, "request_id": "r"},
        ).json()["data"]
        invalid = small_client.post(
            f"{DRILL_PREFIX}/results/{submit['result_id']}/supervisor-score",
            json={"supervisor": "主管甲", "scores": {"product_accuracy": 90, "script": 90}},
        )
        assert invalid.status_code == 400
        assert invalid.json()["code"] == 40001
