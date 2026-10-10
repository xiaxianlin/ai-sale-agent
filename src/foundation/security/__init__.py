"""知识产权归甲方所有（COM-IP-01）

安全基座包（NFR-SEC-02 权限控制）：请求上下文 + RBAC 声明式矩阵/路由鉴权/数据范围过滤。

对外入口统一从本包导入（内部模块路径不属于稳定契约）；
实现依据：docs/07-实现方案/NFR/SR-NFR-SEC-02-权限控制.md。
"""

from foundation.security.context import (
    Principal,
    clear_current_principal,
    get_current_principal,
    principal_context,
    reset_current_principal,
    set_current_principal,
)
from foundation.security.rbac import (
    PRIVILEGED_ROLES,
    ROLE_PERMISSIONS,
    DataScope,
    DenyAuditRecord,
    DenyAuditor,
    PermissionDeniedError,
    RbacError,
    Role,
    RoleCycleError,
    RoleDef,
    ScopeCriterion,
    ScopeFilterError,
    UnknownRoleError,
    is_privileged,
    max_scope,
    rbac_exception_handler,
    require_role,
    require_role_dependency,
    resolve_perms,
    scope_criterion,
    scope_filter,
    set_deny_auditor,
)

__all__ = [
    "PRIVILEGED_ROLES",
    "ROLE_PERMISSIONS",
    "DataScope",
    "DenyAuditRecord",
    "DenyAuditor",
    "PermissionDeniedError",
    "Principal",
    "RbacError",
    "Role",
    "RoleCycleError",
    "RoleDef",
    "ScopeCriterion",
    "ScopeFilterError",
    "UnknownRoleError",
    "clear_current_principal",
    "get_current_principal",
    "is_privileged",
    "max_scope",
    "principal_context",
    "rbac_exception_handler",
    "require_role",
    "require_role_dependency",
    "reset_current_principal",
    "resolve_perms",
    "scope_criterion",
    "scope_filter",
    "set_current_principal",
    "set_deny_auditor",
]
