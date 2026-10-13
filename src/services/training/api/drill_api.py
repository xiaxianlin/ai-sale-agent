# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""实战演练 API 路由（FR-TR-003，迭代 1.2；SR-TR-003 §5 接口增量，drill_api 独占文件）。

端点（统一信封 code/message/data/trace_id，04-接口设计 §1.3 口径）：

| 端点 | 方法与路径 | 说明 |
|---|---|---|
| 案例批量导入 | POST /api/v1/training/drill/cases/import | 逐条过脱敏闸门：干净入库、
|   |   | PII 命中拒绝并随报告返回（不整批失败）；结构非法 → 40001 |
| 分页抽题 | GET  /api/v1/training/drill/cases | 分页 + weak_dim 薄弱维参数预留（FR-TR-005
|   |   | 抽题结果反哺学习路径的接缝）；受启用闸门 TR-1003 与校准冻结 TR-2002 约束 |
| 提交作答 | POST /api/v1/training/drill/submit | {case_id, answer, trainee_id, request_id}
|   |   | → 评分结果；LLM 失败降级 status=pending_manual（TR-2001 语义，200 非报错） |
| 主管校准 | POST /api/v1/training/drill/results/{result_id}/supervisor-score
|   |   | {supervisor, scores} → 一致判定 + 偏差明细（§16.2 KPI 采样回路） |
| 校准报表 | GET  /api/v1/training/drill/calibration/report | 滚动一致率 + 冻结标志 |

错误码映射：数值码沿用仓库口径（40001 入参 / 40404 不存在 / 40902 状态冲突），
drill 业务码以 ``data.biz_code`` 承载 SR-TR-003 §5 的 TR-xxxx：
TR-1001 案例不可投（404）、TR-1002 作答空/超长（400）、TR-1003 案例库未达启用闸门（409）、
TR-2001 评分降级待人工（非错误：submit 200 + status=pending_manual）、
TR-2002 一致率 <85% 冻结投题（409）、TR-2003 结果不存在（404）、TR-2004 结果状态不允许（409）。

装配方式（与 guidance_api 同模式）：生产宿主经 ``app.state.drill_service`` 注入
DrillService 后 ``include_router(drill_api)``；缺省懒加载默认实例（100 条 demo
案例 + 内存存储 + FakeDrillScorer + kb MaskingEngine，TestClient/本地零配置）。
鉴权依赖（JWT → trainee_id）随 training-service 装配任务接入，本迭代以显式
trainee_id 入参承接（NFR-SEC-02 登记项，与 guidance_api 同口径）。

依据文档：docs/07-实现方案/TR/SR-TR-003-实战演练系统.md §5、§6.3、§7；
         docs/04-接口设计 §1（通用规范）。
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from services.training.core.drill.casebank import (
    DIM_PRODUCT,
    CaseCapacityError,
    DrillCase,
    DrillCaseBank,
    RubricPoint,
    build_demo_cases,
)
from services.training.core.drill.scoring import FakeDrillScorer
from services.training.core.drill.service import (
    DrillError,
    DrillService,
)
from services.training.core.drill.store import InMemoryDrillStore

# --------------------------------------------------------------------------- 路径与错误码常量

#: 路由前缀与端点常量（供测试与前端 API 客户端共用同一口径）
DRILL_PREFIX: str = "/api/v1/training/drill"
CASES_IMPORT_PATH: str = f"{DRILL_PREFIX}/cases/import"
CASES_PATH: str = f"{DRILL_PREFIX}/cases"
SUBMIT_PATH: str = f"{DRILL_PREFIX}/submit"
RESULT_SUPERVISOR_PATH: str = f"{DRILL_PREFIX}/results/{{result_id}}/supervisor-score"
CALIBRATION_REPORT_PATH: str = f"{DRILL_PREFIX}/calibration/report"

#: 成功业务码 / 入参校验失败 / 资源不存在 / 状态冲突（04-接口设计 §1.3）
CODE_OK: int = 0
CODE_BAD_REQUEST: int = 40001
CODE_NOT_FOUND: int = 40404
CODE_CONFLICT: int = 40902

#: drill 维度字面量（pydantic 校验口径与核心 DRILL_DIMS 同源）
DrillDim = Literal["product_accuracy", "script", "strategy"]


# --------------------------------------------------------------------------- 入参模型


class RubricPointModel(BaseModel):
    """评分点入参：维度必须落在三维口径内。"""

    model_config = ConfigDict(frozen=True)

    point_id: str = Field(min_length=1, description="评分点编号，如 P1")
    dim: DrillDim = Field(description="所属维度：product_accuracy/script/strategy")
    desc: str = Field(min_length=1, description="评分点描述")
    keywords: list[str] = Field(min_length=1, description="命中关键词（Fake 评分器口径）")


