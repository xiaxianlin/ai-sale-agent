# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""培训效果报告：装配 / 渲染接缝 / 导出任务状态机 / 页面性能判定（FR-TR-009 + NFR-PER-05 工程部分，迭代 1.3）。

模块职责（装配-渲染-交付三段解耦，SR-TR-009 §2）：
- :class:`ReportAssembler`——批次维度（总览/通过率/周期对比 KPI 位/雷达均值/缺口统计）
  与个人维度（历程/成绩单/雷达演进/证书状态位）装配，输出统一 :class:`ReportData`；
  明细行经 :func:`keyset_iterate` 键集分页流式产出（杜绝深 OFFSET、不整批驻留内存）；
- KPI 数据位（SRS §16.2）：培训周期缩短率由批次基线计算（样本不足输出 null + 标注）；
  首月成单率对比位在 OI-10 口径冻结前渲染为显式占位，不编造数值；
- 渲染器注入协议 :class:`ReportRenderer`——:class:`MarkdownRenderer` 内置；
  xlsx/pdf 留接口（实现思路可参考 kb evaluation report 的纯标准库 OOXML 写出器，
  生产以 openpyxl 样式模板渲染器注入，core 不耦合具体库）；
- :class:`ExportTaskManager` / :class:`ExportTaskStore`——异步导出任务状态机
  queued → running → done/failed：同参数未终态任务拒绝重复提交（40902）、
  抢占式领取（并发仅一方渲染）、失败 error_msg 留痕可重导；
- :func:`page_budget_evaluator`——后台页面加载 P95 判定纯函数（NFR-PER-05：常规页
  ≤3s），输出超标页面清单，供 tools/loadtest/admin_page_load.py 与 Playwright 抽测消费。

依据文档：docs/07-实现方案/TR/SR-TR-009-培训效果报告.md §5/§6/§7/§9；
         docs/07-实现方案/TR/SR-TR-006-主管看板.md §2（FR-TR-009 复用统计层）。
