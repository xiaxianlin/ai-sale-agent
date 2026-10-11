# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""能力雷达评估引擎测试（FR-TR-004，SR-TR-004 §6.2/§6.3/§7/§9；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类场景）：
- 等价类：四维证据源聚合（演练/考试/问答加权合成）、快照落库与最新值、
  历史序列升序与 initial 基线、趋势窗口过滤、学员数据隔离（NFR-SEC-02）；
- 边界：时间衰减半衰期恰等（权重减半）、陈旧证据衰减趋零、最小证据数阈值
  （n=min-1 → null / n=min → 出分）、权重和≠1 拒绝 / 和=1（含 0 值）接受、
  半衰期与最小证据数取值范围（SR §7：7~180 / 1~10）；
- 异常：权重矩阵未知维度拒绝、非法触发类型拒绝、校准样本维度残缺/分值越界拒绝、
  无证据学员全维 null（不填 0 分，SR §2 显式降级决策）；
- 并发：同学员并发重算 → 快照 id 唯一且有序（生产为 advisory lock，demo 为存储锁）；
- 降级：校准空样本一致率不定义（None，不冻结不误报）、证据不足维度在快照
  字典中保持 score=null（不参与薄弱判定，供 FR-TR-005 消费）。

校准口径对接 SR-TR-003 §7（演练校准同尺）：维差 ≤15 且总分差 ≤10 判一致；
KPI：一致率 ≥85%（SRS §16.2）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from services.training.core.radar.engine import (
    DIMENSIONS,
    DIM_LABELS,
    CalibrationPair,
    Evidence,
    FakeEvidenceSource,
    InMemoryRadarSnapshotStore,
    RadarConfig,
    RadarEngine,
    RadarSnapshot,
)

NOW = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)


def days_ago(n: float) -> datetime:
    """构造 NOW 往前 n 天的时间戳（时间衰减用例基座）。"""
    return NOW - timedelta(days=n)


def ev(dim: str, source: str, score: float, occurred_at: datetime, ref: str = "r1") -> Evidence:
    """证据构造便捷函数（分数已归一化百分制）。"""
    return Evidence(dim=dim, source=source, ref_id=ref, score=score, occurred_at=occurred_at)


def make_engine(
    seeds: dict[int, list[Evidence]] | None = None,
    *,
    config: RadarConfig | None = None,
    store: InMemoryRadarSnapshotStore | None = None,
    **kwargs: Any,
) -> RadarEngine:
    """标准装配：演练/考试/问答三个 Fake 证据源 + 内存快照存储（demo 口径）。"""
    store = store if store is not None else InMemoryRadarSnapshotStore()
    sources = [
        FakeEvidenceSource("exercise", seeds=seeds),
        FakeEvidenceSource("exam", seeds=seeds),
        FakeEvidenceSource("mentor_qa", seeds=seeds),
    ]
    return RadarEngine(sources, store, config=config, **kwargs)


def cfg(**kw: Any) -> RadarConfig:
    """测试配置便捷函数：缺省 min_evidence=1（其余用 SR §7 缺省）。"""
    kw.setdefault("min_evidence", 1)
    return RadarConfig(**kw)


