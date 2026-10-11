# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""拟人度自检评估器（NFR-SEC-11，SR-NFR-SEC-11 §6.4；迭代 2.3 demo 口径）。

职责（Typer 已有采样——本模块补「可验证闭环」的评估侧）：
- 操作间隔分布 vs 人工基线：**KS 检验简化版**——两样本经验分布函数（ECDF）
  最大间距 D 统计量（纯 Python，无 scipy 本地依赖；数据不出内网语义同 SR §8）；
- 输入速度直方图相似度：N 桶归一化直方图交集（histogram intersection）；
- 拟人度评分：加权合成 [0,1] → 三档处置：none（≥pass_th）/ auto_tuned（中间带，
  轻偏差向基线收敛）/ frozen（<warn_th，先停后查转人工——fail-safe）；
- 机械化特征旗标：等间隔（间隔变异系数 CV < 0.25，SR §6.1 M1）；
- 小样本不评分（防误判，min_samples 同 SR §7 口径）。

纯函数边界：无时钟、无网络；基线蒙特卡洛样本由注入 rng 生成（确定性可回归）。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class HumanBaseline:
    """人工行为基线（human_behavior_baseline 全局行的进程内形态）。

    间隔 = 对数正态（ms 域 μ/σ，与 RiskConfig.ln_mu/ln_sigma 同源）；
    输入速度 = 截断对数正态（字符/秒中位与 σ，夹取 [cps_min, cps_max]）。
    """

    interval_mu: float = 11.0        # ln(ms) ≈ 中位 60s
    interval_sigma: float = 0.6
    cps_median: float = 4.0          # 字符/秒（落 3-5 区间，05 §4.1）
    cps_sigma: float = 0.18
    cps_min: float = 2.5
    cps_max: float = 6.0

    def sample_gaps_ms(self, n: int, rng: random.Random) -> list[float]:
        """基线蒙特卡洛间隔样本（KS 检验的参照分布）。"""
        return [self._clamp_gap(rng.lognormvariate(self.interval_mu, self.interval_sigma))
                for _ in range(n)]

    def sample_cps(self, n: int, rng: random.Random) -> list[float]:
        """基线蒙特卡洛输入速度样本（直方图相似度参照分布）。"""
        return [min(max(rng.lognormvariate(math.log(self.cps_median), self.cps_sigma),
                        self.cps_min), self.cps_max) for _ in range(n)]

    def _clamp_gap(self, gap_ms: float) -> float:
        """间隔夹取（与风控采样同域 [20s, 600s]）。"""
        return min(max(gap_ms, 20_000.0), 600_000.0)


@dataclass(frozen=True)
class HumannessReport:
    """拟人度评估报告（humanness_report 行的进程内形态，验收出证数据源）。"""

    score: float                     # 综合拟人度 [0,1]
    ks_d: float                      # 间隔分布 KS D 统计量
    hist_similarity: float           # 输入速度直方图相似度 [0,1]
    gap_cv: float                    # 间隔变异系数（M1 旗标判据）
    flags: tuple[str, ...]           # 命中的机械化特征（空 = 未命中）
    action: str                      # none / auto_tuned / frozen / insufficient_sample


def evaluate_gaps_ks(sample_a: Sequence[float], sample_b: Sequence[float]) -> float:
    """两样本 KS 检验简化版：经验分布函数最大间距 D ∈ [0,1]（纯 Python）。"""
    if not sample_a or not sample_b:
        return 1.0
    a_sorted = sorted(sample_a)
    b_sorted = sorted(sample_b)
    points = sorted(set(a_sorted) | set(b_sorted))
    d_max = 0.0
    i = j = 0
    for point in points:
        while i < len(a_sorted) and a_sorted[i] <= point:
            i += 1
        while j < len(b_sorted) and b_sorted[j] <= point:
            j += 1
        d = abs(i / len(a_sorted) - j / len(b_sorted))
        d_max = max(d_max, d)
    return d_max


def histogram_intersection(sample_a: Sequence[float], sample_b: Sequence[float],
                           buckets: int = 10) -> float:
    """归一化直方图交集（N 桶等宽，值域取两样本并集范围）→ [0,1] 相似度。"""
    if not sample_a or not sample_b:
        return 0.0
    lo = min(min(sample_a), min(sample_b))
    hi = max(max(sample_a), max(sample_b))
    if hi <= lo:
        return 1.0                                   # 完全同值：恒等分布
    width = (hi - lo) / buckets

    def hist(samples: Sequence[float]) -> list[float]:
        counts = [0.0] * buckets
        for value in samples:
            index = min(int((value - lo) / width), buckets - 1)
            counts[index] += 1.0
        total = sum(counts)
        return [c / total for c in counts]

    hist_a, hist_b = hist(sample_a), hist(sample_b)
    return sum(min(a, b) for a, b in zip(hist_a, hist_b))


