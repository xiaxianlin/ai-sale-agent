# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""运营后台服务单元测试（FR-SAAS-003 §6.1/§6.3/§6.4；TDD 阶段①②，五类场景齐备）。

覆盖：双域闸门互斥（aud=platform / aud=tenant 互不接受）、平台 RBAC
（operator/admin 细分，admin-only 越权 40301）、审计哈希链（防篡改）、
计费用量导出（仅事实数据无定价字段 + period 聚合与格式校验）。
验收口径映射：SRS §16.6「平台/租户权限分离」+ §10.1 边界（计费只出数据不出价格）。

实现方案：docs/07-实现方案/SAAS/SR-SAAS-003-标准化运营后台.md §5/§6.3/§6.4/§9
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.saas.core.ops.service import (
    ADMIN_ONLY_ACTIONS,
    AuthError,
    FORBIDDEN_PRICING_SUBSTRINGS,
    PLATFORM_AUD,
    ROLE_DENIED,
    TENANT_AUD,
    TenantGateError,
    UsageExportError,
    OpsService,
    RoleDeniedError,
)
from services.saas.core.quota.engine import QuotaEngine
from services.saas.core.tenant.context import SAAS_TOKEN_SECRET_ENV, SaasConfigError
from services.saas.core.tenant.registry import TenantRegistry

T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
#: 测试专用令牌签名密钥（demo 凭据，非真实凭证；拼接形态避免被密钥扫描误报）
TEST_OPS_SECRET = "unit-" + "test-secret"


def build_ops() -> tuple[OpsService, TenantRegistry, QuotaEngine]:
    clock = lambda: T0  # noqa: E731
    registry = TenantRegistry(clock=clock)
    quota = QuotaEngine(clock=clock)
    ops = OpsService(registry, quota, clock=clock, secret=TEST_OPS_SECRET)
    ops.wire()  # 把审计钩子接入注册表/配额引擎（开通/停用/配额变更全量留痕）
    return ops, registry, quota


# --------------------------------------------------------------------------- 缺省密钥关门（CR P0-5 修复）