class TestRadarComputeEquivalence:
    """等价类：证据聚合 → 加权衰减 → 快照落库 → 查询视图主链路。"""

    def test_compute_weighted_multi_source(self) -> None:
        """三源合成：product 维 exercise 80/exam 90/mentor_qa 60 按 0.35/0.45/0.20 加权 = 80.5。"""
        engine = make_engine(
            {
                1: [
                    ev("product", "exercise", 80, NOW),
                    ev("product", "exam", 90, NOW),
                    ev("product", "mentor_qa", 60, NOW),
                    ev("communication", "exercise", 70, NOW),
                    ev("negotiation", "exam", 88, NOW),
                    ev("platform", "mentor_qa", 66, NOW),
                ]
            },
            config=cfg(),
        )
        snap = engine.compute(1, trigger="initial", now=NOW)
        scores = {d: snap.dims[d]["score"] for d in DIMENSIONS}
        assert scores == {"product": 80.5, "communication": 70.0, "negotiation": 88.0, "platform": 66.0}
        assert snap.dims["product"]["n"] == 3
        assert snap.trigger == "initial"

    def test_snapshot_persisted_latest_and_digest(self) -> None:
        """快照落库：latest 返回最近一次计算；weights_used 与配置摘要一致（可复现/审计）。"""
        store = InMemoryRadarSnapshotStore()
        engine = make_engine({1: [ev("product", "exam", 90, NOW)]}, config=cfg(), store=store)
        first = engine.compute(1, trigger="initial", now=NOW)
        second = engine.compute(1, trigger="event", now=NOW)
        assert engine.latest(1) is second and second.snapshot_id != first.snapshot_id
        assert second.weights_used == cfg().digest() != ""

    def test_history_ascending_and_baseline_initial(self) -> None:
        """历史序列按落库序升序；baseline 取首个 trigger=initial 快照（入职初评基线）。"""
        engine = make_engine({1: [ev("product", "exam", 90, NOW)]}, config=cfg())
        s1 = engine.compute(1, trigger="initial", now=days_ago(5))
        engine.compute(1, trigger="event", now=days_ago(2))
        s3 = engine.compute(1, trigger="stage", now=NOW)
        history = engine.history(1)
        assert [s.snapshot_id for s in history] == [s1.snapshot_id, history[1].snapshot_id, s3.snapshot_id]
        assert engine.baseline(1) is s1
        assert engine.baseline(1).trigger == "initial"

    def test_trend_window_filter(self) -> None:
        """趋势序列：默认全窗口升序；window_days 收窄后仅保留窗口内快照。"""
        engine = make_engine({1: [ev("product", "exam", 90, NOW)]}, config=cfg())
        engine.compute(1, trigger="initial", now=days_ago(5))
        engine.compute(1, trigger="event", now=days_ago(2))
        engine.compute(1, trigger="event", now=NOW)
        full = engine.trend(1, now=NOW)
        assert [row["trigger"] for row in full] == ["initial", "event", "event"]
        assert all(set(row["scores"]) == set(DIMENSIONS) for row in full)
        narrow = engine.trend(1, now=NOW, window_days=3)
        assert [row["trigger"] for row in narrow] == ["event", "event"]

    def test_trainee_isolation(self) -> None:
        """学员隔离（NFR-SEC-02）：学员 2 无证据，不读到学员 1 的任何分数。"""
        engine = make_engine({1: [ev("product", "exam", 90, NOW)]}, config=cfg())
        engine.compute(1, now=NOW)
        snap2 = engine.compute(2, now=NOW)
        assert all(snap2.dims[d]["score"] is None for d in DIMENSIONS)


