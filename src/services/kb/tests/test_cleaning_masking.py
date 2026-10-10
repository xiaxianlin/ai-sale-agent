# 知识产权归甲方所有（COM-IP-01）
"""脱敏引擎单元测试：检测/替换四类动作、双模式、白名单、价格策略、规则版本化
（SR-NFR-SEC-01 §9 test_rule_classify / test_whitelist_company / test_price_policy 等）。"""
from __future__ import annotations

import pytest

from services.kb.core.pipeline.cleaning.base import CleanAction, STAGE_MASK
from services.kb.core.pipeline.cleaning.masking import (
    InMemoryVault,
    MaskRule,
    MaskingEngine,
    RuleSet,
    content_digest,
    default_ruleset,
    passes_luhn,
)


def _engine(vault: InMemoryVault | None = None, ruleset: RuleSet | None = None) -> MaskingEngine:
    """构造被测引擎：默认规则集；传 vault 为可逆模式，不传为不可逆模式。"""
    return MaskingEngine(ruleset or default_ruleset(), vault)


class TestContactMasking:
    """§9 test_mask_email_phone：邮箱 / 电话 / WhatsApp 可逆脱敏（vault_mask）。"""

    def test_email_vault_reversible(self) -> None:
        vault = InMemoryVault()
        engine = _engine(vault)
        actions: list[CleanAction] = []
        result = engine.mask(
            "请联系 john.doe@example.com 谢谢", block_index=3, source_file="a.docx", actions=actions
        )
        assert result.text == "请联系 j***@example.com 谢谢"  # 展示式：首字符 + ***@域名
        assert result.vault_refs == 1 and len(vault) == 1
        vault_id = result.findings[0].vault_id
        assert vault.reveal(vault_id) == "john.doe@example.com"  # 可逆：授权后可还原
        action = actions[0]
        assert action.stage == STAGE_MASK and action.rule_id == "mask.email"
        # 留痕不落明文：before 为保险库占位指针（SR-KB-011 §4）
        assert action.before == f"(vault:{vault_id})" and action.after == "j***@example.com"

    def test_phone_with_and_without_country_code(self) -> None:
        vault = InMemoryVault()
        engine = _engine(vault)
        r1 = engine.mask("电话 +86 13812345678，随时联系")
        assert r1.text == "电话 +86 ****，随时联系"  # 留国码 + ****（§6.5 展示式）
        r2 = engine.mask("电话 13812345678，随时联系")
        assert r2.text == "电话 ****，随时联系"  # 无国码 → 全遮蔽
        assert vault.reveal(r1.findings[0].vault_id) == "+86 13812345678"
        assert vault.reveal(r2.findings[0].vault_id) == "13812345678"

    def test_whatsapp_label_kept_number_masked(self) -> None:
        engine = _engine(InMemoryVault())
        # 上下文规则优先于通用电话：标签保留、号码入 vault
        r1 = engine.mask("WhatsApp: +86 138 1234 5678，随时联系")
        assert r1.text == "WhatsApp: +86 ****，随时联系"
        assert r1.findings[0].rule_id == "mask.whatsapp"
        r2 = engine.mask("whatsapp号13812345678")
        assert r2.text == "whatsapp号****"
        assert r2.findings[0].rule_id == "mask.whatsapp"  # 与电话规则重叠时上下文规则胜出


class TestAddressMasking:
    """地址不可逆遮蔽（partial_mask）：仅留国家/省层级；无法切分 → 全遮蔽 + needs_review。"""

    def test_province_level_kept(self) -> None:
        engine = _engine()
        r = engine.mask("收件地址：广东省深圳市南山区科技园路1号2栋301室，谢谢")
        assert r.text == "收件地址：广东省[地址已遮蔽]，谢谢"  # 仅留省层级（06 §2）
        assert r.needs_review == 0

    def test_autonomous_region_kept(self) -> None:
        engine = _engine()
        r = engine.mask("收货地址：内蒙古自治区呼和浩特市赛罕区大学东街1号")
        assert r.text == "收货地址：内蒙古自治区[地址已遮蔽]"  # 自治区同省层级保留

    def test_no_province_full_mask_needs_review(self) -> None:
        engine = _engine()
        r = engine.mask("仓库地址：杭州市西湖区文一西路969号")
        assert r.text == "仓库地址：[地址已遮蔽]"  # 无法切分省层级 → 全遮蔽
        assert r.needs_review == 1  # 进入人工复核清单（06 §2 抽样复核）

    def test_plain_sentence_not_matched(self) -> None:
        # 无街道要素 / 无门牌数字的普通句子不误杀（防关键信息丢失，§16.1）
        engine = _engine()
        text = "他说他在3楼开会，知道路很远。"
        assert engine.mask(text).text == text


