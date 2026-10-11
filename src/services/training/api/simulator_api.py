# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""模拟客户 API 路由（FR-TR-002，SR-TR-002 §5 接口；迭代 1.2，普通 JSON 信封）。

端点（SSE 形态随前端联调迭代接入，本迭代普通 JSON；复盘生成为 POST 触发）：

| 端点 | 方法与路径 | 说明 |
|---|---|---|
| 开场 | POST /api/v1/training/simulator/start | 入参 {persona_id, scenario_id,
|      |                                        | difficulty?, request_id?}——出参
|      |                                        | {session_id, opening, …}，开场
|      |                                        | 情境即首条 customer 消息；
| 对话 | POST /api/v1/training/simulator/reply  | 入参 {session_id, content,
|      |                                        | request_id?}——出参 {customer_reply,
|      |                                        | state, round, ended, ended_reason,
|      |                                        | degraded}；
| 复盘 | POST /api/v1/training/simulator/sessions/{id}/debrief 生成复盘报告 |
|      | GET  同路径                             | 读取已生成报告（未生成 40401）。 |

统一信封（04-接口设计 §1.3）：{code, message, data, trace_id}；错误码
40001 入参校验失败（HTTP 400）/ 40401 资源不存在（HTTP 404）/ 50002
下游依赖故障（HTTP 503）。trace_id：X-Trace-Id 头优先，缺失新生成。

装配方式（不改 training-service app.py，宿主侧两行挂载）：

    from services.training.api.simulator_api import simulator_api, create_simulator_app
    app.include_router(simulator_api)          # 复用宿主已有 app（挂 router）
    # 或独立/测试装配：
    app = create_simulator_app(engine)          # engine 缺省 FakeSimulatorLlm 全内存
    # 依赖注入：宿主经 app.state.simulator_engine = SimulatorEngine(...) 覆盖缺省
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.training.core.simulator.engine import (
    FakeSimulatorLlm,
    SimBadRequest,
    SimNotFoundError,
    SimulatorEngine,
    SimulatorError,
)
from services.training.core.simulator.persona import (
    PersonaNotFoundError,
    PersonaRegistry,
    ScenarioNotFoundError,
)
from services.training.core.simulator.store import InMemorySimSessionStore

# --------------------------------------------------------------------------- 路径与错误码常量

#: 路由前缀与端点常量（供测试与前端 API 客户端共用同一口径）
SIMULATOR_PREFIX: str = "/api/v1/training/simulator"
START_PATH: str = f"{SIMULATOR_PREFIX}/start"
REPLY_PATH: str = f"{SIMULATOR_PREFIX}/reply"
DEBRIEF_PATH_FMT: str = f"{SIMULATOR_PREFIX}/sessions/{{session_id}}/debrief"

#: 业务错误码（04-接口设计 §1.3；本模块取 40001/40401/50002）
CODE_OK: int = 0
CODE_BAD_REQUEST: int = 40001
CODE_NOT_FOUND: int = 40401
CODE_DOWNSTREAM: int = 50002


# --------------------------------------------------------------------------- 入参模型


class StartRequest(BaseModel):
    """POST /start 入参（difficulty 缺省取 persona.difficulty_base）。"""

    persona_id: str = Field(min_length=1, max_length=32, description="客户类型编号（C-NN）")
    scenario_id: str = Field(min_length=1, max_length=64, description="场景标识（sc-xxx）")
    difficulty: int | None = Field(default=None, ge=1, le=5, description="难度 1-5（缺省取类型基线）")
    request_id: str = Field(default="", max_length=128, description="幂等键（开场重放不重复建会话）")


class ReplyRequest(BaseModel):
    """POST /reply 入参。"""

    session_id: str = Field(min_length=1, max_length=64, description="会话标识")
    content: str = Field(min_length=1, max_length=500, description="学员消息（1~500 字符）")
    request_id: str = Field(default="", max_length=128, description="幂等键（同键重放原响应）")


