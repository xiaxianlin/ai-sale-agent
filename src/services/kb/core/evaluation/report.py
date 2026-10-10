# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""报告生成与导出（FR-KB-016 / SR-KB-016 §6.5，迭代 0.3）。

产出准确率报告的评审载体（中文）：
- Markdown：首屏门禁结论 + 总体指标两期对比（本期 vs 基线，命中率变化 pp）+
  分维度准确率 + 未命中归因分布 + 未命中样本明细（可下钻）；
- xlsx：三 sheet（汇总 / 明细 / 未命中归因），供工作台存档与业务方查阅。

xlsx 写出器为纯标准库实现（zipfile + XML，单元格用 inlineStr 内联字符串）：
环境未安装 openpyxl（迭代 0.3 依赖盘点确认），且 CLI 门禁须零三方依赖独立
运行——产出文件符合 OOXML 规范，Excel/WPS/openpyxl 均可打开读取；待依赖
就绪后可在本模块内替换为 openpyxl 实现，对外函数签名不变。

指标来源：metrics.aggregate（RunMetrics）；明细来源：runner 逐条结果 +
classify_misses 归因；门禁结论：gate.evaluate_gate（GateResult）。
"""
from __future__ import annotations

import xml.sax.saxutils as saxutils
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .dataset import EvalDataset
from .gate import GateResult
from .metrics import (
    ATTRIBUTION_CATEGORIES,
    ATTRIBUTION_FLOWBACK,
    RunMetrics,
    attribution_distribution,
)
from .runner import EvalCaseResult

#: 归因类别的中文展示名（04-测试文档 §2.4 四类口径）
ATTRIBUTION_LABELS: dict[str, str] = {
    "chunking": "分块问题",
    "embedding": "向量化问题",
    "rewrite": "查询改写问题",
    "knowledge_missing": "知识缺失",
}

#: 报告触发展示名（SR-KB-016 §4 eval_run.trigger）
TRIGGER_LABELS: dict[str, str] = {
    "manual": "人工触发",
    "ci": "CI 门禁",
    "pipeline_gate": "流水线门禁",
}

#: xlsx 三 sheet 名（固定顺序：汇总 / 明细 / 未命中归因）
SHEET_SUMMARY = "汇总"
SHEET_DETAILS = "明细"
SHEET_ATTRIBUTION = "未命中归因"


def now_iso() -> str:
    """UTC ISO-8601 时间戳（秒精度）：报告生成时间的统一口径。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- 报告模型


@dataclass(frozen=True)
class CaseDetail:
    """单用例明细行（报告下钻的最小单元，对应 SR-KB-016 §4 eval_run_item 列集）。"""

    case_id: str
    query: str
    kb_type: str
    dimension_label: str
    expected_chunk_ids: tuple[int, ...]
    hit_at_5: bool
    hit_rank: int | None
    mrr: float
    latency_ms: int
    retrieved_ids: tuple[int, ...]
    rewritten_query: str
    attribution: str
    error: str | None


@dataclass(frozen=True)
class EvaluationReport:
    """一次评测运行的完整报告数据（渲染输入；md/xlsx 共用同一份）。"""

    run_id: str
    dataset_name: str
    dataset_version: int
    dataset_status: str
    trigger: str
    created_at: str
    metrics: RunMetrics
    details: tuple[CaseDetail, ...]
    baseline: RunMetrics | None = None
    baseline_label: str = ""
    gate: GateResult | None = None

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        dataset: EvalDataset,
        results: Sequence[EvalCaseResult],
        metrics: RunMetrics,
        attributions: Mapping[str, str] | None = None,
        baseline: RunMetrics | None = None,
        baseline_label: str = "",
        gate: GateResult | None = None,
        trigger: str = "manual",
        created_at: str | None = None,
    ) -> "EvaluationReport":
        """装配报告数据：用例 + 逐条结果 + 聚合指标 + 归因 + 基线 + 门禁。"""
        attributions = attributions or {}
        case_by_id = {case.case_id: case for case in dataset.cases}
        details = tuple(
            CaseDetail(
                case_id=item.case_id,
                query=case_by_id[item.case_id].query,
                kb_type=case_by_id[item.case_id].kb_type,
                dimension_label=case_by_id[item.case_id].dimension_label(),
                expected_chunk_ids=tuple(case_by_id[item.case_id].expected_chunk_ids),
                hit_at_5=item.hit_at_5,
                hit_rank=item.hit_rank,
                mrr=item.mrr,
                latency_ms=item.latency_ms,
                retrieved_ids=tuple(item.retrieved_ids),
                rewritten_query=item.rewritten_query,
                attribution=attributions.get(item.case_id, ""),
                error=item.error,
            )
            for item in results
            if item.case_id in case_by_id
        )
        return cls(
            run_id=run_id,
            dataset_name=dataset.name,
            dataset_version=dataset.version,
            dataset_status=dataset.status,
            trigger=trigger,
            created_at=created_at or now_iso(),
            metrics=metrics,
            details=details,
            baseline=baseline,
            baseline_label=baseline_label,
            gate=gate,
        )

    @property
    def miss_count(self) -> int:
        """未命中条数（归因分布的分母）。"""
        return sum(1 for detail in self.details if not detail.hit_at_5)


