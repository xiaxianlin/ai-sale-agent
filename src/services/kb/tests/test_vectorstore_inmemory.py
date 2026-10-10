# 知识产权归甲方所有（COM-IP-01）
"""InMemoryVectorStore 行为测试（FR-KB-014 §9 单元测试用例的内存落地）。

覆盖：upsert 幂等 / 删除可见性、混合检索命中、expr 过滤组合、RRF 排序正确性
（手算对照）、未发布条目不可见、top_k 截断、并发读写。
"""
from __future__ import annotations

import math
import threading

import pytest

from services.kb.core.vectorstore import DENSE_DIM, InMemoryVectorStore, VectorRecord


def _dense(*values: float) -> list[float]:
    """构造 1024 维稠密向量：给定前几维，其余补 0（测试便利）。"""
    return [*values, *([0.0] * (DENSE_DIM - len(values)))]


def _record(chunk_id: int, **overrides: object) -> VectorRecord:
    """构造合法 VectorRecord 的默认工厂（product / e1 / 稀疏{1:1}），按需覆盖。"""
    payload: dict[str, object] = {
        "chunk_id": chunk_id,
        "kb_type": "product",
        "dense": _dense(1.0),
        "sparse": {1: 1.0},
    }
    payload.update(overrides)
    return VectorRecord(**payload)  # type: ignore[arg-type]


# ── 写入与删除 ──
def test_upsert_idempotent_by_chunk_id() -> None:
    """同主键重投只保留最后一条（幂等，SR-KB-014 §6.4）。"""
    store = InMemoryVectorStore()
    store.upsert([_record(1), _record(2)])
    store.upsert([_record(1), _record(3)])
    assert len(store) == 3


def test_upsert_replaces_vector() -> None:
    """重投覆盖旧向量：检索结果反映最新内容（即插即查，FR-KB-017）。"""
    store = InMemoryVectorStore()
    store.upsert([_record(1, dense=_dense(1.0))])
    store.upsert([_record(1, dense=_dense(0.0, 1.0))])  # 覆盖为正交方向
    hits = store.hybrid_search(dense=_dense(1.0), top_k=5)
    assert hits == []  # 新向量与查询正交（cos=0），不再被稠密路召回
    hits = store.hybrid_search(dense=_dense(0.0, 1.0), top_k=5)
    assert [hit.chunk_id for hit in hits] == [1]


def test_delete_visibility_and_idempotent() -> None:
    """删除后立即不可见；删除不存在的主键静默忽略（幂等）。"""
    store = InMemoryVectorStore()
    store.upsert([_record(1), _record(2), _record(3, dense=_dense(0.5, 0.5))])
    store.delete([1, 999])  # 999 不存在：不应抛错
    assert len(store) == 2
    hits = store.hybrid_search(dense=_dense(1.0), sparse={1: 1.0}, top_k=5)
    assert 1 not in {hit.chunk_id for hit in hits}
    assert {hit.chunk_id for hit in hits} == {2, 3}
    # 删除当前最优命中后，次优顶上
    store.delete([2])
    hits = store.hybrid_search(dense=_dense(1.0), top_k=5)
    assert [hit.chunk_id for hit in hits] == [3]


def test_empty_store_returns_empty() -> None:
    """空库检索返回空列表。"""
    assert InMemoryVectorStore().hybrid_search(dense=_dense(1.0), top_k=5) == []


# ── 混合检索：单路 / 双路 ──
def test_dense_only_search_ranking() -> None:
    """仅稠密路：按余弦相似度降序，RRF 单路保持原排序。"""
    store = InMemoryVectorStore()
    store.upsert([
        _record(1, dense=_dense(1.0)),
        _record(2, dense=_dense(0.8, 0.6)),   # cos = 0.8
        _record(3, dense=_dense(0.0, 1.0)),   # cos = 0（未召回）
        _record(4, dense=_dense(-1.0)),       # cos = -1（未召回）
    ])
    hits = store.hybrid_search(dense=_dense(1.0), top_k=5)
    assert [hit.chunk_id for hit in hits] == [1, 2]