class TestRadarBoundary:
    """边界：时间衰减半衰期、最小证据数阈值、权重矩阵取值与求和校验。"""

    def test_half_life_boundary_half_weight(self) -> None:
        """半衰期边界：证据恰在 half_life_days（30 天）时权重减半 → (90+0.5*50)/1.5=76.7。"""
        engine = make_engine(
            {1: [ev("product", "exercise", 90, NOW), ev("product", "exercise", 50, days_ago(30))]},
            config=cfg(min_evidence=2),
        )
        snap = engine.compute(1, now=NOW)
        assert snap.dims["product"]["score"] == 76.7

    def test_recent_evidence_dominates(self) -> None:
        """衰减单调性：300 天前（≈10 个半衰期）的低分证据几乎不影响近期高分。"""
        engine = make_engine(
            {1: [ev("product", "exercise", 90, NOW), ev("product", "exercise", 10, days_ago(300))]},
            config=cfg(min_evidence=2),
        )
        snap = engine.compute(1, now=NOW)
        assert snap.dims["product"]["score"] == 89.9

    def test_min_evidence_below_threshold_null(self) -> None:
        """证据不足：n = min_evidence-1 → 该维 score=null（显式降级，不填 0/默认分）。"""
        engine = make_engine(
            {1: [ev("product", "exam", 90, NOW), ev("product", "exercise", 80, NOW)]},
            config=cfg(min_evidence=3),
        )
        snap = engine.compute(1, now=NOW)
        assert snap.dims["product"] == {"score": None, "n": 2}

    def test_min_evidence_at_threshold_scored(self) -> None:
        """阈值边界：n 恰 = min_evidence → 正常出分（加权均值 85.0）。"""
        engine = make_engine(
            {
                1: [
                    ev("product", "exam", 90, NOW),
                    ev("product", "exercise", 80, NOW),
                    ev("product", "mentor_qa", 80, NOW),
                ]
            },
            config=cfg(min_evidence=3),
        )
        snap = engine.compute(1, now=NOW)
        assert snap.dims["product"]["score"] == 84.5  # (0.45*90+0.35*80+0.20*80)/1.0
        assert snap.dims["product"]["n"] == 3

    def test_weight_sum_not_one_rejected(self) -> None:
        """权重和校验：product 三源和=0.9 → 构造期拒绝（启动期 fail fast，SR §9）。"""
        weights = {
            "product": {"exercise": 0.30, "exam": 0.40, "mentor_qa": 0.20},
            "communication": {"exercise": 0.55, "exam": 0.20, "mentor_qa": 0.25},
            "negotiation": {"exercise": 0.60, "exam": 0.25, "mentor_qa": 0.15},
            "platform": {"exercise": 0.25, "exam": 0.55, "mentor_qa": 0.20},
        }
        with pytest.raises(ValueError, match="权重之和必须为 1"):
            RadarConfig(source_weights=weights)

    def test_weight_sum_one_accepted_custom(self) -> None:
        """权重和=1（含 0 值源）接受：单源独占权重时该维分=该源分。"""
        weights = {
            dim: {
                "exercise": 1.0 if dim == "product" else 0.0,
                "exam": 1.0 if dim != "product" else 0.0,
                "mentor_qa": 0.0,
            }
            for dim in DIMENSIONS
        }
        config = RadarConfig(source_weights=weights, min_evidence=1)
        engine = make_engine(
            {1: [ev("product", "exercise", 77, NOW), ev("platform", "exam", 66, NOW)]}, config=config
        )
        snap = engine.compute(1, now=NOW)
        assert snap.dims["product"]["score"] == 77.0
        assert snap.dims["platform"]["score"] == 66.0

    def test_half_life_out_of_range_rejected(self) -> None:
        """半衰期取值范围（SR §7：7~180）：6 与 181 均拒绝。"""
        with pytest.raises(ValueError, match="half_life_days"):
            RadarConfig(half_life_days=6)
        with pytest.raises(ValueError, match="half_life_days"):
            RadarConfig(half_life_days=181)

    def test_min_evidence_out_of_range_rejected(self) -> None:
        """最小证据数取值范围（SR §7：1~10）：0 与 11 均拒绝。"""
        with pytest.raises(ValueError, match="min_evidence"):
            RadarConfig(min_evidence=0)
        with pytest.raises(ValueError, match="min_evidence"):
            RadarConfig(min_evidence=11)


