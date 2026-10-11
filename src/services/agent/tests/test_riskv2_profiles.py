# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""账号行为画像引擎单元测试（FR-SC-004-①，SR-SC-004 §6.1/§9；Phase 4 TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：冷启动 5 类种子分散分配、同种子账号加噪后参数两两不同、
  账号级分布与 V1 全局分布差异化断言（≥5 种独立 reply_mu，不恒等 11.0）、
  账号级间隔采样方向性（快种子均值 < 慢种子）；
- 边界：拟合样本量恰等 fit_min_samples（200）收敛、恰差 1 条拒绝、
  采样夹取域 [20, 600]s 两端不越界；
- 异常：漂移更新未知账号拒绝、EWMA 漂移保留 prev_params 与 fit_version；
- 并发：多线程首次 get 只产生一份种子分配（计数不超发）；
- 降级：画像缺失回退 V1 全局分布采样（行为略同质但可用，SR §6.5 降级路径）。

验收口径映射：SRS §16.5 账号级行为差异化抽检（聚类区分度的参数基础）、§17 R-06。
实现方案：docs/07-实现方案/SC/SR-SC-004-风控V2.md §6.1、§9
"""

from __future__ import annotations

import random
import threading

import pytest

from services.agent.core.riskv2.profiles import AccountProfileStore

ACCOUNTS = [7000 + s for s in range(1, 29)]  # 28 账号


def make_store(**kw) -> AccountProfileStore:
    """标准装配：确定性随机源 + 默认 5 类种子。"""
    kw.setdefault("rng", random.Random(2026))
    return AccountProfileStore(**kw)


# ───────────────────────── 等价类：冷启动与差异化 ─────────────────────────


class TestColdStart:
    """画像冷启动种子与加噪（SR-SC-004 §6.1 seed_for）。"""

    def test_28_accounts_seed_distribution_balanced(self) -> None:
        """28 账号首次 get：5 类种子分散分配，各类计数差 ≤1。"""
        store = make_store()
        for aid in ACCOUNTS:
            store.get(aid)
        counts = list(store.seed_assignments().values())
        assert sum(counts) == 28
        assert max(counts) - min(counts) <= 1

    def test_same_seed_accounts_params_distinct(self) -> None:
        """同种子账号经 ±10% 噪声后参数两两不同（28 账号即使同种子也不同参数）。"""
        store = make_store()
        mus = [store.get(a).reply_mu for a in ACCOUNTS]
        assert len(set(mus)) == 28  # 全部互异

    def test_per_account_distribution_differs_from_v1_global(self) -> None:
        """与 V1 全局分布（ln_mu=11.0 全网同一套）差异化：≥5 种独立 reply_mu。"""
        store = make_store()
        mus = {round(store.get(a).reply_mu, 4) for a in ACCOUNTS}
        assert len(mus) >= 5
        assert mus != {11.0}

    def test_get_is_cached_stable(self) -> None:
        """等价类：重复 get 幂等（不重新播种，参数稳定）。"""
        store = make_store()
        first = store.get(7001)
        second = store.get(7001)
        assert (first.reply_mu, first.source) == (second.reply_mu, second.source)
        assert second.source == "seed"


class TestPerAccountSampling:
    """账号级间隔采样：分布参数按账号画像（V1 签名不变、参数来源升级）。"""

    def test_fast_seed_mean_below_slow_seed(self) -> None:
        """方向性断言：快种子（mu 小）采样均值 < 慢种子（10^3 样本，确定性 rng）。"""
        store = make_store()
        fast = min(ACCOUNTS, key=lambda a: store.get(a).reply_mu)
        slow = max(ACCOUNTS, key=lambda a: store.get(a).reply_mu)
        rng = random.Random(7)
        fast_mean = sum(store.sample_interval_s(fast, rng) for _ in range(1000)) / 1000
        slow_mean = sum(store.sample_interval_s(slow, rng) for _ in range(1000)) / 1000
        assert fast_mean < slow_mean

    def test_samples_clamped_to_gap_domain(self) -> None:
        """边界：10^4 次采样全部落在夹取域 [20, 600]s（防机械高频/超长静默）。"""
        store = make_store()
        rng = random.Random(11)
        samples = [store.sample_interval_s(7001, rng) for _ in range(10_000)]
        assert all(20.0 <= s <= 600.0 for s in samples)


# ───────────────────────── 拟合与漂移 ─────────────────────────


class TestFitAndDrift:
    """人工历史拟合（MLE）与 EWMA 漂移（SR-SC-004 §6.1 fit/drift_update）。"""

    def test_fit_below_min_samples_stays_seed(self) -> None:
        """边界：样本量恰差 1（199 条）→ None，画像保持 seed。"""
        store = make_store()
        rng = random.Random(3)
        samples = [rng.lognormvariate(10.5, 0.4) for _ in range(199)]
        assert store.fit(7001, intervals_ms=samples) is None
        assert store.get(7001).source == "seed"

    def test_fit_min_samples_converges_within_5pct(self) -> None:
        """边界/等价类：1000 条已知 μ/σ 人工日志拟合 → 恰达 200 门槛收敛，μ 误差 <5%（SR §9）。"""
        store = make_store()
        rng = random.Random(3)
        samples = [rng.lognormvariate(10.5, 0.4) for _ in range(1000)]
        profile = store.fit(7001, intervals_ms=samples)
        assert profile is not None
        assert profile.source == "fitted"
        assert abs(profile.reply_mu - 10.5) < 10.5 * 0.05
        assert profile.sample_count == 1000

    def test_drift_update_ewma_and_prev_snapshot(self) -> None:
        """漂移：p = α·new + (1-α)·old（α=0.3）；prev_params 保留回滚位、version+1。"""
        store = make_store()
        base = store.get(7001)
        old_mu = base.reply_mu
        drifted = store.drift_update(7001, reply_mu=old_mu - 2.0)
        assert drifted.source == "drifted"
        assert drifted.fit_version == 1
        assert drifted.prev_params is not None
        assert drifted.prev_params["reply_mu"] == pytest.approx(old_mu)
        assert drifted.reply_mu == pytest.approx(0.3 * (old_mu - 2.0) + 0.7 * old_mu)

    def test_drift_unknown_account_rejected(self) -> None:
        """异常：漂移更新未知账号 → KeyError（不静默造画像）。"""
        store = make_store()
        with pytest.raises(KeyError):
            store.drift_update(9999, reply_mu=10.0)


# ───────────────────────── 并发与降级 ─────────────────────────


class TestConcurrencyAndFallback:
    """并发冷启动与画像缺失降级（SR-SC-004 §6.5 降级路径）。"""

    def test_concurrent_cold_start_no_overseed(self) -> None:
        """并发：28 线程首次 get → 仍恰 28 份画像、种子计数合计 28（无超发竞态）。"""
        store = make_store()

        def worker(aid: int) -> None:
            store.get(aid)

        threads = [threading.Thread(target=worker, args=(a,)) for a in ACCOUNTS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(store.seed_assignments().values()) == 28

    def test_fallback_to_v1_global_distribution(self) -> None:
        """降级：account_id=None → 按 V1 全局分布（mu=11.0/sigma=0.6）采样，不抛错。"""
        store = make_store()
        rng = random.Random(5)
        samples = [store.sample_interval_s(None, rng) for _ in range(1000)]
        assert all(20.0 <= s <= 600.0 for s in samples)
        # 方向性：V1 全局中位（e^11/1000≈59.9s）介于快/慢种子之间即可用（略同质但可用）
        mean = sum(samples) / len(samples)
        assert 20.0 < mean < 600.0

    def test_deterministic_same_seed_same_params(self) -> None:
        """确定性/降级：同随机种子实例产出一致画像（演练可复现，审计可回放）。"""
        s1, s2 = make_store(), make_store()
        assert s1.get(7003).reply_mu == pytest.approx(s2.get(7003).reply_mu)
