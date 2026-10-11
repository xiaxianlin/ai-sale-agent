# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""考试认证 API 集成测试（FR-TR-007，SR-TR-007 §5 接口增量；TDD 阶段①用例设计，TestClient 通道）。

覆盖（统一信封 code/message/data/trace_id 四键，04-接口设计 §1.3 口径）：
- POST /api/v1/training/exam/start：开考（组卷+开考合一 demo 端点）——考生视图无答案、
  attempt_no、会话令牌；非法学员 ID → 40001；
- POST /api/v1/training/exam/submit：交卷评分（全对通过授予 / 全错不通过）、
  未知试卷 40404、业务码 E_* 随 data.biz_code 承载；
- GET  /api/v1/training/exam/cert/{trainee_id}：认证状态查询三态；
- POST /api/v1/training/exam/{uid}/proctor-events + /review：监考事件与复核裁决端点。
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.training.api.exam_api import (
    CERT_PATH,
    EXAM_PROCTOR_EVENTS_PATH,
    EXAM_REVIEW_PATH,
    EXAM_START_PATH,
    EXAM_SUBMIT_PATH,
    CODE_BAD_REQUEST,
    CODE_NOT_FOUND,
    create_exam_app,
)
from services.training.core.exam.paper import InMemoryQuestionBank, build_demo_questions
from services.training.core.exam.proctor import ProctorEvent
from services.training.core.exam.service import ExamService, InMemoryExamStore

from services.training.tests.test_exam_service import FakeClock, correct_answers


@pytest.fixture()
def env() -> dict[str, Any]:
    """标准装配：demo 题库 + 内存存储 + 假时钟 + TestClient。"""
    bank = InMemoryQuestionBank(build_demo_questions(per_dim_obj=11, per_dim_subj=4))
    service = ExamService(bank, InMemoryExamStore(), clock=FakeClock())
    return {"client": TestClient(create_exam_app(exam_service=service)), "service": service}


def start_exam(client: TestClient, trainee_id: int = 901) -> dict[str, Any]:
    """开考并返回 data 段（exam_uid/session_token/questions）。"""
    resp = client.post(EXAM_START_PATH, json={"trainee_id": trainee_id})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


