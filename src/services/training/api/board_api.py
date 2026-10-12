# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""主管看板 API（FR-TR-006，SR-TR-006 §5 三级看板端点细化，迭代 1.3）。

端点（统一信封 code/message/data/trace_id，04-接口设计 §1.3）：
- GET /api/v1/training/board/overview            团队总览（KPI 卡 + 标记计数 + 雷达均值）
- GET /api/v1/training/board/trainee/{trainee_id} 个人详情（进度/成绩/雷达/标记依据 evidence）
- GET /api/v1/training/board/flags?flag_type=     高潜力/需干预名单（scope 过滤 + 类型筛选）

装配方式（与 drill_api/guidance_api 同模式）：数据源/聚合存储/识别登记/团队名录经
``create_board_app`` 工厂注入（demo 全内存形态）；鉴权以 claims_provider 注入
（生产由全局 JWT 中间件解析后写入 request.state，04 §1.2），越权 40301 + 审计
rbac_deny 留痕（NFR-SEC-02/03）。
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from services.training.core.board.aggregate import (
    AuditCallback,
    BoardDataSource,
    DailyStatsStore,
    FlagStore,
    ScopeDenied,
    TeamDirectory,
    ensure_in_scope,
    resolve_scope,
)

# --------------------------------------------------------------------------- 路径与错误码常量

#: 路由前缀与端点常量（供测试与前端 API 客户端共用同一口径）
BOARD_PREFIX: str = "/api/v1/training/board"
OVERVIEW_PATH: str = f"{BOARD_PREFIX}/overview"
DETAIL_PATH: str = f"{BOARD_PREFIX}/trainee/{{trainee_id}}"
FLAGS_PATH: str = f"{BOARD_PREFIX}/flags"

#: 业务码（04-接口设计 §1.3）：成功 / 越权 / 不存在 / 入参校验失败
CODE_OK: int = 0
CODE_FORBIDDEN: int = 40301
CODE_BAD_REQUEST: int = 40001
CODE_NOT_FOUND: int = 40404

#: 合法的标记类型筛选值（flag_type 查询参数）
FLAG_TYPE_VALUES: tuple[str, ...] = ("high_potential", "need_intervention")

#: 空团队/角色缺省团队名（demo 无 train_team 实体，命名占位）
DEFAULT_TEAM_NAME: str = "本团队"


