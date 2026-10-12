# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""移动端 API 单元测试（FR-SC-006 接口面，SR-SC-006 §5/§9；P4 第二批 TDD）。

覆盖：POST /api/v1/mobile/auth（双端换发）；POST /api/v1/mobile/verify/challenge|
token（二次校验）；GET /api/v1/mobile/home（一屏聚合）；POST
/api/v1/mobile/approvals/{id}/confirm|reject（confirm 强制 verify_token）；POST
/api/v1/mobile/takeover；POST /api/v1/mobile/push/register。错误契约 40101/40102/
40301/40401（中文消息，HTTP 401/403/404）。

实现方案：docs/07-实现方案/SC/SR-SC-006-移动端.md §5、§9
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from services.agent.api.mobile_api import create_mobile_app
from services.agent.core.mobile.bff import (
    Binding,
    BindingStore,
    DingTalkWorkFake,
    FakeApprovalSink,
    FakeConversationSink,
    FakeHomeProvider,
    IdentityStub,
    MobileBff,
    MpTemplateFake,
    PushRouter,
    SmsFake,
    VerifyGate,
    WxSubscribeFake,
)

NOW = datetime(2026, 10, 11, 10, 0, 0)
ROLES = {"supervisor:1": ("supervisor",), "sales:1": ("sales",)}
#: 测试专用签名密钥（demo 凭据，非真实凭证；CR P0-4 修复后构造处显式传入）
TEST_SIGNING_KEY = b"unit-" + b"test-mobile-signing-key"


def build_client() -> tuple[TestClient, list[tuple[str, str]], FakeApprovalSink]:
    """标准装配：双端绑定 + 记录型下游 + TestClient（校验码经 code_sender 捕获）。"""
    codes: list[tuple[str, str]] = []
    bindings = BindingStore()
    bindings.add(Binding("supervisor:1", "dingtalk", "dd-001", "王主管"))
    bindings.add(Binding("sales:1", "dingtalk", "dd-002", "李销售"))
    push = PushRouter(DingTalkWorkFake(), WxSubscribeFake(), MpTemplateFake(), SmsFake(),
                      clock=lambda: NOW)
    verify = VerifyGate(code_sender=lambda uid, code: codes.append((uid, code)), clock=lambda: NOW)
    bff = MobileBff(
        bindings=bindings,
        identity=IdentityStub(dingtalk_users={"auth-ok": "dd-001", "auth-sales": "dd-002"}),
        roles_of=ROLES.get,
        home=FakeHomeProvider(), approvals=FakeApprovalSink(), conversations=FakeConversationSink(),
        push=push, verify=verify, clock=lambda: NOW, signing_key=TEST_SIGNING_KEY)
    return TestClient(create_mobile_app(bff=bff)), codes, bff.approvals  # type: ignore[return-value]


def login(client: TestClient, credential: str = "auth-ok") -> str:
    """登录取 token。"""
    resp = client.post("/api/v1/mobile/auth", json={"channel": "dingtalk", "credential": credential})
    assert resp.status_code == 200
    return str(resp.json()["token"])


