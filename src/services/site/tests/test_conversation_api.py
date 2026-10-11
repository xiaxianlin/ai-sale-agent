# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""对话引擎 API 用例（FR-SITE-003：POST /site/chat、/site/invite/check、/site/leads）。"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from services.site.api.app import create_site_app

from .conftest import DEMO_KEY, DEMO_ORIGIN

CHAT = "/api/v1/site/chat"
INVITE_CHECK = "/api/v1/site/invite/check"
LEADS = "/api/v1/site/leads"


def build_client(**kw: object) -> tuple[TestClient, str]:
    """装配应用 + granted 会话，返回 (client, visitor_token)。"""
    client = TestClient(create_site_app(**kw))
    init = client.post(
        "/api/v1/site/session", json={"key": DEMO_KEY}, headers={"Origin": DEMO_ORIGIN}
    ).json()["data"]
    client.post(
        "/api/v1/privacy/consent", json={"status": "granted"},
        headers={"Authorization": f"Bearer {init['visitor_token']}"},
    )
    return client, init["visitor_token"]


def test_chat_endpoint_envelope_and_bad_token() -> None:
    """CA1+CA2 聊天端点：code=0 信封 + content/refs/fallback 结构；坏 token → 40101（等价+异常）。"""
    client, token = build_client()
    resp = client.post(CHAT, json={"visitor_token": token, "text": "MOQ for ST-3200?", "round_no": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0 and body["trace_id"]
    data: dict[str, Any] = body["data"]
    assert data["role"] == "ai" and data["msg_id"]
    assert "content" in data and "knowledge_refs" in data and "fallback" in data
    bad = client.post(CHAT, json={"visitor_token": "vt_bogus.sig", "text": "hi"})
    assert bad.status_code == 401 and bad.json()["code"] == 40101


def test_invite_check_endpoint_triggers() -> None:
    """CA3 邀聊检查端点：停留达阈值 → triggered=true + invite 载荷（等价类）。"""
    client, token = build_client()
    resp = client.post(INVITE_CHECK, json={
        "visitor_token": token,
        "event": {"type": "dwell", "page": "/products/st-3200", "dwell_ms": 45000},
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["triggered"] is True and data["trigger_type"] == "dwell"
    assert data["invite"]["invite_id"]


def test_leads_endpoint_invalid_returns_field_errors() -> None:
    """CA4 留资非法：code=0 业务回执 ok=false + 字段错误（lead_ack 形态，异常类）。"""
    client, token = build_client()
    resp = client.post(LEADS, json={"visitor_token": token, "email": "bad"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["ok"] is False
    assert "email" in body["data"]["errors"]


def test_leads_endpoint_valid_records() -> None:
    """CA5 留资合法：ok=true + customer_id + lead_id（等价类）。"""
    client, token = build_client()
    resp = client.post(LEADS, json={"visitor_token": token, "email": "buyer@x.com"})
    data = resp.json()["data"]
    assert data["ok"] is True and data["customer_id"] > 0 and data["lead_id"]
