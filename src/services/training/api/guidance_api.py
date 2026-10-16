# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""引导式交互 API 路由（NFR-USA-03，迭代 1.1）：导览获取 / 状态上报 / 空状态文案。

本文件为 guidance_api 独占文件（迭代 1.1），只挂 guidance 三个端点，不触碰
training-service 其他路由装配。端点（统一信封 code/message/data/trace_id，
04-接口设计 §1.3 口径，与 services/kb/api/routes.py 同风格）：

| 端点 | 方法与路径 | 说明 |
|---|---|---|
| 当前导览 | GET  /api/v1/training/guidance/tour | 出参 {tour, current_version, tour_id,
|          |     ?tour_id=&trainee_id=            | seen_version?, major_changed?, should_show?}
|          |                                      | ——含版本比较与克制门控结论（trainee_id 可选）；
| 状态上报 | POST /api/v1/training/guidance/tour/state | 入参 {trainee_id, tour_id?,
|          |                                          | outcome: done|skipped, last_step_seq?,
|          |                                          | seen_version?}——前端导览销毁回调上报
|          |                                          | （跳过/完成），出参 {state, should_show,
|          |                                          | current_version}；
| 空状态   | GET  /api/v1/training/guidance/empty-states | 出参 {scenes: [...]}（8 类场景表），
|          |     ?scene_id=                             | scene_id 精确过滤（前端空态组件按页取用）。

装配方式（依赖注入，路由层零业务实现）：
- 生产：宿主 FastAPI 经 ``app.state.guidance_service`` 注入 GuidanceService
  （DB/Redis 存储实现），再 ``app.include_router(guidance_api)``；
- 缺省：未注入时懒加载默认实例（内置 v1 导览 + 内存状态存储）——TestClient
  与本地开发零配置可用；
- 错误信封：``install_guidance_error_handlers`` 将 RequestValidationError →
  40001（data.errors 定位字段）、未知导览 KeyError → 40404，与仓库既有
  kb 路由信封口径一致；``create_guidance_app`` 为测试/本地一体化装配入口。

权限口径（SR-NFR-USA-03 §8）：三端点均为学员登录态下的本人数据读写；迭代 1.1
鉴权依赖（JWT 解析 → trainee_id）随 training-service 装配任务接入，本迭代以
显式 trainee_id 入参承接（NFR-SEC-02 全链接入项，已在文档登记）。

依据文档：
- docs/07-实现方案/NFR/SR-NFR-USA-03-引导式交互.md §5（端点形态）、§6.1/§6.2、§8
- docs/07-实现方案/TR/SR-TR-001-AI导师系统.md §5（响应信封与错误码风格）
- docs/04-接口设计 §1（通用规范：信封 / trace_id / 错误码表）
"""
from __future__ import annotations

from foundation.core.envelope import envelope

import uuid
from typing import Any

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from services.training.core.guidance.config import DEFAULT_TOUR_ID
from services.training.core.guidance.service import GuidanceService, TourOutcome

#: 路由前缀与端点常量（供测试与前端 API 客户端共用同一口径）
GUIDANCE_PREFIX: str = "/api/v1/training/guidance"
TOUR_PATH: str = f"{GUIDANCE_PREFIX}/tour"
TOUR_STATE_PATH: str = f"{GUIDANCE_PREFIX}/tour/state"
EMPTY_STATES_PATH: str = f"{GUIDANCE_PREFIX}/empty-states"

#: 成功业务码 / 参数校验失败码 / 资源不存在码（04-接口设计 §1.3）
CODE_OK: int = 0
CODE_BAD_REQUEST: int = 40001
CODE_NOT_FOUND: int = 40404


# --------------------------------------------------------------------------- 入参/出参模型


class TourStateReportRequest(BaseModel):
    """POST /tour/state 入参：前端导览销毁回调（SR §6.2 onDestroyed）的上报体。"""

    model_config = ConfigDict(frozen=True)

    trainee_id: int = Field(ge=1, description="学员 ID（生产由鉴权态取本人，见模块 docstring 权限口径）")
    tour_id: str = Field(default=DEFAULT_TOUR_ID, description="导览标识，缺省 first-login")
    outcome: TourOutcome = Field(description="终态：done=完整走完，skipped=跳过/中途终止")
    last_step_seq: int | None = Field(
        default=None, ge=1, description="终止时所处步骤号（skipped 时携带，供流失分析）"
    )
    seen_version: str | None = Field(
        default=None, description="前端已看版本（localStorage 镜像口径，major.minor）"
    )


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（CR A2 全面委托化：foundation.core.envelope 单点实现）。"""
    return envelope(code, message, data, trace_id)


def _request_trace_id(request: Request) -> str:
    """取本请求 trace_id：优先透传 X-Trace-Id 头，缺失本地生成 16 位 hex（04 §1.4）。"""
    forwarded = request.headers.get("X-Trace-Id", "").strip()
    return forwarded or uuid.uuid4().hex[:16]


def get_guidance_service(request: Request) -> GuidanceService:
    """依赖注入：优先取宿主 app.state.guidance_service，缺省懒加载默认实例。

    默认实例 = 内置 v1 导览注册表 + 内存状态存储（进程内单例，TestClient/本地
    开发零配置）；生产装配由 training-service 宿主覆盖 app.state 注入 DB 实现。
    """
    service = getattr(request.app.state, "guidance_service", None)
    if service is None:
        service = _default_service()
    return service


_default_service_instance: GuidanceService | None = None