class NotFoundError(Exception):
    """资源不存在（40404；与越权 40301 区分）。"""

    code = CODE_NOT_FOUND


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（04 §1.3）：四键齐全。"""
    return {"code": code, "message": message, "data": data, "trace_id": trace_id}


def _trace_id_of(request: Request, x_trace_id: str) -> str:
    """trace_id：X-Trace-Id 头优先，缺失新生成（04 §1.4）。"""
    return (x_trace_id or "").strip() or uuid.uuid4().hex[:16]


def header_claims_provider(request: Request) -> dict[str, Any]:
    """请求头 claims 提供者：demo 以请求头承载（X-User-Id/X-User-Roles/X-Team-Ids）。

    仅在工厂显式传 ``allow_header_claims=True`` 时启用（本地 demo/测试形态）；
    缺省不信任客户端自声明头（CR P0-3 修复）。
    """
    roles = [r.strip() for r in request.headers.get("X-User-Roles", "").split(",") if r.strip()]
    team_ids = [int(t) for t in request.headers.get("X-Team-Ids", "").split(",") if t.strip().isdigit()]
    return {
        "sub": request.headers.get("X-User-Id", ""),
        "roles": roles,
        "data_scope": {"team_ids": team_ids},
    }


def denied_claims_provider(request: Request) -> dict[str, Any]:
    """缺省关门 claims 提供者（CR P0-3 修复）：未注入 claims_provider 且未显式开启
    allow_header_claims 时一律 40301——绝不回落到信任请求头自声明角色。

    生产 = JWT claims 注入（claims_provider 由全局鉴权中间件解析后提供，04 §1.2）。
    """
    raise ScopeDenied("未配置身份鉴权：主管看板缺省拒绝访问（生产须注入 JWT claims_provider）")


# --------------------------------------------------------------------------- 看板读模型（API 内聚装配）


def build_overview(
    *,
    scope: Any,
    source: BoardDataSource,
    store: DailyStatsStore,
    flag_store: FlagStore,
    today: date,
    team_name: str = DEFAULT_TEAM_NAME,
) -> dict[str, Any]:
    """团队总览读模型：人数/认证/标记计数/今日活跃/雷达均值（SR §5① data 形态）。"""
    trainee_ids = sorted(scope.trainee_ids)
    path_values: list[float] = []
    radar_sums: dict[str, float] = {}
    radar_counts: dict[str, int] = {}
    active_today = 0
    certified_cnt = 0
    for tid in trainee_ids:
        row = store.latest(tid, on_or_before=today)
        profile = source.trainee_profile(tid)
        path = row.path_pct if row is not None and row.path_pct else profile.path_pct
        path_values.append(path)
        radar = row.radar if row is not None and row.radar else profile.radar_current
        for dim, value in radar.items():
            radar_sums[dim] = radar_sums.get(dim, 0.0) + float(value)
            radar_counts[dim] = radar_counts.get(dim, 0) + 1
        if row is not None and (row.study_minutes or row.lessons_done or row.drill_count or row.exam_count):
            active_today += 1
        if profile.cert_status == "certified":
            certified_cnt += 1
    counts = flag_store.count_by_type(trainee_ids=trainee_ids)
    return {
        "team_name": team_name,
        "trainee_total": len(trainee_ids),
        "avg_path_pct": round(sum(path_values) / len(path_values), 1) if path_values else 0.0,
        "certified_cnt": certified_cnt,
        "high_potential_cnt": counts.get("high_potential", 0),
        "need_intervention_cnt": counts.get("need_intervention", 0),
        "active_today": active_today,
        "radar_avg": {
            dim: round(radar_sums[dim] / radar_counts[dim], 1)
            for dim in sorted(radar_sums)
            if radar_counts.get(dim)
        },
    }


def build_detail(
    *,
    trainee_id: int,
    scope: Any,
    source: BoardDataSource,
    store: DailyStatsStore,
    flag_store: FlagStore,
    today: date,
) -> dict[str, Any]:
    """个人详情读模型：进度/认证/当前雷达/标记依据（SR §5③ data 形态）。"""
    try:
        profile = source.trainee_profile(trainee_id)
    except KeyError as exc:
        raise NotFoundError(f"学员不存在：{trainee_id}") from exc
    row = store.latest(trainee_id, on_or_before=today)
    return {
        "trainee_id": trainee_id,
        "name": profile.name,
        "path_pct": row.path_pct if row is not None and row.path_pct else profile.path_pct,
        "cert_status": profile.cert_status,
        "radar_current": dict(row.radar) if row is not None and row.radar else dict(profile.radar_current),
        "flags": [f.to_dict() for f in flag_store.flags_of(trainee_id)],
    }


# --------------------------------------------------------------------------- 应用工厂


def create_board_app(
    *,
    source: BoardDataSource,
    store: DailyStatsStore,
    flag_store: FlagStore,
    directory: TeamDirectory,
    audit_cb: AuditCallback | None = None,
    claims_provider: Callable[[Request], dict[str, Any]] | None = None,
    allow_header_claims: bool = False,
    today_fn: Callable[[], date] | None = None,
    team_name: str = DEFAULT_TEAM_NAME,
    version: str = "0.1.0",
) -> FastAPI:
    """组装主管看板应用（组件契约见模块 docstring；全部依赖鸭子类型注入）。

    :param claims_provider: claims 提供者（生产 = JWT claims 注入）。
    :param allow_header_claims: 是否信任请求头自声明角色（X-User-Roles/X-Team-Ids）。
        缺省 False——未提供 claims_provider 时一律 40301（CR P0-3 修复）；
        True 仅限本地 demo/测试。
    """
    # CR P0-3 修复：缺省不读请求头角色——未注入 claims_provider 即关门（40301）
    claims_of = (
        claims_provider
        if claims_provider is not None
        else (header_claims_provider if allow_header_claims else denied_claims_provider)
    )
    today_of = today_fn if today_fn is not None else date.today

    app = FastAPI(
        title="training-service 主管看板 API",
        version=version,
        description="FR-TR-006 主管看板（SR-TR-006）：团队总览 / 个人详情（标记依据可解释）/ 识别名单。",
    )

    def _error(request: Request, status: int, code: int, message: str) -> JSONResponse:
        """错误信封（trace_id 贯穿）。"""
        trace_id = _trace_id_of(request, request.headers.get("X-Trace-Id", ""))
        return JSONResponse(status_code=status, content=_envelope(trace_id, code, message, None))

    @app.exception_handler(ScopeDenied)
    async def _on_scope_denied(request: Request, exc: ScopeDenied) -> JSONResponse:
        """越权 → HTTP 403 + 40301（审计已由 ensure_in_scope 留痕）。"""
        return _error(request, 403, exc.code, str(exc))

    @app.exception_handler(NotFoundError)
    async def _on_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        """资源不存在 → HTTP 404 + 40404。"""
        return _error(request, 404, exc.code, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """入参校验失败 → HTTP 400 + 40001。"""
        return _error(request, 400, CODE_BAD_REQUEST, "参数校验失败")

    router = APIRouter(prefix=BOARD_PREFIX, tags=["training-board"])

    @router.get("/overview", summary="团队总览（KPI 卡）")
    def overview(request: Request) -> JSONResponse:
        """① 团队总览：仅本团队学员（scope 注入第一防线），无数据日回退档案值。"""
        scope = resolve_scope(claims_of(request), directory)
        data = build_overview(
            scope=scope, source=source, store=store, flag_store=flag_store,
            today=today_of(), team_name=team_name,
        )
        trace_id = _trace_id_of(request, request.headers.get("X-Trace-Id", ""))
        return JSONResponse(content=_envelope(trace_id, CODE_OK, "ok", data))

    @router.get("/trainee/{trainee_id}", summary="个人详情（含标记依据）")
    def trainee_detail(trainee_id: int, request: Request) -> JSONResponse:
        """③ 个人详情：不存在 → 40404；越权 → 40301 + 审计；flags 携带 evidence。"""
        trace_id = _trace_id_of(request, request.headers.get("X-Trace-Id", ""))
        scope = resolve_scope(claims_of(request), directory)
        try:  # 先存在性校验（40404，避免与越权 40301 混淆）……
            source.trainee_profile(trainee_id)
        except KeyError as exc:
            raise NotFoundError(f"学员不存在：{trainee_id}") from exc
        ensure_in_scope(scope, trainee_id, audit_cb=audit_cb)  # ……再越权校验（留痕 + 拒绝）
        data = build_detail(
            trainee_id=trainee_id, scope=scope, source=source, store=store,
            flag_store=flag_store, today=today_of(),
        )
        return JSONResponse(content=_envelope(trace_id, CODE_OK, "ok", data))

    @router.get("/flags", summary="高潜力/需干预名单")
    def flags(
        request: Request,
        flag_type: str | None = Query(default=None, description="标记类型筛选"),
    ) -> JSONResponse:
        """④ 识别名单：scope 过滤（他团队不可见）+ flag_type 筛选 + 证据快照。"""
        trace_id = _trace_id_of(request, request.headers.get("X-Trace-Id", ""))
        scope = resolve_scope(claims_of(request), directory)
        if flag_type is not None and flag_type not in FLAG_TYPE_VALUES:
            raise ValueError(f"flag_type 取值须为 {FLAG_TYPE_VALUES} 之一")
        items = [
            {
                "trainee_id": flag.trainee_id,
                "name": source.trainee_profile(flag.trainee_id).name,
                "flag_type": flag.flag_type,
                "rule_key": flag.rule_key,
                "rule_name": flag.rule_name,
                "first_hit_at": flag.first_hit_at,
                "last_eval_at": flag.last_eval_at,
                "evidence": dict(flag.evidence),
                "status": flag.status,
            }
            for flag in flag_store.list_flags(flag_type=flag_type, trainee_ids=scope.trainee_ids)
        ]
        return JSONResponse(
            content=_envelope(trace_id, CODE_OK, "ok", {"items": items, "total": len(items)})
        )

    app.include_router(router)

    @app.exception_handler(ValueError)
    async def _on_value_error(request: Request, exc: ValueError) -> JSONResponse:
        """查询参数非法 → HTTP 400 + 40001。"""
        return _error(request, 400, CODE_BAD_REQUEST, str(exc))

    return app


__all__ = [
    "BOARD_PREFIX",
    "OVERVIEW_PATH",
    "DETAIL_PATH",
    "FLAGS_PATH",
    "CODE_OK",
    "CODE_FORBIDDEN",
    "CODE_BAD_REQUEST",
    "CODE_NOT_FOUND",
    "build_overview",
    "build_detail",
    "create_board_app",
]
