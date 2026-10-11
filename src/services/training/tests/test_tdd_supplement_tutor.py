# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""AI 导师链路 TDD 补齐测试（FR-TR-001，SR-TR-001 §9 之外的五类场景补强）。

按 TDD 新规五类场景审计既有 test_llm/test_gap/test_pipeline/test_api 后补齐：
- 等价类：引用编号重复/部分越界/单块窗口；缺口聚合键与原因（zero_hit/cite_fail）无关；
- 边界：历史窗口恰好 N 轮与 N+1 轮、答案截断恰好 400/401 字、摘要溢出 >5 条目分支、
  指纹归一化 200 字截断边界、[来源0] 编号下界；
- 异常：LLM 流式中断（token 已外发后抛 LlmDownstreamError）→ 50002 且不落库不记缺口；
- 并发：InMemoryQaStore 同 (session, idempotency_key) 并发 append 恰一胜者；
- 降级：SSE 断连（生成器中途 close）→ 不落库、不误记缺口、会话可继续（断线恢复）。

KPI 映射：SRS §16.2「回答可溯源 100% / 回答准确率」——引用校验边界与降级路径
直接守护 done.knowledge_refs 的可溯源凭证完整性（抽检核对口径）。

依据文档：docs/07-实现方案/TR/SR-TR-001-AI导师系统.md §6.1~§6.3、§9；
         docs/03-技术文档/02-开发规范.md §3。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, Sequence

import pytest

from services.training.core.tutor.gap import KnowledgeGapRecorder
from services.training.core.tutor.llm import LlmDownstreamError
from services.training.core.tutor.pipeline import (
    ANSWER_TRUNCATE,
    InMemoryQaStore,
    QaRecord,
    TutorDownstreamError,
    TutorPipeline,
    validate_citations,
)

from .conftest import drain, make_search_fn, sample_results


# --------------------------------------------------------------------------- 引用校验（闸 2）边界与等价类
# KPI 映射：SRS §16.2 回答可溯源 100%——越界/重复/下界编号是「编造来源」判定的最小单元。


class TestCitationBoundaries:
    def test_duplicate_same_source_passes(self) -> None:
        """等价类：同一来源重复引用合法（集合语义去重），仍判通过。"""
        ok, cited = validate_citations("要点甲 [来源1]，重申 [来源1]。", 2)
        assert ok and cited == {1}

    def test_zero_index_is_out_of_range(self) -> None:
        """边界：编号下界为 1，[来源0] 即越界（编造了不存在的下发块），零容忍判失败。"""
        ok, cited = validate_citations("事实 [来源0]", 1)
        assert not ok and cited == {0}

    def test_mixed_in_range_and_out_of_range_fails(self) -> None:
        """等价类：部分引用越界即整体失败（任一越界 = 回答不可信，不做部分通过）。"""
        ok, cited = validate_citations("甲 [来源1]，乙 [来源9]", 5)
        assert not ok and cited == {1, 9}

    def test_single_ref_window_boundary(self) -> None:
        """边界：n_refs=1 最小窗口——[来源1] 恰好通过，[来源2] 越界失败。"""
        assert validate_citations("依据 [来源1]", 1)[0] is True
        assert validate_citations("依据 [来源2]", 1)[0] is False


# --------------------------------------------------------------------------- 异常/降级：流中断与 SSE 断连
# KPI 映射：SRS §16.2 回答准确率——失败流绝不落库冒充有效回答，保证抽检分母干净。


class _BreakAfterFirstTokenLlm:
    """剧本桩：先产出 1 个 token 再抛下游故障（模拟 DeepSeek 流中断）。"""

    def __init__(self) -> None:
        self.calls = 0

    def generate_stream(
        self, messages: Sequence[Mapping[str, Any]], *, system: str
    ) -> Iterator[str]:
        self.calls += 1
        yield "先给一句点拨，"
        raise LlmDownstreamError("DeepSeek SSE 流中断（测试桩）")


class TestStreamBreakAndDisconnect:
    def test_llm_stream_break_maps_downstream_no_record_no_gap(self, qa_store, gap_recorder) -> None:
        """异常：token 已外发后流中断 → 50002；半截回答不落库、不误记知识缺口。"""
        pipeline = TutorPipeline(
            search_fn=make_search_fn(sample_results(2)),
            llm=_BreakAfterFirstTokenLlm(),
            qa_store=qa_store,
            gap_recorder=gap_recorder,
        )
        with pytest.raises(TutorDownstreamError, match="模型服务不可用"):
            drain(pipeline.ask("ST-3200 最大承载？", "s1"))
        assert qa_store.count("s1") == 0  # 闸 3 未达：半截生成不留痕
        assert gap_recorder.list_gaps() == []  # 系统故障≠知识缺口

    def test_sse_disconnect_mid_stream_no_record_and_session_recovers(
        self, pipeline, fake_llm, qa_store, gap_recorder
    ) -> None:
        """降级：客户端断连（生成器中途 close）→ 本轮不落库、不记缺口；同会话再问正常。"""
        gen = pipeline.ask("ST-3200 支架最大承载是多少？", "s1")
        first = next(gen)  # 消费首个 token 事件后断开
        assert first.type == "token"
        gen.close()  # 模拟 SSE 断连：starlette 关闭流式生成器
        assert qa_store.count("s1") == 0
        assert gap_recorder.list_gaps() == []
        # 断线恢复：同会话完整再问一轮，链路无残留影响
        events = drain(pipeline.ask("再问一次：最大承载是多少？", "s1"))
        assert events[-1].type == "done" and events[-1].data["is_gap"] is False
        assert qa_store.count("s1") == 1


