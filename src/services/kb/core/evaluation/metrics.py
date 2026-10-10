# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""指标聚合与未命中归因（FR-KB-016 / SR-KB-016 §6.1、§6.3，迭代 0.3）。

指标口径与 04-测试文档 §2 完全一致：
- Top5 命中率为主指标（≥90% 达标，SRS §16.1），MRR 辅助观察；
- 单条调用失败的用例按未命中计入分母（保守口径，不允许靠剔除坏样本抬高指标）；
- 延迟逐条记录 latency_ms，聚合 P50/P95（NFR-PER-02 辅助观察）。

分维度聚合：按「kb_type + 自定义维度」分组（EvalCase.dimension_groups），
逐组给出 n / Top5 命中率 / MRR，支撑报告下钻与门禁的分维度回落判定。

未命中归因四类（04-测试文档 §2.4）：分块 chunking / 向量化 embedding /
查询改写 rewrite / 知识缺失 knowledge_missing。分类器经接口注入
（MissClassifier Protocol），本模块提供规则瀑布启发式桩实现
（RuleBasedMissClassifier）——生产接 DB 后换真实现，接口不变。
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .dataset import EvalCase

if TYPE_CHECKING:  # 仅类型标注引用，避免与 runner 的运行时单向依赖成环
    from .runner import EvalCaseResult

#: 命中判定窗口（SRS §16.1 Top5 口径，KB_EVAL_TOP_K=5，勿改）
TOP_K: int = 5

# ---- 未命中归因类别（四类口径同 04-测试文档 §2.4；展示序即报告行序）----

ATTR_CHUNKING = "chunking"
ATTR_EMBEDDING = "embedding"
ATTR_REWRITE = "rewrite"
ATTR_KNOWLEDGE_MISSING = "knowledge_missing"

#: 四类归因（顺序即报告展示序：分块/向量化/改写/知识缺失）
ATTRIBUTION_CATEGORIES: tuple[str, ...] = (
    ATTR_CHUNKING,
    ATTR_EMBEDDING,
    ATTR_REWRITE,
    ATTR_KNOWLEDGE_MISSING,
)

#: 各类归因的回流环节建议（报告「未命中归因分布」列，SR-KB-016 §6.5）
ATTRIBUTION_FLOWBACK: dict[str, str] = {
    ATTR_CHUNKING: "回流 FR-KB-012：调整分块参数与边界规则（语义被切散）",
    ATTR_EMBEDDING: "回流 FR-KB-013/015：检查向量化权重参数与重排配置（语义表征不足）",
    ATTR_REWRITE: "回流 FR-KB-015：修正改写提示词与纠错词表（关键实体丢失）",
    ATTR_KNOWLEDGE_MISSING: "回流素材补充清单：知识未入库/未发布/未向量化（R-02 应对）",
}


# --------------------------------------------------------------------------- 命中名次


def first_hit_rank(
    expected: Sequence[int], retrieved: Sequence[int], top_k: int = TOP_K
) -> int | None:
    """期望块在返回结果中首次出现的名次（1-based）；窗口内未出现返回 None。

    expected 为多块集合（多答案/同义块）：任一期望块命中即算命中，名次取最先者。
    """
    expected_set = set(expected)
    for rank, chunk_id in enumerate(retrieved[:top_k], start=1):
        if chunk_id in expected_set:
            return rank
    return None


# --------------------------------------------------------------------------- 聚合模型


