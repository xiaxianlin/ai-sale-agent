# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""检索质量评测包（FR-KB-016 / SR-KB-016，迭代 0.3）。

模块组成（全部纯标准库，CLI 门禁 tools/regression 可独立引用）：
    dataset  评测集模型：EvalCase / EvalDataset（版本化、≥100 条容量校验、
             重复 case_id 拒绝、JSON 文件加载/保存）；
    runner   批量评测执行器：search_fn 鸭子类型注入（测试桩 / RetrievalPipeline
             包装），线程池并发（默认 8），单条结果 EvalCaseResult{hit@5, mrr,
             latency_ms}，异常降级为未命中计入分母；
    metrics  指标聚合：总体与分维度（kb_type × 自定义维度）Top5 命中率 / MRR /
             延迟分布（P50/P95）；未命中归因分类器接口（MissClassifier）+
             规则瀑布启发式桩（四类：分块/向量化/改写/知识缺失）；
    gate     回归门禁：evaluate_gate（任一维度较基线下降 >2pp 即 fail，
             恰好 2pp 通过，浮点容差防边界抖动；可选 90% 绝对门槛）；
    report   报告生成：中文 Markdown + xlsx（汇总/明细/未命中归因三 sheet，
             纯标准库 OOXML 写出器），两期对比（本期 vs 基线，pp 变化）。

指标与门禁口径与 04-测试文档 §2 完全一致；DB 表（eval_set/eval_case/
eval_run/eval_run_item）与评测集管理/报告下载端点随数据访问层迭代接入，
本迭代以文件 + 内存任务注册表承接同一契约。
"""
from .dataset import (
    DATASET_STATUS_FLOW,
    KB_TYPES,
    MIN_FROZEN_CASES,
    DuplicateCaseIdError,
    DatasetError,
    DatasetStateError,
    DatasetTooSmallError,
    EvalCase,
    EvalDataset,
)
from .gate import (
    EPS,
    GATE_FLOOR_PCT_DEFAULT,
    GATE_THRESHOLD_PP_DEFAULT,
    GateResult,
    evaluate_gate,
)
from .metrics import (
    ATTRIBUTION_CATEGORIES,
    ATTRIBUTION_FLOWBACK,
    ATTR_CHUNKING,
    ATTR_EMBEDDING,
    ATTR_KNOWLEDGE_MISSING,
    ATTR_REWRITE,
    TOP_K,
    AttributionContext,
    DimensionMetrics,
    LatencyStats,
    MissClassifier,
    RuleBasedMissClassifier,
    RunMetrics,
    aggregate,
    attribution_distribution,
    classify_misses,
    first_hit_rank,
    percentile,
)
from .report import (
    ATTRIBUTION_LABELS,
    CaseDetail,
    EvaluationReport,
    build_markdown,
    now_iso,
    write_markdown,
    write_xlsx,
)
from .runner import (
    DEFAULT_CONCURRENCY,
    EvalCaseResult,
    EvalRunner,
    SearchFn,
    parse_search_response,
)

__all__ = [
    "ATTRIBUTION_CATEGORIES",
    "ATTRIBUTION_FLOWBACK",
    "ATTRIBUTION_LABELS",
    "ATTR_CHUNKING",
    "ATTR_EMBEDDING",
    "ATTR_KNOWLEDGE_MISSING",
    "ATTR_REWRITE",
    "AttributionContext",
    "DATASET_STATUS_FLOW",
    "DEFAULT_CONCURRENCY",
    "DimensionMetrics",
    "DuplicateCaseIdError",
    "DatasetError",
    "DatasetStateError",
    "DatasetTooSmallError",
    "EPS",
    "EvalCase",
    "EvalCaseResult",
    "EvalDataset",
    "EvalRunner",
    "EvaluationReport",
    "GATE_FLOOR_PCT_DEFAULT",
    "GATE_THRESHOLD_PP_DEFAULT",
    "GateResult",
    "KB_TYPES",
    "LatencyStats",
    "MIN_FROZEN_CASES",
    "MissClassifier",
    "RuleBasedMissClassifier",
    "RunMetrics",
    "SearchFn",
    "TOP_K",
    "aggregate",
    "attribution_distribution",
    "build_markdown",
    "classify_misses",
    "evaluate_gate",
    "first_hit_rank",
    "now_iso",
    "parse_search_response",
    "percentile",
    "write_markdown",
    "write_xlsx",
]
