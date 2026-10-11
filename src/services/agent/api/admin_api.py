# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V1 API（FR-AG-006，SR-AG-006 §5；迭代 2.3 demo 口径）。

端点（生产经管理后台 JWT+RBAC 鉴权，actor 从会话身份解析；demo 以 X-Operator 头承载）：
- GET  /api/v1/agent/admin/sessions                        会话列表（四态过滤/分页）
- GET  /api/v1/agent/admin/sessions/{uid}/messages         消息流（增量游标）
- POST /api/v1/agent/admin/sessions/{uid}/takeover | /release   人工接管/放回
- GET  /api/v1/agent/admin/sensitive-actions               敏感草稿列表
- GET  /api/v1/agent/admin/sensitive-actions/{uid}         对比视图（draft vs 现状）
- POST /api/v1/agent/admin/sensitive-actions/{uid}/confirm | /reject   确认/驳回
- GET  /api/v1/agent/admin/audit-logs                      审计检索（+chain_ok）

错误契约（沿用 §1.3 + SR-AG-006 §5 增量码）：40401 会话不存在 / 40404 草稿不存在 /
42201 非法迁移 / 40901 状态冲突（乐观并发或已决策）。

响应面：出参仅状态/决策留痕字段，无任何凭证/明文类字段（NFR-SEC-04 掩码口径）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from services.agent.core.admin.views import (
    AdminViews,
    SensitiveNotFoundError,
    SensitiveStateError,
)
from services.agent.core.state import (
    ConversationNotFoundError,
    IllegalTransitionError,
    VersionConflictError,
)

#: 错误码（SR-AG-008 §5 + SR-AG-006 §5 增量）
ERR_CONVERSATION_NOT_FOUND = 40401
ERR_SENSITIVE_NOT_FOUND = 40404
ERR_ILLEGAL_TRANSITION = 42201
ERR_STATE_CONFLICT = 40901

#: 默认操作者（生产 = JWT 身份解析，不接受前端直传）
DEFAULT_OPERATOR = "user:operator"


class ReasonBody(BaseModel):
    """迁移/驳回请求体：reason 必填（审计口径）。"""

    reason: str = Field(min_length=1, description="操作原因（写入审计日志）")


class SensitiveDecisionBody(BaseModel):
    """敏感草稿决策请求体：operator/reason 选填（缺省取 X-Operator 头）。"""

    operator: str | None = Field(default=None, description="决策人（双人可见留痕）")
    reason: str | None = Field(default=None, description="驳回理由（双人可见）")


def _to_http(exc: Exception) -> HTTPException:
    """域异常 → HTTPException（错误码 + 中文消息）。"""
    if isinstance(exc, ConversationNotFoundError):
        return HTTPException(status_code=404, detail={
            "code": ERR_CONVERSATION_NOT_FOUND, "message": str(exc)})
    if isinstance(exc, SensitiveNotFoundError):
        return HTTPException(status_code=404, detail={
            "code": ERR_SENSITIVE_NOT_FOUND, "message": str(exc)})
    if isinstance(exc, IllegalTransitionError):
        return HTTPException(status_code=422, detail={
            "code": ERR_ILLEGAL_TRANSITION, "message": str(exc)})
    if isinstance(exc, (VersionConflictError, SensitiveStateError)):
        return HTTPException(status_code=409, detail={
            "code": ERR_STATE_CONFLICT, "message": str(exc)})
    return HTTPException(status_code=500, detail={"code": 50000, "message": str(exc)})


