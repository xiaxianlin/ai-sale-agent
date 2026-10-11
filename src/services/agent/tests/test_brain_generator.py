# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""回复生成链路单元测试（FR-AG-003 链路②③④⑤ + R-03，SR-AG-003 §6.2/§6.3；迭代 2.2 TDD）。

覆盖（开发规范 §9.1 五类）：
- 等价类：高置信草稿全链（检索→组装→生成→校验→置信度→判定）；FR-KB-030 检索契约；
- 边界类：Token 预算裁剪、近端原文/远端摘要压缩、占位符阶梯价取档；
- 异常类：价格残留硬拦截（R-03 核心）、伪造引用/空引用不得自动发送、占位符悬空、
  LLM 输出不可解析；
- 降级类：知识库零命中零生成（R-03：不得强行生成）、敏感意图不生成、自评关闭权重重分配。

实现方案：docs/07-实现方案/AG/SR-AG-003-AI大脑.md §6.2/§6.3、§9；SRS §17 R-03
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import json
from typing import Any

import pytest

from services.agent.core.brain.confidence import ConfidenceComposer
from services.agent.core.brain.fallback import FallbackGate
from services.agent.core.brain.generator import (
    Customer,
    FakeBrainLlm,
    ReplyGenerator,
    ReplyRequest,
)
from services.agent.core.brain.intent import FakeIntentLlm, IntentClassifier, LlmOutputError

#: 默认检索命中（FR-KB-030 results 形态：chunk_id/score/title/text）
DEFAULT_HITS: list[dict[str, Any]] = [
    {
        "chunk_id": 10241,
        "score": 0.9,
        "title": "ST-3200 参数表",
        "text": "ST-3200 支架采用 304 不锈钢，耐盐雾 1000 小时。",
    },
    {
        "chunk_id": 10242,
        "score": 0.8,
        "title": "报价流程",
        "text": "报价由销售经理按阶梯价表确认后回复。",
    },
]

#: 标准请求（询价消息，带客户档案）
REQ = ReplyRequest(
    message_uid="m-1",
    conversation_uid="c-1",
    account_id=1,
    text="ST-3200 1000pcs price?",
    customer=Customer(uid="u-1", grade="B", profile_tags=("德国", "五金批发"), lang="en"),
)


def make_search(hits: list[dict[str, Any]] | None = None) -> Any:
    """注入式 search_fn（FR-KB-030 契约）并记录调用入参供断言。"""
    calls: list[dict[str, Any]] = []

    def search(query: str, kb_types=None, filters=None, top_k: int = 5) -> dict[str, Any]:
        calls.append(
            {
                "query": query,
                "kb_types": tuple(kb_types or ()),
                "filters": filters,
                "top_k": top_k,
            }
        )
        return {"results": DEFAULT_HITS if hits is None else hits}

    search.calls = calls  # type: ignore[attr-defined]
    return search


def make_generator(
    hits: list[dict[str, Any]] | None = None,
    *,
    script: dict[str, str] | None = None,
    card_fn: Any = None,
    history: list[dict[str, str]] | None = None,
    self_eval_enabled: bool = True,
) -> tuple[ReplyGenerator, FakeBrainLlm, Any]:
    """标准装配：Fake 生成 LLM + 注入检索（返回检索记录器）+ 双阈值门 + 三维置信度。"""
    hist = history if history is not None else [
        {"role": "buyer", "content": "hello, are you there?"},
        {"role": "ai", "content": "Yes, this is Sara from the export team."},
    ]
    llm = FakeBrainLlm(script=script)
    search = make_search(hits)
    generator = ReplyGenerator(
        llm,
        search,
        FallbackGate(),
        ConfidenceComposer(),
        history_fn=lambda uid: hist,
        product_card_fn=card_fn,
        self_eval_enabled=self_eval_enabled,
    )
    return generator, llm, search


def _inquiry_intent() -> Any:
    return IntentClassifier(FakeIntentLlm()).classify(REQ.text)


def test_happy_path_high_confidence_auto_ready() -> None:
    """等价类：命中充分+引用合法 → conf≥T_high，action=auto_send（§16.3 可用率门控路径）。"""
    generator, _, _ = make_generator()
    result = generator.generate(REQ, _inquiry_intent())
    assert result.need_human is False
    assert result.action == "auto_send"
    assert result.reason_code == ""
    assert 0.75 <= result.confidence <= 0.99
    assert set(result.conf_components) == {"retrieval", "self_eval", "fact_check"}


