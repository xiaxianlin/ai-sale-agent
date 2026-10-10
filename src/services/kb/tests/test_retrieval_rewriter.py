# 知识产权归甲方所有（COM-IP-01）
"""查询改写单元测试（SR-KB-015 §9）：纠错/别名扩展/意图对齐/LLM 钩子降级与槽位保真。"""
from __future__ import annotations

import asyncio

from services.kb.core.retrieval.rewriter import (
    SOURCE_RULE,
    SOURCE_RULE_BUDGET_SKIP,
    SOURCE_RULE_LLM,
    SOURCE_RULE_LLM_ERROR,
    SOURCE_RULE_LLM_TIMEOUT,
    QueryRewriter,
)
from services.kb.tests.test_retrieval_stubs import run


class TestCorrections:
    """规则① 纠错：NFKC 全半角规整 + 错拼词典（长词优先）。"""

    def test_fullwidth_normalized_with_trace(self) -> None:
        rewriter = QueryRewriter()
        result = run(rewriter.rewrite("ＳＴ３２００ 支架"))
        assert result.rewritten_query == "ST3200 支架"  # 全角 → 半角
        assert any(a.startswith("correct:") and "ST3200" in a for a in result.actions)

    def test_typo_dict_correction(self) -> None:
        rewriter = QueryRewriter(corrections={"stabilzer": "stabilizer"}, intent_templates=[])
        result = run(rewriter.rewrite("stabilzer 报价"))
        assert result.rewritten_query == "stabilizer 报价"
        assert "correct:stabilzer->stabilizer" in result.actions

    def test_registered_correction_after_construction(self) -> None:
        rewriter = QueryRewriter()
        rewriter.register_correction("gimble", "gimbal")
        result = run(rewriter.rewrite("gimble 支架"))
        assert result.rewritten_query == "gimbal 支架"  # 注册即可生效（免改码，NFR-MAI-02）

    def test_clean_query_no_action(self) -> None:
        rewriter = QueryRewriter(corrections={}, intent_templates=[])
        result = run(rewriter.rewrite("ST-3200 规格书"))
        assert result.rewritten_query == "ST-3200 规格书"
        assert result.actions == []
        assert result.slots == {}
        assert result.source == SOURCE_RULE


class TestAliasExpansion:
    """规则② SKU 别名扩展：词典可注册、大小写不敏感、槽位直出。"""

    def test_alias_hit_fills_sku_slot(self) -> None:
        rewriter = QueryRewriter(intent_templates=[])
        rewriter.register_alias("ST32OO", "ST-3200")
        result = run(rewriter.rewrite("ST32OO 支架能扛多重"))
        assert "ST-3200" in result.rewritten_query  # 口语错拼 → 标准 SKU
        assert result.slots.get("sku") == ["ST-3200"]  # 槽位供结构化路 expr
        assert "alias:ST32OO->ST-3200" in result.actions

    def test_alias_case_insensitive(self) -> None:
        rewriter = QueryRewriter(intent_templates=[], sku_aliases={"云台支架": "Gimbal-BR"})
        result = run(rewriter.rewrite("云台支架多少钱"))
        assert "Gimbal-BR" in result.rewritten_query
        assert result.slots["sku"] == ["Gimbal-BR"]

    def test_no_alias_no_slot(self) -> None:
        rewriter = QueryRewriter(intent_templates=[])
        result = run(rewriter.rewrite("普通查询"))
        assert "sku" not in result.slots
        assert not any(a.startswith("alias:") for a in result.actions)


class TestIntentAlignment:
    """规则③ 意图对齐：口语问句 → 标准维度词 + 意图槽位。"""

    def test_load_capacity_alignment(self) -> None:
        rewriter = QueryRewriter(corrections={})
        result = run(rewriter.rewrite("这款支架能扛多重"))
        assert "最大承载" in result.rewritten_query
        assert "load capacity" in result.rewritten_query
        assert result.slots.get("intent") == "load_capacity"
        assert any(a.startswith("intent:") and "最大承载" in a for a in result.actions)

    def test_intent_append_is_idempotent(self) -> None:
        # 已在场的标准词不重复追加（防堆叠），仅补缺失词；槽位仍记录
        rewriter = QueryRewriter(corrections={})
        result = run(rewriter.rewrite("load capacity 参数"))
        assert result.rewritten_query == "load capacity 参数 最大承载"  # 只补缺失词
        assert result.slots.get("intent") == "load_capacity"
        assert any(a.startswith("intent:") for a in result.actions)

    def test_custom_intent_template(self) -> None:
        rewriter = QueryRewriter(corrections={})
        rewriter.register_intent_template(r"防水吗|防水", "防水 waterproof IP 等级", {"intent": "waterproof"})
        result = run(rewriter.rewrite("这个箱子防水吗"))
        assert "waterproof" in result.rewritten_query  # 缺失标准词被补齐
        assert "防水" in result.rewritten_query  # 口语原词保留（稀疏路可命中）
        assert result.slots["intent"] == "waterproof"


