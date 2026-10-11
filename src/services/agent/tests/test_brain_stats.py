# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""日处理询盘量统计单元测试（NFR-PER-04，SR-NFR-PER-04 §6.1-§6.4；迭代 2.2 TDD）。

覆盖（开发规范 §9.1 五类）：
- 等价类：三终态（ai_auto/human_transfer/fallback）均计 1、构成占比、达标判定；
- 边界类：meet_floor 恰 14/15 两侧；跨日换键重计；response_sec 联动记录；
- 异常类：无当日买家入站 → 拒绝计数（防自发刷量）；
- 并发类：同会话 20 线程并发恰计 1、异会话 50 线程并发无超计（原子计数）；
- 降级类：小时快照对账——计数漂移以事件表为准回修并告警；未达下限归因分类
  （traffic_low/fallback_excess/human_backlog/mixed）。

实现方案：docs/07-实现方案/NFR/SR-NFR-PER-04-日处理询盘量.md §6.1-§6.4、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from services.agent.core.brain.stats import DailyInquiryStats

#: Asia/Shanghai 时区（口径：自然日以上海时区为准，SR-NFR-PER-04 §6.1）
SH = timezone(timedelta(hours=8))
DAY1 = datetime(2026, 10, 11, 9, 0, 0, tzinfo=SH)
DAY2 = datetime(2026, 10, 12, 9, 0, 0, tzinfo=SH)


class FakeNow:
    """可拨钟：record_handled 的统计时钟注入。"""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def make_stats(start: datetime = DAY1, **kwargs: object) -> tuple[DailyInquiryStats, FakeNow]:
    clock = FakeNow(start)
    stats = DailyInquiryStats(clock=clock, **kwargs)  # type: ignore[arg-type]
    return stats, clock


def test_three_process_types_all_counted() -> None:
    """等价类：三终态各计 1 条（口径字段化），response_sec 随事件记录（PER-03 联动）。"""
    for idx, process_type in enumerate(("ai_auto", "human_transfer", "fallback")):
        stats, clock = make_stats()
        stats.record_inbound(1, f"c-{idx}", at=clock.now)
        clock.now = clock.now + timedelta(seconds=30)
        assert stats.record_handled(1, f"c-{idx}", process_type) is True
        counters = stats.counters(1, DAY1.date())
        assert counters["total"] == 1 and counters[process_type] == 1
        event = stats.event_of(1, f"c-{idx}", DAY1.date())
        assert event is not None and event.response_sec == 30.0


def test_same_conv_duplicate_terminal_rejected() -> None:
    """并发/幂等类：同会话当日第二类终态到达 → 拒绝（口径=首终态，去重防刷）。"""
    stats, clock = make_stats()
    stats.record_inbound(1, "c-1", at=clock.now)
    assert stats.record_handled(1, "c-1", "ai_auto") is True
    assert stats.record_handled(1, "c-1", "fallback") is False
    assert stats.counters(1, DAY1.date())["total"] == 1
    event = stats.event_of(1, "c-1", DAY1.date())
    assert event is not None and event.process_type == "ai_auto"


def test_next_day_counts_new_round() -> None:
    """边界类：跨日规则——次日买家新消息并完成处理 → 次日计 1（dedup 键按日换键）。"""
    stats, clock = make_stats()
    stats.record_inbound(1, "c-1", at=clock.now)
    stats.record_handled(1, "c-1", "ai_auto")
    clock.now = DAY2
    stats.record_inbound(1, "c-1", at=clock.now)
    assert stats.record_handled(1, "c-1", "fallback") is True
    assert stats.counters(1, DAY1.date())["total"] == 1
    assert stats.counters(1, DAY2.date())["total"] == 1


def test_no_inbound_rejected() -> None:
    """异常类：当日无买家入站消息即达终态 → 拒绝计数（杜绝系统自发行为刷量）。"""
    stats, _ = make_stats()
    assert stats.record_handled(1, "c-ghost", "ai_auto") is False
    assert stats.counters(1, DAY1.date())["total"] == 0