def test_result_carries_knowledge_refs_and_confidence() -> None:
    """等价类：FR-AG-003 原文「生成结果带知识来源与置信度」——refs 回锚 chunk 与得分。"""
    generator, _, _ = make_generator()
    result = generator.generate(REQ, _inquiry_intent())
    ref = result.knowledge_refs[0]
    assert ref["ref"] == "R1"
    assert ref["chunk_id"] == 10241
    assert ref["score"] == 0.9
    assert "[R1]" in result.reply_draft  # 草稿携带引用标记（§7.4 可追溯）


def test_search_fn_fr_kb030_contract() -> None:
    """等价类：检索调用符合 FR-KB-030 契约——query 含槽位 SKU、kb_types 按意图映射、top_k。"""
    generator, _, search = make_generator()
    generator.generate(REQ, _inquiry_intent())
    call = search.calls[0]
    assert "ST-3200" in call["query"]
    assert "product" in call["kb_types"]
    assert call["top_k"] == 5


def test_zero_hit_no_llm_generation() -> None:
    """降级类：零命中 → ZERO_HIT 转人工+缺口记录，LLM 零调用（R-03：不得强行生成）。"""
    generator, llm, _ = make_generator(hits=[])
    result = generator.generate(REQ, _inquiry_intent())
    assert result.need_human is True
    assert result.reason_code == "ZERO_HIT"
    assert result.verdict.gap_record is True
    assert llm.calls == 0


def test_sensitive_intent_never_generates() -> None:
    """降级类：敏感意图 → SENSITIVE 不生成不检索（交 FR-AG-005/009 处置）。"""
    generator, llm, search = make_generator()
    sensitive = IntentClassifier(FakeIntentLlm()).classify("改价为 9 USD 线下支付可以吗")
    result = generator.generate(REQ, sensitive)
    assert result.reason_code == "SENSITIVE"
    assert result.reply_draft == ""
    assert llm.calls == 0
    assert search.calls == []


def test_customer_profile_injected_into_prompt() -> None:
    """等价类：客户档案注入 System Prompt（等级/画像标签/语言）。"""
    generator, llm, _ = make_generator()
    generator.generate(REQ, _inquiry_intent())
    assert "德国" in llm.last_prompt
    assert "五金批发" in llm.last_prompt
    assert "B" in llm.last_prompt


def test_older_history_compressed_to_summary() -> None:
    """边界类：远端历史压缩为摘要块——原文长尾不进 Prompt，近端原文保留。"""
    history = [
        {"role": "buyer", "content": f"OLD-MARKER-{i} tailtoken{i} " + "y" * 60}
        for i in range(20)
    ] + [
        {"role": "buyer", "content": f"RECENT-{i} 近期消息内容 " + "z" * 40}
        for i in range(16)
    ]
    generator, llm, _ = make_generator(history=history)
    generator.generate(REQ, _inquiry_intent())
    assert "【会话历史摘要】" in llm.last_prompt
    assert "OLD-MARKER-0" not in llm.last_prompt          # 远端全文不入 Prompt
    assert "RECENT-0" in llm.last_prompt                   # 近端原文保留
    assert "y" * 60 not in llm.last_prompt                 # 压缩后长尾截断


def test_chunk_budget_trims_long_block() -> None:
    """边界类：知识块超预算截断——块头保留、长尾丢弃（Token 预算裁剪）。"""
    long_hit = {
        "chunk_id": 99,
        "score": 0.95,
        "title": "超长块",
        "text": "HEAD-PART " + "p" * 2000 + " TAIL_END_999",
    }
    generator, llm, _ = make_generator(hits=[long_hit])
    generator.generate(REQ, _inquiry_intent())
    assert "HEAD-PART" in llm.last_prompt
    assert "TAIL_END_999" not in llm.last_prompt


