# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""引导 API 集成测试（NFR-USA-03，迭代 1.1；TestClient 通道，SR §5/§9）。

覆盖：
- GET /api/v1/training/guidance/tour：统一信封、内置 v1 配置（steps/current_version）、
  trainee_id 携带时的版本比较与门控三键、trace_id 透传（X-Trace-Id）；
- POST /api/v1/training/guidance/tour/state：done/skipped 上报落库、上报后再查
  should_show=False（回访不打扰）、非法 outcome/非法 seen_version → 40001 信封、
  未知 tour_id → 40404 信封；
- GET /api/v1/training/guidance/empty-states：8 类场景表、scene_id 精确过滤、
  未命中返回空列表（前端回退默认文案不阻塞）；
- 依赖注入：app.state.guidance_service 优先于默认懒加载实例（装配契约）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.training.api.guidance_api import (
    EMPTY_STATES_PATH,
    TOUR_PATH,
    TOUR_STATE_PATH,
    create_guidance_app,
    guidance_api,
)
from services.training.core.guidance.config import FIRST_LOGIN_TOUR
from services.training.core.guidance.service import GuidanceService
from services.training.core.guidance.state import InMemoryGuidanceStateStore


@pytest.fixture()
def client() -> TestClient:
    """每用例独立应用实例（独立 GuidanceService，内存存储互不串扰）。"""
    app = create_guidance_app(GuidanceService(states=InMemoryGuidanceStateStore()))
    return TestClient(app)


class TestEnvelopeContract:
    """统一信封（04-接口设计 §1.3）：code/message/data/trace_id 四键齐全。"""

    def test_envelope_keys(self, client: TestClient) -> None:
        resp = client.get(TOUR_PATH)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"code", "message", "data", "trace_id"}
        assert body["code"] == 0

    def test_trace_id_passthrough(self, client: TestClient) -> None:
        """X-Trace-Id 头透传；缺失时本地生成（04 §1.4）。"""
        forwarded = client.get(TOUR_PATH, headers={"X-Trace-Id": "trace-guidance-001"})
        assert forwarded.json()["trace_id"] == "trace-guidance-001"
        generated = client.get(TOUR_PATH).json()["trace_id"]
        assert isinstance(generated, str) and generated  # 非空即本地生成


class TestGetTour:
    """GET /tour：当前导览 + 版本比较。"""

    def test_tour_config_payload(self, client: TestClient) -> None:
        """内置 v1 导览：tour_id/version/enabled/steps（含 data-tour 选择器）。"""
        data = client.get(TOUR_PATH).json()["data"]
        assert data["tour_id"] == "first-login"
        assert data["current_version"] == FIRST_LOGIN_TOUR.version
        tour = data["tour"]
        assert tour["enabled"] is True
        assert len(tour["steps"]) == len(FIRST_LOGIN_TOUR.steps)
        assert all("[data-tour=" in step["target_selector"] for step in tour["steps"])

    def test_tour_with_trainee_gate_fields(self, client: TestClient) -> None:
        """带 trainee_id：首访 should_show=True；跳过上报后 should_show=False
        （同一端点同时服务自动触发与门控复核，前端双保险的服务端权威口径）。"""
        first = client.get(TOUR_PATH, params={"trainee_id": 1}).json()["data"]
        assert first["should_show"] is True
        assert first["seen_version"] is None
        assert first["major_changed"] is False

        client.post(TOUR_STATE_PATH, json={"trainee_id": 1, "outcome": "skipped", "last_step_seq": 1})
        second = client.get(TOUR_PATH, params={"trainee_id": 1}).json()["data"]
        assert second["should_show"] is False
        assert second["seen_version"] == FIRST_LOGIN_TOUR.version

    def test_tour_unknown_tour_id(self, client: TestClient) -> None:
        """未知导览 → HTTP 404 + 40404 信封（中文文案）。"""
        resp = client.get(TOUR_PATH, params={"tour_id": "no-such-tour"})
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == 40404
        assert "导览不存在" in body["message"]

    def test_tour_invalid_trainee_id(self, client: TestClient) -> None:
        """非法 trainee_id（<1）→ HTTP 400 + 40001 信封（data.errors 定位字段）。"""
        resp = client.get(TOUR_PATH, params={"trainee_id": 0})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 40001
        assert body["data"]["errors"], "校验失败应携带 errors 定位字段"


