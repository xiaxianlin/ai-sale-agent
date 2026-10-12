# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-006 管理后台 V2 看板 API（SR-SITE-006 §5/§6.3，Phase 3 第二批；demo 全内存装配）。

端点（统一信封；RBAC site:dashboard:read → supervisor/admin，销售默认无，NFR-SEC-02）：
- GET /api/v1/admin/site/overview      五指标总览（访客量/会话量/留资率/挽回率/邮件效果）
- GET /api/v1/admin/site/funnel        漏斗（访问→会话→留资→成交 + 层间转化率）
- GET /api/v1/admin/site/mail-effect   邮件效果表（三类序列 × 送达/打开/失败率）
- GET /api/v1/admin/site/drill         指标下钻（列定义注册表驱动）

错误码：40001 参数/未注册指标；40301 无 site:dashboard:read 权限。
率值以百分比输出（核心层小数 ×100，保留两位；分母 0 → null 显示「—」）。
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from services.site.core.admin_v2.metrics import DrillUnknown, SiteMetrics

# --------------------------------------------------------------------------- 路径与错误码

ADMIN_PREFIX = "/api/v1/admin/site"
ADMIN_OVERVIEW_PATH = f"{ADMIN_PREFIX}/overview"
ADMIN_FUNNEL_PATH = f"{ADMIN_PREFIX}/funnel"
ADMIN_MAIL_EFFECT_PATH = f"{ADMIN_PREFIX}/mail-effect"
ADMIN_DRILL_PATH = f"{ADMIN_PREFIX}/drill"

CODE_OK = 0
CODE_BAD_REQUEST = 40001
CODE_FORBIDDEN = 40301


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
    """请求头 claims 提供者（demo 请求头承载；生产由 V1 基座 JWT 中间件解析）。

    仅在工厂显式传 ``allow_header_claims=True`` 时启用（本地 demo/测试形态）；
    缺省不信任客户端自声明头（CR P0-3 修复）。
    """
    roles = [r.strip() for r in request.headers.get("X-User-Roles", "").split(",") if r.strip()]
    return {"sub": request.headers.get("X-User-Id", ""), "roles": roles}


def denied_claims_provider(request: Request) -> dict:
    """缺省关门 claims 提供者（CR P0-3 修复）：未注入 claims_provider 且未显式开启
    allow_header_claims 时一律 40301——绝不回落到信任请求头自声明角色。

    生产 = JWT claims 注入（claims_provider 由网关中间件解析后提供）。
    """
    raise BizError(403, CODE_FORBIDDEN, "未配置身份鉴权：管理端缺省拒绝访问（生产须注入 JWT claims_provider）")


def _pct(rate: float | None) -> float | None:
    """小数率 → 百分比（两位；None 透传表示「—」）。"""
    return round(rate * 100, 2) if rate is not None else None


def _next_day(value: date) -> date:
    """次日（区间推进辅助）。"""
    from datetime import timedelta
    return value + timedelta(days=1)


def _parse_day(value: str, name: str) -> date:
    """日期参数解析（非法 → 40001）。"""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BizError(400, CODE_BAD_REQUEST, f"{name} 日期格式非法（须 ISO yyyy-mm-dd）") from exc


# --------------------------------------------------------------------------- 应用工厂


