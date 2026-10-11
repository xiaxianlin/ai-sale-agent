# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""开通向导 API 测试（FR-SAAS-002，SR-SAAS-002 §5/§6.5；TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：七步全 API 演练链路（创建→四步→激活→进度→active）、trace_id 透传、
  模板预填接口、演练报告（drill-report）逐步留证、素材登记回显预估；
- 边界：信封四键齐全；
- 异常：缺幂等键 40001、未知 uid 40401、越权 oss 前缀 40001、凭证无明文回显
  （并发口径由向导层 TestConcurrency 覆盖：同企业名恰一胜/同幂等键恰一任务）；
- 降级：配额超限 42901（HTTP 429）提示升配额。

验收口径映射：SRS §16.6「接入全程 API 化」；04-接口设计 §1.3/§3.8。
实现方案：docs/07-实现方案/SAAS/SR-SAAS-002-配置化部署.md §5、§6.5
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.saas.api.onboarding_api import create_onboarding_app
from services.saas.core.onboarding.wizard import OnboardingWizard

STRATEGY = {"strategy.grade.thresholds": {"A": 80, "B": 60},
            "risk.max_ops_per_day": 200, "strategy.followup.cadence.A": "1h"}


def make_client() -> tuple[TestClient, OnboardingWizard]:
    wizard = OnboardingWizard()
    return TestClient(create_onboarding_app(wizard)), wizard


def create_wizard(client: TestClient) -> dict:
    resp = client.post("/api/v1/saas/onboarding", json={"plan": "standard"})
    assert resp.status_code == 200
    return resp.json()["data"]


def run_four_steps(client: TestClient, uid: str, tenant_id: int) -> None:
    """四步配置全部经 API 完成（零代码：仅数据写入）。"""
    r = client.put(f"/api/v1/saas/onboarding/{uid}/company-info", json={
        "name": "API 演练租户", "brand": {"logo": "https://oss.example.com/tenants/1/b/l.png",
                                          "primary": "#1E63E9"},
        "timezone": "Asia/Shanghai", "language": "zh-CN"})
    assert r.status_code == 200
    r = client.post(f"/api/v1/saas/onboarding/{uid}/kb/materials", json={
        "materials": [{"oss_key": f"t:{tenant_id}/raw/a.pdf", "size": 4096, "format": "pdf"}]})
    assert r.status_code == 200
    r = client.put(f"/api/v1/saas/onboarding/{uid}/accounts", json={
        "accounts": [{"platform": "alibaba", "username": "acc-1",
                      "credential": "cred-abcdef-001"}]})
    assert r.status_code == 200
    r = client.put(f"/api/v1/saas/onboarding/{uid}/strategy", json=STRATEGY)
    assert r.status_code == 200


# ───────────────────────── 等价类：全链 API 化演练 ─────────────────────────


class TestEquivalence:
    def test_create_returns_configuring_with_envelope_and_trace(self) -> None:
        """创建向导：信封四键齐全（code/message/data/trace_id），X-Trace-Id 透传。"""
        client, _ = make_client()
        resp = client.post("/api/v1/saas/onboarding", json={"plan": "standard"},
                           headers={"X-Trace-Id": "trace-abc-123"})
        body = resp.json()
        assert set(body) == {"code", "message", "data", "trace_id"}
        assert body["code"] == 0 and body["message"] == "ok"
        assert body["data"]["status"] == "configuring"
        assert body["trace_id"] == "trace-abc-123"

    def test_full_api_drill_to_active(self) -> None:
        """七步演练：创建→四步→激活→进度轮询→active（§16.6 API 化口径）。"""
        client, wizard = make_client()
        data = create_wizard(client)
        uid, tid = data["uid"], data["tenant_id"]
        run_four_steps(client, uid, tid)
        resp = client.post(f"/api/v1/saas/onboarding/{uid}/activate",
                           headers={"Idempotency-Key": "drill-act-001"})
        assert resp.status_code == 200
        task_uid = resp.json()["data"]["kb_task_uid"]
        wizard.kb_init.run_task(task_uid, lambda k, f: None)
        prog = client.get(f"/api/v1/saas/onboarding/{uid}/progress").json()["data"]
        assert prog["status"] == "active"
        assert prog["kb"]["done_files"] == 1 and prog["kb"]["percent"] == 100.0

    def test_materials_registration_returns_estimate(self) -> None:
        """素材登记同步回显资源预估（chunks/vec_gb/oss_gb/embed_minutes）。"""
        client, _ = make_client()
        data = create_wizard(client)
        resp = client.post(f"/api/v1/saas/onboarding/{data['uid']}/kb/materials", json={
            "materials": [{"oss_key": f"t:{data['tenant_id']}/raw/x.pdf",
                           "size": 100_000, "format": "pdf"}]})
        est = resp.json()["data"]["estimate"]
        assert est["chunks"] > 0 and est["oss_gb"] > 0 and est["embed_minutes"] > 0

    def test_templates_defaults_prefill(self) -> None:
        """GET /templates/defaults：模板预填含全部差异项必填键。"""
        client, _ = make_client()
        items = client.get("/api/v1/saas/templates/defaults").json()["data"]["templates"]
        keys = {t["cfg_key"] for t in items}
        assert {"strategy.grade.thresholds", "risk.max_ops_per_day",
                "strategy.followup.cadence.A"} <= keys
        assert all({"scope", "cfg_key", "default_value", "kind"} <= set(t) for t in items)

    def test_drill_report_records_steps(self) -> None:
        """演练报告（GET drill-report）：接入清单七步逐步留证（与录屏对齐口径）。"""
        client, wizard = make_client()
        data = create_wizard(client)
        run_four_steps(client, data["uid"], data["tenant_id"])
        task_uid = client.post(f"/api/v1/saas/onboarding/{data['uid']}/activate",
                               headers={"Idempotency-Key": "drill-act-002"}
                               ).json()["data"]["kb_task_uid"]
        wizard.kb_init.run_task(task_uid, lambda k, f: None)  # 初始化完成至 active
        steps = client.get(
            f"/api/v1/saas/onboarding/{data['uid']}/drill-report").json()["data"]["steps"]
        assert [s["seq"] for s in steps] == list(range(7))
        assert all(s["ok"] for s in steps)
        assert steps[0]["api"] == "POST /api/v1/saas/onboarding"