"""
from __future__ import annotations

import math
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


# --------------------------------------------------------------------------- 页面性能判定（NFR-PER-05）

#: 后台常规操作页加载预算（SRS NFR-PER-05：≤3 秒，Playwright 抽测口径）
PAGE_BUDGET_MS: float = 3000.0


def p95(values: Sequence[float]) -> float:
    """P95 分位（线性插值法，与 kb metrics.percentile / 检索压测同口径）。

    空样本无定义，显式拒绝（调用方须先保证非空；「无样本即失败」由
    page_budget_evaluator 承载）。
    """
    if not values:
        raise ValueError("分位计算需要非空样本序列")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = (len(ordered) - 1) * 95.0 / 100.0
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return float(ordered[lo])
    frac = idx - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


@dataclass(frozen=True)
class PageBudgetResult:
    """单页面预算判定结果。"""

    path: str
    sample_count: int
    p95_ms: float
    budget_ms: float

    @property
    def passed(self) -> bool:
        """判定：有样本且 P95 ≤ 预算（无样本即失败，不静默放过）。"""
        return self.sample_count > 0 and self.p95_ms <= self.budget_ms


@dataclass(frozen=True)
class PageBudgetReport:
    """页面池整体判定报告（Playwright/压测脚本消费形态）。"""

    budget_ms: float
    results: tuple[PageBudgetResult, ...]

    @property
    def passed(self) -> bool:
        """整体判定：全部页面达标（无页面视为失败，防空池误报通过）。"""
        return bool(self.results) and all(page.passed for page in self.results)

    @property
    def over_budget_pages(self) -> tuple[PageBudgetResult, ...]:
        """超标页面清单（按 P95 降序：最慢在前，处置优先级口径）。"""
        return tuple(sorted((p for p in self.results if not p.passed), key=lambda p: p.p95_ms, reverse=True))

    def as_dict(self) -> dict[str, Any]:
        """导出 dict（--report JSON 落盘形态，验收留档）。"""
        return {
            "budget_ms": self.budget_ms,
            "passed": self.passed,
            "pages": [
                {"path": p.path, "sample_count": p.sample_count,
                 "p95_ms": round(p.p95_ms, 3), "budget_ms": p.budget_ms, "passed": p.passed}
                for p in self.results
            ],
            "over_budget_pages": [p.path for p in self.over_budget_pages],
        }


def page_budget_evaluator(
    pages: Sequence[str | tuple[str, float]],
    samples: Mapping[str, Sequence[float]],
    *,
    budget_ms: float = PAGE_BUDGET_MS,
) -> PageBudgetReport:
    """页面预算评估纯函数：逐页 P95 → 判定 + 超标清单（SR-TR-009 §6 PER-05 落点）。

    pages 元素为路径或 ``(路径, 页面级预算)``（未指定用全局 budget_ms）；
    samples 为 路径 → 计时样本（ms）映射，缺失或空样本的页面判失败。
    """
    results: list[PageBudgetResult] = []
    for entry in pages:
        path, page_budget = (entry, budget_ms) if isinstance(entry, str) else (entry[0], float(entry[1]))
        values = [float(v) for v in samples.get(path, [])]
        results.append(
            PageBudgetResult(
                path=path,
                sample_count=len(values),
                p95_ms=(p95(values) if values else 0.0),
                budget_ms=page_budget,
            )
        )
    return PageBudgetReport(budget_ms=budget_ms, results=tuple(results))


# --------------------------------------------------------------------------- 报告数据契约


#: 雷达四维口径（与 FR-TR-006 统计层同源，避免第二套口径）
RADAR_DIMS: tuple[str, ...] = ("product", "communication", "negotiation", "platform")

#: 批次版章节大纲（SR-TR-009 §6.5，编号即 sections 勾选口径）
SECTION_TITLES_BATCH: dict[int, str] = {
    1: "报告说明", 2: "批次总览", 3: "培训周期对比", 4: "能力雷达均值对比",
    5: "知识缺口统计", 6: "学员明细", 7: "业务效果对比", 8: "附录",
}

#: 个人版章节大纲（SR-TR-009 §6.5）
SECTION_TITLES_INDIVIDUAL: dict[int, str] = {
    1: "基本信息与证书状态", 2: "学习历程时间线", 3: "成绩单", 4: "雷达演进",
    5: "薄弱项与学习建议", 6: "个人培训周期", 7: "业务表现数据位",
}

#: OI-10 占位说明（口径冻结前显式占位，禁止用过程指标替代——SRS R-05 争议防范）
OI10_PLACEHOLDER_NOTE: str = (
    "业务效果对比（新人上岗首月成单率 vs 传统模式）：口径 OI-10 待定"
    "（需 ≥1 个真实业务月数据），本数据位预留，不编造数值。"
)


@dataclass
class ReportData:
    """装配-渲染统一契约（SR-TR-009 §6.1 ReportData；timeline 为个人版扩展位）。"""

    dimension: str  # 'batch' | 'individual'
    meta: dict[str, Any]
    summary: dict[str, Any]
    radar: dict[str, Any]
    detail_rows: Iterator[dict[str, Any]] | Sequence[dict[str, Any]]
    gaps: list[dict[str, Any]] = field(default_factory=list)
    biz_performance: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReportConfig:
    """报告装配配置（生产映射 SR-TR-009 §7 app_config 配置项）。"""

    template_version: str = "v1.0"
    page_size: int = 200  # TR_REPORT_EXPORT_PAGE_SIZE（键集分页页大小）
    window_days: int = 90
    sections: tuple[int, ...] | None = None  # None=全部章节
    oi10_frozen: bool = False  # OI-10 口径冻结开关（demo 恒占位）
    baseline_manual_days: int = 90  # 传统带教基线缺省（批次建档可覆写）
    generated_at: str = ""  # 生成时间（测试注入固定值；缺省 UTC now）


class ReportError(Exception):
    """报告域基础异常（携带业务码，对齐 04-接口设计 §1.3 码表）。"""

    code = 50001

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class BatchNotFound(ReportError):
    """批次不存在（40404）。"""

    code = 40404


class TraineeNotFound(ReportError):
    """学员不存在（40404）。"""

    code = 40404


class UnknownRendererError(ReportError):
    """未注册的渲染格式（40001，提交即拒绝）。"""

    code = 40001


class ExportRunningError(ReportError):
    """同参数导出任务进行中，拒绝重复提交（40902，幂等键语义）。"""

    code = 40902


def _now_iso() -> str:
    """UTC ISO-8601 时间戳（报告生成时间统一口径）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def keyset_iterate(
    fetch_page: Callable[[int, int], Sequence[dict[str, Any]]],
    *,
    page_size: int,
    cursor_of: Callable[[Sequence[dict[str, Any]]], int],
    after: int = 0,
) -> Iterator[dict[str, Any]]:
    """键集分页迭代器（SR-TR-009 §6.2 iter_batch_members 核心形态）。

    以游标（末行 id）翻页杜绝深 OFFSET；短页即止；逐行 yield，
    明细行永不整批驻留内存（NFR-PER-05 大批次装配红线）。
    """
    cursor = after
    while True:
        page = list(fetch_page(cursor, page_size))
        if not page:
            return
        yield from page
        if len(page) < page_size:
            return
        cursor = cursor_of(page)


