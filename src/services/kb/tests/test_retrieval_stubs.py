# 知识产权归甲方所有（COM-IP-01）
"""检索三段式测试共享桩（FR-KB-015 / SR-KB-015 §9）。

向量库与向量化模块由并行任务建设，测试一律自建鸭子类型桩（不导入其具体
实现）；本模块集中存放各测试文件共用的桩与工具（沿 _chunking_testlib.py 先例）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from services.kb.core.retrieval.base import Hit


def run(coro: Any) -> Any:
    """同步测试驱动协程（本仓库未引入 pytest-asyncio，统一 asyncio.run）。"""
    return asyncio.run(coro)


def chunk(cid: str, score: float, *, snippet: str = "", kb_type: str = "product") -> dict[str, Any]:
    """构造向量库桩的候选条目（映射形态，coerce_hit 兼容）。"""
    return {
        "chunk_id": cid,
        "score": score,
        "snippet": snippet or f"{kb_type}-chunk-{cid}",
        "title": f"标题-{cid}",
        "kb_type": kb_type,
        "source": f"doc-{cid}.pdf",
    }


def hit(cid: str, score: float, *, snippet: str = "") -> Hit:
    """直接构造 Hit（RRF 手算等纯融合逻辑用）。"""
    return Hit(chunk_id=cid, score=score, snippet=snippet or f"chunk-{cid}")


@dataclass
class StubEmbedding:
    """EmbeddingResult 鸭子类型桩（FR-KB-013 冻结契约的测试形态）。"""

    dense: list[float] | None = None
    sparse: dict[int, float] | None = None


class StubEmbedder:
    """向量化器桩：单查询 embed_batch 返回预置稠密/稀疏向量，记录调用。"""

    def __init__(self, dense: list[float] | None = None, sparse: dict[int, float] | None = None) -> None:
        self.dense = dense if dense is not None else [0.1, 0.2, 0.3]
        self.sparse = sparse if sparse is not None else {101: 0.9, 102: 0.4}
        self.calls: list[list[str]] = []

    async def embed_batch(self, texts: Sequence[str]) -> StubEmbedding:
        self.calls.append(list(texts))
        return StubEmbedding(dense=self.dense, sparse=self.sparse)


class SlowEmbedder(StubEmbedder):
    """慢向量化器：模拟 TEI 排队（触发流水线预算降级链）。"""

    def __init__(self, delay_s: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.delay_s = delay_s

    async def embed_batch(self, texts: Sequence[str]) -> StubEmbedding:
        await asyncio.sleep(self.delay_s)
        return await super().embed_batch(texts)


class FailingEmbedder:
    """向量化器故障桩：触发判定点 C（本地稀疏兜底）。"""

    async def embed_batch(self, texts: Sequence[str]) -> StubEmbedding:
        raise RuntimeError("TEI embed 服务不可用")


class StubStore:
    """向量库桩：按调用形态（dense/sparse/expr-only）分路返回预置命中。

    calls 记录每次 hybrid_search 的入参快照，供三路调用形态与 expr 断言。
    """

    def __init__(
        self,
        dense_hits: list[dict[str, Any]] | None = None,
        sparse_hits: list[dict[str, Any]] | None = None,
        structured_hits: list[dict[str, Any]] | None = None,
    ) -> None:
        self.dense_hits = dense_hits or []
        self.sparse_hits = sparse_hits or []
        self.structured_hits = structured_hits or []
        self.calls: list[dict[str, Any]] = []

    async def hybrid_search(
        self, dense: Any, sparse: Any, expr: Any, top_k: int,
    ) -> list[dict[str, Any]]:
        self.calls.append({"dense": dense, "sparse": sparse, "expr": expr, "top_k": top_k})
        if dense is not None:
            return self.dense_hits[:top_k]
        if sparse is not None:
            return self.sparse_hits[:top_k]
        return self.structured_hits[:top_k]


class FailingStore:
    """向量库故障桩：全部召回路失败 → 50002 语义（RetrievalDownstreamError）。"""

    calls = 0

    async def hybrid_search(self, dense: Any, sparse: Any, expr: Any, top_k: int) -> list[dict[str, Any]]:
        FailingStore.calls += 1
        raise RuntimeError("Milvus 不可用")


class StructuredFailStore(StubStore):
    """结构化路故障桩：仅 expr-only 调用失败（该路降级不阻断主链路）。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.failures = 0

    async def hybrid_search(self, dense: Any, sparse: Any, expr: Any, top_k: int) -> list[dict[str, Any]]:
        if dense is None and sparse is None:
            self.failures += 1
            raise RuntimeError("结构化路查询失败")
        return await super().hybrid_search(dense, sparse, expr, top_k)


@dataclass
class FakeReranker:
    """确定性重排器桩：按得分倒序重排并写回 rerank_score（契约正例）。

    order="reverse" 时强制倒置入参序（验证重排确实改变顺序）。
    """

    order: str = "reverse"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def rerank(self, query: str, cands: Sequence[Hit], top_k: int) -> list[Hit]:
        self.calls.append({"query": query, "n_cands": len(cands), "top_k": top_k})
        pool = list(cands)
        if self.order == "reverse":
            pool.reverse()
        reranked: list[Hit] = []
        for rank, cand in enumerate(pool[:top_k]):
            score = float(top_k - rank)  # 人造精排分：与 RRF 序显著不同
            reranked.append(replace(cand, score=score, rerank_score=score))
        return reranked
