# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""主管看板 API 集成用例（FR-TR-006，SR-TR-006 §5/§9；TDD 阶段①，TestClient 通道，迭代 1.3）。

端点：GET /api/v1/training/board/overview、GET .../board/trainee/{id}、GET .../board/flags。
验收口径映射：统一信封四键（04 §1.3）；越权 40301 + 审计；主管仅本团队数据；
识别标记 evidence 抽检（SRS §16.2「识别规则可解释」）。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.training.api.board_api import (
    DETAIL_PATH,
    FLAGS_PATH,
    OVERVIEW_PATH,
    create_board_app,
)
from services.training.core.board.aggregate import (
    DailyStatsStore,
    DrillEvent,
    ExamEvent,
    FlagStore,
    FlagEvaluator,
    LearningEvent,
    RadarSnapshot,
    TeamStatsAggregator,
    TraineeProfile,
)

from .test_board_fakes import AuditRecorder, FakeBoardSource, FakeTeamDirectory, envelope_ok

TODAY = date(2026, 10, 10)
TEAM1 = {101, 102}
RADAR_101 = {"product": 75.0, "communication": 70.0, "negotiation": 65.0, "platform": 72.0}


def supervisor_claims(role: str = "supervisor", teams: list[int] | None = None) -> Any:
    """请求级 claims 注入（生产由全局鉴权中间件解析 JWT）。"""
    return lambda request: {
        "sub": "sup-a",
        "roles": [role],
        "data_scope": {"team_ids": [1] if teams is None else teams},
    }


def build_world() -> tuple[FakeBoardSource, DailyStatsStore, FlagStore, AuditRecorder]:
    """装配看板数据：101 活跃（今日有学习/演练/考试/雷达），102 停滞 5 天（NI-1 命中）。"""
    source = FakeBoardSource(
        [
            TraineeProfile(trainee_id=101, name="张三", cert_status="certified", path_pct=80.0),
            TraineeProfile(trainee_id=102, name="李四", cert_status="uncertified", path_pct=10.0),
            TraineeProfile(trainee_id=201, name="赵六", cert_status="uncertified", path_pct=5.0),
            TraineeProfile(trainee_id=202, name="钱七", cert_status="uncertified", path_pct=5.0),
        ],
        learning={101: [LearningEvent(stat_date=TODAY, minutes=60, lessons_done=2, path_pct=80.0)],
                  102: [LearningEvent(stat_date=TODAY - timedelta(days=o), minutes=0,
                                      lessons_done=0, path_pct=10.0) for o in range(5)]},
        drills={101: [DrillEvent(stat_date=TODAY, score=85.0)]},
        exams={101: [ExamEvent(stat_date=TODAY, passed=True)]},
        radars={101: [RadarSnapshot(stat_date=TODAY, radar=RADAR_101)]},
    )
    store = DailyStatsStore()
    TeamStatsAggregator(source, store).aggregate_range(TODAY - timedelta(days=4), TODAY)
    flag_store = FlagStore()
    FlagEvaluator(source, store, flag_store).evaluate_all([101, 102], today=TODAY, window_days=30,
                                                          now="2026-10-10T02:00:00+08:00")
    audit = AuditRecorder()
    return source, store, flag_store, audit


@pytest.fixture()
def client() -> TestClient:
    """主管 A（团队 1）视角的看板应用。"""
    source, store, flag_store, audit = build_world()
    app = create_board_app(
        source=source, store=store, flag_store=flag_store,
        directory=FakeTeamDirectory({1: TEAM1, 2: {201, 202}}),
        audit_cb=audit, claims_provider=supervisor_claims(), today_fn=lambda: TODAY,
    )
    return TestClient(app)