def create_admin_app(*, metrics: SiteMetrics,
                     allowed_roles: tuple[str, ...] = ("supervisor", "admin"),
                     claims_provider: Callable[[Request], dict] | None = None,
                     allow_header_claims: bool = False) -> FastAPI:
    """组装 V2 看板 API（只读消费预聚合层；全部依赖注入）。

    :param claims_provider: claims 提供者（生产 = JWT claims 注入）。
    :param allow_header_claims: 是否信任请求头自声明角色（X-User-Roles）。缺省 False
        ——未提供 claims_provider 时一律 40301（CR P0-3 修复）；True 仅限本地 demo/测试。
    """
    # CR P0-3 修复：缺省不读请求头角色——未注入 claims_provider 即关门（40301）
    claims_of = claims_provider or (header_claims_provider if allow_header_claims else denied_claims_provider)
    app = FastAPI(title="site-service 管理后台 V2 看板 API", version="0.1.0",
                  description="FR-SITE-006（SR-SITE-006）：独立站五指标看板/漏斗/邮件效果/下钻。")

    def _error(request: Request, status: int, code: int, message: str) -> JSONResponse:
        return JSONResponse(status_code=status,
                            content=_envelope(_trace_id_of(request), code, message, None))

    @app.exception_handler(BizError)
    async def _on_biz(request: Request, exc: BizError) -> JSONResponse:
        return _error(request, exc.status, exc.code, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _on_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(request, 400, CODE_BAD_REQUEST, "参数校验失败")

    def _require_perm(request: Request) -> None:
        """site:dashboard:read 权限校验（授予主管/管理员；越权 40301）。"""
        claims = claims_of(request)
        if not set(claims.get("roles") or []) & set(allowed_roles):
            raise BizError(403, CODE_FORBIDDEN, "缺少 site:dashboard:read 权限")

    def _range(request: Request, date_from: str, date_to: str) -> tuple[date, date]:
        """参数解析 + 角色校验 + 读路径新鲜度兜底（区间内逐日补算，幂等，SR §6.2）。"""
        _require_perm(request)
        d_from, d_to = _parse_day(date_from, "date_from"), _parse_day(date_to, "date_to")
        if d_from > d_to:
            raise BizError(400, CODE_BAD_REQUEST, "date_from 不得晚于 date_to")
        if (d_to - d_from).days > 366:
            raise BizError(400, CODE_BAD_REQUEST, "查询区间过长（≤366 天）")
        moment = d_from
        while moment <= d_to:  # 当日/回溯 staleness 兜底：读前补算（幂等覆盖写）
            metrics.aggregate_day(moment)
            moment = _next_day(moment)
        return d_from, d_to

    router = APIRouter(prefix=ADMIN_PREFIX, tags=["site-dashboard"])

    @router.get("/overview", summary="五指标总览（KPI 卡）")
    def overview(request: Request, date_from: str = Query(...), date_to: str = Query(...)) -> JSONResponse:
        d_from, d_to = _range(request, date_from, date_to)
        ov = metrics.overview(d_from, d_to)
        ov["lead_rate"] = _pct(ov["lead_rate"])
        ov["recover_rate"] = _pct(ov["recover_rate"])
        ov["mail"]["delivery_rate"] = _pct(ov["mail"]["delivery_rate"])
        ov["mail"]["open_rate"] = _pct(ov["mail"]["open_rate"])
        return _ok(request, ov)

    @router.get("/funnel", summary="漏斗（访问→会话→留资→成交）")
    def funnel(request: Request, date_from: str = Query(...), date_to: str = Query(...)) -> JSONResponse:
        d_from, d_to = _range(request, date_from, date_to)
        f = metrics.funnel(d_from, d_to)
        f["rates"] = {k: _pct(v) for k, v in f["rates"].items()}
        f["layers"] = [{"name": name, "value": value} for name, value in f["layers"]]
        return _ok(request, f)

    @router.get("/mail-effect", summary="邮件效果表（三类序列 × 三率）")
    def mail_effect(request: Request, date_from: str = Query(...), date_to: str = Query(...),
                    job_type: str | None = Query(default=None)) -> JSONResponse:
        d_from, d_to = _range(request, date_from, date_to)
        effect = metrics.mail_effect(d_from, d_to, job_type=job_type)
        for row in effect["by_type"].values():
            for key in ("delivery_rate", "open_rate", "failed_rate"):
                row[key] = _pct(row[key])
        effect["mail_backfill_until"] = effect["mail_backfill_until"].isoformat()  # date → ISO
        return _ok(request, effect)

    @router.get("/drill", summary="指标下钻（指标→明细，列定义随响应）")
    def drill(request: Request, metric: str = Query(...), date: str = Query(...),
              channel: str | None = Query(default=None),
              page: int = Query(default=1, ge=1), page_size: int = Query(default=20, le=100)) -> JSONResponse:
        _require_perm(request)
        day = _parse_day(date, "date")
        try:
            out = metrics.drill(metric, day, channel=channel, page=page, page_size=page_size)
        except DrillUnknown as exc:  # 未注册指标 → 40001（DRILL 注册表收口）
            raise BizError(400, CODE_BAD_REQUEST, str(exc)) from exc
        out["date"] = out["date"] if isinstance(out["date"], str) else out["date"].isoformat()
        return _ok(request, out)

    app.include_router(router)
    return app