def test_concurrent_atomic_counting() -> None:
    """并发类：同会话 20 线程并发埋点恰计 1；异会话 50 线程并发无超计（原子计数）。"""
    stats, clock = make_stats()
    stats.record_inbound(1, "c-same", at=clock.now)
    for i in range(50):
        stats.record_inbound(2, f"c-dist-{i}", at=clock.now)
    successes_same: list[bool] = []
    successes_dist: list[bool] = []
    lock = threading.Lock()

    def worker_same() -> None:
        ok = stats.record_handled(1, "c-same", "ai_auto")
        with lock:
            successes_same.append(ok)

    def worker_dist(i: int) -> None:
        ok = stats.record_handled(2, f"c-dist-{i}", "ai_auto")
        with lock:
            successes_dist.append(ok)

    threads = [threading.Thread(target=worker_same) for _ in range(20)]
    threads += [threading.Thread(target=worker_dist, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(successes_same) == 1
    assert stats.counters(1, DAY1.date())["total"] == 1
    assert sum(successes_dist) == 50
    assert stats.counters(2, DAY1.date())["total"] == 50


def test_composition_ratios() -> None:
    """等价类：构成占比字段化——8/4/3 之比，达标（≥15）。"""
    stats, clock = make_stats()
    for i in range(8):
        stats.record_inbound(1, f"ca-{i}", at=clock.now)
        stats.record_handled(1, f"ca-{i}", "ai_auto")
    for i in range(4):
        stats.record_inbound(1, f"ch-{i}", at=clock.now)
        stats.record_handled(1, f"ch-{i}", "human_transfer")
    for i in range(3):
        stats.record_inbound(1, f"cf-{i}", at=clock.now)
        stats.record_handled(1, f"cf-{i}", "fallback")
    comp = stats.composition(1, DAY1.date())
    assert comp["handled_total"] == 15
    assert comp["cnt_ai_auto"] == 8 and comp["cnt_human_transfer"] == 4 and comp["cnt_fallback"] == 3
    assert comp["ratios"] == {"ai_auto": 0.533, "human_transfer": 0.267, "fallback": 0.2}
    assert comp["meet_floor"] is True


@pytest.mark.parametrize(
    "handled,expect",
    [
        pytest.param(14, False, id="恰差一条不达标"),
        pytest.param(15, True, id="恰达下限达标"),
    ],
)
def test_meet_floor_boundary(handled: int, expect: bool) -> None:
    """边界类：meet_floor 恰在 14/15 两侧（下限 15 = §16.3 验收线）。"""
    stats, clock = make_stats()
    for i in range(handled):
        stats.record_inbound(1, f"c-{i}", at=clock.now)
        stats.record_handled(1, f"c-{i}", "ai_auto")
    assert stats.composition(1, DAY1.date())["meet_floor"] is expect


def test_snapshot_repairs_counter_drift() -> None:
    """降级类：小时快照对账——Redis 镜像漂移以事件表（PG 事实源）回修并告警 data_drift。"""
    stats, clock = make_stats()
    for i in range(3):
        stats.record_inbound(1, f"c-{i}", at=clock.now)
        stats.record_handled(1, f"c-{i}", "ai_auto")
    stats.override_counter(1, DAY1.date(), total=1)  # 模拟 Redis 少计（宕机丢数）
    rows = stats.snapshot(DAY1.date())
    row = next(r for r in rows if r["account_id"] == 1)
    assert row["handled_total"] == 3 and row["drift"] is True
    assert stats.counters(1, DAY1.date())["total"] == 3  # 已回修
    assert any(a["type"] == "data_drift" for a in stats.alerts)


@pytest.mark.parametrize(
    "setup,kwargs,expect_factor",
    [
        pytest.param("traffic_low", {}, "traffic_low", id="平台侧流量不足"),
        pytest.param("fallback_excess", {}, "fallback_excess", id="兜底占比超阈"),
        pytest.param("human_backlog", {"human_pending": 6}, "human_backlog", id="人工积压"),
    ],
)
def test_attribution_factors(setup: str, kwargs: dict, expect_factor: str) -> None:
    """降级类：未达下限归因分类——平台侧/系统侧因素逐项命中（可提升闭环）。"""
    stats, clock = make_stats()
    if setup == "traffic_low":
        for i in range(10):  # 接入 10 < 15，且无处理事件
            stats.record_inbound(1, f"c-{i}", at=clock.now)
    elif setup == "fallback_excess":
        for i in range(10):
            stats.record_inbound(1, f"c-{i}", at=clock.now)
            stats.record_handled(1, f"c-{i}", "fallback")  # 兜底占比 100% > 40%
    else:  # human_backlog：处理量不足且人工挂起 6 > 5
        for i in range(10):
            stats.record_inbound(1, f"c-{i}", at=clock.now)
        stats.record_handled(1, "c-0", "human_transfer")
    result = stats.attribution(1, DAY1.date(), **kwargs)
    assert expect_factor in result.factors
    assert result.shortfall_code != ""


def test_attribution_mixed_when_multiple_factors() -> None:
    """降级类：多因命中 → shortfall_code=mixed（归因明细列全部命中项）。"""
    stats, clock = make_stats()
    for i in range(10):
        stats.record_inbound(1, f"c-{i}", at=clock.now)
        stats.record_handled(1, f"c-{i}", "fallback")  # 流量不足 + 兜底占比超阈
    result = stats.attribution(1, DAY1.date())
    assert result.shortfall_code == "mixed"
    assert set(result.factors) == {"traffic_low", "fallback_excess"}


def test_daily_check_rows_and_below_floor_alert() -> None:
    """降级类：日终核查——达标账号码空、未达标账号落归因码并产生 stat_below_floor 告警。"""
    stats, clock = make_stats()
    for i in range(15):  # 账号 1 达标
        stats.record_inbound(1, f"a-{i}", at=clock.now)
        stats.record_handled(1, f"a-{i}", "ai_auto")
    for i in range(10):  # 账号 2：接入 10 < 15，未达标
        stats.record_inbound(2, f"b-{i}", at=clock.now)
    rows = stats.daily_check(DAY1.date())
    by_acct = {r["account_id"]: r for r in rows}
    assert by_acct[1]["shortfall_code"] == "" and by_acct[1]["meet_floor"] is True
    assert by_acct[2]["shortfall_code"] == "traffic_low"
    assert any(a["type"] == "stat_below_floor" and a["account_id"] == 2 for a in stats.alerts)