class DrillCaseImportItem(BaseModel):
    """单条案例导入入参（SR §4 drill_case 应用层形态；状态由闸门决定）。"""

    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1, description="案例编号，如 DR-2026-0001")
    inquiry_text: str = Field(min_length=1, description="询盘文本（须已脱敏）")
    context: str = Field(default="", description="询盘上下文/客户画像摘要（须已脱敏）")
    excellent_reply: str = Field(min_length=1, description="优秀回复（业务专家标注）")
    rubric: list[RubricPointModel] = Field(min_length=1, description="评分要点（三维九点口径）")
    kb_type: str = Field(default="training", description="知识库类型（演练库族）")
    difficulty: int = Field(default=2, ge=1, le=3, description="难度 1-3")
    primary_dim: DrillDim = Field(default=DIM_PRODUCT, description="主考核维度（薄弱维定向抽题）")


class CaseImportRequest(BaseModel):
    """POST /cases/import 入参：批量案例清单（单条被闸门拒绝不整批失败）。"""

    model_config = ConfigDict(frozen=True)

    cases: list[DrillCaseImportItem] = Field(min_length=1, description="案例清单")


class SubmitAnswerRequest(BaseModel):
    """POST /submit 入参：作答提交（幂等键 request_id 作用域为学员本人）。"""

    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1, description="案例编号")
    answer: str = Field(description="新人作答（空/超长由核心校验 → TR-1002）")
    trainee_id: int = Field(ge=1, description="学员 ID（生产由鉴权态取本人）")
    request_id: str = Field(min_length=1, description="幂等键（同键重放返回首次结果）")


class SupervisorScoreRequest(BaseModel):
    """POST /results/{id}/supervisor-score 入参：主管逐维打分（三维齐备，0-100）。"""

    model_config = ConfigDict(frozen=True)

    supervisor: str = Field(min_length=1, description="评分手工号（销售主管）")
    scores: dict[DrillDim, int] = Field(description="逐维评分 {product_accuracy, script, strategy}")


