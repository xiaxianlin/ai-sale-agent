# 知识产权归甲方所有（COM-IP-01）
"""多路召回单元测试（SR-KB-015 §9）：RRF 手算融合 / 三路调用形态 / expr 防注入 / 判定点 C。"""
from __future__ import annotations

import pytest

from services.kb.core.retrieval.base import RetrievalDownstreamError
from services.kb.core.retrieval.recall import (
    RECALL_DEPTH_DEFAULT,
    build_expr,
    build_structured_expr,
    escape_expr_value,
    local_sparse_fallback,
    rrf_fuse,
)
from services.kb.core.retrieval.rewriter import RewriteResult
from services.kb.core.retrieval import MultiRouteRecall
from services.kb.tests.test_retrieval_stubs import (
    FailingEmbedder,
    FailingStore,
    StubEmbedding,
    StubEmbedder,
    StubStore,
    StructuredFailStore,
    chunk,
    hit,
    run,
)

K = 60  # 与 KB-014 一致的 RRF 常数（SR-KB-015 §6.3）


def rewritten(query: str = "云台支架", slots: dict | None = None) -> RewriteResult:
    """构造召回入参（不经过改写器，聚焦召回段逻辑）。"""
    return RewriteResult(rewritten_query=query, slots=slots or {})


class TestRrfFuse:
    """应用层 RRF：融合序与 1/(k+rank+1) 手算一致，routes 记录命中路。"""

    def test_three_route_hand_computed(self) -> None:
        dense = [hit("A", 0.9), hit("B", 0.8), hit("C", 0.7)]
        sparse = [hit("B", 5.0), hit("A", 4.0), hit("D", 3.0)]
        structured = [hit("C", 1.0), hit("A", 0.5)]
        fused = rrf_fuse([("dense", dense), ("sparse", sparse), ("structured", structured)], k=K)

        # score(d) = Σ_route 1/(k + rank + 1)，rank 从 0 起：第0名→1/61、第1名→1/62、第2名→1/63
        expect = {
            "A": 1 / (K + 1) + 1 / (K + 2) + 1 / (K + 2),  # dense 第0 + sparse 第1 + structured 第1
            "B": 1 / (K + 2) + 1 / (K + 1),                # dense 第1 + sparse 第0
            "C": 1 / (K + 3) + 1 / (K + 1),                # dense 第2 + structured 第0
            "D": 1 / (K + 3),                              # sparse 第2
        }
        assert [h.chunk_id for h in fused] == ["A", "B", "C", "D"]  # 手算序
        by_id = {h.chunk_id: h for h in fused}
        for cid, score in expect.items():
            assert by_id[cid].rrf_score == pytest.approx(score, rel=1e-12)
            assert by_id[cid].score == pytest.approx(score, rel=1e-12)  # 生效得分切换为融合分
        assert by_id["A"].routes == ["dense", "sparse", "structured"]  # 各路贡献留痕（路遍历序）
        assert by_id["B"].routes == ["dense", "sparse"]
        assert by_id["D"].routes == ["sparse"]

    def test_depth_truncation(self) -> None:
        dense = [hit(f"c{i}", 1.0 - i * 0.01) for i in range(10)]
        fused = rrf_fuse([("dense", dense)], k=K, depth=3)
        assert [h.chunk_id for h in fused] == ["c0", "c1", "c2"]  # TopN 截断

    def test_input_hits_not_mutated(self) -> None:
        # 融合候选为拷贝：不污染各路原始 Hit（可被重排段/影子归因复用）
        dense = [hit("A", 0.9)]
        sparse = [hit("A", 1.0)]
        fused = rrf_fuse([("dense", dense), ("sparse", sparse)], k=K)
        assert dense[0].routes == [] and dense[0].score == 0.9  # 原始路内 Hit 不变
        assert fused[0].routes == ["dense", "sparse"]  # 融合副本记录命中路
        assert fused[0].rrf_score == pytest.approx(2 / (K + 1))  # 双路同秩满分

    def test_invalid_k_rejected(self) -> None:
        with pytest.raises(ValueError):
            rrf_fuse([("dense", [hit("A", 1.0)])], k=0)