class ReportDataSource(Protocol):
    """报告数据源协议（鸭子类型注入；生产为 PG 聚合查询 + 键集分页游标）。"""

    def batch_info(self, batch_id: int) -> dict[str, Any] | None:
        """批次档案（None=不存在）。"""
        ...

    def batch_overview(self, batch_id: int) -> dict[str, Any]:
        """批次总览聚合（total/certified/dropped/avg_cycle_days，单条 GROUP BY 下推）。"""
        ...

    def batch_radar_means(self, batch_id: int) -> dict[str, dict[str, float]]:
        """雷达均值对比（initial=初评 / current=当前，四维）。"""
        ...

    def iter_batch_members(self, batch_id: int, page_size: int) -> Iterator[dict[str, Any]]:
        """成员明细流（键集分页，逐人：周期/雷达/演练/考试/证书）。"""
        ...

    def batch_gaps(self, batch_id: int) -> list[dict[str, Any]]:
        """知识缺口统计（FR-TR-008 只读消费）。"""
        ...

    def trainee_profile(self, trainee_id: int) -> dict[str, Any] | None:
        """学员档案 dict（None=不存在）。"""
        ...

    def trainee_timeline(self, trainee_id: int, window_days: int) -> list[dict[str, Any]]:
        """学习历程时间线（演练/考试/问答合并）。"""
        ...

    def trainee_score_details(self, trainee_id: int, window_days: int) -> list[dict[str, Any]]:
        """成绩单明细（演练 + 考试）。"""
        ...

    def trainee_radar_evolution(self, trainee_id: int) -> list[dict[str, Any]]:
        """雷达演进序列（initial/stage/latest）。"""
        ...

    def trainee_cycle(self, trainee_id: int) -> dict[str, Any] | None:
        """个人培训周期（joined_at→certified_at；未认证 None）。"""
        ...


def _wants(section: int, cfg: ReportConfig) -> bool:
    """章节勾选判定：未配置 sections 视为全选（SR §9 模板章节勾选用例）。"""
    return cfg.sections is None or section in cfg.sections


