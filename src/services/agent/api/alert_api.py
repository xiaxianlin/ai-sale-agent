# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""告警 API（FR-AG-007，SR-AG-007 §5；迭代 2.3 demo 口径）。

端点：
- POST /api/v1/agent/alerts                内部上报（type 默认分级 + 去重聚合）
- GET  /api/v1/agent/alerts                告警列表（status/level/type 过滤；后台告警中心数据源）
- POST /api/v1/agent/alerts/{uid}/ack      人工确认（firing→acknowledged）
- POST /api/v1/agent/alerts/{uid}/resolve  处置完成（acknowledged→resolved）

错误契约：40401 告警不存在 / 40901 非法状态迁移（处置闭环状态机）。
出参形态沿用 §5-1：{alert_uid, aggregated, level, channels}。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from services.agent.core.alert.router import AlertRouter, InvalidTransitionError

#: 错误码（SR-AG-007 §5）
ERR_ALERT_NOT_FOUND = 40401
ERR_INVALID_TRANSITION = 40901

#: 默认操作者（生产 = JWT 身份解析）
DEFAULT_OPERATOR = "user:operator"


class AlertSubmitBody(BaseModel):
    """上报请求体（§5-1 入参 + 向下兼容扩展的复核/去重字段）。"""

    type: str = Field(description="告警类型（big_customer/system_error/risk_hit）")
    title: str = Field(min_length=1, description="标题")
    body: str = Field(min_length=1, description="正文（发送前经脱敏，NFR-SEC-01）")
    level: str | None = Field(default=None, description="级别（缺省按 type 默认映射）")
    dedup_key: str | None = Field(default=None, description="去重聚合键（type+对象）")
    customer_grade: str | None = Field(default=None, description="客户分级（大客户复核）")


class OperatorBody(BaseModel):
    """处置请求体：operator 必填（响应时长统计与审计口径）。"""

    operator: str = Field(min_length=1, description="操作人")
    note: str | None = Field(default=None, description="处置说明（选填）")


def _to_http(exc: Exception) -> HTTPException:
    """域异常 → HTTPException。"""
    if isinstance(exc, InvalidTransitionError):
        return HTTPException(status_code=409, detail={
            "code": ERR_INVALID_TRANSITION, "message": str(exc)})
    return HTTPException(status_code=500, detail={"code": 50000, "message": str(exc)})


def _alert_row(alert: Any) -> dict[str, Any]:
    """告警行序列化（全生命周期字段 + 送达明细）。"""
    return {
        "uid": alert.uid,
        "level": alert.level,
        "type": alert.type,
        "title": alert.title,
        "dedup_key": alert.dedup_key,
        "status": alert.status,
        "agg_count": alert.agg_count,
        "escalated": alert.escalated,
        "acked_by": alert.acked_by,
        "acked_at": alert.acked_at.isoformat() if alert.acked_at else None,
        "resolved_by": alert.resolved_by,
        "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
        "deliveries": [
            {"channel": d.channel, "status": d.status, "attempts": d.attempts}
            for d in alert.deliveries
        ],
    }


def create_alert_app(*, router: AlertRouter) -> FastAPI:
    """装配告警 FastAPI 应用（依赖注入：AlertRouter）。"""
    app = FastAPI(
        title="agent 告警 API（demo）",
        description="告警上报/列表/确认/处置（FR-AG-007）",
        version="0.1.0",
    )

    @app.post("/api/v1/agent/alerts")
    def submit_alert(body: AlertSubmitBody) -> dict[str, Any]:
        """内部上报：去重聚合 + 分级分发（业务方同步轻量入队语义）。"""
        result = router.submit(
            type=body.type, title=body.title, body=body.body, level=body.level,
            dedup_key=body.dedup_key, customer_grade=body.customer_grade,
        )
        return {
            "alert_uid": result.uid,
            "aggregated": result.aggregated,
            "level": result.level,
            "channels": list(result.channels),
        }

    @app.get("/api/v1/agent/alerts")
    def list_alerts(
        status: str | None = None, level: str | None = None, type: str | None = None,  # noqa: A002
    ) -> dict[str, Any]:
        """告警列表（后台告警中心/待办数据源）。"""
        return {"items": [_alert_row(alert) for alert in router.list(
            status=status, level=level, type=type)]}

    @app.post("/api/v1/agent/alerts/{uid}/ack")
    def ack_alert(uid: str, body: OperatorBody) -> dict[str, Any]:
        """人工确认（响应时长统计终点；升级链路随之停止）。"""
        try:
            alert = router.ack(uid, operator=body.operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={
                "code": ERR_ALERT_NOT_FOUND, "message": str(exc)}) from exc
        except InvalidTransitionError as exc:
            raise _to_http(exc) from exc
        return {"uid": uid, "status": alert.status, "acked_by": alert.acked_by}

    @app.post("/api/v1/agent/alerts/{uid}/resolve")
    def resolve_alert(uid: str, body: OperatorBody) -> dict[str, Any]:
        """处置完成（处置人/说明留痕）。"""
        try:
            alert = router.resolve(uid, operator=body.operator)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={
                "code": ERR_ALERT_NOT_FOUND, "message": str(exc)}) from exc
        except InvalidTransitionError as exc:
            raise _to_http(exc) from exc
        return {"uid": uid, "status": alert.status, "resolved_by": alert.resolved_by}

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
