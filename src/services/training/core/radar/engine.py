# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""能力雷达评估引擎（FR-TR-004，SR-TR-004 §6.2 聚合 / §6.3 校准 / §7 配置；迭代 1.3）。

模块职责（SR-TR-004「快照式聚合」关键决策的 demo 形态）：

- **四维口径**：product 产品知识 / communication 沟通技巧 / negotiation 议价能力 /
  platform 平台规则（SRS §6.2，与 ``trainee.radar`` 键一致）；
- **证据源聚合**：演练（exercise）/ 考试（exam）/ 导师问答（mentor_qa）三类源经
  :class:`EvidenceSource` 协议注入（demo 为 :class:`FakeEvidenceSource` 确定性桩，
  生产替换 drill/exam/tutor 存储适配器，协议不变）；
- **加权 + 指数时间衰减**：``w = 源权重 × 0.5^(证据年龄天数 / half_life_days)``，
  近期能力权重更高；权重矩阵与半衰期全配置化（NFR-MAI-02），构造期校验
  （每维三源权重和=1，半衰期 7~180 天，最小证据数 1~10）——非法配置拒绝构造
  （对应 SR §9「配置加载失败，启动期拒绝」）；
- **证据不足显式降级**：某维有效证据数 < ``min_evidence`` 时输出 ``score=None``
  （雷达留空标注），不得以 0 分或默认分填充，避免污染薄弱项判定与组卷（SR §2）；
- **快照落库**：每次计算全量重算并落 :class:`RadarSnapshot` 历史快照
  （trigger 五类：initial 入职初评 / event / daily / manual / stage），
  内存实现线程安全（生产为 PG ``radar_snapshot`` 表 + advisory lock，语义一致）；
- **一致性校准回路**：主管盲评对比（同一把尺）→ 维差 ≤15 且总分差 ≤10 判一致
  （对接 SR-TR-003 §7 演练校准口径）→ 一致率（KPI ≥85%，SRS §16.2）+
  分维下钻 + 偏差调整建议（人工审核后经配置变更生效，不自动改线上）。

依据文档：docs/07-实现方案/TR/SR-TR-004-能力评估系统.md §2、§6.2、§6.3、§7、§9；
        docs/03-技术文档/02-开发规范.md §3（中文注释 / 全量类型注解强制）。
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

# --------------------------------------------------------------------------- 四维口径常量（SR §6.2）

#: 雷达四维（与 trainee.radar 键、FR-TR-005 薄弱排序口径强一致）
DIMENSIONS: tuple[str, ...] = ("product", "communication", "negotiation", "platform")

#: 四维中文名（前端雷达 indicator 与薄弱徽标渲染基座）
DIM_LABELS: dict[str, str] = {
    "product": "产品知识",
    "communication": "沟通技巧",
    "negotiation": "议价能力",
    "platform": "平台规则",
}

#: 证据源枚举：演练 / 考试 / 导师问答（SR §6.1 三类源）
EVIDENCE_SOURCES: tuple[str, ...] = ("exercise", "exam", "mentor_qa")

#: 快照触发类型（SR §4 radar_snapshot.trigger CHECK 五类）
TRIGGERS: tuple[str, ...] = ("initial", "event", "daily", "manual", "stage")

#: 默认源权重矩阵（SR §6.1 各维三源权重，每维和=1）
DEFAULT_SOURCE_WEIGHTS: dict[str, dict[str, float]] = {
    "product": {"exercise": 0.35, "exam": 0.45, "mentor_qa": 0.20},
    "communication": {"exercise": 0.55, "exam": 0.20, "mentor_qa": 0.25},
    "negotiation": {"exercise": 0.60, "exam": 0.25, "mentor_qa": 0.15},
    "platform": {"exercise": 0.25, "exam": 0.55, "mentor_qa": 0.20},
}

#: 权重求和校验容差（浮点累加噪声防护）
WEIGHT_SUM_TOLERANCE: float = 1e-6

#: 一致判定：单维差阈值（对接 SR-TR-003 §7 tr.drill.consistency.dim_tolerance）
DEFAULT_DIM_TOLERANCE: int = 15
#: 一致判定：总分差阈值（SR-TR-003 §7 total_tolerance）
DEFAULT_TOTAL_TOLERANCE: int = 10
#: 一致率 KPI 阈值（SRS §16.2 ≥85%，勿改）
DEFAULT_CONSISTENCY_THRESHOLD: float = 85.0