class ReportAssembler:
    """报告数据装配器：批次 / 个人双维度（只装配，不产生新评分数据）。"""

    def __init__(self, source: ReportDataSource) -> None:
        self._source = source

    # ── 存在性预检（API 层 40404 与导出任务前置校验共用）──
    def ensure_batch(self, batch_id: int) -> dict[str, Any]:
        """批次存在性校验；不存在抛 BatchNotFound(40404)。"""
        info = self._source.batch_info(batch_id)
        if info is None:
            raise BatchNotFound(f"批次不存在：{batch_id}")
        return info

    def ensure_trainee(self, trainee_id: int) -> dict[str, Any]:
        """学员存在性校验；不存在抛 TraineeNotFound(40404)。"""
        profile = self._source.trainee_profile(trainee_id)
        if profile is None:
            raise TraineeNotFound(f"学员不存在：{trainee_id}")
        return profile

    # ── 批次维度 ──
    def assemble_batch(self, batch_id: int, cfg: ReportConfig | None = None) -> ReportData:
        """批次装配：总览/通过率/周期对比 KPI 位/雷达均值/缺口统计/学员明细流。"""
        cfg = cfg or ReportConfig()
        info = self.ensure_batch(batch_id)
        overview = self._source.batch_overview(batch_id)
        total = int(overview.get("total", 0))
        certified = int(overview.get("certified", 0))
        dropped = int(overview.get("dropped", 0))
        avg_cycle = overview.get("avg_cycle_days")
        baseline = int(info.get("baseline_manual_days", cfg.baseline_manual_days))
        cert_rate = round(certified / total, 4) if total else None
        if avg_cycle is not None and avg_cycle > 0 and certified > 0:
            reduction: float | None = round((1.0 - float(avg_cycle) / baseline) * 100.0, 2)
            kpi_note = "培训周期缩短率 = 1 − 结业平均周期 / 传统带教基线（SRS §16.2 KPI 数据位）"
        else:  # 样本不足：禁止展示误导值（SR §6.2-②）
            reduction = None
            kpi_note = "样本不足（无结业学员或周期缺失），不展示培训周期缩短率"
        sections = list(cfg.sections) if cfg.sections is not None else sorted(SECTION_TITLES_BATCH)
        return ReportData(
            dimension="batch",
            meta={
                "template_version": cfg.template_version,
                "generated_at": cfg.generated_at or _now_iso(),
                "dimension": "batch",
                "sections": sections,
                "data_window": {
                    "period_start": info.get("period_start"), "period_end": info.get("period_end")
                },
                "oi10_note": OI10_PLACEHOLDER_NOTE,
            },
            summary={
                "batch_id": batch_id,
                "batch_name": info.get("name", ""),
                "total": total,
                "certified": certified,
                "dropped": dropped,
                "cert_rate": cert_rate,
                "cycle_kpi": {
                    "avg_cycle_days": avg_cycle,
                    "baseline_manual_days": baseline,
                    "reduction_pct": reduction,
                    "note": kpi_note,
                },
            },
            radar=dict(self._source.batch_radar_means(batch_id)) if _wants(4, cfg) else {},
            detail_rows=(
                self._source.iter_batch_members(batch_id, cfg.page_size) if _wants(6, cfg) else iter(())
            ),
            gaps=(
                sorted(self._source.batch_gaps(batch_id), key=lambda g: g.get("count", 0), reverse=True)
                if _wants(5, cfg) else []
            ),
            biz_performance=self._oi10_placeholder(cfg),
        )

    # ── 个人维度 ──
    def assemble_individual(self, trainee_id: int, cfg: ReportConfig | None = None) -> ReportData:
        """个人装配：基本证书状态/历程时间线/成绩单/雷达演进/个人培训周期。"""
        cfg = cfg or ReportConfig()
        profile = self.ensure_trainee(trainee_id)
        timeline = self._source.trainee_timeline(trainee_id, cfg.window_days) if _wants(2, cfg) else []
        scores = self._source.trainee_score_details(trainee_id, cfg.window_days) if _wants(3, cfg) else []
        evolution = self._source.trainee_radar_evolution(trainee_id) if _wants(4, cfg) else []
        cycle = self._source.trainee_cycle(trainee_id) if _wants(6, cfg) else None
        sections = list(cfg.sections) if cfg.sections is not None else sorted(SECTION_TITLES_INDIVIDUAL)
        return ReportData(
            dimension="individual",
            meta={
                "template_version": cfg.template_version,
                "generated_at": cfg.generated_at or _now_iso(),
                "dimension": "individual",
                "sections": sections,
                "data_window": {"window_days": cfg.window_days},
                "oi10_note": OI10_PLACEHOLDER_NOTE,
            },
            summary={
                "trainee_id": trainee_id,
                "name": profile.get("name", ""),
                "cert_status": profile.get("cert_status", ""),
                "onboarded_at": profile.get("onboarded_at"),
                "timeline": timeline,  # 学习历程时间线（个人版第 2 章）
                "cycle": cycle,  # 个人培训周期（KPI 分子数据位；未认证 None）
            },
            radar={"evolution": evolution},
            detail_rows=iter(scores),  # 成绩单明细（流式形态与批次版一致）
            gaps=[],
            biz_performance=self._oi10_placeholder(cfg),
        )

    @staticmethod
    def _oi10_placeholder(cfg: ReportConfig) -> dict[str, Any]:
        """业务效果对比数据位：口径冻结前恒为占位结构（无任何编造数值）。"""
        return {
            "status": "placeholder",
            "kpi": "新人上岗首月成单率",
            "target": "不低于传统模式",
            "frozen": cfg.oi10_frozen,
            "note": OI10_PLACEHOLDER_NOTE,
        }


# --------------------------------------------------------------------------- 渲染器（注入协议）


