# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""学习路径 API 集成测试（FR-TR-005，SR-TR-005 §5 接口增量；TDD 阶段①用例设计，TestClient 通道）。

端点（统一信封 code/message/data/trace_id，04-接口设计 §1.3 口径）：
- GET  /api/v1/training/path/{trainee_id}          当前路径（分段视图 + 完成度）；
- POST /api/v1/training/path/{trainee_id}          评估更新后重算（版本 +1、已完成保留，
                                                     返回 kept_completed 供前端横幅）；
- POST /api/v1/training/path/{trainee_id}/progress 进度事件上报（event_uid 幂等）。

用例分组（开发规范 §9.1 五类场景）：
- 等价类：GET 当前路径主链路、POST 重算（版本/保留计数）、进度上报（状态 + 完成度）；
- 边界：同 event_uid 重放幂等（同响应体）；
- 异常：无路径 → TR-3101、无雷达快照 → TR-3100、前置锁定 → TR-3103（409）、
  课时不在路径 → TR-3102（404）、事件类型非法 → 40001；
- 端到端：雷达链路 × 路径链路串联手验路径（诊断初评 → 生成 v1 → 学习 →
  评估更新 → v2 重算保留已完成），两大链路一次贯通。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from services.training.api.path_api import (
    PATH_PROGRESS_PATH,
    PATH_REPLAN_PATH,
    PATH_VIEW_PATH,
    create_path_app,
)
from services.training.api.radar_api import (
    RADAR_COMPUTE_PATH,
    RADAR_GET_PATH,
    create_radar_app,
)
from services.training.core.path.planner import PathPlanner
from services.training.core.radar.engine import (
    Evidence,
    FakeEvidenceSource,
    InMemoryRadarSnapshotStore,
    RadarConfig,
    RadarEngine,
)
from services.training.tests.test_path_planner import (
    TRAINEE,
    make_catalog,
    make_planner,
)

NOW = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)


def make_radar_engine() -> tuple[RadarEngine, FakeEvidenceSource]:
    """雷达引擎：negotiation 初评 40 分（200 天前），其余三维近期充分证据。"""
    drill = FakeEvidenceSource(
        "exercise",
        seeds={
            TRAINEE: [
                Evidence(dim="negotiation", source="exercise", ref_id="d1", score=40.0,
                         occurred_at=NOW - timedelta(days=200)),
                Evidence(dim="product", source="exercise", ref_id="d2", score=55.0, occurred_at=NOW),
                Evidence(dim="communication", source="exercise", ref_id="d3", score=75.0, occurred_at=NOW),
                Evidence(dim="platform", source="exercise", ref_id="d4", score=88.0, occurred_at=NOW),
            ]
        },
    )
    exam = FakeEvidenceSource(
        "exam",
        seeds={
            TRAINEE: [
                Evidence(dim="product", source="exam", ref_id="e1", score=55.0, occurred_at=NOW),
                Evidence(dim="communication", source="exam", ref_id="e2", score=75.0, occurred_at=NOW),
                Evidence(dim="platform", source="exam", ref_id="e3", score=88.0, occurred_at=NOW),
            ]
        },
    )
    engine = RadarEngine(
        [drill, exam], InMemoryRadarSnapshotStore(), config=RadarConfig(min_evidence=1)
    )
    return engine, drill


@pytest.fixture()
def radar_client() -> TestClient:
    engine, _ = make_radar_engine()
    return TestClient(create_radar_app(engine))


def make_path_client(radar_store: InMemoryRadarSnapshotStore | None = None) -> tuple[TestClient, PathPlanner]:
    """路径应用装配：可选挂载雷达快照存储（重算链路依赖）。"""
    planner = make_planner(with_radar=False)
    if radar_store is not None:
        planner = PathPlanner(
            make_catalog(), planner.plan_store, planner.progress_store, planner.event_store,
            radar_store=radar_store,
        )
    return TestClient(create_path_app(planner)), planner


