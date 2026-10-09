# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""报告生成与回归门禁测试（FR-KB-016 / SR-KB-016 §9，迭代 0.3）。

覆盖：门禁边界（回落恰 2pp 通过 / 超 2pp 阻断 / 仅分维度回落阻断 /
无基线跳过 / 绝对门槛）、Markdown 报告结构（总体指标/两期对比/分维度/
归因分布/未命中明细）、xlsx 三 sheet（汇总/明细/未命中归因）内容与
数值正确性（测试内置 OOXML 读回器；openpyxl 可用时追加第三方读回校验）。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from services.kb.core.evaluation.dataset import EvalCase, EvalDataset
from services.kb.core.evaluation.gate import GateResult, evaluate_gate
from services.kb.core.evaluation.metrics import (
    ATTR_KNOWLEDGE_MISSING,
    ATTR_REWRITE,
    AttributionContext,
    DimensionMetrics,
    RunMetrics,
    aggregate,
    classify_misses,
)
from services.kb.core.evaluation.report import (
    SHEET_ATTRIBUTION,
    SHEET_DETAILS,
    SHEET_SUMMARY,
    EvaluationReport,
    build_markdown,
    write_markdown,
    write_xlsx,
)
from services.kb.core.evaluation.runner import EvalCaseResult

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _metrics(top5_acc: float, n: int = 100, dims: dict[str, float] | None = None) -> RunMetrics:
    """构造聚合指标（门禁/对比测试的简化形态）。"""
    per_dimension = {
        key: DimensionMetrics(key=key, n=20, hit_count=round(acc * 20), top5_acc=acc, mrr=acc * 0.9)
        for key, acc in (dims or {}).items()
    }
    return RunMetrics(n=n, hit_count=round(top5_acc * n), top5_acc=top5_acc,
                      mrr=top5_acc * 0.9, per_dimension=per_dimension)


# ─────────────────────────────── 回归门禁边界 ───────────────────────────────


def test_门禁_回落恰2pp通过() -> None:
    """基线 92% → 本期 90%（回落恰 2.0pp）→ pass（阻断条件是 >2pp，浮点容差内）。"""
    result = evaluate_gate(_metrics(0.90), _metrics(0.92), threshold_pp=2.0)
    assert result == GateResult(passed=True, reasons=())
    assert result.reasons == ()


def test_门禁_回落超2pp阻断() -> None:
    """基线 92.1% → 本期 90%（回落 2.1pp）→ fail，reasons 定位总体回落值。"""
    result = evaluate_gate(_metrics(0.90), _metrics(0.921), threshold_pp=2.0)
    assert result.passed is False
    assert len(result.reasons) == 1
    assert "总体" in result.reasons[0] and "2.10pp" in result.reasons[0]


def test_门禁_仅分维度回落即阻断() -> None:
    """总体持平，单维度回落 30pp → fail（任一维度下降超阈即 fail）。"""
    current = _metrics(0.90, dims={"kb_type=product": 0.60})
    baseline = _metrics(0.90, dims={"kb_type=product": 0.90, "kb_type=process": 0.95})
    result = evaluate_gate(current, baseline, threshold_pp=2.0)
    assert result.passed is False
    assert any("kb_type=product" in reason for reason in result.reasons)
    # 基线独有维度（process）无历史可比，不产生阻断原因
    assert len(result.reasons) == 1


def test_门禁_无基线跳过相对门槛() -> None:
    """首次运行（baseline=None）且不设绝对门槛 → pass（相对门槛跳过）。"""
    assert evaluate_gate(_metrics(0.10), None).passed is True


def test_门禁_绝对门槛可选() -> None:
    """floor_pct=90：89.9% 阻断、90% 通过；不传 floor_pct 则不受下限约束。"""
    blocked = evaluate_gate(_metrics(0.899), None, floor_pct=90.0)
    assert blocked.passed is False
    assert "绝对门槛" in blocked.reasons[0]
    assert evaluate_gate(_metrics(0.90), None, floor_pct=90.0).passed is True
    assert evaluate_gate(_metrics(0.10), None, floor_pct=None).passed is True


# ─────────────────────────────── 报告数据装配 ───────────────────────────────

