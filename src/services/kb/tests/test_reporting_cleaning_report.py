# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""数据清洗报告单元测试（FR-KB-024，SR-KB-024 §9 用例表的内存实现级验证）。

覆盖：
- 五要素聚合正确性（多批次日志构造，含分库/分格式矩阵）；
- 批次 / 时间窗两口径等价（test_scope_equivalence）；
- 重放取最新留痕（test_replay_supersede）；
- 遗留问题三分类归因 + 处置建议命中（test_issue_classify）；
- 问题幂等登记与重放自动闭合（test_issue_idempotent / test_issue_autoclose）；
- 触发器：批次回调与每日定时钩子幂等（test_batch_trigger_idempotent / test_daily_once）；
- xlsx 三 sheet 结构（openpyxl 环境缺失时自动跳过，仓库既有约定）；
- md 导出与 xlsx 五要素数值镜像一致（test_md_mirror）。

实现方案：docs/07-实现方案/KB/SR-KB-024-数据清洗报告.md §6/§9。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.kb.core.reporting import (
    CLASS_CN,
    CleanReportService,
    InMemoryIngestLogStore,
    IngestLogRecord,
    IssueBuilder,
    ReportRenderer,
    ReportScope,
    XLSX_SHEET_NAMES,
)

UTC = timezone.utc
BASE_TS = datetime(2026, 10, 8, 9, 0, 0, tzinfo=UTC)


def _ok_log(
    source_file: str,
    fmt: str,
    stage: str,
    batch_id: str,
    kb_type: str,
    *,
    ts: datetime | None = None,
    **counts: int,
) -> IngestLogRecord:
    """构造一条 success 留痕（计数键入 action_detail，0.1 迭代 ingest_pipeline_log 语义）。"""
    detail: dict[str, object] = {
        "status": "success",
        "batch_id": batch_id,
        "kb_type": kb_type,
        **counts,
    }
    return IngestLogRecord(
        source_file=source_file,
        source_format=fmt,
        stage=stage,
        action_detail=detail,
        created_at=ts or BASE_TS,
    )


def _bad_log(
    source_file: str,
    fmt: str,
    stage: str,
    batch_id: str,
    kb_type: str,
    status: str,
    *,
    ts: datetime | None = None,
    error_code: str = "E_EXAMPLE",
    error_msg: str = "阶段执行失败摘要",
) -> IngestLogRecord:
    """构造一条 failed/pending 留痕。"""
    detail: dict[str, object] = {
        "status": status,
        "batch_id": batch_id,
        "kb_type": kb_type,
        "error_code": error_code,
        "error_msg": error_msg,
    }
    return IngestLogRecord(
        source_file=source_file,
        source_format=fmt,
        stage=stage,
        action_detail=detail,
        created_at=ts or BASE_TS,
    )


