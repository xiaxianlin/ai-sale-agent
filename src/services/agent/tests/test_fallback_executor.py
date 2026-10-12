# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""转人工执行编排单元测试（FR-AG-009，SR-AG-009 §6.2/§6.3/§6.6；迭代 2.2 TDD）。

覆盖（开发规范 §9.1 五类）：
- 等价类：状态迁移 → 兜底话术（模板填充，不经 LLM）→ 告警事件 → 待办入队 四步编排；
- 异常类：告警通知失败 → 编排不中断（best-effort，步骤级容错）；
- 并发/幂等类：同幂等键重复执行 → 待办仅一条；同会话去抖 → 话术只发一次；
- 降级类：知识缺口按判定条件选择性记录；监控计数随原因码累加；
- **架构断言**：兜底执行器不持有 send 能力——依赖面无发送回调，
  兜底路径任何出站事件不携带 LLM 草稿（「不得强行生成」的结构保证）。

实现方案：docs/07-实现方案/AG/SR-AG-009-低置信度兜底.md §6.2/§6.3、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

import pytest

from services.agent.core.brain.fallback import (
    FallbackContext,
    FallbackDeps,
    FallbackVerdict,
    InMemoryAckDedup,
    InMemoryTodoQueue,
    execute_fallback,
    render_ack,
)

#: 判定样例：低置信灰区（不带缺口）
VERDICT = FallbackVerdict(
    need_human=True, action="human_review", reason_code="LOW_CONFIDENCE",
    gap_record=False, confidence=0.6,
)
#: 上下文样例：草稿内容带唯一标记，供「草稿不出兜底路径」断言
CTX = FallbackContext(
    conversation_uid="c-9", message_uid="m-9", account_id=1, intent="询价",
    query="ST-3200 price?", reply_draft="DRAFT-CONTENT-MARKER", confidence=0.6,
)


