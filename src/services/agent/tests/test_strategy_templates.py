# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""跟进模板槽位填充单元测试（FR-AG-004→FR-KB-003，SR-AG-004 §6.3；迭代 2.3 TDD 阶段①用例）。

覆盖（R-03 幻觉防护在跟进侧的落点）：
- 全槽位填充成功；结构化槽优先于自由槽；
- 价格槽仅结构化来源：价格仅出现在自由槽 → 违规（不外发）；
- 槽位缺失 → 不外发残文（text 为空、ok=False、missing 列明）；
- 轮次 → 受控场景标签（词表固定，改词表走版本发布）。

实现方案：docs/07-实现方案/AG/SR-AG-004-销售策略V1.md §6.3、SR-KB-003 §5
"""

from __future__ import annotations

import pytest

from services.agent.core.strategy.templates import (
    ROUND_SCENE,
    render_followup,
    scene_for_round,
)

TEMPLATE = "Dear {name}, our best price for {sku} is {price}. Lead time {lead_time} days."


class TestSlotFilling:
    """槽位填充：结构化来源强制 + 残文防护。"""

    def test_render_all_slots_ok(self) -> None:
        """全槽位齐备 → ok，占位符全部替换。"""
        result = render_followup(
            TEMPLATE,
            structured={"price": "USD 9.9/pc", "lead_time": "25"},
            free={"name": "John", "sku": "ST-3200"},
        )
        assert result.ok
        assert result.text == "Dear John, our best price for ST-3200 is USD 9.9/pc. Lead time 25 days."
        assert result.missing == () and result.violations == ()

    def test_structured_takes_precedence(self) -> None:
        """同槽位双侧供给 → 结构化来源优先（权威源覆盖）。"""
        result = render_followup(
            "price={price}",
            structured={"price": "USD 9.9/pc"},
            free={"price": "free-source"},
        )
        assert result.ok and result.text == "price=USD 9.9/pc"

    def test_price_from_free_source_violation(self) -> None:
        """价格槽仅自由来源 → 违规：不外发（ok=False、text 为空、violation 指 price）。"""
        result = render_followup("best price {price}!", free={"price": "USD 1"})
        assert not result.ok
        assert result.text == ""
        assert "price" in result.violations

    def test_missing_slot_not_sent(self) -> None:
        """槽位缺失 → 不外发残文（text 为空，missing 列明缺槽）。"""
        result = render_followup(TEMPLATE, structured={"price": "USD 9.9/pc"},
                                 free={"name": "John"})
        assert not result.ok
        assert result.text == ""
        assert set(result.missing) == {"sku", "lead_time"}

    def test_custom_structured_required(self) -> None:
        """结构化强制清单配置化：把 lead_time 纳入强制 → 仅自由供给即违规。"""
        result = render_followup("lead {lead_time}", free={"lead_time": "25"},
                                 structured_required=frozenset({"lead_time"}))
        assert not result.ok
        assert "lead_time" in result.violations

    def test_no_placeholder_template(self) -> None:
        """无占位符模板原样渲染（恒 ok）。"""
        result = render_followup("plain text follow-up", free={})
        assert result.ok and result.text == "plain text follow-up"


class TestRoundScene:
    """轮次 → 受控场景标签（SR-KB-003 §6.1 scene_taxonomy）。"""

    @pytest.mark.parametrize("round_no,scene", sorted(ROUND_SCENE.items()))
    def test_scene_mapping(self, round_no: int, scene: str) -> None:
        """1/2/3/4 轮 → 首次跟进/二次跟进/三次跟进/最后唤醒。"""
        assert scene_for_round(round_no) == scene

    def test_unknown_round_rejected(self) -> None:
        """词表外轮次拒绝（防越界编造场景）。"""
        with pytest.raises(ValueError):
            scene_for_round(5)