# --------------------------------------------------------------------------- 边界：历史窗口与答案截断
# KPI 映射：SRS §16.2 回答准确率——窗口/截断口径决定多轮上下文质量（token 成本与连贯性）。


class TestHistoryWindowBoundaries:
    def test_exactly_n_turns_no_summary(self, pipeline, fake_llm, qa_store) -> None:
        """边界：历史恰好等于窗口 N=6 轮 → 全部原文保留，不产生摘要行。"""
        from .conftest import seed_history

        seed_history(qa_store, "s1", turns=6)
        drain(pipeline.ask("第七个问题", "s1"))
        messages = fake_llm.last_messages
        roles = [m["role"] for m in messages]
        assert roles == ["system"] + ["user", "assistant"] * 6 + ["user"]  # 仅【知识资料】一行 system
        assert all("【历史摘要】" not in m["content"] for m in messages)

    def test_n_plus_one_turns_compacts_single_overflow(self, pipeline, fake_llm, qa_store) -> None:
        """边界：历史 N+1=7 轮 → 恰溢出 1 轮进摘要，最近 6 轮原文保留。"""
        from .conftest import seed_history

        seed_history(qa_store, "s2", turns=7)
        drain(pipeline.ask("第八个问题", "s2"))
        messages = fake_llm.last_messages
        assert messages[0]["content"].startswith("【历史摘要】") and "共 1 轮" in messages[0]["content"]
        questions = [m["content"] for m in messages if m["role"] == "user"]
        assert "历史问题 1" not in questions and "历史问题 2" in questions

    def test_answer_truncation_boundary(self, fake_llm, hit_search, qa_store, gap_recorder) -> None:
        """边界：历史答案恰好 ANSWER_TRUNCATE=400 字逐字保留；401 字截断到 400 字。"""
        pipeline = TutorPipeline(
            search_fn=hit_search, llm=fake_llm, qa_store=qa_store, gap_recorder=gap_recorder
        )
        exact = "甲" * ANSWER_TRUNCATE
        over = "乙" * (ANSWER_TRUNCATE + 1)
        qa_store.append(QaRecord(question="q1", answer=exact, session="s400"))
        qa_store.append(QaRecord(question="q2", answer=over, session="s401"))
        # last_messages 只留最近一次入参，逐会话提问后比对
        drain(pipeline.ask("新问题", "s400"))
        kept = [m["content"] for m in fake_llm.last_messages if m["role"] == "assistant"][0]
        assert kept == exact  # 恰好 400 字：逐字保留不截断
        drain(pipeline.ask("新问题", "s401"))
        kept_over = [m["content"] for m in fake_llm.last_messages if m["role"] == "assistant"][0]
        assert kept_over == "乙" * ANSWER_TRUNCATE and len(kept_over) == ANSWER_TRUNCATE

    def test_summary_more_than_five_topics_appends_count_suffix(
        self, pipeline, fake_llm, qa_store
    ) -> None:
        """等价类：溢出轮 >5 → 摘要只列前 5 个问题并追加「等 N 轮」计数后缀。"""
        from .conftest import seed_history

        seed_history(qa_store, "s3", turns=12)  # 窗口 6 → 溢出 6 轮
        drain(pipeline.ask("第十三个问题", "s3"))
        summary = fake_llm.last_messages[0]["content"]
        assert summary.startswith("【历史摘要】") and "共 6 轮" in summary
        assert "历史问题 5" in summary and "历史问题 6" not in summary  # 只列前 5 个
        assert summary.rstrip().endswith("等 6 轮")


# --------------------------------------------------------------------------- 边界/等价类：缺口指纹归一化
# KPI 映射：SRS §16.2 回答准确率（间接）——指纹聚合口径决定 FR-TR-008 缺口热度清单可信度。


class TestGapFingerprintBoundaries:
    def test_fingerprint_truncates_at_norm_max_len(self) -> None:
        """边界：归一化截断恰好 200 字——201 字问题与 200 字问题同指纹（聚合键上限口径）。"""
        from services.training.core.tutor.gap import fingerprint_of, normalize_question

        base = "a" * 200
        assert normalize_question(base + "b") == base
        assert fingerprint_of(base + "b") == fingerprint_of(base)

    def test_gap_aggregates_across_reasons_same_fingerprint(self) -> None:
        """等价类：聚合键 = 归一化问题指纹，与原因枚举无关——zero_hit 与 cite_fail 同题并轨。"""
        recorder = KnowledgeGapRecorder()
        first = recorder.record("ST-3200 防水等级？", session="s1", reason="zero_hit")
        second = recorder.record("st3200 防水等级？", session="s2", reason="cite_fail")
        assert second.gap_id == first.gap_id
        assert first.hit_count == 2
        assert len(recorder.list_gaps()) == 1


# --------------------------------------------------------------------------- 并发：落库幂等键唯一约束
# KPI 映射：SRS §16.2 回答可溯源 100%——重复落库会污染抽检凭证与会话历史（双写即漂移）。


class TestConcurrentQaStore:
    def test_same_idempotency_key_concurrent_append_exactly_once(self) -> None:
        """并发：8 线程同 (session, key) append → 恰 1 成功，其余 DuplicateQaRecordError。"""
        from services.training.core.tutor.pipeline import DuplicateQaRecordError

        store = InMemoryQaStore()
        successes: list[int] = []
        conflicts: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            barrier.wait()  # 同步起跑，最大化竞态窗口
            try:
                store.append(
                    QaRecord(question="q", answer="a", session="s1", idempotency_key="msg-1")
                )
                won = 1
            except DuplicateQaRecordError:
                won = 0
            with lock:
                (successes if won else conflicts).append(index)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(successes) == 1 and len(conflicts) == 7
        assert store.count("s1") == 1  # 落库恰好一条（对齐 PG 唯一约束语义）
