# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""主管看板识别引擎用例（FR-TR-006，SR-TR-006 §6.3/§9；TDD 阶段①，迭代 1.3）。

五类场景落位：
- 等价类/边界：HP-1/HP-2/NI-1/NI-2/NI-3 命中与不命中（阈值两侧、证据不足、
  已结业/已认证豁免）；
- 可解释性：命中必带 rule_key + 规则参数快照 + 数据证据（SRS §16.2 验收口径）；
- 配置化：规则参数热更新（NFR-MAI-02 改参不发版，rule_params 快照随新值）；
- 状态机：重复命中保留 first_hit_at、不再命中撤销 open 标记、confirmed 不撤销；
- 并发：多线程并发上报同 (trainee, rule) 无重复无丢失。
"""
from __future__ import annotations

from datetime import date, timedelta
from threading import Thread

from services.training.core.board.aggregate import (
    DailyStat,
    DailyStatsStore,
    DrillEvent,
    ExamEvent,
    FlagStore,
    FlagEvaluator,
    RuleEngine,
    TraineeProfile,
)

from .test_board_fakes import FakeBoardSource

TODAY = date(2026, 10, 10)
WINDOW = 30
RADAR = {"product": 70.0, "communication": 70.0, "negotiation": 70.0, "platform": 70.0}


def profile(tid: int = 101, *, cert: str = "in_training", path_pct: float = 50.0) -> TraineeProfile:
    """构造学员档案（缺省在训 50%）。"""
    return TraineeProfile(trainee_id=tid, name=f"学员{tid}", cert_status=cert, path_pct=path_pct)


def fill_days(
    store: DailyStatsStore,
    tid: int,
    *,
    start: date,
    days: int,
    lessons: int = 0,
    radar_avg: float | None = None,
    path_pct: float = 50.0,
) -> None:
    """连续 days 天预聚合行（lessons/雷达四维同值注入）。"""
    for offset in range(days):
        day = start + timedelta(days=offset)
        radar = ({k: radar_avg for k in RADAR} if radar_avg is not None else {})
        store.upsert(tid, DailyStat(stat_date=day, lessons_done=lessons, path_pct=path_pct, radar=radar))


def make_evaluator(
    store: DailyStatsStore,
    *,
    profiles: list[TraineeProfile] | None = None,
    drills: dict[int, list[DrillEvent]] | None = None,
    exams: dict[int, list[ExamEvent]] | None = None,
    flag_store: FlagStore | None = None,
) -> FlagEvaluator:
    """装配识别器（事件流经 FakeBoardSource 注入，不 import drill/exam 实现）。"""
    source = FakeBoardSource(list(profiles or [profile()]), drills=drills, exams=exams)
    return FlagEvaluator(source, store, flag_store or FlagStore())


def test_hp1_hit_with_evidence() -> None:
    """HP-1 命中：14 天雷达四维均分 +4 → 周增速 2.0 ≥ 1.0 且现值 ≥ 70；证据含前后雷达。"""
    store = DailyStatsStore()
    fill_days(store, 101, start=TODAY - timedelta(days=14), days=15, lessons=1, radar_avg=70.0)
    store.upsert(101, DailyStat(stat_date=TODAY, lessons_done=1, path_pct=50.0,
                                radar={k: 74.0 for k in RADAR}))
    evaluator = make_evaluator(store, drills={101: [DrillEvent(stat_date=TODAY, score=80.0)] * 10})
    hits = evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW, now="2026-10-10T02:00:00+08:00")

    hp1 = [h for h in hits if h.rule_key == "HP-1"]
    assert len(hp1) == 1
    hit = hp1[0]
    assert hit.flag_type == "high_potential"
    assert hit.evidence["radar_then"] == 70.0  # 可解释：命中哪条规则 + 数据快照
    assert hit.evidence["radar_now"] == 74.0
    assert hit.evidence["growth_per_week"] == 2.0
    assert hit.rule_params == RuleEngine.default().params("HP-1")  # 参数快照随命中落档


def test_hp1_miss_scenarios() -> None:
    """HP-1 不命中：增速不足 / 现值低于 floor 两条路径均不产生标记。"""
    base = TODAY - timedelta(days=14)
    # 增速 0.5/周 < 1.0
    store_a = DailyStatsStore()
    fill_days(store_a, 101, start=base, days=15, lessons=1, radar_avg=70.0)
    store_a.upsert(101, DailyStat(stat_date=TODAY, lessons_done=1, path_pct=50.0,
                                  radar={k: 71.0 for k in RADAR}))
    # 现值 65 < floor 70（增速达标）
    store_b = DailyStatsStore()
    fill_days(store_b, 101, start=base, days=15, lessons=1, radar_avg=60.0)
    store_b.upsert(101, DailyStat(stat_date=TODAY, lessons_done=1, path_pct=50.0,
                                  radar={k: 65.0 for k in RADAR}))
    for store in (store_a, store_b):
        evaluator = make_evaluator(store, drills={101: [DrillEvent(stat_date=TODAY, score=80.0)] * 10})
        hits = evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW)
        assert [h for h in hits if h.rule_key == "HP-1"] == []


def test_hp2_hit_and_insufficient_samples() -> None:
    """HP-2：近 10 次演练均分 ≥80 且标准差 ≤8 命中；仅 9 次（证据不足）不命中。"""
    scores_stable = [80.0 + (i % 4) for i in range(10)]  # 均值 81.5、标准差小
    drills_ok = {101: [DrillEvent(stat_date=TODAY - timedelta(days=i), score=s)
                       for i, s in enumerate(scores_stable)]}
    drills_few = {101: drills_ok[101][:9]}
    for drills, expect_hit in ((drills_ok, True), (drills_few, False)):
        store = DailyStatsStore()
        fill_days(store, 101, start=TODAY - timedelta(days=9), days=10, lessons=1, path_pct=50.0)
        evaluator = make_evaluator(store, drills=drills)
        hits = evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW)
        hp2 = [h for h in hits if h.rule_key == "HP-2"]
        assert bool(hp2) is expect_hit
        if expect_hit:
            assert len(hp2[0].evidence["scores"]) == 10  # 证据：得分序列快照


def test_ni1_stall_boundary_hit() -> None:
    """NI-1 边界=阈值：连续 5 天 0 课时（路径未结业）命中；证据含 stall_days 与逐日课时。"""
    store = DailyStatsStore()
    fill_days(store, 101, start=TODAY - timedelta(days=4), days=5, lessons=0, path_pct=50.0)
    evaluator = make_evaluator(store)
    hits = evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW, now="2026-10-10T02:00:00+08:00")
    ni1 = [h for h in hits if h.rule_key == "NI-1"]
    assert len(ni1) == 1
    hit = ni1[0]
    assert hit.flag_type == "need_intervention"
    assert hit.evidence["stall_days"] == 5
    assert len(hit.evidence["daily_lessons"]) >= 5  # 逐日课时快照（前端「为什么标记他」）


def test_ni1_miss_scenarios() -> None:
    """NI-1 不命中：停滞仅 4 天（阈值另一侧）/ 已结业（path=100）豁免。"""
    store_a = DailyStatsStore()
    fill_days(store_a, 101, start=TODAY - timedelta(days=3), days=4, lessons=0, path_pct=50.0)
    store_b = DailyStatsStore()
    fill_days(store_b, 101, start=TODAY - timedelta(days=4), days=5, lessons=0, path_pct=100.0)
    for store in (store_a, store_b):
        evaluator = make_evaluator(store)
        hits = evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW)
        assert [h for h in hits if h.rule_key == "NI-1"] == []


def test_ni2_hit_and_miss() -> None:
    """NI-2：最近 3 次演练全部 <60 命中；末次回升 90 打断连续性不命中。"""
    low3 = [50.0, 55.0, 58.0]
    broken = [50.0, 55.0, 90.0]
    for scores, expect_hit in ((low3, True), (broken, False)):
        store = DailyStatsStore()
        fill_days(store, 101, start=TODAY - timedelta(days=2), days=3, lessons=1, path_pct=50.0)
        drills = {
            101: [DrillEvent(stat_date=TODAY - timedelta(days=2 - i), score=s) for i, s in enumerate(scores)]
        }
        evaluator = make_evaluator(store, drills=drills)
        hits = evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW)
        ni2 = [h for h in hits if h.rule_key == "NI-2"]
        assert bool(ni2) is expect_hit
        if expect_hit:
            assert ni2[0].evidence["scores"] == low3


def test_ni3_hit_and_certified_exempt() -> None:
    """NI-3：近 30 天 2 次 failed 且未认证命中；已认证（certified）豁免。"""
    fails = [ExamEvent(stat_date=TODAY - timedelta(days=d), passed=False) for d in (2, 5)]
    for cert, expect_hit in (("in_training", True), ("certified", False)):
        store = DailyStatsStore()
        fill_days(store, 101, start=TODAY - timedelta(days=2), days=3, lessons=1, path_pct=50.0)
        evaluator = make_evaluator(store, exams={101: fails}, profiles=[profile(cert=cert)])
        hits = evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW)
        ni3 = [h for h in hits if h.rule_key == "NI-3"]
        assert bool(ni3) is expect_hit
        if expect_hit:
            assert len(ni3[0].evidence["failed_exams"]) == 2


def test_rule_params_hot_reload() -> None:
    """配置化：热更新 NI-1 阈值 5→3 后立即生效，rule_params 快照为新值（改参可追溯）。"""
    store = DailyStatsStore()
    fill_days(store, 101, start=TODAY - timedelta(days=2), days=3, lessons=0, path_pct=50.0)
    engine = RuleEngine.default()
    evaluator = make_evaluator(store)
    evaluator.engine = engine
    assert not [h for h in evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW)
                if h.rule_key == "NI-1"]  # 缺省阈值 5：3 天停滞不命中
    engine.set_params("NI-1", {"stall_days": 3})  # 热更新（app_config 改参不发版语义）
    hits = evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW)
    ni1 = [h for h in hits if h.rule_key == "NI-1"]
    assert len(ni1) == 1
    assert ni1[0].rule_params == {"stall_days": 3}  # 快照为新参数，历史标记可按当时参数解释


def test_flag_upsert_keeps_first_hit() -> None:
    """重复命中：first_hit_at 不变、last_eval_at 更新，且 (trainee, rule) 唯一不重复登记。"""
    store = DailyStatsStore()
    fill_days(store, 101, start=TODAY - timedelta(days=4), days=5, lessons=0, path_pct=50.0)
    flag_store = FlagStore()
    evaluator = make_evaluator(store, flag_store=flag_store)
    evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW, now="2026-10-10T02:00:00+08:00")
    evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW, now="2026-10-10T03:00:00+08:00")
    flags = flag_store.flags_of(101)
    ni1 = [f for f in flags if f.rule_key == "NI-1"]
    assert len(ni1) == 1
    assert ni1[0].first_hit_at == "2026-10-10T02:00:00+08:00"  # 首次命中时间保留
    assert ni1[0].last_eval_at == "2026-10-10T03:00:00+08:00"  # 末次评估刷新


def test_flag_retract_open_but_keep_confirmed() -> None:
    """不再命中：open 标记撤销；主管已确认（confirmed）的标记保留不撤销。"""
    # 学员 101：先命中后恢复（恢复课时）
    store = DailyStatsStore()
    fill_days(store, 101, start=TODAY - timedelta(days=4), days=5, lessons=0, path_pct=50.0)
    flag_store = FlagStore()
    evaluator = make_evaluator(store, flag_store=flag_store)
    evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW, now="n1")
    assert any(f.rule_key == "NI-1" for f in flag_store.flags_of(101))
    for offset in range(5):
        day = TODAY - timedelta(days=4 - offset)
        store.upsert(101, DailyStat(stat_date=day, lessons_done=2, path_pct=50.0))
    evaluator.evaluate_trainee(101, today=TODAY, window_days=WINDOW, now="n2")
    assert not any(f.rule_key == "NI-1" for f in flag_store.flags_of(101))  # open → 撤销
    # 学员 102：同轨迹但先确认，恢复后标记保留
    fill_days(store, 102, start=TODAY - timedelta(days=4), days=5, lessons=0, path_pct=50.0)
    evaluator2 = make_evaluator(store, flag_store=flag_store,
                                profiles=[profile(101), profile(102)])
    evaluator2.evaluate_trainee(102, today=TODAY, window_days=WINDOW, now="n1")
    flag_store.confirm(102, "NI-1")  # 主管确认干预
    for offset in range(5):
        day = TODAY - timedelta(days=4 - offset)
        store.upsert(102, DailyStat(stat_date=day, lessons_done=2, path_pct=50.0))
    evaluator2.evaluate_trainee(102, today=TODAY, window_days=WINDOW, now="n2")
    kept = [f for f in flag_store.flags_of(102) if f.rule_key == "NI-1"]
    assert len(kept) == 1 and kept[0].status == "confirmed"  # 确认件不自动撤销


def test_concurrent_evaluate_no_duplicate() -> None:
    """并发上报：两线程同时 evaluate_all，标记无重复、无丢失（唯一键 + 锁）。"""
    store = DailyStatsStore()
    fill_days(store, 101, start=TODAY - timedelta(days=4), days=5, lessons=0, path_pct=50.0)
    fill_days(store, 102, start=TODAY - timedelta(days=4), days=5, lessons=0, path_pct=50.0)
    flag_store = FlagStore()
    evaluator = make_evaluator(store, flag_store=flag_store,
                               profiles=[profile(101), profile(102)])
    threads = [Thread(target=lambda: evaluator.evaluate_all([101, 102], today=TODAY, window_days=WINDOW,
                                                             now=f"t{i}"))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ni_flags = [f for f in flag_store.list_flags() if f.rule_key == "NI-1"]
    keys = {(f.trainee_id, f.rule_key) for f in ni_flags}
    assert len(ni_flags) == len(keys) == 2  # 无重复登记、两学员各一条
