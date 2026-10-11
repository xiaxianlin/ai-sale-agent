# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""账号行为画像引擎（FR-SC-004-①，SR-SC-004 §6.1；账号级差异化参数源，demo 进程内形态）。

职责（风控 V2「一人一策」的参数基座）：
- **冷启动播种**（§6.1 seed_for）：5 类行为种子（快/中/慢节奏谱系）按轮转均衡分配，
  同种子账号再叠加 ±10% 连续噪声——28 账号即使同种子参数也两两互异（聚类区分度的
  参数基础，SRS §16.5 账号级行为差异化抽检）；
- **账号级间隔采样**：V1 签名不变（对数正态采样 + 夹取域 [20, 600]s），仅参数来源由
  「全网同一套 ln_mu=11.0」升级为账号画像（快种子均值 < 慢种子的方向性可断言）；
  画像缺失（account_id=None）回退 V1 全局分布（mu=11.0/sigma=0.6，略同质但可用）；
- **人工历史拟合**（§6.1 fit）：对数正态 MLE——样本量 ≥ fit_min_samples（默认 200）
  才收敛为 fitted 画像（恰差 1 条拒绝），μ/σ 取对数域均值/标准差；
- **EWMA 漂移**（§6.1 drift_update）：p = α·new + (1-α)·old（α=0.3），prev_params
  保留回滚位、fit_version 单调递增；未知账号拒绝（不静默造画像）。