# --------------------------------------------------------------------------- 信封与装配辅助


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（04-接口设计 §1.3）：四键齐全，data 允许 None。"""
    return {"code": code, "message": message, "data": data, "trace_id": trace_id}


def _request_trace_id(request: Request) -> str:
    """取本请求 trace_id：优先透传 X-Trace-Id 头，缺失本地生成 16 位 hex。"""
    forwarded = request.headers.get("X-Trace-Id", "").strip()
    return forwarded or uuid.uuid4().hex[:16]


def _default_engine() -> SimulatorEngine:
    """缺省引擎（demo 全内存）：FakeSimulatorLlm + 内存存储 + 全 10 类型注册表。"""
    return SimulatorEngine(
        llm=FakeSimulatorLlm(),
        store=InMemorySimSessionStore(),
        registry=PersonaRegistry.with_demo_extensions(),
    )


_default_engine_instance: SimulatorEngine | None = None


def _lazy_default_engine() -> SimulatorEngine:
    """进程级缺省引擎（懒加载；测试/宿主注入 app.state 优先）。"""
    global _default_engine_instance
    if _default_engine_instance is None:
        _default_engine_instance = _default_engine()
    return _default_engine_instance


def get_simulator_engine(request: Request) -> SimulatorEngine:
    """依赖注入：优先取宿主 app.state.simulator_engine，缺省懒加载内存实例。"""
    engine = getattr(request.app.state, "simulator_engine", None)
    return engine if engine is not None else _lazy_default_engine()


# --------------------------------------------------------------------------- 路由定义


simulator_api = APIRouter(prefix=SIMULATOR_PREFIX, tags=["training-simulator"])


@simulator_api.post("/start")
def start_session(payload: StartRequest, request: Request) -> dict[str, Any]:
    """开场：开场情境即首条 customer 消息（SR-TR-002 §5）。

    未知类型/场景 → 40401；难度越界 → 40001；同 request_id 重放原开场。
    """
    engine = get_simulator_engine(request)
    try:
        data = engine.start_session(
            payload.persona_id,
            payload.scenario_id,
            payload.difficulty,
            request_id=payload.request_id.strip(),
        )
    except (PersonaNotFoundError, ScenarioNotFoundError) as exc:
        raise _not_found(str(exc)) from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", data)


@simulator_api.post("/reply")
def reply(payload: ReplyRequest, request: Request) -> dict[str, Any]:
    """推进一轮对话（状态机 + LLM 演绎 + 结束判定）。

    未知会话 → 40401；空/超长/已结束 → 40001；LLM 故障引擎内降级（degraded=true，
    200 正常返回，会话不中断）。
    """
    engine = get_simulator_engine(request)
    try:
        data = engine.reply(
            payload.session_id, payload.content, request_id=payload.request_id.strip()
        )
    except SimNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    except SimBadRequest as exc:
        raise _bad_request(str(exc)) from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", data)


@simulator_api.post("/sessions/{session_id}/debrief")
def generate_debrief(session_id: str, request: Request) -> dict[str, Any]:
    """生成复盘报告（SR-TR-002 §6.3；会话未结束 → 40001，未知会话 → 40401）。"""
    engine = get_simulator_engine(request)
    try:
        report = engine.generate_debrief(session_id)
    except SimNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    except SimBadRequest as exc:
        raise _bad_request(str(exc)) from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", report.model_dump(mode="json"))


@simulator_api.get("/sessions/{session_id}/debrief")
def get_debrief(session_id: str, request: Request) -> dict[str, Any]:
    """读取已生成复盘报告；未生成 → 40401（REPORT 未就绪语义，不报错阻塞轮询）。"""
    engine = get_simulator_engine(request)
    try:
        report = engine.get_debrief(session_id)
    except SimNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    if report is None:
        raise _not_found(f"会话 {session_id} 的复盘报告尚未生成")
    return _envelope(_request_trace_id(request), CODE_OK, "ok", report.model_dump(mode="json"))


# --------------------------------------------------------------------------- 错误信封与装配


class _SimulatorApiError(Exception):
    """路由层业务异常（内部）：携带业务码 / HTTP 状态 / 文案，由信封处理器展开。"""

    def __init__(self, code: int, http_status: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.data = data


def _bad_request(message: str) -> _SimulatorApiError:
    """参数非法 → 40001（HTTP 400）。"""
    return _SimulatorApiError(CODE_BAD_REQUEST, 400, message, None)


def _not_found(message: str) -> _SimulatorApiError:
    """资源不存在 → 40401（HTTP 404）。"""
    return _SimulatorApiError(CODE_NOT_FOUND, 404, message, None)


def install_simulator_error_handlers(app: FastAPI) -> FastAPI:
    """注册 simulator 路由的错误信封处理器（与 guidance/kb 路由信封口径一致）。

    Starlette 按 MRO 由特化到基类解析：SimBadRequest→40001、SimNotFoundError 与
    目录未命中→40401、其余 SimulatorError→50002、RequestValidationError→40001。
    """
    app.add_exception_handler(_SimulatorApiError, _on_simulator_api_error)
    app.add_exception_handler(SimBadRequest, _on_bad_request_exc)
    app.add_exception_handler(SimNotFoundError, _on_not_found_exc)
    app.add_exception_handler(PersonaNotFoundError, _on_not_found_exc)
    app.add_exception_handler(ScenarioNotFoundError, _on_not_found_exc)
    app.add_exception_handler(SimulatorError, _on_downstream_exc)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    return app


async def _on_simulator_api_error(request: Request, exc: _SimulatorApiError) -> JSONResponse:
    """_SimulatorApiError → 统一信封（HTTP 状态 + 业务码）。"""
    return JSONResponse(
        status_code=exc.http_status,
        content=_envelope(_request_trace_id(request), exc.code, exc.message, exc.data),
    )


async def _on_bad_request_exc(request: Request, exc: SimBadRequest) -> JSONResponse:
    """SimBadRequest → 40001 信封。"""
    return JSONResponse(
        status_code=400, content=_envelope(_request_trace_id(request), CODE_BAD_REQUEST, str(exc), None)
    )


async def _on_not_found_exc(request: Request, exc: SimNotFoundError | PersonaNotFoundError | ScenarioNotFoundError) -> JSONResponse:
    """未命中族 → 40401 信封。"""
    return JSONResponse(
        status_code=404, content=_envelope(_request_trace_id(request), CODE_NOT_FOUND, str(exc), None)
    )


async def _on_downstream_exc(request: Request, exc: SimulatorError) -> JSONResponse:
    """未特化引擎异常 → 50002 信封（下游依赖故障语义）。"""
    return JSONResponse(
        status_code=503,
        content=_envelope(_request_trace_id(request), CODE_DOWNSTREAM, str(exc) or "模拟客户服务暂不可用", None),
    )


async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """RequestValidationError → 40001 信封（data.errors 定位字段）。"""
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


def create_simulator_app(engine: SimulatorEngine | None = None, *, app_version: str = "0.1.0") -> FastAPI:
    """测试/本地一体化装配：FastAPI 应用 + simulator 路由 + 错误信封处理器。

    生产装配不走本函数（training-service 宿主 include_router + app.state 注入）；
    传入 ``engine`` 时挂到 app.state（依赖注入优先级高于默认懒加载实例）。
    """
    app = FastAPI(
        title="training-service 模拟客户 API",
        version=app_version,
        description=(
            "FR-TR-002 模拟客户系统（SR-TR-002）：PersonaSpec 参数化角色引擎 + 确定性"
            "状态机（议价立场/情绪/耐心）+ 难度动态调节 + 结束三分支判定（成交/谈崩/"
            "轮数耗尽）+ 复盘报告自动生成。demo 口径：LLM 为可插拔协议，缺省确定性 Fake。"
        ),
    )
    if engine is not None:
        app.state.simulator_engine = engine
    app.include_router(simulator_api)
    install_simulator_error_handlers(app)
    return app


__all__ = [
    "SIMULATOR_PREFIX",
    "START_PATH",
    "REPLY_PATH",
    "DEBRIEF_PATH_FMT",
    "CODE_OK",
    "CODE_BAD_REQUEST",
    "CODE_NOT_FOUND",
    "CODE_DOWNSTREAM",
    "StartRequest",
    "ReplyRequest",
    "simulator_api",
    "get_simulator_engine",
    "install_simulator_error_handlers",
    "create_simulator_app",
]
