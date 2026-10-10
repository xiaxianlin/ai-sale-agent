"""知识产权归甲方所有（COM-IP-01）

NFR-SEC-02 权限控制（RBAC）——平台基座实现。

依据：docs/07-实现方案/NFR/SR-NFR-SEC-02-权限控制.md
- §6.1 声明式权限矩阵（角色×资源×动作，唯一事实源，库内只存绑定关系防配置漂移）；
- §6.2 require() 路由级动作校验（OR 语义 + 越权尝试写审计 + 40301）；
- §6.3 数据范围过滤器（scope 条件注入，业务代码无法绕过）；
- §6.6 最小权限落地检查（端点 __required_perms__ 标记供 CI 扫描与越权矩阵测试读取）。

三道防线落点（SR §2）：矩阵与 require_role/scope_filter（第一、二道）在本文件；
PG RLS 第二防线为数据库层策略（SR §4 DDL），随迁移脚本部署，不在本基座代码范围。
"""

from __future__ import annotations

import inspect
import itertools
import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Any, Callable, Iterable, Mapping, ParamSpec, TypeVar, cast

from fastapi import Request
from fastapi.responses import JSONResponse

from foundation.security.context import Principal, get_current_principal

_LOGGER = logging.getLogger(__name__)
P = ParamSpec("P")  # 被装饰路由函数的参数规格
R = TypeVar("R")    # 被装饰路由函数的返回类型


# --------------------------------------------------------------------------- 异常体系


class RbacError(Exception):
    """RBAC 基础异常（错误码语义对齐 04-接口设计 §1.3）。"""


class UnknownRoleError(RbacError):
    """未在 ROLE_PERMISSIONS 中声明的角色（fail-closed：新增角色必须先改矩阵并走评审）。"""


class RoleCycleError(RbacError):
    """角色继承成环（矩阵自检/展开时暴露，进程启动即失败）。"""


class ScopeFilterError(RbacError):
    """数据范围过滤器异常——fail-closed：抛错拒绝请求，绝不放行无范围查询（SR §8 可靠性）。"""


class PermissionDeniedError(RbacError):
    """RBAC 权限不足（40301）——越权尝试，抛出前已交由拒绝审计钩子留痕（SR §6.2）。"""

    code: int = 40301
    message: str = "RBAC 权限不足"

    def __init__(self, required_perms: Iterable[str], principal_uid: str | None, path: str = "") -> None:
        super().__init__(
            f"RBAC 权限不足: 需要 {sorted(required_perms)} 之一, 用户={principal_uid!r}, path={path!r}"
        )
        self.required_perms: tuple[str, ...] = tuple(sorted(required_perms))
        self.principal_uid = principal_uid
        self.path = path


# --------------------------------------------------------------------------- 声明式权限矩阵（SR §6.1）


class Role(str, Enum):
    """系统角色编码（SRS §4.1；06-安全 §3 六角色 + P5 平台运营补充，SR §2 偏差标注）。"""

    TRAINEE = "trainee"              # 学员
    SALES = "sales"                  # 销售（AI 会话操作者）
    SUPERVISOR = "supervisor"        # 主管
    KB_ADMIN = "kb_admin"            # 知识库管理员
    SYS_ADMIN = "sys_admin"          # 系统管理员
    TENANT_ADMIN = "tenant_admin"    # 租户管理员（P5 激活）
    PLATFORM_OPS = "platform_ops"    # 平台运营（P5，跨租户元数据运营）


class DataScope(str, Enum):
    """数据范围（SR §6.1）：决定 scope 过滤器的范围锚点列与 RLS 策略分支。"""

    SELF = "self"                    # 仅本人数据（owner_user_id）
    ACCOUNT_GROUP = "group"          # 本账号组会话（account_group_id，P2 起）
    TEAM = "team"                    # 本团队（team_id）
    TENANT = "tenant"                # 本租户全量（tenant_id）
    PLATFORM = "platform"            # 平台级（仍受 tenant_id 合规约束，SR §6.3）


# 范围大小排序：多角色取最大范围用（SR §6.2「主管+销售取 TEAM」）
_SCOPE_RANK: dict[DataScope, int] = {
    DataScope.SELF: 0,
    DataScope.ACCOUNT_GROUP: 1,
    DataScope.TEAM: 2,
    DataScope.TENANT: 3,
    DataScope.PLATFORM: 4,
}