def create_admin_app(*, views: AdminViews) -> FastAPI:
    """装配管理后台 FastAPI 应用（依赖注入：AdminViews 查询/控制面）。"""
    app = FastAPI(
        title="agent 管理后台 API（demo）",
        description="会话中心 / 人工介入 / 敏感确认 / 审计检索（FR-AG-006/FR-AG-005②）",
        version="0.1.0",
    )

    @app.get("/api/v1/agent/admin/sessions")
    def list_sessions(
        state: str | None = None, page: int = 1, page_size: int = 20,
    ) -> dict[str, Any]:
        """会话列表：四态过滤 + 服务端分页。"""
        return views.list_sessions(state=state, page=page, page_size=page_size)

    @app.get("/api/v1/agent/admin/sessions/{uid}/messages")
    def session_messages(
        uid: str, after_id: int = 0, limit: int = 100,
    ) -> dict[str, Any]:
        """消息流：增量游标轮询（AI 消息含置信度与知识来源）。"""
        try:
            return views.session_messages(uid, after_id=after_id, limit=limit)
        except ConversationNotFoundError as exc:
            raise _to_http(exc) from exc

    @app.post("/api/v1/agent/admin/sessions/{uid}/takeover")
    def takeover(
        uid: str, body: ReasonBody,
        x_operator: Annotated[str, Header()] = DEFAULT_OPERATOR,
    ) -> dict[str, Any]:
        """人工接管：迁移留痕 + 审计（接管后 AI 停止该会话自动回复）。"""
        try:
            return views.takeover(uid, operator=x_operator, reason=body.reason)
        except (ConversationNotFoundError, IllegalTransitionError, VersionConflictError) as exc:
            raise _to_http(exc) from exc

    @app.post("/api/v1/agent/admin/sessions/{uid}/release")
    def release(
        uid: str, body: ReasonBody,
        x_operator: Annotated[str, Header()] = DEFAULT_OPERATOR,
    ) -> dict[str, Any]:
        """人工放回：AI 恢复自动回复（仅接管人/主管可操作，生产 RBAC 校验）。"""
        try:
            return views.release(uid, operator=x_operator, reason=body.reason)
        except (ConversationNotFoundError, IllegalTransitionError, VersionConflictError) as exc:
            raise _to_http(exc) from exc

    @app.get("/api/v1/agent/admin/sensitive-actions")
    def list_sensitive_actions(status: str | None = None) -> dict[str, Any]:
        """敏感草稿列表（决策人/时刻/驳回理由列双人可见）。"""
        return {"items": views.list_sensitive(status=status)}

    @app.get("/api/v1/agent/admin/sensitive-actions/{uid}")
    def sensitive_diff(uid: str) -> dict[str, Any]:
        """对比视图：draft_content vs current_snapshot + 差异键（标红数据源）。"""
        try:
            return views.sensitive_diff(uid)
        except SensitiveNotFoundError as exc:
            raise _to_http(exc) from exc

    @app.post("/api/v1/agent/admin/sensitive-actions/{uid}/confirm")
    def confirm_sensitive(
        uid: str, body: SensitiveDecisionBody,
        x_operator: Annotated[str, Header()] = DEFAULT_OPERATOR,
    ) -> dict[str, Any]:
        """确认执行：AI 草稿经风控通道正式执行（确认人/时刻全留痕）。"""
        try:
            return views.confirm_sensitive(uid, operator=body.operator or x_operator)
        except (SensitiveNotFoundError, SensitiveStateError) as exc:
            raise _to_http(exc) from exc

    @app.post("/api/v1/agent/admin/sensitive-actions/{uid}/reject")
    def reject_sensitive(
        uid: str, body: SensitiveDecisionBody,
        x_operator: Annotated[str, Header()] = DEFAULT_OPERATOR,
    ) -> dict[str, Any]:
        """驳回草稿：理由对操作者与主管双人可见。"""
        try:
            return views.reject_sensitive(
                uid, operator=body.operator or x_operator, reason=body.reason or "（未填写理由）",
            )
        except (SensitiveNotFoundError, SensitiveStateError) as exc:
            raise _to_http(exc) from exc

    @app.get("/api/v1/agent/admin/audit-logs")
    def search_audit(
        ts_from: str | None = None, ts_to: str | None = None, actor: str | None = None,
        action_type: str | None = None, source: str | None = None,
        page: int = 1, page_size: int = 20,
    ) -> dict[str, Any]:
        """审计检索：多维筛选 + 哈希链完整性标识（chain_ok）。"""
        return views.search_audit(
            ts_from=ts_from, ts_to=ts_to, actor=actor, action_type=action_type,
            source=source, page=page, page_size=page_size,
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> object:  # noqa: ARG001
        """HTTPException 直通（detail 已为 {code, message} 结构）。"""
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return app
