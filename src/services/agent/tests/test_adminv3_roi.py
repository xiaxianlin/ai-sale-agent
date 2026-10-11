# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V3 ROI 统计单元测试（FR-SC-005，SR-SC-005 §6.4/§9；P4 第二批 TDD）。

五类覆盖（开发规范 §9.1）：等价类（账号级四指标与整体视图口径映射）/ 边界（45s
达标恰好边界、零询盘除零保护）/ 异常（非法窗口 from>to、无数据账号）/ 幂等（预
聚合同日重跑覆盖写不漂移；同会话重复登记不重复计数）/ 降级（空窗口零值 + 空趋势）。
验收口径映射：数据统计（SRS §16.5）——§9「预聚合对账」= metric_daily 四指标与
独立手工口径对账一致。

实现方案：docs/07-实现方案/SC/SR-SC-005-管理后台V3.md §6.4、§9
"""

from __future__ import annotations

from datetime import date

import pytest

from services.agent.core.adminv3.roi import InquiryFact, RoiStats

D1 = date(2026, 10, 10)
D2 = date(2026, 10, 11)


def make_stats() -> RoiStats:
    """标准 ROI 统计器（空账本）。"""
    return RoiStats()


def seed_two_days(stats: RoiStats) -> None:
    """两日账本：账号 1（D1 10 询盘 8 回复 7 达标 2 接管 + 确认 50/补录 450 美元）。"""
    for i in range(10):
        stats.record(InquiryFact(
            account_id=1, conversation_id=f"c1-{i}", stat_date=D1,
            replied=i < 8, reply_sec=30.0 if i < 7 else 60.0,
            human_touched=i < 2, gmv_confirmed=50.0 if i == 0 else 0.0))
    stats.record_gmv_manual(1, D1, 450.0)


class TestAccountView:
    """等价类：账号级口径（metric_daily 视图字段）。"""

    def test_four_metrics_match_manual_accounting(self) -> None:
        """对账：询盘 10 / 回复率 0.8 / 45s 达标 7 / 介入率 0.2 / 成交额 500（§9 用例）。"""
        stats = make_stats()
        seed_two_days(stats)
        stats.rollup_day(D1)
        view = stats.account_view(1, D1, D1)
        assert (view["inquiry_cnt"], view["reply_cnt"]) == (10, 8)
        assert view["reply_rate"] == pytest.approx(0.8)
        assert (view["reply_within45s"], view["human_touch_cnt"]) == (7, 2)
        assert view["human_touch_rate"] == pytest.approx(0.2)
        assert view["gmv_total"] == pytest.approx(500.0)
        assert view["gmv_manual"] == pytest.approx(450.0)

    def test_boundary_and_degraded_views(self) -> None:
        """边界/降级：45.0s 恰好达标（≤45s）；零询盘比率为 0 不抛除零；无数据账号 → None。"""
        stats = make_stats()
        stats.record(InquiryFact(account_id=2, conversation_id="c2", stat_date=D1,
                                 replied=True, reply_sec=45.0))
        stats.rollup_day(D1)
        assert stats.account_view(2, D1, D1)["reply_within45s"] == 1
        assert stats.account_view(3, D2, D2)["reply_rate"] == 0.0
        assert stats.account_view(99, D1, D2) is None

    def test_overview_totals_trend_ranking(self) -> None:
        """整体视图：totals 汇总、trend 按日、ranking 按成交额降序（账号对比口径）。"""
        stats = make_stats()
        seed_two_days(stats)
        for i in range(4):
            stats.record(InquiryFact(account_id=2, conversation_id=f"c2-{i}", stat_date=D1,
                                     replied=i < 3, reply_sec=20.0, gmv_confirmed=100.0))
        stats.rollup_day(D1)
        overview = stats.overview(D1, D1)
        assert overview["totals"]["inquiry_cnt"] == 14
        assert overview["totals"]["gmv_total"] == pytest.approx(900.0)
        assert [t["stat_date"] for t in overview["trend"]] == [D1.isoformat()]
        assert overview["ranking"][0]["account_id"] == 1                 # 500 > 400

    def test_empty_window_and_invalid_window(self) -> None:
        """降级/异常：空窗口 → 零值 + 空趋势；from > to → 拒绝。"""
        overview = make_stats().overview(D1, D2)
        assert overview["trend"] == [] and overview["totals"]["inquiry_cnt"] == 0
        with pytest.raises(ValueError):
            make_stats().overview(D2, D1)


class TestRollupIdempotency:
    """并发/幂等：预聚合幂等可重跑（§6.4 UPSERT 口径）。"""

    def test_rerun_same_day_no_duplicate_no_drift(self) -> None:
        """同日重跑：行数不增、指标不变（补录/修正后无脑重跑，§9 用例）。"""
        stats = make_stats()
        seed_two_days(stats)
        first = stats.rollup_day(D1)
        again = stats.rollup_day(D1)
        assert len(stats.rows) == len(first) == len(again)
        assert stats.account_view(1, D1, D1)["reply_rate"] == pytest.approx(0.8)

    def test_record_upsert_and_multi_day_window(self) -> None:
        """幂等：同会话重复登记覆盖写；多日窗口累加（11 询盘 / 700 美元）。"""
        stats = make_stats()
        seed_two_days(stats)
        fact = InquiryFact(account_id=1, conversation_id="c1-x", stat_date=D2,
                           replied=True, reply_sec=10.0, gmv_confirmed=200.0)
        stats.record(fact)
        stats.record(fact)
        stats.rollup_day(D1)
        stats.rollup_day(D2)
        view = stats.account_view(1, D1, D2)
        assert view["inquiry_cnt"] == 11 and view["gmv_total"] == pytest.approx(700.0)
