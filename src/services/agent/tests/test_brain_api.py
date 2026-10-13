# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""AI 大脑 API 集成测试（FR-AG-003/FR-AG-009 端点面，SR-AG-003 §5、SR-AG-009 §5；迭代 2.2 TDD）。

覆盖（开发规范 §9.1 五类）：
- 等价类：POST /api/v1/agent/brain/respond 全链返回决策明细；自动发送经注入 sender 桩；
- 降级类：低置信/零命中/敏感/价格残留 → need_human 路径，发送桩零调用（不得强行生成）；
- 异常类：参数校验 422；LLM 配置缺失 → 502/50002（下游依赖降级，不允许无依据发送）；
- 统计面：GET /api/v1/agent/stats/inquiries 日报表（NFR-PER-04）。

实现方案：docs/07-实现方案/AG/SR-AG-003-AI大脑.md §5、SR-AG-009 §5
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from datetime import datetime

from typing import Any

from fastapi.testclient import TestClient

from services.agent.api.brain_api import SenderStub, create_brain_app
from services.agent.core.brain.confidence import ConfidenceComposer
from services.agent.core.brain.fallback import FallbackGate
from services.agent.core.brain.generator import FakeBrainLlm, ReplyGenerator
from services.agent.core.brain.intent import FakeIntentLlm, IntentClassifier, LlmConfigError
from services.agent.core.brain.stats import DailyInquiryStats

#: 默认检索命中（与生成链路测试同源：0.9/0.8 双命中 → 高置信路径）
HITS: list[dict[str, Any]] = [
    {"chunk_id": 10241, "score": 0.9, "title": "ST-3200 参数表",
     "text": "ST-3200 支架采用 304 不锈钢，耐盐雾 1000 小时。"},
    {"chunk_id": 10242, "score": 0.8, "title": "报价流程",
     "text": "报价由销售经理按阶梯价表确认后回复。"},
]


def build_client(
    hits: list[dict[str, Any]] | None = None,
    *,
    script: dict[str, str] | None = None,
    classifier: Any | None = None,
) -> tuple[TestClient, SenderStub, DailyInquiryStats]:
    """标准装配：Fake 意图/生成 + 注入检索 + 发送桩 + 内存统计。"""
    stats = DailyInquiryStats()
    gen = ReplyGenerator(
        FakeBrainLlm(script=script),
        _make_search(HITS if hits is None else hits),
        FallbackGate(),
        ConfidenceComposer(),
    )
    classifier = classifier or IntentClassifier(FakeIntentLlm())
    sender = SenderStub()
    app = create_brain_app(classifier=classifier, generator=gen, stats=stats, sender=sender)
    return TestClient(app), sender, stats


def _make_search(hits: list[dict[str, Any]]) -> Any:
    def search(query: str, kb_types=None, filters=None, top_k: int = 5) -> dict[str, Any]:
        return {"results": hits}

    return search


def post_respond(client: TestClient, text: str, **extra: Any) -> Any:
    """标准响应请求：trace_id 透传 + 客户档案。"""
    body: dict[str, Any] = {
        "conversation_uid": "c-1",
        "account_id": 1,
        "message_uid": "m-1",
        "text": text,
        "customer": {"uid": "u-1", "grade": "B", "profile_tags": ["德国"], "lang": "en"},
    }
    body.update(extra)
    return client.post(
        "/api/v1/agent/brain/respond", json=body, headers={"X-Trace-Id": "tr-test-1"}
    )


def test_respond_auto_send_via_sender_stub() -> None:
    """等价类：高置信非敏感 → 自动发送恰一次；响应携带全链决策明细（意图/置信度/来源/原因码）。"""
    client, sender, _ = build_client()
    resp = post_respond(client, "ST-3200 1000pcs price?")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sent"] is True and data["need_human"] is False
    assert data["reason_code"] == "" and data["action"] == "auto_send"
    assert len(sender.calls) == 1
    assert sender.calls[0]["conversation_uid"] == "c-1"
    assert data["message_id"] == sender.calls[0]["message_id"]
    for key in ("trace_id", "intent", "confidence", "conf_components", "knowledge_refs"):
        assert key in data
    assert data["trace_id"] == "tr-test-1"  # X-Trace-Id 透传


