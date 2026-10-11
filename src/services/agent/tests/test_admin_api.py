# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V1 API 单元测试（FR-AG-006，SR-AG-006 §5/§9；迭代 2.3 TDD 阶段①用例）。

覆盖（FastAPI 端点契约，错误码沿用 §1.3 + 40901 状态冲突）：
- GET  /api/v1/agent/admin/sessions（四态过滤/分页）
- GET  /api/v1/agent/admin/sessions/{uid}/messages（增量游标）
- POST /api/v1/agent/admin/sessions/{uid}/takeover | /release（状态机 + 错误映射）
- GET/POST /api/v1/agent/admin/sensitive-actions（列表/详情/confirm/reject）
- GET  /api/v1/agent/admin/audit-logs（多维筛选 + chain_ok）
- 40401 会话不存在 / 42201 非法迁移 / 40901 状态冲突 / 40404 草稿不存在。

实现方案：docs/07-实现方案/AG/SR-AG-006-管理后台V1.md §5、§6.3、§9
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from services.agent.core.admin.views import AdminViews, HashChainAuditLog, SensitiveBoard
from services.agent.api.admin_api import create_admin_app
from services.agent.core.state import (
    ConversationStateMachine,
    ConvState,
    InMemorySessionStore,
)

NOW = datetime(2026, 10, 8, 10, 0, 0)


@pytest.fixture()
def client():
    """标准装配：四态会话 + 一条 AI 消息 + 一条待确认草稿。"""
    store = InMemorySessionStore()
    sm = ConversationStateMachine(store)
    views = AdminViews(store=store, state_machine=sm, audit_log=HashChainAuditLog(),
                       sensitive=SensitiveBoard())
    sm.ensure_conversation("c-1")
    sm.ensure_conversation("c-2")
    sm.transition("c-2", ConvState.HUMAN_MANAGED, actor="sale", reason="接管")
    views.messages["c-1"] = [
        {"msg_id": 1, "sender": "ai", "content": "Thanks [R1]", "confidence": 0.9,
         "knowledge_refs": ({"chunk_id": 1, "kb_type": "product", "title": "t", "score": 0.8},),
         "created_at": NOW},
    ]
    views.sensitive.create(conversation_uid="c-1", action_type="price_change",
                           draft_content={"price": 11.8}, current_snapshot={"price": 12.5},
                           now=NOW)
    return TestClient(create_admin_app(views=views))


