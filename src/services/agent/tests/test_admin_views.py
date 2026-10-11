# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V1 查询逻辑单元测试（FR-AG-006，SR-AG-006 §6.3/§9；迭代 2.3 TDD 阶段①用例）。

覆盖（后台 = 纯读 + 控制面，数据源注入）：
- 会话列表：四态过滤 + 服务端分页；
- 消息流：增量游标（after_id）、AI 消息含置信度与知识来源；
- 接管/放回：走状态机迁移 + state_hist 留痕 + 审计；closed 会话接管冲突；
- 敏感确认：对比视图（draft vs snapshot 差异键）、确认/驳回决策双人可见；
- 审计检索：时间/操作者/动作类型/来源多维筛选 + 哈希链完整性校验（篡改可发现）。

实现方案：docs/07-实现方案/AG/SR-AG-006-管理后台V1.md §6.2、§6.3、§9
"""

from __future__ import annotations

from datetime import datetime

import pytest

from services.agent.core.admin.views import (
    AdminViews,
    HashChainAuditLog,
    SensitiveBoard,
    SensitiveNotFoundError,
    SensitiveStateError,
)
from services.agent.core.state import (
    ConversationStateMachine,
    ConvState,
    IllegalTransitionError,
    InMemorySessionStore,
)

NOW = datetime(2026, 10, 8, 10, 0, 0)


def make_views():
    """标准装配：内存存储 + 状态机 + 哈希链审计 + 敏感看板 + 消息流。"""
    store = InMemorySessionStore()
    sm = ConversationStateMachine(store)
    audit_log = HashChainAuditLog()
    sensitive = SensitiveBoard()
    views = AdminViews(store=store, state_machine=sm, audit_log=audit_log,
                       sensitive=sensitive)
    # 造四态会话
    sm.ensure_conversation("c-ai")
    sm.ensure_conversation("c-human")
    sm.ensure_conversation("c-pending")
    sm.ensure_conversation("c-closed")
    sm.transition("c-human", ConvState.HUMAN_MANAGED, actor="sale", reason="接管")
    sm.transition("c-pending", ConvState.PENDING_CONFIRM, actor="system:risk", reason="敏感拦截")
    sm.transition("c-closed", ConvState.CLOSED, actor="sale", reason="结束")
    views.messages["c-ai"] = [
        {"msg_id": 1, "sender": "buyer", "content": "hello", "confidence": None,
         "knowledge_refs": [], "created_at": NOW},
        {"msg_id": 2, "sender": "ai", "content": "Thanks! [R1]", "confidence": 0.91,
         "knowledge_refs": ({"chunk_id": 7, "kb_type": "product", "title": "手册", "score": 0.83},),
         "created_at": NOW},
        {"msg_id": 3, "sender": "buyer", "content": "price?", "confidence": None,
         "knowledge_refs": [], "created_at": NOW},
    ]
    return views, sm, store, audit_log, sensitive


class TestSessionList:
    """会话中心：四态过滤 + 分页。"""

    def test_filter_by_state(self) -> None:
        """四态过滤：state 参数精确过滤（ai/human/pending/closed 各 1）。"""
        views, _, _, _, _ = make_views()
        for state in ("ai_managed", "human_managed", "pending_confirm", "closed"):
            rows = views.list_sessions(state=state)["items"]
            assert len(rows) == 1 and rows[0]["state"] == state

    def test_no_filter_returns_all(self) -> None:
        """不过滤返回全部会话。"""
        views, _, _, _, _ = make_views()
        assert views.list_sessions()["total"] == 4

    def test_pagination(self) -> None:
        """服务端分页：page/page_size 切片 + total 独立于页大小。"""
        views, _, _, _, _ = make_views()
        page1 = views.list_sessions(page=1, page_size=2)
        page2 = views.list_sessions(page=2, page_size=2)
        assert len(page1["items"]) == 2 and page1["total"] == 4
        assert len(page2["items"]) == 2
        assert {r["uid"] for r in page1["items"]}.isdisjoint({r["uid"] for r in page2["items"]})


class TestMessageStream:
    """消息流：增量游标 + AI 置信度与来源。"""

    def test_ai_message_carries_confidence_and_refs(self) -> None:
        """AI 消息含置信度与知识来源（三色标识的数据面）；买家消息为空。"""
        views, _, _, _, _ = make_views()
        items = views.session_messages("c-ai")["items"]
        ai_row = next(m for m in items if m["sender"] == "ai")
        assert ai_row["confidence"] == 0.91
        assert ai_row["knowledge_refs"][0]["chunk_id"] == 7
        assert all(m["confidence"] is None for m in items if m["sender"] == "buyer")

    def test_after_id_incremental(self) -> None:
        """增量游标：after_id 之后才返回，next_after_id 推进。"""
        views, _, _, _, _ = make_views()
        page = views.session_messages("c-ai", after_id=1)
        assert [m["msg_id"] for m in page["items"]] == [2, 3]
        assert page["next_after_id"] == 3

    def test_unknown_conversation_raises(self) -> None:
        """未知会话 → ConversationNotFoundError（HTTP 404 由 API 层映射）。"""
        views, _, _, _, _ = make_views()
        from services.agent.core.state import ConversationNotFoundError
        with pytest.raises(ConversationNotFoundError):
            views.session_messages("c-none")


class TestTakeoverRelease:
    """人工介入开关：状态机迁移 + 留痕 + 审计。"""

    def test_takeover_then_release(self) -> None:
        """接管 → human_managed；放回 → ai_managed；state_hist 全程留痕。"""
        views, sm, _, _, _ = make_views()
        result = views.takeover("c-ai", operator="sale-07", reason="客户投诉")
        assert result["state"] == "human_managed"
        assert result["state_hist"][-1]["by"] == "sale-07"
        back = views.release("c-ai", operator="sale-07", reason="处理完毕")
        assert back["state"] == "ai_managed"

    def test_takeover_audited(self) -> None:
        """接管/放回写审计（NFR-SEC-03，操作日志全量可审计）。"""
        views, _, _, audit_log, _ = make_views()
        views.takeover("c-ai", operator="sale-07", reason="客户投诉")
        actions = [e["action_type"] for e in audit_log.all()]
        assert "human_takeover" in actions

    def test_takeover_closed_conflict(self) -> None:
        """closed 会话接管 → IllegalTransitionError（HTTP 422 由 API 层映射）。"""
        views, _, _, _, _ = make_views()
        with pytest.raises(IllegalTransitionError):
            views.takeover("c-closed", operator="sale", reason="试图接管已结束会话")


class TestSensitiveBoard:
    """敏感确认：对比视图 + 确认/驳回双人可见。"""

    def test_diff_view(self) -> None:
        """对比视图：draft vs snapshot 差异键命中改价字段。"""
        views, _, _, _, sensitive = make_views()
        record = sensitive.create(conversation_uid="c-pending", action_type="price_change",
                                  draft_content={"price": 11.80},
                                  current_snapshot={"price": 12.50}, now=NOW)
        diff = views.sensitive_diff(record.uid)
        assert diff["diff_keys"] == ["price"]
        assert diff["draft_content"]["price"] == 11.80
        assert diff["current_snapshot"]["price"] == 12.50

    def test_confirm_records_decider_visible(self) -> None:
        """确认：status=confirmed，决策人/时刻在列表与详情可见（双人可见）。"""
        views, _, _, _, sensitive = make_views()
        record = sensitive.create(conversation_uid="c-pending", action_type="order_op",
                                  draft_content={"qty": 100}, current_snapshot={}, now=NOW)
        views.confirm_sensitive(record.uid, operator="supervisor", now=NOW)
        row = views.list_sensitive(status="confirmed")[0]
        assert row["decided_by"] == "supervisor" and row["decided_at"] is not None
        assert sensitive.get(record.uid).status == "confirmed"

    def test_reject_records_reason_visible(self) -> None:
        """驳回：理由留痕且双人可见。"""
        views, _, _, _, sensitive = make_views()
        record = sensitive.create(conversation_uid="c-pending", action_type="payment",
                                  draft_content={}, current_snapshot={}, now=NOW)
        views.reject_sensitive(record.uid, operator="supervisor", reason="条款有误", now=NOW)
        row = views.list_sensitive(status="rejected")[0]
        assert row["reject_reason"] == "条款有误"

    def test_double_decision_rejected(self) -> None:
        """已决策草稿不可二次决策（状态冲突）。"""
        views, _, _, _, sensitive = make_views()
        record = sensitive.create(conversation_uid="c-pending", action_type="order_op",
                                  draft_content={}, current_snapshot={}, now=NOW)
        views.confirm_sensitive(record.uid, operator="a", now=NOW)
        with pytest.raises(SensitiveStateError):
            views.reject_sensitive(record.uid, operator="b", reason="再驳", now=NOW)

    def test_unknown_sensitive_uid(self) -> None:
        """未知草稿 uid → SensitiveNotFoundError。"""
        views, _, _, _, _ = make_views()
        with pytest.raises(SensitiveNotFoundError):
            views.sensitive_diff("no-such-uid")


class TestAuditSearch:
    """审计日志检索 + 哈希链完整性。"""

    def test_search_filters(self) -> None:
        """多维筛选：actor / action_type / source 组合过滤。"""
        views, _, _, audit_log, _ = make_views()
        audit_log.append({"action_type": "human_takeover", "target": "c-ai"},
                         actor="sale-07", source="human", now=NOW)
        audit_log.append({"action_type": "send_reply", "target": "c-ai"},
                         actor="ai", source="ai", now=NOW)
        assert views.search_audit(actor="sale-07")["total"] == 1
        assert views.search_audit(action_type="send_reply")["total"] == 1
        assert views.search_audit(source="human")["total"] == 1
        assert views.search_audit()["total"] == 2

    def test_chain_ok_on_intact_log(self) -> None:
        """未篡改日志 → chain_ok=True。"""
        views, _, _, _, _ = make_views()
        views.audit_log.append({"action_type": "x"}, actor="a", source="ai", now=NOW)
        assert views.search_audit()["chain_ok"] is True

    def test_tamper_detected_by_chain(self) -> None:
        """篡改任一条目内容 → chain_ok=False（哈希链可发现，NFR-SEC-03）。"""
        views, _, _, audit_log, _ = make_views()
        audit_log.append({"action_type": "x", "target": "t1"}, actor="a", source="ai", now=NOW)
        audit_log.append({"action_type": "y", "target": "t2"}, actor="b", source="ai", now=NOW)
        audit_log._entries[0]["target"] = "被篡改"
        assert views.search_audit()["chain_ok"] is False