_BASELINE = RunMetrics(
    n=4, hit_count=3, top5_acc=0.75, mrr=0.7,
    per_dimension={"kb_type=product": DimensionMetrics("kb_type=product", 2, 2, 1.0, 0.9),
                   "kb_type=process": DimensionMetrics("kb_type=process", 2, 1, 0.5, 0.5)},
)


def _build_report(baseline: RunMetrics | None) -> tuple[EvaluationReport, RunMetrics]:
    """四用例小集（2 命中 + 2 未命中：rewrite / knowledge_missing 各一）+ 基线对比。"""
    cases = [
        EvalCase("C1", "ST-3200 支架的最大承载和认证情况", (101,), "product",
                 {"scene": "询价"}),
        EvalCase("C2", "GP-100 摄影云台的包装参数", (201,), "product", {"scene": "询价"}),
        EvalCase("C3", "报价流程中交期如何确认", (301,), "process", {"process_node": "报价"}),
        EvalCase("C4", "售后索赔的处理入口", (401,), "process", {"process_node": "售后"}),
    ]
    dataset = EvalDataset(name="frozen_L1", version=3, cases=cases)
    results = [
        EvalCaseResult.from_rank("C1", 1, 10, retrieved_ids=(101,)),
        EvalCaseResult.from_rank("C2", None, 12, retrieved_ids=(210, 211), rewritten_query="云台 包装"),
        EvalCaseResult.from_rank("C3", 3, 8, retrieved_ids=(305, 304, 301)),
        EvalCaseResult.from_rank("C4", None, 9),
    ]
    metrics = aggregate(results, cases)
    context = AttributionContext(indexed_chunk_ids=frozenset({101, 201, 301}))  # 401 未入库
    attributions = classify_misses(results, cases, context=context)
    assert attributions == {"C2": ATTR_REWRITE, "C4": ATTR_KNOWLEDGE_MISSING}
    gate = evaluate_gate(metrics, baseline, threshold_pp=2.0)
    report = EvaluationReport.build(
        run_id="run-test-0001", dataset=dataset, results=results, metrics=metrics,
        attributions=attributions, baseline=baseline, baseline_label="基线 v2",
        gate=gate, trigger="ci",
    )
    return report, metrics


# ─────────────────────────────── Markdown 报告 ───────────────────────────────


def test_Markdown报告_结构与两期对比() -> None:
    """含基线：五节齐备，总体/分维度两期对比 pp 值、归因中文标签、样本下钻。"""
    report, _ = _build_report(_BASELINE)
    text = build_markdown(report)
    for section in ("# 检索质量评测报告", "## 门禁结论", "## 总体指标", "## 分维度准确率",
                    "## 未命中归因分布", "## 未命中样本明细"):
        assert section in text
    assert "frozen_L1 v3（4 条" in text
    assert "50.0%（2/4）" in text  # 本期命中率
    assert "-25.0pp" in text      # 总体两期变化（0.5 - 0.75）
    assert "查询改写问题" in text and "知识缺失" in text
    assert "GP-100 摄影云台的包装参数" in text  # 未命中样本下钻行
    assert "阻断" in text  # 回落 25pp → 门禁阻断
    assert "run-test-0001" in text


def test_Markdown报告_无基线对比列占位() -> None:
    """无基线：对比列记「—」，归因分布分母正确。"""
    report, _ = _build_report(None)
    text = build_markdown(report)
    assert text.count("—") >= 3  # 基线/变化列占位
    assert "未命中样本共 2 条" in text


def test_Markdown落盘(tmp_path: Path) -> None:
    """write_markdown 产出 UTF-8 文件（父目录自动创建）。"""
    report, _ = _build_report(None)
    path = write_markdown(report, tmp_path / "reports" / "r1.md")
    assert path.exists()
    assert "检索质量评测报告" in path.read_text(encoding="utf-8")


# ─────────────────────────────── xlsx 报告 ───────────────────────────────