def test_respond_low_confidence_routes_fallback() -> None:
    """降级类：弱命中低置信 → LOW_CONFIDENCE 转人工；发送桩零调用；待办/挂起态/话术齐备。"""
    weak_hits = [
        {"chunk_id": 1, "score": 0.4, "title": "弱命中一", "text": "部分相关信息一。"},
        {"chunk_id": 2, "score": 0.35, "title": "弱命中二", "text": "部分相关信息二。"},
    ]
    client, sender, _ = build_client(hits=weak_hits)
    resp = post_respond(client, "some vague question about cooperation?")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sent"] is False and data["need_human"] is True
    assert data["reason_code"] == "LOW_CONFIDENCE"
    assert len(sender.calls) == 0  # 草稿绝不自动发出
    assert data["fallback"]["state"] == "pending_confirm"
    assert isinstance(data["fallback"]["todo_id"], int)
    assert "hours" in data["fallback"]["ack_text"]


def test_respond_zero_hit_blocks_send() -> None:
    """降级类：知识库零命中 → ZERO_HIT，不得强行生成（发送桩零调用）。"""
    client, sender, _ = build_client(hits=[])
    resp = post_respond(client, "can you give me a very special custom solution?")
    data = resp.json()
    assert data["reason_code"] == "ZERO_HIT"
    assert data["sent"] is False and len(sender.calls) == 0
    assert data["fallback"]["todo_id"] is not None


def test_respond_sensitive_never_sent() -> None:
    """降级类：敏感意图（红线词快检）→ SENSITIVE 转人工，发送桩零调用。"""
    client, sender, _ = build_client()
    resp = post_respond(client, "改价到 9 USD，线下支付可以吗")
    data = resp.json()
    assert data["intent"]["name"] == "敏感"
    assert data["reason_code"] == "SENSITIVE"
    assert data["sent"] is False and len(sender.calls) == 0


def test_respond_price_leak_never_sent() -> None:
    """降级类（R-03 核心）：LLM 草稿自填价格 → FACT_CHECK_FAIL 硬拦截，绝不自动发送。"""
    script = {
        "【知识片段】": '{"reply_draft": "It is only USD 9.5 per piece [R1].",'
        ' "knowledge_refs": ["R1"], "missing_info": []}'
    }
    client, sender, _ = build_client(script=script)
    resp = post_respond(client, "ST-3200 1000pcs price?")
    data = resp.json()
    assert data["reason_code"] == "FACT_CHECK_FAIL"
    assert data["sent"] is False and len(sender.calls) == 0


def test_stats_inquiries_endpoint() -> None:
    """等价类：GET /api/v1/agent/stats/inquiries 返回构成/占比/达标（NFR-PER-04 报表面）。"""
    client, _, stats = build_client()
    # 固定落账日期（CR P0-1 修复：原用真实时钟 + 硬编码查询日，跨日即时间炸弹）
    day = datetime(2026, 10, 11, 10, 0)
    for i in range(16):
        stats.record_inbound(1, f"c-{i}", at=day)
        stats.record_handled(1, f"c-{i}", "ai_auto" if i < 14 else "fallback", handled_at=day)
    resp = client.get("/api/v1/agent/stats/inquiries", params={"stat_date": "2026-10-11"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["handled_total"] == 16
    row = next(r for r in data["accounts"] if r["account_id"] == 1)
    assert row["cnt_ai_auto"] == 14 and row["cnt_fallback"] == 2
    assert row["meet_floor"] is True


def test_respond_missing_text_422() -> None:
    """异常类：缺 text 参数 → 422 参数校验失败（40001 契约由网关层映射）。"""
    client, _, _ = build_client()
    resp = client.post("/api/v1/agent/brain/respond", json={"conversation_uid": "c-1"})
    assert resp.status_code == 422


def test_respond_llm_config_error_502() -> None:
    """异常类：LLM 未配置 → 502/50002 下游依赖降级；发送桩零调用（不允许无依据发送）。"""

    class ExplodingLlm:
        """构造即代表未配置 DeepSeek 的失败桩。"""

        def complete(self, prompt: str, *, timeout_s: float, max_tokens: int,
                     temperature: float) -> str:
            raise LlmConfigError("未配置环境变量 KB_DEEPSEEK_API_KEY")

    client, sender, _ = build_client(classifier=IntentClassifier(ExplodingLlm()))
    resp = post_respond(client, "ST-3200 1000pcs price?")
    assert resp.status_code == 502
    assert resp.json()["code"] == 50002
    assert len(sender.calls) == 0
