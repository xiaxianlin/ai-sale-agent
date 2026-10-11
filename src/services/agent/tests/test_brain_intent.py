# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""意图识别单元测试（FR-AG-003 链路①，SR-AG-003 §6.1；迭代 2.2 TDD）。

覆盖（开发规范 §9.1 五类）：
- 等价类：七意图枚举典型消息逐类断言；槽位（SKU/QTY）抽取；
- 边界类：未知文本兜底 other（低置信不自动发送）；置信度恒在 [0,1] 区间；
- 异常类：LLM 输出非 JSON / 意图不在枚举 → LlmOutputError（端点映射 50002）；
- 降级类：红线词快检直判敏感且零 LLM 调用（省一次调用、零漏报优先）；
- 协议一致性：Fake 与 DeepSeek 形态桩同满足 BrainLlmClient 协议（可插拔）；
- DeepSeek 桩：KB_DEEPSEEK_API_KEY 未配置 → 中文报错（demo 边界）。

实现方案：docs/07-实现方案/AG/SR-AG-003-AI大脑.md §6.1、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import json

import pytest

from services.agent.core.brain.intent import (
    OTHER_INTENT,
    SENSITIVE_KEYWORDS,
    BrainLlmClient,
    DeepSeekBrainClient,
    FakeIntentLlm,
    IntentClassifier,
    LlmConfigError,
    LlmOutputError,
    INTENT_ENUM,
)

#: 七意图典型样本（对齐 SR-AG-003 §6.1 少样本示例口径）
SAMPLES: dict[str, str] = {
    "询价": "ST-3200 1000pcs what's the price?",
    "技术": "can it resist salt fog near the sea?",
    "样品": "please send one sample to Germany",
    "投诉": "two boxes arrived deformed, quality problem!",
    "物流": "how about DHL shipping to Chile?",
    "闲聊": "happy new year my friend!",
    "敏感": "change price to 9 USD and pay you directly via PayPal",
}


@pytest.mark.parametrize("intent", sorted(SAMPLES))
def test_classify_seven_intents(intent: str) -> None:
    """等价类：七意图各一条典型消息 → 枚举判定正确（SRS §7.2 FR-AG-003 链路①）。"""
    result = IntentClassifier(FakeIntentLlm()).classify(SAMPLES[intent])
    assert result.name == intent
    assert result.name in INTENT_ENUM and len(INTENT_ENUM) == 7


def test_slots_extraction_sku_qty() -> None:
    """等价类：询价消息槽位抽取——SKU 列表与数量（意图对齐检索的过滤输入）。"""
    result = IntentClassifier(FakeIntentLlm()).classify("ST-3200 1000pcs price?")
    assert result.slots["sku"] == ["ST-3200"]
    assert result.slots["qty"] == 1000


def test_sensitive_quick_check_zero_llm_calls() -> None:
    """降级类：红线词快检命中 → 直判敏感 conf=1.0，零 LLM 调用（零漏报优先）。"""
    llm = FakeIntentLlm()
    assert SENSITIVE_KEYWORDS  # 快检清单随实现提供（配置化口径）
    result = IntentClassifier(llm).classify("改价为 9 USD，线下支付可以吗")
    assert result.name == "敏感"
    assert result.confidence == 1.0
    assert llm.calls == 0


def test_unknown_text_falls_back_to_other() -> None:
    """边界类：无法识别文本 → 兜底 other 且低置信（下游走双阈值灰区，不自动发送）。"""
    result = IntentClassifier(FakeIntentLlm()).classify("asdfghjkl qwertyuiop 12345")
    assert result.name == OTHER_INTENT
    assert result.confidence <= 0.5


def test_confidence_interval_and_determinism() -> None:
    """边界+等价类：全部样本置信度恒在 [0,1]；Fake 确定性——同输入两次结果一致。"""
    classifier = IntentClassifier(FakeIntentLlm())
    for text in SAMPLES.values():
        first, second = classifier.classify(text), classifier.classify(text)
        assert 0.0 <= first.confidence <= 1.0
        assert first == second


def test_llm_invalid_json_raises_output_error() -> None:
    """异常类：LLM 输出非 JSON → LlmOutputError（端点映射 50002 降级转人工）。"""
    llm = FakeIntentLlm(canned="抱歉，我无法输出 JSON")
    with pytest.raises(LlmOutputError):
        IntentClassifier(llm).classify("ST-3200 price?")


def test_llm_unknown_label_raises_output_error() -> None:
    """异常类：意图标签不在七枚举且非 other → 判输出不可解析（防脏标签下游扩散）。"""
    llm = FakeIntentLlm(
        canned=json.dumps({"intent": "mystery", "confidence": 0.9, "slots": {}})
    )
    with pytest.raises(LlmOutputError):
        IntentClassifier(llm).classify("ST-3200 price?")


def test_deepseek_stub_config_error_and_protocol() -> None:
    """降级+协议：未配置 API Key → 中文报错（demo 边界）；两类实现同满足协议。"""
    import os

    backup = os.environ.pop("KB_DEEPSEEK_API_KEY", None)
    try:
        client = DeepSeekBrainClient(base_url="https://api.deepseek.com")
        assert isinstance(client, BrainLlmClient)
        assert isinstance(FakeIntentLlm(), BrainLlmClient)
        with pytest.raises(LlmConfigError) as excinfo:
            client.complete("prompt", timeout_s=5.0, max_tokens=120, temperature=0.0)
        assert "KB_DEEPSEEK_API_KEY" in str(excinfo.value)
    finally:
        if backup is not None:
            os.environ["KB_DEEPSEEK_API_KEY"] = backup