def _build_two_batch_store() -> InMemoryIngestLogStore:
    """构造两个批次、三格式两库的多文件留痕（五要素期望值见各断言）。

    批次 B1（product/pdf + training/docx）：
    - 产品手册.pdf     parse(100 块)/dedup(去重 12)/correct(纠错 30)/mask(脱敏 8) 全成功
    - 价格表.xlsx      parse(40)/dedup(6)/correct(10)/mask(2) 全成功
    - 培训讲义.docx    parse(60)/dedup(4)/correct(8)/mask(3) 全成功
    - 损坏扫描件.pdf   parse failed（遗留：解析失败）
    批次 B2（faq/md，product 库）：
    - 常见问题.md      parse(50)/dedup(5)/correct(6)/mask(1) 全成功
    - 录音转写.mp3     parse(80) 成功 / mask pending（遗留：人工待审）
    """
    store = InMemoryIngestLogStore()
    b1 = "B-2026-1008-01"
    b2 = "B-2026-1008-02"
    t1 = BASE_TS
    t2 = BASE_TS + timedelta(hours=2)
    rows = [
        # 批次 B1
        _ok_log("产品手册.pdf", "pdf", "parse", b1, "product", ts=t1, blocks_in=100),
        _ok_log("产品手册.pdf", "pdf", "dedup", b1, "product", ts=t1, dropped_exact=7, dropped_near=5),
        _ok_log("产品手册.pdf", "pdf", "correct", b1, "product", ts=t1, corrections=30),
        _ok_log("产品手册.pdf", "pdf", "mask", b1, "product", ts=t1, masked_blocks=8),
        _ok_log("价格表.xlsx", "xlsx", "parse", b1, "product", ts=t1, blocks_in=40),
        _ok_log("价格表.xlsx", "xlsx", "dedup", b1, "product", ts=t1, dropped_exact=6, dropped_near=0),
        _ok_log("价格表.xlsx", "xlsx", "correct", b1, "product", ts=t1, corrections=10),
        _ok_log("价格表.xlsx", "xlsx", "mask", b1, "product", ts=t1, masked_blocks=2),
        _ok_log("培训讲义.docx", "docx", "parse", b1, "training", ts=t1, blocks_in=60),
        _ok_log("培训讲义.docx", "docx", "dedup", b1, "training", ts=t1, dropped_exact=4, dropped_near=0),
        _ok_log("培训讲义.docx", "docx", "correct", b1, "training", ts=t1, corrections=8),
        _ok_log("培训讲义.docx", "docx", "mask", b1, "training", ts=t1, masked_blocks=3),
        _bad_log("损坏扫描件.pdf", "pdf", "parse", b1, "product", "failed", ts=t1,
                 error_code="E_PDF_CORRUPT", error_msg="文件结构损坏无法解析"),
        # 批次 B2
        _ok_log("常见问题.md", "md", "parse", b2, "faq", ts=t2, blocks_in=50),
        _ok_log("常见问题.md", "md", "dedup", b2, "faq", ts=t2, dropped_exact=5, dropped_near=0),
        _ok_log("常见问题.md", "md", "correct", b2, "faq", ts=t2, corrections=6),
        _ok_log("常见问题.md", "md", "mask", b2, "faq", ts=t2, masked_blocks=1),
        _ok_log("录音转写.mp3", "mp3", "parse", b2, "training", ts=t2, blocks_in=80),
        _bad_log("录音转写.mp3", "mp3", "mask", b2, "training", "pending", ts=t2,
                 error_code="mask.review", error_msg="疑似联系人信息需复核"),
    ]
    store.extend(rows)
    return store


class TestFiveElements:
    """五要素聚合正确性（SR §9 test_five_elements / test_scope_equivalence）。"""

    def test_five_elements_by_batch(self) -> None:
        service = CleanReportService(_build_two_batch_store())
        report = service.on_batch_complete("B-2026-1008-01")
        snap = report.snapshot
        # 处理量 = 100+40+60（损坏件 parse 失败无 blocks_in 计数）+ 处理文件数 = 3
        assert snap.totals["处理量"] == 200
        assert snap.totals["处理文件数"] == 3
        # 去重量 = (7+5) + 6 + 4 = 22
        assert snap.totals["去重量"] == 22
        # 纠错量 = 30 + 10 + 8 = 48
        assert snap.totals["纠错量"] == 48
        # 脱敏量 = 8 + 2 + 3 = 13
        assert snap.totals["脱敏量"] == 13
        # 遗留问题：本批次 failed parse 1 条（解析失败）
        assert report.issue_new == 1 and report.issue_carry == 0
        assert [i.class_cn for i in report.issues] == ["解析失败"]
        # 分库矩阵：product = 手册+价格表+损坏件；training = 讲义
        assert snap.by_kb["product"]["处理量"] == 140
        assert snap.by_kb["product"]["去重量"] == 18
        assert snap.by_kb["training"]["处理量"] == 60
        assert snap.by_kb["training"]["脱敏量"] == 3
        # 分格式矩阵：pdf = 手册 + 损坏件（损坏件 blocks_in=0，仅占行）
        assert snap.by_format["pdf"]["处理量"] == 100
        assert snap.by_format["xlsx"]["处理量"] == 40
        assert snap.by_format["docx"]["纠错量"] == 8

    def test_five_elements_second_batch(self) -> None:
        service = CleanReportService(_build_two_batch_store())
        report = service.on_batch_complete("B-2026-1008-02")
        assert report.snapshot.totals == {
            "处理量": 130, "处理文件数": 2, "去重量": 5, "纠错量": 6, "脱敏量": 1,
        }
        # pending mask → 人工待审
        assert [i.class_cn for i in report.issues] == ["人工待审"]

    def test_scope_equivalence(self) -> None:
        """同数据按 batch_id 与覆盖时间窗聚合，两口径总量一致（SR §9）。"""
        store = _build_two_batch_store()
        service = CleanReportService(store)
        by_batch = service.on_batch_complete("B-2026-1008-01").snapshot
        agg = service.aggregator
        by_window = agg.aggregate(
            ReportScope(
                window_from=BASE_TS - timedelta(minutes=1),
                window_to=BASE_TS + timedelta(minutes=30),
            )
        )
        assert by_batch.totals == by_window.totals
        assert by_batch.by_kb == by_window.by_kb
        assert by_batch.by_format == by_window.by_format

    def test_replay_supersede(self) -> None:
        """同 (文件, 阶段) 先 failed 后 success：聚合取最新行，不进遗留（SR §9）。"""
        store = InMemoryIngestLogStore()
        b = "B-R"
        store.extend([
            _bad_log("重放件.pdf", "pdf", "parse", b, "product", "failed", ts=BASE_TS,
                     error_code="E_ONCE", error_msg="首次失败"),
            _ok_log("重放件.pdf", "pdf", "parse", b, "product", ts=BASE_TS + timedelta(minutes=5), blocks_in=25),
        ])
        service = CleanReportService(store)
        report = service.on_batch_complete(b)
        assert report.snapshot.totals["处理量"] == 25
        assert report.snapshot.totals["处理文件数"] == 1
        assert report.issues == ()  # 旧失败行已被重放成功覆盖