demo 形态边界：画像表由内存字典承载（生产 = PG account_profile，写路径走配置变更
流程）；rng 注入保证演练可复现、审计可回放（确定性：同随机种子 → 同画像序列）。
"""

from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass

#: 冷启动行为种子谱系（ln 毫秒域：e^10.3/1000≈29.8s … e^11.7/1000≈121s，快→慢）
SEED_MUS: tuple[float, ...] = (10.30, 10.65, 11.00, 11.35, 11.70)

#: 种子档位默认形状参数（对数域标准差）
SEED_SIGMA = 0.40

#: EWMA 漂移平滑系数（SR-SC-004 §6.1：p = α·new + (1-α)·old）
DRIFT_ALPHA = 0.3

#: 种子参数加噪幅度（±10%：同种子账号参数两两互异的来源）
NOISE_RATIO = 0.10

#: V1 全局分布（降级回退口径：全网同一套 ln_mu=11.0 / sigma=0.6，e^11/1000≈59.9s）
V1_MU, V1_SIGMA = 11.0, 0.6

#: 间隔夹取域（秒：防机械高频 / 超长静默，SR-SC-004 §6.2）
GAP_MIN_S, GAP_MAX_S = 20.0, 600.0


@dataclass(frozen=True)
class AccountProfile:
    """账号行为画像行（account_profile 的进程内形态，参数为 ln 毫秒域）。"""

    account_id: int
    reply_mu: float                    # 间隔对数正态 μ（ln 毫秒）
    reply_sigma: float                 # 间隔对数正态 σ
    source: str                        # seed | fitted | drifted
    seed_id: int | None = None         # 行为种子档位（冷启动分配）
    fit_version: int = 0               # 画像版本（拟合/漂移单调递增）
    sample_count: int = 0              # 拟合样本量（fitted 画像溯源）
    prev_params: dict | None = None    # 漂移前快照（回滚位）


class AccountProfileStore:
    """账号画像存储：冷启动播种 → 人工拟合 → EWMA 漂移（线程安全的唯一事实源）。

    :param rng: 确定性随机源（同种子实例产出一致画像——演练可复现、审计可回放）；
    :param fit_min_samples: 拟合收敛最低样本量（SR §7，默认 200）；
    :param seed_class_count: 行为种子档位数（默认 5 类，轮转均衡分配）。
    """

    def __init__(
        self,
        *,
        rng: random.Random | None = None,
        fit_min_samples: int = 200,
        seed_class_count: int = len(SEED_MUS),
    ) -> None:
        self._rng = rng if rng is not None else random.Random(2026)
        self._fit_min = fit_min_samples
        self._seed_class_count = seed_class_count
        self._lock = threading.RLock()
        self._profiles: dict[int, AccountProfile] = {}
        self._seed_counter = 0               # 轮转分配游标（均衡的来源）
        self._seed_assignments: dict[int, int] = {}  # seed_id → 账号计数

    # ---- 读取（含冷启动） ----

    def get(self, account_id: int) -> AccountProfile:
        """读取画像；未登记账号冷启动播种（幂等：重复 get 参数稳定不重播）。"""
        with self._lock:
            profile = self._profiles.get(account_id)
            if profile is None:
                profile = self._cold_seed(account_id)
                self._profiles[account_id] = profile
            return profile

    def seed_assignments(self) -> dict[int, int]:
        """种子档位分配计数（冷启动均衡性断言：各类计数差 ≤1）。"""
        with self._lock:
            return dict(self._seed_assignments)

    # ---- 拟合与漂移 ----

    def fit(self, account_id: int, *, intervals_ms: list[float]) -> AccountProfile | None:
        """人工历史拟合（对数正态 MLE）：样本量恰达门槛收敛，μ 误差随样本量收缩。

        :returns: fitted 画像；样本量不足（< fit_min_samples）返回 None（画像保持 seed）。
        """
        samples = [x for x in intervals_ms if x > 0]  # 非正样本剔除（log 域无定义）
        if len(samples) < self._fit_min:
            return None
        logs = [math.log(x) for x in samples]
        mu = sum(logs) / len(logs)
        sigma = math.sqrt(sum((x - mu) ** 2 for x in logs) / len(logs))
        with self._lock:
            prev = self.get(account_id)
            profile = AccountProfile(
                account_id=account_id, reply_mu=mu, reply_sigma=sigma, source="fitted",
                seed_id=prev.seed_id, fit_version=prev.fit_version + 1,
                sample_count=len(samples),
                prev_params={"reply_mu": prev.reply_mu, "reply_sigma": prev.reply_sigma,
                             "source": prev.source})
            self._profiles[account_id] = profile
            return profile

    def drift_update(self, account_id: int, *, reply_mu: float,
                     reply_sigma: float | None = None) -> AccountProfile:
        """EWMA 漂移：p = α·new + (1-α)·old；prev_params 保留回滚位、版本 +1。

        :raises KeyError: 未登记账号拒绝（不静默造画像）。
        """
        with self._lock:
            if account_id not in self._profiles:
                raise KeyError(f"账号未登记画像，拒绝漂移更新：{account_id}")
            prev = self._profiles[account_id]
            new_sigma = (prev.reply_sigma if reply_sigma is None
                         else DRIFT_ALPHA * reply_sigma + (1 - DRIFT_ALPHA) * prev.reply_sigma)
            profile = AccountProfile(
                account_id=account_id,
                reply_mu=DRIFT_ALPHA * reply_mu + (1 - DRIFT_ALPHA) * prev.reply_mu,
                reply_sigma=new_sigma, source="drifted", seed_id=prev.seed_id,
                fit_version=prev.fit_version + 1, sample_count=prev.sample_count,
                prev_params={"reply_mu": prev.reply_mu, "reply_sigma": prev.reply_sigma,
                             "source": prev.source})
            self._profiles[account_id] = profile
            return profile

    # ---- 采样 ----

    def sample_interval_s(self, account_id: int | None, rng: random.Random) -> float:
        """账号级间隔采样（秒）：V1 签名不变（对数正态 + 夹取域），参数按画像。

        account_id=None 回退 V1 全局分布（画像缺失降级路径：略同质但可用）。
        """
        if account_id is None:
            mu, sigma = V1_MU, V1_SIGMA
        else:
            profile = self.get(account_id)
            mu, sigma = profile.reply_mu, profile.reply_sigma
        interval_s = rng.lognormvariate(mu, sigma) / 1000.0  # ln 域为毫秒 → 秒
        return min(max(interval_s, GAP_MIN_S), GAP_MAX_S)    # 夹取域防越界

    # ---- 内部 ----

    def _cold_seed(self, account_id: int) -> AccountProfile:
        """冷启动播种：种子档位轮转均衡分配 + ±10% 连续噪声（须持锁调用）。"""
        seed_id = self._seed_counter % self._seed_class_count
        self._seed_counter += 1
        self._seed_assignments[seed_id] = self._seed_assignments.get(seed_id, 0) + 1
        mu = SEED_MUS[seed_id] * (1.0 + self._rng.uniform(-NOISE_RATIO, NOISE_RATIO))
        sigma = SEED_SIGMA * (1.0 + self._rng.uniform(-NOISE_RATIO, NOISE_RATIO))
        return AccountProfile(account_id=account_id, reply_mu=mu, reply_sigma=sigma,
                              source="seed", seed_id=seed_id)
