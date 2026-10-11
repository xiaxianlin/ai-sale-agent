# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略 V2 议价引擎单元测试（FR-SC-003③，SR-SC-003 §6.3/§8/§9；P4 第二批 TDD）。

五类覆盖（开发规范 §9.1）：等价类（底线校验 hold/human、让步阶梯三段正常路径）/
边界（目标恰等于底线、缺口恰在阈值、起订量赠品档位）/ 异常（无底线行、floor≥list
脏数据）/ 并发（同会话复用、价格改版快照隔离）/ 降级与红线（AI 不可见全量底线——
出参结构性防泄漏；max(target, floor) 硬约束使破底线在代码上不可达，SRS §7.4）。

实现方案：docs/07-实现方案/SC/SR-SC-003-销售策略引擎.md §6.3、§9
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from services.agent.core.strategyv2.negotiation import (
    GiftRule,
    InMemoryFloorStore,
    NegotiationEngine,
    NegotiationError,
    PriceFloor,
)

NOW = datetime(2026, 10, 11, 10, 0, 0)
SKU = "ST-3200"


def make_store() -> InMemoryFloorStore:
    """标准价格库：list 3.35 / floor 3.10 / 首幅 2% / 次幅 1% / 两档赠品。"""
    return InMemoryFloorStore({SKU: PriceFloor(
        sku=SKU, list_price=3.35, floor_price=3.10,
        first_step_pct=0.02, second_step_pct=0.01,
        gift_rules=(
            GiftRule(min_qty=500, gift_skus=("GIFT-01",), budget_usd=50.0),
            GiftRule(min_qty=2000, gift_skus=("GIFT-02", "GIFT-01"), budget_usd=120.0),
        ),
    )})


def make_engine(**kw: object) -> NegotiationEngine:
    """标准议价引擎（固定时钟；store 可注入）。"""
    store = kw.pop("store", None)
    base: dict = dict(clock=lambda: NOW)
    base.update(kw)
    return NegotiationEngine(store if store is not None else make_store(), **base)  # type: ignore[arg-type]


class TestLadder:
    """等价类/边界：让步阶梯三段（list → -2% → -1% → floor）。"""

    def test_three_step_ladder_exact_amounts(self) -> None:
        """三轮递进压价：让步恰为 3.283 / 3.25 / 3.10，三段封顶不破底线（§9 用例）。"""
        engine = make_engine()
        r1 = engine.on_counter_offer("c1", SKU, 500, customer_target=3.28, current_offer=3.35)
        assert (r1.action, r1.stage) == ("concede", "l1")
        assert r1.allowed_min_price == pytest.approx(3.283, abs=1e-6)
        r2 = engine.on_counter_offer("c1", SKU, 500, customer_target=3.25, current_offer=3.283)
        assert (r2.action, r2.stage) == ("concede", "l2")
        assert r2.allowed_min_price == pytest.approx(3.25, abs=1e-6)
        r3 = engine.on_counter_offer("c1", SKU, 500, customer_target=3.10, current_offer=3.25)
        assert (r3.action, r3.stage) == ("concede", "floor")
        assert r3.allowed_min_price == pytest.approx(3.10, abs=1e-6)

    def test_ladder_caps_and_over_concede_guards(self) -> None:
        """边界组合：已到 floor 不再动；客户目标高于让幅只让到目标；极端参数 floor 硬托底。"""
        engine = make_engine()
        engine.on_counter_offer("c1", SKU, 500, 3.33, 3.35)              # 只让到 3.33
        sess = engine.session_of("c1", SKU)
        assert sess is not None and sess.current_offer == pytest.approx(3.33, abs=1e-6)
        engine.on_counter_offer("c1", SKU, 500, 3.10, 3.33)
        engine.on_counter_offer("c1", SKU, 500, 3.10, 3.25)
        r4 = engine.on_counter_offer("c1", SKU, 500, 3.05, 3.10)         # floor 后再压 → hold
        assert r4.action == "hold_floor"
        assert r4.allowed_min_price == pytest.approx(3.10, abs=1e-6)
        extreme = PriceFloor(sku=SKU, list_price=3.35, floor_price=3.20,
                             first_step_pct=0.05, second_step_pct=0.05)
        engine2 = make_engine(store=InMemoryFloorStore({SKU: extreme}))
        engine2.on_counter_offer("c2", SKU, 500, 3.15, 3.35)             # l1 计算价 3.1825 → 托到 3.20
        r2 = engine2.on_counter_offer("c2", SKU, 500, 3.15, 3.1825)
        assert r2.allowed_min_price >= 3.20 - 1e-9