class TestIssueClassification:
    """三分类归因 + 处置建议命中（SR §9 test_issue_classify）。"""

    def test_classify_matrix(self) -> None:
        builder = IssueBuilder()
        cases = [
            ("parse", "failed", "解析失败"),
            ("mask", "pending", "人工待审"),
            ("dedup", "failed", "清洗冲突"),
            ("correct", "failed", "清洗冲突"),
            ("chunk", "failed", "清洗冲突"),
            ("index", "failed", "清洗冲突"),
            ("unknown_stage", "failed", "清洗冲突"),  # 兜底：未知失败按清洗冲突
        ]
        for stage, status, expect_cn in cases:
            cls, tip = builder.classify(stage, status)
            assert CLASS_CN[cls] == expect_cn, f"{stage}+{status} 应归 {expect_cn}"
            assert tip.strip() != ""  # 处置建议文案必须命中规则表

    def test_pending_priority_over_failed(self) -> None:
        """pending 通配全部 stage，优先级高于失败分类（ISSUE_RULES 顺序即优先级）。"""
        builder = IssueBuilder()
        cls, _ = builder.classify("parse", "pending")
        assert CLASS_CN[cls] == "人工待审"

    def test_suggestion_texts(self) -> None:
        """三类建议文案与规则表逐字一致（文案随规则版本化，NFR-MAI-02）。"""
        builder = IssueBuilder()
        _, parse_tip = builder.classify("parse", "failed")
        assert "OCR/ASR" in parse_tip
        _, conflict_tip = builder.classify("dedup", "failed")
        assert "FR-KB-017" in conflict_tip and "重放" in conflict_tip
        _, review_tip = builder.classify("mask", "pending")
        assert "复核" in review_tip

    def test_issue_registry_idempotent(self) -> None:
        """同窗口重复生成报告：问题不重复登记，open 计数不变（SR §9 test_issue_idempotent）。"""
        store = _build_two_batch_store()
        service = CleanReportService(store)
        first = service.on_batch_complete("B-2026-1008-01")
        again = service.on_batch_complete("B-2026-1008-01")
        assert first is again  # 幂等：同批次重复触发返回既有报告
        assert len(service.issue_registry) == 1
        assert again.open_issue_count == 1

    def test_issue_autoclose_after_replay(self) -> None:
        """重放成功后再次生成：问题自动闭合移出遗留栏（SR §9 test_issue_lifecycle）。"""
        store = InMemoryIngestLogStore()
        b = "B-LIFE"
        store.extend([
            _bad_log("生命周期.docx", "docx", "correct", b, "training", "failed",
                     error_code="E_RULE", error_msg="规则集冲突"),
            _ok_log("生命周期.docx", "docx", "parse", b, "training", blocks_in=10),
        ])
        service = CleanReportService(store)
        report1 = service.on_batch_complete(b)
        assert report1.open_issue_count == 1
        # FR-KB-017 幂等重放：correct 阶段补写一条更新的 success 留痕
        store.append(
            _ok_log("生命周期.docx", "docx", "correct", b, "training",
                    ts=BASE_TS + timedelta(minutes=10), corrections=5)
        )
        report2 = service.generate("manual", ReportScope(batch_id=b), actor="tester")
        assert report2.open_issue_count == 0
        assert all(i.status == "resolved" for i in service.issue_registry.values())
        assert service.issue_registry[report1.issues[0].log_id].resolved_note == "重放成功自动闭合"