class TestExamApi:
    """考试端点：信封形态 / 评分链路 / 错误码映射。"""

    def test_start_envelope_and_view(self, env: dict[str, Any]) -> None:
        """开考信封：四键齐全；考生视图不含答案；attempt_no=1、session_token 非空。"""
        client: TestClient = env["client"]
        resp = client.post(EXAM_START_PATH, json={"trainee_id": 901})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"code", "message", "data", "trace_id"}
        assert body["code"] == 0
        data = body["data"]
        assert data["attempt_no"] == 1
        assert data["session_token"]
        assert data["questions"]
        assert all("answer" not in q and "rubric" not in q for q in data["questions"])

    def test_start_twice_conflict(self, env: dict[str, Any]) -> None:
        """进行中重复开考 → 409 + E_EXAM_ONGOING（data.biz_code 承载）。"""
        client: TestClient = env["client"]
        start_exam(client, 902)
        resp = client.post(EXAM_START_PATH, json={"trainee_id": 902})
        assert resp.status_code == 409
        body = resp.json()
        assert body["data"]["biz_code"] == "E_EXAM_ONGOING"

    def test_start_invalid_trainee_40001(self, env: dict[str, Any]) -> None:
        """异常等价类：trainee_id=0 → 40001 参数非法信封。"""
        client: TestClient = env["client"]
        resp = client.post(EXAM_START_PATH, json={"trainee_id": 0})
        assert resp.status_code == 400
        assert resp.json()["code"] == CODE_BAD_REQUEST

    def test_submit_pass_grants_cert(self, env: dict[str, Any]) -> None:
        """全对交卷 → passed=true、cert_status=certified（考试→认证全链 API 跑通）。"""
        client: TestClient = env["client"]
        service: ExamService = env["service"]
        data = start_exam(client, 903)
        answers = correct_answers(service, data["exam_uid"])
        resp = client.post(
            EXAM_SUBMIT_PATH,
            json={"exam_uid": data["exam_uid"], "trainee_id": 903, "answers": answers},
        )
        assert resp.status_code == 200
        result = resp.json()["data"]
        assert result["passed"] is True
        assert result["score"] == 100.0
        assert result["cert_status"] == "certified"
        assert result["section_scores"]

    def test_submit_fail_keeps_uncertified(self, env: dict[str, Any]) -> None:
        """全错交卷 → passed=false、cert_status=uncertified（触发重考策略口径）。"""
        client: TestClient = env["client"]
        data = start_exam(client, 904)
        resp = client.post(
            EXAM_SUBMIT_PATH,
            json={"exam_uid": data["exam_uid"], "trainee_id": 904,
                  "answers": {q["question_id"]: "ZZ" for q in data["questions"]}},
        )
        result = resp.json()["data"]
        assert result["passed"] is False
        assert result["cert_status"] == "uncertified"

    def test_submit_unknown_exam_40404(self, env: dict[str, Any]) -> None:
        """未知试卷交卷 → 404 + 40404 + E_EXAM_NOT_FOUND。"""
        client: TestClient = env["client"]
        resp = client.post(EXAM_SUBMIT_PATH, json={"exam_uid": "EX-999999", "trainee_id": 1, "answers": {}})
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == CODE_NOT_FOUND
        assert body["data"]["biz_code"] == "E_EXAM_NOT_FOUND"

    def test_proctor_events_endpoint(self, env: dict[str, Any]) -> None:
        """监考事件上报端点：返回命中规则与加权分（证据链服务端累计）。"""
        client: TestClient = env["client"]
        data = start_exam(client, 905)
        events = [
            {"t": 1000, "type": "blur"}, {"t": 2000, "type": "focus"},
            {"t": 3000, "type": "blur"}, {"t": 4000, "type": "focus"},
            {"t": 5000, "type": "blur"}, {"t": 6000, "type": "focus"},
        ]
        resp = client.post(
            EXAM_PROCTOR_EVENTS_PATH.format(exam_uid=data["exam_uid"]), json={"events": events}
        )
        assert resp.status_code == 200
        result = resp.json()["data"]
        assert result["flags"][0]["rule"] == "switch_window"
        assert result["cheat_score"] == 3

    def test_review_endpoint_upheld(self, env: dict[str, Any]) -> None:
        """复核裁决端点：upheld → 本卷 void、passed=false（主管裁决走 API）。"""
        client: TestClient = env["client"]
        data = start_exam(client, 906)
        cheat_events = [
            {"t": 1000, "type": "blur"}, {"t": 2000, "type": "focus"},
            {"t": 3000, "type": "blur"}, {"t": 4000, "type": "focus"},
            {"t": 5000, "type": "blur"}, {"t": 6000, "type": "focus"},
            {"t": 7000, "type": "paste", "detail": {"len": 80}},
        ]
        client.post(EXAM_PROCTOR_EVENTS_PATH.format(exam_uid=data["exam_uid"]), json={"events": cheat_events})
        service: ExamService = env["service"]
        answers = correct_answers(service, data["exam_uid"])
        submitted = client.post(
            EXAM_SUBMIT_PATH, json={"exam_uid": data["exam_uid"], "trainee_id": 906, "answers": answers}
        )
        assert submitted.json()["data"]["review_status"] == "pending"
        resp = client.post(
            EXAM_REVIEW_PATH.format(exam_uid=data["exam_uid"]),
            json={"verdict": "upheld", "reviewer": "主管甲", "note": "证据成立"},
        )
        assert resp.status_code == 200
        result = resp.json()["data"]
        assert result["status"] == "void"
        assert result["passed"] is False

    def test_cert_endpoint_states(self, env: dict[str, Any]) -> None:
        """认证状态查询端点：uncertified → certified 迁移可见（三态查询口径）。"""
        client: TestClient = env["client"]
        service: ExamService = env["service"]
        first = client.get(CERT_PATH.format(trainee_id=907)).json()["data"]
        assert first["cert_status"] == "uncertified"
        data = start_exam(client, 907)
        client.post(
            EXAM_SUBMIT_PATH,
            json={"exam_uid": data["exam_uid"], "trainee_id": 907,
                  "answers": correct_answers(service, data["exam_uid"])},
        )
        second = client.get(CERT_PATH.format(trainee_id=907)).json()["data"]
        assert second["cert_status"] == "certified"
        assert second["onboarded_at"]

    def test_trace_id_passthrough(self, env: dict[str, Any]) -> None:
        """trace_id：X-Trace-Id 头优先透传（04 §1.4）。"""
        client: TestClient = env["client"]
        resp = client.post(
            EXAM_START_PATH, json={"trainee_id": 908}, headers={"X-Trace-Id": "trace-exam-001"}
        )
        assert resp.json()["trace_id"] == "trace-exam-001"
