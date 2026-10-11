# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""租户注册表与状态机单元测试（FR-SAAS-001/003 §6.1；TDD 阶段①②，五类场景齐备）。

覆盖：active→suspended→expired 合法迁移与非法迁移拒绝（40901）、停用=闸门拦截
非删除（数据保留）、续期恢复（保留期内）、保留期外拒绝（40903）、并发停用续期
恰一胜（乐观快照 + 锁内复核串行化）、到期扫描（system actor）、全迁移审计留痕。
验收口径映射：SRS §16.6 运营后台功能可用（租户管理三操作）+ 停用不丢数据。

实现方案：docs/07-实现方案/SAAS/SR-SAAS-003-标准化运营后台.md §6.1/§9
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from services.saas.core.tenant.registry import (
    CUSTOMER_TENANT_ID,
    DEFAULT_PLAN,
    LEGAL_TRANSITIONS,
    RETAIN_WINDOW_PASSED,
    TENANT_NAME_DUP,
    TENANT_NOT_FOUND,
    TENANT_STATE_CONFLICT,
    TenantNotFoundError,
    TenantRegistry,
    TenantStateConflict,
)

T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


def make_registry(**kw) -> TenantRegistry:
    kw.setdefault("clock", lambda: T0)
    return TenantRegistry(**kw)


# --------------------------------------------------------------------------- 等价类（合法迁移）


def test_provision_initial_state() -> None:
    """开通 → active；expire_at = now + initial_days；plan 缺省 standard；id 从 2 起。"""
    reg = make_registry()
    tenant = reg.provision("示例外贸公司", initial_days=365)
    assert tenant.id == CUSTOMER_TENANT_ID + 1 == 2  # 租户 1 = 甲方本体（预置）
    assert tenant.status == "active"
    assert tenant.plan == DEFAULT_PLAN == "standard"
    assert tenant.expire_at == T0 + timedelta(days=365)


def test_suspend_sets_freeze_fields() -> None:
    """停用 → suspended；suspended_at/suspend_reason/data_retain_until 齐备（+90 天）。"""
    reg = make_registry()
    tenant = reg.provision("甲公司")
    frozen = reg.suspend(tenant.id, reason="欠费停用", actor="op-1")
    assert frozen.status == "suspended"
    assert frozen.suspend_reason == "欠费停用"
    assert frozen.data_retain_until == T0 + timedelta(days=90)
    assert frozen.suspended_at == T0


def test_reinstate_restores_active_and_extends() -> None:
    """续期恢复：suspended → active；expire_at 顺延；保留期锚点清空（解冻）。"""
    reg = make_registry()
    tid = reg.provision("甲公司").id
    reg.suspend(tid, reason="欠费")
    restored = reg.reinstate(tid, days=30, actor="op-1")
    assert restored.status == "active"
    assert restored.expire_at == T0 + timedelta(days=30)
    assert restored.data_retain_until is None


def test_renew_on_active_only_extends() -> None:
    """活跃租户续期：仅顺延 expire_at，状态不变。"""
    reg = make_registry()
    tid = reg.provision("甲公司", initial_days=100).id
    renewed = reg.renew(tid, days=50)
    assert renewed.status == "active"
    assert renewed.expire_at == T0 + timedelta(days=150)


def test_expire_then_renew_within_retain_window() -> None:
    """到期扫描 → expired；保留期内 renew 恢复 active（SR §5 续期/恢复）。"""
    reg = make_registry()
    tid = reg.provision("甲公司", initial_days=10).id
    expired = reg.expire(tid)
    assert expired.status == "expired"
    assert expired.data_retain_until == T0 + timedelta(days=180)  # expired 追加保留期
    restored = reg.renew(tid, days=365)
    assert restored.status == "active" and restored.expire_at == T0 + timedelta(days=365)


# --------------------------------------------------------------------------- 边界（幂等/表完备/到期扫描）


def test_suspend_idempotent_no_duplicate_state_audit() -> None:
    """停用幂等（SR §9）：重复 suspend 返回当前态，状态迁移审计不重复记录。"""
    audits: list[tuple[str, str, str]] = []
    reg = make_registry(audit=lambda actor, action, target, detail: audits.append((actor, action, target)))
    tid = reg.provision("甲公司").id
    reg.suspend(tid, reason="第一次")
    again = reg.suspend(tid, reason="第二次")
    assert again.status == "suspended"
    state_audits = [a for a in audits if a[1] == "tenant_state"]
    assert len(state_audits) == 1  # 幂等路径不重复记迁移