# --------------------------------------------------------------------------- Markdown 渲染


def _pct(value: float) -> str:
    """命中率百分串（一位小数，如 92.3%）。"""
    return f"{value * 100:.1f}%"


def _delta_pp(current: float, baseline: float) -> str:
    """两期变化（百分点，带符号，如 +2.0pp / -2.1pp）。"""
    return f"{(current - baseline) * 100:+.1f}pp"


def _md_escape(text: str) -> str:
    """Markdown 表格单元格转义（竖线与换行）。"""
    return text.replace("|", "\\|").replace("\n", " ")


def _acc_with_count(hit_count: int, n: int) -> str:
    """命中率 + 计数明细（如 92.0%（46/50））。"""
    return f"{_pct(hit_count / n)}（{hit_count}/{n}）" if n else "—"


def build_markdown(report: EvaluationReport) -> str:
    """渲染中文 Markdown 报告（结构见模块 docstring；基线缺失时对比列记「—」）。"""
    m = report.metrics
    base = report.baseline
    lines: list[str] = [
        "# 检索质量评测报告",
        "",
        f"- 运行编号：`{report.run_id}`",
        f"- 评测集：{report.dataset_name} v{report.dataset_version}"
        f"（{m.n} 条，状态 {report.dataset_status}）",
        f"- 触发方式：{TRIGGER_LABELS.get(report.trigger, report.trigger)}"
        f"｜生成时间：{report.created_at}",
        "",
        "## 门禁结论",
        "",
    ]
    if report.gate is None:
        lines.append("未启用（本次运行未执行回归门禁判定）。")
    elif report.gate.passed:
        lines.append("**通过**")
    else:
        lines.append("**阻断（BLOCKED）**——存在以下回落，禁止发布：")
        lines.extend(f"- {reason}" for reason in report.gate.reasons)
    lines.extend(["", "## 总体指标", "", "| 指标 | 本期 | 基线 | 变化 |", "|---|---|---|---|"])

    def _base_or_dash(value: str) -> str:
        return value if base is not None else "—"

    lines.append(
        f"| Top5 命中率 | {_acc_with_count(m.hit_count, m.n)} "
        f"| {_base_or_dash(_acc_with_count(base.hit_count, base.n)) if base else '—'} "
        f"| {_delta_pp(m.top5_acc, base.top5_acc) if base else '—'} |"
    )
    lines.append(
        f"| MRR | {m.mrr:.3f} | {f'{base.mrr:.3f}' if base else '—'} "
        f"| {f'{m.mrr - base.mrr:+.3f}' if base else '—'} |"
    )
    lines.append(
        f"| 用例数 | {m.n} | {base.n if base else '—'} | — |"
    )
    lines.append(
        f"| P50 延迟 | {m.latency.p50_ms:.0f}ms | "
        f"{f'{base.latency.p50_ms:.0f}ms' if base else '—'} | — |"
    )
    lines.append(
        f"| P95 延迟 | {m.latency.p95_ms:.0f}ms | "
        f"{f'{base.latency.p95_ms:.0f}ms' if base else '—'} | — |"
    )

    lines.extend(["", "## 分维度准确率", "", "| 维度 | 用例数 | Top5 命中率 | MRR | 较基线 |", "|---|---|---|---|---|"])
    for key, dim in m.per_dimension.items():
        base_dim = base.per_dimension.get(key) if base else None
        lines.append(
            f"| {_md_escape(key)} | {dim.n} | {_acc_with_count(dim.hit_count, dim.n)} "
            f"| {dim.mrr:.3f} | "
            f"{_delta_pp(dim.top5_acc, base_dim.top5_acc) if base_dim else '—'} |"
        )

    distribution = attribution_distribution(
        {d.case_id: d.attribution for d in report.details if not d.hit_at_5 and d.attribution}
    )
    lines.extend([
        "", "## 未命中归因分布", "",
        f"未命中样本共 {report.miss_count} 条，归因分布如下：", "",
        "| 归因类别 | 条数 | 占未命中比例 | 回流环节建议 |", "|---|---|---|---|",
    ])
    for category in ATTRIBUTION_CATEGORIES:
        count = distribution.get(category, 0)
        share = f"{_pct(count / report.miss_count)}" if report.miss_count else "—"
        lines.append(
            f"| {ATTRIBUTION_LABELS[category]}（{category}） | {count} | {share} "
            f"| {ATTRIBUTION_FLOWBACK[category]} |"
        )

    lines.extend([
        "", "## 未命中样本明细", "",
        "| 用例 | 查询 | 期望块 | 实际返回 | 归因 |", "|---|---|---|---|---|",
    ])
    missed = [d for d in report.details if not d.hit_at_5]
    if not missed:
        lines.append("|（无未命中样本） | — | — | — | — |")
    for detail in missed:
        lines.append(
            f"| {_md_escape(detail.case_id)} | {_md_escape(detail.query)} "
            f"| {','.join(str(cid) for cid in detail.expected_chunk_ids)} "
            f"| {','.join(str(cid) for cid in detail.retrieved_ids) or '（空）'} "
            f"| {ATTRIBUTION_LABELS.get(detail.attribution, detail.attribution or '未归因')} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_markdown(report: EvaluationReport, path: str | Path) -> Path:
    """写 Markdown 报告文件（UTF-8）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_markdown(report), encoding="utf-8")
    return target


# --------------------------------------------------------------------------- xlsx 写出器（纯标准库）


def _column_letter(index: int) -> str:
    """列号（1-based）→ Excel 列字母（A..ZZ）。"""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _xml_attr(value: str) -> str:
    """XML 属性值转义。"""
    return saxutils.quoteattr(value)


def _cell_xml(ref: str, value: Any) -> str:
    """单元格 XML：数字直写 <v>、字符串内联 inlineStr、None/空串跳过。"""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value!r}</v></c>'
    text = saxutils.escape(str(value))
    preserve = ' xml:space="preserve"' if text != text.strip() else ""
    return f'<c r="{ref}" t="inlineStr"><is><t{preserve}>{text}</t></is></c>'


def _worksheet_xml(rows: Sequence[Sequence[Any]]) -> str:
    """worksheet part XML（inlineStr 单元格；空行/空单元格跳过）。"""
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    for row_index, row in enumerate(rows, start=1):
        cells = [
            cell
            for cell in (
                _cell_xml(f"{_column_letter(column_index)}{row_index}", value)
                for column_index, value in enumerate(row, start=1)
            )
            if cell
        ]
        if cells:
            parts.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    parts.extend(["</sheetData>", "</worksheet>"])
    return "".join(parts)


#: 单元格可承载的值类型（数字/布尔/字符串/空）
SheetValue = Any


def write_xlsx_file(path: str | Path, sheets: Sequence[tuple[str, list[list[SheetValue]]]]) -> Path:
    """写最小合规 xlsx（zip + OOXML parts；inlineStr 免 sharedStrings 依赖）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    workbook_sheets = "".join(
        f"<sheet name={_xml_attr(name)} sheetId=\"{index}\" r:id=\"rId{index}\"/>"
        for index, (name, _rows) in enumerate(sheets, start=1)
    )
    workbook_rels = "".join(
        f'<Relationship Id="rId{index}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    content_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f"{content_overrides}"
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{main_ns}" xmlns:r="{rel_ns}">'
            f"<sheets>{workbook_sheets}</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{workbook_rels}"
            '<Relationship Id="rIdStyles" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/styles.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{main_ns}">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            "</styleSheet>",
        )
        for index, (_name, rows) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _worksheet_xml(rows))
    return target


