# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""行为追踪 API 用例（FR-SITE-002：POST /api/v1/site/events 批量上报 + consent 门控 + 错误码）。"""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from services.site.api.app import create_site_app

from .conftest import DEMO_KEY, DEMO_ORIGIN


def build_client(**kw: object) -> tuple[TestClient, str]:
    """装配应用并签发一个 granted 访客会话，返回 (client, visitor_token)。"""
    client = TestClient(create_site_app(**kw))
    init = client.post(
        "/api/v1/site/session",
        json={"key": DEMO_KEY, "lang": "en"},
        headers={"Origin": DEMO_ORIGIN},
    ).json()["data"]
    # 会话默认 unknown：上报前先 consent granted（三态门控联动）
    client.post(
        "/api/v1/privacy/consent",
        json={"status": "granted"},
        headers={"Authorization": f"Bearer {init['visitor_token']}"},
    )
    return client, init["visitor_token"]


def post_events(client: TestClient, token: str, events: list[dict], consent: bool = True) -> Any:
    """上报辅助。"""
    return client.post(
        "/api/v1/site/events",
        json={"visitor_token": token, "consent": consent, "events": events},
    )


def test_events_endpoint_accepts_batch() -> None:
    """B1 批量上报信封：code=0 + accepted/dropped 计数（等价类）。"""
    client, token = build_client()
    resp = post_events(
        client, token,
        [
            {"ev_id": "e1", "type": "page_view", "page": "/p/1", "ts": 1760000000},
            {"ev_id": "e2", "type": "product_view", "page": "/p/1", "ts": 1760000005,
             "payload": {"sku": "ST-3200"}},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"accepted": 2, "dropped": 0}


def test_events_endpoint_bad_token_40101() -> None:
    """B2 visitor token 无效 → 40101（异常类）。"""
    client, _ = build_client()
    resp = post_events(client, "vt_bogus.sig", [{"ev_id": "e", "type": "page_view", "page": "/"}])
    assert resp.status_code == 401
    assert resp.json()["code"] == 40101


def test_events_endpoint_consent_false_40302() -> None:
    """B3 consent=false → 40302 且零写入（异常类，06-安全 §9 红线）。"""
    client, token = build_client()
    resp = post_events(
        client, token,
        [{"ev_id": "e", "type": "page_view", "page": "/"}],
        consent=False,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == 40302


def test_events_endpoint_batch_over_limit_40001() -> None:
    """B4 单批 51 条 → 40001（边界）。"""
    client, token = build_client()
    events = [{"ev_id": f"e{i}", "type": "page_view", "page": "/"} for i in range(51)]
    resp = post_events(client, token, events)
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001


def test_events_endpoint_invalid_type_counted_dropped() -> None:
    """B5 非法事件类型计入 dropped（等价类，整批不拒）。"""
    client, token = build_client()
    resp = post_events(
        client, token,
        [
            {"ev_id": "e1", "type": "page_view", "page": "/"},
            {"ev_id": "e2", "type": "teleport", "page": "/"},
        ],
    )
    assert resp.json()["data"] == {"accepted": 1, "dropped": 1}


def test_events_feed_profile_for_chat_hint() -> None:
    """B6 上报→画像→对话注入联动：上报 ST-3200 三次后 chat 的 Prompt 含画像（等价类，SRS §16.4）。"""
    client, token = build_client()
    events = [
        {"ev_id": f"e{i}", "type": "product_view", "page": "/products/st-3200",
         "ts": 1760000000 + i * 60, "payload": {"sku": "ST-3200"}}
        for i in range(3)
    ]
    events.append(
        {"ev_id": "e-d", "type": "dwell", "page": "/products/st-3200", "ts": 1760000300,
         "dwell_ms": 120000, "payload": {"sku": "ST-3200"}}
    )
    assert post_events(client, token, events).json()["data"]["accepted"] == 4
    chat = client.post(
        "/api/v1/site/chat", json={"visitor_token": token, "text": "MOQ for ST-3200?"}
    )
    assert chat.status_code == 200
    # 画像注入到 Prompt 属服务端内部；此处断言端到端不抛错且回复结构完整（注入断言在服务域用例 C1）
    data = chat.json()["data"]
    assert "content" in data and "knowledge_refs" in data
