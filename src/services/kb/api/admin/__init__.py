# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""知识库管理后台 API 包（FR-KB-020 可视化管理·后台 API 层，迭代 0.3）。

路由以 APIRouter 交付（本迭代不改 services/kb/app.py 与 api/routes.py）：
- items.py       → /api/v1/knowledge/items（条目 CRUD + 草稿-发布状态机 + 软删）
- schemas_api.py → /api/v1/knowledge/schemas（注册表下发，动态表单渲染依据）
- health.py      → /api/v1/admin/stats（看板统计：条目数/状态分布/近 7 日变更）

挂载方式（维护者，生产形态）::

    from services.kb.api.admin import configure_admin
    app = ...  # 既有 kb 应用（create_app 产物，统一信封错误处理器已就位）
    configure_admin(
        app,
        store=PostgresKnowledgeItemStore(...),   # 生产存储（本包提供 InMemory 实现）
        schema_provider=registry_adapter,        # services/kb/models/registry.py 侧适配器
        sync_hook=indexer_enqueue,               # FR-KB-017 增量索引入队
        audit_hook=audit_writer_adapter,         # 全部写操作 → audit_log（NFR-SEC-03）
    )

依赖注入点：四个组件均挂 ``app.state.admin_*``（admin_item_store /
admin_schema_provider / admin_sync_hook / admin_audit_hook，缺省会装占位实现，
读取依赖见 items.get_item_store 等）；鉴权（JWT/RBAC，NFR-SEC-02）由挂载方在
include_router 前追加路由级 dependencies（本迭代未占用该参数位）。
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import Depends, FastAPI

from services.kb.api.routes import install_error_handlers

from .health import router as stats_router
from .items import (
    AuditHook,
    InMemoryKnowledgeItemStore,
    KnowledgeItemRecord,
    KnowledgeItemStore,
    NoopAuditHook,
    NoopSyncHook,
    SchemaProvider,
    SyncHook,
    deny_write_admin_auth,
)
from .items import router as items_router
from .schemas_api import EmptySchemaProvider
from .schemas_api import router as schemas_router

__all__ = [
    "AuditHook",
    "EmptySchemaProvider",
    "InMemoryKnowledgeItemStore",
    "KnowledgeItemRecord",
    "KnowledgeItemStore",
    "NoopAuditHook",
    "NoopSyncHook",
    "SchemaProvider",
    "SyncHook",
    "configure_admin",
    "create_admin_app",
    "deny_write_admin_auth",
    "items_router",
    "schemas_router",
    "stats_router",
]


def configure_admin(
    app: FastAPI,
    *,
    store: KnowledgeItemStore | None = None,
    schema_provider: SchemaProvider | None = None,
    sync_hook: SyncHook | None = None,
    audit_hook: AuditHook | None = None,
    admin_auth: Callable[..., Any] | None = None,
) -> FastAPI:
    """注入管理端组件并挂载三个路由（写入 app.state.admin_*）。

    缺省组件：store → InMemory；schema_provider → 空注册表；sync/audit → Noop
    （本地开发形态；生产必须显式注入真实实现，尤其审计钩子——NFR-SEC-03）。
    :param admin_auth: 管理端路由鉴权依赖（生产 = JWT/RBAC，NFR-SEC-02）。缺省挂
        deny_write_admin_auth——未配置鉴权即拒绝写操作（40301 信封），读操作放行
        （CR P0-3 修复：安全缺省 fail-closed）。
    注意：不幂等，重复调用会重复挂载路由。
    """
    app.state.admin_item_store = store if store is not None else InMemoryKnowledgeItemStore()
    app.state.admin_schema_provider = schema_provider if schema_provider is not None else EmptySchemaProvider()
    app.state.admin_sync_hook = sync_hook if sync_hook is not None else NoopSyncHook()
    app.state.admin_audit_hook = audit_hook if audit_hook is not None else NoopAuditHook()
    # CR P0-3 修复：未注入 admin_auth 即关门——写操作 40301、读操作放行
    auth_dep = admin_auth if admin_auth is not None else deny_write_admin_auth
    app.include_router(items_router, dependencies=[Depends(auth_dep)])
    app.include_router(schemas_router, dependencies=[Depends(auth_dep)])
    app.include_router(stats_router, dependencies=[Depends(auth_dep)])
    return app


def create_admin_app(
    *,
    store: KnowledgeItemStore | None = None,
    schema_provider: SchemaProvider | None = None,
    sync_hook: SyncHook | None = None,
    audit_hook: AuditHook | None = None,
    admin_auth: Callable[..., Any] | None = None,
) -> FastAPI:
    """独立装配（单元测试/本地开发）：新建应用 + 统一信封错误处理器 + 三路由。

    生产挂载到既有 kb 应用（create_app 产物）时应改用 configure_admin——
    错误处理器已随 create_app 注册，避免二次注册。
    """
    app = FastAPI(
        title="kb-service 管理后台 API",
        version="0.3.0",
        description=(
            "FR-KB-020 知识库可视化管理·后台 API 层（迭代 0.3）："
            "条目 CRUD（草稿-发布状态机）/ Schema 注册表下发 / 看板统计。"
        ),
    )
    install_error_handlers(app)
    return configure_admin(
        app,
        store=store,
        schema_provider=schema_provider,
        sync_hook=sync_hook,
        audit_hook=audit_hook,
        admin_auth=admin_auth,
    )