class TestBuildExpr:
    """expr 构建：白名单 / published 强制 / 转义防注入 / 确定性。"""

    def test_published_always_forced(self) -> None:
        assert build_expr() == 'item_status == "published"'

    def test_kb_types_and_filters_merged(self) -> None:
        expr = build_expr({"scene_tag": "after_sales"}, ["faq", "product"])
        assert expr == 'item_status == "published" and kb_type in ["faq", "product"] ' \
                       'and scene_tag == "after_sales"'  # 字段名排序保证确定性

    def test_injection_neutralized(self) -> None:
        # §9「expr 注入防护」：双引号与反斜杠转义后仅匹配字面值
        malicious = 'a" or "1"=="1'
        expr = build_expr({"sku": malicious})
        escaped = escape_expr_value(malicious)
        assert expr == f'item_status == "published" and sku == "{escaped}"'
        # 转义后不存在逃逸出字面量的未转义引号
        body = expr.split("sku == ", 1)[1]
        assert body.count('"') % 2 == 0
        assert '\\"' in body

    def test_single_quote_value_stays_literal(self) -> None:
        expr = build_expr({"sku": "a' or '1'='1"})
        assert expr == 'item_status == "published" and sku == "a\' or \'1\'=\'1"'

    def test_non_whitelisted_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="非白名单"):
            build_expr({"evil; drop table": "x"})

    def test_structured_expr_from_slots(self) -> None:
        base = build_expr(kb_types=["faq"])
        expr = build_structured_expr({"sku": ["ST-3200", "ST-3300"], "intent": "price"}, base)
        assert expr == 'item_status == "published" and kb_type == "faq" ' \
                       'and sku in ["ST-3200", "ST-3300"]'
        # intent 槽位不参与 expr（白名单外）

    def test_structured_expr_none_without_slots(self) -> None:
        assert build_structured_expr({"intent": "price"}, build_expr()) is None


