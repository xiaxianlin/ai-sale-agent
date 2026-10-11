# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""知识库集群调用池单元测试（FR-SC-002，SR-SC-002 §6.2/§6.5/§9；Phase 4 TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：查询指纹归一化（同语义同指纹）、同指纹合并单次回源 + L1 命中、
  热点晋升 L2 服务；
- 边界：L1 恰过期（age>l1_ttl）冷查询重回源、热度阈值隔离（非热点不吃 L2）、
  kb_types 顺序不敏感/top_k 差异改变指纹；
- 异常：Milvus 不可用 + 无缓存 + 无桩 → KbUnavailable（上层 FR-AG-009 兜底）；
- 并发：50 线程同指纹压测——回源恰 1 次、结果零竞态全一致、P95 ≤ 500ms
  （NFR-PER-02 检索时延契约；容量口径 SR-SC-002 §6.1/§6.7）；
- 降级：降级链 Milvus 不可用 → 缓存（stale 标记）→ BM25 桩（先缓存后桩次序）。

验收口径映射：SRS §11.6/NFR-PER-02（集群并发 P95 ≤500ms）、§16.5（单点故障不影响其余）。
实现方案：docs/07-实现方案/SC/SR-SC-002-知识库集群调用.md §6.2、§6.5、§9
"""

from __future__ import annotations

import threading
import time

import pytest

from services.agent.core.cluster.callpool import (
    BackendUnavailableError,
    Bm25StubBackend,
    ClusterRetrievalPool,
    KbUnavailableError,
    PoolConfig,
    normalize_query,
    query_fingerprint,
)


class FakeMilvusBackend:
    """Milvus 桩：可注入延迟与故障，统计真实回源次数（合并证明的计数器）。"""

    def __init__(self, delay_s: float = 0.0, fail: bool = False) -> None:
        self.delay_s = delay_s
        self.fail = fail
        self.calls = 0
        self.lock = threading.Lock()

    def search(self, req: dict) -> dict:
        with self.lock:
            self.calls += 1
        if self.fail:
            raise BackendUnavailableError("milvus down")
        if self.delay_s:
            time.sleep(self.delay_s)
        return {"hits": [{"chunk_id": "c1", "score": 0.92, "text": "ST-3200 支架报价"}],
                "latency_ms": 12.0}


REQ = {"query": "ST-3200 支架 价格", "kb_types": ["product"], "top_k": 5}


def make_pool(
    *, l1_ttl: float = 60.0, l2_ttl: float = 600.0, hot_threshold: int = 3, **backend_kw
) -> tuple[ClusterRetrievalPool, FakeMilvusBackend]:
    """标准装配：可控时钟（自 0 递增）+ 可故障 Milvus 桩。"""
    backend = FakeMilvusBackend(**backend_kw)
    clock = {"t": 1000.0}
    pool = ClusterRetrievalPool(
        backend,
        config=PoolConfig(l1_ttl_s=l1_ttl, l2_ttl_s=l2_ttl, hot_threshold=hot_threshold),
        clock=lambda: clock["t"],
    )
    pool._clock_handle = clock  # 测试推进时钟（白名单测试钩子）
    return pool, backend


# ───────────────────────── 等价类：指纹归一化与合并语义 ─────────────────────────


class TestQueryFingerprint:
    """查询指纹 = 规范化 query + 参数排序 hash（SR-SC-002 §6.2）。"""

    def test_normalized_equivalent_queries_same_fingerprint(self) -> None:
        """「ST-3200 支架 价格」与「st3200支架，价格」同指纹（空白/标点/大小写归一）。"""
        a = query_fingerprint({"query": "ST-3200 支架 价格", "kb_types": ["product"], "top_k": 5})
        b = query_fingerprint({"query": "st3200支架，价格", "kb_types": ["product"], "top_k": 5})
        assert a == b
        assert normalize_query("  Hello,  World! ") == "helloworld"

    def test_different_params_different_fingerprint(self) -> None:
        """边界：kb_types 顺序不敏感、top_k/kb_types 差异改变指纹（互不误合并）。"""
        base = {"query": "价格", "kb_types": ["product", "process"], "top_k": 5}
        swapped = {"query": "价格", "kb_types": ["process", "product"], "top_k": 5}
        other_topk = {"query": "价格", "kb_types": ["product", "process"], "top_k": 3}
        assert query_fingerprint(base) == query_fingerprint(swapped)
        assert query_fingerprint(base) != query_fingerprint(other_topk)


class TestMerge:
    """同指纹并发合并为单次回源（singleflight，SR-SC-002 §6.2）。"""

    def test_same_fingerprint_merged_single_source(self) -> None:
        """等价类：同指纹串行两查 → 第二次 L1 命中，回源 1 次。"""
        pool, backend = make_pool()
        r1 = pool.search(REQ, requester="runner-01")
        r2 = pool.search(REQ, requester="runner-02")
        assert backend.calls == 1
        assert r2["meta"]["source"] == "cache:l1"
        assert r1["hits"] == r2["hits"]

    def test_different_fingerprint_no_cross_merge(self) -> None:
        """边界：不同指纹（top_k 不同）互不合并，各自回源。"""
        pool, backend = make_pool()
        pool.search(REQ, requester="runner-01")
        pool.search({**REQ, "top_k": 3}, requester="runner-02")
        assert backend.calls == 2


# ───────────────────────── 多级缓存：L1 TTL 与热点 L2 ─────────────────────────


class TestTwoLevelCache:
    """L1 结果缓存 TTL + 热点 L2 晋升（SR-SC-002 §4 ag:l0:kbf 键语义）。"""

    def test_l1_expired_cold_again(self) -> None:
        """边界（TTL 右侧外）：超过 l1_ttl → L1 失效，重新回源（FR-KB-017 增量可见上限）。"""
        pool, backend = make_pool(l1_ttl=60.0, l2_ttl=600.0, hot_threshold=99)
        pool.search(REQ)
        pool._clock_handle["t"] += 61.0
        result = pool.search(REQ)
        assert backend.calls == 2
        assert result["meta"]["source"] == "milvus"

    def test_hot_query_promoted_to_l2(self) -> None:
        """热点晋升：热度恰达阈值后，L1 过期仍可由 L2 服务（热点 L2）。"""
        pool, backend = make_pool(l1_ttl=60.0, l2_ttl=600.0, hot_threshold=3)
        for _ in range(3):  # 3 次触达 → 晋升热点
            pool.search(REQ)
            pool._clock_handle["t"] += 10.0
        pool._clock_handle["t"] += 100.0  # L1 已过期（age>60）但 L2（age≤600）命中
        result = pool.search(REQ)
        assert backend.calls == 1
        assert result["meta"]["source"] == "cache:l2"

    def test_non_hot_expired_not_served_from_l2(self) -> None:
        """边界（非热点）：低频查询 L1 过期后不得吃 L2（热度门槛隔离）。"""
        pool, backend = make_pool(l1_ttl=60.0, l2_ttl=600.0, hot_threshold=5)
        pool.search(REQ)  # 仅 1 次触达，未达热点阈值
        pool._clock_handle["t"] += 100.0
        pool.search(REQ)
        assert backend.calls == 2


# ───────────────────────── 降级链：Milvus → 缓存 → BM25 桩 ─────────────────────────


class TestDegradationChain:
    """缓存降级链（SR-SC-002 §6.5：先缓存降级、再桩兜底，不雪崩）。"""

    def test_milvus_down_serves_stale_cache(self) -> None:
        """降级①：Milvus 不可用 → L2 缓存顶上，degraded=[stale_cache]。"""
        pool, backend = make_pool(l1_ttl=60.0, l2_ttl=600.0, hot_threshold=2)
        pool.search(REQ)
        pool.search(REQ)  # 热点化
        backend.fail = True
        pool._clock_handle["t"] += 100.0  # L1 过期，L2 仍新鲜域内
        result = pool.search(REQ)
        assert result["meta"]["degraded"] == ["stale_cache"]
        assert result["hits"]

    def test_milvus_down_no_cache_falls_to_bm25_stub(self) -> None:
        """降级②：无缓存可吃 → BM25 桩兜底，degraded=[bm25_stub]。"""
        pool, backend = make_pool()
        pool.bm25 = Bm25StubBackend()
        backend.fail = True
        result = pool.search(REQ)
        assert result["meta"]["degraded"] == ["bm25_stub"]
        assert result["hits"][0]["chunk_id"].startswith("bm25:")

    def test_chain_order_cache_before_stub(self) -> None:
        """降级次序：缓存与桩同时可用 → 先缓存（桩仅作最后兜底）。"""
        pool, backend = make_pool(hot_threshold=2)
        pool.bm25 = Bm25StubBackend()
        pool.search(REQ)
        pool.search(REQ)
        backend.fail = True
        result = pool.search(REQ)
        assert result["meta"]["degraded"] == ["stale_cache"]
        assert pool.stats["bm25_fallbacks"] == 0

    def test_all_levels_down_raises_kb_unavailable(self) -> None:
        """异常：Milvus + 无缓存 + 无桩 → KbUnavailable（fail-safe 上抛，不装成功）。"""
        pool, backend = make_pool()
        backend.fail = True
        with pytest.raises(KbUnavailableError):
            pool.search(REQ)


# ───────────────────────── 并发压测：50 线程 P95 与零竞态 ─────────────────────────


class TestConcurrentLoad:
    """并发 50 线程压测（SR-SC-002 §9「并发 50 同指纹回源 1 次」；NFR-PER-02 P95）。"""

    def test_50_threads_same_fingerprint_single_source_no_race(self) -> None:
        """50 线程同指纹：回源恰 1 次、50 份结果全一致（零竞态）。"""
        pool, backend = make_pool()
        backend.delay_s = 0.01
        results: list[dict] = []
        lock = threading.Lock()
        barrier = threading.Barrier(50)

        def worker(i: int) -> None:
            barrier.wait()  # 同时起跑制造真实并发窗口
            r = pool.search(REQ, requester=f"runner-{i:02d}")
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert backend.calls == 1  # 同指纹合并为单次回源
        assert len(results) == 50
        assert all(r["hits"] == results[0]["hits"] for r in results)  # 零竞态
        assert pool.stats["merges"] == 49

    def test_50_threads_p95_within_budget(self) -> None:
        """P95 压测断言：50 线程 × 4 轮同指纹查询，P95 ≤ 500ms（NFR-PER-02 契约线）。"""
        pool, backend = make_pool()
        backend.delay_s = 0.02
        latencies: list[float] = []
        lock = threading.Lock()
        barrier = threading.Barrier(50)

        def worker(i: int) -> None:
            barrier.wait()
            for _ in range(4):
                t0 = time.perf_counter()
                pool.search(REQ, requester=f"runner-{i:02d}")
                with lock:
                    latencies.append((time.perf_counter() - t0) * 1000.0)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95) - 1]
        assert p95 <= 500.0
        assert backend.calls <= 2  # 合并削峰：200 次请求 ≤ 2 次回源（TTL 内）
        assert pool.stats["l1_hits"] + pool.stats["merges"] >= 190

    def test_concurrent_distinct_fingerprints_all_served(self) -> None:
        """并发：28 线程各查不同指纹 → 28 次回源、28 份正确结果（无串扰）。"""
        pool, backend = make_pool()
        results: dict[int, dict] = {}

        def worker(i: int) -> None:
            results[i] = pool.search({**REQ, "top_k": i + 1}, requester=f"runner-{i:02d}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(28)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert backend.calls == 28
        assert len(results) == 28
        assert all(r["hits"] for r in results.values())
