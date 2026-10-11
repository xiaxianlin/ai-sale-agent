# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""上岗拦截中间件测试（FR-TR-007 ★核心实现点，SR-TR-007 §6.4 cert_gate；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类）：
- 等价类：cert_status 三态（uncertified/expired → 403 拦截；certified → 放行）；
- 边界：恰在授予前后同一请求的拦截/放行翻转；
- 异常：未携带身份头（视同未认证拦截）；
- 审计：拦截必留 CERT_BLOCKED 痕（系统级强制的验收证据，NFR-SEC-03）。
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.training.api.exam_api import PROTECTED_DEMO_PATH, create_exam_app
from services.training.core.exam.service import CERT_CERTIFIED, ExamService, InMemoryExamStore
from services.training.core.exam.paper import InMemoryQuestionBank, build_demo_questions

from services.training.tests.test_exam_service import FakeClock, correct_answers


@pytest.fixture()
def env() -> dict[str, Any]:
    """装配 app + service：protected-demo 拦截演示端点基座（假时钟可控有效期）。"""
    bank = InMemoryQuestionBank(build_demo_questions(per_dim_obj=11, per_dim_subj=4))
    clock = FakeClock()
    service = ExamService(bank, InMemoryExamStore(), clock=clock)
    app = create_exam_app(exam_service=service)
    return {"client": TestClient(app), "service": service, "clock": clock}


def get_protected(client: TestClient, trainee_id: int) -> Any:
    return client.get(PROTECTED_DEMO_PATH, headers={"X-Trainee-Id": str(trainee_id)})


class TestCertGate:
    """require_certified 依赖：未认证/过期一律 403，认证后放行（系统级强制，非管理约定）。"""

    def test_uncertified_blocked_403(self, env: dict[str, Any]) -> None:
        """未认证 → HTTP 403 + code=40301 + E_CERT_REQUIRED（SRS §6.2 系统级强制）。"""
        resp = get_protected(env["client"], 801)
        assert resp.status_code == 403
        body = resp.json()
        assert body["code"] == 40301
        assert body["data"]["biz_code"] == "E_CERT_REQUIRED"
        assert body["data"]["cert_status"] == "uncertified"

    def test_expired_blocked_403(self, env: dict[str, Any]) -> None:
        """过期（expired）同样 403 拦截——非 certified 一律拒绝。"""
        service: ExamService = env["service"]
        client: TestClient = env["client"]
        exam = service.create_exam(802)
        service.start(exam.exam_uid)
        service.submit(exam.exam_uid, correct_answers(service, exam.exam_uid))
        assert service.cert_status_of(802) == CERT_CERTIFIED
        env["clock"].advance(days=366)
        assert service.expire_sweep() == [802]
        resp = get_protected(client, 802)
        assert resp.status_code == 403
        assert resp.json()["data"]["cert_status"] == "expired"

    def test_certified_passes_through(self, env: dict[str, Any]) -> None:
        """认证通过 → 同一请求放行（200）。"""
        service: ExamService = env["service"]
        client: TestClient = env["client"]
        exam = service.create_exam(803)
        service.start(exam.exam_uid)
        service.submit(exam.exam_uid, correct_answers(service, exam.exam_uid))
        resp = get_protected(client, 803)
        assert resp.status_code == 200
        assert resp.json()["data"]["granted"] is True

    def test_missing_identity_header_blocked(self, env: dict[str, Any]) -> None:
        """异常：未携带 X-Trainee-Id → 视同未认证 403（不做静默放行）。"""
        resp = env["client"].get(PROTECTED_DEMO_PATH)
        assert resp.status_code == 403
        assert resp.json()["code"] == 40301

    def test_block_writes_audit_evidence(self, env: dict[str, Any]) -> None:
        """拦截留痕：CERT_BLOCKED 审计记录含学员与路径（NFR-SEC-03 验收证据）。"""
        client: TestClient = env["client"]
        service: ExamService = env["service"]
        get_protected(client, 804)
        actions = [e["action"] for e in service.audit_log()]
        assert "CERT_BLOCKED" in actions

    def test_block_then_grant_flips_verdict(self, env: dict[str, Any]) -> None:
        """端到端：未认证 403 → 考试通过 → 同请求 200（SR §9-② 系统级强制演示脚本）。"""
        client: TestClient = env["client"]
        service: ExamService = env["service"]
        assert get_protected(client, 805).status_code == 403
        exam = service.create_exam(805)
        service.start(exam.exam_uid)
        service.submit(exam.exam_uid, correct_answers(service, exam.exam_uid))
        assert get_protected(client, 805).status_code == 200
        actions = [e["action"] for e in service.audit_log()]
        assert actions.index("CERT_BLOCKED") < actions.index("CERT_GRANT")
