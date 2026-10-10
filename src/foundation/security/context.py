"""知识产权归甲方所有（COM-IP-01）

请求上下文（平台基座）：当前用户主体 Principal 的 ContextVar 存取。

依据：docs/07-实现方案/NFR/SR-NFR-SEC-02-权限控制.md §6.2/§6.3
- 鉴权中间件解析 JWT 后调用 set_current_principal 注入主体，业务代码经 get_current_principal 只读；
- ContextVar 按协程/线程隔离（并发安全），子任务继承副本，线程间互不串号；
- 未注入（匿名/未认证）时 get_current_principal() 返回 None，由 require_role 按 40301 拒绝（SR §6.2）。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:  # 仅类型标注引用（运行时单向依赖：rbac -> context，避免循环导入）
    from foundation.security.rbac import DataScope


@dataclass(frozen=True)
class Principal:
    """当前请求主体（JWT 声明展开）：身份、角色、已展开权限与数据范围锚点（SR §6.2）。"""

    uid: str                            # 用户标识（JWT sub）
    roles: tuple[str, ...]              # 角色编码（对应 ROLE_PERMISSIONS 键）
    perms: frozenset[str]               # 继承展开后的全部可执行动作（命名 `资源:动作`）
    scope: "DataScope"                  # 数据范围（多角色取最大：rbac.max_scope）
    tenant_id: int = 1                  # 租户边界（P0-P4 恒为 1，P5 起多租户，SR §6.5）
    team_ids: tuple[int, ...] = ()      # 所属团队（TEAM 范围锚点）
    group_ids: tuple[int, ...] = ()     # 所属账号组（ACCOUNT_GROUP 范围锚点，P2 起 4 主 + 24 子账号）
    privileged: bool = False            # 管理类角色标记（RLS app.privileged 来源，SR §6.1）

    def has_perm(self, *permissions: str) -> bool:
        """动作校验（OR 语义：命中任一即有权限），与 require_role 语义一致。"""
        return any(permission in self.perms for permission in permissions)


_current_principal: ContextVar[Principal | None] = ContextVar("current_principal", default=None)


def set_current_principal(principal: Principal) -> Token[Principal | None]:
    """注入当前主体（鉴权中间件调用；返回 Token 供 reset_current_principal 精确回滚）。"""
    return _current_principal.set(principal)


def get_current_principal() -> Principal | None:
    """读取当前主体（业务代码只读入口；未认证返回 None）。"""
    return _current_principal.get()


def reset_current_principal(token: Token[Principal | None]) -> None:
    """按 Token 回滚到注入前状态（后台任务/测试还原现场，防止上下文泄漏串号）。"""
    _current_principal.reset(token)


def clear_current_principal() -> None:
    """清空当前主体（请求收尾/测试隔离）。"""
    _current_principal.set(None)


@contextmanager
def principal_context(principal: Principal) -> Iterator[Principal]:
    """主体上下文管理器：进入注入、退出还原（后台任务与单元测试推荐用法）。"""
    token = _current_principal.set(principal)
    try:
        yield principal
    finally:
        _current_principal.reset(token)
