# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""AI 大脑 API（FR-AG-003/FR-AG-009 端点面，SR-AG-003 §5、SR-AG-009 §5；迭代 2.2 demo 口径）。

端点：
- POST /api/v1/agent/brain/respond——AI 大脑全链（意图→检索→生成→校验→置信度→兜底判定），
  返回决策明细；need_human=false 时经**注入的发送桩**自动发送（demo 口径：生产为
  FR-AG-005 风控通道 + 拟人输入，NFR-SEC-11）；need_human=true 时执行兜底编排
  （状态迁移/话术/告警/待办——依赖回调注入，执行器无 send 能力）；
- GET  /api/v1/agent/stats/inquiries——日处理询盘量报表（构成/占比/达标/归因，NFR-PER-04）；
- GET  /healthz——存活探针（compose healthcheck 消费）。

错误契约（04-接口设计 §1.3 对齐）：422 参数校验失败（40001 网关层映射）；502/50002
下游依赖降级（LLM 配置缺失/输出不可解析/检索失败）——降级时调用方按 FR-AG-009 转人工，
**不允许无依据发送**（发送桩零调用由测试钉死）。

组件全部注入（classifier/generator/stats/sender/状态机/待办队列），未注入项用进程内
默认装配（demo 形态；生产 = agent-core 容器化装配，01-系统架构设计 §3.1）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from services.agent.core.brain.fallback import (
    FallbackContext,
    FallbackDeps,
    InMemoryAckDedup,
    InMemoryTodoQueue,
    execute_fallback,
)
from services.agent.core.brain.generator import Customer, ReplyGenerator, ReplyRequest
from services.agent.core.brain.intent import IntentClassifier, LlmError
from services.agent.core.brain.stats import PROCESS_TYPES, DailyInquiryStats, shanghai_now
from services.agent.core.state import (
    ConversationStateMachine,
    ConvState,
    InMemorySessionStore,
)

#: 下游依赖降级错误码（04-接口设计 §1.3；LLM 配置缺失/输出不可解析/检索失败统一映射）
ERR_DOWNSTREAM = 50002


class CustomerBody(BaseModel):
    """客户档案请求段（SR-AG-003 §5 (2) 请求示例同形）。"""

    uid: str
    grade: str = "B"
    profile_tags: list[str] = Field(default_factory=list)
    lang: str = "en"


class BrainRespondBody(BaseModel):
    """respond 请求体：会话/账号/消息/文本/客户档案；trace_id 经 X-Trace-Id 头透传。"""

    conversation_uid: str = Field(min_length=1)
    account_id: int = Field(default=1, ge=0)
    message_uid: str = Field(default="")
    text: str = Field(min_length=1)
    customer: CustomerBody | None = None