class ReportRenderer(Protocol):
    """报告渲染器协议（SR-TR-009 §6.3 接缝：core 不耦合 openpyxl/HTML 库）。

    实现约定：把 ReportData 渲染为文件写入 out 并返回路径；扩展名由
    ExportTaskManager 按格式分配。内置 MarkdownRenderer；xlsx/pdf 生产实现
    以注入方式接入（纯标准库 OOXML 思路参考 kb evaluation report.report）。
    """

    def render(self, data: ReportData, out: Path) -> Path:
        """渲染报告到目标文件。"""
        ...


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """Markdown 表格片段（中文表头）。"""
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return lines


class MarkdownRenderer:
    """内置 Markdown 渲染器（demo 预览与下载中心缺省格式）。"""

    def render_text(self, data: ReportData) -> str:
        """渲染 Markdown 文本（章节按 meta.sections 裁剪，未勾选章节不渲染）。"""
        title = "培训效果报告（批次版）" if data.dimension == "batch" else "培训效果报告（个人版）"
        lines: list[str] = [
            f"# {title}",
            "",
            f"- 模板版本：{data.meta.get('template_version', '')}",
            f"- 生成时间：{data.meta.get('generated_at', '')}",
            f"- 口径声明：{data.meta.get('oi10_note', '')}",
            "",
        ]
        if data.dimension == "batch":
            lines.extend(self._batch_sections(data))
        else:
            lines.extend(self._individual_sections(data))
        lines.append("")
        return "\n".join(lines)

    def render(self, data: ReportData, out: Path) -> Path:
        """渲染到文件（ReportRenderer 协议实现）。"""
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.render_text(data), encoding="utf-8")
        return out

    # ── 批次版章节 ──
    def _batch_sections(self, data: ReportData) -> list[str]:
        s = data.summary
        kpi = s.get("cycle_kpi", {})
        lines: list[str] = []
        for section in data.meta.get("sections", []):
            title = SECTION_TITLES_BATCH.get(section, f"章节 {section}")
            lines.extend([f"## {title}", ""])
            if section == 1:
                lines.extend([data.meta.get("oi10_note", ""), ""])
            elif section == 2:
                lines.extend(_md_table(
                    ["指标", "数值"],
                    [["总人数", s.get("total")], ["结业", s.get("certified")],
                     ["流失", s.get("dropped")], ["认证通过率", s.get("cert_rate")]],
                ))
            elif section == 3:
                lines.extend(_md_table(
                    ["结业平均周期(天)", "传统基线(天)", "缩短率(%)", "说明"],
                    [[kpi.get("avg_cycle_days"), kpi.get("baseline_manual_days"),
                      kpi.get("reduction_pct"), kpi.get("note")]],
                ))
            elif section == 4:
                rows = [[dim, (data.radar.get("initial") or {}).get(dim),
                         (data.radar.get("current") or {}).get(dim)] for dim in RADAR_DIMS]
                lines.extend(_md_table(["维度", "初评均值", "当前均值"], rows))
            elif section == 5:
                lines.extend(_md_table(
                    ["缺口主题", "命中数"],
                    [[g.get("gap_theme"), g.get("count")] for g in data.gaps],
                ))
            elif section == 6:
                lines.extend(_md_table(
                    ["学员", "状态", "周期(天)"],
                    [[r.get("name"), r.get("status"), r.get("cycle_days")] for r in data.detail_rows],
                ))
            elif section == 7:
                biz = data.biz_performance or {}
                lines.extend([str(biz.get("note", OI10_PLACEHOLDER_NOTE)), ""])
            elif section == 8:
                lines.extend([f"指标口径与模板版本 {data.meta.get('template_version')}（附录）。", ""])
            lines.append("")
        return lines

    # ── 个人版章节 ──
    def _individual_sections(self, data: ReportData) -> list[str]:
        s = data.summary
        lines: list[str] = []
        for section in data.meta.get("sections", []):
            title = SECTION_TITLES_INDIVIDUAL.get(section, f"章节 {section}")
            lines.extend([f"## {title}", ""])
            if section == 1:
                lines.extend(_md_table(
                    ["学员", "认证状态", "入职日期"],
                    [[s.get("name"), s.get("cert_status"), s.get("onboarded_at")]],
                ))
            elif section == 2:
                lines.extend(_md_table(
                    ["日期", "类型", "事项"],
                    [[e.get("date"), e.get("kind"), e.get("summary")] for e in s.get("timeline", [])],
                ))
            elif section == 3:
                lines.extend(_md_table(
                    ["日期", "类型", "项目", "得分", "通过"],
                    [[r.get("date"), r.get("kind"), r.get("title"), r.get("score"), r.get("passed")]
                     for r in data.detail_rows],
                ))
            elif section == 4:
                evolution = data.radar.get("evolution", [])
                lines.extend(_md_table(
                    ["阶段", "日期", "雷达"],
                    [[e.get("stage"), e.get("date"), e.get("radar")] for e in evolution],
                ))
            elif section == 6:
                cycle = s.get("cycle")
                lines.extend(_md_table(
                    ["入职", "认证", "培训周期(天)"],
                    [[(cycle or {}).get("joined_at"), (cycle or {}).get("certified_at"),
                      (cycle or {}).get("cycle_days")]],
                ))
            elif section == 7:
                biz = data.biz_performance or {}
                lines.extend([str(biz.get("note", OI10_PLACEHOLDER_NOTE)), ""])
            lines.append("")
        return lines


