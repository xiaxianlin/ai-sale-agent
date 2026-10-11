# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-005 CRM 集成 API（SR-SITE-005 §5/§6.5，Phase 3 第二批；demo 全内存装配）。

端点（统一信封；RBAC crm_operator，NFR-SEC-02）：
- POST /api/v1/site/crm/leads                        线索归一化入口（三渠道）
- POST /api/v1/site/crm/merge                        跨渠道合并（confirm=false 预检）
- POST /api/v1/site/crm/merge/{merge_id}/undo        撤销合并（窗口内）
- GET  /api/v1/site/crm/customers/{customer_id}/timeline  客户 360 时间线

错误码：40001 参数；40301 非 crm_operator；40404 对象不存在；40901 重复合并/超撤销窗口/合并环。
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from services.site.core.crm.identity import CHANNELS, LeadNormalizer
from services.site.core.crm.merge import CustomerMerger, MergeError, NotFoundCRM

# --------------------------------------------------------------------------- 路径与错误码

CRM_PREFIX = "/api/v1/site/crm"
CRM_LEADS_PATH = f"{CRM_PREFIX}/leads"
CRM_MERGE_PATH = f"{CRM_PREFIX}/merge"
CRM_TIMELINE_PATH = f"{CRM_PREFIX}/customers/{{customer_id}}/timeline"

CODE_OK = 0
CODE_BAD_REQUEST = 40001
CODE_FORBIDDEN = 40301
CODE_NOT_FOUND = 40404
CODE_CONFLICT = 40901


class BizError(Exception):
    """业务错误（HTTP 状态 + 业务码）。"""

    def __init__(self, status: int, code: int, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict:
    """统一响应信封（04 §1.3）。"""
    return {"code": code, "message": message, "data": data, "trace_id": trace_id}


def _trace_id_of(request: Request) -> str:
    return (request.headers.get("X-Trace-Id") or "").strip() or uuid.uuid4().hex[:16]


def _ok(request: Request, data: Any) -> JSONResponse:
    return JSONResponse(content=_envelope(_trace_id_of(request), CODE_OK, "ok", data))


def header_claims_provider(request: Request) -> dict:
    """缺省 claims 提供者（demo 请求头承载；生产由全局 JWT 中间件解析）。"""
    roles = [r.strip() for r in request.headers.get("X-User-Roles", "").split(",") if r.strip()]
    return {"sub": request.headers.get("X-User-Id", ""), "roles": roles}


def _jsonable(value: Any) -> Any:
    """递归序列化（datetime → ISO；信封输出统一可 JSON 化）。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# --------------------------------------------------------------------------- 请求体模型


class LeadBody(BaseModel):
    """线索上报体：channel + 渠道原始字段（透传给适配器）。"""

    model_config = ConfigDict(extra="allow")
    channel: str


class MergeBody(BaseModel):
    """合并请求体（confirm=false 为 dry_run 预检，SR §5①）。"""

    source_id: int
    target_id: int
    reason: str = ""
    confirm: bool = True


class UndoBody(BaseModel):
    """撤销请求体。"""

    actor: str = "operator"


# --------------------------------------------------------------------------- 应用工厂


def create_crm_app(*, normalizer: LeadNormalizer, merger: CustomerMerger, store: Any,
                   clock: Callable[[], datetime] | None = None,
                   allowed_roles: tuple[str, ...] = ("crm_operator", "admin"),
                   claims_provider: Callable[[Request], dict] | None = None) -> FastAPI:
    """组装 CRM 数据互通 API（依赖注入；合并类端点统一 crm_operator 角色）。"""
    claims_of = claims_provider or header_claims_provider
    app = FastAPI(title="site-service CRM 集成 API", version="0.1.0",
                  description="FR-SITE-005（SR-SITE-005）：线索归一化/跨渠道合并/客户 360。")

    def _error(request: Request, status: int, code: int, message: str) -> JSONResponse:
        return JSONResponse(status_code=status,
                            content=_envelope(_trace_id_of(request), code, message, None))

    @app.exception_handler(BizError)
    async def _on_biz(request: Request, exc: BizError) -> JSONResponse:
        return _error(request, exc.status, exc.code, str(exc))

    @app.exception_handler(MergeError)
    async def _on_merge_error(request: Request, exc: MergeError) -> JSONResponse:
        return _error(request, 409, CODE_CONFLICT, str(exc))

    @app.exception_handler(NotFoundCRM)
    async def _on_not_found(request: Request, exc: NotFoundCRM) -> JSONResponse:
        return _error(request, 404, CODE_NOT_FOUND, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _on_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(request, 400, CODE_BAD_REQUEST, "参数校验失败")

    def _require_operator(request: Request) -> None:
        """合并/查询类端点统一角色校验（NFR-SEC-02；越权 40301）。"""
        claims = claims_of(request)
        if not set(claims.get("roles") or []) & set(allowed_roles):
            raise BizError(403, CODE_FORBIDDEN, "需要 crm_operator 角色")

    router = APIRouter(prefix=CRM_PREFIX, tags=["site-crm"])

    @router.post("/leads", summary="线索归一化入口（三渠道适配 → 五级瀑布分流）")
    def post_lead(body: LeadBody, request: Request) -> JSONResponse:
        _require_operator(request)
        if body.channel not in CHANNELS:
            raise BizError(400, CODE_BAD_REQUEST, f"未注册渠道：{body.channel}")
        raw = body.model_dump(exclude={"channel"}, exclude_none=True)
        try:
            result = normalizer.ingest(body.channel, raw)
        except KeyError as exc:  # 适配器缺字段等形态错误 → 参数校验失败
            raise BizError(400, CODE_BAD_REQUEST, f"线索字段缺失：{exc}") from exc
        return _ok(request, {"lead_id": result.lead.lead_id,
                             "customer_id": result.lead.customer_id,
                             "match_level": result.lead.match_level,
                             "status": result.lead.status,
                             "action": result.result.action,
                             "level": result.result.level})

    @router.post("/merge", summary="跨渠道合并（confirm=false 预检不落库）")
    def merge(body: MergeBody, request: Request) -> JSONResponse:
        _require_operator(request)
        if not body.confirm:
            return _ok(request, merger.dry_run(body.source_id, body.target_id))
        result = merger.execute(body.source_id, body.target_id, trigger_by="operator",
                                reason=body.reason)
        return _ok(request, {"merge_id": result.merge_id, "survivor_id": result.survivor_id,
                             "undo_deadline": result.undo_deadline.isoformat()})

    @router.post("/merge/{merge_id}/undo", summary="撤销合并（窗口内按快照回滚）")
    def undo(merge_id: int, body: UndoBody, request: Request) -> JSONResponse:
        _require_operator(request)
        task = merger.undo(merge_id, actor=body.actor)
        return _ok(request, {"merge_id": merge_id, "status": task.status,
                             "undone_at": task.undone_at.isoformat() if task.undone_at else None})

    @router.get("/customers/{customer_id}/timeline", summary="客户 360 时间线（跨渠道归并）")
    def timeline(customer_id: int, request: Request,
                 days: int = Query(default=180, le=365)) -> JSONResponse:
        _require_operator(request)
        if customer_id not in store.customers:
            raise NotFoundCRM(f"客户不存在：{customer_id}")
        view = merger.build_360(customer_id, days=days)
        return _ok(request, _jsonable(view))

    app.include_router(router)
    return app