# --------------------------------------------------------------------------- 信封与依赖注入


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（04-接口设计 §1.3）：四键齐全，data 允许 None。"""
    return {"code": code, "message": message, "data": data, "trace_id": trace_id}


def _request_trace_id(request: Request) -> str:
    """取本请求 trace_id：优先透传 X-Trace-Id 头，缺失本地生成 16 位 hex（04 §1.4）。"""
    forwarded = request.headers.get("X-Trace-Id", "").strip()
    return forwarded or uuid.uuid4().hex[:16]


_default_service_instance: DrillService | None = None


def _default_service() -> DrillService:
    """进程级默认 DrillService（懒加载）：100 条 demo 案例 + 内存存储 + Fake 评分器。

    脱敏引擎在装配层懒加载 kb MaskingEngine（核心仅依赖 MaskingGate 协议，
    注入边界在本文件——依赖方向：api → core → 协议，core 不反向耦合 kb）。
    """
    global _default_service_instance
    if _default_service_instance is None:
        # CR A1 登记：装配层懒加载（monolith 同进程形态合法）；拆分部署时改为
        # 构造注入 MaskingGate 协议（core 层已按协议解耦，见 drill/casebank.py）
        from services.kb.core.pipeline.cleaning.masking import MaskingEngine, default_ruleset

        bank: DrillCaseBank = DrillCaseBank(MaskingEngine(default_ruleset()))
        bank.import_cases(build_demo_cases(100))
        _default_service_instance = DrillService(bank, InMemoryDrillStore(), FakeDrillScorer())
    return _default_service_instance


def get_drill_service(request: Request) -> DrillService:
    """依赖注入：优先取宿主 app.state.drill_service，缺省懒加载默认实例。"""
    service = getattr(request.app.state, "drill_service", None)
    return service if service is not None else _default_service()


# --------------------------------------------------------------------------- 路由定义


drill_api = APIRouter(prefix=DRILL_PREFIX, tags=["training-drill"])


@drill_api.post("/cases/import")
def post_cases_import(payload: CaseImportRequest, request: Request) -> dict[str, Any]:
    """批量导入案例：逐条过脱敏闸门（PII 命中拒绝并随报告返回，不整批失败）。"""
    service = get_drill_service(request)
    cases = [
        DrillCase(
            case_id=item.case_id,
            inquiry_text=item.inquiry_text,
            context=item.context,
            excellent_reply=item.excellent_reply,
            rubric=tuple(
                RubricPoint(
                    point_id=point.point_id,
                    dim=point.dim,
                    desc=point.desc,
                    keywords=tuple(point.keywords),
                )
                for point in item.rubric
            ),
            kb_type=item.kb_type,
            difficulty=item.difficulty,
            primary_dim=item.primary_dim,
        )
        for item in payload.cases
    ]
    try:
        report = service.import_cases(cases)
    except ValueError as exc:
        raise _bad_request(f"案例导入参数非法：{exc}") from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", report.to_dict())


@drill_api.get("/cases")
def get_cases(
    request: Request,
    page: int = Query(default=1, ge=1, description="页码（1 起）"),
    page_size: int = Query(default=10, ge=1, le=100, description="页大小（1-100）"),
    weak_dim: str | None = Query(
        default=None, description="薄弱维过滤（预留：product_accuracy/script/strategy）"
    ),
) -> dict[str, Any]:
    """分页抽题：受启用闸门（TR-1003）与校准冻结（TR-2002）约束。"""
    service = get_drill_service(request)
    try:
        data = service.assign(page=page, page_size=page_size, weak_dim=weak_dim)
    except CaseCapacityError as exc:
        raise _drill_error("TR-1003", CODE_CONFLICT, 409, str(exc)) from exc
    except DrillError as exc:
        raise _from_drill_error(exc) from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", data)


@drill_api.post("/submit")
def post_submit(payload: SubmitAnswerRequest, request: Request) -> dict[str, Any]:
    """提交作答并评分：幂等重放返回首次结果；LLM 失败降级 pending_manual（200）。"""
    service = get_drill_service(request)
    try:
        result = service.submit(
            case_id=payload.case_id,
            answer=payload.answer,
            trainee_id=payload.trainee_id,
            request_id=payload.request_id,
        )
    except DrillError as exc:
        raise _from_drill_error(exc) from exc
    except ValueError as exc:
        raise _bad_request(f"作答提交参数非法：{exc}") from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", result.to_dict())


@drill_api.post("/results/{result_id}/supervisor-score")
def post_supervisor_score(
    result_id: str, payload: SupervisorScoreRequest, request: Request
) -> dict[str, Any]:
    """主管校准评分：与 AI 评分逐维对比 → 一致判定 + 偏差明细 + 滚动一致率。"""
    service = get_drill_service(request)
    try:
        sample = service.record_supervisor_score(
            result_id=result_id, supervisor=payload.supervisor, scores=dict(payload.scores)
        )
    except DrillError as exc:
        raise _from_drill_error(exc) from exc
    except ValueError as exc:
        raise _bad_request(f"主管评分非法：{exc}") from exc
    report = service.calibration_report()
    return _envelope(
        _request_trace_id(request),
        CODE_OK,
        "ok",
        {
            "calibration": sample.to_dict(),
            "consistency_rate": report.consistency_rate,
            "frozen": report.frozen,
        },
    )


@drill_api.get("/calibration/report")
def get_calibration_report(request: Request) -> dict[str, Any]:
    """校准报表：滚动一致率（§16.2 KPI ≥85%）+ 冻结标志。"""
    service = get_drill_service(request)
    return _envelope(_request_trace_id(request), CODE_OK, "ok", service.calibration_report().to_dict())


# --------------------------------------------------------------------------- 错误信封与装配


class _DrillApiError(Exception):
    """路由层业务异常（内部）：数值码 + HTTP 状态 + 文案 + data（biz_code）。"""

    def __init__(self, code: int, http_status: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.data = data


def _drill_error(biz_code: str, code: int, http_status: int, message: str) -> _DrillApiError:
    """构造带 biz_code 的业务错误（TR-xxxx 码随 data 下发，机器可读）。"""
    return _DrillApiError(code, http_status, message, {"biz_code": biz_code})


def _from_drill_error(exc: DrillError) -> _DrillApiError:
    """DrillError（TR-xxxx + http_status）→ 信封错误（数值码按 HTTP 状态族映射）。"""
    code_by_status = {400: CODE_BAD_REQUEST, 404: CODE_NOT_FOUND, 409: CODE_CONFLICT}
    return _drill_error(
        exc.biz_code, code_by_status.get(exc.http_status, CODE_BAD_REQUEST), exc.http_status, str(exc)
    )


def _bad_request(message: str) -> _DrillApiError:
    """参数非法 → 40001（HTTP 400）。"""
    return _DrillApiError(CODE_BAD_REQUEST, 400, message, None)


async def _on_drill_api_error(request: Request, exc: _DrillApiError) -> JSONResponse:
    """_DrillApiError → 统一信封（HTTP 状态 + 数值码 + data.biz_code）。"""
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


def install_drill_error_handlers(app: FastAPI) -> FastAPI:
    """注册 drill 路由的错误信封处理器（信封格式全路由一致）。"""
    app.add_exception_handler(_DrillApiError, _on_drill_api_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    return app


def create_drill_app(service: DrillService | None = None) -> FastAPI:
    """测试/本地一体化装配：FastAPI 应用 + drill 路由 + 错误信封处理器。

    生产装配不走本函数（training-service 宿主 include_router + app.state 注入）；
    传入 ``service`` 时挂到 app.state（依赖注入优先级高于默认懒加载实例）。
    """
    app = FastAPI(title="training-drill", version="0.1.0")
    if service is not None:
        app.state.drill_service = service
    app.include_router(drill_api)
    install_drill_error_handlers(app)
    return app


__all__ = [
    "DRILL_PREFIX",
    "CASES_IMPORT_PATH",
    "CASES_PATH",
    "SUBMIT_PATH",
    "RESULT_SUPERVISOR_PATH",
    "CALIBRATION_REPORT_PATH",
    "CODE_OK",
    "CODE_BAD_REQUEST",
    "CODE_NOT_FOUND",
    "CODE_CONFLICT",
    "drill_api",
    "install_drill_error_handlers",
    "create_drill_app",
]
