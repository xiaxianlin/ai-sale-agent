# 知识产权归甲方所有（COM-IP-01）
"""纠错规则引擎单元测试：格式 / 日期 / 错别字 / 单位 / 自定义规则注册 / 留痕重放（SR-KB-011 §9）。"""
from __future__ import annotations

from services.kb.core.pipeline.cleaning.base import CleanAction, STAGE_CORRECT
from services.kb.core.pipeline.cleaning.correction import (
    Corrector,
    DateFormatRule,
    FormatNormalizeRule,
    TypoMapRule,
    UnitNormalizeRule,
    default_corrector,
)


class TestFormatAndDate:
    """§9 test_typo_unit_format：格式规整与日期统一。"""

    def test_fullwidth_and_whitespace_fold(self) -> None:
        corrector = Corrector([FormatNormalizeRule()])
        actions: list[CleanAction] = []
        out = corrector.run("型号ＡＢ－１２３，纯度  ９８.５％（内检）", actions)
        assert out == "型号AB-123，纯度 98.5%(内检)"  # 全角→半角 + 双空格折叠（，保留中文标点）
        assert [a.rule_id for a in actions] == ["format.normalize"]
        assert actions[0].before == "型号ＡＢ－１２３，纯度  ９８.５％（内检）"
        assert actions[0].after == out

    def test_date_variants_unified(self) -> None:
        corrector = Corrector([DateFormatRule()])
        actions: list[CleanAction] = []
        out = corrector.run("发货日期2026.1.5，截止2026年1月5日，备份2026/01/05", actions)
        assert out == "发货日期2026-01-05，截止2026-01-05，备份2026-01-05"  # 三种写法统一
        assert [a.rule_id for a in actions] == ["format.date"]

    def test_unchanged_text_no_action(self) -> None:
        # 无可纠内容 → 零留痕（避免噪声）
        corrector = default_corrector()
        actions: list[CleanAction] = []
        out = corrector.run("这段文本完全规范，无需任何纠错处理。", actions)
        assert out == "这段文本完全规范，无需任何纠错处理。"
        assert actions == []


class TestTypoMap:
    """错别字映射表：替换正确 + 长词优先 + 逐词留痕。"""

    def test_typo_replacement_with_trace(self) -> None:
        corrector = Corrector([TypoMapRule({"帐号": "账号", "登陆": "登录"})])
        actions: list[CleanAction] = []
        out = corrector.run("请登陆您的帐号后查看帐号余额", actions)
        assert out == "请登录您的账号后查看账号余额"
        recorded = sorted((a.rule_id, a.before, a.after) for a in actions)
        assert recorded == [("typo:帐号", "帐号", "账号"), ("typo:登陆", "登陆", "登录")]
        assert all(a.stage == STAGE_CORRECT for a in actions)

    def test_long_word_priority(self) -> None:
        # 长词优先防子串误替换：应整词替换「美国市场」，而不是先替换子串「美国」
        corrector = Corrector([TypoMapRule({"美国": "USA", "美国市场": "北美市场"})])
        out = corrector.run("我们深耕美国市场多年", [])
        assert out == "我们深耕北美市场多年"
        # 反例验证：短词优先会得到错误结果「USA市场」
        assert "USA市场" not in out


class TestUnitNormalize:
    """单位归一化：数字 + 单位 → 标准单位，逐次出现留痕。"""

    def test_units_normalized(self) -> None:
        corrector = Corrector([UnitNormalizeRule()])
        actions: list[CleanAction] = []
        out = corrector.run("净重5公斤，毛重2.5千克，每箱100克，数量10 PCS，尺寸3厘米", actions)
        assert out == "净重5kg，毛重2.5kg，每箱100g，数量10pcs，尺寸3cm"
        # 逐次留痕：before/after 为「数值+单位」，便于抽样核对（§16.1）
        recorded = [(a.rule_id, a.before, a.after) for a in actions]
        assert ("unit:公斤", "5公斤", "5kg") in recorded
        assert ("unit:PCS", "10 PCS", "10pcs") in recorded
        assert len(recorded) == 5

    def test_unit_without_number_not_matched(self) -> None:
        # 无数字前缀的「公斤」不替换（防普通词汇误伤）
        corrector = Corrector([UnitNormalizeRule()])
        out = corrector.run("按公斤计价，按克零售", [])
        assert out == "按公斤计价，按克零售"


class TestRegisterableRule:
    """规则可注册：自定义规则无需改引擎即可接入并留痕。"""

    def test_register_custom_rule(self) -> None:
        class SoftBreakRule:
            """自定义规则：软回车标记 → 空格（演示注册扩展点）。"""

            rule_id = "format.softbreak"

            def apply(self, text: str, record) -> str:  # type: ignore[no-untyped-def]
                new = text.replace("␤", " ")
                if new != text:
                    record(self.rule_id, "␤", " ")
                return new

        corrector = Corrector()
        corrector.register(SoftBreakRule())
        corrector.register(TypoMapRule({"sicom": "SICOM"}))
        actions: list[CleanAction] = []
        out = corrector.run("sicom␤报价单", actions)
        assert out == "SICOM 报价单"
        assert [a.rule_id for a in actions] == ["format.softbreak", "typo:sicom"]  # 按注册顺序执行


class TestCombinedAndReplay:
    """§9 test_typo_unit_format 组合用例 + test_diff_chain_replay 纠错段重放。"""

    def test_combined_correction_and_replay(self) -> None:
        # 组合输入：全角数字 / 双空格 / 日期 / 错别字 / 单位 各纠一处
        original = "客户帐号于２０２６.１.５登陆，样品重5公斤，金额  １２００元。"
        corrector = default_corrector()
        actions: list[CleanAction] = []
        out = corrector.run(original, actions)
        assert out == "客户账号于2026-01-05登录，样品重5kg，金额 1200元。"
        # 留痕链完整性：stage 全为 correct，seq 严格递增
        assert [a.seq for a in actions] == list(range(1, len(actions) + 1))
        assert all(a.stage == STAGE_CORRECT for a in actions)
        assert all(a.ts for a in actions)
        # 重放：按 seq 顺序应用 (before → after) 可从原文复现纠错后文本（§12.2 抽样核对脚本化）
        replayed = original
        for a in actions:
            replayed = replayed.replace(a.before, a.after)
        assert replayed == out
