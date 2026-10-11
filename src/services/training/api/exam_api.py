# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""考试认证与培训闭环 API 路由（FR-TR-007/FR-TR-008，迭代 1.3；SR-TR-007 §5 / SR-TR-008 §5）。

端点（统一信封 code/message/data/trace_id，04-接口设计 §1.3 口径）：

| 端点 | 方法与路径 | 说明 |
|---|---|---|
| 开考（组卷+开考） | POST /api/v1/training/exam/start | {trainee_id} → 考生视图（无答案）+ 会话令牌 |
| 交卷 | POST /api/v1/training/exam/submit | {exam_uid, trainee_id, answers, events?} → 评分+认证联动 |
| 监考事件上报 | POST /api/v1/training/exam/{uid}/proctor-events | {events:[…]} → 即时命中规则 |
| 监考复核裁决 | POST /api/v1/training/exam/{uid}/review | {verdict, reviewer, note} → upheld/overturned |
| 认证状态查询 | GET  /api/v1/training/exam/cert/{trainee_id} | cert_status 三态 |
| 上岗拦截演示 | GET  /api/v1/training/exam/protected-demo | 挂 require_certified（★系统级强制演示桩） |
| 缺口事件上报 | POST /api/v1/training/loop/gap-events | 三源缺口统一入口（dedup new/hit） |
| 触发聚合 | POST /api/v1/training/loop/gaps/aggregate | 事件 → 归一化 → 缺口清单 |
| 缺口清单 | GET  /api/v1/training/loop/gaps?status= | 频次/最近/状态 |
| 缺口审核 | POST /api/v1/training/loop/gaps/{id}/approve\\|reject | 批准回流 / 驳回留痕 |
| 闭环指标 | GET  /api/v1/training/loop/metrics | identified/resolved/rehit/closure_rate |

错误码映射：数值码沿用仓库口径（40001 入参 / 40404 不存在 / 40902 状态冲突），
业务码以 ``data.biz_code`` 承载 E_*（SR-TR-007 §5 / SR-TR-008 §5）：
E_CERT_REQUIRED(**40301**/403 上岗拦截，系统级强制)、E_ATTEMPT_COOLDOWN(42901/429)、
E_ATTEMPT_EXCEEDED(40302/403)、E_EXAM_ONGOING / E_EXAM_NOT_ONGOING / E_REVIEW_NOT_PENDING /
E_CERT_TRANSITION / E_LOOP_CONFLICT(40901) / E_BANK_INSUFFICIENT(42201)、
E_LOOP_REFLOW_UNAVAILABLE(50301)。

装配方式（与 drill_api 同模式）：生产宿主经 ``app.state.exam_service`` / ``app.state.loop_service``
注入后 ``include_router(exam_api)``；缺省懒加载默认实例（demo 题库 + 内存存储，零配置）。
鉴权依赖（JWT → trainee_id）随 training-service 装配任务接入，本迭代以显式 trainee_id /
X-Trainee-Id 头承接（NFR-SEC-02 登记项，与 drill_api 同口径）。

依据文档：docs/07-实现方案/TR/SR-TR-007-考试认证系统.md §5、§6.4；
         docs/07-实现方案/TR/SR-TR-008-培训效果闭环.md §5；
         docs/04-接口设计 §1（通用规范）。
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from services.training.core.exam.proctor import ProctorEvent
from services.training.core.exam.service import (
    CERT_CERTIFIED,
    CertRequiredError,
    ExamError,
    ExamService,
    FakeRubricScorer,
    InMemoryExamStore,
)
from services.training.core.exam.paper import InMemoryQuestionBank, build_demo_questions
from services.training.core.loop.closure import LoopError, LoopService

# --------------------------------------------------------------------------- 路径与错误码常量