def test_placeholder_fills_ladder_price() -> None:
    """等价类：{{PRICE:SKU:QTY}} 仅由结构化产品库填充（R-03：按数量取阶梯档）。"""
    card = {
        "sku": "ST-3200",
        "ladder": [{"qty": 500, "price": 12.5}, {"qty": 1000, "price": 11.8}],
    }
    script = {
        "【知识片段】": '{"reply_draft": "The price for 1000 pcs is {{PRICE:ST-3200:1000}} [R1].",'
        ' "knowledge_refs": ["R1"], "missing_info": []}'
    }
    generator, _, _ = make_generator(card_fn=lambda sku: card, script=script)
    result = generator.generate(REQ, _inquiry_intent())
    assert "USD 11.8/pc" in result.reply_draft
    assert "{{PRICE" not in result.reply_draft
    assert result.need_human is False  # 填充合法 → 无残留 → 可自动发送


@pytest.mark.parametrize(
    "card_fn",
    [
        pytest.param(lambda sku: None, id="产品卡缺失"),
        pytest.param(
            lambda sku: {"sku": "ST-3200", "ladder": [{"qty": 500, "price": 12.5}]},
            id="数量低于最低档",
        ),
    ],
)
def test_placeholder_unresolved_blocks_auto_send(card_fn: Any) -> None:
    """异常类：产品卡缺失/数量无匹配档 → unresolved → KEY_FIELD_MISSING 转人工（绝不猜数字）。"""
    script = {
        "【知识片段】": '{"reply_draft": "Price is {{PRICE:ST-3200:100}} [R1].",'
        ' "knowledge_refs": ["R1"], "missing_info": []}'
    }
    generator, _, _ = make_generator(card_fn=card_fn, script=script)
    result = generator.generate(REQ, _inquiry_intent())
    assert result.need_human is True
    assert result.reason_code == "KEY_FIELD_MISSING"


def test_price_leak_intercepted() -> None:
    """异常类（R-03 核心）：LLM 自填价格数字（非结构化字段）→ 泄漏 → FACT_CHECK_FAIL 硬拦截。"""
    script = {
        "【知识片段】": '{"reply_draft": "It is only USD 9.5 per piece [R1].",'
        ' "knowledge_refs": ["R1"], "missing_info": []}'
    }
    generator, _, _ = make_generator(script=script)
    result = generator.generate(REQ, _inquiry_intent())
    assert result.need_human is True
    assert result.reason_code == "FACT_CHECK_FAIL"


def test_filled_value_no_false_positive() -> None:
    """边界类：合法填充值（结构化取数）剥除后无残留 → 不误报泄漏，仍可自动发送。"""
    card = {"sku": "ST-3200", "ladder": [{"qty": 100, "price": 11.8}]}
    script = {
        "【知识片段】": '{"reply_draft": "Unit price {{PRICE:ST-3200:100}} [R1], thanks.",'
        ' "knowledge_refs": ["R1"], "missing_info": []}'
    }
    generator, _, _ = make_generator(card_fn=lambda sku: card, script=script)
    result = generator.generate(REQ, _inquiry_intent())
    assert result.need_human is False
    assert "USD 11.8/pc" in result.reply_draft


@pytest.mark.parametrize(
    "refs",
    [
        pytest.param(["R9"], id="伪造引用不在命中集"),
        pytest.param([], id="空引用无有效来源"),
    ],
)
def test_invalid_refs_block_auto_send(refs: list[str]) -> None:
    """异常类：引用校验——无有效引用 → 不允许自动发送（FACT_CHECK_FAIL，§7.4 可追溯红线）。"""
    script = {
        "【知识片段】": '{"reply_draft": "We can supply 304 stainless brackets [R1].",'
        f' "knowledge_refs": {json.dumps(refs)}, "missing_info": []}}'
    }
    generator, _, _ = make_generator(script=script)
    result = generator.generate(REQ, _inquiry_intent())
    assert result.need_human is True
    assert result.reason_code == "FACT_CHECK_FAIL"


def test_self_eval_disabled_redistributes_weight() -> None:
    """降级类：自评关闭 → 权重重分配至检索（self_eval 分量=检索分量）。"""
    generator, _, _ = make_generator(self_eval_enabled=False)
    result = generator.generate(REQ, _inquiry_intent())
    assert result.conf_components["self_eval"] == result.conf_components["retrieval"]
    assert result.need_human is False


def test_llm_unparseable_output_raises() -> None:
    """异常类：生成输出非 JSON → LlmOutputError（端点映射 50002 降级转人工）。"""
    generator, _, _ = make_generator(script={"【知识片段】": "抱歉，无法输出"})
    with pytest.raises(LlmOutputError):
        generator.generate(REQ, _inquiry_intent())
