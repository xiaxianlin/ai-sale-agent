# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""行为追踪端点（FR-SITE-002；04-接口设计 §3.7：POST /api/v1/site/events 批量上报）。

consent 双端门控（06-安全 §9 红线）：SDK 侧禁发之外，服务端复检 consent（40302 强制）；
事件落库后触发该访客画像重建（有新事件才触发，SR-SITE-002 §6.3）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from services.site.api.common import ok

router = APIRouter(prefix="/api/v1/site", tags=["site-tracking"])


class EventsReq(BaseModel):
    """批量上报入参（SR-SITE-002 §5①）。"""

    visitor_token: str = Field(..., min_length=1)
    consent: bool = Field(..., description="客户端 consent 标志（服务端另复检会话状态）")
    events: list[dict[str, Any]] = Field(..., min_length=1, description="行为事件批（≤50 条）")


@router.post("/events", summary="访客行为批量上报（FR-SITE-002）")
def report_events(payload: EventsReq, request: Request) -> Any:
    """consent 门控 → 日限流 → 枚举/幂等/脱敏 → 批量入库 → 画像重建。"""
    rt = request.app.state.site
    session = rt.session_of_token(payload.visitor_token)
    result = rt.ingest.ingest(session.uid, session.tenant_id, payload.consent, payload.events)
    if result.accepted:
        rt.rebuild_profile(session.uid)
    return ok(request, {"accepted": result.accepted, "dropped": result.dropped})