@dataclass(frozen=True)
class RoleDef:
    """单个角色的声明式定义（矩阵元素）。

    perms 动作命名 `资源:动作`，与路由 require_role 声明一一对应；
    修改矩阵必须走 PR 评审（越权扫描与 CI 检查随之更新），矩阵变更随发版生效（SR §6.1）。
    """

    name: str                          # 角色中文名（展示用）
    perms: frozenset[str]              # 动作集合
    scope: DataScope                   # 数据范围
    extends: tuple[str, ...] = ()      # 角色继承（如主管 ⊇ 销售；冗余存展示用，权威在代码）


ROLE_PERMISSIONS: dict[str, RoleDef] = {
    "trainee": RoleDef(
        name="学员", scope=DataScope.SELF,
        perms=frozenset({"mentor:ask", "exercise:write", "exam:submit", "radar:read"}),
    ),
    "sales": RoleDef(
        name="销售", scope=DataScope.ACCOUNT_GROUP,
        perms=frozenset({"conversation:read", "conversation:takeover", "sensitive:confirm"}),
    ),
    "supervisor": RoleDef(
        name="主管", scope=DataScope.TEAM, extends=("sales",),  # 角色继承：主管含销售全部权限
        perms=frozenset({"dashboard:read", "team:read", "account:monitor", "alert:receive"}),
    ),
    "kb_admin": RoleDef(
        name="知识库管理员", scope=DataScope.TENANT,
        perms=frozenset({"kb:crud", "kb:import", "kb:version", "kb:review"}),  # 无系统配置权
    ),
    "sys_admin": RoleDef(
        name="系统管理员", scope=DataScope.PLATFORM,
        perms=frozenset(
            {"user:manage", "rbac:manage", "system:monitor", "audit:read", "credential:verify"}
        ),  # 凭证仅可见校验状态，明文不可见（NFR-SEC-04，SR §2 偏差标注）
    ),
    "tenant_admin": RoleDef(
        name="租户管理员（P5）", scope=DataScope.TENANT, extends=("sys_admin", "kb_admin"),
        perms=frozenset(),  # 权限全部来自继承；RLS 以 tenant_id 锁定（NFR-SEC-07）
    ),
    "platform_ops": RoleDef(
        name="平台运营（P5）", scope=DataScope.PLATFORM,
        perms=frozenset({"tenant:manage", "quota:manage", "usage:read"}),  # 仅租户元数据/配额/用量
    ),
}

# 管理类角色集合——RLS app.privileged 来源（SR §6.1 PRIVILEGED）
PRIVILEGED_ROLES: frozenset[str] = frozenset({"kb_admin", "sys_admin", "tenant_admin", "platform_ops"})


def _check_matrix_invariant() -> None:
    """导入期自检：Role 枚举与矩阵一一对应；继承引用的角色必须存在（防配置漂移）。"""
    for role in Role:
        if role.value not in ROLE_PERMISSIONS:
            raise RbacError(f"角色 {role.value} 未在 ROLE_PERMISSIONS 声明（枚举与矩阵不一致）")
    for code, definition in ROLE_PERMISSIONS.items():
        for parent in definition.extends:
            if parent not in ROLE_PERMISSIONS:
                raise RbacError(f"角色 {code} 继承了未声明的角色 {parent}")


_check_matrix_invariant()


def resolve_perms(role: str | Role, matrix: dict[str, RoleDef] | None = None) -> set[str]:
    """展开继承链返回角色全部可执行动作（SR §6.1 resolve_perms）。

    - 继承深度 ≤2（矩阵现状），进程内可缓存；
    - 成环抛 RoleCycleError（SR §9 test_role_cycle_detected 覆盖）；
    - 未声明角色抛 UnknownRoleError（fail-closed）；
    - matrix 参数供单测注入自定义矩阵（如成环用例），生产调用使用默认矩阵。
    """
    source = matrix if matrix is not None else ROLE_PERMISSIONS
    code = role.value if isinstance(role, Role) else role

    def _walk(current: str, seen: frozenset[str]) -> set[str]:
        if current in seen:
            raise RoleCycleError(f"角色继承成环: {'->'.join(seen)}->{current}")
        definition = source.get(current)
        if definition is None:
            raise UnknownRoleError(f"未声明的角色: {current!r}")
        out = set(definition.perms)
        for parent in definition.extends:
            out |= _walk(parent, seen | {current})
        return out

    return _walk(code, frozenset())


