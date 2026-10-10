# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""问答编排测试（FR-TR-001 / SR-TR-001 §9：三道闸、引用校验、历史压缩、幂等）。"""
from __future__ import annotations

import pytest

from services.training.core.tutor.llm import FakeTutorLlm
from services.training.core.tutor.pipeline import (
    NO_ANSWER_TEXT,
    TutorBadRequest,
    TutorDownstreamError,
    TutorPipeline,
    validate_citations,
)

from .conftest import ScriptedLlm, drain, make_search_fn, sample_results, seed_history

# --------------------------------------------------------------------------- 引用校验纯函数


class TestValidateCitations:
    def test_valid_subset_passes(self) -> None:
        ok, cited = validate_citations("要点甲 [来源1]，要点乙 [来源2]", 3)
        assert ok and cited == {1, 2}

    def test_missing_citation_fails(self) -> None:
        ok, cited = validate_citations("这里没有任何引用标记", 3)
        assert not ok and cited == set()

    def test_out_of_range_fails(self) -> None:
        """越界引用 = 编造未下发来源，零容忍（SR-TR-001 §9 test_citation_out_of_range）。"""
        ok, _ = validate_citations("事实 [来源9]", 5)
        assert not ok


# --------------------------------------------------------------------------- 闸 1：零命中不调 LLM


class TestZeroHit:
    def test_zero_hit_never_calls_llm(self, qa_store, gap_recorder) -> None:
        """零命中：固定话术 + 缺口登记，LLM 调用次数必须为 0（零编造闸 1）。"""
        search = make_search_fn([])  # 恒空结果
        llm = FakeTutorLlm()
        pipeline = TutorPipeline(search_fn=search, llm=llm, qa_store=qa_store, gap_recorder=gap_recorder)
        events = drain(pipeline.ask("宇宙尽头的答案是几？", "s1"))
        assert llm.calls == 0  # 桩计数断言：检索空 → 生成侧完全不触发
        types = [e.type for e in events]
        assert types == ["gap", "done"]
        assert events[0].data["message"] == NO_ANSWER_TEXT
        assert "知识库暂无该内容，已记录" in events[0].data["message"]
        done = events[1].data
        assert done["is_gap"] is True and done["knowledge_refs"] == []
        # 缺口已登记（pending，hit_count=1）
        gaps = gap_recorder.list_gaps()
        assert len(gaps) == 1 and gaps[0].hit_count == 1 and gaps[0].reason == "zero_hit"
        # 落库闸：兜底话术也落库留痕
        records = qa_store.list_by_session("s1")
        assert len(records) == 1 and records[0].is_gap and records[0].answer == NO_ANSWER_TEXT

    def test_question_validation(self, pipeline) -> None:
        """空问题/超长问题：40001 口径（不进检索不落库）。"""
        with pytest.raises(TutorBadRequest):
            drain(pipeline.ask("   ", "s"))
        with pytest.raises(TutorBadRequest):
            drain(pipeline.ask("长" * 501, "s"))

    def test_search_failure_maps_downstream_no_gap(self, fake_llm, qa_store, gap_recorder) -> None:
        """检索故障：统一 50002，不调 LLM、不记缺口（系统故障≠知识缺口）。"""

        def broken_search(**kwargs):
            raise TimeoutError("kb down")

        pipeline = TutorPipeline(
            search_fn=broken_search, llm=fake_llm, qa_store=qa_store, gap_recorder=gap_recorder
        )
        with pytest.raises(TutorDownstreamError, match="知识服务暂不可用"):
            drain(pipeline.ask("任意问题", "s"))
        assert fake_llm.calls == 0
        assert gap_recorder.list_gaps() == []
        assert qa_store.count() == 0


# --------------------------------------------------------------------------- 命中路径：引用校验通过


