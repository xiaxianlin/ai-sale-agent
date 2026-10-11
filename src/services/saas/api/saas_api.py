# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""SaaS 服务 API（FR-SAAS-001/003 §5，SR-SAAS-003 §5、SR-SAAS-001 §5）。

双域路由（SR-SAAS-003 §6.3 双域闸门）：
- 平台域 /api/v1/saas/platform/*（aud=platform，operator/admin）：tenants CRUD、
  suspend|reinstate|expire、purge（admin-only）、quota 变更、usage 小时查询、
  billing/usage/export（无定价字段）、isolation/report；
- 租户域 /api/v1/saas/tenant/*（aud=tenant，中间件租户闸门）：whoami、知识条目
  增删改查/检索（计费检索走 QPS 闸门）、对象与缓存键（隔离套件靶场）。

统一信封 {code, message, data, trace_id}（04-接口设计 §1.3）；错误码：
40001/40101/40301/40302/40303/40304/40401/40901/40902/40903/42901。
租户上下文经 ContextVar 注入（core.tenant.context），租户域端点签名无 tenant_id
形参——API 形态上杜绝显式传参绕过（SR-SAAS-001 关键决策 2）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from services.saas.core.ops.service import (
    AuthError,
    OpsService,
    RoleDeniedError,
    TenantGateError,
    UsageExportError,
)
from services.saas.core.quota.engine import QuotaEngine, QuotaExceededError
from services.saas.core.tenant.context import (
    DEFAULT_TENANT_ID,
    SaasTokenCodec,
    TenantContextError,
    TenantContextMiddleware,
    get_tenant_id,
)
from services.saas.core.tenant.registry import RegistryError, TenantRegistry
from services.saas.core.tenant.store import (
    KeySpace,
    KnowledgeStore,
    ObjectStore,
    object_prefix,
)

#: 路由前缀（双域，SR-SAAS-003 §6.3）
SAAS_PLATFORM_PREFIX: str = "/api/v1/saas/platform"
SAAS_TENANT_PREFIX: str = "/api/v1/saas/tenant"

#: 计费路径（中间件 QPS 闸门白名单，SR-SAAS-001 §6.1 is_billed）
BILLED_PATHS: tuple[str, ...] = (f"{SAAS_TENANT_PREFIX}/knowledge/search",)

#: 对象前缀越界错误码（本服务细化，NFR-SEC-07 存储层口径）
OBJECT_PREFIX_DENIED: int = 40304


class SaasApiError(Exception):
    """路由层业务异常：统一信封展开（对齐 services/kb/api/routes.ApiError 形态）。"""

    def __init__(self, code: int, http_status: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.data = data


# --------------------------------------------------------------------------- 请求模型


class ProvisionBody(BaseModel):
    """开通租户入参（SR-SAAS-003 §5：name/plan/initial_days）。"""

    name: str = Field(..., min_length=1, max_length=128)
    plan: str = "standard"
    initial_days: int = Field(365, ge=1, le=3650)


class SuspendBody(BaseModel):
    reason: str = ""


class ReinstateBody(BaseModel):
    days: int = Field(365, ge=1, le=3650)


class QuotaBody(BaseModel):
    resource: str
    hard_limit: int = Field(..., ge=0)


class ItemBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=512)
    body: str = ""


class SearchBody(BaseModel):
    q: str = Field("", max_length=512)


# --------------------------------------------------------------------------- 信封


def _envelope_response(data: Any, request: Request, status_code: int = 200) -> JSONResponse:
    """成功信封（04 §1.3 四键；trace_id 优先透传 X-Trace-Id）。"""
    trace_id = request.headers.get("X-Trace-Id", "").strip() or uuid.uuid4().hex
    return JSONResponse(
        status_code=status_code,
        content={"code": 0, "message": "ok", "data": data, "trace_id": trace_id},
    )


def _install_error_handlers(app: FastAPI) -> None:
    """域异常 → 统一信封（RegistryError 派生类经 MRO 命中基类处理器）。"""

    def _wrap(request: Request, code: int, http_status: int, message: str) -> JSONResponse:
        trace_id = request.headers.get("X-Trace-Id", "").strip() or uuid.uuid4().hex
        return JSONResponse(
            status_code=http_status,
            content={"code": code, "message": message, "data": None, "trace_id": trace_id},
        )

    @app.exception_handler(SaasApiError)
    async def _on_api_error(request: Request, exc: SaasApiError) -> JSONResponse:
        trace_id = request.headers.get("X-Trace-Id", "").strip() or uuid.uuid4().hex
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "message": exc.message, "data": exc.data,
                     "trace_id": trace_id},
        )

    @app.exception_handler(RegistryError)
    async def _on_registry_error(request: Request, exc: RegistryError) -> JSONResponse:
        return _wrap(request, exc.code, exc.http_status, exc.message)

    @app.exception_handler(AuthError)
    async def _on_auth_error(request: Request, exc: AuthError) -> JSONResponse:
        return _wrap(request, exc.code, exc.http_status, exc.message)

    @app.exception_handler(RoleDeniedError)
    async def _on_role_error(request: Request, exc: RoleDeniedError) -> JSONResponse:
        return _wrap(request, exc.code, exc.http_status, exc.message)

    @app.exception_handler(TenantGateError)
    async def _on_gate_error(request: Request, exc: TenantGateError) -> JSONResponse:
        return _wrap(request, exc.code, exc.http_status, exc.message)

    @app.exception_handler(UsageExportError)
    async def _on_export_error(request: Request, exc: UsageExportError) -> JSONResponse:
        return _wrap(request, exc.code, exc.http_status, exc.message)

    @app.exception_handler(TenantContextError)
    async def _on_ctx_error(request: Request, exc: TenantContextError) -> JSONResponse:
        return _wrap(request, exc.code, exc.http_status, exc.message)

    @app.exception_handler(QuotaExceededError)
    async def _on_quota_error(request: Request, exc: QuotaExceededError) -> JSONResponse:
        trace_id = request.headers.get("X-Trace-Id", "").strip() or uuid.uuid4().hex
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "message": exc.message,
                     "data": {"resource": exc.resource, "tenant_id": exc.tenant_id},
                     "trace_id": trace_id},
            headers={"Retry-After": str(max(1, exc.retry_after or 1))},
        )


# --------------------------------------------------------------------------- 出参整形


def _iso(value: Any) -> Any:
    """datetime → ISO 字符串（JSONResponse 无法序列化 datetime）。"""
    return value.isoformat() if isinstance(value, datetime) else value


def _tenant_view(row: Any) -> dict[str, Any]:
    """租户行出参整形（平台域列表/详情/生命周期共用）。"""
    return {
        "id": getattr(row, "id", None),
        "name": getattr(row, "name", ""),
        "plan": getattr(row, "plan", ""),
        "status": getattr(row, "status", ""),
        "expire_at": _iso(getattr(row, "expire_at", None)),
        "suspended_at": _iso(getattr(row, "suspended_at", None)),
        "suspend_reason": getattr(row, "suspend_reason", ""),
        "data_retain_until": _iso(getattr(row, "data_retain_until", None)),
        "quota_overrides": dict(getattr(row, "quota_overrides", {}) or {}),
    }


def _snapshot_view(snapshot: Any) -> dict[str, Any]:
    """小时快照出参整形（usage 端点；七字段对齐 tenant_usage_hour，SR §4）。"""
    return {
        "tenant_id": snapshot.tenant_id,
        "stat_hour": snapshot.stat_hour,
        "api_calls": snapshot.api_calls,
        "storage_bytes": snapshot.storage_bytes,
        "account_cnt": snapshot.account_cnt,
        "kb_item_cnt": snapshot.kb_item_cnt,
        "blocked_cnt": snapshot.blocked_cnt,
    }


# --------------------------------------------------------------------------- 装配


def create_saas_app(
    *,
    registry: TenantRegistry | None = None,
    quota: QuotaEngine | None = None,
    ops: OpsService | None = None,
    secret: str = "saas-demo-secret",
    clock: Callable[[], datetime] | None = None,
    multi_tenancy_enabled: bool = True,
    host_resolver: Callable[[str], int | None] | None = None,
    cross_tenant_leak: bool = False,
    isolation_runner: Callable[[], dict] | None = None,
) -> tuple[FastAPI, dict[str, Any]]:
    """装配 SaaS 应用：双域路由 + 租户上下文中间件 + 统一信封。

    cross_tenant_leak 仅供隔离套件缺陷注入元测试（模拟 RLS 失效），产品恒 False。
    返回 (app, components)——components 供测试/隔离套件直接消费核心组件。
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    registry = registry or TenantRegistry(clock=clock)
    quota = quota or QuotaEngine(clock=clock)
    codec = SaasTokenCodec(secret, clock=clock)
    ops = ops or OpsService(registry, quota, codec=codec, clock=clock, secret=secret)
    ops.wire()  # 审计钩子接入注册表（开通/状态/配额全量留痕，NFR-SEC-03）
    store = KnowledgeStore(clock=clock)
    objects = ObjectStore()
    keyspace = KeySpace()

    app = FastAPI(title="ai-sale-agent saas-service")
    _install_error_handlers(app)

    def tenant_id_of(request: Request) -> int:
        """端点内取租户：唯一来源是 ContextVar（中间件注入），fail-closed。"""
        try:
            return get_tenant_id()
        except TenantContextError as exc:
            raise SaasApiError(exc.code, exc.http_status, exc.message) from exc

    # ---------------------------------------------------------------- 平台域路由

    @app.post(f"{SAAS_PLATFORM_PREFIX}/tenants", status_code=201)
    async def provision_tenant(body: ProvisionBody, request: Request) -> JSONResponse:
        ops.platform_principal(request)  # 平台域闸门（aud=platform + 角色集）
        row = registry.provision(body.name, plan=body.plan, initial_days=body.initial_days)
        return _envelope_response(_tenant_view(row), request, status_code=201)

    @app.get(f"{SAAS_PLATFORM_PREFIX}/tenants")
    async def list_tenants(request: Request) -> JSONResponse:
        ops.platform_principal(request)
        items = [_tenant_view(row) for row in registry.list_tenants()]
        return _envelope_response({"items": items, "total": len(items)}, request)

    @app.get(f"{SAAS_PLATFORM_PREFIX}/tenants/{{tid}}")
    async def get_tenant(tid: int, request: Request) -> JSONResponse:
        ops.platform_principal(request)
        row = registry.get(tid)
        if row is None:
            raise SaasApiError(40401, 404, f"租户不存在: {tid}")
        return _envelope_response(_tenant_view(row), request)

    @app.put(f"{SAAS_PLATFORM_PREFIX}/tenants/{{tid}}/quota")
    async def update_quota(tid: int, body: QuotaBody, request: Request) -> JSONResponse:
        ops.platform_principal(request)
        if registry.get(tid) is None:
            raise SaasApiError(40401, 404, f"租户不存在: {tid}")
        try:
            quota.set_limit(tid, body.resource, body.hard_limit)  # 引擎侧即时生效
        except ValueError as exc:
            raise SaasApiError(40001, 400, str(exc)) from exc
        registry.set_quota(tid, body.resource, body.hard_limit)  # 总账 + 审计
        return _envelope_response(
            {"tenant_id": tid, "resource": body.resource, "hard_limit": body.hard_limit}, request
        )

    @app.post(f"{SAAS_PLATFORM_PREFIX}/tenants/{{tid}}/suspend")
    async def suspend_tenant(tid: int, body: SuspendBody, request: Request) -> JSONResponse:
        ops.platform_principal(request)
        return _envelope_response(_tenant_view(registry.suspend(tid, reason=body.reason)), request)

    @app.post(f"{SAAS_PLATFORM_PREFIX}/tenants/{{tid}}/reinstate")
    async def reinstate_tenant(tid: int, body: ReinstateBody, request: Request) -> JSONResponse:
        ops.platform_principal(request)
        return _envelope_response(_tenant_view(registry.reinstate(tid, days=body.days)), request)

    @app.post(f"{SAAS_PLATFORM_PREFIX}/tenants/{{tid}}/expire")
    async def expire_tenant(tid: int, request: Request) -> JSONResponse:
        ops.platform_principal(request)
        return _envelope_response(_tenant_view(registry.expire(tid)), request)

    @app.delete(f"{SAAS_PLATFORM_PREFIX}/tenants/{{tid}}")
    async def purge_tenant(tid: int, request: Request) -> JSONResponse:
        """物理清理（admin-only，保留期届满后；SR §6.1 保留期外清理双确认口径）。"""
        principal = ops.platform_principal(request)
        ops.require_role(principal, "tenant_purge")
        registry.purge(tid)
        return _envelope_response({"tenant_id": tid, "purged": True}, request)

    @app.get(f"{SAAS_PLATFORM_PREFIX}/tenants/{{tid}}/usage")
    async def tenant_usage(tid: int, request: Request) -> JSONResponse:
        """用量查询：小时数组 + 汇总（计费数据源，FR-SAAS-003 消费口径）。"""
        ops.platform_principal(request)
        items = [_snapshot_view(s) for s in quota.usage_snapshots(tid)]
        summary = {key: sum(item[key] for item in items)
                   for key in ("api_calls", "storage_bytes", "account_cnt", "kb_item_cnt", "blocked_cnt")}
        return _envelope_response({"items": items, "summary": summary}, request)

    @app.get(f"{SAAS_PLATFORM_PREFIX}/billing/usage/export")
    async def export_usage(period: str, request: Request) -> PlainTextResponse:
        """计费导出：CSV（仅用量事实数据，无定价字段，SRS §10.1 边界）。"""
        ops.platform_principal(request)
        try:
            csv_text = ops.export_usage_csv(period)
        except UsageExportError as exc:
            raise SaasApiError(exc.code, exc.http_status, exc.message) from exc
        return PlainTextResponse(
            csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=usage-{period}.csv"},
        )

    @app.post(f"{SAAS_PLATFORM_PREFIX}/isolation/report")
    async def isolation_report(request: Request) -> JSONResponse:
        """隔离性测试报告（NFR-SEC-07 交付口）：执行器注入，产出 verdict+报告。"""
        ops.platform_principal(request)
        if isolation_runner is None:
            return _envelope_response(
                {"verdict": "SKIP", "reason": "未配置隔离套件执行器"}, request
            )
        return _envelope_response(isolation_runner(), request)

    # ---------------------------------------------------------------- 租户域路由

    @app.get(f"{SAAS_TENANT_PREFIX}/whoami")
    async def whoami(request: Request) -> JSONResponse:
        tid = tenant_id_of(request)
        row = registry.get(tid)
        return _envelope_response(
            {"tenant_id": tid, "name": getattr(row, "name", ""),
             "plan": getattr(row, "plan", ""), "status": getattr(row, "status", "active")},
            request,
        )

    def _own_item(uid: str) -> Any:
        """租户边界过滤（等价 RLS：他租户同 uid 不可见，404 不泄露存在性）。"""
        item = store.get(uid)
        if item is None or (not cross_tenant_leak and item.tenant_id != get_tenant_id()):
            raise SaasApiError(40401, 404, "条目不存在或不可见")
        return item

    @app.post(f"{SAAS_TENANT_PREFIX}/knowledge/items", status_code=201)
    async def create_item(body: ItemBody, request: Request) -> JSONResponse:
        tid = tenant_id_of(request)
        quota.require(tid, "kb_item_cnt")  # 写路径前置校验（低频资源，SR §6.4）
        item = store.create(tid, body.title, body.body)  # 行级 tenant_id 以上下文覆写
        return _envelope_response({"uid": item.uid, "tenant_id": tid}, request, status_code=201)

    @app.get(f"{SAAS_TENANT_PREFIX}/knowledge/items/{{uid}}")
    async def get_item(uid: str, request: Request) -> JSONResponse:
        tenant_id_of(request)
        item = _own_item(uid)
        return _envelope_response({"uid": item.uid, "title": item.title, "body": item.body}, request)

    @app.put(f"{SAAS_TENANT_PREFIX}/knowledge/items/{{uid}}")
    async def update_item(uid: str, body: ItemBody, request: Request) -> JSONResponse:
        tenant_id_of(request)
        _own_item(uid)
        store.update(uid, title=body.title, body=body.body)
        return _envelope_response({"uid": uid, "updated": True}, request)

    @app.delete(f"{SAAS_TENANT_PREFIX}/knowledge/items/{{uid}}")
    async def delete_item(uid: str, request: Request) -> JSONResponse:
        tenant_id_of(request)
        _own_item(uid)
        store.delete(uid)
        return _envelope_response({"uid": uid, "deleted": True}, request)

    @app.post(f"{SAAS_TENANT_PREFIX}/knowledge/search")
    async def search(body: SearchBody, request: Request) -> JSONResponse:
        """统一检索（计费路径：QPS 闸门已在中间件完成扣减，SR §6.1）。"""
        tid = tenant_id_of(request)
        rows = store.search(tid, body.q)  # 按上下文租户过滤（等价 Partition Key）
        return _envelope_response(
            {"results": [{"uid": r.uid, "title": r.title} for r in rows],
             "total": len(rows), "tenant_id": tid},
            request,
        )

    @app.put(f"{SAAS_TENANT_PREFIX}/objects/{{key:path}}", status_code=201)
    async def put_object(key: str, request: Request) -> JSONResponse:
        tid = tenant_id_of(request)
        content = (await request.body()).decode("utf-8", errors="replace")
        canonical = objects.put(tid, key, content)  # 强制归位租户前缀
        return _envelope_response({"key": canonical}, request, status_code=201)

    @app.get(f"{SAAS_TENANT_PREFIX}/objects/{{key:path}}")
    async def get_object(key: str, request: Request) -> JSONResponse:
        tid = tenant_id_of(request)
        content = objects.get(object_prefix(tid) + key.lstrip("/"))
        if content is None and cross_tenant_leak:
            content = objects.get(key)  # 缺陷注入：绕过前缀归一（模拟缺失前缀校验）
        if content is None:
            raise SaasApiError(40401, 404, "对象不存在或不可见")  # 不泄露存在性
        return _envelope_response({"key": key, "content": content}, request)

    @app.put(f"{SAAS_TENANT_PREFIX}/cache/{{name}}", status_code=201)
    async def put_cache_key(name: str, request: Request) -> JSONResponse:
        tid = tenant_id_of(request)
        return _envelope_response({"key": keyspace.issue(tid, name)}, request, status_code=201)

    @app.get(f"{SAAS_TENANT_PREFIX}/cache")
    async def list_cache_keys(request: Request) -> JSONResponse:
        tid = tenant_id_of(request)
        return _envelope_response({"keys": keyspace.scan(tid)}, request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ---------------------------------------------------------------- 中间件挂载

    app.add_middleware(
        TenantContextMiddleware,
        registry=registry,
        codec=codec,
        clock=clock,
        multi_tenancy_enabled=multi_tenancy_enabled,
        host_resolver=host_resolver,
        quota_engine=quota,
        billed_paths=BILLED_PATHS,
        platform_prefix=SAAS_PLATFORM_PREFIX,
        blocked_audit=lambda tid, path: ops.audit.append(
            "system", "quota_blocked", path, {"tenant_id": tid}
        ),
    )

    components: dict[str, Any] = {
        "registry": registry,
        "quota": quota,
        "ops": ops,
        "codec": codec,
        "clock": clock,
        "store": store,
        "objects": objects,
        "keyspace": keyspace,
    }
    return app, components


#: 默认租户常量再导出（消费方从 api 层取默认口径，避免深入 core）
__all__ = [
    "BILLED_PATHS", "DEFAULT_TENANT_ID", "OBJECT_PREFIX_DENIED", "SAAS_PLATFORM_PREFIX",
    "SAAS_TENANT_PREFIX", "SaasApiError", "create_saas_app",
]
