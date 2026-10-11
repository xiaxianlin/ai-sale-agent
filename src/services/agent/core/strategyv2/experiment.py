# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略 V2 A/B 分桶实验：账号组分桶 + 效果统计（FR-SC-003 重点，SR-SC-003 §6.5）。

- 账号组分桶（control/treatment 不重叠），对接 FR-SC-007 先 10 后 28 的灰度节奏；
- 配置解析链：running 实验处理组变体 > 常规 app_config（对照组走常规值）；
  仅策略键白名单可被实验覆盖（resolve 只作用于策略域键）；
- 效果统计：分桶聚合 n/成功数/率 + 双比例 z 检验（正态近似，无重依赖）；
  样本不足给「继续观察」而非结论（decision=continue）。

demo 形态：内存实验表与计数器（生产 = PG strategy_ab_experiment + strategy_stat_day 聚合）。
"""

from __future__ import annotations

import math
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

#: 指标白名单（strategy_ab_experiment.metric CHECK 口径）
METRICS: tuple[str, ...] = ("followup_reply_rate", "deal_rate", "winback_rate", "bargain_deal_rate")
#: 实验状态机
EXPERIMENT_STATUSES: tuple[str, ...] = ("draft", "running", "paused", "concluded")
#: 可被实验覆盖的策略键白名单前缀（仅策略域；集群/风控等键永不生效）
OVERRIDABLE_PREFIXES: tuple[str, ...] = ("strategy.",)


class ExperimentError(Exception):
    """实验域业务异常（40001 参数/校验类）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class AbExperiment:
    """实验行（strategy_ab_experiment 语义；buckets 落库快照防漂移）。"""

    exp_id: str
    name: str
    metric: str
    buckets: dict[str, tuple[int, ...]]
    variant_cfg: dict[str, dict[str, object]]
    status: str = "draft"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    conclusion: str | None = None


@dataclass
class _BucketCounter:
    """分桶计数器（n/成功数；会话键去重）。"""

    n: int = 0
    successes: int = 0
    seen_keys: set[str] = field(default_factory=set)


