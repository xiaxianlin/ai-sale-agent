# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""主管看板数据权限用例（FR-TR-006 / NFR-SEC-02，SR-TR-006 §6.2/§9；TDD 阶段①，迭代 1.3）。

验收口径映射（SRS §16.2 / SR §1）：主管仅可见本团队学员数据；越权访问返回 40301
并写审计（rbac_deny）；空团队不退化为全量；管理员全量。
"""
from __future__ import annotations

import pytest

from services.training.core.board.aggregate import (
    ScopeDenied,
    ensure_in_scope,
    resolve_scope,
)

from .test_board_fakes import AuditRecorder, FakeTeamDirectory


def claims(roles: list[str], team_ids: list[int], sub: str = "sup-1") -> dict[str, object]:
    """JWT claims 形态（06 §3：roles + data_scope.team_ids + sub）。"""
    return {"sub": sub, "roles": roles, "data_scope": {"team_ids": team_ids}}


DIRECTORY = FakeTeamDirectory({1: {101, 102}, 2: {201, 202}})


def test_resolve_supervisor_scope_members() -> None:
    """主管 A（团队 1）→ 本团队学员集合 {101, 102}，非管理员。"""
    scope = resolve_scope(claims(["supervisor"], [1]), DIRECTORY)
    assert scope.trainee_ids == frozenset({101, 102})
    assert scope.team_ids == (1,)
    assert scope.is_admin is False


def test_resolve_admin_scope_all() -> None:
    """管理员全量：任意学员均在范围内（in_scope 放行）。"""
    scope = resolve_scope(claims(["admin"], [], sub="root"), DIRECTORY)
    assert scope.is_admin is True
    assert 201 in scope.trainee_ids
    ensure_in_scope(scope, 201)  # 不抛即放行


def test_resolve_non_supervisor_denied() -> None:
    """普通角色（无 supervisor/admin）→ 40301，拒绝访问主管看板。"""
    with pytest.raises(ScopeDenied) as info:
        resolve_scope(claims(["trainee"], [1]), DIRECTORY)
    assert info.value.code == 40301


def test_resolve_empty_team_denied_not_full() -> None:
    """名下无团队学员 → 40301，绝不退化为全量（SR §9 test_scope_denied_40301）。"""
    with pytest.raises(ScopeDenied) as info:
        resolve_scope(claims(["supervisor"], [9]), DIRECTORY)
    assert info.value.code == 40301


def test_ensure_in_scope_pass_and_deny_with_audit() -> None:
    """范围校验：本团队学员放行；越权 → 审计 rbac_deny 留痕 + 40301。"""
    audit = AuditRecorder()
    scope = resolve_scope(claims(["supervisor"], [1]), DIRECTORY)
    ensure_in_scope(scope, 101, audit_cb=audit)  # 本团队放行
    with pytest.raises(ScopeDenied) as info:
        ensure_in_scope(scope, 201, audit_cb=audit)  # 团队 2 学员 → 越权
    assert info.value.code == 40301
    records = audit.of_type("rbac_deny")
    assert len(records) == 1
    assert "201" in records[0][1]  # 审计明细携带越权目标


def test_ensure_in_scope_deny_without_audit_hook() -> None:
    """未注入审计钩子时越权仍拒绝（审计缺失不放大权限）。"""
    scope = resolve_scope(claims(["supervisor"], [1]), DIRECTORY)
    with pytest.raises(ScopeDenied) as info:
        ensure_in_scope(scope, 202)
    assert info.value.code == 40301


def test_two_supervisors_isolated() -> None:
    """双主管互不可见：A（团队 1）与 B（团队 2）解析出的学员集合不相交。"""
    scope_a = resolve_scope(claims(["supervisor"], [1], sub="sup-a"), DIRECTORY)
    scope_b = resolve_scope(claims(["supervisor"], [2], sub="sup-b"), DIRECTORY)
    assert scope_a.trainee_ids.isdisjoint(scope_b.trainee_ids)
    with pytest.raises(ScopeDenied):
        ensure_in_scope(scope_a, 202)  # A 查 B 的学员 → 拒绝
