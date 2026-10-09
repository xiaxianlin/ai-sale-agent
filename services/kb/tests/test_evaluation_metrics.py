# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""指标聚合与未命中归因测试（FR-KB-016 / SR-KB-016 §9，迭代 0.3）。

覆盖：first_hit_rank 名次与边界、aggregate 手算对照（SR §9 金样本：3 命中
名次 1/2/5 + 2 未命中含 1 条超时 → top5_acc=0.6、mrr=0.34、超时计入分母）、
分维度聚合（kb_type × 自定义维度）、延迟分布手算、归因规则瀑布四类各一、
分布计数与序列化 round-trip。
"""
from __future__ import annotations

import pytest

from services.kb.core.evaluation.dataset import EvalCase
from services.kb.core.evaluation.metrics import (
    ATTR_CHUNKING,
    ATTR_EMBEDDING,
    ATTR_KNOWLEDGE_MISSING,
    ATTR_REWRITE,
    AttributionContext,
    RunMetrics,
    aggregate,
    attribution_distribution,
    classify_misses,
    first_hit_rank,
    percentile,
)
from services.kb.core.evaluation.runner import EvalCaseResult


def _case(case_id: str, kb_type: str = "product", dimensions: dict[str, str] | None = None) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        query="ST-3200 支架的最大承载和认证情况",
        expected_chunk_ids=(101,),
        kb_type=kb_type,
        dimensions=dimensions or {},
    )


def _hit(case_id: str, rank: int, latency_ms: int = 10) -> EvalCaseResult:
    """命中名次 rank 的结果（mrr=1/rank）。"""
    return EvalCaseResult.from_rank(case_id, rank, latency_ms, retrieved_ids=tuple(range(rank)))


def _miss(case_id: str, latency_ms: int = 10, error: str | None = None) -> EvalCaseResult:
    """未命中结果（超时口径：error 非空、latency 计 0 由调用方给定）。"""
    if error is not None:
        return EvalCaseResult.failed(case_id, error, latency_ms)
    return EvalCaseResult.from_rank(case_id, None, latency_ms)


# ─────────────────────────────── 命中名次 ───────────────────────────────


@pytest.mark.parametrize(
    "expected, retrieved, want",
    [
        ([7], [3, 7, 9, 1, 5], 2),          # SR §9 金样本：名次 2
        ([7], [3, 1, 9, 5, 2, 7], None),     # 第 6 位：窗口外未命中
        ([7, 9], [8, 4, 9], 3),              # 多期望块：任一命中取最先
        ([7], [], None),                     # 空结果
        ([7], [7, 7, 3], 1),                 # 重复块取首个名次
    ],
)
def test_first_hit_rank_名次与边界(expected: list[int], retrieved: list[int], want: int | None) -> None:
    """命中名次窗口判定（Top5 口径，1-based）。"""
    assert first_hit_rank(expected, retrieved) == want


# ─────────────────────────────── 指标聚合（SR §9 手算金样本）───────────────────────────────


def test_聚合手算对照_超时计入分母() -> None:
    """3 命中（名次 1/2/5）+ 2 未命中（其一超时）→ acc=0.6、mrr=0.34。"""
    results = [
        _hit("C1", 1), _hit("C2", 2), _hit("C3", 5),
        _miss("C4"), _miss("C5", error="TimeoutError: 检索链路超时"),
    ]
    cases = [_case(f"C{i}") for i in range(1, 6)]
    m = aggregate(results, cases)
    assert m.n == 5
    assert m.hit_count == 3
    assert m.top5_acc == pytest.approx(0.6)
    assert m.mrr == pytest.approx((1.0 + 0.5 + 0.2) / 5)  # = 0.34
    assert m.latency.mean_ms == pytest.approx(10.0)  # 5 条各 10ms（超时条目也计实测延迟）


def test_延迟分布手算() -> None:
    """latencies [10,20,30,40,50]：P50=30、P95=48（线性插值）、max=50、mean=30。"""
    results = [EvalCaseResult.from_rank(f"C{i}", 1, latency) for i, latency in
               enumerate([10, 20, 30, 40, 50], start=1)]
    m = aggregate(results, [_case(item.case_id) for item in results])
    assert m.latency.p50_ms == pytest.approx(30.0)
    assert m.latency.p95_ms == pytest.approx(48.0)
    assert m.latency.max_ms == 50.0
    assert m.latency.mean_ms == pytest.approx(30.0)
    assert percentile([10, 20, 30, 40, 50], 95) == pytest.approx(48.0)


def test_分维度聚合() -> None:
    """kb_type 组 + 自定义维度组各自聚合（组间同一结果可计入多组）。"""
    cases = [
        _case("C1", kb_type="product", dimensions={"scene": "询价"}),
        _case("C2", kb_type="product", dimensions={"scene": "询价"}),
        _case("C3", kb_type="process", dimensions={"scene": "售后"}),
        _case("C4", kb_type="process", dimensions={"scene": "售后"}),
    ]
    results = [_hit("C1", 1), _miss("C2"), _hit("C3", 4), _miss("C4")]
    m = aggregate(results, cases)

    assert set(m.per_dimension) == {"kb_type=product", "kb_type=process",
                                    "scene=询价", "scene=售后"}
    product = m.per_dimension["kb_type=product"]
    assert (product.n, product.hit_count) == (2, 1)
    assert product.top5_acc == pytest.approx(0.5)
    assert product.mrr == pytest.approx(0.5)  # (1.0 + 0) / 2
    inquiry = m.per_dimension["scene=询价"]
    assert (inquiry.n, inquiry.hit_count, inquiry.top5_acc) == (2, 1, 0.5)
    assert m.per_dimension["kb_type=process"].mrr == pytest.approx(0.125)  # 0.25 / 2


def test_未知用例装配错误显式抛错() -> None:
    """结果引用用例集外的 case_id → ValueError（调用方装配错误不静默吞没）。"""
    with pytest.raises(ValueError, match="C99"):
        aggregate([_hit("C99", 1)], [_case("C1")])


def test_RunMetrics序列化往返() -> None:
    """to_dict/from_dict round-trip（基线 JSON 契约）：总体与分维度逐项一致。"""
    results = [_hit("C1", 1), _miss("C2")]
    cases = [_case("C1"), _case("C2")]
    m = aggregate(results, cases)
    restored = RunMetrics.from_dict(m.to_dict())
    assert restored.top5_acc == pytest.approx(m.top5_acc)
    assert restored.n == m.n and restored.hit_count == m.hit_count
    assert restored.per_dimension["kb_type=product"].top5_acc == pytest.approx(0.5)

    with pytest.raises(ValueError, match="结构非法"):
        RunMetrics.from_dict({"top5_acc": "bad"})
    with pytest.raises(ValueError, match=r"\[0,1\]"):
        RunMetrics.from_dict({"n": 1, "top5_acc": 1.5})


# ─────────────────────────────── 未命中归因（规则瀑布四类）───────────────────────────────


def _unhit(case: EvalCase, retrieved: tuple[int, ...] = (), rewritten: str = "改写后的查询") -> EvalCaseResult:
    """未命中样本（携带归因判定所需观测：实际返回 + 改写文本）。"""
    return EvalCaseResult(
        case_id=case.case_id, hit_at_5=False, mrr=0.0, latency_ms=5,
        retrieved_ids=retrieved, rewritten_query=rewritten,
    )


def test_归因瀑布_知识缺失优先() -> None:
    """期望块不在已入库集合 → knowledge_missing（回流素材补充清单）。"""
    case = _case("C1")
    context = AttributionContext(indexed_chunk_ids=frozenset({202, 203}))
    attributions = classify_misses([_unhit(case)], [case], context=context)
    assert attributions["C1"] == ATTR_KNOWLEDGE_MISSING


def test_归因瀑布_改写丢关键实体() -> None:
    """改写结果丢失 SKU 关键实体（ST-3200）→ rewrite。"""
    case = _case("C1")
    context = AttributionContext(indexed_chunk_ids=frozenset({101}))
    sample = _unhit(case, rewritten="支架的最大承载情况（改写丢了型号）")
    assert classify_misses([sample], [case], context=context)["C1"] == ATTR_REWRITE


def test_归因瀑布_改写缺失即改写问题() -> None:
    """统一检索 API 未返回改写文本（rewritten_query 空）→ rewrite。"""
    case = _case("C1")
    context = AttributionContext(indexed_chunk_ids=frozenset({101}))
    sample = _unhit(case, rewritten="")
    assert classify_misses([sample], [case], context=context)["C1"] == ATTR_REWRITE


def test_归因瀑布_命中相邻块判分块问题() -> None:
    """TopN 返回中出现期望块的相邻块（语义被切散）→ chunking（改写正常前提下）。"""
    case = _case("C1")
    context = AttributionContext(
        indexed_chunk_ids=frozenset({101, 102}),
        neighbor_ids={101: frozenset({102})},
    )
    sample = _unhit(case, retrieved=(301, 102, 302), rewritten="ST-3200 支架最大承载 认证")
    assert classify_misses([sample], [case], context=context)["C1"] == ATTR_CHUNKING


def test_归因瀑布_兜底判向量化问题() -> None:
    """已入库、改写保留实体、邻居未命中 → embedding（表征不足兜底）。"""
    case = _case("C1")
    context = AttributionContext(
        indexed_chunk_ids=frozenset({101}),
        neighbor_ids={101: frozenset({102})},
    )
    sample = _unhit(case, retrieved=(301, 302), rewritten="ST-3200 支架最大承载与认证")
    assert classify_misses([sample], [case], context=context)["C1"] == ATTR_EMBEDDING


def test_语料可见性未知时跳过知识缺失判定() -> None:
    """indexed=None（未提供语料知识）→ 瀑布从规则②起判（本例落 rewrite）。"""
    case = _case("C1")
    sample = _unhit(case, rewritten="")
    assert classify_misses([sample], [case])["C1"] == ATTR_REWRITE


def test_归因仅针对未命中样本_分布恒全键() -> None:
    """命中样本不参与归因；分布计数四类恒全键（0 计保留，报告行序稳定）。"""
    cases = [_case("C1"), _case("C2"), _case("C3")]
    results = [_hit("C1", 1), _unhit(cases[1], rewritten=""), _unhit(cases[2], rewritten="")]
    attributions = classify_misses(results, cases)
    assert set(attributions) == {"C2", "C3"}
    distribution = attribution_distribution(attributions)
    assert set(distribution) == {ATTR_CHUNKING, ATTR_EMBEDDING, ATTR_REWRITE, ATTR_KNOWLEDGE_MISSING}
    assert distribution[ATTR_REWRITE] == 2
    assert distribution[ATTR_CHUNKING] == 0
