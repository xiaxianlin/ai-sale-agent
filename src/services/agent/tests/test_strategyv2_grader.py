# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略 V2 客户分级单元测试（FR-SC-003①，SR-SC-003 §6.1/§9；P4 第二批 TDD）。

五类覆盖（开发规范 §9.1）：等价类（四因子加权评分正常落桶）/ 边界（阈值两侧、
空数据全零）/ 异常（改写缺 reason、非法分级、未知客户）/ 并发（保护期内自动重算
不覆盖人工判断、同值不重复留痕）/ 降级（最近交互缺失按 0 分不抛异常）。
验收口径映射：分级准确性抽检一致率 ≥80%（SRS §16.5 建议项）——score_breakdown
逐因子留痕即抽检解释依据。

实现方案：docs/07-实现方案/SC/SR-SC-003-销售策略引擎.md §6.1、§9
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.agent.core.strategyv2.grader import (
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
    CustomerGrader,
    CustomerProfile,
    GradingError,
    InMemoryCustomerSource,
)

NOW = datetime(2026, 10, 11, 10, 0, 0)


def make_grader(**kw: object) -> CustomerGrader:
    """标准分级器：一个高价值返单客户（应评 A）+ 固定时钟。"""
    profiles = kw.pop("profiles", {
        1: CustomerProfile(
            customer_id=1, deal_amounts=(20000.0, 18000.0, 15000.0),
            last_inbound_at=NOW - timedelta(days=1), channels=("return_order",), grade="D",
        ),
    })
    base: dict = dict(clock=lambda: NOW)
    base.update(kw)
    return CustomerGrader(InMemoryCustomerSource(profiles), **base)  # type: ignore[arg-type]


class TestScoring:
    """等价类：RFM 变体四因子评分与落桶。"""

    def test_score_breakdown_factors_and_weights(self) -> None:
        """四因子得分与权重全部入 breakdown（抽检解释依据，§16.5）。"""
        grader = make_grader()
        breakdown = grader.score_breakdown(grader.source.profile(1))
        assert set(breakdown["factors"]) == {"amount", "freq", "recency", "channel"}
        assert breakdown["weights"] == DEFAULT_WEIGHTS
        assert 0.0 <= breakdown["score"] <= 100.0

    def test_regrade_a_appends_auto_log(self) -> None:
        """高额高频近期返单客户 → A 级：回写 + auto 留痕（批次号 + 明细）。"""
        grader = make_grader()
        change = grader.regrade(1, batch_no="b-20261011")
        assert (change.old_grade, change.new_grade, change.source) == ("D", "A", "auto")
        assert grader.source.profile(1).grade == "A"
        (row,) = grader.grade_log
        assert row.reason == "b-20261011" and row.score is not None and row.score_breakdown

    def test_bucket_boundary_both_sides(self) -> None:
        """边界：阈值两侧（80/60/40 分界恰好与差一分）——≥80 A / ≥60 B / ≥40 C / 其余 D。"""
        bucket = make_grader().bucket
        assert [bucket(80.0), bucket(79.99), bucket(60.0), bucket(59.99),
                bucket(40.0), bucket(39.99), bucket(0.0)] == list("ABBCCDD")

    def test_weights_and_thresholds_injectable(self) -> None:
        """权重/阈值可注入（app_config 免发版调参，NFR-MAI-02）。"""
        grader = make_grader(
            weights={"amount": 0.4, "freq": 0.3, "recency": 0.2, "channel": 0.1},
            thresholds={"A": 70.0, "B": 50.0, "C": 30.0},
        )
        assert grader.thresholds == {"A": 70.0, "B": 50.0, "C": 30.0}
        assert DEFAULT_THRESHOLDS == {"A": 80.0, "B": 60.0, "C": 40.0}


class TestDegraded:
    """降级/边界：空数据与缺失字段（§6.1 归一化下限）。"""

    def test_empty_profile_zero_score_buckets_d(self) -> None:
        """空订单 + 无渠道 + 无交互 → 0 分落 D，永不异常。"""
        grader = make_grader(profiles={
            9: CustomerProfile(customer_id=9, deal_amounts=(), last_inbound_at=None, channels=()),
        })
        change = grader.regrade(9, batch_no="b1")
        assert (change.new_grade, change.score) == ("D", 0.0)

    def test_missing_inbound_and_unknown_channel_degrade(self) -> None:
        """最近交互缺失 → recency=0；未知渠道 → 最低默认分 40（不抬级、不抛异常）。"""
        grader = make_grader(profiles={
            8: CustomerProfile(customer_id=8, deal_amounts=(10000.0,),
                               last_inbound_at=None, channels=("weird_channel",)),
        })
        breakdown = grader.score_breakdown(grader.source.profile(8))
        assert breakdown["factors"]["recency"] == 0.0
        assert breakdown["factors"]["channel"] == pytest.approx(40.0)


class TestManualOverride:
    """异常/并发：人工改写留痕与保护期（§6.1②）。"""

    def test_override_rejects_blank_reason_and_bad_grade(self) -> None:
        """改写缺 reason / 非法分级（E）→ 40001 拒绝。"""
        grader = make_grader()
        with pytest.raises(GradingError) as blank:
            grader.manual_override(1, "A", "  ", actor="s")
        assert blank.value.code == 40001
        with pytest.raises(GradingError):
            grader.manual_override(1, "E", "理由", actor="s")

    def test_override_logs_with_protection_window(self) -> None:
        """人工改写成功：manual 留痕 + 保护期截止 = now+90d + is_protected。"""
        grader = make_grader()
        change = grader.manual_override(1, "B", "主管口径 OI-06 抽检改判", actor="supervisor:1")
        assert (change.source, change.old_grade, change.new_grade) == ("manual", "D", "B")
        assert change.effective_until == NOW + timedelta(days=90)
        assert grader.is_protected(1)

    def test_auto_respects_protection_then_expires(self) -> None:
        """并发：保护期内自动重算不覆盖（manual_protected）；过期（+91d）恢复覆盖。"""
        grader = make_grader()
        grader.manual_override(1, "D", "观察期临时降级", actor="s")     # 人工压到 D
        blocked = grader.regrade(1, batch_no="b2")
        assert blocked.note == "manual_protected"
        assert grader.source.profile(1).grade == "D"
        later = CustomerGrader(grader.source, clock=lambda: NOW + timedelta(days=91))
        change = later.regrade(1, batch_no="b3")
        assert change.note == "" and change.new_grade == "C"            # 交互衰减后重算回 C

    def test_same_grade_regrade_no_duplicate_log(self) -> None:
        """同值重算不重复留痕（幂等，note=unchanged）。"""
        grader = make_grader()
        assert grader.regrade(1, batch_no="b1").new_grade == "A"
        assert grader.regrade(1, batch_no="b2").note == "unchanged"
        assert len(grader.grade_log) == 1

    def test_unknown_customer_and_missing_log_reason(self) -> None:
        """异常：未知客户重算 → 40001（非 500）。"""
        with pytest.raises(GradingError) as ei:
            make_grader().regrade(999, batch_no="b")
        assert ei.value.code == 40001
