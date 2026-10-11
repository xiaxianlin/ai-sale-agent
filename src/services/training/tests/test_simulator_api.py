# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""模拟客户 API 集成测试（FR-TR-002，SR-TR-002 §5 接口 + §9；迭代 1.2 TDD 阶段①）。

端点（普通 JSON 信封，复盘生成为 POST 触发）：
- POST /api/v1/training/simulator/start                    开场（开场情境即首条 customer 消息）
- POST /api/v1/training/simulator/reply                     对话推进
- GET|POST /api/v1/training/simulator/sessions/{id}/debrief 复盘读取 / 生成

五类场景覆盖（开发规范 §9.1）：
- 等价类：信封契约、start→reply→debrief 全链、复盘读取；
- 边界：难度越界、复盘未生成即读取（40401 REPORT 语义）；
- 异常：未知 persona/场景/会话、空内容、已结束会话再回复、缺参校验；
- 并发/幂等：同 request_id 重放（start/reply）；
- 降级：LLM 故障 → 200 + degraded=true（不 500）。

错误码（04-接口设计 §1.3 口径，本模块取 40001/40401/50002）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.training.api.simulator_api import (
    CODE_BAD_REQUEST,
    CODE_NOT_FOUND,
    DEBRIEF_PATH_FMT,
    REPLY_PATH,
    START_PATH,
    create_simulator_app,
)
from services.training.core.simulator.engine import (
    FakeSimulatorLlm,
    SimulatorEngine,
    SimulatorLlmError,
)
from services.training.core.simulator.persona import PersonaRegistry
from services.training.core.simulator.store import InMemorySimSessionStore

NEUTRAL_MSG = "你好，请继续。"


def make_client(llm=None, *, max_rounds: int = 20) -> TestClient:
    """每用例独立应用实例（独立引擎 + 内存存储，互不串扰）。"""
    engine = SimulatorEngine(
        llm=llm if llm is not None else FakeSimulatorLlm(),
        store=InMemorySimSessionStore(),
        registry=PersonaRegistry.with_demo_extensions(),
        max_rounds=max_rounds,
    )
    return TestClient(create_simulator_app(engine))


class TestEnvelopeContract:
    """统一信封（04-接口设计 §1.3）：code/message/data/trace_id 四键齐全。"""

    def test_envelope_keys_and_trace_passthrough(self) -> None:
        """信封四键 + X-Trace-Id 透传。"""
        client = make_client()
        resp = client.post(START_PATH, json={"persona_id": "C-01", "scenario_id": "sc-bargain-moq"},
                           headers={"X-Trace-Id": "trace-sim-001"})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"code", "message", "data", "trace_id"}
        assert body["code"] == 0 and body["trace_id"] == "trace-sim-001"


class TestHappyPath:
    """等价类：start → reply →（结束）→ debrief 全链。"""

    def test_start_payload(self) -> None:
        """start 出参：session_id/opening/difficulty/max_rounds/state。"""
        client = make_client()
        data = client.post(
            START_PATH, json={"persona_id": "C-01", "scenario_id": "sc-bargain-moq"}
        ).json()["data"]
        assert data["session_id"].startswith("sim-")
        assert data["persona_id"] == "C-01" and data["difficulty"] == 2
        assert data["max_rounds"] == 20
        assert "隔壁供应商低 8%" in data["opening"]
        assert data["state"]["stance"] == "坚持"

    def test_reply_payload(self) -> None:
        """reply 出参：customer_reply/state/round/ended/ended_reason/degraded。"""
        client = make_client()
        sid = client.post(
            START_PATH, json={"persona_id": "C-01", "scenario_id": "sc-bargain-moq", "difficulty": 3}
        ).json()["data"]["session_id"]
        data = client.post(REPLY_PATH, json={"session_id": sid, "content": NEUTRAL_MSG}).json()["data"]
        assert data["customer_reply"]
        assert data["round"] == 1 and data["ended"] is False
        assert data["degraded"] is False and data["ended_reason"] is None
        assert set(data["state"]) == {"price_stance", "emotion", "patience", "stance", "difficulty"}

    def test_reply_until_timeout_then_debrief(self) -> None:
        """全链：推进至超时结束 → POST 生成复盘 → GET 读取一致。"""
        client = make_client(max_rounds=2)
        sid = client.post(
            START_PATH, json={"persona_id": "C-01", "scenario_id": "sc-bargain-moq"}
        ).json()["data"]["session_id"]
        last = None
        for _ in range(2):
            last = client.post(REPLY_PATH, json={"session_id": sid, "content": NEUTRAL_MSG}).json()["data"]
        assert last["ended"] is True and last["ended_reason"] == "timeout"

        debrief_path = DEBRIEF_PATH_FMT.format(session_id=sid)
        generated = client.post(debrief_path).json()["data"]
        assert generated["verdict"] == "timeout" and generated["session_id"] == sid
        assert len(generated["suggestions"]) == 3
        fetched = client.get(debrief_path).json()["data"]
        assert fetched == generated

    def test_get_debrief_before_generation_40401(self) -> None:
        """边界：会话存在但复盘未生成 → GET 40401（REPORT 未就绪语义）。"""
        client = make_client()
        sid = client.post(
            START_PATH, json={"persona_id": "C-01", "scenario_id": "sc-bargain-moq"}
        ).json()["data"]["session_id"]
        resp = client.get(DEBRIEF_PATH_FMT.format(session_id=sid))
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == CODE_NOT_FOUND and "尚未生成" in body["message"]


