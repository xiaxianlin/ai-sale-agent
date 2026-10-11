# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V3 API：状态墙帧 / 批量操作 / ROI 只读视图（FR-SC-005 接口面，SR-SC-005 §5）。

路由（前缀 /api/v1/agent/v3）：
- GET  /wall?client_ver=  —— full/delta/ack 三态帧（WS 帧语义的 HTTP 轮询形态；
  delta.chin 仅为账号 ID 列表，卡片明细走 full 或按版本追赶）；
- POST /batch             —— 批量启停/策略调整（X-Idempotency-Key 头幂等，同步受理）；
- GET  /batch/{uid}       —— 批量进度查询（含逐项状态与失败清单）；
- GET  /roi               —— ROI 整体视图（totals/trend/ranking，预聚合只读）；
- GET  /roi/{account_id}  —— ROI 账号视图（无数据账号 404/40401）。

错误契约（04-接口设计 §1.3）：40001 参数/校验 → HTTP 400；40401 资源不存在 →
HTTP 404；detail 统一 {code, message}（中文消息）。
装配走 :func:`create_adminv3_app` 依赖注入（状态墙/批量/ROI 三核心，demo 默认进程内形态）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from services.agent.core.adminv3.batch import ACTIONS, BatchError, BatchOps
from services.agent.core.adminv3.roi import RoiStats
from services.agent.core.adminv3.wall import ClusterWall


class BatchBody(BaseModel):
    """批量受理入参（POST /batch）。"""

    action: str
    account_ids: list[int]
    params: dict[str, Any] | None = None


def _http_error(exc: BatchError) -> HTTPException:
    """BatchError → HTTP 契约（40001→400；40401→404；detail={code,message}）。"""
    status = 404 if exc.code == 40401 else 400
    return HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})


def _task_view(task: Any) -> dict[str, Any]:
    """批量任务出参（进度查询同构：uid/状态/总数/逐项清单/失败账号）。"""
    return {
        "uid": task.uid,
        "action": task.action,
        "status": task.status,
        "total": task.total,
        "account_ids": list(task.account_ids),
        "items": [
            {"account_id": item.account_id, "state": item.state, "error_msg": item.error_msg}
            for item in task.items
        ],
        "failed_account_ids": list(task.failed_account_ids),
        "created_by": task.created_by,
        "created_at": task.created_at.isoformat(),
    }


def _wall_frame_view(frame: dict[str, Any]) -> dict[str, Any]:
    """状态墙帧出参整形：delta.changes 由 {账号: 卡片} 转为账号 ID 升序列表
    (JSON 对象键恒为字符串，无法承载 int 语义；明细随 full 帧或按版本追赶)。"""
    if frame.get("type") == "delta":
        return {
            "type": "delta",
            "ver": frame["ver"],
            "changes": sorted(int(k) for k in frame["changes"]),
        }
    return frame


def _parse_window(date_from: date, date_to: date) -> tuple[date, date]:
    """ROI 窗口参数校验（from > to → 400/40001）。"""
    if date_from > date_to:
        raise HTTPException(status_code=400, detail={
            "code": 40001, "message": f"ROI 窗口起止倒置：{date_from} > {date_to}"})
    return date_from, date_to


def create_adminv3_app(
    *,
    wall: ClusterWall | None = None,
    batch_ops: BatchOps | None = None,
    roi: RoiStats | None = None,
) -> FastAPI:
    """装配管理后台 V3 FastAPI 应用（三核心依赖注入；默认项为进程内 demo 形态）。"""
    app = FastAPI(
        title="管理后台 V3 API（demo）",
        description="集群状态墙 + 批量操作 + ROI 统计（FR-SC-005，SR-SC-005 §5）",
        version="0.3.0",
    )
    wall = wall if wall is not None else ClusterWall()
    roi = roi if roi is not None else RoiStats()
    if batch_ops is None:
        def _demo_ctl(action: str, account_id: int) -> None:
            """demo 实例控制：无外部编排，语义成功（生产 = agent-runner 指令通道）。"""
            return None

        def _demo_writer(cfg_key: str, cfg_value: object, actor: str) -> None:
            return None

        batch_ops = BatchOps(instance_ctl=_demo_ctl, config_writer=_demo_writer)
    app.state.wall = wall
    app.state.batch_ops = batch_ops
    app.state.roi = roi

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """存活探针。"""
        return {"status": "ok"}

    @app.get("/api/v1/agent/v3/wall")
    def wall_frame(client_ver: int | None = Query(default=None)) -> dict[str, Any]:
        """状态墙帧：无参 → full；落后版本 → delta（账号 ID 列表）；最新 → ack。"""
        try:
            return _wall_frame_view(wall.frame(client_ver))
        except BatchError as exc:  # pragma: no cover——wall 帧不抛 BatchError，防御对齐
            raise _http_error(exc) from exc

    @app.post("/api/v1/agent/v3/batch")
    def batch_submit(body: BatchBody, request: Request) -> dict[str, Any]:
        """批量受理（同步执行 demo 形态）：X-Idempotency-Key 头幂等，必填。"""
        idem_key = request.headers.get("X-Idempotency-Key", "").strip()
        if not idem_key:
            raise HTTPException(status_code=400, detail={
                "code": 40001, "message": "批量操作必须携带 X-Idempotency-Key 幂等键"})
        try:
            task = batch_ops.submit(body.action, body.account_ids, idem_key=idem_key,
                                    actor="admin:api", params=body.params)
        except BatchError as exc:
            raise _http_error(exc) from exc
        return _task_view(task)

    @app.get("/api/v1/agent/v3/batch/{uid}")
    def batch_progress(uid: str) -> dict[str, Any]:
        """批量进度查询（含逐项状态与失败清单）。"""
        try:
            return _task_view(batch_ops.task(uid))
        except BatchError as exc:
            raise _http_error(exc) from exc

    @app.get("/api/v1/agent/v3/roi")
    def roi_overview(date_from: date = Query(...), date_to: date = Query(...)) -> dict[str, Any]:
        """ROI 整体视图（totals/trend/ranking；metric_daily 预聚合只读）。"""
        _parse_window(date_from, date_to)
        return roi.overview(date_from, date_to)

    @app.get("/api/v1/agent/v3/roi/{account_id}")
    def roi_account(account_id: int, date_from: date = Query(...),
                    date_to: date = Query(...)) -> dict[str, Any]:
        """ROI 账号视图（无数据账号 → 404/40401）。"""
        _parse_window(date_from, date_to)
        view = roi.account_view(account_id, date_from, date_to)
        if view is None:
            raise HTTPException(status_code=404, detail={
                "code": 40401, "message": f"账号 {account_id} 在窗口内无 ROI 数据"})
        return view

    # 动作白名单暴露给 OpenAPI 文档（batch_task CHECK 口径自描述）
    app.state.batch_actions = ACTIONS
    return app


__all__ = ["BatchBody", "create_adminv3_app"]