def max_scope(roles: Iterable[str | Role]) -> DataScope:
    """多角色取最大数据范围（SR §6.2：主管+销售场景取 TEAM）；空角色集兜底 SELF（最小权限）。"""
    codes = [r.value if isinstance(r, Role) else r for r in roles]
    if not codes:
        return DataScope.SELF
    unknown = [c for c in codes if c not in ROLE_PERMISSIONS]
    if unknown:
        raise UnknownRoleError(f"未声明的角色: {unknown}")
    return max((ROLE_PERMISSIONS[c].scope for c in codes), key=lambda s: _SCOPE_RANK[s])


def is_privileged(roles: Iterable[str | Role]) -> bool:
    """是否管理类角色（Principal.privileged / RLS app.privileged 来源，SR §6.1）。"""
    return any((r.value if isinstance(r, Role) else r) in PRIVILEGED_ROLES for r in roles)


# --------------------------------------------------------------------------- 数据范围过滤器（SR §6.3，第一防线）


@dataclass(frozen=True)
class ScopeCriterion:
    """数据范围判定条件：predicate 为行级谓词（等价 ORM 注入的 where 条件），expression 为可读描述。"""

    scope: DataScope
    expression: str
    predicate: Callable[[Mapping[str, object]], bool]


def _anchor_criterion(
    principal: Principal,
    column: str,
    match: Callable[[object], bool],
    anchor_expr: str,
) -> ScopeCriterion:
    """通用锚点谓词：租户边界恒生效 AND（锚点列存在则按锚点匹配，否则退化为仅租户边界）。

    与 PG RLS 策略同构（SR §4：tenant_id 边界恒生效 + owner/group/team 锚点）；
    「未声明锚点列的表默认仅租户边界」为最小权限下的保守放行（SR §6.3）。
    """
    tenant_id = principal.tenant_id

    def predicate(row: Mapping[str, object]) -> bool:
        if row.get("tenant_id") != tenant_id:  # 租户边界恒生效（含他租户同 uid 也不可见）
            return False
        if column not in row:  # 未声明锚点列：仅租户边界
            return True
        return match(row[column])

    return ScopeCriterion(principal.scope, f"tenant_id == {tenant_id} AND {anchor_expr}", predicate)


def scope_criterion(principal: Principal) -> ScopeCriterion:
    """按主体数据范围构造判定条件（第一防线核心谓词，SR §6.3）。

    生产形态为 SQLAlchemy do_orm_execute 事件对 SELECT 自动注入同语义 where 条件
    （绕过路径仅裸 SQL——被编码规范与 CI 静态扫描拦截，并有 RLS 兜底）；
    本基座以行集合谓词实现可测核心。未知范围 fail-closed 抛 ScopeFilterError。
    """
    match principal.scope:
        case DataScope.SELF:
            anchor = f"owner_user_id == {principal.uid!r}"
            return _anchor_criterion(principal, "owner_user_id", lambda v: v == principal.uid, anchor)
        case DataScope.ACCOUNT_GROUP:
            group_ids = tuple(sorted(principal.group_ids))
            anchor = f"account_group_id in {list(group_ids)!r}"
            return _anchor_criterion(principal, "account_group_id", lambda v: v in group_ids, anchor)
        case DataScope.TEAM:
            team_ids = tuple(sorted(principal.team_ids))
            return _anchor_criterion(
                principal, "team_id", lambda v: v in team_ids, f"team_id in {list(team_ids)!r}"
            )
        case DataScope.TENANT | DataScope.PLATFORM:
            # 平台角色仍受租户列约束（合规视图：PLATFORM 不越租户，SR §6.3/§6.5/§9）
            tenant_id = principal.tenant_id
            return ScopeCriterion(
                principal.scope,
                f"tenant_id == {tenant_id}",
                lambda row: row.get("tenant_id") == tenant_id,
            )
        case _:
            raise ScopeFilterError(f"未知数据范围（fail-closed）: {principal.scope!r}")


def scope_filter(rows: Iterable[Mapping[str, object]], principal: Principal) -> list[dict[str, object]]:
    """数据范围过滤器：按主体 scope 过滤行集合（返回行副本，防止业务侧改写原始数据）。"""
    criterion = scope_criterion(principal)
    return [dict(row) for row in rows if criterion.predicate(row)]


# --------------------------------------------------------------------------- 越权拒绝审计钩子（NFR-SEC-03 联动）


DenyAuditRecord = Mapping[str, object]
DenyAuditor = Callable[[DenyAuditRecord], None]
_on_access_denied: DenyAuditor | None = None


