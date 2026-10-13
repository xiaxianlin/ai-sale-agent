# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""agent-runner 对内 API（FR-AG-008 接口面，SR-AG-008 §5；迭代 2.1 demo 口径）。

端点（管理后台人工介入通道，生产经管理后台 JWT+RBAC 鉴权，actor 从会话身份解析）：
- POST /api/v1/agent/sessions/{uid}/takeover  人工接管 → human_managed
- POST /api/v1/agent/sessions/{uid}/release   人工放回 → ai_managed
- GET  /api/v1/agent/sessions                 会话列表（状态/版本/留痕数）
- GET  /api/v1/agent/sessions/{uid}           会话详情（state_hist 全量可查）
- GET  /healthz                               存活探针（compose healthcheck 消费）

错误契约（SR-AG-008 §5 错误码 + 中文消息）：
  404/40401 会话不存在；422/42201 非法迁移；409/40901 乐观并发版本冲突。

响应面结构性防回显：出参仅状态/版本/留痕字段，无任何凭证/明文类字段
（「后台明文不可见」为模型层保证而非约定，NFR-SEC-04 掩码口径）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from services.agent.core.state import (
    AISendGate,
    ConversationNotFoundError,
    ConversationStateMachine,
    ConvState,
    IllegalTransitionError,
    SessionStoreProtocol,
    VersionConflictError,
)

#: 错误码（SR-AG-008 §5）
ERR_CONVERSATION_NOT_FOUND = 40401
ERR_ILLEGAL_TRANSITION = 42201
ERR_VERSION_CONFLICT = 40901

#: 默认操作者（生产 = JWT 身份解析，不接受前端直传）
DEFAULT_OPERATOR = "user:operator"


class TransitionBody(BaseModel):
    """迁移请求体：reason 必填（审计口径），expect_version 选填（乐观并发）。"""

    reason: str = Field(min_length=1, description="迁移原因（留痕/审计必填）")
    expect_version: int | None = Field(default=None, description="期望版本号（乐观并发 CAS）")


def _to_http(exc: Exception) -> HTTPException:
    """状态机异常 → HTTPException（错误码 + 中文消息，供网关透传）。"""
    if isinstance(exc, ConversationNotFoundError):
        return HTTPException(status_code=404, detail={
            "code": ERR_CONVERSATION_NOT_FOUND, "message": str(exc),
        })
    if isinstance(exc, IllegalTransitionError):
        return HTTPException(status_code=422, detail={
            "code": ERR_ILLEGAL_TRANSITION, "message": str(exc),
        })
    if isinstance(exc, VersionConflictError):
        return HTTPException(status_code=409, detail={
            "code": ERR_VERSION_CONFLICT, "message": str(exc),
        })
    return HTTPException(status_code=500, detail={"code": 50000, "message": str(exc)})


def create_runner_app(
    *,
    store: SessionStoreProtocol,
    state_machine: ConversationStateMachine,
    gate: AISendGate,
    audit: Any | None = None,
) -> FastAPI:
    """装配 agent-runner FastAPI 应用（依赖注入：存储/状态机/闸门/审计）。"""
    app = FastAPI(
        title="agent-runner 执行器 API（demo）",
        description="会话状态机人工介入通道 + 存活探针（FR-AG-008/FR-AG-001）",
        version="0.1.0",
    )
    app.state.state_machine = state_machine
    app.state.store = store
    app.state.gate = gate          # 发送闸门装配位（demo 无出站端点，P2 随 gated_sender 启用）
    app.state.audit = audit

    def _do_transition(
        uid: str, to_state: ConvState, operator: str, body: TransitionBody
    ) -> dict[str, object]:
        """takeover/release 共用迁移路径：走状态机（留痕+审计）→ 迁移结果。"""
        try:
            result = state_machine.transition(
                uid, to_state, actor=operator, reason=body.reason,
                expect_version=body.expect_version,
            )
        except (ConversationNotFoundError, IllegalTransitionError, VersionConflictError) as exc:
            raise _to_http(exc) from exc
        return {"uid": uid, "state": result.to_state.value, "version": result.version}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """存活探针（compose healthcheck / 业务探针基础位）。"""
        return {"status": "ok", "service": "agent-runner"}

    @app.get("/api/v1/agent/sessions")
    def list_sessions() -> dict[str, list[dict[str, object]]]:
        """会话列表：状态/版本/留痕数/挂起时刻（无任何内容明文）。"""
        rows = []
        for record in store.list_all():
            rows.append({
                "uid": record.uid,
                "state": record.state.value,
                "state_version": record.state_version,
                "state_hist_len": len(record.state_hist),
                "pending_since": record.pending_since,
            })
        return {"sessions": rows}

    @app.get("/api/v1/agent/sessions/{uid}")
    def session_detail(uid: str) -> dict[str, object]:
        """会话详情：state_hist 全量留痕（from/to/at/by/reason 逐条可查）。"""
        record = store.get(uid)
        if record is None:
            raise _to_http(ConversationNotFoundError(f"会话 {uid} 不存在"))
        return {
            "uid": record.uid,
            "state": record.state.value,
            "state_version": record.state_version,
            "state_hist": [dict(entry) for entry in record.state_hist],
            "pending_since": record.pending_since,
        }

    @app.post("/api/v1/agent/sessions/{uid}/takeover")
    def takeover(
        uid: str,
        body: TransitionBody,
        x_operator: Annotated[str, Header()] = DEFAULT_OPERATOR,
    ) -> dict[str, object]:
        """人工接管：ai_managed（或 closed 重开）→ human_managed。"""
        return _do_transition(uid, ConvState.HUMAN_MANAGED, x_operator, body)

    @app.post("/api/v1/agent/sessions/{uid}/release")
    def release(
        uid: str,
        body: TransitionBody,
        x_operator: Annotated[str, Header()] = DEFAULT_OPERATOR,
    ) -> dict[str, object]:
        """人工放回：human_managed / pending_confirm → ai_managed。"""
        return _do_transition(uid, ConvState.AI_MANAGED, x_operator, body)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> object:  # noqa: ARG001
        """HTTPException 直通（detail 已为 {code, message} 结构）。"""
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    install_agent_error_handlers(app)  # CR A2：统一信封
    return app


def install_agent_error_handlers(app) -> None:
    """统一信封错误处理（CR A2 补齐：agent 域 HTTPException → 四键信封同构）。

    此前本域错误经 FastAPI 默认包裹为 {"detail": {...}}，与 04-接口设计 §1.3
    四键信封不同构；各 app 工厂尾部调用本函数即可对齐。
    """
    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse

    from foundation.core.envelope import resolve_trace_id

    @app.exception_handler(HTTPException)
    async def _agent_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        code = detail.get("code", exc.status_code * 100)  # 原样透传（兼容字符串业务码与 int 信封码）
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": code,
                "message": str(detail.get("message", "请求失败")),
                "data": detail.get("data"),
                "trace_id": resolve_trace_id(request),
            },
        )
