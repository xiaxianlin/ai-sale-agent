# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""培训服务 API 路由（FR-TR-001；04-接口设计 §1 通用规范 + §3.6 培训接口）。

端点（SSE 事件序列：命中 token* → done；零命中 gap → done）：
- POST /api/v1/training/tutor/ask           提问（SSE 流式，事件 token/done/gap）
- GET  /api/v1/training/tutor/sessions/{id} 会话历史（含 knowledge_refs，断线回放）
- GET  /api/v1/training/gaps                知识缺口清单（FR-TR-008 回流消费）

统一信封（04 §1.3）：{code, message, data, trace_id}；SSE 端点的业务错误在流内
以 ``event: error`` 下发（信封字段内嵌 data），流建立前的错误直接回 JSON 信封。
错误码：40001 入参校验失败（HTTP 400）；50002 下游依赖故障（HTTP 503）。
trace_id：优先 X-Trace-Id 请求头，缺失新生成（04 §1.4）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from services.training.core.tutor.gap import KnowledgeGapRecorder
from services.training.core.tutor.pipeline import (
    TutorBadRequest,
    TutorDownstreamError,
    TutorPipeline,
)

#: 业务错误码（04-接口设计 §1.3；与 kb-service 路由层同码表）
CODE_OK: int = 0
CODE_BAD_REQUEST: int = 40001
CODE_DOWNSTREAM: int = 50002

#: SSE 终止事件类型（客户端据此关闭流）
SSE_EVENT_TYPES: tuple[str, ...] = ("token", "gap", "done", "error")

router = APIRouter(prefix="/api/v1/training", tags=["training-tutor"])


# --------------------------------------------------------------------------- 请求/响应模型


class AskRequest(BaseModel):
    """提问入参（SSE 流式端点请求体）。"""

    question: str = Field(..., min_length=1, max_length=500, description="学员提问（1~500 字符）")
    session: str = Field("default", min_length=1, max_length=64, description="会话标识")
    idempotency_key: str = Field("", max_length=128, description="幂等键（同会话重复请求不重复落库）")


class AskSessionResponse(BaseModel):
    """会话历史出参 data 段。"""

    session: str
    records: list[dict[str, Any]]
    count: int


class GapsResponse(BaseModel):
    """缺口清单出参 data 段。"""

    gaps: list[dict[str, Any]]
    count: int


# --------------------------------------------------------------------------- 辅助


def _trace_id_of(request: Request, x_trace: str) -> str:
    """解析本请求 trace_id：X-Trace-Id 头优先，缺失新生成（04 §1.4）。"""
    return (x_trace or "").strip() or uuid.uuid4().hex[:16]


def _sse_frame(event: str, data: dict[str, Any]) -> str:
    """SSE 帧：``event: <type>\\ndata: <json>\\n\\n``。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _pipeline_of(request: Request) -> TutorPipeline:
    """从 app.state 取装配好的编排器（app.py 工厂注入）。"""
    pipeline: TutorPipeline | None = getattr(request.app.state, "tutor_pipeline", None)
    if pipeline is None:
        raise TutorDownstreamError("tutor_pipeline 未装配（应用工厂装配缺失）")
    return pipeline


# --------------------------------------------------------------------------- 端点


@router.post(
    "/tutor/ask",
    summary="导师提问（SSE 流式）",
    response_class=StreamingResponse,
)
def tutor_ask(
    payload: AskRequest,
    request: Request,
    x_trace_id: str = Header("", alias="X-Trace-Id"),
    idempotency_key_header: str = Header("", alias="Idempotency-Key"),
) -> StreamingResponse:
    """提问端点：SSE 流式返回 token/done/gap 事件（04-接口设计 §3.6）。

    - 空问题/超长 → 40001 JSON 信封（流建立前失败）；
    - 检索/模型下游故障：流已建立时以 ``event: error``（code 50002）收尾，
      该轮不落库、不记缺口（系统故障≠知识缺口）；
    - Idempotency-Key 头优先于请求体 idempotency_key（网关侧友好）。
    """
    trace_id = _trace_id_of(request, x_trace_id)
    question = payload.question.strip()
    if not question:
        raise TutorBadRequest("问题不能为空")
    idem_key = idempotency_key_header.strip() or payload.idempotency_key.strip()
    pipeline = _pipeline_of(request)

    def stream() -> Any:
        """事件流生成器：pipeline 事件直出 SSE 帧；下游故障转 error 事件。"""
        try:
            for event in pipeline.ask(
                question, payload.session, idempotency_key=idem_key
            ):
                yield _sse_frame(event.type, {"trace_id": trace_id, **event.data})
        except TutorDownstreamError as exc:
            yield _sse_frame("error", {"code": CODE_DOWNSTREAM, "message": str(exc), "trace_id": trace_id})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Nginx 前置时不缓冲，保流式体验
            "X-Trace-Id": trace_id,
        },
    )


@router.get("/tutor/sessions/{session_id}", summary="会话历史（含引用凭证）")
def session_history(
    session_id: str,
    request: Request,
    x_trace_id: str = Header("", alias="X-Trace-Id"),
) -> JSONResponse:
    """取指定会话的全部问答记录（knowledge_refs 供断线回放与抽检核对）。"""
    trace_id = _trace_id_of(request, x_trace_id)
    pipeline = _pipeline_of(request)
    records = [r.to_dict() for r in pipeline.qa_store.list_by_session(session_id)]
    body = AskSessionResponse(session=session_id, records=records, count=len(records))
    return JSONResponse(
        content={"code": CODE_OK, "message": "ok", "trace_id": trace_id, "data": body.model_dump()}
    )


@router.get("/gaps", summary="知识缺口清单（FR-TR-008 回流消费）")
def list_gaps(
    request: Request,
    status: str | None = None,
    x_trace_id: str = Header("", alias="X-Trace-Id"),
) -> JSONResponse:
    """缺口清单：按 hit_count 降序（热度口径）；status 过滤 pending/resolved。"""
    trace_id = _trace_id_of(request, x_trace_id)
    pipeline = _pipeline_of(request)
    recorder: KnowledgeGapRecorder = pipeline.gap_recorder
    gaps = [g.to_dict() for g in recorder.list_gaps(status=status)]
    body = GapsResponse(gaps=gaps, count=len(gaps))
    return JSONResponse(
        content={"code": CODE_OK, "message": "ok", "trace_id": trace_id, "data": body.model_dump()}
    )


__all__ = [
    "CODE_OK",
    "CODE_BAD_REQUEST",
    "CODE_DOWNSTREAM",
    "SSE_EVENT_TYPES",
    "AskRequest",
    "router",
]
