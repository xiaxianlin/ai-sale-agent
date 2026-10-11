# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""错峰调度器单元测试（FR-SC-004-②，SR-SC-004 §6.2/§9；Phase 4 TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：28 账号窗口起点黄金比例错开（两两互异=无两账号同窗口，SR §9 验收线）、
  窗口时长保持、当日计划可复现；
- 边界：偏移落在 ±spread 域内、槽位配额恰等（同槽在线 ≤ slot_quota）、
  每账号至少 1 槽（公平性下界）；
- 异常：未登记账号查询拒绝；
- 并发：多线程同时请求窗口计划，结果一致（无状态竞争）；
- 降级：账号数缩容（10 账号 P2 形态）算法同样成立（NFR-SCA-01 参数化）。

验收口径映射：SRS §9.2 FR-SC-004-②（操作时段错峰）、§17 R-06（关联风险）。
实现方案：docs/07-实现方案/SC/SR-SC-004-风控V2.md §6.2、§9
"""

from __future__ import annotations

import datetime as dt
import threading
from datetime import date, datetime, timedelta

import pytest

from services.agent.core.riskv2.stagger import StaggerScheduler

ACCOUNTS = [7000 + s for s in range(1, 29)]
DAY = date(2026, 10, 12)


def make_scheduler(n: int = 28, **kw) -> StaggerScheduler:
    """标准装配：n 账号（默认 28）、基准窗口 08:30-18:30、错峰幅度 ±45min。"""
    return StaggerScheduler(ACCOUNTS[:n], **kw)


# ───────────────────────── 等价类：窗口错开 ─────────────────────────


class TestWindowStagger:
    """28 账号工作窗口起点错开算法（SR-SC-004 §6.2 ①：黄金比例低差异序列）。"""

    def test_28_windows_all_distinct_starts(self) -> None:
        """核心断言：28 账号窗口起点两两互异（无两账号同窗口起点）。"""
        scheduler = make_scheduler()
        starts = [scheduler.window_for(a, day=DAY)[0] for a in ACCOUNTS]
        assert len(set(starts)) == 28

    def test_window_duration_preserved(self) -> None:
        """窗口平移不错切时长：终点-起点恒等于基准窗口时长（10h）。"""
        scheduler = make_scheduler()
        start, end = scheduler.window_for(7007, day=DAY)
        assert end - start == timedelta(hours=10)

    def test_offsets_bounded_and_min_gap_positive(self) -> None:
        """边界：偏移全部落在 ±45min 域内，且 28 个起点最小两两间隔 > 0（无聚堆）。"""
        scheduler = make_scheduler()
        starts = sorted(scheduler.window_for(a, day=DAY)[0] for a in ACCOUNTS)
        for start in starts:
            offset = (start - datetime.combine(DAY, dt.time(8, 30))).total_seconds() / 60.0
            assert -45.0 <= offset <= 45.0
        gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:])]
        assert min(gaps) > 0.0


# ───────────────────────── 槽位配额 ─────────────────────────


class TestSlotQuota:
    """时段槽位配额：全网同槽在线 ≤ slot_quota（SR-SC-004 §6.2 ②）。"""

    def test_per_slot_count_within_quota(self) -> None:
        """等价类：分配后任意 30min 槽在线账号 ≤ 10（quota 默认）。"""
        scheduler = make_scheduler()
        allocation = scheduler.allocate_slots(day=DAY)
        load: dict[int, int] = {}
        for slots in allocation.values():
            for s in slots:
                load[s] = load.get(s, 0) + 1
        assert load and max(load.values()) <= 10

    def test_every_account_gets_at_least_one_slot(self) -> None:
        """边界/公平：每账号至少分配 1 个槽（不允许饿死）。"""
        scheduler = make_scheduler()
        allocation = scheduler.allocate_slots(day=DAY)
        assert set(allocation) == set(ACCOUNTS)
        assert all(len(slots) >= 1 for slots in allocation.values())


# ───────────────────────── 异常 / 并发 / 降级 ─────────────────────────


class TestEdgeAndScale:
    """未登记账号拒绝、并发一致与 P2 形态缩容。"""

    def test_unknown_account_rejected(self) -> None:
        """异常：未登记账号查询窗口 → KeyError（fail fast 防错配）。"""
        scheduler = make_scheduler()
        with pytest.raises(KeyError):
            scheduler.window_for(9999, day=DAY)

    def test_concurrent_plan_consistent(self) -> None:
        """并发：14 线程同时取窗口，结果与串行一致（无状态竞争）。"""
        scheduler = make_scheduler()
        expected = scheduler.plan(day=DAY)
        results: list[list] = []
        lock = threading.Lock()

        def worker() -> None:
            plan = scheduler.plan(day=DAY)
            with lock:
                results.append(plan)

        threads = [threading.Thread(target=worker) for _ in range(14)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(p == expected for p in results)

    def test_allocation_idempotent_and_p2_scale(self) -> None:
        """确定性/参数化：同日两次分配一致；10 账号（P2 形态）同样无同窗口（NFR-SCA-01）。"""
        scheduler = make_scheduler()
        assert scheduler.allocate_slots(day=DAY) == scheduler.allocate_slots(day=DAY)

        """降级/参数化：10 账号（P2 形态）同样满足「无两账号同窗口」（NFR-SCA-01）。"""
        scaled = make_scheduler(10)
        starts = [scaled.window_for(a, day=DAY)[0] for a in ACCOUNTS[:10]]
        assert len(set(starts)) == 10
