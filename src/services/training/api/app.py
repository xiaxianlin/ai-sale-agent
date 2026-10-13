# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""training-service 应用工厂（FR-TR-001 / SR-TR-001 §5 接口的装配层）。

create_training_app 组装 AI 导师全链路，全部组件鸭子类型注入（demo 内存实现
缺省可用；生产替换 PG 存储 + Redis 限流 + DeepSeek 真实客户端，协议不变）：

    search_fn（FR-KB-030 统一检索契约，kb-service 只读依赖）
      + llm（LlmClient 协议：DeepSeek 生产 / FakeTutorLlm demo）
      → TutorPipeline（零编造三道闸编排）
      → 路由层（SSE ask / 会话历史 / 缺口清单）

统一信封与错误码（04-接口设计 §1.3）：40001 入参校验失败（HTTP 400）、
50002 下游依赖故障（HTTP 503）；成功 code=0。
"""
from __future__ import annotations

from foundation.core.envelope import envelope, resolve_trace_id

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from services.training.core.tutor.gap import KnowledgeGapRecorder
from services.training.core.tutor.llm import LlmClient
from services.training.core.tutor.pipeline import (
    TutorBadRequest,
    TutorDownstreamError,
    TutorPipeline,
)

from .routes import CODE_BAD_REQUEST, CODE_DOWNSTREAM, router


def _envelope(trace_id: str, code: int, message: str, data: Any) -> dict[str, Any]:
    """统一响应信封（CR A2：委托 foundation 单点；trace_id 缺省补齐非空）。"""
    return envelope(code, message, data, trace_id)


def install_training_error_handlers(app: FastAPI) -> FastAPI:
    """注册统一信封错误处理器（40001/50002 两族；SSE 流内错误由路由层处理）。"""

    @app.exception_handler(TutorBadRequest)
    async def _on_bad_request(request: Request, exc: TutorBadRequest) -> JSONResponse:
        return JSONResponse(status_code=400, content=_envelope(resolve_trace_id(request), CODE_BAD_REQUEST, str(exc), None))

    @app.exception_handler(TutorDownstreamError)
    async def _on_downstream(request: Request, exc: TutorDownstreamError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content=_envelope(resolve_trace_id(request), CODE_DOWNSTREAM, str(exc) or "知识服务暂不可用，请稍后重试", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content=_envelope(resolve_trace_id(request), CODE_BAD_REQUEST, "参数校验失败", {"errors": errors}),
        )

    return app


def create_training_app(
    search_fn: Callable[..., Any],
    llm: LlmClient,
    *,
    qa_store: Any | None = None,
    gap_recorder: KnowledgeGapRecorder | None = None,
    kb_types: list[str] | None = None,
    top_k: int = 5,
    history_window: int = 6,
    app_version: str = "0.1.0",
) -> FastAPI:
    """组装 AI 导师应用（组件契约见模块 docstring）。

    - search_fn：检索依赖（query/kb_types/filters/top_k → {"results": [...]} 信封）；
    - llm：LLM 实现（demo 传 FakeTutorLlm；生产传 DeepSeekLlmClient）；
    - qa_store/gap_recorder：缺省内存实现（生产注入 PG 形态）；
    - kb_types/top_k/history_window：检索与多轮窗口配置（SR-TR-001 §7 外置项）。
    """
    pipeline = TutorPipeline(
        search_fn=search_fn,
        llm=llm,
        qa_store=qa_store,
        gap_recorder=gap_recorder,
        kb_types=kb_types,
        top_k=top_k,
        history_window=history_window,
    )
    app = FastAPI(
        title="training-service AI 导师 API",
        version=app_version,
        description=(
            "FR-TR-001 AI 导师系统（SR-TR-001）：基于企业知识库的教学化问答，"
            "零编造三道闸（零命中不生成 / 无有效引用不展示 / 落库必带 knowledge_refs）。"
            "SSE 事件：token / gap / done（done 携带 knowledge_refs 溯源凭证）。"
        ),
    )
    install_training_error_handlers(app)
    app.include_router(router)

    @app.get("/healthz", tags=["internal"], include_in_schema=False, summary="健康检查")
    def healthz() -> dict[str, str]:
        """存活探针（部署与运维方案 §4 容器健康检查端点）。"""
        return {"status": "ok"}

    # 依赖注入（routes.py 端点从 app.state 取用）
    app.state.tutor_pipeline = pipeline
    return app


__all__ = ["create_training_app", "install_training_error_handlers"]
