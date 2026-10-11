# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""统一检索 API（FR-KB-030 / SR-KB-030）TDD 补齐测试（五类场景补强，不改既有文件）。

按 TDD 新规五列审计既有 test_api_search 后补齐（复用其 ApiTestEnv 真实链路装配）：
- 边界：query 恰 512 字符（上界内）/ 513（上界外 → 40001 定位字段）；
- 等价类：kb_types=[] + filters={} 空集合语义 = 查全部库、零过滤；
- 空集：空库检索 → 200 + results=[]（零命中是正常响应而非错误）；
- 并发：8 线程并发请求 → 全部 200、trace_id 互不相同、限流不误伤。

KPI 映射：SRS §16.1「检索准确率 / 检索响应时延」——契约边界（512/top_k）
与并发正确性是 P1-P5 全模块唯一知识入口的可用性底线。

依据文档：docs/07-实现方案/KB/SR-KB-030-检索API.md §6.1、§6.2、§6.3、§9；
         docs/02-设计文档/04-接口设计.md §1.3、§2。
"""
from __future__ import annotations

import threading

from services.kb.core.vectorstore import InMemoryVectorStore
from services.kb.tests.test_api_search import PATH, QUERY, ApiTestEnv


class TestQueryLengthBoundary:
    """边界：query 长度上界 512（冻结契约 KB_API_QUERY_MAX_LEN）。"""

    def test_query_exactly_512_accepted(self) -> None:
        """恰 512 字符：200 且正常出结果（上界内含等号）。"""
        env = ApiTestEnv()
        response = env.search({"query": "支" * 512})
        assert response.status_code == 200
        body = response.json()
        assert body["code"] == 0 and body["data"]["results"]

    def test_query_513_rejected_40001_with_loc(self) -> None:
        """513 字符：40001 + data.errors 定位到 query 字段（string_too_long）。"""
        env = ApiTestEnv()
        response = env.search({"query": "支" * 513})
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == 40001
        errors = body["data"]["errors"]
        assert any("query" in error["loc"] for error in errors)


class TestEmptyCollectionSemantics:
    """等价类/空集：空集合参数与空库。"""

    def test_empty_kb_types_and_filters_means_all(self) -> None:
        """kb_types=[] + filters={}：等价于查全部库、无过滤（空集合不产生过滤条件）。"""
        env = ApiTestEnv()
        response = env.search({"query": QUERY, "kb_types": [], "filters": {}})
        assert response.status_code == 200
        body = response.json()
        assert body["code"] == 0
        kb_types = {item["kb_type"] for item in body["data"]["results"]}
        assert len(kb_types) >= 2  # 未被过滤：多库命中

    def test_empty_store_returns_200_with_empty_results(self) -> None:
        """空库：零命中为 200 正常响应（results=[]，非 5xx），latency_ms 仍在。"""
        env = ApiTestEnv(store=InMemoryVectorStore())
        response = env.search({"query": QUERY})
        assert response.status_code == 200
        body = response.json()
        assert body["code"] == 0
        assert body["data"]["results"] == []
        assert isinstance(body["data"]["latency_ms"], int)


class TestConcurrentRequests:
    """并发：8 线程并发检索全部成功（限流余量内），trace_id 唯一可追溯。"""

    def test_eight_concurrent_searches_all_succeed(self) -> None:
        env = ApiTestEnv(qps=1000)
        results: list[tuple[int, str]] = []  # (status, trace_id)
        lock = threading.Lock()
        barrier = threading.Barrier(8)
        payload = {"query": QUERY, "top_k": 3}

        def worker(index: int) -> None:
            barrier.wait()
            response = env.search(payload)
            with lock:
                results.append((response.status_code, response.json().get("trace_id", "")))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 8
        assert all(status == 200 for status, _trace in results)
        trace_ids = [trace for _status, trace in results]
        assert len(set(trace_ids)) == 8  # 每请求独立 trace_id（透传/生成均唯一）
