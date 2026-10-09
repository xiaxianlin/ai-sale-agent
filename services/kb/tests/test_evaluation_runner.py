# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""批量评测执行器测试（FR-KB-016 / SR-KB-016 §9：命中/MRR 手算对照，迭代 0.3）。

search_fn 为确定性桩（鸭子契约：query/kb_types/filters/top_k →
results[{chunk_id, score}]）；覆盖：单条命中名次与 Top5 边界、MRR 手算、
统一检索 API data 段形态解析（rewritten_query 上浮）、单条异常降级为
未命中且不中断整批、线程池并发上限与结果保序。
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from services.kb.core.evaluation.dataset import EvalCase, EvalDataset
from services.kb.core.evaluation.runner import EvalRunner, parse_search_response


class StubSearch:
    """确定性检索桩：query → 结果列表；记录每次调用入参供契约断言。"""

    def __init__(self, mapping: dict[str, list[dict[str, Any]]]) -> None:
        self._mapping = mapping
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(
        self,
        *,
        query: str,
        kb_types: list[str] | None = None,
        filters: dict[str, list[str]] | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self.calls.append(
                {"query": query, "kb_types": kb_types, "filters": filters, "top_k": top_k}
            )
        return self._mapping.get(query, [])


class FlakySearch:
    """故障桩：指定 query 抛错（其余返回空结果）——单条降级口径。"""

    def __init__(self, fail_query: str) -> None:
        self._fail_query = fail_query

    def __call__(self, *, query: str, top_k: int = 5, **_kwargs: Any) -> list[dict[str, Any]]:
        if query == self._fail_query:
            raise TimeoutError("检索链路超时（测试桩）")
        return [{"chunk_id": 999, "score": 0.1}]


class SleepySearch:
    """慢查询桩：记录并发峰值（线程池并发上限验证）。"""

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s
        self._current = 0
        self.peak = 0
        self._lock = threading.Lock()

    def __call__(self, *, query: str, top_k: int = 5, **_kwargs: Any) -> list[dict[str, Any]]:
        with self._lock:
            self._current += 1
            self.peak = max(self.peak, self._current)
        time.sleep(self._delay_s)
        with self._lock:
            self._current -= 1
        return [{"chunk_id": 1, "score": 1.0}]


def _case(case_id: str, query: str, expected: tuple[int, ...], **overrides: Any) -> EvalCase:
    return EvalCase(case_id=case_id, query=query, expected_chunk_ids=expected,
                    kb_type="product", **overrides)


def _results(ids: list[int]) -> list[dict[str, Any]]:
    """构造 [{chunk_id, score}] 形态（得分单调递减，模拟排序结果）。"""
    return [{"chunk_id": cid, "score": round(1.0 - index * 0.1, 3)}
            for index, cid in enumerate(ids)]


# ─────────────────────────────── 命中名次与 Top5 边界（SR §9 手算对照）───────────────────────────────


def test_命中名次手算对照() -> None:
    """expected=[7]、retrieved=[3,7,9,1,5] → hit_rank=2、hit@5=True、mrr=0.5。"""
    stub = StubSearch({"q": _results([3, 7, 9, 1, 5])})
    results = EvalRunner(stub).run([_case("C1", "q", (7,))])
    assert len(results) == 1
    item = results[0]
    assert item.hit_at_5 is True
    assert item.hit_rank == 2
    assert item.mrr == pytest.approx(0.5)
    assert item.error is None
    assert item.latency_ms >= 0


def test_目标块在第6位判未命中() -> None:
    """目标块名次 6（窗口外）→ hit@5=False、mrr=0、hit_rank=None（Top5 口径）。"""
    stub = StubSearch({"q": _results([3, 1, 9, 5, 2, 7])})
    results = EvalRunner(stub).run([_case("C1", "q", (7,))])
    assert results[0].hit_at_5 is False
    assert results[0].hit_rank is None
    assert results[0].mrr == 0.0


def test_多期望块任一命中即算() -> None:
    """expected 多块（多答案/同义块）：任一命中即命中，名次取最先者。"""
    stub = StubSearch({"q": _results([8, 4, 9])})
    results = EvalRunner(stub).run([_case("C1", "q", (7, 9))])
    assert results[0].hit_at_5 is True
    assert results[0].hit_rank == 3
    assert results[0].mrr == pytest.approx(1 / 3)


# ─────────────────────────────── search_fn 契约 ───────────────────────────────


def test_桩检索入参契约() -> None:
    """按用例库类型隔离发起检索：kb_types=[kb_type]、filters 直传、top_k 透传。"""
    stub = StubSearch({"q1": _results([1]), "q2": _results([2])})
    cases = [
        _case("C1", "q1", (1,)),
        _case("C2", "q2", (2,), filters={"sku": ["ST-3200"]}),
    ]
    EvalRunner(stub, top_k=5).run(cases)
    assert [call["query"] for call in stub.calls] == ["q1", "q2"]
    assert all(call["kb_types"] == ["product"] for call in stub.calls)
    assert all(call["top_k"] == 5 for call in stub.calls)
    assert stub.calls[0]["filters"] is None
    assert stub.calls[1]["filters"] == {"sku": ["ST-3200"]}


def test_统一检索API形态解析_rewritten_query上浮() -> None:
    """dict 形态返回（data 段）：results 供命中判定，rewritten_query 供归因。"""
    payload = {"results": _results([11, 12]), "rewritten_query": "ST-3200 承载 认证",
               "latency_ms": 42}
    assert parse_search_response(payload) == ([11, 12], "ST-3200 承载 认证")
    assert parse_search_response(_results([3])) == ([3], "")
    assert parse_search_response(None) == ([], "")


def test_结果保序与数据集便捷入口() -> None:
    """run/run_dataset 结果与用例同序（case_id 逐位对应）。"""
    stub = StubSearch({"q1": _results([1]), "q2": [], "q3": _results([3])})
    cases = [_case("C1", "q1", (1,)), _case("C2", "q2", (2,)), _case("C3", "q3", (3,))]
    dataset = EvalDataset(name="smoke", version=1, cases=cases)
    results = EvalRunner(stub).run_dataset(dataset)
    assert [item.case_id for item in results] == ["C1", "C2", "C3"]
    assert [item.hit_at_5 for item in results] == [True, False, True]


# ─────────────────────────────── 异常降级 ───────────────────────────────


def test_单条异常降级为未命中且整批不中断() -> None:
    """超时用例按未命中计入分母（保守口径），error 留痕，其余用例正常评测。"""
    runner = EvalRunner(FlakySearch("q_bad"))
    results = runner.run([
        _case("C1", "q_bad", (7,)),
        _case("C2", "q_ok", (999,)),
    ])
    failed, ok = results
    assert failed.hit_at_5 is False
    assert failed.mrr == 0.0
    assert failed.error is not None and "TimeoutError" in failed.error
    assert ok.hit_at_5 is True  # 整批未被单条故障中断


# ─────────────────────────────── 线程池并发 ───────────────────────────────


def test_并发上限受控且确实并行() -> None:
    """并发 2：峰值不超 2；并发 4：慢查询下峰值 >1（线程池真实并行）。"""
    cases = [_case(f"C{i}", f"q{i}", (1,)) for i in range(6)]

    limited = SleepySearch(0.05)
    EvalRunner(limited, concurrency=2).run(cases)
    assert limited.peak <= 2

    wide = SleepySearch(0.08)
    EvalRunner(wide, concurrency=4).run(cases)
    assert wide.peak > 1


def test_构造参数越界拒绝() -> None:
    """并发 0 / top_k 0 → ValueError（配置取值范围守卫）。"""
    with pytest.raises(ValueError, match="concurrency"):
        EvalRunner(StubSearch({}), concurrency=0)
    with pytest.raises(ValueError, match="top_k"):
        EvalRunner(StubSearch({}), top_k=0)