class TestHitPath:
    def test_hit_answer_with_valid_citations(self, pipeline, fake_llm, hit_search, qa_store) -> None:
        """命中：token 流 → done（含 knowledge_refs），引用全部对回检索块。"""
        events = drain(pipeline.ask("ST-3200 支架最大承载是多少？", "s1"))
        types = [e.type for e in events]
        assert types[0] == "token" and types[-1] == "done" and "gap" not in types
        answer = "".join(e.data["text"] for e in events if e.type == "token")
        done = events[-1].data
        assert done["answer"] == answer  # token 聚合 = 落库回答
        assert done["is_gap"] is False
        refs = done["knowledge_refs"]
        assert [r["no"] for r in refs] == [1, 2]
        assert refs[0]["chunk_id"] == 1000
        # 引用编号全部落在下发的知识块内（闸 2 通过）
        ok, cited = validate_citations(answer, len(refs))
        assert ok and cited and cited <= {r["no"] for r in refs}
        # 检索入参形态（FR-KB-030 契约：kb_types 默认六库）
        assert hit_search.calls[0]["query"] == "ST-3200 支架最大承载是多少？"
        assert set(hit_search.calls[0]["kb_types"]) == {
            "product", "customer", "skill", "process", "negative_case", "training"
        }
        assert qa_store.count("s1") == 1

    def test_kb_types_configurable(self, fake_llm, qa_store, gap_recorder) -> None:
        """kb_types 可配（本例只查产品库 + 流程库）。"""
        search = make_search_fn(sample_results(1))
        pipeline = TutorPipeline(
            search_fn=search, llm=fake_llm, qa_store=qa_store, gap_recorder=gap_recorder,
            kb_types=["product", "process"],
        )
        drain(pipeline.ask("问题", "s"))
        assert search.calls[0]["kb_types"] == ["product", "process"]


# --------------------------------------------------------------------------- 闸 2：无引用回答降级


class TestCitationFallback:
    def test_fabricated_answer_without_citation_degrades(self, qa_store, gap_recorder) -> None:
        """伪造无引用回答：弃用生成内容 → 降级为仅来源列表（零编造闸 2）。"""
        search = make_search_fn(sample_results(2))
        llm = ScriptedLlm("这个问题据我所知应该是四十五千克左右。")  # 无 [来源N]
        pipeline = TutorPipeline(search_fn=search, llm=llm, qa_store=qa_store, gap_recorder=gap_recorder)
        events = drain(pipeline.ask("ST-3200 最大承载？", "s1"))
        types = [e.type for e in events]
        # token 已流出（数量取决于剧本长度），随后 gap 降级事件 + done 终态
        assert types[0] == "token" and types[-2:] == ["gap", "done"]
        gap_event, done = events[-2].data, events[-1].data
        assert gap_event["reason"] == "cite_fail"
        assert done["is_gap"] is True
        # 降级回答 = 仅来源列表，自带合法 [来源N]（仍可溯源）
        assert done["answer"].startswith("生成的回答未通过引用校验")
        ok, cited = validate_citations(done["answer"], len(done["knowledge_refs"]))
        assert ok and cited == {1, 2}
        assert "四十五千克" not in done["answer"]  # 编造内容不落库
        gaps = gap_recorder.list_gaps()
        assert gaps[0].reason == "cite_fail"

    def test_out_of_range_citation_degrades(self, qa_store, gap_recorder) -> None:
        """越界引用（[来源9] 但仅 2 块）：同样判失败走降级。"""
        search = make_search_fn(sample_results(2))
        llm = ScriptedLlm("答案见神秘资料 [来源9]。")
        pipeline = TutorPipeline(search_fn=search, llm=llm, qa_store=qa_store, gap_recorder=gap_recorder)
        events = drain(pipeline.ask("任意", "s1"))
        assert events[-1].data["is_gap"] is True
        assert "[来源9]" not in events[-1].data["answer"]


# --------------------------------------------------------------------------- 多轮历史与压缩