#: 路由前缀与端点常量（供测试与前端 API 客户端共用同一口径）
EXAM_PREFIX: str = "/api/v1/training"
EXAM_START_PATH: str = f"{EXAM_PREFIX}/exam/start"
EXAM_SUBMIT_PATH: str = f"{EXAM_PREFIX}/exam/submit"
EXAM_PROCTOR_EVENTS_PATH: str = f"{EXAM_PREFIX}/exam/{{exam_uid}}/proctor-events"
EXAM_REVIEW_PATH: str = f"{EXAM_PREFIX}/exam/{{exam_uid}}/review"
EXAM_RESULT_PATH: str = f"{EXAM_PREFIX}/exam/{{exam_uid}}/result"
CERT_PATH: str = f"{EXAM_PREFIX}/exam/cert/{{trainee_id}}"
PROTECTED_DEMO_PATH: str = f"{EXAM_PREFIX}/exam/protected-demo"
LOOP_GAP_EVENTS_PATH: str = f"{EXAM_PREFIX}/loop/gap-events"
LOOP_AGGREGATE_PATH: str = f"{EXAM_PREFIX}/loop/gaps/aggregate"
LOOP_GAPS_PATH: str = f"{EXAM_PREFIX}/loop/gaps"
LOOP_GAP_APPROVE_PATH: str = f"{EXAM_PREFIX}/loop/gaps/{{gap_id}}/approve"
LOOP_GAP_REJECT_PATH: str = f"{EXAM_PREFIX}/loop/gaps/{{gap_id}}/reject"
LOOP_METRICS_PATH: str = f"{EXAM_PREFIX}/loop/metrics"

#: 成功 / 入参校验失败 / 不存在 / 状态冲突（04-接口设计 §1.3 仓库口径）
CODE_OK: int = 0
CODE_BAD_REQUEST: int = 40001
CODE_NOT_FOUND: int = 40404
CODE_CONFLICT: int = 40902


# --------------------------------------------------------------------------- 入参模型


class ExamStartRequest(BaseModel):
    """POST /exam/start 入参：创建并开考（demo 合一端点，生产拆 组卷/开考 两步）。"""

    model_config = ConfigDict(frozen=True)

    trainee_id: int = Field(ge=1, description="学员 ID（生产由鉴权态取本人）")


class ExamSubmitRequest(BaseModel):
    """POST /exam/submit 入参：作答 + 可选监考事件批次（交卷时终判）。"""

    model_config = ConfigDict(frozen=True)

    exam_uid: str = Field(min_length=1, description="考试编号")
    trainee_id: int = Field(ge=1, description="学员 ID（本人校验）")
    answers: dict[str, Any] = Field(default_factory=dict, description="作答 {question_id: 答案}")
    events: list[dict[str, Any]] = Field(default_factory=list, description="监考事件批次")


class ProctorEventsRequest(BaseModel):
    """POST /exam/{uid}/proctor-events 入参：监考事件批量上报。"""

    model_config = ConfigDict(frozen=True)

    events: list[dict[str, Any]] = Field(min_length=1, description="事件 [{t, type, detail}]")


class ReviewRequest(BaseModel):
    """POST /exam/{uid}/review 入参：主管复核裁决。"""

    model_config = ConfigDict(frozen=True)

    verdict: str = Field(pattern="^(upheld|overturned)$", description="裁决：upheld 成立 / overturned 翻案")
    reviewer: str = Field(min_length=1, description="主管工号")
    note: str = Field(default="", description="裁决备注")


class GapEventRequest(BaseModel):
    """POST /loop/gap-events 入参：三源缺口事件上报。"""

    model_config = ConfigDict(frozen=True)

    source: str = Field(min_length=1, description="来源：mentor_zero_hit/drill_low_score/exam_wrong_point")
    ref_type: str = Field(min_length=1, description="来源记录类型（mentor_session/exercise/exam）")
    ref_id: int = Field(ge=1, description="来源记录主键")
    raw_text: str = Field(min_length=1, description="学员原话 / 题干 / 失分问答")
    trainee_id: int | None = Field(default=None, ge=1, description="学员 ID（可空）")
    context: dict[str, Any] = Field(default_factory=dict, description="场景/得分/知识点提示")