# --------------------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class Evidence:
    """单条能力证据：某维某源的一次可追溯评分（score 已归一化百分制）。"""

    dim: str
    source: str  # exercise / exam / mentor_qa
    ref_id: str  # 源记录标识（演练结果/考卷/问答记录 id，供下钻追溯）
    score: float
    occurred_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "dim": self.dim,
            "source": self.source,
            "ref_id": self.ref_id,
            "score": self.score,
            "occurred_at": self.occurred_at.isoformat(),
        }


@dataclass(frozen=True)
class DimResult:
    """单维聚合结果：score=None 表示证据不足（显式降级，不填 0/默认分）。"""

    score: float | None
    n: int

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "n": self.n}


@dataclass(frozen=True)
class RadarConfig:
    """雷达配置（NFR-MAI-02 配置外置的进程内形态）：权重矩阵 + 半衰期 + 证据阈值。

    构造期即校验（非法配置根本无法构造——对应 SR §9「配置加载失败，启动期拒绝」）：
    - ``source_weights``：键恰为四维、每维键恰为三源、每维权重和=1（容差 1e-6）；
    - ``half_life_days``：7~180（SR §7 取值范围）；
    - ``min_evidence``：1~10；``trend_window_days``：30~365。
    """

    source_weights: dict[str, dict[str, float]] = field(
        default_factory=lambda: {dim: dict(weights) for dim, weights in DEFAULT_SOURCE_WEIGHTS.items()}
    )
    half_life_days: float = 30.0
    min_evidence: int = 3
    trend_window_days: int = 90

    def __post_init__(self) -> None:
        if set(self.source_weights) != set(DIMENSIONS):
            raise ValueError(
                f"权重矩阵维度口径非法：{sorted(self.source_weights)}，要求恰为 {list(DIMENSIONS)}"
            )
        for dim in DIMENSIONS:
            weights = self.source_weights[dim]
            if not isinstance(weights, dict) or set(weights) != set(EVIDENCE_SOURCES):
                raise ValueError(
                    f"维度 {dim} 的源权重键非法：{sorted(weights) if isinstance(weights, dict) else weights!r}，"
                    f"要求恰为 {list(EVIDENCE_SOURCES)}"
                )
            total = 0.0
            for source, value in weights.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
                    raise ValueError(f"维度 {dim} 源 {source} 权重非法（须为 0~1 数值）：{value!r}")
                total += float(value)
            if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
                raise ValueError(
                    f"维度 {dim} 三源权重之和必须为 1（实为 {total:.6f}），拒绝加载（NFR-MAI-02 校验）"
                )
        if isinstance(self.half_life_days, bool) or not isinstance(self.half_life_days, (int, float)):
            raise ValueError(f"half_life_days 须为数值，实为 {type(self.half_life_days).__name__}")
        if not 7 <= float(self.half_life_days) <= 180:
            raise ValueError(f"half_life_days 超出取值范围 7~180（SR §7）：{self.half_life_days!r}")
        if isinstance(self.min_evidence, bool) or not isinstance(self.min_evidence, int):
            raise ValueError(f"min_evidence 须为整数，实为 {type(self.min_evidence).__name__}")
        if not 1 <= self.min_evidence <= 10:
            raise ValueError(f"min_evidence 超出取值范围 1~10（SR §7）：{self.min_evidence!r}")
        if isinstance(self.trend_window_days, bool) or not isinstance(self.trend_window_days, int):
            raise ValueError(f"trend_window_days 须为整数，实为 {type(self.trend_window_days).__name__}")
        if not 30 <= self.trend_window_days <= 365:
            raise ValueError(f"trend_window_days 超出取值范围 30~365（SR §7）：{self.trend_window_days!r}")

    def digest(self) -> str:
        """配置摘要（快照 weights_used 落库，支撑复现与审计，SR §4/§8）。"""
        payload = json.dumps(
            {
                "weights": {dim: self.source_weights[dim] for dim in DIMENSIONS},
                "half_life_days": float(self.half_life_days),
                "min_evidence": self.min_evidence,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class RadarSnapshot:
    """雷达快照（历史序列单元）：一次计算的四维结果 + 所用配置摘要。"""

    snapshot_id: str
    trainee_id: int
    trigger: str  # initial / event / daily / manual / stage
    dims: dict[str, dict[str, Any]]  # {dim: {"score": float|None, "n": int}}
    weights_used: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "trainee_id": self.trainee_id,
            "trigger": self.trigger,
            "dims": {dim: dict(self.dims[dim]) for dim in DIMENSIONS},
            "weights_used": self.weights_used,
            "created_at": self.created_at.isoformat(),
        }


# --------------------------------------------------------------------------- 存储与证据源协议


class RadarSnapshotStore(Protocol):
    """雷达快照存储协议（demo 内存实现；生产替换 PG radar_snapshot 表形态）。"""

    def next_snapshot_id(self) -> str:
        """分配下一个快照 id（线程安全，全局唯一递增）。"""
        ...

    def save(self, snapshot: RadarSnapshot) -> None:
        """追加落库一份快照。"""
        ...

    def list_by_trainee(self, trainee_id: int) -> list[RadarSnapshot]:
        """按落库序返回该学员全部快照（升序历史序列）。"""
        ...


class InMemoryRadarSnapshotStore:
    """进程内内存实现（demo/测试）：锁保护取号与写入，按学员隔离存清单。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: dict[int, list[RadarSnapshot]] = {}
        self._seq = 0

    def next_snapshot_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"RS-{self._seq:04d}"

    def save(self, snapshot: RadarSnapshot) -> None:
        with self._lock:
            self._snapshots.setdefault(snapshot.trainee_id, []).append(snapshot)

    def list_by_trainee(self, trainee_id: int) -> list[RadarSnapshot]:
        with self._lock:
            return list(self._snapshots.get(trainee_id, []))


@runtime_checkable
class EvidenceSource(Protocol):
    """证据源协议：演练/考试/问答三类源各自实现 collect（依赖倒置接缝）。"""

    def collect(self, trainee_id: int) -> list[Evidence]:
        """采集指定学员的全部证据（实现方保证分数已归一化百分制）。"""
        ...


class FakeEvidenceSource:
    """确定性 Fake 证据源（demo/测试）：预置种子证据按学员隔离输出，不发起任何外部调用。

    ``add`` 供测试在两次计算之间追加证据（驱动「评估更新 → 重算」链路）；
    同一 seeds 字典可被多个源共享（按 source 标签区分权重归属）。
    """

    def __init__(self, source: str, seeds: Mapping[int, Sequence[Evidence]] | None = None) -> None:
        if source not in EVIDENCE_SOURCES:
            raise ValueError(f"证据源标签非法（须为 {list(EVIDENCE_SOURCES)}）：{source!r}")
        self.source = source
        self._seeds: dict[int, list[Evidence]] = {
            trainee_id: list(items) for trainee_id, items in (seeds or {}).items()
        }
        self._lock = threading.Lock()

    def add(self, trainee_id: int, evidence: Evidence) -> None:
        """追加一条证据（测试驱动评估更新的接缝）。"""
        if evidence.dim not in DIMENSIONS:
            raise ValueError(f"证据维度非法：{evidence.dim!r}（须为 {list(DIMENSIONS)}）")
        with self._lock:
            self._seeds.setdefault(trainee_id, []).append(evidence)

    def collect(self, trainee_id: int) -> list[Evidence]:
        with self._lock:
            seeds = self._seeds.get(trainee_id, [])
            return [evidence for evidence in seeds if evidence.source == self.source]


# --------------------------------------------------------------------------- 一致性校准数据模型


@dataclass(frozen=True)
class CalibrationPair:
    """一组盲评对比样本：同一份评估材料上 AI 四维分 vs 主管盲评四维分（同尺同料）。"""

    pair_id: str
    ai_scores: dict[str, float]
    supervisor_scores: dict[str, float]


@dataclass(frozen=True)
class CalibrationReport:
    """一致率报表（SRS §16.2 KPI ≥85% 证据）：整体一致率 + 分维下钻 + 偏差调整建议。"""

    samples: int
    consistent: int
    consistency_rate: float | None  # 无有效样本时不定义（None）
    threshold: float
    dim_tolerance: int
    total_tolerance: int
    by_dim: dict[str, dict[str, Any]]
    suggestions: tuple[str, ...]
    kpi_passed: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "consistent": self.consistent,
            "consistency_rate": self.consistency_rate,
            "threshold": self.threshold,
            "dim_tolerance": self.dim_tolerance,
            "total_tolerance": self.total_tolerance,
            "by_dim": {dim: dict(stats) for dim, stats in self.by_dim.items()},
            "suggestions": list(self.suggestions),
            "kpi_passed": self.kpi_passed,
        }


# --------------------------------------------------------------------------- 评估聚合引擎


class RadarEngine:
    """能力雷达评估引擎：证据采集 → 加权时间衰减聚合 → 快照落库 → 校准回路。

    并发口径：demo 单进程以引擎内锁串行化「取号→落库」临界区（保证历史序列 id 有序），
    生产为 PG advisory lock（SR §6.2），语义一致：并发重算产出有序且不丢快照。
    """

    def __init__(
        self,
        sources: Sequence[EvidenceSource],
        store: RadarSnapshotStore,
        *,
        config: RadarConfig | None = None,
        dim_tolerance: int = DEFAULT_DIM_TOLERANCE,
        total_tolerance: int = DEFAULT_TOTAL_TOLERANCE,
        consistency_threshold: float = DEFAULT_CONSISTENCY_THRESHOLD,
    ) -> None:
        self.sources = list(sources)
        self.store = store
        self.config = config if config is not None else RadarConfig()
        self.dim_tolerance = dim_tolerance
        self.total_tolerance = total_tolerance
        self.consistency_threshold = consistency_threshold
        self._lock = threading.Lock()

    # ── 计算与快照 ──────────────────────────────────────────────────────────
    def compute(
        self, trainee_id: int, *, trigger: str = "event", now: datetime | None = None
    ) -> RadarSnapshot:
        """全量重算并落快照：三类源证据采集 → 逐维加权衰减 → 快照落库（原子临界区）。"""
        if trigger not in TRIGGERS:
            raise ValueError(f"trigger 非法（须为 {list(TRIGGERS)}）：{trigger!r}")
        at = now if now is not None else datetime.now(timezone.utc)
        with self._lock:  # 生产为 pg_advisory_xact_lock(88001+tid)（SR §6.2），demo 等价语义
            evidence = self._collect(trainee_id)
            dims = {
                dim: self._weighted_score(
                    [item for item in evidence if item.dim == dim],
                    now=at,
                )
                for dim in DIMENSIONS
            }
            snapshot = RadarSnapshot(
                snapshot_id=self.store.next_snapshot_id(),
                trainee_id=trainee_id,
                trigger=trigger,
                dims={dim: result.to_dict() for dim, result in dims.items()},
                weights_used=self.config.digest(),
                created_at=at,
            )
            self.store.save(snapshot)
        return snapshot

    def _collect(self, trainee_id: int) -> list[Evidence]:
        """三类证据源聚合：过滤四维之外的杂维（防御口径不一致的脏数据）。"""
        items: list[Evidence] = []
        for source in self.sources:
            items.extend(item for item in source.collect(trainee_id) if item.dim in DIMENSIONS)
        return items

    def _weighted_score(self, items: list[Evidence], *, now: datetime) -> DimResult:
        """单维加权：w = 源权重 × 0.5^(年龄天数/半衰期)；证据不足 → score=None 显式降级。"""
        if len(items) < self.config.min_evidence:
            return DimResult(score=None, n=len(items))
        num = den = 0.0
        for item in items:
            age_days = max(0.0, (now - item.occurred_at).total_seconds() / 86400.0)  # 未来证据按 0 龄计
            weight = self.config.source_weights[item.dim].get(item.source, 0.0) * (
                0.5 ** (age_days / float(self.config.half_life_days))
            )
            num += weight * item.score
            den += weight
        if den <= 0.0:  # 全部证据权重为 0（配置错配）：与证据不足同口径降级，不产无效分
            return DimResult(score=None, n=len(items))
        return DimResult(score=round(num / den, 1), n=len(items))

    # ── 查询视图 ────────────────────────────────────────────────────────────
    def history(self, trainee_id: int) -> list[RadarSnapshot]:
        """历史快照序列（落库序升序）。"""
        return self.store.list_by_trainee(trainee_id)

    def latest(self, trainee_id: int) -> RadarSnapshot | None:
        """最近一次快照；无任何快照返回 None（API 层映射 TR-3001）。"""
        snapshots = self.history(trainee_id)
        return snapshots[-1] if snapshots else None

    def baseline(self, trainee_id: int) -> RadarSnapshot | None:
        """入职初评基线（首个 trigger=initial 快照，SRS §14.2 阶段对比锚点）。"""
        for snapshot in self.history(trainee_id):
            if snapshot.trigger == "initial":
                return snapshot
        return None

    def trend(
        self, trainee_id: int, *, now: datetime | None = None, window_days: int | None = None
    ) -> list[dict[str, Any]]:
        """趋势序列：窗口内快照的 {at, trigger, scores} 投影（默认窗口配置化，SR §7）。"""
        at = now if now is not None else datetime.now(timezone.utc)
        days = window_days if window_days is not None else self.config.trend_window_days
        since = at - timedelta(days=days)
        return [
            {
                "at": snapshot.created_at.isoformat(),
                "trigger": snapshot.trigger,
                "scores": {dim: snapshot.dims[dim]["score"] for dim in DIMENSIONS},
            }
            for snapshot in self.history(trainee_id)
            if snapshot.created_at >= since
        ]

    def radar_view(self, trainee_id: int, *, now: datetime | None = None) -> dict[str, Any] | None:
        """雷达查询视图（GET /radar/{tid} 的 data 段）：当前 + 初评基线 + 趋势一次返回。"""
        latest = self.latest(trainee_id)
        if latest is None:
            return None
        baseline = self.baseline(trainee_id)
        return {
            "radar": {dim: dict(latest.dims[dim]) for dim in DIMENSIONS},
            "baseline": baseline.to_dict() if baseline is not None else None,
            "trend": self.trend(trainee_id, now=now),
        }

    # ── 一致性校准回路（SR §6.3，KPI ≥85% 的测量与改进闭环）────────────────
    def calibrate(self, pairs: Sequence[CalibrationPair]) -> CalibrationReport:
        """盲评对比 → 一致判定（维差≤15 且总分差≤10，对接 SR-TR-003 §7）→ 一致率 + 偏差报告。

        建议仅供人工审核（经配置变更/rubric 评审生效，不自动改线上——SR §6.3 决策）。
        """
        validated = [self._validate_pair(pair) for pair in pairs]
        judged = [self._judge_pair(pair) for pair in validated]
        by_dim = self._by_dim_breakdown(validated)
        rate = round(sum(1.0 for c in judged if c) / len(judged) * 100, 1) if judged else None
        kpi_passed = None if rate is None else rate >= self.consistency_threshold
        return CalibrationReport(
            samples=len(judged),
            consistent=sum(1 for c in judged if c),
            consistency_rate=rate,
            threshold=self.consistency_threshold,
            dim_tolerance=self.dim_tolerance,
            total_tolerance=self.total_tolerance,
            by_dim=by_dim,
            suggestions=self._suggestions(rate, kpi_passed, by_dim),
            kpi_passed=kpi_passed,
        )

    @staticmethod
    def _validate_pair(pair: CalibrationPair) -> CalibrationPair:
        """校准样本校验：四维齐备、分值 0-100（主管与 AI 同尺同口径）。"""
        if not isinstance(pair.pair_id, str) or not pair.pair_id.strip():
            raise ValueError("校准样本 pair_id 不能为空")
        for name, scores in (("AI 评分", pair.ai_scores), ("主管盲评", pair.supervisor_scores)):
            if not isinstance(scores, dict) or set(scores) != set(DIMENSIONS):
                raise ValueError(
                    f"{name}维度必须与雷达四维齐备一致：{sorted(scores) if isinstance(scores, dict) else scores!r}"
                )
            for dim, value in scores.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
                    raise ValueError(f"{name}维度 {dim} 分值非法（须为 0-100 数值）：{value!r}")
        return pair

    def _judge_pair(self, pair: CalibrationPair) -> bool:
        """一致判定（drill 同尺）：各维差 ≤ dim_tolerance 且总分（均分）差 ≤ total_tolerance。"""
        dim_diffs = {
            dim: round(abs(pair.supervisor_scores[dim] - pair.ai_scores[dim]), 1) for dim in DIMENSIONS
        }
        supervisor_total = sum(pair.supervisor_scores.values()) / len(DIMENSIONS)
        ai_total = sum(pair.ai_scores.values()) / len(DIMENSIONS)
        total_diff = round(abs(supervisor_total - ai_total), 1)
        return max(dim_diffs.values()) <= self.dim_tolerance and total_diff <= self.total_tolerance

    def _by_dim_breakdown(self, pairs: list[CalibrationPair]) -> dict[str, dict[str, Any]]:
        """分维下钻：逐维判定（维差 ≤ dim_tolerance）计一致率 + 偏差均值（主管−AI，定方向）。"""
        breakdown: dict[str, dict[str, Any]] = {}
        for dim in DIMENSIONS:
            consistent = sum(
                1
                for pair in pairs
                if round(abs(pair.supervisor_scores[dim] - pair.ai_scores[dim]), 1) <= self.dim_tolerance
            )
            bias = (
                round(
                    sum(pair.supervisor_scores[dim] - pair.ai_scores[dim] for pair in pairs) / len(pairs), 1
                )
                if pairs
                else None
            )
            breakdown[dim] = {
                "samples": len(pairs),
                "consistent": consistent,
                "rate": round(consistent / len(pairs) * 100, 1) if pairs else None,
                "bias_mean": bias,
            }
        return breakdown

    def _suggestions(
        self, rate: float | None, kpi_passed: bool | None, by_dim: dict[str, dict[str, Any]]
    ) -> tuple[str, ...]:
        """偏差分析 → 调整建议（SR §6.3：整体达标性 + 分维系统性偏差定位）。"""
        if rate is None or kpi_passed is None:
            return ("暂无有效盲评样本，一致率不定义（主管盲评录入后重新生成报告）",)
        suggestions: list[str] = []
        if kpi_passed:
            suggestions.append(
                f"整体一致率 {rate}% 达到 KPI 阈值 {self.consistency_threshold}%（SRS §16.2），"
                "维持当前权重与 rubric 配置"
            )
        else:
            suggestions.append(
                f"整体一致率 {rate}% 低于 KPI 阈值 {self.consistency_threshold}%，本期不达标留痕："
                "建议收紧评分 Prompt 给分锚点并修订 rubric，偏差样本回流评分优化，下期复测"
            )
        for dim in DIMENSIONS:
            stats = by_dim[dim]
            dim_rate, bias = stats["rate"], stats["bias_mean"]
            if (
                dim_rate is not None
                and dim_rate < self.consistency_threshold
                and bias is not None
                and abs(bias) >= 1.0
            ):
                direction = "偏高" if bias < 0 else "偏低"  # bias=主管−AI：主管分更低 → AI 系统性偏高
                suggestions.append(
                    f"维度「{DIM_LABELS[dim]}」一致率 {dim_rate}% 低于阈值且 AI 系统性{direction}"
                    f"（主管−AI 均差 {bias:+.1f}），建议下调该维相关证据源权重或修订 rubric 条目"
                    "（人工审核后经配置变更生效）"
                )
        return tuple(suggestions)


__all__ = [
    "DIMENSIONS",
    "DIM_LABELS",
    "EVIDENCE_SOURCES",
    "TRIGGERS",
    "DEFAULT_SOURCE_WEIGHTS",
    "WEIGHT_SUM_TOLERANCE",
    "DEFAULT_DIM_TOLERANCE",
    "DEFAULT_TOTAL_TOLERANCE",
    "DEFAULT_CONSISTENCY_THRESHOLD",
    "Evidence",
    "DimResult",
    "RadarConfig",
    "RadarSnapshot",
    "RadarSnapshotStore",
    "InMemoryRadarSnapshotStore",
    "EvidenceSource",
    "FakeEvidenceSource",
    "CalibrationPair",
    "CalibrationReport",
    "RadarEngine",
]