class TestLlmHook:
    """可选 LLM 钩子：None 仅规则 / 合并 / 超时降级 / 异常降级 / 预算跳过。"""

    def test_hook_none_rules_only(self) -> None:
        rewriter = QueryRewriter(llm_hook=None)
        result = run(rewriter.rewrite("支架能扛多重"))
        assert result.source == SOURCE_RULE
        assert not any(a.startswith("llm:") for a in result.actions)

    def test_llm_merged_source(self) -> None:
        async def hook(text: str) -> str:
            return f"{text} gimbal bracket"

        rewriter = QueryRewriter(corrections={}, intent_templates=[], llm_hook=hook)
        result = run(rewriter.rewrite("云台"))
        assert result.source == SOURCE_RULE_LLM
        assert "gimbal bracket" in result.rewritten_query
        assert any(a.startswith("llm:") for a in result.actions)

    def test_llm_sync_hook_supported(self) -> None:
        # 同步钩子（maybe_await 鸭子类型兼容）同样可用
        rewriter = QueryRewriter(corrections={}, intent_templates=[], llm_hook=lambda t: f"{t} sync-ok")
        result = run(rewriter.rewrite("云台"))
        assert "sync-ok" in result.rewritten_query

    def test_llm_slot_fidelity(self) -> None:
        # 槽位保真：LLM 改丢 SKU 与数字 → 合并后以规则槽位为准补回
        async def hook(_text: str) -> str:
            return "gimbal bracket specification"  # 不含 ST-3200 与 50

        rewriter = QueryRewriter(
            corrections={}, intent_templates=[], llm_hook=hook, llm_timeout_ms=1000,
        )
        rewriter.register_alias("ST32OO", "ST-3200")
        result = run(rewriter.rewrite("ST32OO 承重 50 公斤"))
        assert "ST-3200" in result.rewritten_query  # SKU 槽位未被 LLM 改写丢失
        assert "50" in result.rewritten_query  # 数字保真（禁编造亦禁丢失）

    def test_llm_timeout_degrades_to_rules(self) -> None:
        async def slow_hook(_text: str) -> str:
            await asyncio.sleep(0.2)
            return "不应被采用"

        rewriter = QueryRewriter(corrections={}, intent_templates=[], llm_hook=slow_hook, llm_timeout_ms=30)
        result = run(rewriter.rewrite("云台"))
        assert result.source == SOURCE_RULE_LLM_TIMEOUT  # 判定点 A：超时规则直出
        assert result.rewritten_query == "云台"
        assert not any(a.startswith("llm:") for a in result.actions)

    def test_llm_error_degrades_to_rules(self) -> None:
        async def broken_hook(_text: str) -> str:
            raise ValueError("LLM 网关异常")

        rewriter = QueryRewriter(corrections={}, intent_templates=[], llm_hook=broken_hook)
        result = run(rewriter.rewrite("云台"))
        assert result.source == SOURCE_RULE_LLM_ERROR
        assert result.rewritten_query == "云台"

    def test_use_llm_false_skips_hook(self) -> None:
        calls: list[str] = []

        async def hook(text: str) -> str:
            calls.append(text)
            return "不应出现"

        rewriter = QueryRewriter(corrections={}, intent_templates=[], llm_hook=hook)
        result = run(rewriter.rewrite("云台", use_llm=False))  # 判定点 A2：预算不足
        assert calls == []  # 钩子不被调用（省下 LLM 段预算）
        assert result.source == SOURCE_RULE_BUDGET_SKIP
        assert result.rewritten_query == "云台"
        # 语义别名入口一致
        alias_result = run(rewriter.rewrite_rules_only("云台"))
        assert alias_result.source == SOURCE_RULE_BUDGET_SKIP


class TestRuleChainOrder:
    """三步规则组合：纠错 → 别名 → 意图，留痕按执行顺序追加。"""

    def test_combined_rule_chain(self) -> None:
        rewriter = QueryRewriter(intent_templates=[
            (r"能扛多重", "最大承载 load capacity", {"intent": "load_capacity"}),
        ])
        rewriter.register_alias("ST32OO", "ST-3200")
        result = run(rewriter.rewrite("ST32OO 能扛多重"))
        kinds = [a.split(":", 1)[0] for a in result.actions]
        assert kinds == ["alias", "intent"]  # 注册顺序执行，可重放审计
        assert result.rewritten_query == "ST-3200 能扛多重 最大承载 load capacity"
        assert result.slots == {"sku": ["ST-3200"], "intent": "load_capacity"}