def _default_service() -> GuidanceService:
    """进程级默认 GuidanceService（懒加载；测试需要隔离时经 app.state 显式注入）。"""
    global _default_service_instance
    if _default_service_instance is None:
        _default_service_instance = GuidanceService()
    return _default_service_instance


# --------------------------------------------------------------------------- 路由定义


guidance_api = APIRouter(prefix=GUIDANCE_PREFIX, tags=["training-guidance"])


@guidance_api.get("/tour")
def get_tour(
    request: Request,
    tour_id: str = Query(default=DEFAULT_TOUR_ID, description="导览标识，缺省 first-login"),
    trainee_id: int | None = Query(default=None, ge=1, description="学员 ID（携带则附版本比较与门控结论）"),
) -> dict[str, Any]:
    """当前导览 + 版本比较（trainee_id 可选）：前端 GuidanceTour.vue 的拉取端点。

    - 手动入口（页脚「使用指引」）不带 trainee_id，直接取配置渲染（不受门控限制）；
    - 自动触发带 trainee_id：``should_show`` 即克制门控结论（回访不打扰、主版本
      升级重显一次，规则见 core/guidance/state.py）。
    """
    service = get_guidance_service(request)
    try:
        view = service.tour_view(tour_id, trainee_id)
    except KeyError as exc:
        raise _tour_not_found(request, exc) from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", view)


@guidance_api.post("/tour/state")
def post_tour_state(payload: TourStateReportRequest, request: Request) -> dict[str, Any]:
    """上报导览终态（跳过/完成）：落服务端存储并回执门控结论。

    幂等口径：同 (trainee_id, tour_id) 覆盖写（终态以最后一次上报为准）；
    跳过与完成同权重（SR §6.2「不区分对待」），均不再自动弹出（除非主版本升级）。
    """
    service = get_guidance_service(request)
    try:
        receipt = service.report_tour_state(
            tour_id=payload.tour_id,
            trainee_id=payload.trainee_id,
            outcome=payload.outcome,
            last_step_seq=payload.last_step_seq,
            seen_version=payload.seen_version,
        )
    except KeyError as exc:
        raise _tour_not_found(request, exc) from exc
    except ValueError as exc:
        # seen_version 格式非法（TourVersionError 为 ValueError 子类）→ 40001
        raise _bad_request(f"导览状态上报参数非法：{exc}") from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", receipt)


@guidance_api.get("/empty-states")
def get_empty_states(
    request: Request,
    scene_id: str | None = Query(default=None, description="场景标识，携带则精确过滤单条"),
) -> dict[str, Any]:
    """空状态引导文案表（8 类）：前端 EmptyState.vue 按页面场景消费。

    scene_id 未命中时返回空列表（前端回退默认文案，不阻塞页面——引导层故障
    不产生用户可见错误，SR §8）。
    """
    service = get_guidance_service(request)
    scenes = service.empty_states()
    if scene_id is not None:
        scenes = [spec for spec in scenes if spec.scene_id == scene_id]
    return _envelope(
        _request_trace_id(request),
        CODE_OK,
        "ok",
        {"scenes": [spec.model_dump() for spec in scenes]},
    )


# --------------------------------------------------------------------------- 错误信封与装配


class _GuidanceApiError(Exception):
    """路由层业务异常（内部）：携带业务码 / HTTP 状态 / 文案，由信封处理器展开。"""

    def __init__(self, code: int, http_status: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.data = data


def _tour_not_found(request: Request, exc: KeyError) -> _GuidanceApiError:
    """未知导览标识 → 40404（HTTP 404，信封 data 带 tour_id 定位）。"""
    return _GuidanceApiError(
        CODE_NOT_FOUND,
        404,
        f"导览不存在：{exc.args[0] if exc.args else '未知'}",
        None,
    )


def _bad_request(message: str) -> _GuidanceApiError:
    """参数非法 → 40001（HTTP 400）。"""
    return _GuidanceApiError(CODE_BAD_REQUEST, 400, message, None)


def install_guidance_error_handlers(app: FastAPI) -> FastAPI:
    """注册 guidance 路由的错误信封处理器（信封格式全路由一致）。

    - ``_GuidanceApiError``：路由层主动抛出的业务错误（40404/40001 族）；
    - ``RequestValidationError``：Pydantic 校验失败 → 40001（data.errors 定位字段，
      loc/msg/type 三键 JSON 安全子集，与 kb 路由同口径）。
    """
    app.add_exception_handler(_GuidanceApiError, _on_guidance_api_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    return app


async def _on_guidance_api_error(request: Request, exc: _GuidanceApiError) -> JSONResponse:
    """_GuidanceApiError → 统一信封（HTTP 状态 + 业务码）。"""
    return JSONResponse(
        status_code=exc.http_status,
        content=_envelope(_request_trace_id(request), exc.code, exc.message, exc.data),
    )


async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """RequestValidationError → 40001 信封（data.errors 为 loc/msg/type 子集）。"""
    errors = [
        {
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=400,
        content=_envelope(_request_trace_id(request), CODE_BAD_REQUEST, "参数校验失败", {"errors": errors}),
    )


def create_guidance_app(service: GuidanceService | None = None) -> FastAPI:
    """测试/本地一体化装配：FastAPI 应用 + guidance 路由 + 错误信封处理器。

    生产装配不走本函数（training-service 宿主 include_router + app.state 注入）；
    传入 ``service`` 时挂到 app.state（依赖注入优先级高于默认懒加载实例）。
    """
    app = FastAPI(title="training-guidance", version="0.1.0")
    if service is not None:
        app.state.guidance_service = service
    app.include_router(guidance_api)
    install_guidance_error_handlers(app)
    return app
