# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""SaaS API 集成面单元测试（FR-SAAS-001/003 §5；TDD 阶段①②，五类场景齐备）。

覆盖：tenants CRUD / suspend|reinstate|expire、配额变更即时生效、用量查询、
计费导出（无定价字段）、双域闸门（aud 互斥）、停用闸门、非平台角色 40301、
隔离报告端点、trace_id 透传、42901+Retry-After 契约。
验收口径映射：SRS §16.6「运营后台功能可用」+「多租户隔离测试通过」（联动 FR-SAAS-003）。

实现方案：docs/07-实现方案/SAAS/SR-SAAS-003-标准化运营后台.md §5、SR-SAAS-001 §5
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from services.saas.api.saas_api import SAAS_PLATFORM_PREFIX, SAAS_TENANT_PREFIX, create_saas_app
from services.saas.core.quota.engine import QuotaEngine
from services.saas.core.tenant.context import SAAS_TOKEN_SECRET_ENV, SaasConfigError
from services.saas.core.tenant.registry import TenantRegistry

T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
#: 测试专用令牌签名密钥（demo 凭据，非真实凭证；CR P0-5 修复后构造处显式传入）
TEST_SAAS_SECRET = "unit-" + "test-saas-secret"


def build_app(**kw):
    """标准装配：真实注册表 + 配额引擎 + 运营服务 + 租户业务面（隔离套件靶场）。

    令牌 TTL 取 10 年：部分用例会拨动时钟（保留期演练），平台令牌不得先于业务过期。"""
    clock = kw.pop("clock", lambda: T0)
    registry = kw.pop("registry", None) or TenantRegistry(clock=clock)
    kw.setdefault("secret", TEST_SAAS_SECRET)
    quota = kw.pop("quota", None) or QuotaEngine(clock=clock)
    app, comps = create_saas_app(registry=registry, quota=quota, clock=clock, **kw)
    client = TestClient(app)
    long_ttl = 10 * 365 * 24 * 3600
    platform_token = comps["ops"].issue_platform_token("ops-1", "operator", ttl_sec=long_ttl)
    admin_token = comps["ops"].issue_platform_token("ad-1", "admin", ttl_sec=long_ttl)
    return client, comps, registry, quota, platform_token, admin_token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- 租户 CRUD / 生命周期


