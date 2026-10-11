# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""能力雷达 API 路由（FR-TR-004，迭代 1.3；SR-TR-004 §5 接口增量，radar_api 独占文件）。

端点（统一信封 code/message/data/trace_id，04-接口设计 §1.3 口径）：

| 端点 | 方法与路径 | 说明 |
|---|---|---|
| 雷达查询 | GET  /api/v1/training/radar/{trainee_id} | 当前四维（{score,n}，null=证据不足）
|   |   | + 入职初评基线 baseline + 趋势序列 trend（默认近 90 天窗口） |
| 手动重算 | POST /api/v1/training/radar/{trainee_id}/compute | trigger=manual 全量重算
|   |   | 并落快照（SR §5 recalc 形态；demo 以触发参数承接，幂等键随装配任务接入） |
| 校准报告 | POST /api/v1/training/radar/{trainee_id}/calibrate | 主管盲评对比样本批量提交
|   |   | → 一致率（KPI ≥85%，SRS §16.2）+ 分维下钻 + 偏差调整建议 |

错误码映射：数值码沿用仓库口径（40001 入参 / 40404 不存在），
radar 业务码以 ``data.biz_code`` 承载 SR-TR-004 §5 的语义：
TR-3001 学员无雷达快照（未完成诊断初评，404）。

装配方式（与 drill_api 同模式）：生产宿主经 ``app.state.radar_engine`` 注入
RadarEngine 后 ``include_router(radar_api)``；测试/本地用 :func:`create_radar_app`。
鉴权依赖（JWT → trainee_id）随 training-service 装配任务接入，本迭代以显式
trainee_id 入参承接（NFR-SEC-02 登记项，与 drill_api 同口径）。

依据文档：docs/07-实现方案/TR/SR-TR-004-能力评估系统.md §5、§6.3、§8；
         docs/04-接口设计 §1（通用规范）。
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from services.training.core.radar.engine import (
    DIMENSIONS,
    CalibrationPair,
    FakeEvidenceSource,
    InMemoryRadarSnapshotStore,
    RadarEngine,
)

# --------------------------------------------------------------------------- 路径与错误码常量

#: 路由前缀与端点常量（供测试与前端 API 客户端共用同一口径）
RADAR_PREFIX: str = "/api/v1/training/radar"
RADAR_GET_PATH: str = f"{RADAR_PREFIX}/{{trainee_id}}"
RADAR_COMPUTE_PATH: str = f"{RADAR_PREFIX}/{{trainee_id}}/compute"
RADAR_CALIBRATE_PATH: str = f"{RADAR_PREFIX}/{{trainee_id}}/calibrate"

#: 成功业务码 / 入参校验失败 / 资源不存在 / 状态冲突（04-接口设计 §1.3）
CODE_OK: int = 0
CODE_BAD_REQUEST: int = 40001
CODE_NOT_FOUND: int = 40404
CODE_CONFLICT: int = 40902

#: radar 业务码（SR-TR-004 §5）
BIZ_NO_SNAPSHOT: str = "TR-3001"  # 学员无雷达快照（未完成诊断初评）

#: 重算触发类型（manual 为本端点语义缺省；其余类型由事件链路内部触发）
ComputeTrigger = Literal["initial", "event", "daily", "manual", "stage"]


# --------------------------------------------------------------------------- 入参模型


class ComputeRequest(BaseModel):
    """POST /compute 入参：手动重算触发类型（SR §5 trigger=manual）。"""

    model_config = ConfigDict(frozen=True)

    trigger: ComputeTrigger = Field(default="manual", description="快照触发类型（手动重算缺省 manual）")


class CalibratePairModel(BaseModel):
    """单组盲评对比样本：AI 四维分 vs 主管盲评四维分（同尺同料，维度齐备性由核心校验）。"""

    model_config = ConfigDict(frozen=True)

    pair_id: str = Field(min_length=1, description="样本对编号（如 P1，溯源用）")
    ai_scores: dict[str, float] = Field(description="AI 四维评分 {product, communication, negotiation, platform}")
    supervisor_scores: dict[str, float] = Field(description="主管盲评四维评分（盲评界面不展示 AI 分）")


class CalibrateRequest(BaseModel):
    """POST /calibrate 入参：盲评对比样本批量提交（空清单被 min_length 拦截 → 40001）。"""

    model_config = ConfigDict(frozen=True)

    pairs: list[CalibratePairModel] = Field(min_length=1, description="盲评对比样本清单")


