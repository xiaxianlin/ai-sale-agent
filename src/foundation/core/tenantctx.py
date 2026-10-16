# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""业务服务租户上下文薄中间件（CR 遗留 R2：四业务服务提前挂载验证，SR-SAAS-001 分期执行）。

与 saas 完整版（JWT/域名解析/状态闸门/计费）的分工：
- 本薄版供 kb/training/agent/site 四个业务 app 挂载——demo 单租户形态注入默认
  租户 1（03-数据库设计 §2 语义），使租户维度自 P0 起全链路可观测；
- ``multi_tenancy_enabled=True`` 时读取**网关注入的可信头**（X-Tenant-Id，经 mTLS
  /网关鉴权后的内网头，非客户端可伪造头），生产多租户由部署开启；
- 伪造的查询参数/客户端自声明头永不参与解析（与 saas 完整版同纪律）。
"""
from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable

from starlette.types import ASGIApp, Receive, Scope, Send

#: 当前请求租户上下文（业务代码经 get_tenant_id() 读取；缺省 fail-closed 抛错）
_current_tenant: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_tenant", default=None
)

DEFAULT_TENANT_ID = 1  # 甲方本体租户（03-数据库设计 §2）

#: 网关注入的可信租户头（生产：网关按 JWT/域名解析后注入；客户端同名头被网关覆盖）
TRUSTED_TENANT_HEADER = "X-Tenant-Id"


class TenantContextError(RuntimeError):
    """无租户上下文即读写（fail-closed：业务代码必须在中间件覆盖的请求内取值）。"""


def get_tenant_id() -> int:
    """读取当前请求租户（缺上下文抛错——测试/后台任务外不得直接调用业务路径）。"""
    value = _current_tenant.get()
    if value is None:
        raise TenantContextError("无租户上下文：请求未经 TenantContextLiteMiddleware 覆盖")
    return value


def current_tenant_or_default() -> int:
    """宽松读取：无上下文（后台任务/测试）回落默认租户 1。"""
    return _current_tenant.get() or DEFAULT_TENANT_ID


class TenantContextLiteMiddleware:
    """业务服务薄中间件（纯 ASGI，零响应改写：仅设/复位上下文）。

    挂载：``app.add_middleware(TenantContextLiteMiddleware)``——
    单租户形态缺省 ``multi_tenancy_enabled=False``（恒默认租户 1）。
    """

    def __init__(self, app: ASGIApp, *, multi_tenancy_enabled: bool = False) -> None:
        self.app = app
        self._multi = multi_tenancy_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        tenant = DEFAULT_TENANT_ID
        if self._multi:
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            raw = headers.get(TRUSTED_TENANT_HEADER.lower(), "")
            if raw.isdigit() and int(raw) > 0:
                tenant = int(raw)
            # 非法值（含负数/0/非数字）回落默认租户 1 并继续——租户合法性由 saas 层闸门负责
        token = _current_tenant.set(tenant)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_tenant.reset(token)