class TestApiErrors:
    """异常 + 边界：错误码映射。"""

    def test_start_unknown_persona_40401(self) -> None:
        client = make_client()
        resp = client.post(START_PATH, json={"persona_id": "C-99", "scenario_id": "sc-bargain-moq"})
        assert resp.status_code == 404 and resp.json()["code"] == CODE_NOT_FOUND

    def test_start_unknown_scenario_40401(self) -> None:
        client = make_client()
        resp = client.post(START_PATH, json={"persona_id": "C-01", "scenario_id": "sc-not-exist"})
        assert resp.status_code == 404 and resp.json()["code"] == CODE_NOT_FOUND

    def test_start_difficulty_out_of_range_40001(self) -> None:
        """难度 0/7 → pydantic 校验失败 → 40001（data.errors 定位字段）。"""
        client = make_client()
        for bad in (0, 7):
            resp = client.post(
                START_PATH,
                json={"persona_id": "C-01", "scenario_id": "sc-bargain-moq", "difficulty": bad},
            )
            assert resp.status_code == 400 and resp.json()["code"] == CODE_BAD_REQUEST

    def test_reply_unknown_session_40401(self) -> None:
        client = make_client()
        resp = client.post(REPLY_PATH, json={"session_id": "sim-not-exist", "content": NEUTRAL_MSG})
        assert resp.status_code == 404 and resp.json()["code"] == CODE_NOT_FOUND

    def test_reply_empty_content_40001(self) -> None:
        client = make_client()
        sid = client.post(
            START_PATH, json={"persona_id": "C-01", "scenario_id": "sc-bargain-moq"}
        ).json()["data"]["session_id"]
        resp = client.post(REPLY_PATH, json={"session_id": sid, "content": ""})
        assert resp.status_code == 400 and resp.json()["code"] == CODE_BAD_REQUEST

    def test_reply_after_ended_40001(self) -> None:
        client = make_client(max_rounds=1)
        sid = client.post(
            START_PATH, json={"persona_id": "C-01", "scenario_id": "sc-bargain-moq"}
        ).json()["data"]["session_id"]
        client.post(REPLY_PATH, json={"session_id": sid, "content": NEUTRAL_MSG})
        resp = client.post(REPLY_PATH, json={"session_id": sid, "content": NEUTRAL_MSG})
        assert resp.status_code == 400 and resp.json()["code"] == CODE_BAD_REQUEST
        assert "已结束" in resp.json()["message"]

    def test_post_debrief_not_ended_40001(self) -> None:
        client = make_client()
        sid = client.post(
            START_PATH, json={"persona_id": "C-01", "scenario_id": "sc-bargain-moq"}
        ).json()["data"]["session_id"]
        resp = client.post(DEBRIEF_PATH_FMT.format(session_id=sid))
        assert resp.status_code == 400 and resp.json()["code"] == CODE_BAD_REQUEST

    def test_missing_required_field_40001(self) -> None:
        """缺参：persona_id 缺失 → RequestValidationError → 40001 信封（data.errors）。"""
        client = make_client()
        resp = client.post(START_PATH, json={"scenario_id": "sc-bargain-moq"})
        body = resp.json()
        assert resp.status_code == 400 and body["code"] == CODE_BAD_REQUEST
        assert body["data"]["errors"][0]["loc"] == ["body", "persona_id"]


class TestApiIdempotency:
    """并发/幂等：request_id 重放（同一响应、状态只推进一次）。"""

    def test_start_idempotent(self) -> None:
        client = make_client()
        payload = {"persona_id": "C-01", "scenario_id": "sc-bargain-moq", "request_id": "api-start-1"}
        first = client.post(START_PATH, json=payload).json()["data"]
        second = client.post(START_PATH, json=payload).json()["data"]
        assert first == second

    def test_reply_idempotent(self) -> None:
        client = make_client()
        sid = client.post(
            START_PATH, json={"persona_id": "C-01", "scenario_id": "sc-bargain-moq", "difficulty": 3}
        ).json()["data"]["session_id"]
        payload = {"session_id": sid, "content": NEUTRAL_MSG, "request_id": "api-msg-1"}
        first = client.post(REPLY_PATH, json=payload).json()["data"]
        second = client.post(REPLY_PATH, json=payload).json()["data"]
        assert first == second
        # 重放不推进：再次（新 request_id）回复 round 仍为 2 而非 3
        third = client.post(
            REPLY_PATH, json={"session_id": sid, "content": NEUTRAL_MSG, "request_id": "api-msg-2"}
        ).json()["data"]
        assert third["round"] == 2


class TestApiDegradation:
    """降级：LLM 故障经 API 表现为 200 + degraded=true（会话不中断）。"""

    def test_broken_llm_reply_degraded_200(self) -> None:
        class BrokenLlm:
            def generate_reply(self, **kwargs) -> str:
                raise SimulatorLlmError("模型超时（模拟故障）")

        client = make_client(llm=BrokenLlm())
        sid = client.post(
            START_PATH, json={"persona_id": "C-01", "scenario_id": "sc-bargain-moq", "difficulty": 3}
        ).json()["data"]["session_id"]
        resp = client.post(REPLY_PATH, json={"session_id": sid, "content": NEUTRAL_MSG})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["degraded"] is True and data["customer_reply"]
        assert data["state"]["patience"] == 92  # 状态机照常演化