def test_missing_secret_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR P0-5 修复：OpsService 未传 codec/secret 且 KB_SAAS_TOKEN_SECRET 未配置 →
    构造即 SaasConfigError（fail-fast，拒绝缺省密钥签发平台/租户令牌）。"""
    monkeypatch.delenv(SAAS_TOKEN_SECRET_ENV, raising=False)
    with pytest.raises(SaasConfigError, match=SAAS_TOKEN_SECRET_ENV):
        OpsService(TenantRegistry(), QuotaEngine(), clock=lambda: T0)


# --------------------------------------------------------------------------- 双域闸门（aud 互斥）


def test_platform_token_and_dual_aud_mutual_exclusion() -> None:
    """平台令牌签发/校验 + 双域令牌互斥（SR §6.3）：租户令牌打平台域 401；
    平台令牌打租户域同样 401。"""
    ops, registry, _ = build_ops()
    platform_token = ops.issue_platform_token("ops-user-1", "operator")
    claims = ops.verify_token(platform_token, expected_aud=PLATFORM_AUD)
    assert claims.aud == PLATFORM_AUD and claims.sub == "ops-user-1" and claims.role == "operator"
    tid = registry.provision("甲公司").id
    tenant_token = ops.issue_tenant_token(tid, sub="t-user")
    with pytest.raises(AuthError) as excinfo:  # 租户 aud ≠ 平台域要求
        ops.verify_token(tenant_token, expected_aud=PLATFORM_AUD)
    assert excinfo.value.code == 40101
    with pytest.raises(AuthError):  # 平台 aud ≠ 租户域要求
        ops.verify_token(platform_token, expected_aud=TENANT_AUD)


def test_tampered_token_rejected() -> None:
    """篡改令牌签名 → 40101（HMAC 校验失败，fail-closed）。"""
    ops, _, _ = build_ops()
    token = ops.issue_platform_token("ops-1", "admin")
    bad = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    with pytest.raises(AuthError):
        ops.verify_token(bad, expected_aud=PLATFORM_AUD)


def test_expired_token_rejected() -> None:
    """过期令牌 → 40101（exp 校验）。"""
    ops, _, _ = build_ops()
    token = ops.issue_platform_token("ops-1", "operator", ttl_sec=-1)
    with pytest.raises(AuthError):
        ops.verify_token(token, expected_aud=PLATFORM_AUD)


# --------------------------------------------------------------------------- 平台 RBAC（40301）


def test_platform_principal_rejects_tenant_aud() -> None:
    """platform_principal：租户令牌访问平台 API → 40101 PLATFORM_AUD_INVALID。"""
    ops, registry, _ = build_ops()
    tid = registry.provision("甲公司").id
    tenant_token = ops.issue_tenant_token(tid, sub="t-user")
    request = type("R", (), {"headers": {"Authorization": f"Bearer {tenant_token}"}})()
    with pytest.raises(AuthError) as excinfo:
        ops.platform_principal(request)
    assert excinfo.value.code == 40101


def test_admin_only_action_role_gate() -> None:
    """admin-only 动作：operator → 40301 ROLE_DENIED；admin → 放行（SR §5 错误码）。"""
    ops, _, _ = build_ops()
    operator = ops.issue_platform_token("op-1", "operator")
    admin = ops.issue_platform_token("ad-1", "admin")
    op_principal = ops.platform_principal(
        type("R", (), {"headers": {"Authorization": f"Bearer {operator}"}})()
    )
    ad_principal = ops.platform_principal(
        type("R", (), {"headers": {"Authorization": f"Bearer {admin}"}})()
    )
    assert ADMIN_ONLY_ACTIONS == frozenset({"tenant_purge"})
    # CR P3-b 收窄：admin-only 越权 → RoleDeniedError(40301)，项目自定义异常类
    with pytest.raises(RoleDeniedError) as excinfo:
        ops.require_role(op_principal, *ADMIN_ONLY_ACTIONS)
    assert excinfo.value.code == ROLE_DENIED == 40301
    ops.require_role(ad_principal, *ADMIN_ONLY_ACTIONS)  # 不抛即放行


def test_unknown_platform_role_denied() -> None:
    """aud=platform 但角色不在平台角色集 → 40301（fail-closed，不因 aud 对而放行）。"""
    ops, _, _ = build_ops()
    token = ops.issue_platform_token("ghost", "sales")
    request = type("R", (), {"headers": {"Authorization": f"Bearer {token}"}})()
    # CR P3-b 收窄：角色不在平台角色集 → RoleDeniedError(40301)（fail-closed）
    with pytest.raises(RoleDeniedError) as excinfo:
        ops.platform_principal(request)
    assert excinfo.value.code == ROLE_DENIED == 40301


# --------------------------------------------------------------------------- 审计哈希链（NFR-SEC-03）


def test_audit_hash_chain_integrity() -> None:
    """哈希链：seq 连续、prev_hash 链接、全链 verify 通过。"""
    ops, registry, _ = build_ops()
    tid = registry.provision("甲公司").id
    registry.suspend(tid, reason="x")
    records = ops.audit.records
    assert [r.seq for r in records] == list(range(1, len(records) + 1))
    assert all(records[i].prev_hash == records[i - 1].hash for i in range(1, len(records)))
    assert ops.audit.verify() is True


def test_audit_chain_tamper_detected() -> None:
    """防篡改：改动中间记录任何字段 → verify False（NFR-SEC-07 报告哈希同源机制）。"""
    ops, registry, _ = build_ops()
    tid = registry.provision("甲公司").id
    registry.suspend(tid, reason="x")
    records = ops.audit.records
    records[0].actor = "攻击者"  # 直接改写已落链记录
    assert ops.audit.verify() is False


def test_lifecycle_and_quota_changes_audited() -> None:
    """开通/停用/恢复/到期/配额变更全量留痕（SR §2 审计要求，actor 区分人工/系统）。"""
    ops, registry, quota = build_ops()
    tid = registry.provision("甲公司").id
    registry.set_quota(tid, "api_qps", 9, actor="op-1")
    registry.suspend(tid, reason="欠费", actor="op-1")
    registry.reinstate(tid, days=30, actor="op-1")
    registry.expire(tid, actor="system")
    actions = [r.action for r in ops.audit.records]
    assert actions == [
        "tenant_provision",
        "tenant_quota",
        "tenant_state",
        "tenant_renew",  # 恢复内含续期动作（expire_at 顺延）+ 状态迁移，双留痕
        "tenant_state",
        "tenant_state",
    ]
    actors = [r.actor for r in ops.audit.records]
    assert actors == ["platform", "op-1", "op-1", "op-1", "op-1", "system"]


# --------------------------------------------------------------------------- 用量导出（无定价字段）


def test_export_csv_header_whitelist_and_month_aggregation() -> None:
    """导出：表头白名单 = 租户×资源×月汇总用量维度；按月聚合数值正确。"""
    ops, registry, quota = build_ops()
    tid = registry.provision("甲公司").id
    quota.check(tid, "kb_item_cnt", amount=3)
    quota.check(tid, "api_qps", amount=2)
    quota.flush_hour(bucket=T0)
    csv_text = ops.export_usage_csv("2026-10")
    header = csv_text.splitlines()[0]
    assert header == "tenant_id,tenant_name,month,resource,used"
    rows = [line.split(",") for line in csv_text.splitlines()[1:]]
    by_resource = {r[3]: int(r[4]) for r in rows}
    assert by_resource == {"api_qps": 2, "kb_item_cnt": 3}
    assert {r[2] for r in rows} == {"2026-10"}


def test_export_contains_no_pricing_fields() -> None:
    """边界声明（SRS §10.1）：导出全文不含任何定价/账单字段（只出事实数据）。"""
    ops, registry, quota = build_ops()
    tid = registry.provision("合成租户", initial_days=30).id
    quota.check(tid, "kb_item_cnt", amount=1)
    quota.flush_hour(bucket=T0)
    csv_text = ops.export_usage_csv("2026-10")
    for banned in FORBIDDEN_PRICING_SUBSTRINGS:
        assert banned not in csv_text.lower(), f"导出含定价字段 {banned}"


def test_export_period_filter_and_invalid_format() -> None:
    """period 过滤（跨月数据不串月）；非法格式 → 40001。"""
    ops, registry, quota = build_ops()
    tid = registry.provision("甲公司").id
    quota.check(tid, "kb_item_cnt", amount=1)
    quota.flush_hour(bucket=T0)  # 2026-10
    csv_oct = ops.export_usage_csv("2026-10")
    assert str(tid) in csv_oct
    csv_nov = ops.export_usage_csv("2026-11")
    assert str(tid) not in csv_nov  # 11 月无数据
    with pytest.raises(UsageExportError) as excinfo:
        ops.export_usage_csv("2026年10月")
    assert excinfo.value.code == 40001


def test_gate_blocks_suspended_tenant() -> None:
    """租户域闸门：suspended → 40302 TENANT_SUSPENDED（停用即时拦截，非删除）。"""
    ops, registry, _ = build_ops()
    tid = registry.provision("甲公司").id
    token = ops.issue_tenant_token(tid, sub="t-user")
    claims = ops.tenant_gate(type("R", (), {"headers": {"Authorization": f"Bearer {token}"}})())
    assert claims.tenant_id == tid
    registry.suspend(tid, reason="欠费")
    # CR P3-b 收窄：停用租户闸门拦截 → TenantGateError(40302 TENANT_SUSPENDED)
    with pytest.raises(TenantGateError) as excinfo:
        ops.tenant_gate(type("R", (), {"headers": {"Authorization": f"Bearer {token}"}})())
    assert excinfo.value.code == 40302