def set_deny_auditor(hook: DenyAuditor | None) -> None:
    """注册/清除越权拒绝审计钩子（默认关闭）。

    钩子负责把记录写入审计链：action_type=perm_change、result=denied、
    detail.kind='rbac_deny'——对齐 SR-NFR-SEC-02 §6.4 的 'rbac_deny' 命名，
    同时满足 SR-NFR-SEC-03 §4 的八类闭枚举约束（两文档口径在此收敛，见 audit.py 注释）。
    """
    global _on_access_denied
    _on_access_denied = hook


def _audit_deny(principal: Principal | None, required: frozenset[str], path: str) -> None:
    """越权尝试留痕（06-安全 §3：越权尝试写审计）。

    审计写入失败不改变拒绝结论（拒绝优先），仅记录告警日志——不是吞异常，可查可追。
    """
    if _on_access_denied is None:
        return
    record: dict[str, object] = {
        "actor": principal.uid if principal is not None else "anonymous",
        "action_type": "perm_change",
        "result": "denied",
        "detail": {"kind": "rbac_deny", "required_perms": sorted(required), "path": path},
    }
    try:
        _on_access_denied(record)
    except Exception as exc:  # 有意捕获：越权必须被拒，审计降级为日志告警（deny 频次告警由部署侧联动）
        _LOGGER.warning("越权审计写入失败（拒绝结论不变）: %s", exc)


def _extract_path(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """从装饰器入参中尽力提取 FastAPI Request 路径（供越权审计定位端点）。"""
    for value in itertools.chain(args, kwargs.values()):
        if isinstance(value, Request):
            return str(value.url.path)
    return ""


def _ensure_allowed(principal: Principal | None, permissions: frozenset[str], path: str) -> Principal:
    """动作校验（OR 语义）：命中任一动作放行；否则审计留痕并抛 40301（SR §6.2 require）。"""
    if principal is not None and any(permission in principal.perms for permission in permissions):
        return principal
    _audit_deny(principal, permissions, path)
    raise PermissionDeniedError(permissions, principal.uid if principal is not None else None, path)


# --------------------------------------------------------------------------- require_role（SR §6.2，动作级最小权限落点）


def require_role(*permissions: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """路由级权限声明（OR 语义：命中任一动作即放行）——最小权限的动作粒度落点。

    用法（FastAPI 路由与普通函数皆可，同步/异步端点均支持）::

        @app.get("/api/v1/admin/users")
        @require_role("user:manage")
        async def list_users(): ...

    - 被装饰函数挂 ``__required_perms__`` 标记，供 CI「未声明端点拒绝合并」扫描
      与角色×端点越权矩阵测试读取（SR §6.6）；
    - 拒绝路径：越权审计留痕 → PermissionDeniedError(40301)，
      配合 rbac_exception_handler 输出统一错误体（04-接口设计 §1.3）。
    """
    required = frozenset(permissions)

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                _ensure_allowed(get_current_principal(), required, _extract_path(args, kwargs))
                return await fn(*args, **kwargs)

            async_wrapper.__required_perms__ = set(permissions)  # type: ignore[attr-defined]
            return cast(Callable[P, R], async_wrapper)

        @wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            _ensure_allowed(get_current_principal(), required, _extract_path(args, kwargs))
            return fn(*args, **kwargs)

        wrapper.__required_perms__ = set(permissions)  # type: ignore[attr-defined]
        return wrapper

    return decorator


def require_role_dependency(*permissions: str) -> Callable[[Request], Principal]:
    """FastAPI 依赖形态：``Depends(require_role_dependency("audit:read"))``，通过后返回当前 Principal。

    与装饰器同语义（OR + 40301 + 越权审计留痕）；适合路由内需要主体信息的参数注入。
    标记同样挂 ``__required_perms__``（在依赖函数上，CI 扫描需覆盖依赖声明）。
    """
    required = frozenset(permissions)

    def dependency(request: Request) -> Principal:
        return _ensure_allowed(get_current_principal(), required, str(request.url.path))

    dependency.__required_perms__ = set(permissions)  # type: ignore[attr-defined]
    return dependency


async def rbac_exception_handler(request: Request, exc: PermissionDeniedError) -> JSONResponse:
    """把 PermissionDeniedError 转为统一错误体（04-接口设计 §1.3：code 40301 + trace_id）。

    用法：``app.add_exception_handler(PermissionDeniedError, rbac_exception_handler)``。
    """
    trace_id = getattr(request.state, "trace_id", "") or f"tr-{uuid.uuid4().hex[:12]}"
    return JSONResponse(
        status_code=403,
        content={"code": exc.code, "message": exc.message, "data": None, "trace_id": trace_id},
    )