class TestSessionEndpoints:
    """会话中心端点。"""

    def test_list_sessions_with_state_filter(self, client: TestClient) -> None:
        """四态过滤参数生效；响应含 uid/state/version。"""
        resp = client.get("/api/v1/agent/admin/sessions", params={"state": "human_managed"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["uid"] == "c-2"
        assert body["items"][0]["state"] == "human_managed"

    def test_messages_cursor(self, client: TestClient) -> None:
        """消息流增量游标：after_id 之后返回，AI 消息带置信度与来源。"""
        resp = client.get("/api/v1/agent/admin/sessions/c-1/messages", params={"after_id": 0})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items[0]["sender"] == "ai"
        assert items[0]["confidence"] == 0.9
        assert items[0]["knowledge_refs"][0]["kb_type"] == "product"

    def test_takeover_and_release(self, client: TestClient) -> None:
        """接管→放回：状态迁移返回，state_hist 留痕操作者。"""
        took = client.post("/api/v1/agent/admin/sessions/c-1/takeover",
                           json={"reason": "客户投诉"}, headers={"X-Operator": "sale-07"})
        assert took.status_code == 200
        assert took.json()["state"] == "human_managed"
        released = client.post("/api/v1/agent/admin/sessions/c-1/release",
                               json={"reason": "处理完毕"}, headers={"X-Operator": "sale-07"})
        assert released.status_code == 200
        assert released.json()["state"] == "ai_managed"
        hist = released.json()["state_hist"]
        assert hist[-1]["by"] == "sale-07"

    def test_takeover_closed_is_422(self, client: TestClient) -> None:
        """closed 会话接管 → 422 + 错误码 42201（迁移表无此路径）。"""
        store = InMemorySessionStore()
        sm = ConversationStateMachine(store)
        views = AdminViews(store=store, state_machine=sm, audit_log=HashChainAuditLog())
        sm.ensure_conversation("c-x")
        sm.transition("c-x", ConvState.CLOSED, actor="sale", reason="结束")
        app = TestClient(create_admin_app(views=views))
        resp = app.post("/api/v1/agent/admin/sessions/c-x/takeover", json={"reason": "r"})
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == 42201

    def test_unknown_session_404(self, client: TestClient) -> None:
        """未知会话 → 404 + 错误码 40401。"""
        resp = client.get("/api/v1/agent/admin/sessions/c-none/messages")
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == 40401


class TestSensitiveEndpoints:
    """敏感操作端点（对比视图 + 确认/驳回）。"""

    def test_list_pending_with_decider_columns(self, client: TestClient) -> None:
        """待确认列表：含决策人/时刻/驳回理由列（双人可见的数据面）。"""
        resp = client.get("/api/v1/agent/admin/sensitive-actions", params={"status": "pending"})
        assert resp.status_code == 200
        row = resp.json()["items"][0]
        assert row["action_type"] == "price_change"
        assert {"decided_by", "decided_at", "reject_reason"} <= set(row)

    def test_diff_view_endpoint(self, client: TestClient) -> None:
        """对比视图端点：draft/snapshot/差异键。"""
        uid = client.get("/api/v1/agent/admin/sensitive-actions").json()["items"][0]["uid"]
        resp = client.get(f"/api/v1/agent/admin/sensitive-actions/{uid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["diff_keys"] == ["price"]
        assert body["draft_content"]["price"] == 11.8

    def test_confirm_then_reject_conflict(self, client: TestClient) -> None:
        """确认成功；二次驳回 → 409 + 40901（已决策不可再改）。"""
        uid = client.get("/api/v1/agent/admin/sensitive-actions").json()["items"][0]["uid"]
        ok = client.post(f"/api/v1/agent/admin/sensitive-actions/{uid}/confirm",
                         json={"operator": "supervisor"})
        assert ok.status_code == 200
        assert ok.json()["status"] == "confirmed"
        dup = client.post(f"/api/v1/agent/admin/sensitive-actions/{uid}/reject",
                          json={"operator": "other", "reason": "再驳"})
        assert dup.status_code == 409
        assert dup.json()["detail"]["code"] == 40901

    def test_reject_records_reason(self, client: TestClient) -> None:
        """驳回理由留痕并可在已驳回列表读回。"""
        uid = client.get("/api/v1/agent/admin/sensitive-actions").json()["items"][0]["uid"]
        ok = client.post(f"/api/v1/agent/admin/sensitive-actions/{uid}/reject",
                         json={"operator": "supervisor", "reason": "条款有误"})
        assert ok.status_code == 200
        rows = client.get("/api/v1/agent/admin/sensitive-actions",
                          params={"status": "rejected"}).json()["items"]
        assert rows[0]["reject_reason"] == "条款有误"

    def test_unknown_sensitive_404(self, client: TestClient) -> None:
        """未知草稿 → 404 + 40404。"""
        resp = client.get("/api/v1/agent/admin/sensitive-actions/no-such")
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == 40404


class TestAuditEndpoint:
    """审计检索端点。"""

    def test_audit_logs_with_chain(self, client: TestClient) -> None:
        """审计检索：筛选参数 + chain_ok 完整性标识。"""
        client.post("/api/v1/agent/admin/sessions/c-1/takeover", json={"reason": "r"},
                    headers={"X-Operator": "sale-07"})
        resp = client.get("/api/v1/agent/admin/audit-logs",
                          params={"action_type": "human_takeover", "actor": "sale-07"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["chain_ok"] is True
        assert body["items"][0]["action_type"] == "human_takeover"
