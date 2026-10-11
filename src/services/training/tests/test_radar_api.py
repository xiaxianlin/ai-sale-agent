# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""能力雷达 API 集成测试（FR-TR-004，SR-TR-004 §5 接口增量；TDD 阶段①用例设计，TestClient 通道）。

端点（统一信封 code/message/data/trace_id，04-接口设计 §1.3 口径）：
- GET  /api/v1/training/radar/{trainee_id}          当前四维 + 初评基线 + 趋势序列；
- POST /api/v1/training/radar/{trainee_id}/compute  手动重算（trigger=manual，SR §5 recalc 形态）；
- POST /api/v1/training/radar/{trainee_id}/calibrate 一致性校准（主管盲评对比 → 一致率 +
  偏差调整建议，SR §6.3；KPI ≥85%，SRS §16.2）。

用例分组（开发规范 §9.1 五类场景）：
- 等价类：GET 主链路（radar/baseline/trend 结构）、compute 落快照后可查、
  calibrate 出报表（一致率/kpi_passed）；
- 边界：证据不足维 score=null 透传、X-Trace-Id 头回显；
- 异常：无任何快照 → 404 + biz_code=TR-3001、校准样本维度残缺/空样本 → 40001；
- 并发/降级由引擎层测试覆盖（API 层为薄封装）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.training.api.radar_api import (
    RADAR_CALIBRATE_PATH,
    RADAR_COMPUTE_PATH,
    RADAR_GET_PATH,
    create_radar_app,
)
from services.training.core.radar.engine import (
    DIMENSIONS,
    Evidence,
    FakeEvidenceSource,
    InMemoryRadarSnapshotStore,
    RadarConfig,
    RadarEngine,
)

NOW = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)
TRAINEE = 1024


def ev(dim: str, source: str, score: float, at: datetime, ref: str = "r1") -> Evidence:
    return Evidence(dim=dim, source=source, ref_id=ref, score=score, occurred_at=at)


def make_engine(trainee: int = TRAINEE) -> tuple[RadarEngine, FakeEvidenceSource]:
    """带两组种子的引擎：全维充分证据学员 + 仅单条 product 证据学员（证据不足对照）。"""
    drill = FakeEvidenceSource(
        "exercise",
        seeds={
            trainee: [ev(d, "exercise", s, NOW - timedelta(days=1)) for d, s in
                      (("product", 80), ("communication", 75), ("negotiation", 40), ("platform", 88))],
            2048: [ev("product", "exercise", 80, NOW)],
        },
    )
    exam = FakeEvidenceSource(
        "exam",
        seeds={trainee: [ev(d, "exam", s, NOW - timedelta(days=1)) for d, s in
                         (("product", 85), ("communication", 70), ("negotiation", 45), ("platform", 82))]},
    )
    store = InMemoryRadarSnapshotStore()
    engine = RadarEngine([drill, exam], store, config=RadarConfig(min_evidence=2))
    return engine, drill


@pytest.fixture()
def client() -> TestClient:
    """标准装配：已完成 initial + event 两次快照的引擎（查询/校准基座）。"""
    engine, _ = make_engine()
    engine.compute(TRAINEE, trigger="initial", now=NOW - timedelta(days=5))
    engine.compute(TRAINEE, trigger="event", now=NOW)
    return TestClient(create_radar_app(engine))