# --------------------------------------------------------------------------- 报告 → xlsx 三 sheet


def _summary_rows(report: EvaluationReport) -> list[list[SheetValue]]:
    """汇总 sheet 行集：运行信息 + 门禁 + 总体指标两期对比 + 分维度表。"""
    m = report.metrics
    base = report.baseline
    gate_text = (
        "未启用" if report.gate is None else ("通过" if report.gate.passed else "阻断")
    )
    rows: list[list[SheetValue]] = [
        ["检索质量评测报告"],
        ["运行编号", report.run_id],
        ["评测集", f"{report.dataset_name} v{report.dataset_version}（{m.n} 条）"],
        ["触发方式", TRIGGER_LABELS.get(report.trigger, report.trigger)],
        ["生成时间", report.created_at],
        ["门禁结论", gate_text],
    ]
    if report.gate is not None:
        rows.extend([["阻断原因", reason] for reason in report.gate.reasons])
    rows.extend([
        [],
        ["指标", "本期", "基线", "变化（pp）"],
        [
            "Top5 命中率", m.top5_acc,
            base.top5_acc if base else None,
            round((m.top5_acc - base.top5_acc) * 100.0, 2) if base else None,
        ],
        ["Top5 命中数", m.hit_count, base.hit_count if base else None, None],
        ["MRR", round(m.mrr, 4), round(base.mrr, 4) if base else None, None],
        ["用例数", m.n, base.n if base else None, None],
        ["P50 延迟(ms)", round(m.latency.p50_ms, 1), round(base.latency.p50_ms, 1) if base else None, None],
        ["P95 延迟(ms)", round(m.latency.p95_ms, 1), round(base.latency.p95_ms, 1) if base else None, None],
        [],
        ["维度", "用例数", "Top5 命中率", "MRR", "较基线（pp）"],
    ])
    for key, dim in m.per_dimension.items():
        base_dim = base.per_dimension.get(key) if base else None
        rows.append([
            key, dim.n, round(dim.top5_acc, 4), round(dim.mrr, 4),
            round((dim.top5_acc - base_dim.top5_acc) * 100.0, 2) if base_dim else None,
        ])
    return rows


