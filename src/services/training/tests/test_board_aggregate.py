# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""主管看板预聚合用例（FR-TR-006，SR-TR-006 §9；TDD 阶段①，迭代 1.3）。

五类场景落位：
- 等价类/边界：来源映射（学习/演练/考试/雷达逐列核对）、空数据日补零、
  部分学员无事件补零、聚合日界（不串日）、雷达快照取当日或最近；
- 并发/幂等：同日重算覆盖重放（ON CONFLICT 语义，行数不变值相同）。
"""
from __future__ import annotations

from datetime import date

from services.training.core.board.aggregate import (
    DailyStat,
    DailyStatsStore,
    DrillEvent,
    ExamEvent,
    LearningEvent,
    RadarSnapshot,
    TeamStatsAggregator,
    TraineeProfile,
)

from .test_board_fakes import FakeBoardSource

D1 = date(2026, 10, 9)
D2 = date(2026, 10, 10)  # 「今日」（today_fn 注入口径）

PROFILES = (
    TraineeProfile(trainee_id=101, name="张三", cert_status="in_training", path_pct=55.0),
    TraineeProfile(trainee_id=102, name="李四", cert_status="uncertified", path_pct=20.0),
)
RADAR_D2 = {"product": 75.0, "communication": 70.0, "negotiation": 65.0, "platform": 72.0}


def make_source(**kwargs: object) -> FakeBoardSource:
    """默认两学员源（kwargs 覆盖事件流）。"""
    return FakeBoardSource(list(PROFILES), **kwargs)  # type: ignore[arg-type]


def test_aggregate_day_maps_sources() -> None:
    """来源映射：当日 2 次演练均分 / 1 次考试通过 / 90 分钟学习 / 3 课时 / 路径 55%。"""
    source = make_source(
        learning={101: [LearningEvent(stat_date=D2, minutes=60, lessons_done=2, path_pct=40.0),
                        LearningEvent(stat_date=D2, minutes=30, lessons_done=1, path_pct=55.0)]},
        drills={101: [DrillEvent(stat_date=D2, score=80.0), DrillEvent(stat_date=D2, score=90.0)]},
        exams={101: [ExamEvent(stat_date=D2, passed=True)]},
        radars={101: [RadarSnapshot(stat_date=D2, radar=RADAR_D2)]},
    )
    store = DailyStatsStore()
    rows = TeamStatsAggregator(source, store).aggregate_day(D2)

    stat = store.get(101, D2)
    assert stat is not None
    assert stat.study_minutes == 90  # 学习时长求和（SR §9 test_aggregate_sources_map 口径）
    assert stat.lessons_done == 3
    assert stat.path_pct == 55.0  # 路径累计完成率取最大值
    assert stat.drill_count == 2
    assert stat.drill_avg_score == 85.0
    assert stat.exam_count == 1
    assert stat.exam_pass_count == 1
    assert stat.radar == RADAR_D2
    assert rows == 2  # 返回写入行数：101 有数据 + 102 补零，全员各一行


def test_aggregate_day_exam_fail_count() -> None:
    """考试通过数只计 passed=True（2 次 1 过 → pass_count=1）。"""
    source = make_source(
        exams={101: [ExamEvent(stat_date=D2, passed=True), ExamEvent(stat_date=D2, passed=False)]}
    )
    store = DailyStatsStore()
    TeamStatsAggregator(source, store).aggregate_day(D2)
    stat = store.get(101, D2)
    assert stat is not None
    assert stat.exam_count == 2
    assert stat.exam_pass_count == 1
    assert stat.drill_avg_score is None  # 无演练 → 平均分空


def test_aggregate_idempotent_replay() -> None:
    """幂等重放：同日重算覆盖（ON CONFLICT 语义）——行数不变、值一致。"""
    source = make_source(
        drills={101: [DrillEvent(stat_date=D2, score=70.0), DrillEvent(stat_date=D2, score=90.0)]},
        learning={101: [LearningEvent(stat_date=D2, minutes=45, lessons_done=1, path_pct=30.0)]},
    )
    store = DailyStatsStore()
    agg = TeamStatsAggregator(source, store)
    agg.aggregate_day(D2)
    first = store.get(101, D2)
    rows_second = agg.aggregate_day(D2)  # 重放（SR §9 test_aggregate_idempotent）
    assert rows_second == 2  # 写入行数 = 学员数（含补零），与首次一致
    assert store.count() == 2  # 101 + 补零的 102，无重复行
    assert store.get(101, D2) == first


def test_aggregate_zero_fill_empty_day() -> None:
    """空数据日补零：无任何事件的日期也为全员写出 0 值行（看板永远有数）。"""
    store = DailyStatsStore()
    TeamStatsAggregator(make_source(), store).aggregate_day(D2)
    for tid in (101, 102):
        stat = store.get(tid, D2)
        assert stat == DailyStat(stat_date=D2)  # 全零缺省行、雷达空字典


def test_aggregate_day_boundary_no_bleed() -> None:
    """聚合日界：仅统计当日事件，不串日（D1 事件不进 D2 行）。"""
    source = make_source(
        learning={101: [LearningEvent(stat_date=D1, minutes=120, lessons_done=4, path_pct=10.0)]},
        drills={101: [DrillEvent(stat_date=D1, score=88.0)]},
    )
    store = DailyStatsStore()
    TeamStatsAggregator(source, store).aggregate_day(D2)
    today_stat = store.get(101, D2)
    assert today_stat is not None
    assert today_stat.study_minutes == 0
    assert today_stat.drill_count == 0
    yesterday = store.get(101, D1)
    assert yesterday is None  # 未聚合的日期不产生行


def test_aggregate_radar_latest_on_or_before() -> None:
    """雷达快照取当日值；当日缺失取最近一次（≤ 当日）快照。"""
    radar_d1 = {"product": 60.0, "communication": 58.0, "negotiation": 55.0, "platform": 61.0}
    source = make_source(radars={101: [RadarSnapshot(stat_date=D1, radar=radar_d1)]})
    store = DailyStatsStore()
    TeamStatsAggregator(source, store).aggregate_day(D2)
    stat = store.get(101, D2)
    assert stat is not None
    assert stat.radar == radar_d1  # D2 无快照 → 沿用 D1 最新值


def test_aggregate_range_covers_all_days() -> None:
    """区间聚合：D1..D2 两天 × 2 学员 = 4 行；返回值为写入行数。"""
    store = DailyStatsStore()
    rows = TeamStatsAggregator(make_source(), store).aggregate_range(D1, D2)
    assert rows == 4
    assert store.count() == 4
    assert store.get(101, D1) is not None and store.get(102, D2) is not None
