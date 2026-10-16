# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""学习路径 API 路由（FR-TR-005，迭代 1.3；SR-TR-005 §5 接口增量，path_api 独占文件）。

端点（统一信封 code/message/data/trace_id，04-接口设计 §1.3 口径）：

| 端点 | 方法与路径 | 说明 |
|---|---|---|
| 当前路径 | GET  /api/v1/training/path/{trainee_id} | 分段视图（薄弱突破置顶）+ 实时
|   |   | 状态/锁定 + 完成度统计（学员端进度条与主管看板同源） |
| 生成/重算 | POST /api/v1/training/path/{trainee_id} | 取雷达最新快照重算（version+1、
|   |   | 已完成保留），data.kept_completed 供「已完成 N 课时已保留」横幅 |
| 进度上报 | POST /api/v1/training/path/{trainee_id}/progress | {event_uid, kind,
|   |   | lesson_uid, score?} → 状态机结算；event_uid 幂等重放返回首次结果 |

错误码映射：数值码沿用仓库口径（40001 入参 / 40404 不存在 / 40902 状态冲突），
path 业务码以 ``data.biz_code`` 承载 SR-TR-005 §5 的语义：
TR-3100 无雷达快照（404）、TR-3101 无进行中路径（404）、TR-3102 课时不在路径（404）、
TR-3103 前置课时未完成锁定（409）、TR-3104 进度状态迁移非法（409）。

装配方式（与 drill_api/radar_api 同模式）：生产宿主经 ``app.state.path_planner``
注入 PathPlanner 后 ``include_router(path_api)``；测试/本地用 :func:`create_path_app`。
鉴权依赖（JWT → trainee_id）随 training-service 装配任务接入，本迭代以显式
trainee_id 入参承接（NFR-SEC-02 登记项）。

依据文档：docs/07-实现方案/TR/SR-TR-005-学习路径系统.md §5、§6.1~§6.3、§8；
         docs/04-接口设计 §1（通用规范）。
"""
from __future__ import annotations

from foundation.core.envelope import envelope

import uuid
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from services.training.core.path.planner import (
    PathError,
    PathPlanner,
)

# --------------------------------------------------------------------------- 路径与错误码常量

#: 路由前缀与端点常量（供测试与前端 API 客户端共用同一口径）
PATH_PREFIX: str = "/api/v1/training/path"
PATH_VIEW_PATH: str = f"{PATH_PREFIX}/{{trainee_id}}"
PATH_REPLAN_PATH: str = f"{PATH_PREFIX}/{{trainee_id}}"  # 与视图同路径，POST=生成/重算
PATH_PROGRESS_PATH: str = f"{PATH_PREFIX}/{{trainee_id}}/progress"

#: 成功业务码 / 入参校验失败 / 资源不存在 / 状态冲突（04-接口设计 §1.3）
CODE_OK: int = 0
CODE_BAD_REQUEST: int = 40001
CODE_NOT_FOUND: int = 40404
CODE_CONFLICT: int = 40902

#: 进度事件类型（SR §6.2：quiz_submitted 按通过线派生结算，其余为状态机直接事件）
ProgressEventKind = Literal[
    "lesson_started", "lesson_finished", "quiz_passed", "quiz_failed", "quiz_submitted"
]


# --------------------------------------------------------------------------- 入参模型


class ProgressRequest(BaseModel):
    """POST /progress 入参：课时进度事件（event_uid 幂等键；测验事件须携带 score）。"""

    model_config = ConfigDict(frozen=True)

    event_uid: str = Field(min_length=1, description="事件幂等键（客户端生成，重放返回首次结果）")
    kind: ProgressEventKind = Field(
        description="事件类型：lesson_started/lesson_finished/quiz_passed/quiz_failed/quiz_submitted"
    )
    lesson_uid: str = Field(min_length=1, description="课时 uid（须在当前路径内且未被前置锁定）")
    score: float | None = Field(
        default=None, ge=0, le=100, description="测验得分（quiz_* 事件必填；lesson_* 事件留空）"
    )


# --------------------------------------------------------------------------- 信封与依赖注入


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（CR A2 全面委托化：foundation.core.envelope 单点实现）。"""
    return envelope(code, message, data, trace_id)


def _request_trace_id(request: Request) -> str:
    """取本请求 trace_id：优先透传 X-Trace-Id 头，缺失本地生成 16 位 hex（04 §1.4）。"""
    forwarded = request.headers.get("X-Trace-Id", "").strip()
    return forwarded or uuid.uuid4().hex[:16]