class TestFloorGuard:
    """等价类/边界：底线校验与转人工缺口。"""

    def test_below_floor_within_gap_holds_with_gift(self) -> None:
        """目标 < 底线但缺口 ≤10% → hold_floor + 赠品建议（§9 用例）。"""
        hint = make_engine().on_counter_offer("c1", SKU, 500, customer_target=3.00, current_offer=3.283)
        assert hint.action == "hold_floor"
        assert hint.talk_track_tag == "议价_坚守底线"
        assert hint.gift_candidates == ("GIFT-01",)

    def test_far_below_floor_and_threshold_injectable(self) -> None:
        """离谱压价（缺口 >10%）→ 转人工；human_gap_pct 可配置且边界两侧行为正确。"""
        engine = make_engine(human_gap_pct=0.05)
        assert engine.on_counter_offer("c1", SKU, 500, 2.50, 3.283).action == "human"
        edge = engine.on_counter_offer("c1", SKU, 500, 2.94, 3.283)      # 3.10*(1-0.05)=2.945 > 2.94
        assert edge.action == "human"
        assert engine.on_counter_offer("c2", SKU, 500, 3.10, 3.35).action == "concede"


class TestGiftRules:
    """等价类/边界：赠品组合规则（按起订量匹配）。"""

    def test_gift_best_tier_or_empty_floor_human(self) -> None:
        """qty=2500 命中最高档；qty=100 无档位 → floor 阶段无赠品转人工（§6.3③）。"""
        engine = make_engine()
        hi = engine.on_counter_offer("c1", SKU, 2500, 3.30, 3.35)
        assert hi.gift_candidates == ("GIFT-02", "GIFT-01")
        engine.on_counter_offer("c2", SKU, 100, 3.12, 3.35)
        engine.on_counter_offer("c2", SKU, 100, 3.12, 3.283)
        lo = engine.on_counter_offer("c2", SKU, 100, 3.12, 3.25)
        assert lo.gift_candidates == () and lo.action == "human"


class TestDegradedAndRedline:
    """降级与红线：无底线行 / AI 不可见全量 / 快照隔离 / 幂等复用。"""

    def test_no_floor_row_goes_human_without_session(self) -> None:
        """SKU 无底线记录 → action=human 不自动报价、不建会话（宁可不谈，不越权报价）。"""
        engine = make_engine()
        hint = engine.on_counter_offer("c1", "UNKNOWN-SKU", 500, 1.0, 2.0)
        assert hint.action == "human"
        assert engine.session_of("c1", "UNKNOWN-SKU") is None

    def test_ai_payload_never_leaks_floor_fields(self) -> None:
        """AI 不可见全量断言：出参仅本阶段可报区间，无底线/挂牌/阶梯字段（§8 红线）。"""
        hint = make_engine().on_counter_offer("c1", SKU, 500, 3.20, 3.35)
        payload = hint.to_ai_payload()
        assert set(payload) <= {"action", "stage", "allowed_min_price",
                                "gift_candidates", "talk_track_tag"}
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("floor_price", "list_price", "first_step_pct", "second_step_pct"):
            assert forbidden not in payload and forbidden not in serialized

    def test_price_change_midflight_uses_snapshot_and_same_session(self) -> None:
        """并发/幂等：价格库改版不影响在途会话（创建时快照）；重复压价复用同会话并逐段留痕。"""
        store = make_store()
        engine = make_engine(store=store)
        engine.on_counter_offer("c1", SKU, 500, 3.28, 3.35)
        first = engine.session_of("c1", SKU)
        store.put(PriceFloor(sku=SKU, list_price=3.50, floor_price=3.30))  # 中途改版
        r2 = engine.on_counter_offer("c1", SKU, 500, 3.20, 3.283)
        assert r2.allowed_min_price == pytest.approx(3.2495, abs=1e-6)      # 仍按旧快照阶梯
        assert engine.session_of("c1", SKU) is first
        assert first is not None and first.floor_price == 3.10
        assert [c["stage"] for c in first.concessions] == ["l1", "l2"]

    def test_on_deal_closes_session_with_audit(self) -> None:
        """达成一致：会话关闭 stage=deal + 审计留痕（改价仍走起草→人工确认红线）。"""
        engine = make_engine()
        engine.on_counter_offer("c1", SKU, 500, 3.28, 3.35)
        engine.on_deal("c1", SKU)
        sess = engine.session_of("c1", SKU)
        assert sess is not None and sess.stage == "deal" and sess.status == "closed"
        assert any(ev["event"] == "bargain_deal" for ev in engine.audit)

    def test_illegal_floor_config_rejected(self) -> None:
        """异常：floor ≥ list 的价格库脏数据 → 构造即拒绝（应用层校验）。"""
        with pytest.raises(NegotiationError):
            PriceFloor(sku=SKU, list_price=3.00, floor_price=3.10)