def test_sparse_only_search_ranking() -> None:
    """仅稀疏路：按稀疏点积降序；零重叠不召回。"""
    store = InMemoryVectorStore()
    store.upsert([
        _record(1, sparse={1: 1.0}),
        _record(2, sparse={1: 5.0}),
        _record(3, sparse={9: 1.0}),  # 与查询无重叠 → 点积 0 → 未召回
    ])
    hits = store.hybrid_search(sparse={1: 1.0}, top_k=5)
    assert [hit.chunk_id for hit in hits] == [2, 1]


def test_hybrid_search_requires_at_least_one_channel() -> None:
    """dense 与 sparse 都缺省时拒绝检索（无召回路）。"""
    with pytest.raises(ValueError, match="至少提供一路"):
        InMemoryVectorStore().hybrid_search(top_k=5)


def test_hybrid_search_rejects_wrong_dense_dim() -> None:
    """稠密查询向量维度必须 1024。"""
    with pytest.raises(ValueError, match="1024"):
        InMemoryVectorStore().hybrid_search(dense=[1.0, 0.0], top_k=5)


def test_hybrid_search_rejects_bad_top_k() -> None:
    """top_k 必须 >= 1。"""
    with pytest.raises(ValueError, match="top_k"):
        InMemoryVectorStore().hybrid_search(dense=_dense(1.0), top_k=0)


def test_rrf_k_validation() -> None:
    """RRF 常数 k 必须 >= 1（可配，默认 60）。"""
    with pytest.raises(ValueError, match="rrf_k"):
        InMemoryVectorStore(rrf_k=0)


# ── RRF 融合排序正确性（手算对照，k=60）──
def test_rrf_ordering_matches_hand_computation() -> None:
    """构造已知双路排名：稠密序 1>3>2、稀疏序 2>3（1 未召回），手算 RRF 期望序 2>3>1。

    - 稠密查询 q=(1, 0.1)（未归一化，余弦只看夹角）：
      cos(1)=1/√1.01、cos(3)=1.1/(√1.01·√2)、cos(2)=0.1/√1.01>0 → 名次 1,3,2；
    - 稀疏查询 {2:1.0}：点积 2=5.0、3=1.0（1 无重叠不召回）→ 名次 2,3；
    - RRF(k=60)：2 → 1/61+1/63≈0.0322665；3 → 1/62+1/62≈0.0322581；
      1 → 1/61≈0.0163934 → 期望序 [2, 3, 1]，且双路命中的 2/3 压过单路第一名 1。
    """
    store = InMemoryVectorStore()  # rrf_k 默认 60
    store.upsert([
        _record(1, dense=_dense(1.0), sparse={}),
        _record(2, dense=_dense(0.0, 1.0), sparse={2: 5.0}),
        _record(3, dense=_dense(1 / math.sqrt(2), 1 / math.sqrt(2)), sparse={2: 1.0}),
    ])
    hits = store.hybrid_search(dense=_dense(1.0, 0.1), sparse={2: 1.0}, top_k=5)
    assert [hit.chunk_id for hit in hits] == [2, 3, 1]
    assert hits[0].score == pytest.approx(1 / 61 + 1 / 63)
    assert hits[1].score == pytest.approx(1 / 62 + 1 / 62)
    assert hits[2].score == pytest.approx(1 / 61)


def test_rrf_dual_channel_beats_single_channel_top1() -> None:
    """双路都命中的记录压过「单路第一名」：融合收益可观测。"""
    store = InMemoryVectorStore()
    store.upsert([
        _record(1, dense=_dense(1.0), sparse={}),        # 仅稠密路第 1
        _record(2, dense=_dense(0.0, 1.0), sparse={2: 1.0}),  # 稀疏路第 1、稠密路低分
    ])
    hits = store.hybrid_search(dense=_dense(1.0, 0.1), sparse={2: 1.0}, top_k=5)
    assert [hit.chunk_id for hit in hits] == [2, 1]
    assert hits[0].score > hits[1].score


