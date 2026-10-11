# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""培训效果报告渲染与导出任务用例（FR-TR-009，SR-TR-009 §6.3/§6.4/§9；TDD 阶段①，迭代 1.3）。

五类场景落位：
- 等价类：Markdown 渲染（批次版章节/KPI 数据位）、注入渲染器协议（renderer 不耦合
  具体库——xlsx 以纯标准库 OOXML 桩演示，实现思路参考 kb evaluation report）；
- 异常：渲染失败 → 任务 failed + error_msg 留痕；未注册格式 → 40001；
- 并发/幂等：重复提交（存在未终态同参任务）→ 40902；双 worker 抢占领取仅一方渲染；
- 状态机：queued → running → done + 下载引用。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from services.training.core.board.report import (
    ExportRunningError,
    ExportTaskManager,
    ExportTaskStore,
    MarkdownRenderer,
    ReportAssembler,
    ReportConfig,
    ReportData,
    UnknownRendererError,
)

from .test_board_fakes import FakeReportSource

BATCH_ID = 1
TRAINEE_ID = 101


# --------------------------------------------------------------------------- 测试用渲染器


class MinimalXlsxRenderer:
    """纯标准库 OOXML 渲染桩（zipfile + inlineStr；实现思路同 kb evaluation report）。

    作为注入渲染器演示协议契约：core 不耦合 openpyxl/具体实现，生产可替换为
    openpyxl 样式模板渲染器，签名不变（SR-TR-009 §6.3 渲染层接缝）。
    """

    def render(self, data: ReportData, out: Path) -> Path:
        """写两 sheet（汇总/明细）的最小合规 xlsx。"""
        summary_rows: list[list[Any]] = [["培训效果报告"], [data.summary.get("batch_name", "")]]
        detail_rows: list[list[Any]] = [["学员", "状态", "周期(天)"]]
        for row in data.detail_rows:
            detail_rows.append([row.get("name", ""), row.get("status", ""), row.get("cycle_days")])
        sheets = [("汇总", summary_rows)]
        if data.dimension == "batch":
            sheets.append(("学员明细", detail_rows))
        return write_minimal_xlsx(out, sheets)