def test_transition_table_completeness() -> None:
    """迁移表完备性：恰 5 条合法边；3 态×2 目标全组合逐一核对（表驱动，非法 409）。"""
    assert LEGAL_TRANSITIONS == frozenset(
        {
            ("active", "suspended"),
            ("active", "expired"),
            ("suspended", "active"),
            ("suspended", "expired"),
            ("expired", "active"),
        }
    )
    reg = make_registry()
    states = ("active", "suspended", "expired")
    for from_state in states:
        for to_state in states:
            if to_state == from_state:
                continue  # 自环由幂等/冲突语义单独覆盖，不在表内
            # 每条边独立摆态，避免前序迁移污染（名称须唯一：名称是开通幂等锚点）
            tid = reg.provision(f"甲公司-{from_state}-{to_state}").id
            if from_state == "suspended":
                reg.suspend(tid)
            elif from_state == "expired":
                reg.expire(tid)
            if (from_state, to_state) in LEGAL_TRANSITIONS:
                result = reg.transition(tid, to_state, expected_status=from_state)
                assert result.status == to_state
            else:
                with pytest.raises(TenantStateConflict):
                    reg.transition(tid, to_state, expected_status=from_state)


def test_expire_scan_marks_overdue_active_with_system_actor() -> None:
    """到期扫描：过 expire_at 的 active → expired，审计 actor=system（自动迁移）。"""
    now = T0
    reg = make_registry(clock=lambda: now)
    audits: list[tuple[str, str, str]] = []
    reg.audit = lambda actor, action, target, detail: audits.append((actor, action, target))
    tid_live = reg.provision("未到期", initial_days=30).id
    tid_due = reg.provision("已到期", initial_days=5).id
    now = T0 + timedelta(days=10)
    expired_list = reg.expire_scan()
    assert [t.id for t in expired_list] == [tid_due]
    assert reg.get(tid_live).status == "active"
    assert any(a == ("system", "tenant_state", str(tid_due)) for a in audits)


# --------------------------------------------------------------------------- 异常（非法迁移/未知租户/保留期外）


def test_illegal_transitions_and_invalid_reinstate_rejected() -> None:
    """非法迁移拒绝（40901）：自环边非法；快照过期同样拒绝；
    对 active 租户执行「恢复」→ 40901（恢复仅面向 suspended/expired）。"""
    reg = make_registry()
    tid = reg.provision("甲公司").id
    with pytest.raises(TenantStateConflict) as excinfo:
        reg.transition(tid, "active", expected_status="active")  # 自环不在合法边内
    assert excinfo.value.code == TENANT_STATE_CONFLICT == 40901
    with pytest.raises(TenantStateConflict) as excinfo:
        reg.transition(tid, "suspended", expected_status="expired")  # 期望态与实际态不符
    assert excinfo.value.code == TENANT_STATE_CONFLICT
    with pytest.raises(TenantStateConflict):
        reg.reinstate(tid, days=30)  # active 无「恢复」语义


def test_renew_after_retain_window_rejected() -> None:
    """保留期外恢复拒绝（SR §9）：expired 且 data_retain_until 已过 → 40903。"""
    now = T0
    reg = make_registry(clock=lambda: now)
    tid = reg.provision("甲公司", initial_days=5).id
    reg.expire(tid)
    now = T0 + timedelta(days=181)  # 超过 expired 追加保留期 180 天
    with pytest.raises(TenantStateConflict) as excinfo:
        reg.renew(tid, days=30)
    assert excinfo.value.code == RETAIN_WINDOW_PASSED == 40903


def test_provision_duplicate_name_conflict() -> None:
    """名称重复 → 40902 TENANT_NAME_DUP（开通幂等锚点：名称唯一，SR §6.1）。"""
    reg = make_registry()
    reg.provision("示例外贸公司")
    with pytest.raises(TenantStateConflict) as excinfo:
        reg.provision("示例外贸公司")
    assert excinfo.value.code == TENANT_NAME_DUP == 40902