class TestPathApiEquivalence:
    """等价类：当前路径 / 重算 / 进度上报 主链路。"""

    def test_get_current_path_happy(self) -> None:
        """GET 当前路径：先生成后可查——分段视图、weak_dims、completion_pct=0.0、version=1。"""
        planner = make_planner(with_radar=True)
        client = TestClient(create_path_app(planner))
        replan = client.post(PATH_REPLAN_PATH.format(trainee_id=TRAINEE))
        assert replan.status_code == 200 and replan.json()["data"]["version"] == 1
        resp = client.get(PATH_VIEW_PATH.format(trainee_id=TRAINEE))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["weak_dims"] == ["negotiation", "product"]
        assert data["stats"]["completion_pct"] == 0.0
        sections = {item["section"] for item in data["items"]}
        assert sections == {"weak", "required", "elective"}
        assert any(item["locked"] for item in data["items"])  # L-req-2 前置锁定可见

    def test_post_replan_happy_version_kept(self) -> None:
        """POST 重算：版本 +1，data.kept_completed 报告保留已完成课时数（前端横幅文案）。"""
        planner = make_planner(with_radar=True)
        client = TestClient(create_path_app(planner))
        client.post(PATH_REPLAN_PATH.format(trainee_id=TRAINEE))
        client.post(
            PATH_PROGRESS_PATH.format(trainee_id=TRAINEE),
            json={"event_uid": "e1", "kind": "quiz_passed", "lesson_uid": "L-neg-1", "score": 90.0},
        )
        resp = client.post(PATH_REPLAN_PATH.format(trainee_id=TRAINEE))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["version"] == 2 and data["kept_completed"] == 1

    def test_post_progress_happy(self) -> None:
        """POST 进度：合法事件 → 200 返回 {status, completion_pct}，再次 GET 状态一致。"""
        planner = make_planner(with_radar=True)
        client = TestClient(create_path_app(planner))
        client.post(PATH_REPLAN_PATH.format(trainee_id=TRAINEE))
        resp = client.post(
            PATH_PROGRESS_PATH.format(trainee_id=TRAINEE),
            json={"event_uid": "e1", "kind": "lesson_finished", "lesson_uid": "L-req-1"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "completed" and data["replayed"] is False
        assert data["completion_pct"] == round(1 / 6, 4)


class TestPathApiBoundary:
    """边界：事件重放幂等（同响应体）。"""

    def test_post_progress_replay_idempotent(self) -> None:
        """同 event_uid 重复上报：两次响应一致（replayed=True），完成度不重复累计。"""
        planner = make_planner(with_radar=True)
        client = TestClient(create_path_app(planner))
        client.post(PATH_REPLAN_PATH.format(trainee_id=TRAINEE))
        payload = {"event_uid": "e1", "kind": "quiz_passed", "lesson_uid": "L-neg-1", "score": 90.0}
        first = client.post(PATH_PROGRESS_PATH.format(trainee_id=TRAINEE), json=payload).json()["data"]
        replay = client.post(PATH_PROGRESS_PATH.format(trainee_id=TRAINEE), json=payload).json()["data"]
        assert replay["replayed"] is True and first["replayed"] is False
        assert replay["status"] == first["status"] == "completed"
        assert replay["completion_pct"] == first["completion_pct"]


class TestPathApiException:
    """异常：业务码随信封 data.biz_code 下发（TR-3100~3104）。"""

    def test_get_current_path_not_found_tr3101(self) -> None:
        planner = make_planner()
        resp = TestClient(create_path_app(planner)).get(PATH_VIEW_PATH.format(trainee_id=TRAINEE))
        assert resp.status_code == 404 and resp.json()["data"] == {"biz_code": "TR-3101"}

    def test_post_replan_no_radar_tr3100(self) -> None:
        """无雷达快照（未完成诊断初评）→ 404 + TR-3100。"""
        client, _ = make_path_client()
        resp = client.post(PATH_REPLAN_PATH.format(trainee_id=TRAINEE))
        assert resp.status_code == 404 and resp.json()["data"] == {"biz_code": "TR-3100"}

    def test_post_progress_locked_tr3103(self) -> None:
        """前置课时未完成 → 409 + TR-3103（前置锁定闸门）。"""
        planner = make_planner(with_radar=True)
        client = TestClient(create_path_app(planner))
        client.post(PATH_REPLAN_PATH.format(trainee_id=TRAINEE))
        resp = client.post(
            PATH_PROGRESS_PATH.format(trainee_id=TRAINEE),
            json={"event_uid": "e1", "kind": "lesson_started", "lesson_uid": "L-req-2"},
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["code"] == 40902 and body["data"] == {"biz_code": "TR-3103"}

    def test_post_progress_unknown_lesson_tr3102(self) -> None:
        """课时不在当前路径 → 404 + TR-3102。"""
        planner = make_planner(with_radar=True)
        client = TestClient(create_path_app(planner))
        client.post(PATH_REPLAN_PATH.format(trainee_id=TRAINEE))
        resp = client.post(
            PATH_PROGRESS_PATH.format(trainee_id=TRAINEE),
            json={"event_uid": "e1", "kind": "lesson_finished", "lesson_uid": "L-ghost"},
        )
        assert resp.status_code == 404 and resp.json()["data"] == {"biz_code": "TR-3102"}

    def test_post_progress_invalid_kind_40001(self) -> None:
        """事件类型枚举外 → pydantic 拦截 → 40001 信封（data.errors 定位字段）。"""
        planner = make_planner(with_radar=True)
        client = TestClient(create_path_app(planner))
        client.post(PATH_REPLAN_PATH.format(trainee_id=TRAINEE))
        resp = client.post(
            PATH_PROGRESS_PATH.format(trainee_id=TRAINEE),
            json={"event_uid": "e1", "kind": "lesson_skipped", "lesson_uid": "L-req-1"},
        )
        assert resp.status_code == 400 and resp.json()["code"] == 40001


class TestPathApiEndToEnd:
    """端到端：两大链路一次贯通（雷达链路 × 路径链路，收口预演手验路径）。"""

    def test_e2e_radar_to_path_two_chains(self, radar_client: TestClient) -> None:
        """诊断初评 → 生成 v1（薄弱段置顶）→ 学习完成 → 评估更新 → v2 重算保留已完成。"""
        engine, drill = make_radar_engine()
        radar_store = engine.store
        radar = TestClient(create_radar_app(engine))
        path_client, _ = make_path_client(radar_store)

        # ① 雷达链路：入职初评（trigger=initial）
        resp = radar.post(RADAR_COMPUTE_PATH.format(trainee_id=TRAINEE), json={"trigger": "initial"})
        assert resp.status_code == 200
        view = radar.get(RADAR_GET_PATH.format(trainee_id=TRAINEE)).json()["data"]
        assert view["radar"]["negotiation"]["score"] == 40.0
        assert view["baseline"]["trigger"] == "initial"

        # ② 路径链路：由初评快照生成 v1，薄弱维 = 议价能力/产品知识
        v1 = path_client.post(PATH_REPLAN_PATH.format(trainee_id=TRAINEE)).json()["data"]
        assert v1["version"] == 1 and v1["weak_dims"] == ["negotiation", "product"]

        # ③ 学员学习：议价突破课测验通过
        progress = path_client.post(
            PATH_PROGRESS_PATH.format(trainee_id=TRAINEE),
            json={"event_uid": "e1", "kind": "quiz_passed", "lesson_uid": "L-neg-1", "score": 90.0},
        ).json()["data"]
        assert progress["status"] == "completed" and progress["completion_pct"] == round(1 / 6, 4)

        # ④ 雷达链路：议价能力新证据（90 分）→ 重算，薄弱维退位
        drill.add(
            TRAINEE,
            Evidence(dim="negotiation", source="exercise", ref_id="d9", score=90.0, occurred_at=NOW),
        )
        radar.post(RADAR_COMPUTE_PATH.format(trainee_id=TRAINEE), json={"trigger": "event"})
        radar_now = radar.get(RADAR_GET_PATH.format(trainee_id=TRAINEE)).json()["data"]["radar"]
        assert radar_now["negotiation"]["score"] == 89.5

        # ⑤ 路径链路：评估更新后 v2 重算——已完成课时保留、薄弱维换位
        v2 = path_client.post(PATH_REPLAN_PATH.format(trainee_id=TRAINEE)).json()["data"]
        assert v2["version"] == 2 and v2["weak_dims"] == ["product", "communication"]
        assert v2["kept_completed"] == 1
        status_of = {item["lesson_uid"]: item["status"] for item in v2["items"]}
        assert status_of["L-neg-1"] == "completed"  # 已完成保留（纵使移出薄弱段）
        assert status_of["L-pro-1"] == "not_started"


__all__ = ["NOW", "make_radar_engine", "make_path_client"]
