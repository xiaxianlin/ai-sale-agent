# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-004 邮件自动化 API（SR-SITE-004 §5，Phase 3 第二批；demo 全内存装配）。

端点（统一信封 code/message/data/trace_id，04-接口设计 §1.3）：
- POST /api/v1/site/mail/sequences/{kind}/enroll   序列开启（三类内置 kind；幂等）
- GET  /api/v1/site/mail/jobs                      任务查询（sequence_id/status 过滤）
- POST /api/v1/site/mail/templates                 模板 upsert（管理入口归 FR-SITE-006）
- POST /api/v1/site/mail/tracking/event            回执/打开/退信 webhook（X-Api-Key 内部鉴权）
- GET  /api/v1/site/mail/stats                     统计（送达率 + ≥95% 判定，SRS §16.4）

错误码：40001 参数/模板校验失败；40101 内部事件入口鉴权失败；40301 角色越权（声明身份时校验）。
装配方式：scheduler/tracker/templates/store 全部依赖注入（create_mail_app 工厂，demo 全内存）；
api_key 缺省读环境变量 KB_MAIL_WEBHOOK_KEY，均缺启动失败（CR P0-5 修复：无缺省密钥）。
"""
from __future__ import annotations

from foundation.core.envelope import envelope

import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.site.core.errors import resolve_site_secret
from services.site.core.mailing.scheduler import MailEvent, MailScheduler
from services.site.core.mailing.sequences import DEFAULT_SEQUENCE_DEFS, MailStore
from services.site.core.mailing.templates import TemplateError, TemplateRegistry
from services.site.core.mailing.tracking import DeliveryTracker, TrackEvent

# --------------------------------------------------------------------------- 路径与错误码

MAIL_PREFIX = "/api/v1/site/mail"
ENROLL_PATH = f"{MAIL_PREFIX}/sequences/{{kind}}/enroll"
JOBS_PATH = f"{MAIL_PREFIX}/jobs"
MAIL_TEMPLATE_PATH = f"{MAIL_PREFIX}/templates"
MAIL_TRACKING_EVENT_PATH = f"{MAIL_PREFIX}/tracking/event"
MAIL_STATS_PATH = f"{MAIL_PREFIX}/stats"

CODE_OK = 0
CODE_BAD_REQUEST = 40001
CODE_UNAUTHORIZED = 40101
CODE_FORBIDDEN = 40301


class BizError(Exception):
    """业务错误（携带 HTTP 状态与业务码，统一信封输出）。"""

    def __init__(self, status: int, code: int, message: str) -> None:
        super().__init__(message)
        self.status, self.code = status, code


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict:
    """统一响应信封（CR A2 全面委托化：foundation 单点实现）。"""
    return envelope(code, message, data, trace_id)


def _trace_id_of(request: Request) -> str:
    """trace_id：X-Trace-Id 头优先，缺失新生成。"""
    return (request.headers.get("X-Trace-Id") or "").strip() or uuid.uuid4().hex[:16]


def _ok(request: Request, data: Any) -> JSONResponse:
    """成功信封。"""
    return JSONResponse(content=_envelope(_trace_id_of(request), CODE_OK, "ok", data))


def header_claims_provider(request: Request) -> dict:
    """请求头 claims 提供者：demo 以请求头承载（X-User-Id/X-User-Roles）。

    仅在工厂显式传 ``allow_header_claims=True`` 时启用（本地 demo/测试形态）；
    缺省不信任客户端自声明头（CR P0-3 修复）。
    """
    roles = [r.strip() for r in request.headers.get("X-User-Roles", "").split(",") if r.strip()]
    return {"sub": request.headers.get("X-User-Id", ""), "roles": roles}


def denied_claims_provider(request: Request) -> dict:
    """缺省关门 claims 提供者（CR P0-3 修复）：未注入 claims_provider 且未显式开启
    allow_header_claims 时写操作一律 40301——绝不回落到信任请求头自声明角色。

    生产 = JWT claims 注入（claims_provider 由网关中间件解析后提供）。
    """
    raise BizError(403, CODE_FORBIDDEN, "未配置身份鉴权：邮件管理写操作缺省拒绝（生产须注入 JWT claims_provider）")


def _iso(value: datetime | None) -> str | None:
    """datetime → ISO（信封内时间统一 ISO 字符串）。"""
    return value.isoformat() if value else None


# --------------------------------------------------------------------------- 请求体模型


class EnrollBody(BaseModel):
    """序列开启请求体（邮件事件入口形态，SR §5④）。"""

    customer_id: int
    email: str | None = None
    lang: str = "en"
    context: dict = Field(default_factory=dict)


class TemplateUpsertBody(BaseModel):
    """模板 upsert 请求体（SR §5①）。"""

    template_key: str
    versions: dict[str, dict[str, str]]
    sample_vars: dict | None = None


class TrackEventBody(BaseModel):
    """追踪事件请求体（服务商回执/打开/退信 webhook 形态）。"""

    kind: str
    job_id: int | None = None
    message_id: str | None = None
    email: str | None = None
    dsn_status: str | None = None
    customer_id: int | None = None


# --------------------------------------------------------------------------- 应用工厂


def create_mail_app(*, scheduler: MailScheduler, tracker: DeliveryTracker,
                    templates: TemplateRegistry, store: MailStore,
                    api_key: str | None = None,
                    allowed_roles: tuple[str, ...] = ("admin", "marketer", "crm_operator"),
                    claims_provider: Callable[[Request], dict] | None = None,
                    allow_header_claims: bool = False,
                    clock: Callable[[], datetime] | None = None) -> FastAPI:
    """组装邮件自动化 API（依赖全部注入；clock 供统计日期缺省）。

    :param api_key: 内部事件入口（X-Api-Key）密钥。缺省读环境变量 KB_MAIL_WEBHOOK_KEY，
        均缺则启动即 SiteConfigError（CR P0-5 修复：无缺省密钥，fail-fast）。
    :param claims_provider: claims 提供者（生产 = JWT claims 注入）。
    :param allow_header_claims: 是否信任请求头自声明角色（X-User-Roles）。缺省 False
        ——未提供 claims_provider 时写操作一律 40301（CR P0-3 修复）；True 仅限本地 demo/测试。
    """
    # CR P0-5 修复：缺省密钥关门——参数 → env → 启动失败（中文报错）
    webhook_key = resolve_site_secret(api_key, "KB_MAIL_WEBHOOK_KEY", "邮件回执入口密钥（api_key）")
    # CR P0-3 修复：缺省不读请求头角色——未注入 claims_provider 即关门（40301）
    claims_of = claims_provider or (header_claims_provider if allow_header_claims else denied_claims_provider)
    app = FastAPI(title="site-service 邮件自动化 API", version="0.1.0",
                  description="FR-SITE-004（SR-SITE-004）：序列/任务/模板/追踪/统计。")

    def _error(request: Request, status: int, code: int, message: str) -> JSONResponse:
        return JSONResponse(status_code=status,
                            content=_envelope(_trace_id_of(request), code, message, None))

    @app.exception_handler(BizError)
    async def _on_biz(request: Request, exc: BizError) -> JSONResponse:
        return _error(request, exc.status, exc.code, str(exc))

    @app.exception_handler(TemplateError)
    async def _on_template_error(request: Request, exc: TemplateError) -> JSONResponse:
        return _error(request, 400, exc.code, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _on_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(request, 400, CODE_BAD_REQUEST, "参数校验失败")

    def _require_roles(request: Request) -> None:
        """声明身份时校验角色；未声明（服务间内部调用）由网关层鉴权兜底。"""
        claims = claims_of(request)
        if claims.get("sub") and not set(claims.get("roles") or []) & set(allowed_roles):
            raise BizError(403, CODE_FORBIDDEN, "无邮件管理权限")

    router = APIRouter(prefix=MAIL_PREFIX, tags=["site-mail"])

    @router.post("/sequences/{kind}/enroll", summary="序列开启（三类内置；dedup 幂等）")
    def enroll(kind: str, body: EnrollBody, request: Request) -> JSONResponse:
        if kind not in DEFAULT_SEQUENCE_DEFS:
            raise BizError(400, CODE_BAD_REQUEST, f"未知序列类型：{kind}")
        if not body.email:
            raise BizError(400, CODE_BAD_REQUEST, "缺少收件邮箱（留资邮箱必填）")
        _require_roles(request)
        outcome = scheduler.on_event(MailEvent(
            seq_type=kind, customer_id=body.customer_id, email=body.email,
            lang=body.lang, context=body.context, triggered_at=None))
        return _ok(request, {
            "sequence_id": outcome.sequence.seq_id if outcome.sequence else None,
            "created": outcome.created,
            "jobs": [{"job_id": j.job_id, "step_no": j.step_no,
                      "template_key": j.template_key, "schedule_at": _iso(j.schedule_at)}
                     for j in outcome.jobs],
        })

    @router.get("/jobs", summary="任务查询（序列/状态过滤）")
    def jobs(request: Request, sequence_id: int | None = Query(default=None),
             status: str | None = Query(default=None)) -> JSONResponse:
        items = list(store.jobs.values())
        if sequence_id is not None:
            items = [j for j in items if j.sequence_id == sequence_id]
        if status is not None:
            items = [j for j in items if j.status == status]
        return _ok(request, {"items": [
            {"job_id": j.job_id, "sequence_id": j.sequence_id, "seq_type": j.seq_type,
             "step_no": j.step_no, "recipient": j.recipient, "status": j.status,
             "schedule_at": _iso(j.schedule_at), "sent_at": _iso(j.sent_at),
             "message_id": j.message_id, "fail_reason": j.fail_reason}
            for j in sorted(items, key=lambda x: (x.sequence_id, x.step_no))],
            "total": len(items)})

    @router.post("/templates", summary="模板 upsert（draft/published；结构非法 40001）")
    def upsert_template(body: TemplateUpsertBody, request: Request) -> JSONResponse:
        _require_roles(request)
        try:
            templates.upsert(body.template_key, body.versions, sample_vars=body.sample_vars)
        except ValueError as exc:
            raise BizError(400, CODE_BAD_REQUEST, str(exc)) from exc
        return _ok(request, {"template_key": body.template_key, "status": "published",
                             "langs": sorted(body.versions)})

    @router.post("/tracking/event", summary="追踪事件 webhook（X-Api-Key 内部鉴权）")
    def tracking_event(body: TrackEventBody, request: Request) -> JSONResponse:
        if request.headers.get("X-Api-Key", "") != webhook_key:
            return _error(request, 401, CODE_UNAUTHORIZED, "内部事件入口鉴权失败")
        outcome = tracker.on_event(TrackEvent(
            kind=body.kind, job_id=body.job_id, message_id=body.message_id,
            email=body.email, dsn_status=body.dsn_status, customer_id=body.customer_id))
        return _ok(request, outcome)

    @router.get("/stats", summary="统计看板（送达率 + ≥95% 判定）")
    def stats(request: Request,
              seq_type: str | None = Query(default=None),
              date_from: str | None = Query(default=None),
              date_to: str | None = Query(default=None)) -> JSONResponse:
        try:
            d_from = date.fromisoformat(date_from) if date_from else None
            d_to = date.fromisoformat(date_to) if date_to else None
        except ValueError as exc:
            raise BizError(400, CODE_BAD_REQUEST, "日期格式非法（须 ISO yyyy-mm-dd）") from exc
        data = tracker.stats(seq_type=seq_type, date_from=d_from, date_to=d_to)
        data["meets_target"] = tracker.passes_delivery_rate(data)  # SRS §16.4 ≥95%
        return _ok(request, data)

    app.include_router(router)
    return app