# --------------------------------------------------------------------------- 信封与依赖注入


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（04-接口设计 §1.3）：四键齐全，data 允许 None。"""
    return {"code": code, "message": message, "data": data, "trace_id": trace_id}


def _request_trace_id(request: Request) -> str:
    """取本请求 trace_id：优先透传 X-Trace-Id 头，缺失本地生成 16 位 hex（04 §1.4）。"""
    forwarded = request.headers.get("X-Trace-Id", "").strip()
    return forwarded or uuid.uuid4().hex[:16]


def get_radar_engine(request: Request) -> RadarEngine:
    """依赖注入：取宿主 app.state.radar_engine（生产/测试装配时注入）。"""
    engine: RadarEngine | None = getattr(request.app.state, "radar_engine", None)
    if engine is None:
        raise RuntimeError("radar_engine 未装配（应用工厂须注入 app.state.radar_engine）")
    return engine


# --------------------------------------------------------------------------- 路由定义


radar_api = APIRouter(prefix=RADAR_PREFIX, tags=["training-radar"])


@radar_api.get("/{trainee_id}")
def get_radar(trainee_id: int, request: Request) -> dict[str, Any]:
    """雷达查询：当前四维 + 初评基线 + 趋势序列一次返回（SR §5；score=null=证据不足）。"""
    engine = get_radar_engine(request)
    view = engine.radar_view(trainee_id)
    if view is None:
        raise _RadarApiError(
            CODE_NOT_FOUND,
            404,
            f"学员 {trainee_id} 无雷达快照：未完成诊断初评（{BIZ_NO_SNAPSHOT}）",
            {"biz_code": BIZ_NO_SNAPSHOT},
        )
    return _envelope(_request_trace_id(request), CODE_OK, "ok", view)


@radar_api.post("/{trainee_id}/compute")
def post_compute(trainee_id: int, payload: ComputeRequest, request: Request) -> dict[str, Any]:
    """手动重算：全量聚合三类证据源并落快照（trigger=manual，SR §5 recalc 形态）。"""
    engine = get_radar_engine(request)
    snapshot = engine.compute(trainee_id, trigger=payload.trigger)
    return _envelope(
        _request_trace_id(request),
        CODE_OK,
        "ok",
        {
            "snapshot_id": snapshot.snapshot_id,
            "trainee_id": snapshot.trainee_id,
            "trigger": snapshot.trigger,
            "dims": {dim: dict(snapshot.dims[dim]) for dim in DIMENSIONS},
            "created_at": snapshot.created_at.isoformat(),
        },
    )


@radar_api.post("/{trainee_id}/calibrate")
def post_calibrate(trainee_id: int, payload: CalibrateRequest, request: Request) -> dict[str, Any]:
    """一致性校准：主管盲评对比 → 一致率 + 分维下钻 + 偏差调整建议（SR §6.3）。"""
    engine = get_radar_engine(request)
    pairs = [
        CalibrationPair(
            pair_id=item.pair_id,
            ai_scores=dict(item.ai_scores),
            supervisor_scores=dict(item.supervisor_scores),
        )
        for item in payload.pairs
    ]
    try:
        report = engine.calibrate(pairs)
    except ValueError as exc:
        raise _bad_request(f"校准样本非法：{exc}") from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", report.to_dict())


# --------------------------------------------------------------------------- 错误信封与装配


class _RadarApiError(Exception):
    """路由层业务异常（内部）：数值码 + HTTP 状态 + 文案 + data（biz_code）。"""

    def __init__(self, code: int, http_status: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.data = data


def _bad_request(message: str) -> _RadarApiError:
    """参数非法 → 40001（HTTP 400）。"""
    return _RadarApiError(CODE_BAD_REQUEST, 400, message, None)


async def _on_radar_api_error(request: Request, exc: _RadarApiError) -> JSONResponse:
    """_RadarApiError → 统一信封（HTTP 状态 + 数值码 + data.biz_code）。"""
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


def install_radar_error_handlers(app: FastAPI) -> FastAPI:
    """注册 radar 路由的错误信封处理器（信封格式全路由一致）。"""
    app.add_exception_handler(_RadarApiError, _on_radar_api_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    return app


def create_radar_app(engine: RadarEngine | None = None) -> FastAPI:
    """测试/本地一体化装配：FastAPI 应用 + radar 路由 + 错误信封处理器。

    生产装配不走本函数（training-service 宿主 include_router + app.state 注入）；
    传入 ``engine`` 时挂到 app.state（缺省懒加载最小 demo 实例：三类 Fake 证据源 +
    内存快照存储，TestClient/本地零配置——与 drill_api 同模式）。
    """
    app = FastAPI(title="training-radar", version="0.1.0")
    app.state.radar_engine = engine if engine is not None else _default_engine()
    app.include_router(radar_api)
    install_radar_error_handlers(app)
    return app


def _default_engine() -> RadarEngine:
    """进程级缺省引擎（懒加载）：Fake 证据源 + 内存快照存储（demo/测试零配置）。"""
    return RadarEngine(
        [FakeEvidenceSource("exercise"), FakeEvidenceSource("exam"), FakeEvidenceSource("mentor_qa")],
        InMemoryRadarSnapshotStore(),
    )


__all__ = [
    "RADAR_PREFIX",
    "RADAR_GET_PATH",
    "RADAR_COMPUTE_PATH",
    "RADAR_CALIBRATE_PATH",
    "CODE_OK",
    "CODE_BAD_REQUEST",
    "CODE_NOT_FOUND",
    "CODE_CONFLICT",
    "BIZ_NO_SNAPSHOT",
    "radar_api",
    "get_radar_engine",
    "install_radar_error_handlers",
    "create_radar_app",
]
