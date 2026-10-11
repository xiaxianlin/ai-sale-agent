# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""培训效果闭环单元测试（FR-TR-008，SR-TR-008 §6.1~§6.5；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类）：
- 等价类：三源缺口事件（mentor_zero_hit/drill_low_score/exam_wrong_point）统一入口；
- 边界：同知识点 3 条事件聚合 1 行（频次=3）/ resolved 再现计 hit 不计频次；
- 异常：重复事件幂等命中 / 非法来源拒绝 / 驳回缺 reason / 版本乐观锁冲突；
- 降级：考试错题 point_hint 缺失时回落文本归一化。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from services.training.core.loop.closure import (
    GAP_SOURCE_DRILL,
    GAP_SOURCE_EXAM,
    GAP_SOURCE_MENTOR,
    GAP_STATUS_OPEN,
    GAP_STATUS_RESOLVED,
    GapConflictError,
    LoopService,
    MissingFieldError,
)

_START = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """假时钟：手动推进（最近发生时间/统计窗口断言）。"""

    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now += timedelta(**kwargs)


class RecordingHook:
    """回流钩子记录桩：登记调用参数并派发确定性条目 uid（断言注入点被调用）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, point_tag: str, kb_type: str, item: dict[str, Any]) -> str:
        self.calls.append((point_tag, kb_type, dict(item)))
        return f"ui-rec-{len(self.calls):04d}"


def make_service(
    *, clock: FakeClock | None = None, hook: RecordingHook | None = None, mask_fn: Any = None
) -> tuple[LoopService, FakeClock, RecordingHook]:
    """标准装配：内存 store + 假时钟 + 记录桩回流钩子（mask_fn 可注入脱敏口径）。"""
    clk = clock or FakeClock()
    rec = hook or RecordingHook()
    return LoopService(clock=clk, reflow_hook=rec, mask_fn=mask_fn), clk, rec


# --------------------------------------------------------------------------- 事件采集（等价类/幂等）


class TestGapCollector:
    """三源缺口事件采集：统一入口 + 幂等去重。"""

    def test_emit_three_sources(self) -> None:
        """等价类：三源事件均可上报（mentor/drill/exam），各得唯一 event_id。"""
        service, _, _ = make_service()
        for source, ref_type in (
            (GAP_SOURCE_MENTOR, "mentor_session"),
            (GAP_SOURCE_DRILL, "exercise"),
            (GAP_SOURCE_EXAM, "exam"),
        ):
            event_id, dedup = service.emit_gap(source, ref_type, 1, "FOB 与 CIF 的报价差异是什么？")
            assert event_id and dedup == "new"

    def test_emit_dedup_hit(self) -> None:
        """同一事件重复上报 → dedup=hit，仅落 1 行（append-only 幂等）。"""
        service, _, _ = make_service()
        first = service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", 7, "信用证软条款怎么识别？")
        second = service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", 7, "信用证软条款怎么识别？")
        assert second[1] == "hit"
        assert first[0] == second[0] or len(service.store.list_events()) == 1

    def test_emit_invalid_source_or_empty_text(self) -> None:
        """异常：来源不在枚举 / 原文空白 → ValueError（拒绝落库）。"""
        service, _, _ = make_service()
        with pytest.raises(ValueError):
            service.emit_gap("ghost_source", "mentor_session", 1, "问题")
        with pytest.raises(ValueError):
            service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", 1, "   ")


# --------------------------------------------------------------------------- 聚合（归一化/频次/再命中）


class TestGapAggregator:
    """缺口聚合器：事件 → 归一化 → 缺口清单（频次/最近/样本）。"""

    def test_aggregate_creates_gap(self) -> None:
        """新事件聚合 → 开放态缺口 1 行，freq=1，kb_type 按来源建议。"""
        service, _, _ = make_service()
        service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", 1, "FOB 报价怎么算？")
        assert service.aggregate() == 1
        (gap,) = service.list_gaps()
        assert gap.freq == 1
        assert gap.status == GAP_STATUS_OPEN
        assert gap.kb_type  # 建议归属库非空

    def test_aggregate_merges_same_tag(self) -> None:
        """同知识点 3 条事件 → 清单 1 行 freq=3，last_seen 推进，样本收齐。"""
        service, clock, _ = make_service()
        for i in range(3):
            service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", i + 1, "信用证软条款怎么识别？")
            clock.advance(hours=1)
        assert service.aggregate() == 3
        (gap,) = service.list_gaps()
        assert gap.freq == 3
        assert len(gap.sample_events) == 3
        assert gap.last_seen_at > gap.first_seen_at

    def test_aggregate_exam_hint_precedence(self) -> None:
        """考试错题：context.point_hint 优先于题干归一化（题面不同同知识点合并）。"""
        service, _, _ = make_service()
        service.emit_gap(GAP_SOURCE_EXAM, "exam", 1, "题干甲：贸易术语", context={"point_hint": "Incoterms 2020"})
        service.emit_gap(GAP_SOURCE_EXAM, "exam", 1, "题干乙：装运条款", context={"point_hint": "Incoterms 2020"})
        service.aggregate()
        (gap,) = service.list_gaps()
        assert gap.freq == 2
        assert "incoterms" in gap.point_tag

    def test_aggregate_resolved_rehit(self) -> None:
        """已入库知识点再现：hit_count+1、freq 不变、first_hit_at 落点（闭环第三段信号）。"""
        service, clock, _ = make_service()
        service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", 1, "HS 编码归类错误怎么办？")
        service.aggregate()
        gap = service.submit_draft(service.list_gaps()[0].gap_id, "HS 编码归类", "归类流程：…")
        resolved = service.review_gap(gap.gap_id, approve=True, reviewer="管理员")
        assert resolved.status == GAP_STATUS_RESOLVED
        clock.advance(days=2)
        service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", 9, "HS 编码归类错误怎么办？")
        service.aggregate()
        again = service.list_gaps()[0]
        assert again.gap_id == resolved.gap_id
        assert again.status == GAP_STATUS_RESOLVED
        assert again.hit_count == 1
        assert again.freq == 1  # 再现不计频次
        assert again.first_hit_at is not None


# --------------------------------------------------------------------------- 审核回流工作流


class TestReviewWorkflow:
    """审核状态机：open→drafted→resolved/rejected；乐观锁与留痕。"""

    def test_review_approve_calls_reflow_hook(self) -> None:
        """批准：reflow_hook 注入被调用（tag/kb_type/条目），uid 回写 + 审计留痕。"""
        service, _, hook = make_service()
        service.emit_gap(GAP_SOURCE_DRILL, "exercise", 5, "客户压价时如何锚定价值？")
        service.aggregate()
        gap = service.submit_draft(service.list_gaps()[0].gap_id, "价值锚定话术", "先报价值区间…")
        resolved = service.review_gap(gap.gap_id, approve=True, reviewer="管理员")
        assert len(hook.calls) == 1
        tag, kb_type, item = hook.calls[0]
        assert item["title"] == "价值锚定话术"
        assert resolved.resolved_item_uid == "ui-rec-0001"
        assert resolved.reviewer == "管理员"
        actions = [e["action"] for e in service.audit_log()]
        assert "LOOP_GAP_REVIEW" in actions

    def test_review_approve_without_content_conflict(self) -> None:
        """异常：无草稿直接批准（缺条目内容）→ MissingFieldError（40902）。"""
        service, _, _ = make_service()
        service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", 1, "T/T 与 L/C 的风险差异？")
        service.aggregate()
        gap_id = service.list_gaps()[0].gap_id
        with pytest.raises(MissingFieldError):
            service.review_gap(gap_id, approve=True, reviewer="管理员")

    def test_review_reject_requires_reason_and_leaves_trace(self) -> None:
        """驳回：缺 reason → 40902 且状态不变；带 reason → rejected + 留痕。"""
        service, _, _ = make_service()
        service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", 1, "客户要求样品费减免？")
        service.aggregate()
        gap_id = service.list_gaps()[0].gap_id
        with pytest.raises(MissingFieldError):
            service.review_gap(gap_id, approve=False, reviewer="管理员")
        rejected = service.review_gap(gap_id, approve=False, reviewer="管理员", reason="内容与知识库重复")
        assert rejected.status == "rejected"
        assert rejected.reject_reason == "内容与知识库重复"
        assert rejected.reviewer == "管理员"

    def test_review_version_conflict(self) -> None:
        """并发审核：携带过期 version → GapConflictError（40901 乐观锁）。"""
        service, _, _ = make_service()
        service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", 1, "paypal 手续费谁承担？")
        service.aggregate()
        gap = service.submit_draft(service.list_gaps()[0].gap_id, "T", "C")
        stale_version = gap.version - 1
        with pytest.raises(GapConflictError):
            service.review_gap(gap.gap_id, approve=True, reviewer="管理员", version=stale_version)

    def test_talk_approve_and_mask(self) -> None:
        """话术沉淀：入库前过脱敏注入（mask_fn）；批准回流 kb_type='skill'。"""
        service, _, hook = make_service(mask_fn=lambda text: text.replace("13800138000", "***"))
        talk = service.collect_talk(
            "drill_supervisor_marked", 11, "call 我 13800138000 细谈", scenario_key="C-03"
        )
        assert "13800138000" not in talk.talk_text
        assert talk.status == "pending"
        approved = service.review_talk(talk.talk_id, approve=True, reviewer="主管乙")
        assert approved.status == "approved"
        assert approved.item_uid == "ui-rec-0001"
        assert hook.calls[0][1] == "skill"


# --------------------------------------------------------------------------- 闭环率


class TestLoopMetrics:
    """缺口闭环率：识别 → 入库 → 再命中（工程过程指标）。"""

    def test_closure_rate(self) -> None:
        """10 识别 / 6 入库 / 4 再命中 → closure_rate=0.4（SR §6.5 口径）。"""
        service, _, _ = make_service()
        gap_ids: list[str] = []
        for i in range(10):
            service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", i + 1, f"独特问题编号{i}的合规要求？")
        service.aggregate()
        gap_ids = [g.gap_id for g in service.list_gaps()]
        assert len(gap_ids) == 10
        for gid in gap_ids[:6]:
            service.submit_draft(gid, f"标题{gid}", "内容正文")
            service.review_gap(gid, approve=True, reviewer="管理员")
        for i in range(4):
            service.emit_gap(GAP_SOURCE_MENTOR, "mentor_session", 100 + i, f"独特问题编号{i}的合规要求？")
        service.aggregate()
        snapshot = service.metrics()
        assert snapshot["identified"] == 10
        assert snapshot["resolved"] == 6
        assert snapshot["rehit"] == 4
        assert snapshot["closure_rate"] == 0.4

    def test_metrics_empty_window(self) -> None:
        """空窗口：identified=0 → closure_rate=None（不定义，不除零）。"""
        service, _, _ = make_service()
        snapshot = service.metrics()
        assert snapshot["identified"] == 0
        assert snapshot["closure_rate"] is None
