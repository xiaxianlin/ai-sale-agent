# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""集群管理 API（FR-SC-001/007 对外接口面，SR-SC-001 §5、SR-SC-007 §5；demo 进程内形态）。

端点（前缀 /api/v1/agent/cluster）：
- GET  /instances                          集群视图（total/by_status/items）；
- POST /instances/{id}/start|stop|restart  实例级启停重启（start = 状态翻转 + 容器拉起）；
- POST /batch                              批量操作对账（accepted/failed/report_id）；
- GET  /gray/status                        灰度状态视图（计划/状态/分组计数/决策数）；
- POST /gray/promote | /gray/rollback      放量/回滚（审批人口径，决策落台账）。

错误契约（detail.code 机器可读）：
- 404 NOT_FOUND（实例/计划不存在）；409 PLAN_STATE_INVALID / INST_BUSY（状态机与互斥）；
- 422 ILLEGAL_TRANSITION（非法状态迁移）；503 DOCKER_UNREACHABLE（容器操作失败）。

装配：create_cluster_app(registry, orchestrator, gray, alerts) 依赖注入——生产以 PG
注册表 + Docker 编排器替换注入件，路由契约不变。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from services.agent.core.cluster.gray import GrayError, GrayReleaseManager
from services.agent.core.cluster.orchestrator import ClusterOrchestrator
from services.agent.core.cluster.registry import ClusterError, InstanceRegistry

#: 接口前缀（SR-SC-001 §5）
PREFIX = "/api/v1/agent/cluster"


class BatchBody(BaseModel):
    """批量操作请求体（action 限 start/stop/restart）。"""

    action: str = Field(description="批量动作（start/stop/restart）")
    account_ids: list[int] = Field(description="目标账号清单")


class ApproverBody(BaseModel):
    """放量/回滚请求体（审批人必填——§15.3 决策记录口径）。"""

    approver: str = Field(min_length=1, description="审批人")
    reason: str | None = Field(default=None, description="回滚原因（回滚必填口径）")


def _detail(code: str, message: str) -> dict[str, str]:
    """错误 detail 结构（code 机器可读 + message 中文可读）。"""
    return {"code": code, "message": message}


def _op_response(result) -> dict[str, Any]:
    """OpResult → HTTP：ok 直返；ILLEGAL_TRANSITION→422；DOCKER_UNREACHABLE→503。"""
    if result.ok:
        return {"status": result.status, "code": result.code}
    if result.code == "ILLEGAL_TRANSITION":
        raise HTTPException(status_code=422, detail=_detail(
            "ILLEGAL_TRANSITION", "非法状态迁移（实例须经人工处置后方可操作）"))
    raise HTTPException(status_code=503, detail=_detail(
        "DOCKER_UNREACHABLE", "容器操作失败（Docker 不可达或启动被拒绝）"))


