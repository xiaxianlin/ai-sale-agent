# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""培训效果报告装配用例（FR-TR-009，SR-TR-009 §6.2/§9；TDD 阶段①，迭代 1.3）。

五类场景落位：
- 等价类：批次总览/通过率/雷达均值/缺口统计；个人历程/成绩单/雷达演进/证书状态；
- 边界：周期缩短率 50.00%（SRS §16.2 KPI 数据位）、无结业样本不足、205 人键集
  分页（页大小 200）无重复无遗漏、明细流式装配不整批驻留内存；
- 异常：批次/学员不存在（40404）；
- 降级：KPI 数据位 OI-10 未冻结渲染为占位（不编造数值）、sections 勾选裁剪装配。
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from services.training.core.board.report import (
    BatchNotFound,
    ReportAssembler,
    ReportConfig,
    TraineeNotFound,
    keyset_iterate,
)

from .test_board_fakes import FakeReportSource

BATCH_ID = 1
TRAINEE_ID = 101


def make_source() -> FakeReportSource:
    """标准批次：3 名成员（2 结业周期 40/50 天、1 流失）+ 雷达均值 + 缺口。"""
    return FakeReportSource(
        batches={BATCH_ID: {"batch_id": BATCH_ID, "name": "2026Q4 新人第 3 期",
                            "baseline_manual_days": 90, "period_start": "2026-09-01",
                            "period_end": None, "status": "active"}},
        members={BATCH_ID: [
            {"id": 1, "trainee_id": 101, "name": "张三", "status": "certified", "cycle_days": 40,
             "cert_status": "certified", "radar_current": {"product": 75.0}, "ex_n": 12, "exam_pass_n": 1},
            {"id": 2, "trainee_id": 102, "name": "李四", "status": "certified", "cycle_days": 50,
             "cert_status": "certified", "radar_current": {"product": 65.0}, "ex_n": 8, "exam_pass_n": 1},
            {"id": 3, "trainee_id": 103, "name": "王五", "status": "dropped", "cycle_days": None,
             "cert_status": "uncertified", "radar_current": {"product": 40.0}, "ex_n": 2, "exam_pass_n": 0},
        ]},
        radar_means={BATCH_ID: {
            "initial": {"product": 55.0, "communication": 50.0, "negotiation": 45.0, "platform": 52.0},
            "current": {"product": 60.0, "communication": 58.0, "negotiation": 50.0, "platform": 61.0},
        }},
        gaps={BATCH_ID: [{"gap_theme": "价格异议", "count": 3}, {"gap_theme": "物流条款", "count": 1}]},
        individuals={TRAINEE_ID: {
            "profile": {"trainee_id": TRAINEE_ID, "name": "张三", "cert_status": "certified",
                        "onboarded_at": "2026-08-01"},
            "timeline": [{"date": "2026-10-01", "kind": "drill", "summary": "产品参数演练 85 分"},
                         {"date": "2026-10-02", "kind": "qa", "summary": "提问：CE 认证流程"}],
            "scores": [
                {"date": "2026-10-01", "kind": "drill", "title": "产品参数", "score": 85.0, "passed": None},
                {"date": "2026-10-03", "kind": "exam", "title": "结业考试", "score": 88.0, "passed": True},
            ],
            "radar_evolution": [
                {"stage": "initial", "date": "2026-08-01", "radar": {"product": 55.0}},
                {"stage": "latest", "date": "2026-10-03", "radar": {"product": 75.0}},
            ],
            "cycle": {"joined_at": "2026-08-01", "certified_at": "2026-09-15", "cycle_days": 45},
        }},
    )