def _read_xlsx(path: Path) -> dict[str, list[dict[str, str]]]:
    """测试内置 OOXML 读回器：sheet 名 → 行列表（单元格按列字母索引，值转字符串）。

    兼容 inlineStr（本实现）与 sharedStrings（openpyxl 产物）两种字符串形态，
    保证产出文件被第三方库读回时口径一致。
    """
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = [
                "".join(t.text or "" for t in item.iter() if t.tag.endswith("}t"))
                for item in ET.fromstring(archive.read("xl/sharedStrings.xml"))
            ]
        rels = {
            rel.get("Id"): rel.get("Target", "")
            for rel in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        }
        sheets: dict[str, list[dict[str, str]]] = {}
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        for sheet in workbook.iter(f"{_NS_MAIN}sheet"):
            target = rels[sheet.get(f"{_NS_REL}id")].lstrip("/")
            part = target if target.startswith("xl/") else f"xl/{target}"
            rows: list[dict[str, str]] = []
            for row in ET.fromstring(archive.read(part)).iter(f"{_NS_MAIN}row"):
                cells: dict[str, str] = {}
                for cell in row.iter(f"{_NS_MAIN}c"):
                    column = "".join(ch for ch in cell.get("r", "") if ch.isalpha())
                    value_node = cell.find(f"{_NS_MAIN}v")
                    if cell.get("t") == "inlineStr":
                        value = "".join(t.text or "" for t in cell.iter(f"{_NS_MAIN}t"))
                    elif cell.get("t") == "s" and value_node is not None:
                        value = shared[int(value_node.text)]
                    elif value_node is not None:
                        value = value_node.text or ""
                    else:
                        value = ""
                    cells[column] = value
                rows.append(cells)
            sheets[sheet.get("name", "")] = rows
        return sheets


def test_xlsx报告_三sheet结构与内容(tmp_path: Path) -> None:
    """xlsx 为合法 zip 包，三 sheet 命名正确，汇总/明细/归因关键行值手算对照。"""
    report, metrics = _build_report(_BASELINE)
    sheets = _read_xlsx(write_xlsx(report, tmp_path / "reports" / "run.xlsx"))

    assert set(sheets) == {SHEET_SUMMARY, SHEET_DETAILS, SHEET_ATTRIBUTION}
    summary = sheets[SHEET_SUMMARY]
    assert summary[0]["A"] == "检索质量评测报告"
    assert summary[5]["A"] == "门禁结论" and summary[5]["B"] == "阻断"
    header_index = next(i for i, row in enumerate(summary) if row.get("A") == "指标")
    acc_row = summary[header_index + 1]
    assert acc_row["A"] == "Top5 命中率"
    assert float(acc_row["B"]) == pytest.approx(metrics.top5_acc)
    assert float(acc_row["C"]) == pytest.approx(_BASELINE.top5_acc)
    assert float(acc_row["D"]) == pytest.approx(-25.0)

    details = sheets[SHEET_DETAILS]
    assert details[0]["A"] == "用例编号"
    assert details[0]["K"] == "改写查询" and details[0]["L"] == "归因"
    miss_row = next(row for row in details if row.get("A") == "C2")
    assert miss_row["E"] == "否" and miss_row["L"] == "查询改写问题"
    hit_row = next(row for row in details if row.get("A") == "C1")
    assert hit_row["E"] == "是" and hit_row["F"] == "1"

    attribution = sheets[SHEET_ATTRIBUTION]
    labels = {row.get("A") for row in attribution[1:]}
    assert labels == {"分块问题", "向量化问题", "查询改写问题", "知识缺失"}
    rewrite_row = next(row for row in attribution if row.get("A") == "查询改写问题")
    assert rewrite_row["C"] == "1"  # C2 一条
    embedding_row = next(row for row in attribution if row.get("A") == "向量化问题")
    assert embedding_row["C"] == "0"  # 0 计保留


def test_xlsx报告_openpyxl读回(tmp_path: Path) -> None:
    """openpyxl 可用时的第三方读回校验（环境缺依赖则自动跳过）。"""
    openpyxl = pytest.importorskip("openpyxl")
    report, _ = _build_report(None)
    path = write_xlsx(report, tmp_path / "r.xlsx")
    workbook = openpyxl.load_workbook(path)
    assert workbook.sheetnames == [SHEET_SUMMARY, SHEET_DETAILS, SHEET_ATTRIBUTION]
