# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""拟人化自检单元测试（NFR-SEC-11，SR-NFR-SEC-11 §6.4；迭代 2.3 TDD 阶段①用例）。

覆盖（Typer 已有采样——本文件补「拟人度自检」评估闭环）：
- 操作间隔分布 vs 人工基线：KS 检验简化版（D 统计量，无 scipy 依赖）；
- 输入速度直方图相似度：N 桶归一化直方图交集；
- 拟人度评分：加权合成 → none / auto_tuned / frozen 三档处置；
- 机械化特征旗标：等间隔（CV < 0.25）命中 constant_gap；
- 小样本不评分（防误判）；确定性种子可复现。

实现方案：docs/07-实现方案/NFR/SR-NFR-SEC-11-操作节奏拟人化.md §6.4、§9
"""

from __future__ import annotations

import math
import random

from services.agent.core.risk.pacing import (
    HumanBaseline,
    HumannessEvaluator,
    evaluate_gaps_ks,
)


def humanlike_gaps(n: int, seed: int = 42) -> list[float]:
    """从人工基线分布自采样操作间隔（毫秒）——模拟「拟人」轨迹。"""
    rng = random.Random(seed)
    base = HumanBaseline()
    return [min(max(rng.lognormvariate(base.interval_mu, base.interval_sigma), 20_000.0),
                600_000.0) for _ in range(n)]


def humanlike_cps(n: int, seed: int = 43) -> list[float]:
    """从人工基线分布自采样输入速度（字符/秒）。"""
    rng = random.Random(seed)
    base = HumanBaseline()
    return [min(max(rng.lognormvariate(math.log(base.cps_median), base.cps_sigma),
                    base.cps_min), base.cps_max) for _ in range(n)]


class TestKSStatistic:
    """简化版 KS 检验（两样本经验分布最大间距，scipy 本地依赖的纯 Python 等价）。"""

    def test_same_distribution_small_d(self) -> None:
        """同分布两样本 D 统计量小（< 0.10 量级）。"""
        a = humanlike_gaps(800, seed=1)
        b = humanlike_gaps(800, seed=2)
        assert evaluate_gaps_ks(a, b) < 0.10

    def test_disjoint_distributions_large_d(self) -> None:
        """完全分离的两分布 D → 1（等间隔机械轨迹 vs 对数正态基线）。"""
        mechanical = [60_000.0] * 500
        human = humanlike_gaps(500, seed=3)
        assert evaluate_gaps_ks(mechanical, human) > 0.5

    def test_d_bounded(self) -> None:
        """D 统计量落在 [0, 1]。"""
        d = evaluate_gaps_ks(humanlike_gaps(100, 4), humanlike_gaps(100, 5))
        assert 0.0 <= d <= 1.0


class TestHumannessEvaluator:
    """拟人度评分：KS + 直方图相似度 → 评分与处置。"""

    def test_humanlike_scores_pass(self) -> None:
        """基线自采样轨迹：评分 ≥ pass_th，无机械旗标，处置 none。"""
        ev = HumannessEvaluator()
        report = ev.evaluate(humanlike_gaps(800), humanlike_cps(800))
        assert report.score >= ev.pass_th
        assert report.flags == ()
        assert report.action == "none"

    def test_constant_gaps_flagged_and_frozen(self) -> None:
        """等间隔机械轨迹：命中 constant_gap（M1），评分 < warn_th，处置 frozen。"""
        ev = HumannessEvaluator()
        report = ev.evaluate([60_000.0] * 500, humanlike_cps(500))
        assert "constant_gap" in report.flags
        assert report.score < ev.warn_th
        assert report.action == "frozen"

    def test_cps_similarity_reflected(self) -> None:
        """输入速度偏离基线（恒速 10cps）→ 直方图相似度低、评分不达 pass。"""
        ev = HumannessEvaluator()
        report = ev.evaluate(humanlike_gaps(500), [10.0] * 500)
        assert report.hist_similarity < 0.5
        assert report.score < ev.pass_th

    def test_auto_tuned_band(self) -> None:
        """中间带处置：抬高 pass_th 使拟人样本落入 [warn_th, pass_th) → auto_tuned。"""
        ev = HumannessEvaluator(pass_th=0.999, warn_th=0.5)
        report = ev.evaluate(humanlike_gaps(800), humanlike_cps(800))
        assert ev.warn_th <= report.score < ev.pass_th
        assert report.action == "auto_tuned"

    def test_insufficient_samples_not_scored(self) -> None:
        """样本 < min_samples 不评分（防小样本误判），处置 insufficient_sample。"""
        ev = HumannessEvaluator(min_samples=100)
        report = ev.evaluate([60_000.0] * 10, [4.0] * 10)
        assert report.action == "insufficient_sample"
        assert report.score == 0.0

    def test_deterministic_with_seed(self) -> None:
        """同一种子评估同一轨迹 → 评分完全一致（可回归）。"""
        gaps, cps = humanlike_gaps(300), humanlike_cps(300)
        a = HumannessEvaluator().evaluate(gaps, cps, rng=random.Random(11))
        b = HumannessEvaluator().evaluate(gaps, cps, rng=random.Random(11))
        assert a.score == b.score and a.ks_d == b.ks_d

    def test_report_metrics_complete(self) -> None:
        """报告分项齐全：ks_d / hist_similarity / gap_cv 可单独出证（验收抽查口径）。"""
        report = HumannessEvaluator().evaluate(humanlike_gaps(200), humanlike_cps(200))
        assert 0.0 <= report.ks_d <= 1.0
        assert 0.0 <= report.hist_similarity <= 1.0
        assert report.gap_cv > 0.25