# ───────────────────────── 异常：幂等键 / 404 / 隔离 / 状态 / 校验 ─────────────────────────


class TestExceptions:
    def test_activate_requires_idempotency_key(self) -> None:
        """激活缺 Idempotency-Key → 40001（幂等强制）。"""
        client, _ = make_client()
        data = create_wizard(client)
        run_four_steps(client, data["uid"], data["tenant_id"])
        resp = client.post(f"/api/v1/saas/onboarding/{data['uid']}/activate")
        assert resp.status_code == 400
        assert resp.json()["code"] == 40001

    def test_unknown_uid_envelope_404(self) -> None:
        """未知 uid → HTTP 404 + code 40401 信封。"""
        client, _ = make_client()
        resp = client.get("/api/v1/saas/onboarding/no-such/progress")
        assert resp.status_code == 404
        assert resp.json()["code"] == 40401

    def test_foreign_oss_prefix_rejected(self) -> None:
        """越权 oss 前缀（他租户目录）→ 40001（NFR-SEC-07 隔离）。"""
        client, _ = make_client()
        data = create_wizard(client)
        resp = client.post(f"/api/v1/saas/onboarding/{data['uid']}/kb/materials", json={
            "materials": [{"oss_key": "t:424242/raw/evil.pdf", "size": 1, "format": "pdf"}]})
        assert resp.status_code == 400 and resp.json()["code"] == 40001

    def test_credentials_never_in_api_response(self) -> None:
        """账号提交响应与后续响应均无凭证明文（仅掩码回显）。"""
        client, _ = make_client()
        data = create_wizard(client)
        resp = client.put(f"/api/v1/saas/onboarding/{data['uid']}/accounts", json={
            "accounts": [{"platform": "alibaba", "username": "u", "credential": "plain-secret-9"}]})
        assert "plain-secret-9" not in resp.text
        assert resp.json()["data"]["accounts"][0]["credential"].startswith("<enc:")


# ───────────────────────── 并发/降级 ─────────────────────────


class TestDegrade:
    def test_quota_exceeded_maps_429(self) -> None:
        """预估超配额 → HTTP 429 + code 42901，提示升配额（FR-SAAS-003 联动）。"""
        client, _ = make_client()
        data = create_wizard(client)
        uid, tid = data["uid"], data["tenant_id"]
        client.put(f"/api/v1/saas/onboarding/{uid}/company-info", json={
            "name": "大素材租户", "brand": {"logo": "https://oss.example.com/tenants/1/b/l.png",
                                            "primary": "#1E63E9"},
            "timezone": "UTC", "language": "en"})
        client.post(f"/api/v1/saas/onboarding/{uid}/kb/materials", json={
            "materials": [{"oss_key": f"t:{tid}/raw/huge.bin", "size": int(1.5 * 2**30),
                           "format": "pdf"}]})
        client.put(f"/api/v1/saas/onboarding/{uid}/accounts", json={
            "accounts": [{"platform": "alibaba", "username": "u", "credential": "cred-12345678"}]})
        client.put(f"/api/v1/saas/onboarding/{uid}/strategy", json=STRATEGY)
        resp = client.post(f"/api/v1/saas/onboarding/{uid}/activate",
                           headers={"Idempotency-Key": "k-429"})
        assert resp.status_code == 429
        body = resp.json()
        assert body["code"] == 42901 and "升配额" in body["message"]
