# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""品牌定制（白标）API（FR-SAAS-006，SR-SAAS-006 §5/§6.3；04-接口设计 §3.8）。

- PUT/GET ``/api/v1/saas/tenants/{tid}/brand-config``：租户管理员写（JWT+RBAC 由
  挂载方叠加）、即时生效返回 {version,state}；
- GET ``/api/v1/public/brand-config``：匿名三端拉取，白名单字段 + ETag；
  ``If-None-Match`` 命中当前版本 → 304 零载荷（未变更免传输），Cache-Control:
  no-cache（必须经服务端校验 304，禁中间层长缓存）；
- POST ``/api/v1/saas/domains`` / ``POST …/{id}/verify`` / GET 列表：域名
  注册-验证-证书-路由（SR §6.5）。

偏差标注：公开读取的租户解析（登录页按 Host 走 resolver / JWT 载荷优先）未接网关，
demo 以 ``tenant_id`` 查询参数显式指定——生产由 brand-router.conf 注入 X-Tenant-Id。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse

from services.saas.api.onboarding_api import (
    HTTP_BY_CODE,
    SaasApiError,
    install_error_handlers,
    request_trace_id,
)
from services.saas.core.branding.service import (
    CODE_CONFLICT,
    CODE_NOT_FOUND,
    CODE_PARAM,
    BrandingError,
    BrandingService,
    brand_etag,
)

#: 白标域业务码 → HTTP 状态（409 冲突语义）
HTTP_BY_CODE = {**HTTP_BY_CODE, CODE_CONFLICT: 409, CODE_NOT_FOUND: 404}


def _domain_error(request: Request, exc: BrandingError) -> JSONResponse:
    """BrandingError → 统一信封（40901→HTTP 409）。"""
    return JSONResponse(status_code=HTTP_BY_CODE.get(exc.code, 400),
                        content={"code": exc.code, "message": exc.message, "data": None,
                                 "trace_id": request_trace_id(request)})


def create_branding_app(service: BrandingService) -> FastAPI:
    """装配品牌定制 API（demo 独立应用；生产由 saas-service 挂载并叠加 RBAC）。"""
    app = FastAPI(title="saas-branding-api", version="0.1.0")
    install_error_handlers(app)

    @app.exception_handler(BrandingError)
    async def _branding_error(request: Request, exc: BrandingError) -> JSONResponse:
        return _domain_error(request, exc)

    router = APIRouter(prefix="/api/v1")

    @router.put("/saas/tenants/{tenant_id}/brand-config")
    async def put_brand_config(tenant_id: int, request: Request, body: dict) -> dict:
        result = service.put_config(tenant_id, body)
        return {"code": 0, "message": "ok", "data": result,
                "trace_id": request_trace_id(request)}

    @router.get("/saas/tenants/{tenant_id}/brand-config")
    async def get_brand_config(tenant_id: int, request: Request) -> dict:
        return {"code": 0, "message": "ok", "data": service.get_config(tenant_id),
                "trace_id": request_trace_id(request)}

    @router.get("/public/brand-config")
    async def public_brand_config(request: Request, tenant_id: int = Query(...)) -> Any:
        """公开读取：ETag/304 零载荷（SR §6.3；租户解析偏差见模块 docstring）。"""
        view = service.public_view(tenant_id)
        etag = brand_etag(tenant_id, int(view.get("version", 0)))
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag,
                                                      "Cache-Control": "no-cache"})
        return JSONResponse(
            content={"code": 0, "message": "ok", "data": view,
                     "trace_id": request_trace_id(request)},
            headers={"ETag": etag, "Cache-Control": "no-cache"})

    @router.post("/saas/domains")
    async def register_domain(request: Request, body: dict) -> dict:
        result = service.register_domain(int(body["tenant_id"]), body["domain"])
        return {"code": 0, "message": "ok", "data": result,
                "trace_id": request_trace_id(request)}

    @router.post("/saas/domains/{domain_id}/verify")
    async def verify_domain(domain_id: int, request: Request) -> dict:
        result = service.verify_domain(domain_id)
        return {"code": 0, "message": "ok", "data": result,
                "trace_id": request_trace_id(request)}

    @router.get("/saas/domains")
    async def list_domains(request: Request, tenant_id: int = Query(...)) -> dict:
        rows = service.list_domains(tenant_id)
        return {"code": 0, "message": "ok",
                "data": {"domains": [row.as_dict() for row in rows]},
                "trace_id": request_trace_id(request)}

    app.include_router(router)
    return app
