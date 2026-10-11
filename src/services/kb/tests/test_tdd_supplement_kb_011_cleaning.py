# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""数据清洗与脱敏（FR-KB-011 / SEC-01 / SR-KB-011）TDD 补齐测试（五类场景补强）。

按 TDD 新规五列审计既有 test_cleaning_* 后补齐：
- 边界：电话位数下限（6 不命中 / 7 命中）、身份证 X 终结符、支付卡 Luhn
  恰好通过/不通过（16 位）——误杀防护边界；
- 空集：空文本与零 PII 文本的 mask() 恒等输出；
- 并发：共享 MaskingEngine 跨线程无串扰（规则集与正则缓存只读复用）；
- 降级：清洗编排层 kb_type 价格策略分派（演练库遮蔽 / 产品库保留，
  既有用例只覆盖引擎级，此处补流水线级贯通）。

KPI 映射：SRS §12.2「数据脱敏 100%」——演练库出库文本零敏感明文（边界处
不漏杀、Luhn 不误杀是 100% 覆盖与可用性的两个端点）。

依据文档：docs/07-实现方案/KB/SR-KB-011-数据清洗.md §6.5、§6.6、§9；
         docs/07-实现方案/NFR/SR-NFR-SEC-01-数据脱敏.md §6.1、§6.2。
"""
from __future__ import annotations

import threading

from services.kb.core.pipeline.cleaning import (
    ParsedBlock,
    build_pipeline,
)
from services.kb.core.pipeline.cleaning.masking import (
    InMemoryVault,
    MaskingEngine,
    default_ruleset,
)


class TestDetectionBoundaries:
    """检测边界（等价类/边界）：位数与校验位端点。"""

    def test_phone_digit_count_boundary(self) -> None:
        """电话位数下限：6 位不命中（等价类外），7 位恰好命中（边界内最小值）。"""
        engine = MaskingEngine(default_ruleset())
        short = engine.mask("单号 123456")
        assert short.findings == [] and short.text == "单号 123456"
        hit = engine.mask("电话 1234567")
        assert [f.rule_id for f in hit.findings] == ["mask.phone"]
        assert "1234567" not in hit.text

    def test_idcard_x_terminator_deleted(self) -> None:
        """身份证 X 终结符：17 数字 + X 命中 delete 规则，全文无明文残留。"""
        engine = MaskingEngine(default_ruleset())
        result = engine.mask("证件号 11010119900307777X 请核对")
        assert [f.rule_id for f in result.findings] == ["mask.idnum"]
        assert "11010119900307777X" not in result.text
        assert "[mask.idnum已删除]" in result.text

    def test_paycard_luhn_boundary_pass_and_fail(self) -> None:
        """支付卡 Luhn 边界：通过 → 按卡 delete；不通过 → 不按卡删除，
        但 16 位数字串仍落入电话兜底规则遮蔽（宁过不漏：Luhn 只决定
        「是否按卡删除」，不决定「是否脱敏」，§12.2 方向安全）。"""
        engine = MaskingEngine(default_ruleset())
        passing = engine.mask("卡号 4111111111111111 有效")  # 经典测试卡号，Luhn 通过
        assert [f.rule_id for f in passing.findings] == ["mask.pay"]
        assert "4111111111111111" not in passing.text
        failing = engine.mask("订单 4111111111111112 已发货")  # 仅末位差 1，Luhn 不通过
        assert [f.rule_id for f in failing.findings] == ["mask.phone"]  # 未按卡删除
        assert "4111111111111112" not in failing.text  # 但明文零残留（过遮蔽安全向）
        assert "****" in failing.text


class TestEmptyAndNoPii:
    """空集：空文本与零 PII 文本输出恒等、零命中。"""

    def test_mask_empty_text(self) -> None:
        """空文本：findings 空、输出空、无复核、无保险库引用（空集恒等）。"""
        engine = MaskingEngine(default_ruleset())
        result = engine.mask("")
        assert result.text == ""
        assert result.findings == []
        assert result.needs_review == 0 and result.vault_refs == 0

    def test_mask_plain_text_untouched(self) -> None:
        """零 PII 商务文本：原文恒等输出（除空格外逐字节一致），零命中零留痕。"""
        engine = MaskingEngine(default_ruleset())
        text = "本批货物的海运周期约为三十天，包装方式为出口标准纸箱。"
        result = engine.mask(text)
        assert result.text == text
        assert result.findings == [] and result.needs_review == 0


class TestConcurrentMasking:
    """并发：共享引擎跨线程脱敏无串扰（规则集只读、模块级正则缓存良性竞态）。"""

    def test_shared_engine_matches_sequential_reference(self) -> None:
        """8 线程 × 20 次 mask 共享同一引擎：结果与单线程基准逐字节一致。"""
        engine = MaskingEngine(default_ruleset(), InMemoryVault())
        samples = [
            "联系人 john.doe@example.com 电话 +86 13800138000",
            "地址：上海市浦东新区世纪大道 100 号 45 楼",
            "无敏感信息的普通产品介绍段落，讨论材质与尺寸参数。",
        ]
        reference = [engine.mask(s).text for s in samples]
        results: list[tuple[int, list[str]]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            barrier.wait()
            local = [engine.mask(s).text for s in samples]
            with lock:
                results.append((index, local))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 8
        for _index, local in results:
            assert local == reference  # 无串扰：与单线程基准完全一致


class TestPipelinePricePolicyByKbType:
    """降级/等价类（流水线级贯通）：价格策略按 kb_type 分派——演练库遮蔽、产品库保留。"""

    def test_training_kb_masks_price_product_kb_keeps(self) -> None:
        """同一含价文本：kb_type=training 出 [价格已遮蔽]；kb_type=product 原文保留。"""
        text = "本型号云台支架的出口单价为 USD 299，批量另有折扣。"
        training_doc = build_pipeline(kb_type="training").run(
            [ParsedBlock(0, "text", text=text)], "演练.docx"
        )
        assert "[价格已遮蔽]" in training_doc.blocks[0].text
        assert "USD 299" not in training_doc.blocks[0].text
        product_doc = build_pipeline(kb_type="product").run(
            [ParsedBlock(0, "text", text=text)], "产品.docx"
        )
        assert "USD 299" in product_doc.blocks[0].text  # 产品库价格保留（06 §2）

    def test_price_mask_recorded_in_trace_chain(self) -> None:
        """演练库价格遮蔽留痕：mask 段动作携带 mask.price 规则与前后对照。"""
        pipe = build_pipeline(kb_type="training")
        doc = pipe.run([ParsedBlock(0, "text", text="报价 EUR 1500 一件，含税。")], "演练.docx")
        price_actions = [
            a for a in doc.blocks[0].actions if a.stage == "mask" and a.rule_id == "mask.price"
        ]
        assert len(price_actions) == 1
        assert "EUR 1500" in price_actions[0].before  # 价格非客户 PII，留痕保留原文便于核对
        assert price_actions[0].after == "[价格已遮蔽]"