class TestTriggers:
    """自动生成触发：批次完成回调 + 每日定时钩子（SR §6.4 / §9 test_daily_once）。"""

    def test_batch_trigger_idempotent(self) -> None:
        service = CleanReportService(_build_two_batch_store())
        r1 = service.on_batch_complete("B-2026-1008-02")
        r2 = service.on_batch_complete("B-2026-1008-02")
        assert r1 is r2
        assert service.generate_count == 1  # 实际生成恰一次
        assert len(service.list_reports()) == 1
        assert r1.report_kind == "batch"

    def test_daily_once(self) -> None:
        """同日 daily 钩子触发两次：同窗口仅一份报告（SR §9）。"""
        service = CleanReportService(_build_two_batch_store())
        day = BASE_TS.date()
        d1 = service.on_daily_timer(day)
        d2 = service.on_daily_timer(day)
        assert d1 is d2
        assert d1.report_kind == "daily"
        assert service.generate_count == 1
        # 时间窗覆盖 B1/B2 两个批次 → 总量等于两批次之和
        assert d1.snapshot.totals["处理量"] == 330
        assert d1.snapshot.totals["去重量"] == 27

    def test_manual_scope_conflict(self) -> None:
        """batch_id 与时间窗同时给出 → 互斥校验拒绝（E_RPT_SCOPE_CONFLICT）。"""
        service = CleanReportService(InMemoryIngestLogStore())
        with pytest.raises(ValueError, match="互斥"):
            service.generate_manual(
                batch_id="B-X", window_from=BASE_TS, window_to=BASE_TS + timedelta(hours=1)
            )


class TestRenderers:
    """xlsx 三 sheet 结构与 md 镜像（SR §9 test_xlsx_structure / test_md_mirror）。"""

    def _make_service(self, tmp_path: Path) -> CleanReportService:
        """构造落盘到 pytest 临时目录的服务（避免污染仓库 out/）。"""
        return CleanReportService(
            _build_two_batch_store(), renderer=ReportRenderer(tmp_path / "clean-reports")
        )

    def test_xlsx_structure(self, tmp_path: Path) -> None:
        openpyxl = pytest.importorskip(
            "openpyxl", reason="openpyxl 未安装：xlsx 结构用例自动跳过（核心依赖，仓库既有约定）"
        )
        service = self._make_service(tmp_path)
        report = service.on_batch_complete("B-2026-1008-01")
        path, _md = service.export_files(report)
        wb = openpyxl.load_workbook(path)
        assert wb.sheetnames == list(XLSX_SHEET_NAMES)  # 汇总 / 文件明细 / 遗留问题清单
        # 汇总 sheet：五要素齐备（验收口径一），明细文件名不混入汇总
        ws_summary = wb["汇总"]
        summary_texts = [str(c.value) for row in ws_summary.iter_rows() for c in row if c.value is not None]
        joined = "\n".join(summary_texts)
        for label in ("处理量", "去重量", "纠错量", "脱敏量", "遗留问题"):
            assert label in joined
        assert "产品手册" not in joined
        # 文件明细 sheet：中文表头 + 逐文件行（终态中文化）
        ws_detail = wb["文件明细"]
        detail_header = [c.value for c in next(ws_detail.iter_rows(min_row=1, max_row=1))]
        assert detail_header == ["来源文件", "格式", "知识库", "处理量", "去重量", "纠错量", "脱敏量", "终态"]
        detail_texts = [str(c.value) for row in ws_detail.iter_rows() for c in row if c.value is not None]
        assert "损坏扫描件.pdf" in detail_texts and "失败" in detail_texts
        # 遗留问题清单 sheet：中文表头 + 分类 + 处置建议列
        ws_issue = wb["遗留问题清单"]
        issue_header = [c.value for c in next(ws_issue.iter_rows(min_row=1, max_row=1))]
        assert issue_header == ["编号", "分类", "来源文件", "阶段", "问题描述", "处置建议", "状态", "首次发现批次"]
        issue_texts = [str(c.value) for row in ws_issue.iter_rows() for c in row if c.value is not None]
        assert "解析失败" in issue_texts
        assert any("调整 OCR/ASR 参数重试" in t for t in issue_texts)  # 处置建议随行落表

    def test_md_mirror(self, tmp_path: Path) -> None:
        service = self._make_service(tmp_path)
        report = service.on_batch_complete("B-2026-1008-01")
        md = service.renderer.render_md(report)  # md 导出不依赖 openpyxl，始终可验
        totals = report.snapshot.totals
        for label in ("处理量", "去重量", "纠错量", "脱敏量"):
            assert f"| {label} | {totals[label]} |" in md  # 与 xlsx 同源同口径
        assert "解析失败" in md and "损坏扫描件.pdf" in md
        assert md.startswith("# 数据清洗报告")
