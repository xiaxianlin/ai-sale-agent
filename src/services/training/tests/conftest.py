# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""AI 导师单元测试公共夹具（FR-TR-001，SR-TR-001 §9 测试要点）。"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pytest

from services.training.core.tutor.gap import KnowledgeGapRecorder
from services.training.core.tutor.llm import FakeTutorLlm
from services.training.core.tutor.pipeline import (
    InMemoryQaStore,
    QaRecord,
    TutorPipeline,
)


def make_search_fn(results: Sequence[dict[str, Any]]):
    """构造桩检索：恒返回给定结果（记录调用次数与入参，供断言）。"""

    class _StubSearch:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def __call__(self, *, query: str, kb_types=None, filters=None, top_k: int = 5):
            self.calls.append(
                {"query": query, "kb_types": list(kb_types or []), "filters": filters, "top_k": top_k}
            )
            return {"results": [dict(r) for r in results]}

    return _StubSearch()


def sample_results(n: int = 2) -> list[dict[str, Any]]:
    """构造 n 条样例检索块（带 title/kb_type/text，模拟回表富化形态）。"""
    return [
        {
            "chunk_id": 1000 + i,
            "score": 0.9 - i * 0.1,
            "title": f"样例知识块 {i + 1}",
            "kb_type": "product",
            "text": f"第 {i + 1} 块正文：壁挂支架最大承载 {35 + i} 千克，通过 CE 认证。",
        }
        for i in range(n)
    ]


class ScriptedLlm:
    """剧本桩：无视输入恒定产出预设文本流（测引用校验降级路径）。"""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0

    def generate_stream(
        self, messages: Sequence[Mapping[str, Any]], *, system: str
    ) -> Iterator[str]:
        self.calls += 1
        step = 8
        for i in range(0, len(self.answer), step):
            yield self.answer[i : i + step]


@pytest.fixture
def fake_llm() -> FakeTutorLlm:
    """确定性教学桩（记录调用次数与最近入参）。"""
    return FakeTutorLlm()


@pytest.fixture
def qa_store() -> InMemoryQaStore:
    return InMemoryQaStore()


@pytest.fixture
def gap_recorder() -> KnowledgeGapRecorder:
    return KnowledgeGapRecorder()


@pytest.fixture
def hit_search():
    """恒命中 2 块的桩检索。"""
    return make_search_fn(sample_results(2))


@pytest.fixture
def pipeline(hit_search, fake_llm, qa_store, gap_recorder) -> TutorPipeline:
    """标准装配：命中检索 + Fake 桩 + 内存存储。"""
    return TutorPipeline(
        search_fn=hit_search, llm=fake_llm, qa_store=qa_store, gap_recorder=gap_recorder
    )


def drain(events) -> list:
    """耗尽事件流为列表（测试便利）。"""
    return list(events)


def seed_history(store: InMemoryQaStore, session: str, turns: int) -> list[QaRecord]:
    """向会话预置 n 轮历史（直接落库，绕过链路以精确控制窗口）。"""
    records: list[QaRecord] = []
    for i in range(turns):
        record = QaRecord(
            question=f"历史问题 {i + 1}",
            answer=f"历史回答 {i + 1} [来源1]",
            session=session,
            knowledge_refs=[{"no": 1, "chunk_id": 1001, "kb_type": "product", "title": "t", "score": 0.9}],
        )
        store.append(record)
        records.append(record)
    return records