def test_provision_endpoint_201_list_and_trace_passthrough() -> None:
    """开通 → 201 信封（status/expire_at）；列表可见；X-Trace-Id 透传（04 §1.4）。"""
    client, comps, registry, _, ptoken, _ = build_app()
    resp = client.post(
        f"{SAAS_PLATFORM_PREFIX}/tenants",
        json={"name": "示例外贸公司", "initial_days": 365},
        headers={**auth(ptoken), "X-Trace-Id": "tr-fixed-42"},
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["status"] == "active" and data["plan"] == "standard"
    assert set(resp.json()) == {"code", "message", "data", "trace_id"}
    assert resp.json()["trace_id"] == "tr-fixed-42"
    listed = client.get(
        f"{SAAS_PLATFORM_PREFIX}/tenants", headers=auth(ptoken)
    ).json()["data"]["items"]
    assert any(item["id"] == data["id"] for item in listed)
    detail = client.get(f"{SAAS_PLATFORM_PREFIX}/tenants/{data['id']}", headers=auth(ptoken)).json()
    assert detail["data"]["name"] == "示例外贸公司"


def test_provision_duplicate_409_envelope() -> None:
    """开通名称重复 → HTTP 409 / code 40902（TENANT_NAME_DUP，中文消息）。"""
    client, _, _, _, ptoken, _ = build_app()
    body = {"name": "重名公司"}
    client.post(f"{SAAS_PLATFORM_PREFIX}/tenants", json=body, headers=auth(ptoken))
    resp = client.post(f"{SAAS_PLATFORM_PREFIX}/tenants", json=body, headers=auth(ptoken))
    assert resp.status_code == 409 and resp.json()["code"] == 40902


def test_get_unknown_tenant_404() -> None:
    """未知租户 → HTTP 404 / code 40401。"""
    client, _, _, _, ptoken, _ = build_app()
    resp = client.get(f"{SAAS_PLATFORM_PREFIX}/tenants/999", headers=auth(ptoken))
    assert resp.status_code == 404 and resp.json()["code"] == 40401


def test_suspend_reinstate_expire_endpoints() -> None:
    """停用/续期恢复/到期三端点：状态跟随迁移（SR §5 租户管理三操作）。"""
    client, _, registry, _, ptoken, _ = build_app()
    tid = client.post(
        f"{SAAS_PLATFORM_PREFIX}/tenants", json={"name": "甲公司"}, headers=auth(ptoken)
    ).json()["data"]["id"]
    assert (
        client.post(
            f"{SAAS_PLATFORM_PREFIX}/tenants/{tid}/suspend",
            json={"reason": "欠费"},
            headers=auth(ptoken),
        ).json()["data"]["status"]
        == "suspended"
    )
    assert (
        client.post(
            f"{SAAS_PLATFORM_PREFIX}/tenants/{tid}/reinstate",
            json={"days": 30},
            headers=auth(ptoken),
        ).json()["data"]["status"]
        == "active"
    )
    assert (
        client.post(
            f"{SAAS_PLATFORM_PREFIX}/tenants/{tid}/expire", headers=auth(ptoken)
        ).json()["data"]["status"]
        == "expired"
    )


def test_quota_update_endpoint_effective() -> None:
    """配额变更端点：变更即生效并留痕——超新限立即 42901（改配额免发版口径）。"""
    client, comps, registry, quota, ptoken, _ = build_app()
    tid = client.post(
        f"{SAAS_PLATFORM_PREFIX}/tenants", json={"name": "甲公司"}, headers=auth(ptoken)
    ).json()["data"]["id"]
    resp = client.put(
        f"{SAAS_PLATFORM_PREFIX}/tenants/{tid}/quota",
        json={"resource": "kb_item_cnt", "hard_limit": 2},
        headers=auth(ptoken),
    )
    assert resp.status_code == 200 and resp.json()["data"]["hard_limit"] == 2
    assert quota.limit_of(tid, "kb_item_cnt") == 2  # 引擎同步生效
    token = comps["ops"].issue_tenant_token(tid, sub="u")
    h = auth(token)
    for _ in range(2):  # 消费至恰等上限
        client.post(
            f"{SAAS_TENANT_PREFIX}/knowledge/items",
            json={"title": "t", "body": "b"},
            headers=h,
        )
    resp = client.post(
        f"{SAAS_TENANT_PREFIX}/knowledge/items", json={"title": "t", "body": "b"}, headers=h
    )
    assert resp.status_code == 429 and resp.json()["code"] == 42901


def test_usage_endpoint_returns_hourly_items() -> None:
    """用量查询端点：小时数组 + 汇总（计费数据源，FR-SAAS-003 消费口径）。"""
    client, comps, registry, quota, ptoken, _ = build_app()
    tid = client.post(
        f"{SAAS_PLATFORM_PREFIX}/tenants", json={"name": "甲公司"}, headers=auth(ptoken)
    ).json()["data"]["id"]
    quota.check(tid, "api_qps", amount=3)
    quota.flush_hour(bucket=T0)
    data = client.get(
        f"{SAAS_PLATFORM_PREFIX}/tenants/{tid}/usage", headers=auth(ptoken)
    ).json()["data"]
    assert data["items"][0]["api_calls"] == 3
    assert data["summary"]["api_calls"] == 3


def test_billing_export_csv_response_headers() -> None:
    """计费导出端点：text/csv + Content-Disposition；全文无定价字段（§10.1 边界）。"""
    client, comps, registry, quota, ptoken, _ = build_app()
    tid = client.post(
        f"{SAAS_PLATFORM_PREFIX}/tenants", json={"name": "甲公司"}, headers=auth(ptoken)
    ).json()["data"]["id"]
    quota.check(tid, "kb_item_cnt", amount=2)
    quota.flush_hour(bucket=T0)
    resp = client.get(
        f"{SAAS_PLATFORM_PREFIX}/billing/usage/export",
        params={"period": "2026-10"},
        headers=auth(ptoken),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers.get("content-disposition", "")
    for banned in ("price", "amount", "fee", "单价", "金额", "账单", "定价"):
        assert banned not in resp.text.lower()


# --------------------------------------------------------------------------- 双域闸门 / RBAC（API 级）


def test_dual_aud_mutual_exclusion_at_api() -> None:
    """双域互斥 API 级：租户令牌打平台路由 401；平台令牌打租户路由 401；无令牌 401。"""
    client, comps, registry, _, ptoken, _ = build_app()
    tid = client.post(
        f"{SAAS_PLATFORM_PREFIX}/tenants", json={"name": "甲公司"}, headers=auth(ptoken)
    ).json()["data"]["id"]
    tenant_token = comps["ops"].issue_tenant_token(tid, sub="t")
    r1 = client.get(f"{SAAS_PLATFORM_PREFIX}/tenants", headers=auth(tenant_token))
    r2 = client.get(f"{SAAS_TENANT_PREFIX}/whoami", headers=auth(ptoken))
    r3 = client.get(f"{SAAS_TENANT_PREFIX}/whoami")
    assert r1.status_code == 401 and r1.json()["code"] == 40101
    assert r2.status_code == 401 and r2.json()["code"] == 40101
    assert r3.status_code == 401


def test_suspended_gate_blocks_tenant_route() -> None:
    """停用闸门：suspended 租户任意租户域请求 → 403 TENANT_SUSPENDED（闸门拦截非删除）。"""
    client, comps, registry, _, ptoken, _ = build_app()
    tid = client.post(
        f"{SAAS_PLATFORM_PREFIX}/tenants", json={"name": "甲公司"}, headers=auth(ptoken)
    ).json()["data"]["id"]
    token = comps["ops"].issue_tenant_token(tid, sub="t")
    client.post(
        f"{SAAS_PLATFORM_PREFIX}/tenants/{tid}/suspend",
        json={"reason": "演练"},
        headers=auth(ptoken),
    )
    resp = client.get(f"{SAAS_TENANT_PREFIX}/whoami", headers=auth(token))
    assert resp.status_code == 403 and resp.json()["code"] == 40302
    assert "suspended" in resp.json()["message"]
    assert registry.get(tid) is not None  # 数据未删（闸门≠删除）


def test_non_platform_role_40301_at_api() -> None:
    """非平台角色（aud=platform 但 role 不在 operator/admin）→ 40301。"""
    client, comps, _, _, _, _ = build_app()
    intruder = comps["ops"].issue_platform_token("ghost", "sales")
    resp = client.get(f"{SAAS_PLATFORM_PREFIX}/tenants", headers=auth(intruder))
    assert resp.status_code == 403 and resp.json()["code"] == 40301


def test_purge_admin_only_and_retain_window() -> None:
    """清理（admin-only）：operator → 40301；未过保留期 → 409；保留期后 admin 清理成功。"""
    now = T0
    client, comps, registry, _, _, admin_token = build_app(
        clock=lambda: now,
    )
    ptoken = comps["ops"].issue_platform_token("ops-1", "operator")
    tid = client.post(
        f"{SAAS_PLATFORM_PREFIX}/tenants", json={"name": "甲公司", "initial_days": 5},
        headers=auth(ptoken),
    ).json()["data"]["id"]
    r_forbidden = client.delete(
        f"{SAAS_PLATFORM_PREFIX}/tenants/{tid}", headers=auth(ptoken)
    )
    assert r_forbidden.status_code == 403 and r_forbidden.json()["code"] == 40301
    client.post(f"{SAAS_PLATFORM_PREFIX}/tenants/{tid}/expire", headers=auth(admin_token))
    r_early = client.delete(f"{SAAS_PLATFORM_PREFIX}/tenants/{tid}", headers=auth(admin_token))
    assert r_early.status_code == 409 and r_early.json()["code"] == 40903  # 保留期未届满禁清理
    now = T0 + timedelta(days=181)  # 保留期（180 天）届满
    r_ok = client.delete(f"{SAAS_PLATFORM_PREFIX}/tenants/{tid}", headers=auth(admin_token))
    assert r_ok.status_code == 200
    assert registry.get(tid) is None  # 保留期届满后物理清理（与停用冻结相区分）


# --------------------------------------------------------------------------- 隔离报告 / 观测


def test_isolation_report_endpoint() -> None:
    """隔离报告端点：调用执行器 → 返回 verdict 与报告路径（NFR-SEC-07 报告交付口）。"""
    runner_called: list[bool] = []

    def runner() -> dict[str, object]:
        runner_called.append(True)
        return {"verdict": "PASS", "passed": 9, "failed": 0, "report_path": "out/iso-r.md"}

    client, comps, _, _, ptoken, _ = build_app(isolation_runner=runner)
    data = client.post(
        f"{SAAS_PLATFORM_PREFIX}/isolation/report", headers=auth(ptoken)
    ).json()["data"]
    assert runner_called == [True]
    assert data["verdict"] == "PASS" and data["failed"] == 0


def test_search_quota_429_retry_after_header() -> None:
    """计费检索限流：超 QPS → 429 + Retry-After: 1 + 信封 42901（中间件闸门路径）。"""
    client, comps, registry, quota, ptoken, _ = build_app()
    tid = client.post(
        f"{SAAS_PLATFORM_PREFIX}/tenants", json={"name": "甲公司"}, headers=auth(ptoken)
    ).json()["data"]["id"]
    quota.set_limit(tid, "api_qps", 1)
    token = comps["ops"].issue_tenant_token(tid, sub="u")
    h = {**auth(token), "Content-Type": "application/json"}
    ok = client.post(f"{SAAS_TENANT_PREFIX}/knowledge/search", json={"q": "支架"}, headers=h)
    assert ok.status_code == 200
    blocked = client.post(f"{SAAS_TENANT_PREFIX}/knowledge/search", json={"q": "支架"}, headers=h)
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After") == "1"
    assert blocked.json()["code"] == 42901
    assert blocked.json()["data"]["resource"] == "api_qps"


def test_missing_secret_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR P0-5 修复：未传 secret 且环境变量 KB_SAAS_TOKEN_SECRET 未配置 → 装配即
    SaasConfigError（fail-fast，拒绝缺省密钥运行；对齐 vault 缺主密钥口径）。"""
    monkeypatch.delenv(SAAS_TOKEN_SECRET_ENV, raising=False)
    with pytest.raises(SaasConfigError, match=SAAS_TOKEN_SECRET_ENV):
        create_saas_app()