def percentile(values: Sequence[float], q: float) -> float:
    """分位值（线性插值法，与 services/kb/api/metrics.py percentile 同口径）。

    本包不导入 API 层（保持零框架依赖供 CLI 独立复用），算法与之一致：
    无序入参内部排序；q 取 (0, 100]；空序列抛 ValueError（aggregate 已保证非空）。
    """
    if not values:
        raise ValueError("分位计算需要非空样本序列")
    if not 0 < q <= 100:
        raise ValueError(f"分位数 q 须在 (0, 100] 区间：{q}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = (len(ordered) - 1) * q / 100.0
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return float(ordered[lo])
    frac = idx - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


@dataclass(frozen=True)
class LatencyStats:
    """延迟分布（毫秒）：P50/P95 辅助观察（NFR-PER-02）+ 均值/最大值。"""

    p50_ms: float = 0.0
    p95_ms: float = 0.0
    mean_ms: float = 0.0
    max_ms: float = 0.0

    def to_dict(self) -> dict[str, float]:
        """序列化（基线 JSON / 报告数据口径）。"""
        return {
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "mean_ms": self.mean_ms,
            "max_ms": self.max_ms,
        }


@dataclass(frozen=True)
class DimensionMetrics:
    """分维度指标（维度键形如 kb_type=product / scene=询价）。"""

    key: str
    n: int
    hit_count: int
    top5_acc: float
    mrr: float

    def to_dict(self) -> dict[str, float | int | str]:
        """序列化（基线 JSON / 报告数据口径）。"""
        return {
            "key": self.key,
            "n": self.n,
            "hit_count": self.hit_count,
            "top5_acc": self.top5_acc,
            "mrr": self.mrr,
        }


@dataclass(frozen=True)
class RunMetrics:
    """一次批量评测的聚合指标（总体 + 分维度 + 延迟分布）。

    基线 JSON 即本模型的 to_dict 形态——回归门禁（gate.py）与两期对比
    （report.py）直接消费同一结构。
    """

    n: int
    hit_count: int
    top5_acc: float
    mrr: float
    latency: LatencyStats = field(default_factory=LatencyStats)
    per_dimension: dict[str, DimensionMetrics] = field(default_factory=dict)

    def to_dict(self) -> dict[str, float | int | dict]:
        """序列化（基线 JSON 的顶层结构）。"""
        return {
            "n": self.n,
            "hit_count": self.hit_count,
            "top5_acc": self.top5_acc,
            "mrr": self.mrr,
            "latency": self.latency.to_dict(),
            "per_dimension": {key: dim.to_dict() for key, dim in self.per_dimension.items()},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "RunMetrics":
        """反序列化（基线 JSON → 门禁比对；轻量校验，容错缺省 latency/per_dimension）。"""
        try:
            top5_acc = float(data["top5_acc"])  # type: ignore[arg-type]
            mrr = float(data.get("mrr", 0.0))  # type: ignore[arg-type]
            n = int(data["n"])  # type: ignore[arg-type]
            hit_count = int(data.get("hit_count", 0))  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"基线指标结构非法（须含 n/top5_acc 数值字段）: {exc}") from exc
        if not 0.0 <= top5_acc <= 1.0:
            raise ValueError(f"基线 top5_acc 须在 [0,1]: {top5_acc}")
        latency_raw = data.get("latency") or {}
        latency = LatencyStats(
            **{key: float(latency_raw[key]) for key in ("p50_ms", "p95_ms", "mean_ms", "max_ms")
               if isinstance(latency_raw, Mapping) and key in latency_raw}
        )
        dims_raw = data.get("per_dimension") or {}
        per_dimension: dict[str, DimensionMetrics] = {}
        if isinstance(dims_raw, Mapping):
            for key, dim in dims_raw.items():
                if not isinstance(dim, Mapping):
                    continue
                per_dimension[str(key)] = DimensionMetrics(
                    key=str(key),
                    n=int(dim.get("n", 0)),
                    hit_count=int(dim.get("hit_count", 0)),
                    top5_acc=float(dim.get("top5_acc", 0.0)),
                    mrr=float(dim.get("mrr", 0.0)),
                )
        return cls(
            n=n, hit_count=hit_count, top5_acc=top5_acc, mrr=mrr,
            latency=latency, per_dimension=per_dimension,
        )


def aggregate(results: Sequence["EvalCaseResult"], cases: Sequence[EvalCase]) -> RunMetrics:
    """聚合单条结果 → 总体 + 分维度 Top5 命中率 / MRR + 延迟分布。

    - 分母恒为 len(results)（失败/超时条目已按未命中计入，不允许剔除）；
    - 分组键来自用例的 dimension_groups（kb_type + 自定义维度），用例与结果按
      case_id 关联，未知 case_id 属调用方装配错误，显式抛 ValueError；
    - latency_ms 缺失（失败条目）按 0 计入延迟样本（与 SR §6.1 口径一致）。
    """
    if not results:
        return RunMetrics(n=0, hit_count=0, top5_acc=0.0, mrr=0.0)
    case_by_id = {case.case_id: case for case in cases}
    n = len(results)
    hit_count = sum(1 for item in results if item.hit_at_5)
    top5_acc = hit_count / n
    mrr = sum(item.mrr for item in results) / n

    grouped: dict[str, list["EvalCaseResult"]] = {}
    for item in results:
        case = case_by_id.get(item.case_id)
        if case is None:
            raise ValueError(f"评测结果引用了未知用例: {item.case_id!r}（结果与用例集不匹配）")
        for group in case.dimension_groups():
            grouped.setdefault(group, []).append(item)

    per_dimension = {
        key: DimensionMetrics(
            key=key,
            n=len(items),
            hit_count=sum(1 for item in items if item.hit_at_5),
            top5_acc=sum(1 for item in items if item.hit_at_5) / len(items),
            mrr=sum(item.mrr for item in items) / len(items),
        )
        for key, items in sorted(grouped.items())
    }
    latencies = [float(item.latency_ms or 0) for item in results]
    latency = LatencyStats(
        p50_ms=percentile(latencies, 50),
        p95_ms=percentile(latencies, 95),
        mean_ms=sum(latencies) / n,
        max_ms=max(latencies),
    )
    return RunMetrics(
        n=n, hit_count=hit_count, top5_acc=top5_acc, mrr=mrr,
        latency=latency, per_dimension=per_dimension,
    )


# --------------------------------------------------------------------------- 未命中归因

#: 关键实体近似抽取（受控词表的轻量替代）：含数字的字母数字串（SKU/型号，如
#: ST-3200、304）与 ≥2 字符的全大写词（认证/贸易词，如 CE、MOQ）
_ENTITY_TOKEN_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/]*")