def write_minimal_xlsx(path: Path, sheets: list[tuple[str, list[list[Any]]]]) -> Path:
    """最小合规 xlsx 写出器（OOXML parts；inlineStr 免 sharedStrings）——测试桩专用。"""
    import xml.sax.saxutils as saxutils

    def column_letter(index: int) -> str:
        letters = ""
        while index > 0:
            index, remainder = divmod(index - 1, 26)
            letters = chr(ord("A") + remainder) + letters
        return letters

    def cell_xml(ref: str, value: Any) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value!r}</v></c>'
        return f'<c r="{ref}" t="inlineStr"><is><t>{saxutils.escape(str(value))}</t></is></c>'

    def worksheet_xml(rows: list[list[Any]]) -> str:
        body: list[str] = ["<sheetData>"]
        for r_idx, row in enumerate(rows, start=1):
            cells = "".join(
                cell_xml(f"{column_letter(c_idx)}{r_idx}", v) for c_idx, v in enumerate(row, start=1)
            )
            if cells:
                body.append(f'<row r="{r_idx}">{cells}</row>')
        return (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + "".join(body)
            + "</sheetData></worksheet>"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", "<Relationships/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
        for idx, (_name, rows) in enumerate(sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", worksheet_xml(rows))
        zf.writestr("xl/sheetnames.txt", "\n".join(name for name, _ in sheets))
    return path


class SpyRenderer:
    """记录型渲染桩：验证协议入参与调用次数（并发领取断言用）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.barrier: Barrier | None = None

    def render(self, data: ReportData, out: Path) -> Path:
        self.calls.append(data.dimension)
        if self.barrier is not None:  # 并发领取用例：放大竞态窗口
            self.barrier.wait(timeout=5)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("spy", encoding="utf-8")
        return out


# --------------------------------------------------------------------------- 夹具


def make_manager(
    tmp_path: Path,
    renderers: dict[str, Any] | None = None,
    *,
    executor: Any = None,
) -> tuple[ExportTaskManager, ReportAssembler, FakeReportSource]:
    """标准装配：3 人批次 + 个人 101（数据同 test_report_assembler 口径）。"""
    source = FakeReportSource(
        batches={BATCH_ID: {"batch_id": BATCH_ID, "name": "2026Q4 新人第 3 期", "baseline_manual_days": 90}},
        members={BATCH_ID: [
            {"id": 1, "trainee_id": 101, "name": "张三", "status": "certified", "cycle_days": 40,
             "cert_status": "certified", "radar_current": {}, "ex_n": 1, "exam_pass_n": 1},
            {"id": 2, "trainee_id": 102, "name": "李四", "status": "in_training", "cycle_days": None,
             "cert_status": "uncertified", "radar_current": {}, "ex_n": 0, "exam_pass_n": 0},
            {"id": 3, "trainee_id": 103, "name": "王五", "status": "dropped", "cycle_days": None,
             "cert_status": "uncertified", "radar_current": {}, "ex_n": 0, "exam_pass_n": 0},
        ]},
        radar_means={BATCH_ID: {"initial": {}, "current": {}}},
        gaps={BATCH_ID: [{"gap_theme": "价格异议", "count": 3}]},
        individuals={TRAINEE_ID: {
            "profile": {"trainee_id": TRAINEE_ID, "name": "张三", "cert_status": "certified"},
            "timeline": [{"date": "2026-10-01", "kind": "drill", "summary": "演练"}],
            "scores": [
                {"date": "2026-10-01", "kind": "exam", "title": "结业考试", "score": 88.0, "passed": True}
            ],
            "radar_evolution": [{"stage": "latest", "date": "2026-10-01", "radar": {"product": 75.0}}],
            "cycle": {"joined_at": "2026-08-01", "certified_at": "2026-09-15", "cycle_days": 45},
        }},
    )
    assembler = ReportAssembler(source)
    manager = ExportTaskManager(
        store=ExportTaskStore(),
        assembler=assembler,
        renderers=renderers if renderers is not None else {"md": MarkdownRenderer()},
        output_dir=tmp_path / "exports",
        executor=executor,
    )
    return manager, assembler, source


class DeferredExecutor:
    """延迟执行器：任务停留在 queued（状态机/幂等提交/下载未就绪用例）。"""

    def __init__(self) -> None:
        self.jobs: list[Any] = []

    def __call__(self, job: Any) -> None:
        self.jobs.append(job)


# --------------------------------------------------------------------------- 渲染用例


def test_markdown_batch_contains_kpi(tmp_path: Path) -> None:
    """Markdown（批次版）：标题/周期对比/OI-10 占位说明齐备。"""
    _manager, assembler, _source = make_manager(tmp_path)
    data = assembler.assemble_batch(BATCH_ID)
    text = MarkdownRenderer().render_text(data)
    assert "培训效果报告" in text
    assert "批次总览" in text
    assert "培训周期对比" in text
    assert "OI-10" in text  # KPI 数据位占位说明（不编造数值）


def test_markdown_respects_sections(tmp_path: Path) -> None:
    """章节勾选传导到渲染：未勾选章节不出现（SR §9 模板章节勾选用例）。"""
    _manager, assembler, _source = make_manager(tmp_path)
    data = assembler.assemble_batch(BATCH_ID, ReportConfig(sections=(2, 3)))
    text = MarkdownRenderer().render_text(data)
    assert "批次总览" in text
    assert "培训周期对比" in text
    assert "知识缺口" not in text
    assert "学员明细" not in text


def test_xlsx_renderer_structure(tmp_path: Path) -> None:
    """注入 xlsx 渲染器导出结构：zip 可开、含 workbook、明细 sheet 中文表头与行数正确。"""
    manager, _assembler, _source = make_manager(
        tmp_path, renderers={"md": MarkdownRenderer(), "xlsx": MinimalXlsxRenderer()}
    )
    task = manager.submit("batch", BATCH_ID, requested_by="sup-a", fmt="xlsx")
    assert task.status == "done"
    assert task.file_key is not None and task.file_key.endswith(".xlsx")
    with zipfile.ZipFile(task.file_key) as zf:
        names = zf.namelist()
        assert "[Content_Types].xml" in names and "xl/workbook.xml" in names
        assert "学员明细" in zf.read("xl/sheetnames.txt").decode("utf-8")  # 双 sheet（汇总/学员明细）
        sheet2 = ET.fromstring(zf.read("xl/worksheets/sheet2.xml"))
        main_ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        texts = [t.text or "" for t in sheet2.iter(f"{main_ns}t")]
        for expected in ("学员", "张三", "李四", "王五"):  # 中文表头 + 全部成员明细行
            assert expected in texts


def test_renderer_receives_report_data(tmp_path: Path) -> None:
    """协议契约：manager 把装配好的 ReportData 传给注入渲染器（防耦合校验）。"""
    spy = SpyRenderer()
    manager, _assembler, _source = make_manager(tmp_path, renderers={"xlsx": spy})
    manager.submit("individual", TRAINEE_ID, requested_by="sup-a", fmt="xlsx")
    assert spy.calls == ["individual"]


# --------------------------------------------------------------------------- 任务状态机用例


def test_task_lifecycle_queued_to_done(tmp_path: Path) -> None:
    """状态机：submit 后 queued（延迟执行器）→ run_pending → done + 文件落盘。"""
    executor = DeferredExecutor()
    manager, _assembler, _source = make_manager(tmp_path, executor=executor)
    task = manager.submit("batch", BATCH_ID, requested_by="sup-a", fmt="md")
    assert task.status == "queued" and len(executor.jobs) == 1
    executor.jobs[0]()  # 手动推进（模拟 worker 消费）
    done = manager.store.get(task.uid)
    assert done is not None and done.status == "done"
    assert done.file_bytes is not None and done.file_bytes > 0
    assert done.download_ref is not None and task.uid in done.download_ref


def test_submit_inline_done_with_download_ref(tmp_path: Path) -> None:
    """缺省内联执行器：submit 即跑完状态机，done 任务携带下载引用。"""
    manager, _assembler, _source = make_manager(tmp_path)
    task = manager.submit("batch", BATCH_ID, requested_by="sup-a", fmt="md")
    assert task.status == "done"
    assert task.download_ref == f"/api/v1/training/reports/tasks/{task.uid}/download"


def test_duplicate_submit_while_running(tmp_path: Path) -> None:
    """幂等提交：同目标同参数存在未终态任务 → 40902（不新建任务）。"""
    executor = DeferredExecutor()
    manager, _assembler, _source = make_manager(tmp_path, executor=executor)
    first = manager.submit("batch", BATCH_ID, requested_by="sup-a", fmt="md")
    with pytest.raises(ExportRunningError) as info:
        manager.submit("batch", BATCH_ID, requested_by="sup-a", fmt="md")
    assert info.value.code == 40902
    assert len(manager.store.list_tasks()) == 1  # 未新建
    executor.jobs[0]()
    manager.submit("batch", BATCH_ID, requested_by="sup-a", fmt="md")  # 终态后允许重导
    assert len(manager.store.list_tasks()) == 2


def test_failed_task_records_error(tmp_path: Path) -> None:
    """渲染失败：任务 failed + error_msg 留痕（可重导），不留半成品文件引用。"""
    class _Boom:
        def render(self, data: ReportData, out: Path) -> Path:
            raise RuntimeError("boom")

    manager, _assembler, _source = make_manager(tmp_path, renderers={"md": _Boom()})
    task = manager.submit("batch", BATCH_ID, requested_by="sup-a", fmt="md")
    assert task.status == "failed"
    assert task.error_msg is not None and "boom" in task.error_msg
    assert task.file_key is None


def test_concurrent_claim_single_render(tmp_path: Path) -> None:
    """并发领取：双 worker 同时 run_pending 同一任务，仅一方执行渲染（抢占幂等）。"""
    spy = SpyRenderer()  # barrier 不启用：领取互斥由存储层锁保证，渲染必然恰好一次
    executor = DeferredExecutor()
    manager, _assembler, _source = make_manager(tmp_path, renderers={"xlsx": spy}, executor=executor)
    task = manager.submit("batch", BATCH_ID, requested_by="sup-a", fmt="xlsx")  # 停在 queued
    results: list[str | None] = []

    def worker() -> None:  # 两线程同时抢领
        done = manager.run_pending(task.uid)
        results.append(done.status if done else None)

    threads = [Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert spy.calls.count("batch") == 1  # 渲染仅执行一次
    assert results.count("done") == 1  # 仅一方领取成功，另一方返回 None


def test_unknown_format_rejected(tmp_path: Path) -> None:
    """未注册格式（如 pdf 渲染器未接入）→ 40001，提交即拒绝不产生任务。"""
    manager, _assembler, _source = make_manager(tmp_path)
    with pytest.raises(UnknownRendererError) as info:
        manager.submit("batch", BATCH_ID, requested_by="sup-a", fmt="pdf")
    assert info.value.code == 40001
    assert manager.store.list_tasks() == []