class TestDeleteRules:
    """§9 test_delete_idcard_card：身份证 / 支付卡完全删除，不入保险库（06 §2 硬规则）。"""

    def test_idcard_deleted_no_vault(self) -> None:
        vault = InMemoryVault()
        engine = _engine(vault)
        r = engine.mask("身份证号110101199003077758，请核实")
        assert r.text == "身份证号[mask.idnum已删除]，请核实"
        assert len(vault) == 0  # 完全删除：不留存即不可还原
        assert r.findings[0].action == "delete"

    def test_card_luhn_pass_deleted(self) -> None:
        engine = _engine()
        r = engine.mask("卡号 4111 1111 1111 1111 已扣款")
        assert r.text == "卡号 [mask.pay已删除] 已扣款"
        assert "4111" not in r.text

    def test_card_luhn_fail_kept(self) -> None:
        # Luhn 不通过 → 判为订单号等业务编号，不误杀（数据完整性）
        engine = _engine()
        text = "订单号 4111 1111 1111 1112 请勿删除"
        assert engine.mask(text).text == text

    def test_luhn_helper(self) -> None:
        assert passes_luhn("4111111111111111") is True
        assert passes_luhn("4111111111111112") is False


class TestDualMode:
    """不可逆遮蔽与可逆 vault 占位双模式。"""

    def test_irreversible_mode_without_vault(self) -> None:
        engine = _engine(vault=None)  # 不注入保险库 → 不可逆模式
        actions: list[CleanAction] = []
        r = engine.mask("邮箱 a.b@corp.com", actions=actions)
        assert r.text == "邮箱 a***@corp.com"  # 仍部分遮蔽（更严格方向，非明文降级）
        assert r.vault_refs == 0
        assert actions[0].before == "(masked:mask.email)"  # 留痕为不可逆占位符，无 vault 指针

    def test_reversible_mode_seals_vault(self) -> None:
        vault = InMemoryVault()
        engine = _engine(vault)
        r = engine.mask("邮箱 a.b@corp.com")
        assert r.vault_refs == 1
        assert vault.sealed[0][3] == "mask.email"  # 台账记录 rule_id
        assert vault.sealed[0][2] == 0  # block_index 绑定（AAD 防错位还原的进程内等价物）


class TestWhitelist:
    """§9 test_whitelist_company：三级白名单（显式白名单 + 公司后缀词典）零替换。"""

    def test_explicit_whitelist_and_company_suffix(self) -> None:
        ruleset = RuleSet(
            "mask.rules@test",
            [MaskRule("mask.custom", r"(?:CODE-\d{3}|[\u4e00-\u9fa5]{4,14}有限公司)", "partial_mask")],
            whitelist=("code-123",),
        )
        engine = MaskingEngine(ruleset, InMemoryVault())
        text = "优惠码 CODE-123，供应商深圳市宏发贸易有限公司盖章"
        actions: list[CleanAction] = []
        r = engine.mask(text, actions=actions)
        assert r.text == text  # 两级白名单全部保留，零替换
        assert [a.rule_id for a in actions] == ["whitelist.keep", "whitelist.keep"]
        assert all(f.action == "whitelist_keep" for f in r.findings)
        assert engine.scan_only(text) == []  # 白名单命中不计为残留（E2 闸门口径）


class TestPricePolicy:
    """§9 test_price_policy：同一含价文本，training 遮蔽 / product 保留（06 §2）。"""

    def test_training_masked_product_kept(self) -> None:
        engine = _engine()
        text = "单价 USD 12.5，运费 100元。"
        r_training = engine.mask(text, kb_type="training")
        assert r_training.text.count("[价格已遮蔽]") == 2
        assert "USD 12.5" not in r_training.text and "100元" not in r_training.text
        r_product = engine.mask(text, kb_type="product")
        assert r_product.text == text  # 产品库价格保留


class TestRuleSetVersioning:
    """规则版本化（决策 D3）：Finding 携带版本；坏规则拒绝发布。"""

    def test_version_stamped_on_findings(self) -> None:
        engine = _engine(ruleset=default_ruleset(version="mask.rules@v9"))
        findings = engine.detect("邮箱 a@b.com，电话 +86 13812345678")
        assert findings and all(f.ruleset_version == "mask.rules@v9" for f in findings)

    def test_bad_ruleset_rejected(self) -> None:
        with pytest.raises(ValueError):
            RuleSet("v", [MaskRule("x", "(", "delete")])  # 坏正则：预编译校验拒绝
        with pytest.raises(ValueError):
            RuleSet("v", [MaskRule("x", r"\d+", "leak")])  # 非法动作枚举
        with pytest.raises(ValueError):
            RuleSet("", [])  # 空版本号


class TestDigestAndScan:
    """留痕摘要（决策 D5）与只检测不改写接口。"""

    def test_content_digest_deterministic_and_salted(self) -> None:
        d1 = content_digest("salt", "john@x.com")
        assert d1 == content_digest("salt", "john@x.com")  # 可复算比对
        assert d1 != content_digest("salt2", "john@x.com")  # 带盐：换盐摘要变
        assert len(d1) == 32 and "john" not in d1  # 不泄露原文

    def test_scan_only_detects_without_rewrite(self) -> None:
        engine = _engine()
        text = "邮箱 a@b.com，电话 +86 13812345678"
        assert {f.rule_id for f in engine.scan_only(text)} == {"mask.email", "mask.phone"}
        assert text.startswith("邮箱 a@b.com")  # 原文不被改写
        assert engine.scan_only("正常商务文本，无个人信息。") == []