def _cv(samples: Sequence[float]) -> float:
    """变异系数 CV（均值非零；零均值返回无穷大视为极端机械）。"""
    mean = sum(samples) / len(samples)
    if mean == 0:
        return float("inf")
    variance = sum((x - mean) ** 2 for x in samples) / len(samples)
    return math.sqrt(variance) / mean


class HumannessEvaluator:
    """拟人度评估器：自身操作轨迹 vs 人工基线 → 统计检验 → 评分 → 处置。

    权重：interval_ks 0.55 / cps_hist 0.35 / mechanical 0.10（SR §6.4 权重表的
    demo 简化档——本评估仅含间隔与输入速度两要素 + M1 旗标）。
    """

    W_INTERVAL = 0.55
    W_CPS = 0.35
    W_MECHANICAL = 0.10

    def __init__(
        self,
        *,
        baseline: HumanBaseline | None = None,
        pass_th: float = 0.85,
        warn_th: float = 0.75,
        ks_d_max: float = 0.20,
        cv_floor: float = 0.25,
        buckets: int = 10,
        min_samples: int = 100,
        monte_carlo_n: int = 2000,
    ) -> None:
        if not (0.0 < warn_th < pass_th <= 1.0):
            raise ValueError("处置阈值非法：须 0 < warn_th < pass_th <= 1")
        self.baseline = baseline or HumanBaseline()
        self.pass_th = pass_th
        self.warn_th = warn_th
        self.ks_d_max = ks_d_max
        self.cv_floor = cv_floor
        self.buckets = buckets
        self.min_samples = min_samples
        self.monte_carlo_n = monte_carlo_n

    def evaluate(
        self,
        gaps_ms: Sequence[float],
        cps_samples: Sequence[float],
        *,
        rng: random.Random | None = None,
    ) -> HumannessReport:
        """评估主流程：小样本守卫 → KS → 直方图 → 旗标 → 评分 → 处置。

        :param gaps_ms: 操作间隔样本（毫秒，agent_action_trace.gap_prev_ms）；
        :param cps_samples: 输入速度样本（字符/秒）；
        :param rng: 基线蒙特卡洛随机源（注入即确定性可回归）。
        """
        rng = rng if rng is not None else random.Random(2026)
        if len(gaps_ms) < self.min_samples or len(cps_samples) < self.min_samples:
            # 防小样本误判：不评分（SR §6.4 样本 <min_eval_samples 不评分）
            return HumannessReport(score=0.0, ks_d=0.0, hist_similarity=0.0,
                                   gap_cv=0.0, flags=(), action="insufficient_sample")
        baseline_gaps = self.baseline.sample_gaps_ms(self.monte_carlo_n, rng)
        baseline_cps = self.baseline.sample_cps(self.monte_carlo_n, rng)
        # ① 间隔 KS 检验（简化版）：D 越小越拟人；D ≥ ks_d_max 该项 0 分
        ks_d = evaluate_gaps_ks(list(gaps_ms), baseline_gaps)
        s_ks = max(0.0, 1.0 - ks_d / self.ks_d_max)
        # ② 输入速度直方图相似度（归一化交集）
        s_cps = histogram_intersection(list(cps_samples), baseline_cps, self.buckets)
        # ③ 机械化特征旗标（M1：等间隔 CV < 0.25）
        gap_cv = _cv(list(gaps_ms))
        flags: tuple[str, ...] = ()
        if gap_cv < self.cv_floor:
            flags = ("constant_gap",)
        s_mech = 1.0 if not flags else 0.0
        score = round(
            self.W_INTERVAL * s_ks + self.W_CPS * s_cps + self.W_MECHANICAL * s_mech, 4
        )
        # ④ 处置三档：none / auto_tuned（轻偏差向基线收敛）/ frozen（先停后查）
        if score >= self.pass_th:
            action = "none"
        elif score >= self.warn_th:
            action = "auto_tuned"
        else:
            action = "frozen"
        return HumannessReport(score=score, ks_d=round(ks_d, 4),
                               hist_similarity=round(s_cps, 4), gap_cv=round(gap_cv, 4),
                               flags=flags, action=action)


__all__ = [
    "HumanBaseline",
    "HumannessReport",
    "HumannessEvaluator",
    "evaluate_gaps_ks",
    "histogram_intersection",
]