def test_overview_envelope_and_counts(client: TestClient) -> None:
    """团队总览：信封四键 + 人数/认证数/雷达均值/X-Trace-Id 透传。"""
    resp = client.get(OVERVIEW_PATH, headers={"X-Trace-Id": "trace-ov-1"})
    assert resp.status_code == 200
    body = resp.json()
    envelope_ok(body)
    assert body["trace_id"] == "trace-ov-1"
    data = body["data"]
    assert data["trainee_total"] == 2  # 仅本团队（team 1）
    assert data["certified_cnt"] == 1
    assert data["avg_path_pct"] == 45.0  # (80 + 10) / 2
    assert data["active_today"] == 1  # 仅 101 今日有学习/演练事件
    assert data["radar_avg"]["product"] == 75.0  # 仅 101 有雷达 → 即其值
    assert set(data["radar_avg"]) == {"product", "communication", "negotiation", "platform"}


def test_overview_flag_counts(client: TestClient) -> None:
    """总览标记计数：102 停滞 5 天命中 NI-1 → 需干预 1、高潜力 0。"""
    data = client.get(OVERVIEW_PATH).json()["data"]
    assert data["need_intervention_cnt"] == 1
    assert data["high_potential_cnt"] == 0


def test_detail_in_scope_with_evidence(client: TestClient) -> None:
    """个人详情：进度/认证/雷达/标记字段齐全；evidence 结构可解释。"""
    body = client.get(DETAIL_PATH.format(trainee_id=101)).json()
    envelope_ok(body)
    detail = body["data"]
    assert detail["trainee_id"] == 101
    assert detail["name"] == "张三"
    assert detail["cert_status"] == "certified"
    assert detail["path_pct"] == 80.0
    assert detail["radar_current"] == RADAR_101
    assert isinstance(detail["flags"], list)


def test_detail_out_of_scope_40301_with_audit() -> None:
    """越权访问他团队学员 → HTTP 403 + code 40301 + 审计 rbac_deny（主管数据边界验收口径）。"""
    source, store, flag_store, audit = build_world()
    app = create_board_app(
        source=source, store=store, flag_store=flag_store,
        directory=FakeTeamDirectory({1: TEAM1, 2: {201, 202}}),
        audit_cb=audit, claims_provider=supervisor_claims(), today_fn=lambda: TODAY,
    )
    resp = TestClient(app).get(DETAIL_PATH.format(trainee_id=201))
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == 40301
    assert "201" in body["message"] or "无权" in body["message"]
    assert audit.of_type("rbac_deny"), "越权必须写审计留痕（NFR-SEC-03）"


def test_detail_unknown_trainee_40404(client: TestClient) -> None:
    """学员不存在 → 40404（区别于越权 40301）。"""
    resp = client.get(DETAIL_PATH.format(trainee_id=424242))
    assert resp.status_code == 404
    assert resp.json()["code"] == 40404


def test_flags_filter_and_scope(client: TestClient) -> None:
    """标记名单：flag_type 过滤生效；名单仅含本团队学员（scope 过滤）。"""
    body = client.get(FLAGS_PATH).json()
    envelope_ok(body)
    items = body["data"]["items"]
    assert {item["trainee_id"] for item in items} <= TEAM1  # 他团队 201/202 不出现
    only_ni = client.get(FLAGS_PATH, params={"flag_type": "need_intervention"}).json()["data"]["items"]
    assert all(item["flag_type"] == "need_intervention" for item in only_ni)
    for item in items:  # 可解释口径：每条标记携带规则编码与证据
        assert item["rule_key"]
        assert isinstance(item["evidence"], dict)


def test_endpoint_requires_supervisor_role() -> None:
    """非主管/管理员角色访问看板 → 40301（角色无权）。"""
    source, store, flag_store, audit = build_world()
    app = create_board_app(
        source=source, store=store, flag_store=flag_store,
        directory=FakeTeamDirectory({1: TEAM1}),
        audit_cb=audit, claims_provider=supervisor_claims(role="trainee"), today_fn=lambda: TODAY,
    )
    resp = TestClient(app).get(OVERVIEW_PATH)
    assert resp.status_code == 403
    assert resp.json()["code"] == 40301