def test_rrf_custom_k_changes_scores_not_availability() -> None:
    """rrf_k 可配：分数随 k 变化，命中集合不变。"""
    records = [
        _record(1, dense=_dense(1.0), sparse={1: 1.0}),
        _record(2, dense=_dense(0.9, 0.1), sparse={1: 0.5}),
    ]
    default_store = InMemoryVectorStore()
    custom_store = InMemoryVectorStore(rrf_k=10)
    default_store.upsert(records)
    custom_store.upsert(records)
    default_hits = default_store.hybrid_search(dense=_dense(1.0), sparse={1: 1.0}, top_k=5)
    custom_hits = custom_store.hybrid_search(dense=_dense(1.0), sparse={1: 1.0}, top_k=5)
    assert [h.chunk_id for h in default_hits] == [h.chunk_id for h in custom_hits] == [1, 2]
    assert custom_hits[0].score == pytest.approx(1 / 11 + 1 / 11)
    assert default_hits[0].score == pytest.approx(1 / 61 + 1 / 61)


# ── 未发布条目不可见（03 §4.2 仅索引 published）──
def test_unpublished_records_invisible() -> None:
    """非 published 条目允许入库但不参与检索——即使向量完全匹配。"""
    store = InMemoryVectorStore()
    store.upsert([
        _record(1, item_status="draft", dense=_dense(1.0), sparse={1: 9.0}),  # 完美匹配但未发布
        _record(2, dense=_dense(1.0), sparse={1: 1.0}),
    ])
    assert len(store) == 2  # 未发布条目仍被存储
    hits = store.hybrid_search(dense=_dense(1.0), sparse={1: 1.0}, top_k=5)
    assert [hit.chunk_id for hit in hits] == [2]


def test_unpublished_invisible_regardless_of_expr() -> None:
    """expr 无法绕过 published 强制条件：显式查询未发布状态只得空集。"""
    store = InMemoryVectorStore()
    store.upsert([_record(1, item_status="unpublished")])
    assert store.hybrid_search(dense=_dense(1.0), expr="item_status = 'unpublished'") == []


# ── expr 标量过滤组合 ──
def test_expr_kb_type_in_filter() -> None:
    """多值成员过滤：kb_type in (...) 命中集合正确（兼分区裁剪语义）。"""
    store = InMemoryVectorStore()
    store.upsert([
        _record(101, kb_type="product"),
        _record(102, kb_type="skill"),
        _record(103, kb_type="process"),
        _record(104, kb_type="product"),
    ])
    hits = store.hybrid_search(dense=_dense(1.0), expr="kb_type in ('product','skill')", top_k=10)
    assert {hit.chunk_id for hit in hits} == {101, 102, 104}


def test_expr_equality_and_combined_with_and() -> None:
    """等值过滤 + and 组合：交集语义，组合结果窄于任一单条件。"""
    store = InMemoryVectorStore()
    store.upsert([
        _record(101, kb_type="product", sku="ST-3200"),
        _record(102, kb_type="skill", sku="ST-3200"),
        _record(103, kb_type="process", sku="ST-3200"),
        _record(104, kb_type="product", sku="AB-1"),
    ])
    by_type = store.hybrid_search(dense=_dense(1.0), expr="kb_type in ('product','skill')", top_k=10)
    by_sku = store.hybrid_search(dense=_dense(1.0), expr="sku = 'ST-3200'", top_k=10)
    combined = store.hybrid_search(
        dense=_dense(1.0), expr="kb_type in ('product','skill') and sku = 'ST-3200'", top_k=10)
    assert {h.chunk_id for h in by_type} == {101, 102, 104}
    assert {h.chunk_id for h in by_sku} == {101, 102, 103}
    assert {h.chunk_id for h in combined} == {101, 102}  # 交集