class TestPostTourState:
    """POST /tour/state：跳过/完成上报。"""

    def test_report_done(self, client: TestClient) -> None:
        """完成上报：state.done=True、should_show=False、幂等信封 code=0。"""
        resp = client.post(TOUR_STATE_PATH, json={"trainee_id": 21, "outcome": "done"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["state"]["done"] is True
        assert data["state"]["tour_id"] == "first-login"
        assert data["should_show"] is False

    def test_report_skipped_defaults(self, client: TestClient) -> None:
        """跳过上报：tour_id 缺省 first-login、last_step_seq 可选。"""
        resp = client.post(
            TOUR_STATE_PATH, json={"trainee_id": 22, "outcome": "skipped", "last_step_seq": 4}
        )
        data = resp.json()["data"]
        assert data["state"]["done"] is False
        assert data["state"]["last_step_seq"] == 4

    def test_report_invalid_outcome(self, client: TestClient) -> None:
        """非法 outcome → 40001 信封（Literal 枚举拦截）。"""
        resp = client.post(TOUR_STATE_PATH, json={"trainee_id": 23, "outcome": "paused"})
        assert resp.status_code == 400
        assert resp.json()["code"] == 40001

    def test_report_invalid_seen_version(self, client: TestClient) -> None:
        """非法 seen_version（格式违约）→ 40001 信封（中文报错）。"""
        resp = client.post(
            TOUR_STATE_PATH,
            json={"trainee_id": 24, "outcome": "done", "seen_version": "one"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == 40001
        assert "导览状态上报参数非法" in resp.json()["message"]

    def test_report_unknown_tour(self, client: TestClient) -> None:
        """未知导览上报 → 40404 信封。"""
        resp = client.post(
            TOUR_STATE_PATH, json={"trainee_id": 25, "tour_id": "no-such-tour", "outcome": "done"}
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == 40404


class TestEmptyStates:
    """GET /empty-states：8 类空状态文案表。"""

    def test_full_scene_table(self, client: TestClient) -> None:
        """全量表：8 类、字段齐全（scene_id/title/hint/action_label/action_route）。"""
        data = client.get(EMPTY_STATES_PATH).json()["data"]
        scenes = data["scenes"]
        assert len(scenes) == 8
        assert {scene["scene_id"] for scene in scenes} == {
            "tutor_no_qa", "tutor_no_session", "gap_empty", "simulate_empty",
            "drill_empty", "radar_empty", "path_empty", "exam_empty",
        }
        for scene in scenes:
            for key in ("scene_id", "page_route", "title", "hint", "action_label", "action_route"):
                assert scene.get(key), f"场景 {scene.get('scene_id')} 缺字段 {key}"

    def test_scene_id_filter(self, client: TestClient) -> None:
        """scene_id 过滤：命中返回单条；未命中返回空列表（不报错）。"""
        hit = client.get(EMPTY_STATES_PATH, params={"scene_id": "tutor_no_qa"}).json()["data"]
        assert [scene["scene_id"] for scene in hit["scenes"]] == ["tutor_no_qa"]
        miss = client.get(EMPTY_STATES_PATH, params={"scene_id": "no-such"}).json()["data"]
        assert miss["scenes"] == []


class TestWiring:
    """装配契约：路由前缀、依赖注入优先级。"""

    def test_router_prefix(self) -> None:
        """路由前缀与端点常量一致（前端/测试共用同一口径）。"""
        assert guidance_api.prefix == "/api/v1/training/guidance"
        assert TOUR_PATH.endswith("/tour")
        assert TOUR_STATE_PATH.endswith("/tour/state")
        assert EMPTY_STATES_PATH.endswith("/empty-states")

    def test_app_state_injection_preferred(self) -> None:
        """app.state.guidance_service 注入优先：隔离实例不污染默认懒加载单例。"""
        service = GuidanceService(states=InMemoryGuidanceStateStore())
        app = create_guidance_app(service)
        with TestClient(app) as injected:
            injected.post(TOUR_STATE_PATH, json={"trainee_id": 99, "outcome": "done"})
            assert service.get_state("first-login", 99) is not None