def create_cluster_app(
    *,
    registry: InstanceRegistry,
    orchestrator: ClusterOrchestrator,
    gray: GrayReleaseManager,
    alerts=None,  # noqa: ARG001 —— 告警通道经注入件自带，预留装配一致性
) -> FastAPI:
    """装配集群管理 FastAPI 应用（依赖注入：注册表/编排器/灰度管理器）。"""
    app = FastAPI(
        title="agent 集群管理 API（demo）",
        description="28 实例集群视图/实例操作/批量对账/灰度放量回滚（FR-SC-001/007）",
        version="0.1.0",
    )

    @app.get(f"{PREFIX}/instances")
    def instances() -> dict[str, Any]:
        """集群视图：total/by_status/items（看板与供给对账数据源）。"""
        rows = registry.all()
        return {
            "total": len(rows),
            "by_status": registry.by_status(),
            "items": [{
                "account_id": r.account_id,
                "container_name": r.container_name,
                "proxy_ip": r.proxy_ip,
                "cookie_volume": r.cookie_volume,
                "status": r.status,
                "gray_batch": r.gray_batch,
                "group_tag": r.group_tag,
            } for r in rows],
        }

    def _cluster_error(exc: ClusterError) -> HTTPException:
        """注册表/编排器域异常 → HTTP 错误契约（NOT_FOUND/ILLEGAL_TRANSITION/INST_BUSY）。"""
        status = {"NOT_FOUND": 404, "ILLEGAL_TRANSITION": 422, "INST_BUSY": 409}.get(
            exc.code, 409)
        return HTTPException(status_code=status, detail=_detail(exc.code, exc.message))

    def _instance_action(account_id: int, action: str) -> dict[str, Any]:
        """实例操作统一入口：域异常 → HTTP 错误契约；start 追加容器真实拉起。"""
        try:
            if action == "start":
                result = orchestrator.start_instance(account_id)  # 状态机翻转
                if result.ok:
                    result = orchestrator.launch_container(account_id)  # 容器拉起（可 503）
            else:
                method = {"stop": orchestrator.stop_instance,
                          "restart": orchestrator.restart_instance}[action]
                result = method(account_id)
        except ClusterError as exc:
            raise _cluster_error(exc) from exc
        return _op_response(result)

    @app.post(f"{PREFIX}/instances/{{account_id}}/start")
    def start_instance(account_id: int) -> dict[str, Any]:
        """启动实例（offline/paused → running；Docker 失败 503，不虚报状态）。"""
        return _instance_action(account_id, "start")

    @app.post(f"{PREFIX}/instances/{{account_id}}/stop")
    def stop_instance(account_id: int) -> dict[str, Any]:
        """停止实例（running/paused → paused；容器停止、Cookie 卷保留）。"""
        return _instance_action(account_id, "stop")

    @app.post(f"{PREFIX}/instances/{{account_id}}/restart")
    def restart_instance(account_id: int) -> dict[str, Any]:
        """重启实例（running → running；互斥锁占用 409）。"""
        return _instance_action(account_id, "restart")

    @app.post(f"{PREFIX}/batch")
    def batch(body: BatchBody) -> dict[str, Any]:
        """批量操作对账：逐实例独立执行（单点失败不扩散），accepted/failed 对账出参。"""
        accepted, failed = 0, []
        for account_id in body.account_ids:
            try:
                result = _instance_action(account_id, body.action)
            except HTTPException as exc:
                code = exc.detail.get("code", "ERROR") if isinstance(exc.detail, dict) else "ERROR"
                failed.append({"account_id": account_id, "code": code})
                continue
            if result.get("status"):
                accepted += 1
            else:
                failed.append({"account_id": account_id, "code": result.get("code", "ERROR")})
        return {"accepted": accepted, "failed": failed,
                "report_id": f"br-{uuid.uuid4().hex[:12]}"}

    # ---- 灰度接口（SR-SC-007 §5） ----

    @app.get(f"{PREFIX}/gray/status")
    def gray_status() -> dict[str, Any]:
        """灰度状态视图：计划号/状态/分组计数/决策数（看板数据源契约）。"""
        plan = gray.latest_plan()
        if plan is None:
            raise HTTPException(status_code=404, detail=_detail("NOT_FOUND", "暂无灰度计划"))
        return {
            "plan_no": plan.plan_no,
            "state": plan.state,
            "groups": registry.group_counts(),
            "decision_count": len(gray.decisions),
            "gray_group": plan.gray_group,
            "observe_window_days": plan.observe_window_days,
        }

    @app.post(f"{PREFIX}/gray/promote")
    def gray_promote(body: ApproverBody) -> dict[str, Any]:
        """放量（promote）：仅 gate_passed 可放量；状态非法 409（决策落台账）。"""
        plan = gray.latest_plan()
        if plan is None:
            raise HTTPException(status_code=404, detail=_detail("NOT_FOUND", "暂无灰度计划"))
        try:
            result = gray.rollout(plan.plan_no, approver=body.approver, now=datetime.now())
        except GrayError as exc:
            raise HTTPException(status_code=409,
                                detail=_detail(exc.code, exc.message)) from exc
        return result

    @app.post(f"{PREFIX}/gray/rollback")
    def gray_rollback(body: ApproverBody) -> dict[str, Any]:
        """一键回滚：覆盖摘除 + 试点组暂停打回 control；重复回滚 409 幂等拦截。"""
        plan = gray.latest_plan()
        if plan is None:
            raise HTTPException(status_code=404, detail=_detail("NOT_FOUND", "暂无灰度计划"))
        try:
            result = gray.rollback(plan.plan_no, approver=body.approver,
                                   reason=body.reason or "未填写原因", now=datetime.now())
        except GrayError as exc:
            raise HTTPException(status_code=409,
                                detail=_detail(exc.code, exc.message)) from exc
        return result

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