class TestMultiRouteRecall:
    """三路召回主链路：调用形态 / 融合 / 路级容错 / 判定点 C 兜底。"""

    def _recall_with(self, store, embedder, slots=None, filters=None, kb_types=None, **kwargs):
        recall = MultiRouteRecall(store, embedder, **kwargs)
        return run(recall.recall(rewritten(slots=slots), filters=filters, kb_types=kb_types))

    def test_three_routes_called_with_distinct_shapes(self) -> None:
        store = StubStore(
            dense_hits=[chunk("d1", 0.9), chunk("d2", 0.8)],
            sparse_hits=[chunk("s1", 5.0), chunk("d1", 4.0)],
            structured_hits=[chunk("t1", 1.0)],
        )
        result = self._recall_with(store, StubEmbedder(), slots={"sku": ["ST-3200"]},
                                   filters={"scene_tag": "after_sales"}, kb_types=["faq"])
        assert len(store.calls) == 3
        shapes = [(c["dense"] is not None, c["sparse"] is not None) for c in store.calls]
        assert shapes == [(True, False), (False, True), (False, False)]  # 路①/路②/路③
        # 路①② 入参过滤 expr；路③ 叠加槽位条件
        assert store.calls[0]["expr"] == 'item_status == "published" and kb_type == "faq" ' \
                                        'and scene_tag == "after_sales"'
        assert 'sku == "ST-3200"' in store.calls[2]["expr"]
        # 融合：d1 双路命中 RRF 最高
        assert result.hits[0].chunk_id == "d1"
        assert result.route_stats == {"dense": 2, "sparse": 2, "structured": 1, "fused": 4}
        assert result.degrade == []

    def test_no_slots_skips_structured_route(self) -> None:
        store = StubStore(dense_hits=[chunk("d1", 0.9)], sparse_hits=[])
        result = self._recall_with(store, StubEmbedder())
        assert len(store.calls) == 2  # 仅向量路 + 关键词路
        assert set(result.route_stats) == {"dense", "sparse", "fused"}

    def test_embed_failure_falls_back_to_local_sparse(self) -> None:
        # 判定点 C：向量化失败 → 本地稀疏兜底，仅关键词路发起
        store = StubStore(dense_hits=[chunk("d1", 0.9)], sparse_hits=[chunk("s1", 5.0)])
        result = self._recall_with(store, FailingEmbedder())
        assert len(store.calls) == 1
        assert store.calls[0]["dense"] is None and store.calls[0]["sparse"] is not None
        assert "embed_failed_local_sparse_fallback" in result.degrade
        assert [h.chunk_id for h in result.hits] == ["s1"]

    def test_all_routes_fail_raises_downstream_error(self) -> None:
        # 全部路失败 → 50002 语义（无可用降级）
        with pytest.raises(RetrievalDownstreamError):
            self._recall_with(FailingStore(), StubEmbedder())

    def test_structured_route_failure_tolerated(self) -> None:
        # 结构化路失败不阻断主链路，degrade 留痕
        store = StructuredFailStore(dense_hits=[chunk("d1", 0.9)], sparse_hits=[chunk("s1", 4.0)])
        result = self._recall_with(store, StubEmbedder(), slots={"sku": ["ST-3200"]})
        assert store.failures == 1
        assert "route_failed:structured" in result.degrade
        assert result.hits  # 向量+关键词路结果仍在

    def test_depth_forwarded_to_store(self) -> None:
        store = StubStore(dense_hits=[chunk(f"d{i}", 1.0) for i in range(20)])
        result = self._recall_with(store, StubEmbedder(), depth=7)
        assert all(c["top_k"] == 7 for c in store.calls)  # 候选池深度透传各路
        assert len(result.hits) == 7

    def test_default_depth_is_50(self) -> None:
        assert RECALL_DEPTH_DEFAULT == 50  # Top50 → Top5 口径（SR-KB-015 §6.4）


class TestLocalSparseFallback:
    """本地稀疏兜底（判定点 C 占位实现）：确定性 / 值域 / 词元生效。"""

    def test_deterministic_across_calls(self) -> None:
        first = local_sparse_fallback("ST-3200 云台支架")
        second = local_sparse_fallback("ST-3200 云台支架")
        assert first == second  # md5 截断 id 不受 PYTHONHASHSEED 影响
        assert first  # 非空（词元生效）

    def test_values_within_space(self) -> None:
        sparse = local_sparse_fallback("能扛多重 50kg", space=1024)
        assert sparse and all(0 <= tid < 1024 for tid in sparse)
        assert all(weight > 0 for weight in sparse.values())

    def test_shared_token_collides_and_accumulates(self) -> None:
        sparse = local_sparse_fallback("gimbal gimbal")
        assert sparse and max(sparse.values()) >= 2.0  # 重复词元计数累加


class TestSyncDuckTyping:
    """同步实现兼容：store/embedder 同步形态经 maybe_await 直接可用。"""

    def test_sync_store_and_embedder(self) -> None:
        class SyncEmbedder:
            def embed_batch(self, texts):
                return StubEmbedding(dense=[0.5, 0.5], sparse={7: 1.0})

        class SyncStore:
            def __init__(self) -> None:
                self.calls = 0

            def hybrid_search(self, dense, sparse, expr, top_k):
                self.calls += 1
                return [chunk("x1", 0.9)]

        store, embedder = SyncStore(), SyncEmbedder()
        result = run(MultiRouteRecall(store, embedder).recall(rewritten()))
        assert store.calls == 2  # 同步实现同样发起两路
        assert result.hits[0].chunk_id == "x1"