# --------------------------------------------------------------------------- 导出任务状态机


#: 下载引用前缀（与 report_api 路由同口径）
DOWNLOAD_REF_PREFIX: str = "/api/v1/training/reports/tasks"

#: 支持的导出格式 → 扩展名（xlsx/pdf 由注入渲染器承接）
FORMAT_EXTENSIONS: dict[str, str] = {"md": ".md", "xlsx": ".xlsx", "pdf": ".pdf"}


@dataclass
class ExportTask:
    """导出任务（report_export 形态；状态机 queued → running → done/failed）。"""

    uid: str
    dimension: str  # batch | individual
    target_id: int
    fmt: str
    sections: list[int] | None
    requested_by: str
    created_at: str
    status: str = "queued"
    file_key: str | None = None
    file_bytes: int | None = None
    error_msg: str | None = None
    finished_at: str | None = None

    @property
    def download_ref(self) -> str | None:
        """下载引用（仅 done 任务携带，杜绝半成品下载）。"""
        if self.status != "done":
            return None
        return f"{DOWNLOAD_REF_PREFIX}/{self.uid}/download"

    def to_dict(self) -> dict[str, Any]:
        """导出 dict（任务查询端点 data 段形态）。"""
        return {
            "task_uid": self.uid,
            "dimension": self.dimension,
            "target_id": self.target_id,
            "format": self.fmt,
            "sections": list(self.sections) if self.sections is not None else None,
            "status": self.status,
            "requested_by": self.requested_by,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "file_key": self.file_key,
            "file_bytes": self.file_bytes,
            "error_msg": self.error_msg,
            "download_ref": self.download_ref,
        }