def test_unknown_tenant_not_found() -> None:
    """未知租户 → 40401 TENANT_NOT_FOUND。"""
    reg = make_registry()
    with pytest.raises(TenantNotFoundError) as excinfo:
        reg.suspend(999, reason="x")
    assert excinfo.value.code == TENANT_NOT_FOUND == 40401


# --------------------------------------------------------------------------- 停用=闸门拦截非删除


def test_suspend_is_gate_not_deletion() -> None:
    """停用=资源冻结而非删除（SR §2 决策 2）：行仍在、配额覆盖仍在、数据可查，
    恢复后全部原样可用——仅状态闸门拦截（闸门语义在中间件层验证）。"""
    reg = make_registry()
    tid = reg.provision("甲公司").id
    reg.set_quota(tid, "api_qps", 3, actor="op")
    reg.suspend(tid, reason="演练")
    row = reg.get(tid)
    assert row is not None and row.status == "suspended"  # 行未删
    assert reg.quota_of(tid, "api_qps") == 3  # 配置未删
    restored = reg.reinstate(tid, days=1)
    assert restored.status == "active" and restored.expire_at == T0 + timedelta(days=1)
    assert reg.quota_of(tid, "api_qps") == 3


# --------------------------------------------------------------------------- 并发（恰一胜）


def test_concurrent_suspend_and_reinstate_exactly_one_wins() -> None:
    """并发停用 vs 恢复恰一胜（SR §9 并发停用/续期竞争）：
    已停用租户上并发「重复停用（幂等）」与「恢复」——乐观快照 + 锁内复核
    （等价 FOR UPDATE 串行）下状态迁移审计恰 1 条、终态恒为恢复后 active、
    观察到过期快照的一方收 409。"""
    audits: list[str] = []
    reg = make_registry()
    reg.audit = lambda actor, action, target, detail: audits.append(action)
    tid = reg.provision("甲公司").id
    reg.suspend(tid, reason="初始停用")
    audits.clear()  # 竞争窗口从「已停用」基线起算
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}
    # 乐观并发语义：双方在竞争前各自持有同 observed 快照（等价事务读取行后提交）
    snapshot = reg.get(tid).status  # "suspended"

    def do_suspend() -> None:
        try:
            reg.suspend(tid, reason="并发重复停用", expected_status=snapshot)
            outcomes["suspend"] = "ok"
        except TenantStateConflict:
            outcomes["suspend"] = "conflict"

    def do_reinstate() -> None:
        try:
            reg.reinstate(tid, days=30, expected_status=snapshot)
            outcomes["reinstate"] = "ok"
        except TenantStateConflict:
            outcomes["reinstate"] = "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(lambda: (barrier.wait(), do_suspend()))
        f2 = pool.submit(lambda: (barrier.wait(), do_reinstate()))
        for f in (f1, f2):
            f.result()

    # 两条执行路径均可，但不变式恒成立：
    # ① 恢复恒成功（先到则迁移、后到则基线未变仍迁移）；② 终态恒 active；
    # ③ 状态迁移审计恰 1 条（恰一胜）；④ 重复停用要么幂等成功要么 409，绝不产生第二次迁移。
    assert outcomes["reinstate"] == "ok"
    assert outcomes["suspend"] in {"ok", "conflict"}
    assert reg.get(tid).status == "active"
    assert len([a for a in audits if a == "tenant_state"]) == 1


# --------------------------------------------------------------------------- 审计联动


def test_lifecycle_transitions_all_audited() -> None:
    """开通/停用/恢复/到期/配额变更全量写审计（NFR-SEC-03 联动）。"""
    audits: list[tuple[str, str]] = []
    reg = make_registry()
    reg.audit = lambda actor, action, target, detail: audits.append((action, target))
    tid = reg.provision("甲公司").id
    reg.set_quota(tid, "api_qps", 9, actor="op")
    reg.suspend(tid, reason="x")
    reg.reinstate(tid, days=1)
    reg.expire(tid)
    actions = [a for a, _ in audits]
    assert actions == [
        "tenant_provision",
        "tenant_quota",
        "tenant_state",
        "tenant_renew",  # 恢复内含续期动作（expire_at 顺延）+ 状态迁移，双留痕
        "tenant_state",
        "tenant_state",
    ]
