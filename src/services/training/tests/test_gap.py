# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""知识缺口测试（FR-TR-001 §6.3 / FR-TR-008：频次聚合 + resolved 回流）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.training.core.tutor.gap import (
    GAP_STATUS_PENDING,
    GAP_STATUS_RESOLVED,
    InMemoryGapStore,
    KnowledgeGapRecorder,
    fingerprint_of,
    normalize_question,
)


def _clock(start: datetime, step_s: int = 10):
    """假时钟：每次调用前进 step_s 秒（断言 last_asked_at 刷新）。"""
    state = {"now": start}

    def tick() -> datetime:
        state["now"] = state["now"] + timedelta(seconds=step_s)
        return state["now"]

    return tick


class TestNormalize:
    def test_fingerprint_aggregates_variants(self) -> None:
        """空白/大小写/标点差异 → 同指纹（频次聚合键）。"""
        a = fingerprint_of("ST-3200 的 最大承载？")
        b = fingerprint_of("st3200的最大承载")
        assert a == b
        assert fingerprint_of("完全不同的问题") != a

    def test_normalize_strips_and_lowercases(self) -> None:
        assert normalize_question("  Hello, 世界！ HELLO ") == "hello世界hello"


class TestGapRecorder:
    def test_first_record_creates_pending_gap(self) -> None:
        recorder = KnowledgeGapRecorder()
        gap = recorder.record("ST-3200 防水等级是多少？", session="s1")
        assert gap.status == GAP_STATUS_PENDING
        assert gap.hit_count == 1
        assert gap.sample_question == "ST-3200 防水等级是多少？"
        assert recorder.list_gaps(status=GAP_STATUS_PENDING) == [gap]

    def test_same_fingerprint_aggregates_hit_count(self) -> None:
        """同义问题两连问（空白/大小写/标点差异）：同一缺口行 hit_count=2，last_asked_at 刷新。"""
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        recorder = KnowledgeGapRecorder(clock=_clock(start))
        first = recorder.record("ST-3200 防水等级是多少？", session="s1")
        first_last_asked = first.last_asked_at  # 聚合返回同一对象，先留存刷新前时间
        second = recorder.record("st3200 防水等级是多少？!", session="s2")  # 归一化同指纹
        assert second.gap_id == first.gap_id  # 同一行（聚合，不新增）
        assert second.hit_count == 2
        assert second.last_asked_at > first_last_asked
        assert second.sample_question == "st3200 防水等级是多少？!"
        assert len(recorder.list_gaps()) == 1

    def test_distinct_questions_open_distinct_gaps(self) -> None:
        recorder = KnowledgeGapRecorder()
        recorder.record("问题甲", session="s")
        recorder.record("问题乙", session="s")
        assert len(recorder.list_gaps()) == 2

    def test_list_gaps_sorted_by_hotness(self) -> None:
        """清单按 hit_count 降序（FR-TR-008 热度口径）。"""
        recorder = KnowledgeGapRecorder()
        cold = recorder.record("冷门问题", session="s")
        hot = recorder.record("热门问题", session="s")
        recorder.record("热门问题 ", session="s")
        recorder.record("热门问题!", session="s")
        gaps = recorder.list_gaps()
        assert [g.gap_id for g in gaps] == [hot.gap_id, cold.gap_id]
        assert gaps[0].hit_count == 3

    def test_resolved_via_ingestion_hook_and_reopen(self) -> None:
        """入库回流钩子：开放缺口 → resolved（带 item_uid）；同问题再现开新缺口行。"""
        recorder = KnowledgeGapRecorder()
        gap = recorder.record("ST-3200 防水等级是多少？", session="s1")
        resolved = recorder.on_knowledge_ingested(
            "ST-3200 防水等级是多少？", item_uid="ui-0001"
        )
        assert resolved is not None and resolved.gap_id == gap.gap_id
        assert resolved.status == GAP_STATUS_RESOLVED
        assert resolved.resolved_item_uid == "ui-0001"
        # resolved 退出聚合：同问题再现 → 新开 pending 行
        reopened = recorder.record("ST-3200 防水等级是多少", session="s1")
        assert reopened.gap_id != gap.gap_id
        assert reopened.status == GAP_STATUS_PENDING and reopened.hit_count == 1
        # 无开放缺口时钩子安静返回 None
        recorder.on_knowledge_ingested("无人问过的问题", item_uid="ui-0002")
        assert recorder.on_knowledge_ingested("无人问过的问题", item_uid="ui-0002") is None

    def test_cite_fail_reason_accepted(self) -> None:
        recorder = KnowledgeGapRecorder()
        gap = recorder.record("某问题", session="s", reason="cite_fail")
        assert gap.reason == "cite_fail"

    def test_invalid_reason_rejected(self) -> None:
        recorder = KnowledgeGapRecorder()
        try:
            recorder.record("某问题", session="s", reason="unknown")
            raise AssertionError("非法 reason 应被拒绝")
        except ValueError as exc:
            assert "unknown" in str(exc)

    def test_store_swap_in_memory(self) -> None:
        """存储协议可替换（生产 PG 形态，协议不变）。"""
        store = InMemoryGapStore()
        recorder = KnowledgeGapRecorder(store=store)
        gap = recorder.record("问题", session="s")
        assert store.find_by_id(gap.gap_id) is gap
        assert store.list_all() == [gap]