class TestHistoryWindow:
    def test_window_overflow_compacts_to_summary(self, pipeline, fake_llm, qa_store) -> None:
        """8 轮历史（窗口 6）：消息含历史摘要行 + 最近 6 轮原文。"""
        seed_history(qa_store, "s1", turns=8)
        drain(pipeline.ask("第九个问题", "s1"))
        messages = fake_llm.last_messages
        roles = [m["role"] for m in messages]
        # 窗口外 2 轮 → 1 行摘要（system）+ 知识块（system）+ 6×(user, assistant) + 本次 user
        assert roles == ["system", "system"] + ["user", "assistant"] * 6 + ["user"]
        assert len(messages) == 2 + 12 + 1
        summary = messages[0]["content"]
        assert summary.startswith("【历史摘要】") and "共 2 轮" in summary
        assert "禁止作为回答依据" in summary
        # 最近 6 轮原文保留（历史问题 3~8），窗口外的第 1~2 轮只剩摘要
        questions = [m["content"] for m in messages if m["role"] == "user"]
        assert "历史问题 3" in questions and "历史问题 8" in questions
        assert "历史问题 1" not in questions and "历史问题 2" not in questions
        assert questions[-1] == "第九个问题"

    def test_within_window_no_summary(self, pipeline, fake_llm, qa_store) -> None:
        """窗口内（2 轮）：不产生摘要行。"""
        seed_history(qa_store, "s2", turns=2)
        drain(pipeline.ask("第三个问题", "s2"))
        system_messages = [m for m in fake_llm.last_messages if m["role"] == "system"]
        assert len(system_messages) == 1  # 仅【知识资料】块
        assert "【历史摘要】" not in system_messages[0]["content"]

    def test_window_configurable(self, fake_llm, qa_store, gap_recorder) -> None:
        """窗口可配：window=2 时 4 轮历史只保留最近 2 轮。"""
        search = make_search_fn(sample_results(1))
        pipeline = TutorPipeline(
            search_fn=search, llm=fake_llm, qa_store=qa_store, gap_recorder=gap_recorder,
            history_window=2,
        )
        seed_history(qa_store, "s3", turns=4)
        drain(pipeline.ask("第五个问题", "s3"))
        messages = fake_llm.last_messages
        questions = [m["content"] for m in messages if m["role"] == "user"]
        assert questions[-3:] == ["历史问题 3", "历史问题 4", "第五个问题"]
        assert messages[0]["content"].startswith("【历史摘要】") and "共 2 轮" in messages[0]["content"]


# --------------------------------------------------------------------------- 幂等


class TestIdempotency:
    def test_same_key_replays_without_duplicate_records(
        self, pipeline, fake_llm, hit_search, qa_store
    ) -> None:
        """同 idempotency-key 重发：不重复检索、不重复调 LLM、不重复落库，重放 done。"""
        first = drain(pipeline.ask("第一次提问", "s1", idempotency_key="msg-001"))
        assert fake_llm.calls == 1 and qa_store.count("s1") == 1
        search_calls_before = len(hit_search.calls)
        second = drain(pipeline.ask("第一次提问", "s1", idempotency_key="msg-001"))
        # 重放：无检索、无生成、无新落库
        assert len(hit_search.calls) == search_calls_before
        assert fake_llm.calls == 1
        assert qa_store.count("s1") == 1
        assert [e.type for e in second] == ["done"]
        assert second[0].data["idempotent_replay"] is True
        assert second[0].data["record_id"] == first[-1].data["record_id"]
        assert second[0].data["knowledge_refs"] == first[-1].data["knowledge_refs"]

    def test_different_key_records_again(self, pipeline, qa_store) -> None:
        drain(pipeline.ask("问题", "s1", idempotency_key="k1"))
        drain(pipeline.ask("问题", "s1", idempotency_key="k2"))
        assert qa_store.count("s1") == 2

    def test_store_rejects_duplicate_idem_key(self, qa_store) -> None:
        """存储层唯一约束兜底：同 (session, key) 直接 append 被拒。"""
        from services.training.core.tutor.pipeline import DuplicateQaRecordError, QaRecord

        qa_store.append(QaRecord(question="q", answer="a", session="s", idempotency_key="k"))
        with pytest.raises(DuplicateQaRecordError):
            qa_store.append(QaRecord(question="q", answer="a", session="s", idempotency_key="k"))