class TestRadarApiEquivalence:
    """等价类：查询/重算/校准三端点主链路。"""

    def test_get_radar_happy_envelope(self, client: TestClient) -> None:
        """GET 雷达：信封四键齐全；radar 为 {dim:{score,n}}；baseline=initial；trend 升序。"""
        resp = client.get(RADAR_GET_PATH.format(trainee_id=TRAINEE))
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"code", "message", "data", "trace_id"} and body["code"] == 0
        data = body["data"]
        assert data["radar"]["negotiation"] == {"score": 41.5, "n": 2}  # (0.60*40+0.25*45)/0.85
        assert data["baseline"]["trigger"] == "initial"
        assert [row["trigger"] for row in data["trend"]] == ["initial", "event"]
        assert all(set(row["scores"]) == set(DIMENSIONS) for row in data["trend"])

    def test_post_compute_manual_then_visible(self) -> None:
        """POST 重算：trigger=manual 落新快照并返回 snapshot_id；GET 立即可见。"""
        engine, _ = make_engine()
        engine.compute(TRAINEE, trigger="initial", now=NOW - timedelta(days=5))
        client = TestClient(create_radar_app(engine))
        resp = client.post(RADAR_COMPUTE_PATH.format(trainee_id=TRAINEE), json={"trigger": "manual"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["trigger"] == "manual" and data["snapshot_id"]
        latest = client.get(RADAR_GET_PATH.format(trainee_id=TRAINEE)).json()["data"]
        assert latest["radar"]["product"]["score"] == 82.8  # (0.35*80+0.45*85)/0.80，同龄衰减同比消去
        assert [row["trigger"] for row in latest["trend"]] == ["initial", "manual"]

    def test_post_calibrate_happy_kpi_pass(self, client: TestClient) -> None:
        """POST 校准：主管分与 AI 分一致 → 一致率 100%、kpi_passed=True、建议维持现状。"""
        payload = {
            "pairs": [
                {
                    "pair_id": f"P{i}",
                    "ai_scores": {
                        "product": 80.0, "communication": 70.0, "negotiation": 60.0, "platform": 90.0
                    },
                    "supervisor_scores": {
                        "product": 80.0 + delta, "communication": 70.0, "negotiation": 60.0, "platform": 90.0
                    },
                }
                for i, delta in enumerate((0.0, 2.0))
            ]
        }
        resp = client.post(RADAR_CALIBRATE_PATH.format(trainee_id=TRAINEE), json=payload)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["samples"] == 2 and data["consistent"] == 2
        assert data["consistency_rate"] == 100.0 and data["kpi_passed"] is True
        assert data["dim_tolerance"] == 15 and data["total_tolerance"] == 10
        assert any("维持当前权重" in s for s in data["suggestions"])


class TestRadarApiBoundary:
    """边界：null 维透传与 trace_id 回显。"""

    def test_get_radar_insufficient_dim_null(self) -> None:
        """证据不足维（n<min_evidence）→ score=null 原样透传，不无声置 0。"""
        engine, _ = make_engine()
        engine.compute(2048, trigger="event", now=NOW)
        resp = TestClient(create_radar_app(engine)).get(RADAR_GET_PATH.format(trainee_id=2048))
        radar = resp.json()["data"]["radar"]
        assert radar["product"] == {"score": None, "n": 1}
        assert radar["communication"]["score"] is None

    def test_get_radar_trace_id_echo(self, client: TestClient) -> None:
        """X-Trace-Id 头优先回显（04-接口设计 §1.4），贯穿链路追踪。"""
        resp = client.get(
            RADAR_GET_PATH.format(trainee_id=TRAINEE), headers={"X-Trace-Id": "trace-radar-001"}
        )
        assert resp.json()["trace_id"] == "trace-radar-001"


class TestRadarApiException:
    """异常：资源缺失与入参非法的信封映射。"""

    def test_get_radar_not_found_tr3001(self) -> None:
        """无任何快照（未完成诊断初评）→ 404 + data.biz_code=TR-3001。"""
        engine, _ = make_engine()
        resp = TestClient(create_radar_app(engine)).get(RADAR_GET_PATH.format(trainee_id=TRAINEE))
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == 40404 and body["data"] == {"biz_code": "TR-3001"}
        assert "TR-3001" in body["message"]

    def test_post_calibrate_missing_dim_40001(self, client: TestClient) -> None:
        """校准样本维度残缺 → 40001 信封（核心 ValueError 映射）。"""
        payload = {
            "pairs": [
                {
                    "pair_id": "P1",
                    "ai_scores": {d: 80.0 for d in DIMENSIONS},
                    "supervisor_scores": {d: 80.0 for d in DIMENSIONS if d != "platform"},
                }
            ]
        }
        resp = client.post(RADAR_CALIBRATE_PATH.format(trainee_id=TRAINEE), json=payload)
        assert resp.status_code == 400 and resp.json()["code"] == 40001

    def test_post_calibrate_empty_pairs_40001(self, client: TestClient) -> None:
        """空样本清单 → pydantic min_length 拦截 → 40001 信封（data.errors 定位字段）。"""
        resp = client.post(RADAR_CALIBRATE_PATH.format(trainee_id=TRAINEE), json={"pairs": []})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 40001 and body["data"]["errors"]


__all__ = ["NOW", "TRAINEE", "make_engine"]