def test_expr_other_scalar_fields() -> None:
    """scene_tag / process_node / reason_type / customer_ref 均可过滤。"""
    store = InMemoryVectorStore()
    store.upsert([
        _record(1, scene_tag="after_sale", process_node="quote", reason_type="price",
                customer_ref="ACME"),
        _record(2, scene_tag="pre_sale", process_node="sample", reason_type="moq",
                customer_ref="BETA"),
    ])
    hits = store.hybrid_search(
        dense=_dense(1.0),
        expr="scene_tag = 'after_sale' and process_node = 'quote' and reason_type = 'price'"
             " and customer_ref = 'ACME'",
        top_k=10)
    assert {hit.chunk_id for hit in hits} == {1}


def test_expr_no_match_returns_empty() -> None:
    """过滤后无候选时返回空列表。"""
    store = InMemoryVectorStore()
    store.upsert([_record(1, sku="ST-3200")])
    assert store.hybrid_search(dense=_dense(1.0), expr="sku = 'AB-1'", top_k=5) == []


def test_expr_malformed_raises() -> None:
    """非法 expr（白名单外字段 / or 语法）在检索入口即抛 ValueError（fail-closed）。"""
    store = InMemoryVectorStore()
    store.upsert([_record(1)])
    with pytest.raises(ValueError, match="白名单"):
        store.hybrid_search(dense=_dense(1.0), expr="foo = 'x'", top_k=5)
    with pytest.raises(ValueError, match="语法"):
        store.hybrid_search(dense=_dense(1.0), expr="sku = 'x' or kb_type = 'product'", top_k=5)


# ── top_k 截断 ──
def test_top_k_truncation() -> None:
    """返回条数不超过 top_k，且为融合序的前 top_k 条。"""
    store = InMemoryVectorStore()
    store.upsert([_record(300 + i, dense=_dense(1.0, 0.1 * i)) for i in range(6)])
    full = store.hybrid_search(dense=_dense(1.0), top_k=10)
    truncated = store.hybrid_search(dense=_dense(1.0), top_k=3)
    assert len(full) == 6
    assert [hit.chunk_id for hit in truncated] == [h.chunk_id for h in full[:3]]
    assert [hit.chunk_id for hit in truncated] == [300, 301, 302]  # 相似度随 i 递减


# ── 并发读写（线程安全）──
def test_concurrent_upsert_delete_search() -> None:
    """8 线程混合 upsert / delete / hybrid_search：无异常、无死锁，终态一致。

    每线程写入 50 条独立主键后删除其中偶数尾号条；最终应剩 8×25=200 条，
    且全部可被同一查询召回（向量相同、融合同分，chunk_id 稳定排序）。
    """
    store = InMemoryVectorStore()
    thread_count, per_thread = 8, 50
    errors: list[BaseException] = []

    def worker(thread_id: int) -> None:
        try:
            base = thread_id * 1000
            for j in range(per_thread):
                store.upsert([_record(base + j, dense=_dense(1.0), sparse={1: 1.0})])
                if j % 5 == 0:  # 写入过程中穿插检索（读侧不得观察到半写状态或抛错）
                    store.hybrid_search(dense=_dense(1.0), sparse={1: 1.0}, top_k=10)
            store.delete([base + j for j in range(0, per_thread, 2)])  # 删除偶数尾号
        except BaseException as exc:  # noqa: BLE001——测试收集任意失败
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(1, thread_count + 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "并发线程超时未结束（疑似死锁）"

    assert errors == []
    expected_ids = {
        t * 1000 + j for t in range(1, thread_count + 1) for j in range(1, per_thread, 2)
    }
    assert len(store) == len(expected_ids)
    hits = store.hybrid_search(dense=_dense(1.0), sparse={1: 1.0}, top_k=1000)
    assert {hit.chunk_id for hit in hits} == expected_ids
    # 双路向量完全相同：两路名次并列、同分按 chunk_id 稳定排序，第 i 名得分 = 2/(60+i)
    assert [hit.chunk_id for hit in hits] == sorted(expected_ids)
    assert hits[0].score == pytest.approx(2 / 61)
    assert hits[-1].score == pytest.approx(2 / (60 + len(expected_ids)))