def obtain_verify_token(client: TestClient, token: str, codes: list[tuple[str, str]]) -> str:
    """挑战 → 正确校验码 → 一次性确认令牌。"""
    assert client.post("/api/v1/mobile/verify/challenge",
                       headers={"Authorization": f"Bearer {token}"}).status_code == 200
    code = codes[-1][1]
    resp = client.post("/api/v1/mobile/verify/token",
                       json={"answer": code}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    return str(resp.json()["verify_token"])


def test_healthz_ok() -> None:
    """存活探针。"""
    client, _, _ = build_client()
    assert client.get("/healthz").json()["status"] == "ok"


def test_auth_and_home_summary() -> None:
    """登录换发 + 一屏聚合：Bearer 鉴权通过返回四卡片；无 token → 401/40101。"""
    client, _, _ = build_client()
    token = login(client)
    home = client.get("/api/v1/mobile/home", headers={"Authorization": f"Bearer {token}"})
    assert home.status_code == 200
    assert set(home.json()) == {"todo_alerts", "todo_approvals", "taking_over", "today"}
    bare = client.get("/api/v1/mobile/home")
    assert bare.status_code == 401 and bare.json()["detail"]["code"] == 40101


def test_auth_unbound_403_no_leak() -> None:
    """未绑定凭证 → 403/40301，文案不泄露账号存在性。"""
    client, _, _ = build_client()
    resp = client.post("/api/v1/mobile/auth", json={"channel": "dingtalk", "credential": "nope"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == 40301
    assert "不存在" not in resp.json()["detail"]["message"]


def test_verify_challenge_and_token_flow() -> None:
    """二次校验：challenge → 错误码 401/40102 → 正确码发 verify_token。"""
    client, codes, _ = build_client()
    token = login(client)
    assert client.post("/api/v1/mobile/verify/challenge",
                       headers={"Authorization": f"Bearer {token}"}).json()["expires_in"] == 120
    wrong = client.post("/api/v1/mobile/verify/token", json={"answer": "000000"},
                        headers={"Authorization": f"Bearer {token}"})
    assert wrong.status_code == 401 and wrong.json()["detail"]["code"] == 40102
    right = client.post("/api/v1/mobile/verify/token", json={"answer": codes[-1][1]},
                        headers={"Authorization": f"Bearer {token}"})
    assert right.status_code == 200 and right.json()["expires_in"] == 120


def test_confirm_without_token_blocked() -> None:
    """红线兜底：缺 verify_token confirm → 401/40102 且下游零调用（绝不透传）。"""
    client, _, sink = build_client()
    token = login(client)
    resp = client.post("/api/v1/mobile/approvals/sa-1/confirm", json={"verify_token": ""},
                       headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": "k1"})
    assert resp.status_code == 401 and resp.json()["detail"]["code"] == 40102
    assert sink.executed == 0


def test_confirm_with_token_ok_and_replay_blocked() -> None:
    """正常流：二次校验后确认 200（下游执行）；令牌重放 → 401/40102。"""
    client, codes, sink = build_client()
    token = login(client)
    vt = obtain_verify_token(client, token, codes)
    ok = client.post("/api/v1/mobile/approvals/sa-1/confirm", json={"verify_token": vt},
                     headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": "k2"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    replay = client.post("/api/v1/mobile/approvals/sa-1/confirm", json={"verify_token": vt},
                         headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": "k3"})
    assert replay.status_code == 401 and sink.executed == 1


def test_reject_no_verify_needed() -> None:
    """驳回：免二次校验（低风险），幂等键透传，200 返回。"""
    client, _, sink = build_client()
    token = login(client)
    resp = client.post("/api/v1/mobile/approvals/sa-1/reject", json={"reason": "金额存疑"},
                       headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": "k4"})
    assert resp.status_code == 200
    assert sink.reject_executed == 1


def test_takeover_push_register_and_role_guard() -> None:
    """接管透传状态机；push_register 累加订阅额度；学员角色 confirm → 403/40301。"""
    client, _, _ = build_client()
    token = login(client)
    takeover = client.post("/api/v1/mobile/takeover",
                           json={"conversation_uid": "conv-9", "reason": "投诉升级"},
                           headers={"Authorization": f"Bearer {token}"})
    assert takeover.status_code == 200 and takeover.json()["state"] == "human_managed"
    register = client.post("/api/v1/mobile/push/register", json={"openid": "wx-x", "add_quota": 1},
                           headers={"Authorization": f"Bearer {token}"})
    assert register.status_code == 200
    sales = login(client, credential="auth-sales")
    forbidden = client.post("/api/v1/mobile/approvals/sa-1/confirm", json={"verify_token": "x"},
                            headers={"Authorization": f"Bearer {sales}", "X-Idempotency-Key": "k5"})
    assert forbidden.status_code == 403 and forbidden.json()["detail"]["code"] == 40301