class GapApproveRequest(BaseModel):
    """POST /loop/gaps/{id}/approve 入参：批准（可携带修订后草稿）。"""

    model_config = ConfigDict(frozen=True)

    reviewer: str = Field(min_length=1, description="审核人（kb_reviewer/admin）")
    draft: dict[str, Any] | None = Field(default=None, description="条目草稿 {title, content}")
    version: int | None = Field(default=None, ge=1, description="乐观锁版本（并发审核防护）")


class GapRejectRequest(BaseModel):
    """POST /loop/gaps/{id}/reject 入参：驳回（原因必填留痕）。"""

    model_config = ConfigDict(frozen=True)

    reviewer: str = Field(min_length=1, description="审核人")
    reason: str = Field(default="", description="驳回原因（必填，缺省由核心校验拒绝）")


# --------------------------------------------------------------------------- 信封与依赖注入


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（04-接口设计 §1.3）：四键齐全，data 允许 None。"""
    return {"code": code, "message": message, "data": data, "trace_id": trace_id}


def _request_trace_id(request: Request) -> str:
    """取本请求 trace_id：优先透传 X-Trace-Id 头，缺失本地生成 16 位 hex（04 §1.4）。"""
    forwarded = request.headers.get("X-Trace-Id", "").strip()
    return forwarded or uuid.uuid4().hex[:16]


_default_exam_instance: ExamService | None = None
_default_loop_instance: LoopService | None = None


def _default_exam_service() -> ExamService:
    """进程级默认 ExamService（懒加载）：demo 题库 + 内存存储 + Fake rubric 评分器。"""
    global _default_exam_instance
    if _default_exam_instance is None:
        bank = InMemoryQuestionBank(build_demo_questions())
        _default_exam_instance = ExamService(bank, InMemoryExamStore(), FakeRubricScorer())
    return _default_exam_instance


def _default_loop_service() -> LoopService:
    """进程级默认 LoopService（懒加载）：内存 store + 确定性回流钩子。"""
    global _default_loop_instance
    if _default_loop_instance is None:
        _default_loop_instance = LoopService()
    return _default_loop_instance


def get_exam_service(request: Request) -> ExamService:
    """依赖注入：优先取宿主 app.state.exam_service，缺省懒加载默认实例。"""
    service = getattr(request.app.state, "exam_service", None)
    return service if service is not None else _default_exam_service()


def get_loop_service(request: Request) -> LoopService:
    """依赖注入：优先取宿主 app.state.loop_service，缺省懒加载默认实例。"""
    service = getattr(request.app.state, "loop_service", None)
    return service if service is not None else _default_loop_service()


def require_certified(
    request: Request, x_trainee_id: str = Header("", alias="X-Trainee-Id")
) -> None:
    """★上岗拦截依赖（SR-TR-007 §6.4 cert_gate 的共享依赖形态，业务域 import 即用）。

    cert_status != certified（含 uncertified/expired）一律 403 E_CERT_REQUIRED，
    拦截必写 CERT_BLOCKED 审计（系统级强制的验收证据，NFR-SEC-03）；
    业务域关键写操作（P2 agent / P3 site）挂载同一依赖即继承强制校验。
    demo 鉴权以 X-Trainee-Id 头承接；未携带视同未认证（不做静默放行）。
    """
    service = get_exam_service(request)
    raw = (x_trainee_id or "").strip()
    trainee_id = int(raw) if raw.isdigit() else None
    status = service.cert_status_of(trainee_id) if trainee_id is not None else "uncertified"
    if status != CERT_CERTIFIED:
        service.audit("CERT_BLOCKED", trainee_id=trainee_id, path=PROTECTED_DEMO_PATH)
        raise CertRequiredError(cert_status=status, path=PROTECTED_DEMO_PATH)


# --------------------------------------------------------------------------- 事件解析


def _parse_events(raw_events: list[dict[str, Any]]) -> list[ProctorEvent]:
    """监考事件入参 → ProctorEvent（t 必填整数；非法即 ValueError → 40001）。"""
    events: list[ProctorEvent] = []
    for item in raw_events:
        t = item.get("t")
        if not isinstance(t, int) or isinstance(t, bool):
            raise ValueError("监考事件 t 必须为整数毫秒时间戳")
        events.append(ProctorEvent(t=t, type=str(item.get("type", "")), detail=dict(item.get("detail", {}))))
    return events


# --------------------------------------------------------------------------- 考试路由


exam_api = APIRouter(prefix=EXAM_PREFIX, tags=["training-exam"])


@exam_api.post("/exam/start")
def post_exam_start(payload: ExamStartRequest, request: Request) -> dict[str, Any]:
    """开考（组卷+开考）：进行中冲突 409 E_EXAM_ONGOING；冷却/超限 429/403。"""
    service = get_exam_service(request)
    try:
        record = service.create_exam(payload.trainee_id)
        data = service.start(record.exam_uid)
    except ExamError as exc:
        raise _from_exam_error(exc) from exc
    except ValueError as exc:
        raise _bad_request(f"开考参数非法：{exc}") from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", data)


@exam_api.post("/exam/submit")
def post_exam_submit(payload: ExamSubmitRequest, request: Request) -> dict[str, Any]:
    """交卷评分：幂等重放返回首次结果；嫌疑冻结 review（passed=null）；通过即授予认证。"""
    service = get_exam_service(request)
    try:
        events = _parse_events(payload.events)
        outcome = service.submit(payload.exam_uid, dict(payload.answers), events=events)
    except ExamError as exc:
        raise _from_exam_error(exc) from exc
    except ValueError as exc:
        raise _bad_request(f"交卷参数非法：{exc}") from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", outcome.to_dict())


@exam_api.post("/exam/{exam_uid}/proctor-events")
def post_proctor_events(exam_uid: str, payload: ProctorEventsRequest, request: Request) -> dict[str, Any]:
    """监考事件上报：服务端即时返回命中规则与当前加权分（证据链累计）。"""
    service = get_exam_service(request)
    try:
        events = _parse_events(payload.events)
        flags = service.submit_proctor_events(exam_uid, events)
    except ExamError as exc:
        raise _from_exam_error(exc) from exc
    except ValueError as exc:
        raise _bad_request(f"监考事件非法：{exc}") from exc
    data = {
        "flags": [f.to_dict() for f in flags],
        "cheat_score": float(sum(f.weight for f in flags)),  # 当前加权分（未达阈值仅留痕）
    }
    return _envelope(_request_trace_id(request), CODE_OK, "ok", data)


@exam_api.post("/exam/{exam_uid}/review")
def post_exam_review(exam_uid: str, payload: ReviewRequest, request: Request) -> dict[str, Any]:
    """监考复核裁决：upheld 作废+冷却翻倍 / overturned 撤销嫌疑并补授予。"""
    service = get_exam_service(request)
    try:
        outcome = service.apply_review(
            exam_uid, payload.verdict, reviewer=payload.reviewer, note=payload.note
        )
    except ExamError as exc:
        raise _from_exam_error(exc) from exc
    except ValueError as exc:
        raise _bad_request(f"复核参数非法：{exc}") from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", outcome.to_dict())


@exam_api.get("/exam/{exam_uid}/result")
def get_exam_result(exam_uid: str, request: Request) -> dict[str, Any]:
    """成绩与认证结果：review 未决时 passed=null（SR §5 出参口径）。"""
    service = get_exam_service(request)
    try:
        outcome = service.get_outcome(exam_uid)
    except ExamError as exc:
        raise _from_exam_error(exc) from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", outcome.to_dict())


@exam_api.get("/exam/cert/{trainee_id}")
def get_cert(trainee_id: int, request: Request) -> dict[str, Any]:
    """认证状态查询：cert_status 三态 + 最近授予时间 + 最近考试。"""
    service = get_exam_service(request)
    trainee = service.get_trainee(trainee_id)
    exams = service.list_outcomes(trainee_id)
    data = {
        "trainee_id": trainee_id,
        "cert_status": trainee.cert_status,
        "onboarded_at": trainee.onboarded_at.isoformat(timespec="seconds") if trainee.onboarded_at else None,
        "last_exam_uid": exams[-1].exam_uid if exams else None,
    }
    return _envelope(_request_trace_id(request), CODE_OK, "ok", data)


@exam_api.get("/exam/protected-demo")
def get_protected_demo(
    request: Request,
    _granted: None = Depends(require_certified),
) -> dict[str, Any]:
    """上岗拦截演示桩（验收核心脚本挂载点）：certified 才放行，否则 403 E_CERT_REQUIRED。

    Phase 2 业务域写操作（回复发送/报价起草/订单确认等）挂同一依赖即继承系统级强制。
    """
    raw = request.headers.get("X-Trainee-Id", "").strip()
    trainee_id = int(raw) if raw.isdigit() else None
    data = {"granted": True, "trainee_id": trainee_id, "op": "demo/business-op"}
    return _envelope(_request_trace_id(request), CODE_OK, "ok", data)


# --------------------------------------------------------------------------- 闭环路由


@exam_api.post("/loop/gap-events")
def post_gap_event(payload: GapEventRequest, request: Request) -> dict[str, Any]:
    """三源缺口事件上报：dedup 幂等（重复返回 hit，不落新行）。"""
    loop = get_loop_service(request)
    try:
        event_id, dedup = loop.emit_gap(
            payload.source, payload.ref_type, payload.ref_id, payload.raw_text,
            trainee_id=payload.trainee_id, context=payload.context,
        )
    except ValueError as exc:
        raise _bad_request(f"缺口事件非法：{exc}") from exc
    return _envelope(_request_trace_id(request), CODE_OK, "ok", {"event_id": event_id, "dedup": dedup})


@exam_api.post("/loop/gaps/aggregate")
def post_gap_aggregate(request: Request) -> dict[str, Any]:
    """触发一轮缺口聚合（事件 → 归一化 → 清单 UPSERT），返回归并事件数。"""
    loop = get_loop_service(request)
    return _envelope(_request_trace_id(request), CODE_OK, "ok", {"merged": loop.aggregate()})


@exam_api.get("/loop/gaps")
def get_loop_gaps(
    request: Request,
    status: str | None = Query(default=None, description="状态过滤 open/drafted/resolved/rejected"),
) -> dict[str, Any]:
    """缺口清单：频次降序（热度口径），状态过滤可选。"""
    loop = get_loop_service(request)
    gaps = [g.to_dict() for g in loop.list_gaps(status=status)]
    return _envelope(_request_trace_id(request), CODE_OK, "ok", {"gaps": gaps, "count": len(gaps)})


@exam_api.post("/loop/gaps/{gap_id}/approve")
def post_gap_approve(gap_id: str, payload: GapApproveRequest, request: Request) -> dict[str, Any]:
    """缺口批准：reflow_hook 注入回流（生产为 kb-service FR-KB-020 创建），条目 uid 回写。"""
    loop = get_loop_service(request)
    try:
        gap = loop.review_gap(
            gap_id, approve=True, reviewer=payload.reviewer,
            draft=payload.draft, version=payload.version,
        )
    except LoopError as exc:
        raise _from_loop_error(exc) from exc
    data = {"status": gap.status, "item_uid": gap.resolved_item_uid, "version": gap.version}
    return _envelope(_request_trace_id(request), CODE_OK, "ok", data)


@exam_api.post("/loop/gaps/{gap_id}/reject")
def post_gap_reject(gap_id: str, payload: GapRejectRequest, request: Request) -> dict[str, Any]:
    """缺口驳回：原因必填（缺省 40902），留痕 reviewer/reject_reason。"""
    loop = get_loop_service(request)
    try:
        gap = loop.review_gap(gap_id, approve=False, reviewer=payload.reviewer, reason=payload.reason or None)
    except LoopError as exc:
        raise _from_loop_error(exc) from exc
    data = {"status": gap.status, "reject_reason": gap.reject_reason, "version": gap.version}
    return _envelope(_request_trace_id(request), CODE_OK, "ok", data)


@exam_api.get("/loop/metrics")
def get_loop_metrics(
    request: Request,
    window_days: int = Query(default=30, ge=1, le=365, description="统计窗口（天）"),
) -> dict[str, Any]:
    """闭环指标：identified/resolved/rehit/closure_rate（缺口闭环率，工程过程指标）。"""
    loop = get_loop_service(request)
    return _envelope(_request_trace_id(request), CODE_OK, "ok", loop.metrics(window_days))


# --------------------------------------------------------------------------- 错误信封与装配


class _ExamApiError(Exception):
    """路由层业务异常（内部）：数值码 + HTTP 状态 + 文案 + data（biz_code）。"""

    def __init__(self, code: int, http_status: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.data = data


def _from_exam_error(exc: ExamError) -> _ExamApiError:
    """ExamError → 信封错误（data.biz_code 承载 E_*；E_CERT_REQUIRED 附 cert_status）。"""
    data: dict[str, Any] = {"biz_code": exc.biz_code}
    if isinstance(exc, CertRequiredError):
        data["cert_status"] = exc.cert_status
    return _ExamApiError(exc.numeric_code, exc.http_status, str(exc), data)


def _from_loop_error(exc: LoopError) -> _ExamApiError:
    """LoopError → 信封错误（40901/40902/50301，SR-TR-008 §5 错误码）。"""
    return _ExamApiError(exc.numeric_code, exc.http_status, str(exc), {"biz_code": exc.biz_code})


def _bad_request(message: str) -> _ExamApiError:
    """参数非法 → 40001（HTTP 400）。"""
    return _ExamApiError(CODE_BAD_REQUEST, 400, message, None)


async def _on_exam_api_error(request: Request, exc: _ExamApiError) -> JSONResponse:
    """_ExamApiError → 统一信封（HTTP 状态 + 数值码 + data.biz_code）。"""
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


def install_exam_error_handlers(app: FastAPI) -> FastAPI:
    """注册 exam/loop 路由的错误信封处理器（信封格式全路由一致）。

    领域异常（ExamError/LoopError）直接注册处理器：require_certified 依赖在
    路由前置阶段抛 CertRequiredError，也走同一信封（403/40301/E_CERT_REQUIRED）。
    """
    app.add_exception_handler(_ExamApiError, _on_exam_api_error)
    app.add_exception_handler(ExamError, _on_domain_error)
    app.add_exception_handler(LoopError, _on_domain_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    return app


async def _on_domain_error(request: Request, exc: ExamError | LoopError) -> JSONResponse:
    """领域异常 → 信封（依赖/路由内抛出的 ExamError/LoopError 统一映射）。"""
    if isinstance(exc, ExamError):
        mapped = _from_exam_error(exc)
    else:
        mapped = _from_loop_error(exc)
    return await _on_exam_api_error(request, mapped)


def create_exam_app(
    exam_service: ExamService | None = None, loop_service: LoopService | None = None
) -> FastAPI:
    """测试/本地一体化装配：FastAPI 应用 + exam/loop 路由 + 错误信封处理器。

    生产装配不走本函数（training-service 宿主 include_router + app.state 注入）；
    传入 service 时挂到 app.state（依赖注入优先级高于默认懒加载实例）。
    """
    app = FastAPI(title="training-exam", version="0.1.0")
    if exam_service is not None:
        app.state.exam_service = exam_service
    if loop_service is not None:
        app.state.loop_service = loop_service
    app.include_router(exam_api)
    install_exam_error_handlers(app)
    return app


__all__ = [
    "EXAM_PREFIX",
    "EXAM_START_PATH",
    "EXAM_SUBMIT_PATH",
    "EXAM_PROCTOR_EVENTS_PATH",
    "EXAM_REVIEW_PATH",
    "EXAM_RESULT_PATH",
    "CERT_PATH",
    "PROTECTED_DEMO_PATH",
    "LOOP_GAP_EVENTS_PATH",
    "LOOP_AGGREGATE_PATH",
    "LOOP_GAPS_PATH",
    "LOOP_GAP_APPROVE_PATH",
    "LOOP_GAP_REJECT_PATH",
    "LOOP_METRICS_PATH",
    "CODE_OK",
    "CODE_BAD_REQUEST",
    "CODE_NOT_FOUND",
    "CODE_CONFLICT",
    "require_certified",
    "exam_api",
    "install_exam_error_handlers",
    "create_exam_app",
]