class TestRadarException:
    """异常：非法配置与非法校准样本的拒绝口径。"""

    def test_weights_unknown_dim_rejected(self) -> None:
        """权重矩阵出现四维之外的键（或缺失维）→ 构造期拒绝。"""
        weights = {dim: {"exercise": 0.34, "exam": 0.33, "mentor_qa": 0.33} for dim in DIMENSIONS}
        bad_extra = dict(weights)
        bad_extra["cheating"] = {"exercise": 1.0, "exam": 0.0, "mentor_qa": 0.0}
        with pytest.raises(ValueError, match="维度"):
            RadarConfig(source_weights=bad_extra)
        bad_missing = {k: v for k, v in weights.items() if k != "platform"}
        with pytest.raises(ValueError, match="维度"):
            RadarConfig(source_weights=bad_missing)

    def test_invalid_trigger_rejected(self) -> None:
        """触发类型非法（五类之外）→ 拒绝计算。"""
        engine = make_engine({1: [ev("product", "exam", 90, NOW)]}, config=cfg())
        with pytest.raises(ValueError, match="trigger"):
            engine.compute(1, trigger="bogus", now=NOW)

    def test_calibrate_missing_dim_rejected(self) -> None:
        """校准样本维度残缺（盲评须四维齐备，与 AI 同尺）→ ValueError。"""
        engine = make_engine(config=cfg())
        pair = CalibrationPair(
            pair_id="P1",
            ai_scores={d: 80.0 for d in DIMENSIONS},
            supervisor_scores={d: 80.0 for d in DIMENSIONS if d != "platform"},
        )
        with pytest.raises(ValueError, match="维度"):
            engine.calibrate([pair])

    def test_calibrate_score_out_of_range_rejected(self) -> None:
        """校准分值越界（>100 或负值）→ ValueError。"""
        engine = make_engine(config=cfg())
        over = CalibrationPair(
            pair_id="P1",
            ai_scores={d: 80.0 for d in DIMENSIONS},
            supervisor_scores={d: 101.0 for d in DIMENSIONS},
        )
        neg = CalibrationPair(
            pair_id="P2",
            ai_scores={d: 80.0 for d in DIMENSIONS},
            supervisor_scores={d: -1.0 for d in DIMENSIONS},
        )
        with pytest.raises(ValueError, match="0-100"):
            engine.calibrate([over])
        with pytest.raises(ValueError, match="0-100"):
            engine.calibrate([neg])

    def test_no_evidence_all_dims_null(self) -> None:
        """无任何证据：四维全 null（n=0），不得以 0 分污染薄弱项判定（SR §2）。"""
        engine = make_engine(config=cfg())
        snap = engine.compute(7, now=NOW)
        assert snap.dims == {d: {"score": None, "n": 0} for d in DIMENSIONS}
        assert all(snap.dims[d]["score"] is None for d in DIMENSIONS)  # 明确非 0 分填充


class TestRadarConcurrency:
    """并发：同学员并发重算（生产 advisory lock / demo 存储锁）→ 快照有序不丢。"""

    def test_concurrent_compute_snapshots_unique_ordered(self) -> None:
        engine = make_engine({1: [ev("product", "exam", 90, NOW)]}, config=cfg())
        with ThreadPoolExecutor(max_workers=4) as pool:
            snapshots = list(pool.map(lambda _: engine.compute(1, trigger="event", now=NOW), range(4)))
        assert len({s.snapshot_id for s in snapshots}) == 4
        history = engine.history(1)
        assert [s.snapshot_id for s in history] == sorted(s.snapshot_id for s in history)
        assert engine.latest(1).snapshot_id == history[-1].snapshot_id


class TestRadarDegradation:
    """降级：一致率无样本不定义；证据不足维度 null 贯穿快照字典。"""

    def test_calibrate_empty_samples_undefined_rate(self) -> None:
        """无有效盲评样本：一致率 None、kpi_passed None（不误报达标/不达标）。"""
        engine = make_engine(config=cfg())
        report = engine.calibrate([])
        assert report.consistency_rate is None and report.kpi_passed is None
        assert report.samples == 0
        assert any("暂无" in s for s in report.suggestions)

    def test_insufficient_dim_null_in_snapshot_dict(self) -> None:
        """部分维度证据不足：null 维保持 score=None 且 n 正确（供 FR-TR-005 排除薄弱排序）。"""
        engine = make_engine(
            {
                1: [
                    ev("product", "exam", 90, NOW),  # product 仅 1 条（< min_evidence=2）→ null
                    ev("negotiation", "exercise", 40, NOW),
                    ev("negotiation", "exam", 45, NOW),
                    ev("communication", "exercise", 75, NOW),
                    ev("communication", "exam", 70, NOW),
                    ev("platform", "exercise", 88, NOW),
                    ev("platform", "exam", 82, NOW),
                ]
            },
            config=cfg(min_evidence=2),
        )
        snap = engine.compute(1, trigger="initial", now=NOW)
        assert snap.dims["product"] == {"score": None, "n": 1}
        assert snap.dims["negotiation"]["score"] == 41.5  # (0.60*40+0.25*45)/0.85
        assert DIM_LABELS["product"] == "产品知识"  # 中文维度名随四维口径固定（前端渲染基座）


__all__ = ["NOW", "days_ago", "ev", "make_engine", "cfg", "RadarSnapshot"]