class SenderStub:
    """自动发送桩（demo）：记录调用供测试断言「零调用 = 未强行发送」。

    生产形态为 FR-AG-005 风控通道 + 拟人输入器（FR-AG-002/NFR-SEC-11）；本桩仅承载
    「need_human=false 才可发送」的编排语义，不做任何外呼。
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._seq = 0

    def send(
        self, conversation_uid: str, draft: str, knowledge_refs: list[dict[str, Any]], *,
        account_id: int = 0,
    ) -> str:
        """记录一次自动发送并返回消息 ID（demo 内存序号；生产为平台消息 ID）。"""
        self._seq += 1
        message_id = f"msg-{self._seq}"
        self.calls.append({
            "conversation_uid": conversation_uid,
            "draft": draft,
            "knowledge_refs": knowledge_refs,
            "account_id": account_id,
            "message_id": message_id,
        })
        return message_id


def create_brain_app(
    *,
    classifier: IntentClassifier,
    generator: ReplyGenerator,
    stats: DailyInquiryStats,
    sender: SenderStub | None = None,
    store: InMemorySessionStore | None = None,
    state_machine: ConversationStateMachine | None = None,
    notify: Any | None = None,
    todo_queue: InMemoryTodoQueue | None = None,
    ack_dedup: Any | None = None,
    gap_recorder: Any | None = None,
    monitor: Any | None = None,
    promise_hours: int = 4,
) -> FastAPI:
    """装配 AI 大脑 FastAPI 应用（依赖注入；默认项为进程内 demo 形态）。

    :param notify: 告警事件汇聚（生产 = FR-AG-007 POST /api/v1/alerts；demo 进程内列表）。
    """
    app = FastAPI(
        title="AI 大脑 API（demo）",
        description="回复生成全链 + 低置信兜底 + 询盘统计（FR-AG-003/009、NFR-PER-03/04）",
        version="0.1.0",
    )
    sender = sender if sender is not None else SenderStub()
    store = store if store is not None else InMemorySessionStore()
    state_machine = state_machine if state_machine is not None else ConversationStateMachine(store)
    alerts: list[dict[str, Any]] = []
    todo_queue = todo_queue if todo_queue is not None else InMemoryTodoQueue()
    dedup = ack_dedup if ack_dedup is not None else InMemoryAckDedup().first_seen
    app.state.classifier = classifier
    app.state.generator = generator
    app.state.stats = stats
    app.state.sender = sender
    app.state.alerts = alerts
    app.state.todo_queue = todo_queue

    def _default_notify(payload: dict[str, Any]) -> None:
        alerts.append(payload)

    def _deps() -> FallbackDeps:
        """按请求装配兜底依赖（状态迁移绑状态机；告警绑进程内汇聚）。"""

        def state_transition(uid: str, to_state: str, actor: str, reason: str) -> Any:
            return state_machine.transition(
                uid, ConvState(to_state), actor=actor, reason=reason,
            )

        return FallbackDeps(
            state_transition=state_transition,
            notify=notify if notify is not None else _default_notify,
            pending_queue=todo_queue,
            ack_dedup=dedup,
            gap_recorder=gap_recorder,
            monitor=monitor,
            promise_hours=promise_hours,
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """存活探针。"""
        return {"status": "ok"}

    @app.post("/api/v1/agent/brain/respond")
    def respond(body: BrainRespondBody, request: Request) -> dict[str, Any]:
        """AI 大脑全链响应：决策明细 + 自动发送/兜底编排结果。"""
        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
        customer = (
            Customer(
                uid=body.customer.uid, grade=body.customer.grade,
                profile_tags=tuple(body.customer.profile_tags), lang=body.customer.lang,
            )
            if body.customer is not None
            else Customer(uid="anonymous")
        )
        req = ReplyRequest(
            message_uid=body.message_uid or f"msg-{uuid.uuid4().hex[:8]}",
            conversation_uid=body.conversation_uid,
            account_id=body.account_id,
            text=body.text,
            customer=customer,
        )
        try:
            intent = classifier.classify(body.text)
            result = generator.generate(req, intent)
        except LlmError as exc:
            # 下游依赖降级（50002）：调用方按 FR-AG-009 转人工，不允许无依据发送
            raise HTTPException(status_code=502, detail={
                "code": ERR_DOWNSTREAM, "message": str(exc), "trace_id": trace_id,
            }) from exc
        state_machine.ensure_conversation(body.conversation_uid)  # 会话定位（默认 ai_managed）
        fallback_payload: dict[str, Any] | None = None
        message_id: str | None = None
        if result.need_human:
            outcome = execute_fallback(
                result.verdict,
                FallbackContext(
                    conversation_uid=body.conversation_uid,
                    message_uid=req.message_uid,
                    account_id=body.account_id,
                    intent=intent.name,
                    query=body.text,
                    reply_draft=result.reply_draft,
                    confidence=result.confidence,
                ),
                _deps(),
            )
            fallback_payload = {
                "todo_id": outcome.todo_id,
                "state": outcome.state,
                "ack_sent": outcome.ack_sent,
                "ack_text": outcome.ack_text,
                "alert_emitted": outcome.alert_emitted,
                "gap_recorded": outcome.gap_recorded,
                "errors": list(outcome.errors),
            }
        else:
            # 唯一自动发送出口：need_human=false 才触达发送桩（结构保证，非约定）
            message_id = sender.send(
                body.conversation_uid, result.reply_draft, result.knowledge_refs,
                account_id=body.account_id,
            )
        # 询盘统计埋点（NFR-PER-04）：买家入站前提由请求本身承载（demo 口径）；
        # 终态映射 sent→ai_auto、兜底→fallback（转人工落定为 human_transfer，归管理后台通道）
        stats.record_inbound(body.account_id, body.conversation_uid)
        stats.record_handled(
            body.account_id, body.conversation_uid,
            "ai_auto" if message_id is not None else "fallback",
        )
        return {
            "trace_id": trace_id,
            "intent": {"name": intent.name, "confidence": intent.confidence, "slots": intent.slots},
            "reply_draft": result.reply_draft,
            "confidence": result.confidence,
            "conf_components": result.conf_components,
            "knowledge_refs": result.knowledge_refs,
            "need_human": result.need_human,
            "reason_code": result.reason_code,
            "action": result.action,
            "sent": message_id is not None,
            "message_id": message_id,
            "fallback": fallback_payload,
        }

    @app.get("/api/v1/agent/stats/inquiries")
    def stats_inquiries(stat_date: str | None = None, account_id: int | None = None) -> dict[str, Any]:
        """日处理询盘量报表：构成/占比/达标/归因（NFR-PER-04 §16.3 证据面）。"""
        try:
            day = (
                datetime.fromisoformat(stat_date).date()
                if stat_date
                else shanghai_now().date()
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={
                "code": 40001, "message": f"stat_date 格式非法（须 YYYY-MM-DD）：{stat_date!r}",
            }) from exc
        rows = stats.daily_check(day)
        if account_id is not None:
            rows = [row for row in rows if row["account_id"] == account_id]
        return {
            "stat_date": day.isoformat(),
            "floor": 15,
            "handled_total": sum(row["handled_total"] for row in rows),
            "accounts": rows,
            "process_types": list(PROCESS_TYPES),
        }

    install_agent_error_handlers(app)  # CR A2：统一信封
    # CR 遗留 R2：租户上下文薄中间件（demo 单租户默认 1；多租户经部署开关）
    from foundation.core.tenantctx import TenantContextLiteMiddleware

    app.add_middleware(TenantContextLiteMiddleware)

    return app


__all__ = ["ERR_DOWNSTREAM", "BrainRespondBody", "CustomerBody", "SenderStub", "create_brain_app"]


def install_agent_error_handlers(app) -> None:
    """统一信封错误处理（CR A2 补齐：HTTPException → 四键信封同构）。"""
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
                "trace_id": detail.get("trace_id") or resolve_trace_id(request),
            },
        )