def _key_entities(query: str) -> list[str]:
    """从原始 query 抽取关键实体（改写丢实体判定的比对基准，启发式）。"""
    keys: list[str] = []
    for token in _ENTITY_TOKEN_RE.findall(query):
        if any(char.isdigit() for char in token) or (token.isupper() and len(token) >= 2):
            keys.append(token)
    return keys


def _loses_key_entities(query: str, rewritten: str) -> bool:
    """改写结果是否丢失 query 关键实体（大小写不敏感的包含判定）。"""
    rewritten_lower = rewritten.lower()
    return any(key.lower() not in rewritten_lower for key in _key_entities(query))


@dataclass(frozen=True)
class AttributionContext:
    """归因判定的上下文知识（迭代 0.3 由调用方显式供给；生产接 DB 回查）。

    - indexed_chunk_ids：当前已向量化入库的 chunk 主键集合；None 表示语料可见性
      未知（跳过 knowledge_missing 判定），空集表示已知全缺（全部命中该类）；
    - neighbor_ids：chunk → 同条目相邻块集合（分块切散判定的依据，FR-KB-012 口径）。
    """

    indexed_chunk_ids: frozenset[int] | None = None
    neighbor_ids: Mapping[int, frozenset[int]] = field(default_factory=dict)


@runtime_checkable
class MissClassifier(Protocol):
    """未命中归因分类器接口（生产实现接 DB 回查，桩为规则瀑布启发式）。"""

    def classify(
        self, result: "EvalCaseResult", case: EvalCase, context: AttributionContext
    ) -> str:
        """对一条未命中样本返回四类归因之一（ATTRIBUTION_CATEGORIES 取值）。"""
        ...  # pragma: no cover - Protocol 桩


class RuleBasedMissClassifier:
    """规则瀑布启发式桩（SR-KB-016 §6.3 四类口径，顺序即优先级）：

    ① knowledge_missing：任一期望块不在已入库集合（未入库/未发布/回滚下线）；
    ② rewrite：改写结果缺失或丢失 query 关键实体（SKU/型号/认证词近似抽取比对）；
    ③ chunking：实际返回命中期望块的相邻块（语义被切散：邻居含答案，目标块不含）；
    ④ embedding：已入库、改写正常、邻居未命中 → 语义表征/相似度不足（兜底）。

    置信度低的模糊情形由 ④ 兜底承接；生产版本对 loses_key_entities 低置信样本
    返回 pending 人工复核，属 DB 接入后的增强，桩不引入第五类取值。
    """

    def classify(
        self, result: "EvalCaseResult", case: EvalCase, context: AttributionContext
    ) -> str:
        """按瀑布顺序判定（首个命中的规则即返回，规则间互斥）。"""
        # ① 知识缺失：期望块不存在 / 未向量化入库（含回滚下线，FR-KB-022）
        if context.indexed_chunk_ids is not None:
            if any(cid not in context.indexed_chunk_ids for cid in case.expected_chunk_ids):
                return ATTR_KNOWLEDGE_MISSING
        # ② 查询改写问题：改写缺失，或改写文本丢了 query 的关键实体
        rewritten = (result.rewritten_query or "").strip()
        if not rewritten or _loses_key_entities(case.query, rewritten):
            return ATTR_REWRITE
        # ③ 分块问题：TopN 返回中出现期望块的相邻块（答案被切到邻居里）
        neighbors: set[int] = set()
        for cid in case.expected_chunk_ids:
            neighbors.update(context.neighbor_ids.get(cid, ()))
        if neighbors.intersection(result.retrieved_ids):
            return ATTR_CHUNKING
        # ④ 向量化问题：入库、改写正常、邻居未命中 → 表征不足
        return ATTR_EMBEDDING


def classify_misses(
    results: Sequence["EvalCaseResult"],
    cases: Sequence[EvalCase],
    classifier: MissClassifier | None = None,
    context: AttributionContext | None = None,
) -> dict[str, str]:
    """对全部未命中样本执行归因，返回 case_id → 归因类别（命中样本不参与）。

    classifier 缺省用规则瀑布桩；context 缺省为空上下文（语料可见性未知）。
    """
    if classifier is None:
        classifier = RuleBasedMissClassifier()
    if context is None:
        context = AttributionContext()
    case_by_id = {case.case_id: case for case in cases}
    attributions: dict[str, str] = {}
    for item in results:
        if item.hit_at_5:
            continue
        case = case_by_id.get(item.case_id)
        if case is None:
            continue  # 与 aggregate 同一装配假设：未知用例属调用方错误，聚合层已显式抛错
        attributions[item.case_id] = classifier.classify(item, case, context)
    return attributions


def attribution_distribution(attributions: Mapping[str, str]) -> dict[str, int]:
    """归因分布计数：四类恒全键（0 计也保留），报告行序稳定。"""
    counts = {category: 0 for category in ATTRIBUTION_CATEGORIES}
    for category in attributions.values():
        if category in counts:
            counts[category] += 1
    return counts
