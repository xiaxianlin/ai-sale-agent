# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""隐私合规端点（FR-SITE-007；SR-SITE-007 §5①②，增补码按 04 §5 走 v1.x 登记）。

- POST /api/v1/privacy/consent          consent 三态上报落证（Bearer visitor token）
- POST /api/v1/privacy/dsr/delete       DSR 删除工单（邮箱定位 → 跨表删除）
- POST /api/v1/privacy/dsr/export       DSR 导出工单（JSON 打包）
- POST /api/v1/privacy/dsr/{id}/execute 执行工单（幂等：completed 返回首次 result）
- GET  /api/v1/privacy/dsr/{id}         工单查询（状态/result/时限告警分级）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from services.site.api.common import envelope, ok

router = APIRouter(prefix="/api/v1/privacy", tags=["site-privacy"])


class ConsentReq(BaseModel):
    """consent 上报入参（SR-SITE-007 §5①）。"""

    status: str = Field(..., min_length=1, description="granted | necessary | denied")
    sdk_version: str = Field("", max_length=32)
    policy_ver: str = Field("", max_length=16)


class DsrReq(BaseModel):
    """DSR 工单入参（数据主体自助/后台代录）。"""

    email: str = Field(..., min_length=3, max_length=256)


def _bearer_token(request: Request) -> str:
    """取 Bearer visitor token（缺失 → 空串交由会话校验报 40101）。"""
    header = request.headers.get("authorization", "")
    return header[len("Bearer "):].strip() if header.lower().startswith("bearer ") else ""


@router.post("/consent", summary="consent 状态上报落证（FR-SITE-007）")
def report_consent(payload: ConsentReq, request: Request) -> Any:
    """三态落证（10s 同状态去重幂等）+ 审计；同步门控行为采集与邀聊。"""
    rt = request.app.state.site
    session = rt.session_of_token(_bearer_token(request))
    record = rt.consent.set_consent(
        session.uid, payload.status,
        sdk_version=payload.sdk_version, policy_ver=payload.policy_ver or None,
    )
    return ok(request, {"recorded": record.recorded})


def _open_dsr(payload: DsrReq, request: Request, req_type: str) -> Any:
    """建单共用体（delete/export）。"""
    rt = request.app.state.site
    ticket = rt.dsr.open_ticket(payload.email, req_type)
    return ok(request, {
        "ticket_id": ticket.ticket_id, "req_type": ticket.req_type,
        "email": ticket.email, "status": ticket.status,
        "created_at": ticket.created_at, "due_at": ticket.due_at,
    })


@router.post("/dsr/delete", summary="DSR 删除工单（GDPR Art.17）")
def dsr_delete(payload: DsrReq, request: Request) -> Any:
    """建删除工单：due_at = 创建 + 30 天（时限跟踪）。"""
    return _open_dsr(payload, request, "delete")


@router.post("/dsr/export", summary="DSR 导出工单（GDPR Art.15）")
def dsr_export(payload: DsrReq, request: Request) -> Any:
    """建导出工单：JSON 打包全部留存数据段。"""
    return _open_dsr(payload, request, "export")


@router.post("/dsr/{ticket_id}/execute", summary="执行 DSR 工单（幂等）")
def dsr_execute(ticket_id: str, request: Request) -> Any:
    """执行删除（跨表清单计数）或导出（打包段清单）；已 completed 返回首次 result。"""
    rt = request.app.state.site
    result = rt.dsr.execute(ticket_id)
    ticket = rt.dsr.get(ticket_id)
    return ok(request, {"ticket_id": ticket_id, "status": ticket.status, "result": result})


@router.get("/dsr/{ticket_id}", summary="DSR 工单查询（含时限告警分级）")
def dsr_get(ticket_id: str, request: Request) -> Any:
    """工单状态 + result + due_at 剩余天数与告警分级（mid=临期 ≤3 天，high=超期）。"""
    rt = request.app.state.site
    ticket = rt.dsr.get(ticket_id)
    rows = [r for r in rt.dsr.deadline_report() if r["ticket_id"] == ticket_id]
    alert = rows[0]["alert"] if rows else "none"
    return ok(request, {
        "ticket_id": ticket.ticket_id, "req_type": ticket.req_type,
        "email": ticket.email, "status": ticket.status,
        "created_at": ticket.created_at, "due_at": ticket.due_at,
        "result": ticket.result, "alert": alert,
    })
