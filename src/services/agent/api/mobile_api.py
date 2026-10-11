# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""移动端 API：双端免登 / 二次校验 / 一屏聚合 / 审批确认驳回 / 接管 / 推送登记
（FR-SC-006 接口面，SR-SC-006 §5）。

路由（前缀 /api/v1/mobile）：
- POST /auth                      —— 双端免登换发（channel=dingtalk|wechat）；
- GET  /home                      —— 一屏聚合（Bearer 鉴权，todo_alerts/todo_approvals/
                                     taking_over/today 四卡片）；
- POST /verify/challenge|token    —— 高危操作二次校验（校验码 → verify_token）；
- POST /approvals/{id}/confirm    —— 审批确认（**强制 verify_token**，SRS §7.4 红线；
                                     X-Idempotency-Key 透传幂等）；
- POST /approvals/{id}/reject     —— 审批驳回（低风险免二次校验，幂等透传）；
- POST /takeover                  —— 人工介入接管；
- POST /push/register             —— 微信订阅额度登记。

错误契约（04-接口设计 §1.3，detail={code,message} 中文消息）：
40001→400 / 40101→401 / 40102→401 / 40301→403 / 40401→404。
装配走 :func:`create_mobile_app`（注入 :class:`MobileBff`，demo 进程内形态）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from services.agent.core.mobile.bff import MobileBff, MobileError

_STATUS_BY_CODE: dict[int, int] = {
    40001: 400, 40101: 401, 40102: 401, 40301: 403, 40401: 404,
}


class AuthBody(BaseModel):
    """登录入参（钉钉免登凭据 / 微信 js_code）。"""

    channel: str
    credential: str


class VerifyTokenBody(BaseModel):
    """校验码换令牌入参。"""

    answer: str


class ConfirmBody(BaseModel):
    """审批确认入参（verify_token 必填——缺失直接 40102，绝不透传下游）。"""

    verify_token: str
    note: str = ""


class RejectBody(BaseModel):
    """审批驳回归参。"""

    reason: str


class TakeoverBody(BaseModel):
    """接管入参。"""

    conversation_uid: str
    reason: str = ""


class PushRegisterBody(BaseModel):
    """订阅额度登记入参。"""

    openid: str
    add_quota: int = 1


def _http_error(exc: MobileError) -> HTTPException:
    """MobileError → HTTP 契约（detail={code,message}）。"""
    return HTTPException(status_code=_STATUS_BY_CODE.get(exc.code, 400),
                         detail={"code": exc.code, "message": exc.message})


def create_mobile_app(*, bff: MobileBff) -> FastAPI:
    """装配移动端 FastAPI 应用（BFF 依赖注入）。"""
    app = FastAPI(
        title="移动端 API（demo）",
        description="双端免登 + 一屏聚合 + 审批移动流（FR-SC-006，SR-SC-006 §5）",
        version="0.1.0",
    )
    app.state.bff = bff

    def current_session(authorization: Annotated[str | None, Header()] = None) -> str:
        """Bearer 鉴权依赖：缺失/非 Bearer/无效一律 401/40101；返回原始令牌
        （BFF 业务方法内部再鉴权，令牌单点校验语义不重复实现）。"""
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail={
                "code": 40101, "message": "缺少登录态，请先登录"})
        raw = authorization.removeprefix("Bearer ").strip()
        try:
            bff.auth(raw)
        except MobileError as exc:
            raise _http_error(exc) from exc
        return raw

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """存活探针。"""
        return {"status": "ok"}

    @app.post("/api/v1/mobile/auth")
    def auth(body: AuthBody) -> dict[str, Any]:
        """双端免登换发（未绑定 403/40301，文案不泄露账号存在性）。"""
        try:
            return bff.login(body.channel, body.credential)
        except MobileError as exc:
            raise _http_error(exc) from exc

    @app.get("/api/v1/mobile/home")
    def home(token: str = Depends(current_session)) -> dict[str, Any]:
        """一屏聚合：四卡片一次返回（15s 短缓存；单路降级置 null）。"""
        try:
            return bff.home_summary(token)
        except MobileError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/v1/mobile/verify/challenge")
    def verify_challenge(token: str = Depends(current_session)) -> dict[str, Any]:
        """发起二次校验挑战（下发 6 位校验码）。"""
        try:
            return bff.request_challenge(token)
        except MobileError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/v1/mobile/verify/token")
    def verify_token(body: VerifyTokenBody,
                     token: str = Depends(current_session)) -> dict[str, Any]:
        """校验码换一次性确认令牌（错误/锁定 401/40102）。"""
        try:
            return bff.issue_verify_token(token, body.answer)
        except MobileError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/v1/mobile/approvals/{approval_id}/confirm")
    def approval_confirm(approval_id: str, body: ConfirmBody, request: Request,
                         token: str = Depends(current_session)) -> dict[str, Any]:
        """审批确认（敏感红线：强制 verify_token；无令牌/重放 401/40102 且下游零调用）。"""
        try:
            return bff.confirm_approval(
                token, approval_id, body.verify_token, note=body.note,
                idem_key=request.headers.get("X-Idempotency-Key"))
        except MobileError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/v1/mobile/approvals/{approval_id}/reject")
    def approval_reject(approval_id: str, body: RejectBody, request: Request,
                        token: str = Depends(current_session)) -> dict[str, Any]:
        """审批驳回（免二次校验；X-Idempotency-Key 透传幂等）。"""
        try:
            return bff.reject_approval(
                token, approval_id, body.reason,
                idem_key=request.headers.get("X-Idempotency-Key"))
        except MobileError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/v1/mobile/takeover")
    def takeover(body: TakeoverBody,
                 token: str = Depends(current_session)) -> dict[str, Any]:
        """人工介入接管（透传会话状态机 → human_managed）。"""
        try:
            return bff.takeover(token, body.conversation_uid, reason=body.reason)
        except MobileError as exc:
            raise _http_error(exc) from exc

    @app.post("/api/v1/mobile/push/register")
    def push_register(body: PushRegisterBody,
                      token: str = Depends(current_session)) -> dict[str, Any]:
        """微信订阅额度登记（引导授权口径）。"""
        try:
            return bff.push_register(token, body.openid, add_quota=body.add_quota)
        except MobileError as exc:
            raise _http_error(exc) from exc

    return app



__all__ = ["AuthBody", "ConfirmBody", "RejectBody", "TakeoverBody",
           "PushRegisterBody", "VerifyTokenBody", "create_mobile_app"]
