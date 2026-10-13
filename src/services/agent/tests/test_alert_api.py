# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""告警 API 单元测试（FR-AG-007，SR-AG-007 §5；迭代 2.3 TDD 阶段①用例）。

覆盖（FastAPI 端点契约）：
- POST /api/v1/agent/alerts（内部上报：默认分级 + 路由结果回显）
- GET  /api/v1/agent/alerts（status/level/type 过滤）
- POST /api/v1/agent/alerts/{uid}/ack | /resolve（闭环状态机）
- 大客户 A 级强制 high；错误码 40401 不存在 / 40901 非法迁移。

实现方案：docs/07-实现方案/AG/SR-AG-007-告警通知.md §5、§9
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from services.agent.api.alert_api import create_alert_app
from services.agent.core.alert.router import AlertRouter, FakeChannel

NOW = datetime(2026, 10, 8, 10, 0, 0)


@pytest.fixture()
def client():
    """标准装配：全 Fake 渠道。"""
    router = AlertRouter(
        channels={"dingtalk_robot": FakeChannel("dingtalk_robot"),
                  "wechat_app": FakeChannel("wechat_app"),
                  "dingtalk_group": FakeChannel("dingtalk_group"),
                  "backend_todo": FakeChannel("backend_todo"),
                  "supervisor": FakeChannel("supervisor")},
        fallback_channel=FakeChannel("sms"),
        clock=lambda: NOW,
    )
    return TestClient(create_alert_app(router=router)), router


class TestAlertEndpoints:
    """告警上报/查询/处置端点。"""

    def test_submit_returns_routing(self, client) -> None:
        """上报返回 alert_uid/aggregated/level/channels（路由结果回显）。"""
        api, _ = client
        resp = api.post("/api/v1/agent/alerts", json={
            "type": "risk_hit", "title": "红线拦截", "body": "命中 RED_OFFSITE_INDUCE",
            "dedup_key": "risk:acc1",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["level"] == "high"
        assert body["aggregated"] is False
        assert set(body["channels"]) == {"dingtalk_robot", "wechat_app"}
        assert body["alert_uid"]

    def test_list_filters(self, client) -> None:
        """列表过滤：status/level/type 组合。"""
        api, _ = client
        api.post("/api/v1/agent/alerts", json={"type": "risk_hit", "title": "a", "body": "b",
                                               "dedup_key": "k1"})
        api.post("/api/v1/agent/alerts", json={"type": "system_error", "title": "c", "body": "d",
                                               "dedup_key": "k2"})
        resp = api.get("/api/v1/agent/alerts", params={"type": "system_error"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1 and items[0]["type"] == "system_error"
        firing = api.get("/api/v1/agent/alerts", params={"status": "firing"}).json()["items"]
        assert len(firing) == 2

    def test_ack_then_resolve(self, client) -> None:
        """ack→resolve 闭环：状态回显操作人。"""
        api, _ = client
        uid = api.post("/api/v1/agent/alerts", json={
            "type": "risk_hit", "title": "t", "body": "b", "dedup_key": "k"}).json()["alert_uid"]
        acked = api.post(f"/api/v1/agent/alerts/{uid}/ack", json={"operator": "sale-07"})
        assert acked.status_code == 200
        assert acked.json()["status"] == "acknowledged"
        resolved = api.post(f"/api/v1/agent/alerts/{uid}/resolve", json={"operator": "sale-07"})
        assert resolved.status_code == 200
        assert resolved.json()["status"] == "resolved"

    def test_resolve_without_ack_conflict(self, client) -> None:
        """未确认直接 resolve → 409 + 40901（非法迁移）。"""
        api, _ = client
        uid = api.post("/api/v1/agent/alerts", json={
            "type": "risk_hit", "title": "t", "body": "b", "dedup_key": "k"}).json()["alert_uid"]
        resp = api.post(f"/api/v1/agent/alerts/{uid}/resolve", json={"operator": "x"})
        assert resp.status_code == 409
        assert resp.json()["code"] == 40901

    def test_unknown_uid_404(self, client) -> None:
        """未知告警 uid → 404 + 40401。"""
        api, _ = client
        resp = api.post("/api/v1/agent/alerts/no-such/ack", json={"operator": "x"})
        assert resp.status_code == 404
        assert resp.json()["code"] == 40401

    def test_big_customer_grade_a_high(self, client) -> None:
        """大客户 A 级：复核通过强制 high 双通道。"""
        api, router = client
        resp = api.post("/api/v1/agent/alerts", json={
            "type": "big_customer", "title": "A 级询盘", "body": "b",
            "customer_grade": "A", "dedup_key": "big"})
        assert resp.status_code == 200
        assert resp.json()["level"] == "high"
        alert = router.get(resp.json()["alert_uid"])
        assert len(alert.deliveries) == 2