class ExportTaskStore:
    """导出任务存储（进程内线程安全；生产为 PG report_export 表 + 部分唯一索引）。

    claim 为抢占式领取（``UPDATE ... WHERE status='queued'`` 语义）：
    并发双 worker 同时领取同一任务，仅一方成功（幂等）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, ExportTask] = {}

    def create(
        self,
        dimension: str,
        target_id: int,
        fmt: str,
        sections: list[int] | None,
        requested_by: str,
        *,
        uid: str | None = None,
        created_at: str | None = None,
    ) -> ExportTask:
        """登记新任务（queued）。"""
        task = ExportTask(
            uid=uid or uuid.uuid4().hex[:16],
            dimension=dimension,
            target_id=target_id,
            fmt=fmt,
            sections=list(sections) if sections is not None else None,
            requested_by=requested_by,
            created_at=created_at or _now_iso(),
        )
        with self._lock:
            self._tasks[task.uid] = task
        return task

    def get(self, uid: str) -> ExportTask | None:
        """按 uid 取任务。"""
        with self._lock:
            return self._tasks.get(uid)

    def list_tasks(self) -> list[ExportTask]:
        """全部任务（按创建序）。"""
        with self._lock:
            return list(self._tasks.values())

    def find_running(self, dimension: str, target_id: int, fmt: str) -> ExportTask | None:
        """同目标同参数的未终态任务（幂等提交校验口径）。"""
        with self._lock:
            for task in self._tasks.values():
                if (task.dimension == dimension and task.target_id == target_id
                        and task.fmt == fmt and task.status in ("queued", "running")):
                    return task
            return None

    def claim(self, uid: str) -> ExportTask | None:
        """抢占式领取：queued → running；非 queued（已被领取/终态）返回 None。"""
        with self._lock:
            task = self._tasks.get(uid)
            if task is None or task.status != "queued":
                return None
            self._tasks[uid] = ExportTask(**{**task.__dict__, "status": "running"})
            return self._tasks[uid]

    def finish(self, uid: str, *, file_key: str, file_bytes: int) -> ExportTask | None:
        """完成任务（done + 产物引用）。"""
        with self._lock:
            task = self._tasks.get(uid)
            if task is None:
                return None
            self._tasks[uid] = ExportTask(**{
                **task.__dict__, "status": "done", "file_key": file_key,
                "file_bytes": file_bytes, "finished_at": _now_iso(),
            })
            return self._tasks[uid]

    def fail(self, uid: str, error_msg: str) -> ExportTask | None:
        """任务失败（error_msg 留痕，可重导）。"""
        with self._lock:
            task = self._tasks.get(uid)
            if task is None:
                return None
            self._tasks[uid] = ExportTask(**{
                **task.__dict__, "status": "failed", "error_msg": error_msg, "finished_at": _now_iso(),
            })
            return self._tasks[uid]


class ExportTaskManager:
    """导出任务管理器：提交（幂等 + 前置校验）→ 执行（装配→渲染→落盘）。

    executor 注入：缺省内联立即执行（demo 同步完成状态机）；生产注入队列
    consumer（延迟执行时任务停留在 queued，由 worker 调 run_pending 消费）。
    """

    def __init__(
        self,
        *,
        store: ExportTaskStore,
        assembler: ReportAssembler,
        renderers: Mapping[str, ReportRenderer],
        output_dir: Path,
        executor: Callable[[Callable[[], None]], Any] | None = None,
    ) -> None:
        self.store = store
        self._assembler = assembler
        self._renderers = dict(renderers)
        self._output_dir = Path(output_dir)
        self._executor = executor if executor is not None else (lambda job: job())

    def submit(
        self,
        dimension: str,
        target_id: int,
        *,
        requested_by: str,
        fmt: str = "md",
        sections: Sequence[int] | None = None,
    ) -> ExportTask:
        """提交导出任务：格式校验（40001）→ 存在性校验（40404）→ 幂等校验（40902）。"""
        if fmt not in self._renderers:
            raise UnknownRendererError(f"未注册的报告渲染格式：{fmt}")
        if dimension == "batch":
            self._assembler.ensure_batch(target_id)
        elif dimension == "individual":
            self._assembler.ensure_trainee(target_id)
        else:
            raise UnknownRendererError(f"未知导出维度：{dimension}")
        if self.store.find_running(dimension, target_id, fmt) is not None:
            raise ExportRunningError("同参数导出任务进行中，请等待完成或稍后重试")
        section_list = [int(s) for s in sections] if sections is not None else None
        task = self.store.create(dimension, target_id, fmt, section_list, requested_by)
        self._executor(lambda: self.run_pending(task.uid))
        # 缺省内联执行器已在上方跑完状态机：返回存储中的最新状态（queued/running/done）
        return self.store.get(task.uid) or task

    def run_pending(self, task_uid: str) -> ExportTask | None:
        """执行一个 queued 任务（worker 消费口径）：领取失败（被抢/不存在）返回 None。"""
        task = self.store.claim(task_uid)
        if task is None:
            return None
        try:
            cfg = ReportConfig(sections=tuple(task.sections) if task.sections is not None else None)
            if task.dimension == "batch":
                data = self._assembler.assemble_batch(task.target_id, cfg)
            else:
                data = self._assembler.assemble_individual(task.target_id, cfg)
            renderer = self._renderers[task.fmt]
            out = self._output_dir / f"{task.uid}{FORMAT_EXTENSIONS.get(task.fmt, '.bin')}"
            renderer.render(data, out)
            return self.store.finish(task.uid, file_key=str(out), file_bytes=out.stat().st_size)
        except Exception as exc:  # 渲染/装配失败：error_msg 留痕（可重导）
            return self.store.fail(task.uid, f"{type(exc).__name__}: {exc}")