def test_batch_summary_counts_and_rate() -> None:
    """批次总览：人数/结业/流失/认证通过率与雷达四维均值结构。"""
    data = ReportAssembler(make_source()).assemble_batch(BATCH_ID)
    assert data.dimension == "batch"
    assert data.summary["total"] == 3
    assert data.summary["certified"] == 2
    assert data.summary["dropped"] == 1
    assert data.summary["cert_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert set(data.radar["initial"]) == {"product", "communication", "negotiation", "platform"}
    assert data.radar["initial"]["product"] == 55.0
    assert data.radar["current"]["product"] == 60.0


def test_cycle_reduction_kpi() -> None:
    """KPI 数据位：结业均周期 45 天 / 基线 90 → 缩短率 50.00%（SRS §16.2 培训周期缩短）。"""
    data = ReportAssembler(make_source()).assemble_batch(BATCH_ID)
    kpi = data.summary["cycle_kpi"]
    assert kpi["avg_cycle_days"] == 45.0
    assert kpi["baseline_manual_days"] == 90
    assert kpi["reduction_pct"] == 50.0
    assert kpi["note"]


def test_cycle_reduction_insufficient_sample() -> None:
    """无结业样本 → 缩短率 null + 「样本不足」标注，禁止展示误导值。"""
    source = make_source()
    for member in source.members[BATCH_ID]:
        member["status"] = "dropped"
        member["cycle_days"] = None
    data = ReportAssembler(source).assemble_batch(BATCH_ID)
    kpi = data.summary["cycle_kpi"]
    assert kpi["reduction_pct"] is None
    assert kpi["avg_cycle_days"] is None
    assert "样本不足" in kpi["note"]


def test_batch_gaps_sorted_desc() -> None:
    """缺口统计按命中数降序（回流 FR-TR-008 的可读口径）。"""
    data = ReportAssembler(make_source()).assemble_batch(BATCH_ID)
    counts = [g["count"] for g in data.gaps]
    assert counts == sorted(counts, reverse=True)
    assert data.gaps[0]["gap_theme"] == "价格异议"


def test_keyset_pagination_complete() -> None:
    """键集分页：205 人 × 页大小 200 → 两页取全、无重复无遗漏、游标单调递增。"""
    source = FakeReportSource(
        batches={BATCH_ID: {"batch_id": BATCH_ID, "name": "大批次", "baseline_manual_days": 90}},
        members={BATCH_ID: [
            {"id": i, "trainee_id": 1000 + i, "name": f"学员{i}", "status": "in_training",
             "cycle_days": None, "cert_status": "uncertified",
             "radar_current": {}, "ex_n": 0, "exam_pass_n": 0}
            for i in range(1, 206)
        ]},
    )
    rows = list(keyset_iterate(lambda after, limit: source.fetch_member_page(BATCH_ID, after, limit),
                               page_size=200, cursor_of=lambda page: page[-1]["id"]))
    assert len(rows) == 205
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == 205  # 无重复
    assert set(ids) == set(range(1, 206))  # 无遗漏
    assert ids == sorted(ids)  # 游标单调（id 升序）
    assert len(source.fetch_log) == 2  # 200 + 5 两页


def test_keyset_iterate_stops_on_short_page() -> None:
    """键集迭代终止条件：短页即止（不足页大小 → 最后一页）。"""
    calls: list[int] = []

    def fetch(after: int, limit: int) -> list[dict[str, Any]]:
        calls.append(after)
        rows = [{"id": after + i} for i in range(1, min(limit, 3) + 1)]  # 恒 3 行 < 页大小
        return rows

    rows = list(keyset_iterate(fetch, page_size=10, cursor_of=lambda page: page[-1]["id"]))
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert len(calls) == 1  # 短页终止，不多取一轮
    # 满页 + 空页终止路径：恰好整页数据时再多取一轮空页后停止
    full_calls: list[int] = []

    def fetch_full(after: int, limit: int) -> list[dict[str, Any]]:
        full_calls.append(after)
        if len(full_calls) == 1:  # 第一页：满页 10 行
            return [{"id": after + i} for i in range(1, limit + 1)]
        return []  # 第二页：空页终止

    exact = list(keyset_iterate(fetch_full, page_size=10, cursor_of=lambda page: page[-1]["id"]))
    assert len(exact) == 10 and len(full_calls) == 2  # 满页后取一轮空页终止


def test_detail_rows_streamed_not_materialized() -> None:
    """大批量装配不超内存：detail_rows 为惰性迭代器，消费 1 行仅触发 1 页取数。"""
    source = make_source()
    data = ReportAssembler(source).assemble_batch(BATCH_ID)
    assert isinstance(data.detail_rows, Iterator), "明细必须流式（生成器），禁止整批列表"
    first = next(iter(data.detail_rows))
    assert first["trainee_id"] == 101
    assert len(source.fetch_log) == 1  # 惰性：只取了第一页
    assert all(limit <= 200 for _, _, limit in source.fetch_log)


def test_individual_assembly_full_sections() -> None:
    """个人维度：历程时间线/成绩单/雷达演进/证书状态四区齐全。"""
    data = ReportAssembler(make_source()).assemble_individual(TRAINEE_ID)
    assert data.dimension == "individual"
    assert data.summary["name"] == "张三"
    assert data.summary["cert_status"] == "certified"
    assert len(data.summary["timeline"]) == 2  # 学习历程
    assert len(list(data.detail_rows)) == 2  # 成绩单：演练 + 考试
    stages = [r["stage"] for r in data.radar["evolution"]]
    assert stages == ["initial", "latest"]
    assert data.summary["cycle"]["cycle_days"] == 45  # 个人培训周期（KPI 分子数据位）


def test_individual_uncertified_cycle() -> None:
    """未认证学员：周期 null（无 certified_at），报告不得编造结业天数。"""
    source = make_source()
    source.individuals[TRAINEE_ID]["profile"]["cert_status"] = "in_training"
    source.individuals[TRAINEE_ID]["cycle"] = None
    data = ReportAssembler(source).assemble_individual(TRAINEE_ID)
    assert data.summary["cycle"] is None


def test_sections_filter_prunes_assembly() -> None:
    """章节勾选：sections=(2,3,4) → 缺口/明细数据不装配（数据源零调用）。"""
    source = make_source()
    cfg = ReportConfig(sections=(2, 3, 4))
    data = ReportAssembler(source).assemble_batch(BATCH_ID, cfg)
    assert source.gap_calls == 0  # 第 5 章未勾选 → 缺口统计不查询
    assert list(data.detail_rows) == []  # 第 6 章未勾选 → 明细不装配
    assert data.meta["sections"] == [2, 3, 4]
    assert data.summary["certified"] == 2  # 已勾选的第 2 章总览正常装配


def test_oi10_placeholder_no_fabrication() -> None:
    """KPI 数据位：OI-10 未冻结 → biz_performance 为显式占位，无成单率数值。"""
    data = ReportAssembler(make_source()).assemble_batch(BATCH_ID)
    biz = data.biz_performance
    assert biz is not None
    assert biz["status"] == "placeholder"
    assert "OI-10" in biz["note"]
    assert not any(k in biz for k in ("rate", "value", "成单率_value")), "口径未冻结禁止编造数值"
    assert "OI-10" in data.meta["oi10_note"]


def test_targets_not_found() -> None:
    """批次/学员不存在 → 40404 族异常（区别于空数据）。"""
    assembler = ReportAssembler(make_source())
    with pytest.raises(BatchNotFound) as batch_info:
        assembler.assemble_batch(99)
    assert batch_info.value.code == 40404
    with pytest.raises(TraineeNotFound) as trainee_info:
        assembler.assemble_individual(424242)
    assert trainee_info.value.code == 40404


def test_keyset_iterate_stops_on_short_page() -> None:
    """键集迭代终止条件：短页即止（不足页大小 → 最后一页）。"""
    calls: list[int] = []

    def fetch(after: int, limit: int) -> list[dict[str, Any]]:
        calls.append(after)
        rows = [{"id": after + i} for i in range(1, min(limit, 3) + 1)]  # 恒 3 行 < 页大小
        return rows

    rows = list(keyset_iterate(fetch, page_size=10, cursor_of=lambda page: page[-1]["id"]))
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert len(calls) == 1  # 短页终止，不多取一轮