def _detail_rows(report: EvaluationReport) -> list[list[SheetValue]]:
    """明细 sheet 行集：逐用例结果（命中 + 未命中全量，含归因与改写观测）。"""
    rows: list[list[SheetValue]] = [[
        "用例编号", "查询", "知识库", "维度标签", "Top5命中", "首次命中名次",
        "MRR", "延迟(ms)", "期望块ID", "实际返回ID", "改写查询", "归因", "错误",
    ]]
    for detail in report.details:
        rows.append([
            detail.case_id, detail.query, detail.kb_type, detail.dimension_label,
            "是" if detail.hit_at_5 else "否",
            detail.hit_rank if detail.hit_rank is not None else "未命中",
            round(detail.mrr, 4), detail.latency_ms,
            ",".join(str(cid) for cid in detail.expected_chunk_ids),
            ",".join(str(cid) for cid in detail.retrieved_ids),
            detail.rewritten_query,
            ATTRIBUTION_LABELS.get(detail.attribution, detail.attribution or ""),
            detail.error or "",
        ])
    return rows


def _attribution_rows(report: EvaluationReport) -> list[list[SheetValue]]:
    """未命中归因 sheet 行集：四类计数/占比/回流建议（恒全键，0 计保留）。"""
    distribution = attribution_distribution(
        {d.case_id: d.attribution for d in report.details if not d.hit_at_5 and d.attribution}
    )
    rows: list[list[SheetValue]] = [["归因类别", "类别码", "条数", "占未命中比例", "回流环节建议"]]
    for category in ATTRIBUTION_CATEGORIES:
        count = distribution.get(category, 0)
        share = round(count / report.miss_count, 4) if report.miss_count else None
        rows.append([
            ATTRIBUTION_LABELS[category], category, count, share,
            ATTRIBUTION_FLOWBACK[category],
        ])
    return rows


def write_xlsx(report: EvaluationReport, path: str | Path) -> Path:
    """写 xlsx 报告（三 sheet：汇总 / 明细 / 未命中归因）。"""
    return write_xlsx_file(
        path,
        [
            (SHEET_SUMMARY, _summary_rows(report)),
            (SHEET_DETAILS, _detail_rows(report)),
            (SHEET_ATTRIBUTION, _attribution_rows(report)),
        ],
    )
