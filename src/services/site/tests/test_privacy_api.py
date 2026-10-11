# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""隐私合规 API 用例（FR-SITE-007：/api/v1/privacy/consent + DSR delete/export/get）。"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from services.site.api.app import create_site_app

from .conftest import DEMO_KEY, DEMO_ORIGIN


def build_client(**kw: object) -> tuple[TestClient, str]:
    """装配应用 + 会话，返回 (client, visitor_token)。"""
    client = TestClient(create_site_app(**kw))
    init = client.post(
        "/api/v1/site/session", json={"key": DEMO_KEY}, headers={"Origin": DEMO_ORIGIN}
    ).json()["data"]
    return client, init["visitor_token"]


def consent(client: TestClient, token: str, status: str) -> Any:
    """consent 上报辅助（Bearer visitor token，SR-SITE-007 §5①）。"""
    return client.post(
        "/api/v1/privacy/consent",
        json={"status": status, "sdk_version": "1.0.0"},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_consent_endpoint_records_and_validates() -> None:
    """PA1+PA2 consent 上报：合法三态落证 recorded=true；非法状态 → 40001（等价+异常）。"""
    client, token = build_client()
    ok_resp = consent(client, token, "granted")
    assert ok_resp.status_code == 200
    body = ok_resp.json()
    assert body["code"] == 0 and body["data"]["recorded"] is True
    bad = consent(client, token, "sure-why-not")
    assert bad.status_code == 400 and bad.json()["code"] == 40001


def test_consent_endpoint_bad_token_40101() -> None:
    """PA3 无/坏 token → 40101（异常类）。"""
    client, _ = build_client()
    resp = client.post(
        "/api/v1/privacy/consent", json={"status": "granted"},
        headers={"Authorization": "Bearer vt_bogus.sig"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_consent_denied_blocks_events_after_granted() -> None:
    """PA4 门控联动：granted 后撤回为 denied → /site/events 40302（C2 拒绝有效性端到端）。"""
    client, token = build_client()
    assert consent(client, token, "granted").json()["data"]["recorded"] is True
    ok = client.post("/api/v1/site/events", json={
        "visitor_token": token, "consent": True,
        "events": [{"ev_id": "e1", "type": "page_view", "page": "/"}]})
    assert ok.json()["data"]["accepted"] == 1
    assert consent(client, token, "denied").json()["data"]["recorded"] is True
    blocked = client.post("/api/v1/site/events", json={
        "visitor_token": token, "consent": True,
        "events": [{"ev_id": "e2", "type": "page_view", "page": "/"}]})
    assert blocked.status_code == 403 and blocked.json()["code"] == 40302


def test_dsr_delete_endpoint_creates_ticket() -> None:
    """PA5 DSR 删除工单：ticket_id + due_at（创建+30 天）；空邮箱 → 40001（等价+异常）。"""
    client, _ = build_client()
    resp = client.post("/api/v1/privacy/dsr/delete", json={"email": "buyer@x.com"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ticket_id"].startswith("DSR-") and data["req_type"] == "delete"
    assert data["due_at"] > data["created_at"]
    bad = client.post("/api/v1/privacy/dsr/delete", json={"email": ""})
    assert bad.status_code == 400 and bad.json()["code"] == 40001


def test_dsr_export_and_get_completed() -> None:
    """PA6 导出工单 + GET 查询：执行后 completed + result 带导出段（等价类）。"""
    client, _ = build_client()
    ticket = client.post("/api/v1/privacy/dsr/export", json={"email": "buyer@x.com"}).json()["data"]
    done = client.post(f"/api/v1/privacy/dsr/{ticket['ticket_id']}/execute")
    assert done.json()["data"]["status"] == "completed"
    got = client.get(f"/api/v1/privacy/dsr/{ticket['ticket_id']}").json()["data"]
    assert got["status"] == "completed"
    assert "export" in got["result"]


def test_dsr_get_unknown_40401() -> None:
    """PA7 GET 未知工单 → 40401 信封（异常类）。"""
    client, _ = build_client()
    resp = client.get("/api/v1/privacy/dsr/DSR-2099-9999")
    assert resp.status_code == 404
    assert resp.json()["code"] == 40401
