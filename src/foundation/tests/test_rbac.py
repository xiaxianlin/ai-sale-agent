"""知识产权归甲方所有（COM-IP-01）

NFR-SEC-02 权限控制（RBAC）单元/集成测试。

覆盖（SR-NFR-SEC-02 §9 测试要点 + §6.6 越权矩阵扫描）：
- 矩阵：继承展开 / 成环检测 / 未知角色 / 枚举一致性 / 多角色最大范围；
- 数据范围：scope_criterion 表达式（SELF/TEAM/TENANT 锚点）与 scope_filter 行过滤、
  锚点列缺失退化为租户边界、空组不放行、未知范围 fail-closed；
- require_role：放行 / 越权 40301 / 匿名拒绝 / OR 语义 / CI 扫描标记；
- FastAPI 集成（TestClient）：角色×端点越权矩阵扫描 + 越权拒绝写审计（NFR-SEC-03 联动）。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterator, Mapping

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient

from foundation.audit import AuditEvent, AuditEventType, AuditWriter, InMemoryAuditStorage
from foundation.security import (
    ROLE_PERMISSIONS,
    DataScope,
    PermissionDeniedError,
    Principal,
    Role,
    RoleCycleError,
    RoleDef,
    ScopeFilterError,
    UnknownRoleError,
    clear_current_principal,
    is_privileged,
    max_scope,
    principal_context,
    rbac_exception_handler,
    require_role,
    require_role_dependency,
    resolve_perms,
    scope_criterion,
    scope_filter,
    set_current_principal,
    set_deny_auditor,
)


def make_principal(
    role: str,
    uid: str = "1001",
    tenant_id: int = 1,
    team_ids: tuple[int, ...] = (),
    group_ids: tuple[int, ...] = (),
) -> Principal:
    """按矩阵构造单角色主体（等价 SR §6.2 鉴权中间件对 JWT 声明的展开）。"""
    return Principal(
        uid=uid,
        roles=(role,),
        perms=frozenset(resolve_perms(role)),
        scope=max_scope((role,)),
        tenant_id=tenant_id,
        team_ids=team_ids,
        group_ids=group_ids,
        privileged=is_privileged((role,)),
    )


@pytest.fixture(autouse=True)
def _reset_deny_auditor() -> Iterator[None]:
    """每个用例后清理越权审计钩子，避免用例间串扰。"""
    yield
    set_deny_auditor(None)


# --------------------------------------------------------------------------- 矩阵单元测试


class TestMatrix:
    def test_resolve_perms_inheritance(self) -> None:
        """主管继承销售全部动作（SR §9 test_resolve_perms_inheritance）。"""
        supervisor = resolve_perms("supervisor")
        assert resolve_perms("sales") <= supervisor
        assert {"dashboard:read", "team:read", "account:monitor", "alert:receive"} <= supervisor

    def test_tenant_admin_inherits_sys_and_kb(self) -> None:
        """租户管理员 ⊇ 系统管理员 + 知识库管理员（SR §6.1 继承链展开）。"""
        perms = resolve_perms("tenant_admin")
        assert resolve_perms("sys_admin") <= perms
        assert resolve_perms("kb_admin") <= perms

    def test_role_cycle_detected(self) -> None:
        """构造 A→B→A 继承：必须抛 RoleCycleError（SR §9 test_role_cycle_detected）。"""
        cyclic: dict[str, RoleDef] = {
            "a": RoleDef(name="A", perms=frozenset({"a:do"}), scope=DataScope.SELF, extends=("b",)),
            "b": RoleDef(name="B", perms=frozenset({"b:do"}), scope=DataScope.SELF, extends=("a",)),
        }
        with pytest.raises(RoleCycleError):
            resolve_perms("a", matrix=cyclic)

    def test_unknown_role_rejected(self) -> None:
        """未声明角色 fail-closed（防配置漂移）。"""
        with pytest.raises(UnknownRoleError):
            resolve_perms("ghost")

    def test_matrix_covers_role_enum(self) -> None:
        """Role 枚举与矩阵键一一对应（导入期自检的用例化表达）。"""
        assert {r.value for r in Role} == set(ROLE_PERMISSIONS)

    def test_max_scope_takes_widest(self) -> None:
        """多角色取最大数据范围（SR §6.2：主管+销售取 TEAM）。"""
        assert max_scope(("supervisor", "sales")) is DataScope.TEAM
        assert max_scope(("sales",)) is DataScope.ACCOUNT_GROUP
        assert max_scope(()) is DataScope.SELF  # 空角色集最小权限兜底

    def test_privileged_flags(self) -> None:
        """管理类角色标记（RLS app.privileged 来源）。"""
        assert is_privileged(("sys_admin",)) is True
        assert is_privileged(("kb_admin", "platform_ops")) is True
        assert is_privileged(("sales", "supervisor")) is False


# --------------------------------------------------------------------------- 数据范围过滤单元测试


class TestScopeFilter:
    ROWS: list[dict[str, object]] = [
        {"id": 1, "tenant_id": 1, "owner_user_id": "1001", "account_group_id": 10, "team_id": 7},
        {"id": 2, "tenant_id": 1, "owner_user_id": "1002", "account_group_id": 10, "team_id": 7},
        {"id": 3, "tenant_id": 1, "owner_user_id": "1002", "account_group_id": 20, "team_id": 8},
        # 他租户行：任何范围都不可见（租户边界恒生效，SR §4 RLS 同构）
        {"id": 4, "tenant_id": 2, "owner_user_id": "1001", "account_group_id": 10, "team_id": 7},
    ]

    def test_scope_criterion_self(self) -> None:
        """学员 Principal：条件含 owner_user_id == uid（SR §9 test_scope_criterion_self）。"""
        principal = make_principal("trainee", uid="1001")
        criterion = scope_criterion(principal)
        assert "owner_user_id" in criterion.expression and "1001" in criterion.expression
        assert [r["id"] for r in scope_filter(self.ROWS, principal)] == [1]

    def test_scope_criterion_group(self) -> None:
        """销售 Principal：仅本账号组会话（P2 起 4 主 + 24 子账号的组边界）。"""
        principal = make_principal("sales", uid="1002", group_ids=(10,))
        criterion = scope_criterion(principal)
        assert "account_group_id" in criterion.expression
        assert [r["id"] for r in scope_filter(self.ROWS, principal)] == [1, 2]

    def test_scope_criterion_team(self) -> None:
        """主管 Principal：本团队数据（看板/团队成绩范围）。"""
        principal = make_principal("supervisor", uid="1003", team_ids=(7,))
        criterion = scope_criterion(principal)
        assert "team_id" in criterion.expression
        assert [r["id"] for r in scope_filter(self.ROWS, principal)] == [1, 2]

    def test_scope_criterion_tenant_lock(self) -> None:
        """平台角色仍受租户列约束（SR §9 test_scope_criterion_tenant_lock：PLATFORM 不越租户）。"""
        principal = make_principal("platform_ops", tenant_id=1)
        criterion = scope_criterion(principal)
        assert "tenant_id" in criterion.expression and "1" in criterion.expression
        assert [r["id"] for r in scope_filter(self.ROWS, principal)] == [1, 2, 3]  # 租户 2 不可见

    def test_scope_missing_anchor_falls_back_to_tenant(self) -> None:
        """未声明锚点列的表 → 默认仅租户边界（SR §6.3 最小权限）。"""
        rows: list[dict[str, object]] = [{"tenant_id": 1, "v": "x"}, {"tenant_id": 2, "v": "y"}]
        assert [r["v"] for r in scope_filter(rows, make_principal("trainee"))] == ["x"]

    def test_scope_empty_group_matches_nothing(self) -> None:
        """账号组为空：有锚点列但无组归属 → 匹配不到任何行（最小权限）。"""
        principal = make_principal("sales", group_ids=())
        assert scope_filter(self.ROWS, principal) == []

    def test_scope_filter_fail_closed(self) -> None:
        """未知数据范围：过滤器抛错拒绝（fail-closed，SR §8 可靠性）。"""
        principal = make_principal("trainee")
        object.__setattr__(principal, "scope", "bogus")  # 白盒注入非法范围（防御性用例）
        with pytest.raises(ScopeFilterError):
            scope_filter([{"tenant_id": 1}], principal)


# --------------------------------------------------------------------------- require_role 单元测试（无 HTTP）


class TestRequireRole:
    def test_allowed_calls_through(self) -> None:
        @require_role("kb:crud")
        def handler() -> str:
            return "ok"

        with principal_context(make_principal("kb_admin")):
            assert handler() == "ok"

    def test_denied_without_principal(self) -> None:
        """匿名（无主体）→ 40301（SR §6.2：p is None 即拒）。"""
        @require_role("kb:crud")
        def handler() -> str:
            return "ok"

        clear_current_principal()
        with pytest.raises(PermissionDeniedError) as excinfo:
            handler()
        assert excinfo.value.code == 40301
        assert "kb:crud" in excinfo.value.required_perms

    def test_denied_cross_role(self) -> None:
        """学员访问管理端点 → 40301。"""
        @require_role("user:manage")
        def handler() -> str:
            return "ok"

        with principal_context(make_principal("trainee")):
            with pytest.raises(PermissionDeniedError):
                handler()

    def test_or_semantics(self) -> None:
        """OR 语义：命中任一声明动作即放行（SR §6.2 require()）。"""
        @require_role("team:read", "kb:review")
        def handler() -> str:
            return "ok"

        with principal_context(make_principal("supervisor")):  # 命中 team:read
            assert handler() == "ok"

    def test_required_perms_marker_for_ci(self) -> None:
        """__required_perms__ 标记可被 CI 静态扫描读取（SR §6.6 未声明端点拒绝合并）。"""
        @require_role("rbac:manage")
        def handler() -> str:
            return "ok"

        assert handler.__required_perms__ == {"rbac:manage"}  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- FastAPI 集成：越权矩阵扫描 + 拒绝审计


MATRIX_ENDPOINTS: list[tuple[str, str, frozenset[str]]] = [
    ("POST", "/api/v1/exam/submit", frozenset({"exam:submit"})),
    ("GET", "/api/v1/conversations", frozenset({"conversation:read"})),
    ("GET", "/api/v1/team/dashboard", frozenset({"dashboard:read"})),
    ("POST", "/api/v1/kb/items", frozenset({"kb:crud"})),
    ("GET", "/api/v1/admin/users", frozenset({"user:manage"})),
    ("GET", "/api/v1/admin/audit-logs", frozenset({"audit:read"})),
]


def _build_matrix_app() -> FastAPI:
    """构造越权矩阵测试应用：六个典型端点覆盖六类角色（SR §6.6 矩阵扫描的受测面）。"""
    app = FastAPI()
    app.add_exception_handler(PermissionDeniedError, rbac_exception_handler)

    @app.middleware("http")
    async def _auth_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """模拟生产鉴权中间件（SR §6.2）：解析凭证注入 Principal（生产为 JWT + 版本号校验）。"""
        role = request.headers.get("X-Test-Role")
        if role is None:
            clear_current_principal()
        else:
            set_current_principal(make_principal(role))
        return await call_next(request)

    @app.post("/api/v1/exam/submit")
    @require_role("exam:submit")
    def exam_submit() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/v1/conversations")
    @require_role("conversation:read")
    def list_conversations() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/v1/team/dashboard")
    @require_role("dashboard:read")
    def team_dashboard() -> dict[str, Any]:
        return {"ok": True}

    @app.post("/api/v1/kb/items")
    @require_role("kb:crud")
    def create_kb_item() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/v1/admin/users")
    @require_role("user:manage")
    def admin_users(request: Request) -> dict[str, Any]:
        # 声明 Request 参数：FastAPI 注入后由装饰器透传，供越权审计定位端点路径
        return {"ok": True}

    @app.get("/api/v1/admin/audit-logs")
    def admin_audit_logs(
        principal: Principal = Depends(require_role_dependency("audit:read")),
    ) -> dict[str, Any]:
        # FastAPI 依赖形态：通过后拿到 Principal 供业务使用（SR §6.2）
        return {"ok": True, "uid": principal.uid}

    return app


class TestPrivilegeEscalationMatrix:
    @pytest.mark.parametrize("role", sorted(ROLE_PERMISSIONS))
    @pytest.mark.parametrize("method,path,perms", MATRIX_ENDPOINTS)
    def test_no_privilege_escalation(self, role: str, method: str, path: str, perms: frozenset[str]) -> None:
        """角色×端点越权矩阵扫描（SR §6.6）：放行 ⇔ 声明动作与角色权限有交集。

        被拒侧必须返回统一错误体 code=40301（04-接口设计 §1.3）；
        放行侧正常 200——矩阵任何一格出现越权放行即本用例失败。
        """
        client = TestClient(_build_matrix_app())
        allowed = bool(perms & resolve_perms(role))
        resp = client.request(method, path, headers={"X-Test-Role": role})
        if allowed:
            assert resp.status_code == 200, f"{role} 访问 {path} 应放行: {resp.text}"
        else:
            assert resp.status_code == 403, f"{role} 访问 {path} 应被拒: {resp.text}"
            body = resp.json()
            assert body["code"] == 40301
            assert body["message"] == "RBAC 权限不足"
            assert body["data"] is None
            assert body["trace_id"]

    def test_anonymous_denied_40301(self) -> None:
        """无凭证（匿名）访问管理端点 → 403 + 统一错误体。"""
        client = TestClient(_build_matrix_app())
        resp = client.get("/api/v1/admin/users")
        assert resp.status_code == 403
        assert resp.json()["code"] == 40301

    def test_dependency_form_returns_principal(self) -> None:
        """FastAPI 依赖形态：sys_admin 通过 audit:read 依赖并拿到主体。"""
        client = TestClient(_build_matrix_app())
        resp = client.get("/api/v1/admin/audit-logs", headers={"X-Test-Role": "sys_admin"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "uid": "1001"}

    def test_ci_scan_all_routes_declared(self) -> None:
        """模拟 tools/check_rbac_declared.py：装饰器形态端点全部带权限声明标记（SR §6.6）。"""
        app = _build_matrix_app()
        for route in app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/api/") and path != "/api/v1/admin/audit-logs":
                assert getattr(route.endpoint, "__required_perms__", None), f"{path} 缺少权限声明"


def test_require_deny_audits() -> None:
    """越权尝试写审计（SR §9 test_require_deny_audits；联动 NFR-SEC-03 哈希链）。

    学员访问 user:manage 端点 → 403 + 审计链出现 perm_change/denied 行（detail.kind=rbac_deny）。
    """
    writer = AuditWriter(InMemoryAuditStorage())

    def _deny_hook(record: Mapping[str, object]) -> None:
        detail = dict(record["detail"])  # type: ignore[arg-type]
        writer.append(
            AuditEvent(
                actor=str(record["actor"]),
                action_type=AuditEventType.PERM_CHANGE,
                target=str(detail.get("path", "-")),
                result="denied",
                detail=detail,
            )
        )

    set_deny_auditor(_deny_hook)
    client = TestClient(_build_matrix_app())
    resp = client.get("/api/v1/admin/users", headers={"X-Test-Role": "trainee"})
    assert resp.status_code == 403

    rows = list(writer.storage.iter_rows())
    assert len(rows) == 1
    row = rows[0]
    assert row.action_type == "perm_change"
    assert row.result == "denied"
    assert row.detail["kind"] == "rbac_deny"
    assert "user:manage" in row.detail["required_perms"]
    assert row.target == "/api/v1/admin/users"
    assert writer.verify_chain().ok  # 审计行本身入链且可校验