class Bus:
    """记录型总线：捕获全部回调出站事件（state/alert/dedup/monitor/gap）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def push(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))


def make_deps(
    bus: Bus | None = None,
    *,
    dedup: InMemoryAckDedup | None = None,
    notify: Any = None,
    gap_record: bool = False,
) -> tuple[FallbackDeps, Bus, InMemoryTodoQueue]:
    """标准依赖装配：全部回调记录到 Bus（notify 可注入抛异常实现）。"""
    bus = bus or Bus()
    queue = InMemoryTodoQueue()

    def state_transition(uid: str, to_state: str, actor: str, reason: str) -> dict[str, str]:
        payload = {"uid": uid, "to": to_state, "actor": actor, "reason": reason}
        bus.push("state", payload)
        return payload

    def notify_fn(payload: dict[str, Any]) -> None:
        bus.push("alert", payload)
        if notify is not None:
            notify(payload)

    def gap_recorder(query_digest: str, query_text: str, intent: str) -> None:
        bus.push("gap", {"query_digest": query_digest, "query_text": query_text, "intent": intent})

    def monitor(reason_code: str) -> None:
        bus.push("monitor", {"reason_code": reason_code})

    dedup = dedup or InMemoryAckDedup()
    deps = FallbackDeps(
        state_transition=state_transition,
        notify=notify_fn,
        pending_queue=queue,
        ack_dedup=dedup.first_seen,
        gap_recorder=gap_recorder,
        monitor=monitor,
    )
    return deps, bus, queue


def test_state_transition_to_pending_confirm() -> None:
    """等价类：迁移 pending_confirm，actor=system:fr_ag_009，reason=原因码（FR-AG-008 衔接）。"""
    deps, bus, _ = make_deps()
    execute_fallback(VERDICT, CTX, deps)
    state_events = [p for kind, p in bus.events if kind == "state"]
    assert state_events == [{
        "uid": "c-9", "to": "pending_confirm",
        "actor": "system:fr_ag_009", "reason": "LOW_CONFIDENCE",
    }]


def test_ack_template_filled_without_llm() -> None:
    """等价类：兜底话术=模板占位符填充（承诺时限写入），依赖面无 LLM（物理无编造来源）。"""
    deps, _, _ = make_deps()
    outcome = execute_fallback(VERDICT, CTX, deps)
    assert outcome.ack_sent is True
    assert "4" in outcome.ack_text  # 默认承诺 4 小时
    assert "within 4 hours" in outcome.ack_text
    assert render_ack("答复发货进度需 {promise_hours} 小时", promise_hours=2) == \
        "答复发货进度需 2 小时"
    assert not any("llm" in f.name for f in dataclasses.fields(FallbackDeps))


def test_ack_dedup_second_execute_not_resent() -> None:
    """并发/幂等类：同会话去抖窗口内二次兜底 → 话术不再发（防噪声）。"""
    dedup = InMemoryAckDedup()
    deps1, _, _ = make_deps(dedup=dedup)
    deps2, _, _ = make_deps(dedup=dedup)
    first = execute_fallback(VERDICT, CTX, deps1)
    second = execute_fallback(VERDICT, CTX, deps2)
    assert first.ack_sent is True
    assert second.ack_sent is False
    assert second.ack_text == ""


def test_alert_event_emitted_with_reason() -> None:
    """等价类：告警事件 level=high、type=conf_fallback、正文含原因码与置信度（FR-AG-007）。"""
    deps, bus, _ = make_deps()
    outcome = execute_fallback(VERDICT, CTX, deps)
    assert outcome.alert_emitted is True
    alerts = [p for kind, p in bus.events if kind == "alert"]
    assert alerts and alerts[0]["level"] == "high"
    assert alerts[0]["type"] == "conf_fallback"
    assert "LOW_CONFIDENCE" in alerts[0]["body"]
    # 兜底率监控同链累加（知识库退化信号，联动 FR-KB-016）
    monitors = [p for kind, p in bus.events if kind == "monitor"]
    assert monitors == [{"reason_code": "LOW_CONFIDENCE"}]


def test_todo_enqueue_idempotent_key() -> None:
    """幂等重放（CR P2-a 正名）：幂等键 {conv}:{msg}:conf_fallback——同一 verdict
    顺序重放两次，待办仅一条、ID 稳定。非真并发用例：待办队列是内存 list 无锁，
    双线程并发 execute 会真双入队（引入 flaky）；重放幂等由确定性顺序重放钉死。"""
    deps, _, queue = make_deps()
    first = execute_fallback(VERDICT, CTX, deps)
    second = execute_fallback(VERDICT, CTX, deps)
    assert first.todo_id == second.todo_id
    assert len(queue.items) == 1
    assert queue.items[0]["idempotent_key"] == "c-9:m-9:conf_fallback"
    assert queue.items[0]["draft"] == "DRAFT-CONTENT-MARKER"  # 草稿仅入待办供人工参考


@pytest.mark.parametrize("gap", [True, False])
def test_gap_record_conditional(gap: bool) -> None:
    """降级类：缺口记录严格随 verdict.gap_record（ZERO_HIT/低置信记，灰区不记）。"""
    deps, bus, _ = make_deps()
    verdict = FallbackVerdict(True, "human_review_gap", "LOW_CONFIDENCE", gap, 0.6)
    outcome = execute_fallback(verdict, CTX, deps)
    gap_events = [p for kind, p in bus.events if kind == "gap"]
    assert outcome.gap_recorded is gap
    assert len(gap_events) == (1 if gap else 0)



def test_notify_failure_does_not_break_orchestration() -> None:
    """异常类：告警通知抛异常 → 记入 errors、告警未发出，但状态/话术/待办照常完成。"""
    deps, bus, queue = make_deps(notify=RuntimeError("钉钉不可用"))
    outcome = execute_fallback(VERDICT, CTX, deps)
    assert outcome.alert_emitted is False
    assert outcome.todo_id is not None and queue.items  # 后续步骤未被中断
    assert any("notify" in err for err in outcome.errors)


def test_architecture_no_send_capability() -> None:
    """**架构断言**：兜底执行器不持有 send 能力——依赖字段与函数签名均无发送回调。"""
    field_names = {f.name for f in dataclasses.fields(FallbackDeps)}
    assert not any("send" in name for name in field_names), field_names
    param_names = set(inspect.signature(execute_fallback).parameters)
    assert not any("send" in name for name in param_names), param_names


def test_draft_never_dispatched_on_fallback_path() -> None:
    """**架构断言（行为侧）**：兜底路径全部出站事件（状态/告警/去抖/监控/缺口）
    均不携带 LLM 草稿——草稿仅入待办 payload 供人工参考，物理上无草稿发送调用。"""
    deps, bus, queue = make_deps()
    execute_fallback(VERDICT, CTX, deps)
    for kind, payload in bus.events:
        assert "draft" not in payload, (kind, payload)
        assert "DRAFT-CONTENT-MARKER" not in str(payload), (kind, payload)
    # 待办队列是存储不是发送：草稿仅在此处对人工可见
    assert queue.items[0]["draft"] == "DRAFT-CONTENT-MARKER"
