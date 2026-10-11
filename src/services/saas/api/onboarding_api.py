# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""开通向导 API（FR-SAAS-002，SR-SAAS-002 §5；04-接口设计 §1.3/§3.8 细化增补）。

统一信封 code/message/data/trace_id 四键齐全；错误码映射：
40001→HTTP 400（状态违规/差异项缺失/校验失败）、42901→HTTP 429（预估超配额）、
40401→HTTP 404（向导不存在）；trace_id 优先透传 X-Trace-Id。

装配（本迭代不改全局 app）：``create_onboarding_app(wizard)`` 独立 FastAPI；
生产由 saas-service 挂载 APIRouter 并叠加 JWT/RBAC（NFR-SEC-02，平台运营管理员）。
偏差标注（相对 SR §5）：激活幂等键经 ``Idempotency-Key`` 头（进程内账本，
生产 Redis TTL 86400s）；进度查询走直读（生产 Redis Hash 缓存 TTL 60s）。
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from services.saas.core.onboarding.wizard import (
    CODE_NOT_FOUND,
    CODE_PARAM,
    CODE_QUOTA,
    OnboardingError,
    OnboardingWizard,
)

#: 业务码 → HTTP 状态映射（kb-service 同口径：HTTP 4xx/5xx + 业务码信封）
HTTP_BY_CODE: dict[int, int] = {CODE_PARAM: 400, CODE_QUOTA: 429, CODE_NOT_FOUND: 404}
CODE_SERVER: int = 50001  # 兜底（04 §1.3）


class SaasApiError(Exception):
    """路由层业务异常：携带业务码 / HTTP 状态 / 文案 / 可选 data。"""

    def __init__(self, code: int, message: str, data: Any = None,
                 http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status or HTTP_BY_CODE.get(code, 400)
        self.message = message
        self.data = data


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（04 §1.3）：四键齐全，data 允许 None。"""
    return {"code": code, "message": message, "data": data, "trace_id": trace_id}


def request_trace_id(request: Request) -> str:
    """trace_id 解析：优先透传 X-Trace-Id，缺失新生成 uuid（04 §1.4）。"""
    return request.headers.get("x-trace-id") or f"trc_{uuid.uuid4().hex[:12]}"


def install_error_handlers(app: FastAPI) -> FastAPI:
    """注册统一错误信封：业务异常 / 请求体校验失败 / 兜底 500。"""

    @app.exception_handler(SaasApiError)
    async def _saas_error(request: Request, exc: SaasApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status,
                            content=_envelope(request_trace_id(request), exc.code,
                                              exc.message, exc.data))

    @app.exception_handler(OnboardingError)
    async def _domain_error(request: Request, exc: OnboardingError) -> JSONResponse:
        return JSONResponse(status_code=HTTP_BY_CODE.get(exc.code, 400),
                            content=_envelope(request_trace_id(request), exc.code,
                                              exc.message, None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=400,
                            content=_envelope(request_trace_id(request), CODE_PARAM,
                                              "参数校验失败",
                                              {"errors": exc.errors()[:5]}))

    @app.exception_handler(Exception)
    async def _fallback(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500,
                            content=_envelope(request_trace_id(request), CODE_SERVER,
                                              "服务内部错误", None))

    return app


def create_onboarding_app(wizard: OnboardingWizard) -> FastAPI:
    """装配开通向导 API（demo 独立应用；路由同步交付 APIRouter 形态）。"""
    app = FastAPI(title="saas-onboarding-api", version="0.1.0")
    install_error_handlers(app)
    router = APIRouter(prefix="/api/v1/saas")

    @router.post("/onboarding")
    async def create_onboarding(request: Request, body: dict | None = None) -> dict:
        payload = body or {}
        ob = wizard.create(plan=payload.get("plan", "standard"))
        return _envelope(request_trace_id(request), 0, "ok",
                         {"uid": ob.uid, "tenant_id": ob.tenant_id, "plan": ob.plan,
                          "status": ob.status})

    @router.put("/onboarding/{uid}/company-info")
    async def put_company_info(uid: str, request: Request, body: dict) -> dict:
        result = wizard.submit_step(uid, "company_info", body)
        return _envelope(request_trace_id(request), 0, "ok",
                         {"step": result.step, "all_done": result.all_done,
                          "steps_done": list(result.steps_done)})

    @router.post("/onboarding/{uid}/kb/materials")
    async def post_kb_materials(uid: str, request: Request, body: dict) -> dict:
        result = wizard.submit_step(uid, "kb_init", body)
        ob = wizard.store.must_get(uid)
        return _envelope(request_trace_id(request), 0, "ok",
                         {"step": result.step, "all_done": result.all_done,
                          "steps_done": list(result.steps_done),
                          "estimate": wizard.estimate(
                              ob.steps["kb_init"]["materials"]).as_dict()})

    @router.put("/onboarding/{uid}/accounts")
    async def put_accounts(uid: str, request: Request, body: dict) -> dict:
        result = wizard.submit_step(uid, "accounts", body)
        # 回显仅掩码（NFR-SEC-04：凭证只收不回）
        return _envelope(request_trace_id(request), 0, "ok",
                         {"step": result.step, "all_done": result.all_done,
                          "steps_done": list(result.steps_done),
                          "accounts": wizard.masked_accounts(uid)})

    @router.put("/onboarding/{uid}/strategy")
    async def put_strategy(uid: str, request: Request, body: dict) -> dict:
        result = wizard.submit_step(uid, "strategy", body)
        return _envelope(request_trace_id(request), 0, "ok",
                         {"step": result.step, "all_done": result.all_done,
                          "steps_done": list(result.steps_done)})

    @router.get("/templates/defaults")
    async def get_template_defaults(request: Request) -> dict:
        return _envelope(request_trace_id(request), 0, "ok",
                         {"templates": wizard.templates.defaults_for_form()})

    @router.post("/onboarding/{uid}/activate")
    async def activate(uid: str, request: Request) -> dict:
        idem_key = request.headers.get("idempotency-key", "")
        result = wizard.activate(uid, idem_key)
        return _envelope(request_trace_id(request), 0, "ok",
                         {"uid": result.uid, "tenant_id": result.tenant_id,
                          "kb_task_uid": result.kb_task_uid, "cfg_rows": result.cfg_rows,
                          "estimate": result.estimate.as_dict(), "status": result.status})

    @router.get("/onboarding/{uid}/progress")
    async def get_progress(uid: str, request: Request) -> dict:
        return _envelope(request_trace_id(request), 0, "ok", wizard.progress(uid))

    @router.post("/onboarding/{uid}/kb/retry")
    async def post_kb_retry(uid: str, request: Request) -> dict:
        reset = wizard.retry_kb(uid)
        ob = wizard.store.must_get(uid)
        return _envelope(request_trace_id(request), 0, "ok",
                         {"reset_files": reset, "status": ob.status})

    @router.get("/onboarding/{uid}/drill-report")
    async def get_drill_report(uid: str, request: Request) -> dict:
        return _envelope(request_trace_id(request), 0, "ok",
                         {"uid": uid, "steps": wizard.drill_report(uid)})

    app.include_router(router)
    return app