def get_path_planner(request: Request) -> PathPlanner:
    """依赖注入：取宿主 app.state.path_planner（生产/测试装配时注入）。"""
    planner: PathPlanner | None = getattr(request.app.state, "path_planner", None)
    if planner is None:
        raise RuntimeError("path_planner 未装配（应用工厂须注入 app.state.path_planner）")
    return planner


# --------------------------------------------------------------------------- 路由定义


path_api = APIRouter(prefix=PATH_PREFIX, tags=["training-path"])


@path_api.get("/{trainee_id}")
def get_current_path(trainee_id: int, request: Request) -> dict[str, Any]:
    """当前路径：分段视图 + 实时进度合并 + 完成度统计（SR §5 current 形态）。"""
    planner = get_path_planner(request)
    view = planner.current_view(trainee_id)
    if view is None:
        raise _PathApiError(
            CODE_NOT_FOUND,
            404,
            f"学员 {trainee_id} 无进行中路径：尚未生成学习路径（TR-3101）",
            {"biz_code": "TR-3101"},
        )
    return _envelope(_request_trace_id(request), CODE_OK, "ok", view)


@path_api.post("/{trainee_id}")
def post_replan_path(trainee_id: int, request: Request) -> dict[str, Any]:
    """生成/重算路径：取雷达最新快照重新编排（version+1、已完成保留，SR §6.3）。"""
    planner = get_path_planner(request)
    try:
        planner.replan(trainee_id)
    except PathError as exc:
        raise _from_path_error(exc) from exc
    view = planner.current_view(trainee_id)
    assert view is not None  # replan 刚落库，active 必存在；断言仅为类型收窄
    return _envelope(_request_trace_id(request), CODE_OK, "ok", view)


@path_api.post("/{trainee_id}/progress")
def post_progress(trainee_id: int, payload: ProgressRequest, request: Request) -> dict[str, Any]:
    """进度上报：状态机结算（幂等/锁定/重修超限干预标记），返回状态与完成度。"""
    planner = get_path_planner(request)
    try:
        result = planner.apply_progress_event(
            trainee_id,
            event_uid=payload.event_uid,
            kind=payload.kind,
            lesson_uid=payload.lesson_uid,
            score=payload.score,
        )
    except PathError as exc:
        raise _from_path_error(exc) from exc
    except ValueError as exc:
        raise _bad_request(f"进度事件非法：{exc}") from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", result.to_dict())


# --------------------------------------------------------------------------- 错误信封与装配


class _PathApiError(Exception):
    """路由层业务异常（内部）：数值码 + HTTP 状态 + 文案 + data（biz_code）。"""

    def __init__(self, code: int, http_status: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.data = data


def _bad_request(message: str) -> _PathApiError:
    """参数非法 → 40001（HTTP 400）。"""
    return _PathApiError(CODE_BAD_REQUEST, 400, message, None)


def _from_path_error(exc: PathError) -> _PathApiError:
    """PathError（TR-31xx + http_status）→ 信封错误（数值码按 HTTP 状态族映射）。"""
    code_by_status = {400: CODE_BAD_REQUEST, 404: CODE_NOT_FOUND, 409: CODE_CONFLICT}
    return _PathApiError(
        code_by_status.get(exc.http_status, CODE_BAD_REQUEST),
        exc.http_status,
        str(exc),
        {"biz_code": exc.biz_code},
    )


async def _on_path_api_error(request: Request, exc: _PathApiError) -> JSONResponse:
    """_PathApiError → 统一信封（HTTP 状态 + 数值码 + data.biz_code）。"""
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


def install_path_error_handlers(app: FastAPI) -> FastAPI:
    """注册 path 路由的错误信封处理器（信封格式全路由一致）。"""
    app.add_exception_handler(_PathApiError, _on_path_api_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    return app


def create_path_app(planner: PathPlanner) -> FastAPI:
    """测试/本地一体化装配：FastAPI 应用 + path 路由 + 错误信封处理器。

    规划器必须显式注入（课程映射表与三存储由装配方决定，无合理全局缺省）；
    生产装配走 training-service 宿主 include_router + app.state 注入。
    """
    app = FastAPI(title="training-path", version="0.1.0")
    app.state.path_planner = planner
    app.include_router(path_api)
    install_path_error_handlers(app)
    return app


__all__ = [
    "PATH_PREFIX",
    "PATH_VIEW_PATH",
    "PATH_REPLAN_PATH",
    "PATH_PROGRESS_PATH",
    "CODE_OK",
    "CODE_BAD_REQUEST",
    "CODE_NOT_FOUND",
    "CODE_CONFLICT",
    "path_api",
    "get_path_planner",
    "install_path_error_handlers",
    "create_path_app",
]
