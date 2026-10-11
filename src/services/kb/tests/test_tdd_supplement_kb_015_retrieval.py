# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""三段式检索（FR-KB-015 / SR-KB-015）TDD 补齐测试（五类场景补强，不改既有文件）。

按 TDD 新规五列审计既有 test_retrieval_* 后补齐：
- 边界：BudgetGuard 剩余预算恰等于需求（等号放行）/ 恰差 1ms / 已透支三端点
  （既有用例只覆盖 remaining+1 拒绝侧，未钉死等号放行）；
- 空集：空库端到端零命中 → hits=[]、kb_search_zero_hit_total 恰 1、无降级；
- 异常：filters 非白名单字段 → ValueError 原样上传（fail-closed，由 API 层
  映射 40001，不得被召回容错吞进降级链）；
- 并发：asyncio.gather 并发两路检索 → 结果互不串扰、kb_search_total 恰 2；
- 降级：向量化失败（判定点 C）→ 本地稀疏兜底链路仍出命中（embed_failed_local_sparse_fallback）。

KPI 映射：SRS §16.1「检索准确率 / 检索响应时延」——预算守卫的等号语义决定
500ms 线附近行为一致性；零命中与兜底链路决定「有答必答、无答不误报」。

依据文档：docs/07-实现方案/KB/SR-KB-015-三段式检索.md §6.1、§6.3、§6.4、§9。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from services.kb.core.retrieval import (
    BudgetGuard,
    MetricsRegistry,
    MultiRouteRecall,
    RetrievalPipeline,
    StageBudgets,
)
from services.kb.core.retrieval.rewriter import QueryRewriter
from services.kb.tests.test_retrieval_stubs import (
    FailingEmbedder,
    StubEmbedder,
    StubStore,
    chunk,
    run,
)


def build(
    *,
    embedder: Any = None,
    store: StubStore | None = None,
    total_budget_ms: float = 500.0,
) -> tuple[RetrievalPipeline, MetricsRegistry]:
    """最小被测流水线：与既有 build_pipeline 同构，返回 (pipeline, metrics)。"""
    metrics = MetricsRegistry()
    pipeline = RetrievalPipeline(
        rewriter=QueryRewriter(corrections={}, intent_templates=[]),
        recall=MultiRouteRecall(
            store if store is not None else StubStore(dense_hits=[chunk("d1", 0.9)]),
            embedder if embedder is not None else StubEmbedder(),
        ),
        metrics=metrics,
        total_budget_ms=total_budget_ms,
    )
    return pipeline, metrics


class TestBudgetGuardExactBoundary:
    """边界：allows 的等号语义（进入判定 >=）。"""

    def test_remaining_equals_need_allows(self) -> None:
        """剩余 == 需求：放行（既有用例只钉了 remaining+1 拒绝侧）。"""
        now = {"t": 0.0}
        guard = BudgetGuard(total_ms=100.0, budgets=StageBudgets(), timer=lambda: now["t"])
        assert guard.allows(100.0) is True  # 恰满：等号放行
        now["t"] = 0.1  # 已耗时 100ms → 剩余 0
        assert guard.remaining_ms() == 0.0
        assert guard.allows(0.0) is True  # 零开销段在恰好用尽时仍可进入
        assert guard.allows(0.1) is False  # 差 0.1ms 即拒绝

    def test_overdrawn_budget_allows_nothing(self) -> None:
        """透支（剩余为负）：任何非负需求一律拒绝（允许为负表示已超）。"""
        now = {"t": 0.0}
        guard = BudgetGuard(total_ms=50.0, budgets=StageBudgets(), timer=lambda: now["t"])
        now["t"] = 0.075  # 剩余 -25ms
        assert guard.remaining_ms() < 0
        assert guard.allows(0.0) is False


class TestZeroHitEndToEnd:
    """空集：空库检索零命中（200 语义的模块内形态）。"""

    def test_empty_store_zero_hit_no_degrade(self) -> None:
        pipeline, metrics = build(store=StubStore())  # 三路全空
        result = run(pipeline.search("完全无关的查询词", top_k=3))
        assert result.hits == []
        assert result.degrade == []  # 零命中不是降级
        assert result.route_stats == {"dense": 0, "sparse": 0, "fused": 0}
        snap = metrics.snapshot()["counters"]
        assert snap.get("kb_search_zero_hit_total") == 1.0
        assert snap.get("kb_search_total") == 1.0


class TestFilterFailClosed:
    """异常：非白名单过滤字段必须上抛（fail-closed），不得静默吞进降级链。"""

    def test_non_whitelisted_filter_propagates_valueerror(self) -> None:
        pipeline, _metrics = build()
        with pytest.raises(ValueError, match="非白名单字段"):
            run(pipeline.search("查询词", filters={"evil_field": ["x"]}))


class TestConcurrentSearches:
    """并发：同一流水线实例并发检索互不串扰。"""

    def test_two_concurrent_searches_isolated(self) -> None:
        store = StubStore(dense_hits=[chunk("d1", 0.9), chunk("d2", 0.8)])
        pipeline, metrics = build(store=store)

        async def gathered() -> list[Any]:
            return await asyncio.gather(
                pipeline.search("查询甲", top_k=1),
                pipeline.search("查询乙", top_k=2),
            )

        first, second = run(gathered())
        assert [h.chunk_id for h in first.hits] == ["d1"]  # top_k=1 截断
        assert [h.chunk_id for h in second.hits] == ["d1", "d2"]  # top_k=2 截断
        assert first.total_ms > 0 and second.total_ms > 0
        snap = metrics.snapshot()["counters"]
        assert snap.get("kb_search_total") == 2.0  # 两次请求独立打点
        assert len(store.calls) == 4  # 每次检索 dense+sparse 两路


class TestEmbedFailureLocalFallback:
    """降级：判定点 C——向量化失败 → 本地稀疏兜底，链路仍出命中。"""

    def test_embed_down_falls_back_to_local_sparse(self) -> None:
        store = StubStore(sparse_hits=[chunk("s1", 4.2), chunk("s2", 3.1)])
        pipeline, metrics = build(embedder=FailingEmbedder(), store=store)
        result = run(pipeline.search("云台支架参数", top_k=2))
        assert [h.chunk_id for h in result.hits] == ["s1", "s2"]  # 本地稀疏路的命中兜底
        assert any(d.startswith("embed_failed_local_sparse_fallback") for d in result.degrade)
        snap = metrics.snapshot()["counters"]
        assert snap.get('kb_search_degrade_total{reason="embed_failed_local_sparse_fallback"}') == 1.0
