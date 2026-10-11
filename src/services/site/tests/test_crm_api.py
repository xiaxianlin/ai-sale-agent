# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-005 CRM 集成 API 通道用例（SR-SITE-005 §5/§6.5；TDD 阶段①②，TestClient）。

端点：POST .../crm/leads（归一化入口）、POST .../crm/merge（confirm 预检/执行）、
POST .../crm/merge/{id}/undo、GET .../crm/customers/{id}/timeline（客户 360）。
RBAC：crm_operator 之外 40301；业务冲突 40901；统一信封四键。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from services.site.api.crm_api import (
    CRM_LEADS_PATH,
    CRM_MERGE_PATH,
    create_crm_app,
)
from services.site.core.crm.identity import CRMStore, IdentityResolver, LeadNormalizer
from services.site.core.crm.merge import CustomerMerger, TimelineSource

from .fakes import MutableClock, T0

OPERATOR = {"X-User-Id": "op-1", "X-User-Roles": "crm_operator"}


def make_client():
    """装配 CRM API 应用并预置一个 site 渠道客户。"""
    clock = MutableClock()
    store = CRMStore()
    resolver = IdentityResolver(store)
    normalizer = LeadNormalizer(store, resolver, clock=clock.now)
    merger = CustomerMerger(store, clock=clock.now)
    app = create_crm_app(normalizer=normalizer, merger=merger, store=store, clock=clock.now)
    client = TestClient(app)
    r = client.post(CRM_LEADS_PATH, json={"channel": "site", "visitor_uid": "v-1",
                                          "email": "bob@corp.com", "name": "Bob",
                                          "company": "Acme", "country": "US"},
                    headers=OPERATOR)
    assert r.json()["code"] == 0
    return client, store, r.json()["data"]["customer_id"]


class TestCrmApi:
    """API 通道。"""

    def test_post_lead_creates_customer(self) -> None:
        """POST leads：归一化入口建档 → customer_id + action=create + level=5。"""
        client, store, _ = make_client()
        r = client.post(CRM_LEADS_PATH, json={"channel": "alibaba", "buyer_login_id": "b-1",
                                              "email": "alice@x.com", "buyer_name": "Alice"},
                        headers=OPERATOR)
        data = r.json()["data"]
        assert r.json()["code"] == 0
        assert data["action"] == "create" and data["level"] == 5 and data["customer_id"]

    def test_post_lead_invalid_channel_40001(self) -> None:
        """未注册渠道（weixin）→ 40001。"""
        client, *_ = make_client()
        r = client.post(CRM_LEADS_PATH, json={"channel": "weixin", "external_id": "x"}, headers=OPERATOR)
        assert r.status_code == 400 and r.json()["code"] == 40001

    def test_merge_confirm_executes(self) -> None:
        """POST merge confirm=true：执行合并，返回 merge_id/survivor/undo_deadline。"""
        client, store, cid = make_client()
        other = client.post(CRM_LEADS_PATH, json={"channel": "alibaba", "buyer_login_id": "b-2",
                                                  "email": "alice@x.com"},
                            headers=OPERATOR).json()["data"]["customer_id"]
        r = client.post(CRM_MERGE_PATH, json={"source_id": other, "target_id": cid, "confirm": True},
                        headers=OPERATOR)
        assert r.json()["code"] == 0
        data = r.json()["data"]
        assert data["survivor_id"] == cid and data["merge_id"] and data["undo_deadline"]
        assert store.customers[other].merged_into == cid

    def test_merge_conflict_envelope_40901(self) -> None:
        """已合并 source 再合并 → HTTP 409 + 40901 信封。"""
        client, store, cid = make_client()
        a = client.post(CRM_LEADS_PATH, json={"channel": "alibaba", "buyer_login_id": "b-4",
                                              "email": "a4@x.com"}, headers=OPERATOR)
        a = a.json()["data"]["customer_id"]
        merge_body = {"source_id": a, "target_id": cid, "confirm": True}
        client.post(CRM_MERGE_PATH, json=merge_body, headers=OPERATOR)
        r = client.post(CRM_MERGE_PATH, json=merge_body, headers=OPERATOR)
        assert r.status_code == 409 and r.json()["code"] == 40901

    def test_undo_endpoint_ok_then_conflict(self) -> None:
        """undo：窗口内 → code=0；二次撤销 → 40901。"""
        client, store, cid = make_client()
        a = client.post(CRM_LEADS_PATH, json={"channel": "alibaba", "buyer_login_id": "b-5",
                                              "email": "a5@x.com"}, headers=OPERATOR)
        a = a.json()["data"]["customer_id"]
        merge_id = client.post(CRM_MERGE_PATH, json={"source_id": a, "target_id": cid, "confirm": True},
                               headers=OPERATOR).json()["data"]["merge_id"]
        ok = client.post(f"{CRM_MERGE_PATH}/{merge_id}/undo", json={"actor": "op-1"}, headers=OPERATOR)
        assert ok.json()["code"] == 0
        bad = client.post(f"{CRM_MERGE_PATH}/{merge_id}/undo", json={"actor": "op-1"}, headers=OPERATOR)
        assert bad.status_code == 409 and bad.json()["code"] == 40901

    def test_timeline_endpoint_sorted(self) -> None:
        """GET timeline：返回客户 + items（按时间升序）+ channels。"""
        client, store, cid = make_client()
        r = client.get(f"/api/v1/site/crm/customers/{cid}/timeline", headers=OPERATOR)
        data = r.json()["data"]
        assert data["customer"]["customer_id"] == cid
        ts_list = [i["ts"] for i in data["items"]]
        assert ts_list == sorted(ts_list)

    def test_crm_requires_operator_role(self) -> None:
        """无 crm_operator 角色 → HTTP 403 + 40301（NFR-SEC-02）。"""
        client, *_ = make_client()
        r = client.get(f"/api/v1/site/crm/customers/1/timeline",
                       headers={"X-User-Id": "sales", "X-User-Roles": "sales"})
        assert r.status_code == 403 and r.json()["code"] == 40301
