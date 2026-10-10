# 知识产权归甲方所有（COM-IP-01）
"""重排单元测试（SR-KB-015 §9）：顺序与 TopK / 判定点 D 超时与不可达降级 / 直通兜底。

注：必须先导入被测 reranker 模块再 import httpx——reranker 模块头部完成了
标准库 platform 自愈（仓库顶层同名包遮蔽问题，见其模块 docstring）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from services.kb.core.retrieval.reranker import (
    DEGRADE_RERANK_BAD_RESPONSE,
    DEGRADE_RERANK_TIMEOUT,
    DEGRADE_RERANK_UNREACHABLE,
    BgeRerankerClient,
    PassthroughReranker,
    Reranker,
)
from services.kb.tests.test_retrieval_stubs import hit, run

import httpx  # noqa: E402 —— 须位于 reranker 导入（platform 自愈）之后


def make_client(handler: Any, *, timeout_ms: float = 250.0, on_degrade: Any = None) -> BgeRerankerClient:
    """以 MockTransport 构造 TEI 客户端（handler 收到 Request 返回 Response）。"""
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return BgeRerankerClient(timeout_ms=timeout_ms, client=client, on_degrade=on_degrade)


class SlowTransport(httpx.AsyncBaseTransport):
    """异步慢传输：真实让出事件循环，驱动 asyncio.wait_for 超时（判定点 D）。"""

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self.delay_s)
        return httpx.Response(200, json=[])


class TestBgeRerankerClient:
    """本地 TEI /rerank 客户端：精排顺序 / TopK / 请求载荷契约。"""

    def test_order_and_top_k(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["payload"] = json.loads(request.content)
            # TEI 契约：按 score 降序的 [{index, score}]——此处给乱序由客户端防御排序
            return httpx.Response(200, json=[
                {"index": 1, "score": 0.7}, {"index": 2, "score": 0.95}, {"index": 0, "score": 0.8},
            ])

        cands = [hit("c0", 0.016), hit("c1", 0.015), hit("c2", 0.014)]
        client = make_client(handler)
        reranked = run(client.rerank("云台 最大承载", cands, top_k=2))
        run(client.aclose())

        assert captured["path"] == "/rerank"
        assert captured["payload"] == {
            "query": "云台 最大承载",
            "texts": ["chunk-c0", "chunk-c1", "chunk-c2"],
            "top_n": 2,
        }
        assert [h.chunk_id for h in reranked] == ["c2", "c0"]  # 精排分序（非 RRF 序）
        assert reranked[0].rerank_score == 0.95  # 契约：生效精排写回 rerank_score
        assert reranked[0].score == 0.95

    def test_empty_candidates_short_circuit(self) -> None:
        client = make_client(lambda request: httpx.Response(200, json=[]))
        assert run(client.rerank("q", [], top_k=5)) == []

    def test_timeout_degrades_to_rrf_order(self) -> None:
        # 判定点 D：250ms 硬超时 → RRF 序直出 TopK + on_degrade 留痕
        reasons: list[str] = []
        client = BgeRerankerClient(
            timeout_ms=30.0,
            client=httpx.AsyncClient(transport=SlowTransport(delay_s=0.3)),
            on_degrade=reasons.append,
        )
        cands = [hit("c0", 0.03), hit("c1", 0.02), hit("c2", 0.01)]
        reranked = run(client.rerank("q", cands, top_k=2))
        run(client.aclose())
        assert [h.chunk_id for h in reranked] == ["c0", "c1"]  # 入参序（RRF 序）直出
        assert reasons == [DEGRADE_RERANK_TIMEOUT]
        assert all(h.rerank_score is None for h in reranked)  # 未写回即降级判据

    def test_unreachable_degrades(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("tei-reranker 容器不可达")

        reasons: list[str] = []
        client = make_client(handler, on_degrade=reasons.append)
        cands = [hit("c0", 0.03), hit("c1", 0.02)]
        reranked = run(client.rerank("q", cands, top_k=5))
        run(client.aclose())
        assert [h.chunk_id for h in reranked] == ["c0", "c1"]
        assert reasons == [DEGRADE_RERANK_UNREACHABLE]

    def test_bad_response_degrades(self) -> None:
        # 非 2xx（raise_for_status）→ 坏响应降级，不抛给主链路
        reasons: list[str] = []
        client = make_client(lambda request: httpx.Response(500, text="boom"), on_degrade=reasons.append)
        cands = [hit("c0", 0.03)]
        reranked = run(client.rerank("q", cands, top_k=1))
        run(client.aclose())
        assert [h.chunk_id for h in reranked] == ["c0"]
        assert reasons == [DEGRADE_RERANK_BAD_RESPONSE]

    def test_malformed_payload_degrades(self) -> None:
        # 越界 index / 非列表体 → 坏响应降级（防御下游数据损坏）
        reasons: list[str] = []
        client = make_client(lambda request: httpx.Response(200, json=[{"index": 9, "score": 0.9}]),
                             on_degrade=reasons.append)
        cands = [hit("c0", 0.03)]
        reranked = run(client.rerank("q", cands, top_k=1))
        run(client.aclose())
        assert [h.chunk_id for h in reranked] == ["c0"]
        assert reasons == [DEGRADE_RERANK_BAD_RESPONSE]


class TestPassthroughReranker:
    """直通重排器：rerank=false 的语义载体（按 RRF 序截断，不写回精排分）。"""

    def test_passthrough_keeps_order(self) -> None:
        cands = [hit("c0", 0.03), hit("c1", 0.02), hit("c2", 0.01)]
        reranked = run(PassthroughReranker().rerank("q", cands, top_k=2))
        assert [h.chunk_id for h in reranked] == ["c0", "c1"]
        assert all(h.rerank_score is None for h in reranked)

    def test_protocol_conformance(self) -> None:
        # Reranker Protocol 鸭子类型：两种实现均可通过运行时检查
        assert isinstance(PassthroughReranker(), Reranker)
        assert isinstance(BgeRerankerClient(client=httpx.AsyncClient()), Reranker)