class AbSplitter:
    """A/B 分桶器（SR-SC-003 §6.5 工程级形态）。

    :param min_sample: 判定最小样本量（§7 strategy.ab.min_sample）；
    :param z_critical: 显著性临界值（默认 1.96，双尾 5%）；
    :param clock: 时钟注入。
    """

    def __init__(self, *, min_sample: int = 300, z_critical: float = 1.96,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._min_sample = min_sample
        self._z_critical = z_critical
        self._clock = clock or (lambda: datetime.now())
        self._lock = threading.RLock()
        self._experiments: dict[str, AbExperiment] = {}
        self._counters: dict[str, dict[str, _BucketCounter]] = {}

    # ---------------------------------------------------------------- 生命周期

    def create(self, name: str, metric: str, buckets: dict[str, tuple[int, ...]],
               variant_cfg: dict[str, dict[str, object]]) -> AbExperiment:
        """创建实验（draft）：分桶必须 control/treatment 且账号不重叠、双方非空。"""
        if metric not in METRICS:
            raise ExperimentError(40001, f"非法指标 {metric}（仅 {'/'.join(METRICS)}）")
        if set(buckets) != {"control", "treatment"}:
            raise ExperimentError(40001, "分桶必须为 control/treatment 两组")
        ids = [a for group in buckets.values() for a in group]
        if not buckets["control"] or not buckets["treatment"]:
            raise ExperimentError(40001, "对照组与处理组均不得为空")
        if len(ids) != len(set(ids)):
            raise ExperimentError(40001, "分桶账号不得重叠")
        exp_id = uuid.uuid4().hex[:8]
        experiment = AbExperiment(
            exp_id=exp_id, name=name, metric=metric,
            buckets={k: tuple(v) for k, v in buckets.items()},
            variant_cfg=variant_cfg,
        )
        with self._lock:
            self._experiments[exp_id] = experiment
            self._counters[exp_id] = {"control": _BucketCounter(), "treatment": _BucketCounter()}
        return experiment

    def start(self, exp_id: str) -> None:
        """draft/paused → running。"""
        self._transition(exp_id, "running")

    def pause(self, exp_id: str) -> None:
        """running → paused（变体立即失效）。"""
        self._transition(exp_id, "paused")

    def conclude(self, exp_id: str, conclusion: str) -> None:
        """写入结论并 concluded（审计链可查）。"""
        with self._lock:
            experiment = self._require(exp_id)
            experiment.status = "concluded"
            experiment.conclusion = conclusion
            experiment.ended_at = self._clock()

    def experiment_of(self, exp_id: str) -> AbExperiment:
        """按 id 取实验行；不存在抛 KeyError。"""
        with self._lock:
            return self._require(exp_id)

    # ---------------------------------------------------------------- 变体解析

    def resolve(self, key: str, *, account_id: int) -> object | None:
        """配置解析链：running 实验处理组命中白名单键 → 变体值；否则 None（走常规值）。"""
        if not key.startswith(OVERRIDABLE_PREFIXES):
            return None
        with self._lock:
            for experiment in self._experiments.values():
                if experiment.status != "running":
                    continue
                treatment_ids = experiment.buckets.get("treatment", ())
                if account_id in treatment_ids:
                    variants = experiment.variant_cfg.get("treatment", {})
                    if key in variants:
                        return variants[key]
        return None

    # ---------------------------------------------------------------- 效果统计

    def bucket_of(self, exp_id: str, account_id: int) -> str:
        """账号归属分桶；分桶外账号拒绝（不入组不计数）。"""
        experiment = self.experiment_of(exp_id)
        for group, ids in experiment.buckets.items():
            if account_id in ids:
                return group
        raise ExperimentError(40001, f"账号 {account_id} 不在实验分桶内")

    def record(self, exp_id: str, *, account_id: int, success: bool,
               conversation_key: str | None = None) -> bool:
        """记录一次结果（bucket 自动推断；会话键去重幂等，返回是否计入）。"""
        group = self.bucket_of(exp_id, account_id)
        with self._lock:
            counter = self._counters[exp_id][group]
            if conversation_key is not None:
                if conversation_key in counter.seen_keys:
                    return False
                counter.seen_keys.add(conversation_key)
            counter.n += 1
            if success:
                counter.successes += 1
            return True

    def report(self, exp_id: str) -> dict[str, object]:
        """分桶报告：两组 n/率 + 双比例 z 检验 + 判定（样本不足 → 继续观察）。"""
        with self._lock:
            self._require(exp_id)
            control = self._counters[exp_id]["control"]
            treatment = self._counters[exp_id]["treatment"]
            c_n, c_s, t_n, t_s = control.n, control.successes, treatment.n, treatment.successes
        c_rate = (c_s / c_n) if c_n else 0.0
        t_rate = (t_s / t_n) if t_n else 0.0
        z = _z_test_two_proportion(c_rate, c_n, t_rate, t_n)
        significant = bool(abs(z) >= self._z_critical and min(c_n, t_n) >= self._min_sample)
        return {
            "control": {"n": c_n, "successes": c_s, "rate": c_rate},
            "treatment": {"n": t_n, "successes": t_s, "rate": t_rate},
            "z": round(z, 3),
            "significant": significant,
            "decision": "significant" if significant else "continue",
        }

    # ---------------------------------------------------------------- 内部

    def _require(self, exp_id: str) -> AbExperiment:
        """取实验行或抛 KeyError。"""
        if exp_id not in self._experiments:
            raise KeyError(f"实验 {exp_id} 不存在")
        return self._experiments[exp_id]

    def _transition(self, exp_id: str, status: str) -> None:
        with self._lock:
            experiment = self._require(exp_id)
            if status == "running":
                experiment.started_at = experiment.started_at or self._clock()
            experiment.status = status


def _z_test_two_proportion(p1: float, n1: int, p2: float, n2: int) -> float:
    """双比例 z 检验（合并比例正态近似；零样本/零方差安全返回 0）。"""
    if n1 == 0 or n2 == 0:
        return 0.0
    pooled = (p1 * n1 + p2 * n2) / (n1 + n2)
    if pooled in (0.0, 1.0):
        return 0.0
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0.0:
        return 0.0
    return (p2 - p1) / se


__all__ = [
    "METRICS", "EXPERIMENT_STATUSES", "ExperimentError", "AbExperiment", "AbSplitter",
]
