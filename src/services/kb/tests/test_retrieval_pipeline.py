# 知识产权归甲方所有（COM-IP-01）
"""三段编排单元测试（SR-KB-015 §9 / SR-NFR-PER-02 §6.3）：预算守卫降级链 / 分段计时 / 指标打点。

注：先导入被测模块再 import httpx（reranker 模块头部完成标准库 platform 自愈）。
"""
from __future__ import annotations

from typing import Any

from services.kb.core.retrieval import (
    DEGRADE_OVER_TOTAL,
    DEGRADE_RERANK_FAILED,
    DEGRADE_SKIP_LLM_REWRITE,
    DEGRADE_SKIP_RERANK,
    BudgetGuard,
    BgeRerankerClient,
    MetricsRegistry,
    MultiRouteRecall,
    QueryRewriter,
    RetrievalPipeline,
    StageBudgets,
)
from services.kb.core.retrieval.rewriter import SOURCE_RULE, SOURCE_RULE_BUDGET_SKIP
from services.kb.tests.test_retrieval_stubs import (
    FakeReranker,
    SlowEmbedder,
    StubEmbedder,
    StubStore,
    chunk,
    run,
)

import httpx  # noqa: E402 —— 须位于被测模块导入（platform 自愈）之后
from foundation.net.egress import EgressDestination, EgressGuard  # noqa: E402


def _tei_guard() -> EgressGuard:
    """构造已注册本地 TEI 内网目的地的守卫（CR S3 后 BgeRerankerClient 必经守卫）。"""
    guard = EgressGuard()
    guard.register(
        EgressDestination(
            code="tei_rerank", host="tei-rerank", port=8081,
            schemes=("http",), purposes=("rerank",), owner="ai-sale-agent",
        )
    )
    return guard


def build_pipeline(
    *,
    embedder: Any = None,
    store: StubStore | None = None,
    reranker: Any = None,
    metrics: MetricsRegistry | None = None,
    rewriter: QueryRewriter | None = None,
    total_budget_ms: float = 500.0,
) -> tuple[RetrievalPipeline, FakeReranker, MetricsRegistry]:
    """组装被测流水线：默认快桩 + 可注入慢桩/故障桩/指标。"""
    embedder = embedder if embedder is not None else StubEmbedder()
    store = store if store is not None else StubStore(
        dense_hits=[chunk("d1", 0.9), chunk("d2", 0.8), chunk("d3", 0.7)],
        sparse_hits=[chunk("s1", 5.0), chunk("d1", 4.0)],
    )
    fake_reranker = reranker if reranker is not None else FakeReranker()
    metrics = metrics if metrics is not None else MetricsRegistry()
    rewriter = rewriter if rewriter is not None else QueryRewriter(corrections={}, intent_templates=[])
    pipeline = RetrievalPipeline(
        rewriter=rewriter,
        recall=MultiRouteRecall(store, embedder),
        reranker=fake_reranker,
        metrics=metrics,
        total_budget_ms=total_budget_ms,
    )
    return pipeline, fake_reranker, metrics


class TestNormalChain:
    """正常主链路：改写→召回→重排→组装，字段与分段计时齐全。"""

    def test_full_chain_fields(self) -> None:
        rewriter = QueryRewriter(corrections={}, intent_templates=[])
        rewriter.register_alias("ST32OO", "ST-3200")
        pipeline, reranker, _ = build_pipeline(rewriter=rewriter)
        result = run(pipeline.search("ST32OO 支架", top_k=2))

        assert "ST-3200" in result.rewritten_query  # 改写留痕上浮
        assert result.rewrite_source == SOURCE_RULE
        assert any(a.startswith("alias:") for a in result.rewrite_actions)
        assert result.slots.get("sku") == ["ST-3200"]
        assert result.route_stats["dense"] == 3  # 召回统计上浮
        assert len(reranker.calls) == 1 and reranker.calls[0]["top_k"] == 2  # Top50→Top2
        # RRF 融合序为 [d1, s1, d2, d3]（d1 双路命中居首），FakeReranker 倒序取 Top2
        assert [h.chunk_id for h in result.hits] == ["d3", "d2"]
        assert all(h.rerank_score is not None for h in result.hits)
        assert set(result.stage_ms) == {"rewrite", "recall", "rerank", "assemble", "total"}
        assert result.degrade == []
        assert result.total_ms > 0 and not result.cache_hit  # 缓存层后续迭代占位

    def test_rerank_false_skips_stage_three(self) -> None:
        pipeline, reranker, _ = build_pipeline()
        result = run(pipeline.search("云台支架", top_k=2, rerank=False))
        assert reranker.calls == []  # 第③段未发起
        assert [h.chunk_id for h in result.hits] == ["d1", "s1"]  # RRF 序直出 TopK
        assert result.stage_ms["rerank"] == 0.0
        assert result.degrade == []  # 主动关闭不算降级


