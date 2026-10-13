# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""agent-runner 对内 API 单元测试（FR-AG-008 接口面，SR-AG-008 §5）。

覆盖：
- GET /healthz 存活探针；
- GET /api/v1/agent/sessions 列表（含空态）与单会话详情（state_hist 留痕可查）；
- POST .../takeover、.../release 走状态机（成功迁移 + 审计留痕 + 操作者来自请求头）；
- 错误契约：404 会话不存在 / 422 非法迁移 / 409 版本冲突（错误码与中文消息）；
- 响应面无凭证类字段（掩码口径的结构性保证）。

实现方案：docs/07-实现方案/AG/SR-AG-008-会话状态机.md §5、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from fastapi.testclient import TestClient

from services.agent.api.runner_api import create_runner_app
from services.agent.core.state import (
    AISendGate,
    ConversationStateMachine,
    InMemorySessionStore,
)

UID = "alibaba:C001"


def build_client() -> tuple[TestClient, InMemorySessionStore, ConversationStateMachine, list[dict]]:
    """标准装配：内存存储 + 状态机 + 记录型审计 + TestClient。"""
    store = InMemorySessionStore()
    audit: list[dict] = []
    sm = ConversationStateMachine(store, audit=audit.append)
    app = create_runner_app(store=store, state_machine=sm, gate=AISendGate(store), audit=audit.append)
    return TestClient(app), store, sm, audit


def seed_session(sm: ConversationStateMachine) -> None:
    """预置一个 ai_managed 会话。"""
    sm.ensure_conversation(UID)


def test_healthz_ok() -> None:
    """存活探针：/healthz 返回 ok 与服务名。"""
    client, *_ = build_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "agent-runner"


def test_list_sessions_empty_and_with_data() -> None:
    """列表：空态返回空数组；有数据时字段含 uid/state/state_version/state_hist_len。"""
    client, _, sm, _ = build_client()
    resp = client.get("/api/v1/agent/sessions")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": []}
    seed_session(sm)
    sm.transition(UID, "human_managed", actor="user:a", reason="接管")
    resp = client.get("/api/v1/agent/sessions")
    sessions = resp.json()["sessions"]
    assert len(sessions) == 1
    row = sessions[0]
    assert row["uid"] == UID
    assert row["state"] == "human_managed"
    assert row["state_version"] == 1
    assert row["state_hist_len"] == 1


def test_session_detail_contains_hist() -> None:
    """单会话详情：state_hist 逐条可查（from/to/at/by/reason）。"""
    client, _, sm, _ = build_client()
    seed_session(sm)
    sm.transition(UID, "pending_confirm", actor="ai:brain", reason="敏感操作挂起")
    resp = client.get(f"/api/v1/agent/sessions/{UID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "pending_confirm"
    assert body["pending_since"] is not None
    entry = body["state_hist"][-1]
    assert entry["from"] == "ai_managed" and entry["to"] == "pending_confirm"
    assert entry["reason"] == "敏感操作挂起"


def test_takeover_success_with_audit() -> None:
    """人工接管：走状态机迁移 human_managed，审计恰一条、操作者取自请求头。"""
    client, _, sm, audit = build_client()
    seed_session(sm)
    resp = client.post(
        f"/api/v1/agent/sessions/{UID}/takeover",
        json={"reason": "客户议价超出授权"},
        headers={"X-Operator": "user:zhangsan"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "human_managed"
    assert body["version"] == 1
    assert len(audit) == 1
    assert audit[0]["by"] == "user:zhangsan"
    assert audit[0]["reason"] == "客户议价超出授权"


def _sm_of(client: TestClient):
    """取装配内状态机（测试辅助：app.state 挂载）。"""
    sm = client.app.state.state_machine  # type: ignore[attr-defined]
    sm.ensure_conversation(UID)
    return sm


def test_release_success() -> None:
    """人工放回：takeover 后 release → ai_managed，版本连续递增。"""
    client, _, _, _ = build_client()
    _sm_of(client)
    client.post(f"/api/v1/agent/sessions/{UID}/takeover", json={"reason": "接管"})
    resp = client.post(f"/api/v1/agent/sessions/{UID}/release", json={"reason": "恢复自动"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "ai_managed"
    assert resp.json()["version"] == 2


def test_takeover_unknown_uid_404() -> None:
    """未知会话：404 + 错误码 40401 + 中文消息。"""
    client, *_ = build_client()
    resp = client.post("/api/v1/agent/sessions/alibaba:GHOST/takeover", json={"reason": "x"})
    assert resp.status_code == 404
    assert resp.json()["code"] == 40401
    assert "不存在" in resp.json()["message"]


def test_takeover_from_pending_illegal_422() -> None:
    """非法迁移：pending_confirm 态人工接管 → 422 + 42201 + 中文消息（无写库）。"""
    client, _, sm, _ = build_client()
    seed_session(sm)
    sm.transition(UID, "pending_confirm", actor="ai:brain", reason="挂起")
    resp = client.post(f"/api/v1/agent/sessions/{UID}/takeover", json={"reason": "x"})
    assert resp.status_code == 422
    assert resp.json()["code"] == 42201
    assert "非法" in resp.json()["message"]


def test_stale_expect_version_conflict_409() -> None:
    """版本冲突：携带过期 expect_version → 409 + 40901 + 中文消息。"""
    client, _, sm, _ = build_client()
    seed_session(sm)
    resp = client.post(
        f"/api/v1/agent/sessions/{UID}/takeover",
        json={"reason": "竞态写者", "expect_version": 99},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == 40901
    assert "冲突" in resp.json()["message"]


def test_response_surface_has_no_credential_fields() -> None:
    """结构防回显：全部响应键不含明文/密文/凭证类字段（掩码口径的模型层保证）。"""
    client, _, sm, _ = build_client()
    seed_session(sm)
    client.post(f"/api/v1/agent/sessions/{UID}/takeover", json={"reason": "x"})
    forbidden = {"plaintext", "cipher_text", "ciphertext", "secret", "cookie"}
    for resp in (
        client.get("/api/v1/agent/sessions"),
        client.get(f"/api/v1/agent/sessions/{UID}"),
        client.get("/healthz"),
    ):
        body_text = resp.text.lower()
        assert not any(f'"{k}"' in body_text for k in forbidden)
