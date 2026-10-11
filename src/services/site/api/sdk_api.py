# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""SDK 面端点（FR-SITE-001/003；04-接口设计 §3.7 + SR-SITE-003 §5① 增补）。

- POST /api/v1/site/session       会话初始化：data-key + Origin 白名单 → 签发 visitor token
- POST /api/v1/site/chat          聊天上行（demo REST 形态；生产走 WS /ws/chat，消息协议同形）
- POST /api/v1/site/invite/check  主动邀聊判定（WS invite 下行的 REST 对端，事件驱动）
- POST /api/v1/site/leads         留资表单提交（lead_ack 形态：校验失败 ok=false + 字段错误）

统一信封 {code, message, data, trace_id}；错误码见 core/errors（40101 token、40001 入参、
40301 白名单、40302 consent）。
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from services.site.api.common import ok
from services.site.core.tracking.service import TrackedEvent

router = APIRouter(prefix="/api/v1/site", tags=["site-sdk"])


# --------------------------------------------------------------------------- 请求模型


class SessionReq(BaseModel):
    """会话初始化入参（SR-SITE-001 §5①）。"""

    key: str = Field(..., min_length=1, description="data-key（pk_site_ 前缀）")
    page: str = Field("", max_length=2048, description="当前页 URL")
    ref: str = Field("", max_length=2048, description="document.referrer")
    lang: str = Field("", max_length=16, description="navigator.language（服务端复检）")
    uid: str = Field("", max_length=64, description="跨会话匿名访客号（asa_uid，可空）")


class ChatReq(BaseModel):
    """聊天上行入参（WS type=text 的 REST 对端）。"""

    visitor_token: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=2000)
    round_no: int = Field(0, ge=0, description="会话轮次（0=会话建立，画像必注入）")
    msg_id: str = Field("", max_length=64, description="幂等键（WS msg_id 同形）")


class InviteCheckReq(BaseModel):
    """邀聊判定入参（行为事件驱动的 REST 对端）。"""

    visitor_token: str = Field(..., min_length=1)
    event: dict[str, Any] = Field(..., description="行为事件 {type, page, dwell_ms?, payload?}")


class LeadReq(BaseModel):
    """留资表单入参（WS type=lead 同形）。"""

    visitor_token: str = Field(..., min_length=1)
    email: str = Field("", max_length=256)
    whatsapp: str = Field("", max_length=32)
    source: str = Field("chat_form", max_length=32)
    trigger_event: str = Field("quote", max_length=32)


# --------------------------------------------------------------------------- 辅助


def _runtime(request: Request) -> Any:
    """取装配运行时（app.py 工厂注入）。"""
    return request.app.state.site


# --------------------------------------------------------------------------- 端点


@router.post("/session", summary="SDK 初始化：签发 visitor token（FR-SITE-001）")
def create_session(payload: SessionReq, request: Request) -> Any:
    """data-key + Origin 白名单校验后签发一次性 visitor token（04 §1.2/§3.7）。"""
    rt = _runtime(request)
    init = rt.sessions.create_session(
        key=payload.key,
        origin=request.headers.get("origin", ""),
        page=payload.page,
        referrer=payload.ref,
        lang=payload.lang,
        existing_uid=payload.uid,
    )
    return ok(request, {
            "visitor_token": init.visitor_token, "visitor_uid": init.visitor_uid,
            "lang": init.lang, "theme": init.theme, "faq": init.faq,
            "consent": {"required": init.consent_required, "granted": False},
            "ws_url": init.ws_url,
        })


@router.post("/chat", summary="访客消息 → 独立站对话引擎（FR-SITE-003）")
def chat(payload: ChatReq, request: Request) -> Any:
    """画像注入 → agent-core(site) 生成 → 幻觉防线 → 下行（引用随行，§7.4）。"""
    rt = _runtime(request)
    session = rt.session_of_token(payload.visitor_token)
    reply = rt.chat.reply(session, payload.text, round_no=payload.round_no, msg_id=payload.msg_id)
    return ok(request, {
            "msg_id": payload.msg_id or f"m-{uuid.uuid4().hex[:8]}",
            "role": "ai", "content": reply.content,
            "knowledge_refs": reply.knowledge_refs,
            "fallback": reply.fallback, "reason_code": reply.reason_code,
        })


@router.post("/invite/check", summary="主动邀聊判定（FR-SITE-003：阈值+频率上限）")
def invite_check(payload: InviteCheckReq, request: Request) -> Any:
    """行为事件 → 触发规则（dwell/repeat_view/add_cart）→ 每日 ≤2 次 + 冷却窗 → 开场白。"""
    rt = _runtime(request)
    session = rt.session_of_token(payload.visitor_token)
    raw = payload.event
    event = TrackedEvent(
        ev_id=str(raw.get("ev_id") or uuid.uuid4()),
        type=str(raw.get("type", "")),
        page=str(raw.get("page", "")),
        ts=float(raw.get("ts") or 0.0),
        dwell_ms=int(raw.get("dwell_ms") or 0),
        payload=dict(raw.get("payload") or {}),
    )
    decision = rt.invite_engine.on_event(session.uid, event, lang=session.lang)
    return ok(request, {
            "triggered": decision.triggered, "trigger_type": decision.trigger_type,
            "reason": decision.reason, "invite": decision.invite,
        })


@router.post("/leads", summary="询盘线索留资（FR-SITE-003：校验+Customer 渠道 site）")
def submit_lead(payload: LeadReq, request: Request) -> Any:
    """email/WhatsApp 结构化校验 → CustomerWriter（渠道 site）→ lead 事件/漏斗回填。

    校验失败为业务回执（code=0 + ok=false + 字段错误，WS lead_ack(ok=false) 同形）。
    """
    rt = _runtime(request)
    session = rt.session_of_token(payload.visitor_token)
    result = rt.leads.capture(
        session,
        {"email": payload.email, "whatsapp": payload.whatsapp},
        source=payload.source, trigger_event=payload.trigger_event,
    )
    return {
        "code": 0, "message": "ok", "data": result,
        "trace_id": request.headers.get("x-trace-id") or uuid.uuid4().hex[:16],
    }