class TestBudgetDegradeLadder:
    """预算守卫降级阶梯：先跳过重排（B）→ 再跳过改写 LLM（A2）→ 超总量观测。"""

    def test_slow_embedder_skips_rerank_first(self) -> None:
        # 判定点 B：慢向量化（召回段 400ms）→ 剩余不足「重排+组装」→ 跳过重排
        pipeline, reranker, metrics = build_pipeline(embedder=SlowEmbedder(delay_s=0.4))
        result = run(pipeline.search("云台支架", top_k=2))
        assert reranker.calls == []  # 重排未发起
        assert [h.chunk_id for h in result.hits] == ["d1", "s1"]  # RRF 序直出
        assert len(result.degrade) == 1 and result.degrade[0].startswith(DEGRADE_SKIP_RERANK)
        assert "remaining_ms=" in result.degrade[0]  # 原因留痕含预算明细
        assert result.stage_ms["total"] > 400
        snap = metrics.snapshot()["counters"]
        assert snap.get(f'kb_search_degrade_total{{reason="{DEGRADE_SKIP_RERANK}"}}') == 1.0

    def test_tight_budget_skips_llm_rewrite_keeps_rerank(self) -> None:
        # 判定点 A2：总预算 300ms < 改写80+召回250+组装20 → LLM 不进，规则改写直出
        hook_calls: list[str] = []

        async def llm_hook(text: str) -> str:
            hook_calls.append(text)
            return "不应被采用"

        rewriter = QueryRewriter(corrections={}, intent_templates=[], llm_hook=llm_hook)
        pipeline, reranker, metrics = build_pipeline(rewriter=rewriter, total_budget_ms=300.0)
        result = run(pipeline.search("云台支架", top_k=2))
        assert hook_calls == []  # 钩子未调用（省下 LLM 段预算）
        assert result.rewrite_source == SOURCE_RULE_BUDGET_SKIP
        assert any(d.startswith(DEGRADE_SKIP_LLM_REWRITE) for d in result.degrade)
        assert len(reranker.calls) == 1  # 剩余 ~300ms ≥ 170ms：重排保留（阶梯序：先弃重排才轮到改写）
        assert result.hits  # 链路仍返回
        snap = metrics.snapshot()["counters"]
        assert snap.get(f'kb_search_degrade_total{{reason="{DEGRADE_SKIP_LLM_REWRITE}"}}') == 1.0

    def test_over_total_budget_recorded(self) -> None:
        # 召回段 600ms：跳过重排之外，全链超总预算记 over_total（50002 前兆口径）
        pipeline, reranker, _ = build_pipeline(embedder=SlowEmbedder(delay_s=0.6))
        result = run(pipeline.search("云台支架", top_k=2))
        tokens = [d.split("(", 1)[0] for d in result.degrade]
        assert tokens == [DEGRADE_SKIP_RERANK, DEGRADE_OVER_TOTAL]
        assert result.stage_ms["total"] > 500

    def test_budget_guard_semantics(self) -> None:
        # BudgetGuard 单元语义：剩余预算随时间递减，allows 按需判定
        guard = BudgetGuard(total_ms=100.0, budgets=StageBudgets())
        assert guard.remaining_ms() <= 100.0
        assert guard.allows(50.0) is True
        assert guard.allows(guard.remaining_ms() + 1) is False


class TestRerankerDegradeDetection:
    """重排器降级识别（判定点 D）：rerank_score 未写回 → 流水线 degrade 链记录。"""

    def test_unreachable_tei_detected_by_pipeline(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("tei-reranker 不可达")

        reasons: list[str] = []
        reranker = BgeRerankerClient(
            base_url="http://tei-rerank:8081",
            timeout_ms=50.0,
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            on_degrade=reasons.append,
            guard=_tei_guard(),
        )
        pipeline, _, metrics = build_pipeline(reranker=reranker)
        result = run(pipeline.search("云台支架", top_k=2))
        assert reasons == ["rerank_unreachable"]  # 客户端回调留痕
        assert any(d.startswith(DEGRADE_RERANK_FAILED) for d in result.degrade)  # 流水线链留痕
        assert [h.chunk_id for h in result.hits] == ["d1", "s1"]  # RRF 序兜底
        assert all(h.rerank_score is None for h in result.hits)
        snap = metrics.snapshot()["counters"]
        assert snap.get(f'kb_search_degrade_total{{reason="{DEGRADE_RERANK_FAILED}"}}') == 1.0


class TestMetricsCompleteness:
    """指标打点完整性（§6.5）：计数 / 各段直方图 / 全链直方图 / 零命中。"""

    def test_normal_search_metrics(self) -> None:
        pipeline, _, metrics = build_pipeline()
        run(pipeline.search("云台支架", top_k=2))
        snap = metrics.snapshot()
        assert snap["counters"]["kb_search_total"] == 1.0
        # 各段直方图恰好观测一次（含 total 段的段名集）
        for stage in ("rewrite", "recall", "rerank", "assemble"):
            hist = snap["histograms"][f'kb_search_stage_ms{{stage="{stage}"}}']
            assert hist["count"] == 1 and hist["sum"] >= 0.0
        request_hist = snap["histograms"]["kb_search_request_ms"]
        assert request_hist["count"] == 1 and request_hist["max"] > 0.0
        assert "kb_search_zero_hit_total" not in snap["counters"]  # 有命中不计零命中

    def test_zero_hit_counter(self) -> None:
        empty_store = StubStore(dense_hits=[], sparse_hits=[])
        pipeline, _, metrics = build_pipeline(store=empty_store)
        result = run(pipeline.search("查不到的知识", top_k=3))
        assert result.hits == []
        assert metrics.snapshot()["counters"].get("kb_search_zero_hit_total") == 1.0

    def test_degrade_reasons_are_labeled(self) -> None:
        # 降级计数按 reason 标签分序列（§6.5 降级触发计数）
        pipeline, _, metrics = build_pipeline(embedder=SlowEmbedder(delay_s=0.4))
        run(pipeline.search("云台支架", top_k=2))
        run(pipeline.search("云台支架", top_k=2))
        snap = metrics.snapshot()["counters"]
        assert snap["kb_search_total"] == 2.0
        assert snap[f'kb_search_degrade_total{{reason="{DEGRADE_SKIP_RERANK}"}}'] == 2.0

    def test_default_registry_created_when_not_injected(self) -> None:
        pipeline, _, _ = build_pipeline(metrics=None)
        run(pipeline.search("云台支架"))
        assert pipeline.metrics.snapshot()["counters"]["kb_search_total"] == 1.0
