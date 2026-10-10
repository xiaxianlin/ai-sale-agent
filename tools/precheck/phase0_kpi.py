# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""§16.1 KPI 预跑 CLI（迭代 0.3 收口：开发计划 §4.1 退出条件「KPI 预跑全绿」的作业入口）。

四项检查（SRS §16.1 Phase 0 验收 KPI 的内存链路预跑口径）：
  ① 知识库覆盖率 100%：材料清单（样例清单 × 六库分配）经 IngestionOrchestrator
     全链入库后，coverage.compute_coverage 核对（缺口清单直出）；
  ② 检索准确率 ≥90%（Top5）：evaluation.EvalRunner 跑内置样例评测集
     （≥20 条），内存链路 search_fn（FakeEmbeddingClient + InMemoryVectorStore），
     evaluate_gate 绝对门槛 floor_pct=90 判定；
  ③ 检索响应 ≤500ms（P95）：评测侧逐条计时（runner.latency_ms 即内存链路
     在线测量）聚合 P95，NFR-PER-02 预跑口径；
  ④ 数据完整性无关键信息丢失：分块账目对账（quality.reconcile 口径，
     lossless_blocks / block_ledger_ok / text_ledger_ok）抽样核对 + ingest
     留痕七段齐全性核对。

输出：中文 KPI 报告（Markdown，默认 out/phase0_kpi_report.md）+ 退出码
0（全绿）/ 1（任一不达标或运行失败）。

重要口径（生产验收替换项）：
- 本预跑使用**样例语料**（tools/precheck/sample_corpus/，8 个素材覆盖六库）
  与**样例评测集**（≥20 条）；生产验收必须替换为**甲方真实素材**与
  **≥100 条人工评测集**（SRS §5.3，evaluation.dataset.MIN_FROZEN_CASES），
  并以生产部署形态（BGE-M3/TEI + Milvus + 统一检索 API）复测。

用法（仓库根）：
  python3 tools/precheck/phase0_kpi.py [--corpus DIR] [--eval-set FILE]
                                       [--report FILE] [--floor-pct 90] [--p95-budget-ms 500]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import sysconfig
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

# 仓库根自举：直接以文件路径执行时 sys.path[0] 为脚本目录，补齐仓库根才能导入 services 包
_REPO_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(_REPO_ROOT / "src"))


def _restore_stdlib_platform() -> None:
    """仓库根包名 platform 与标准库 platform 冲突的自愈（同 tools/regression、tools/loadtest）。

    仓库根进入 sys.path 后顶层目录 platform 先于标准库被解析，httpx→rich→attrs
    链路上的 ``platform.python_implementation()`` 会 AttributeError；在导入
    services（间接导入 httpx）之前恢复标准库语义。幂等：已是标准库语义直接返回。
    """
    cached = sys.modules.get("platform")
    if cached is not None and hasattr(cached, "python_implementation"):
        return
    std_path = os.path.join(sysconfig.get_paths()["stdlib"], "platform.py")
    spec = importlib.util.spec_from_file_location("platform", std_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法定位标准库 platform 模块（运行环境损坏）")
    module = ModuleType("platform")
    spec.loader.exec_module(module)
    if cached is None:
        sys.modules["platform"] = module
        return
    for name, value in vars(module).items():
        if not name.startswith("__"):
            setattr(cached, name, value)


# services 导入链会触达 httpx（embedding 客户端模块），必须先恢复标准库 platform 语义
_restore_stdlib_platform()

from services.kb.core.evaluation.dataset import EvalDataset  # noqa: E402 (自举后导入)
from services.kb.core.evaluation.gate import GATE_FLOOR_PCT_DEFAULT, evaluate_gate  # noqa: E402
from services.kb.core.evaluation.metrics import (  # noqa: E402
    TOP_K,
    RunMetrics,
    aggregate,
)
from services.kb.core.evaluation.runner import EvalRunner  # noqa: E402
from services.kb.core.ingestion import (  # noqa: E402
    BatchManifest,
    IngestionOrchestrator,
    MaterialExpectation,
    SourceMaterial,
    compute_coverage,
    material_plan_from_sources,
)
from services.kb.core.pipeline.embedding.client import FakeEmbeddingClient  # noqa: E402
from services.kb.core.vectorstore import InMemoryVectorStore  # noqa: E402

# ── 常量：样例清单（文件名 → kb_type；生产替换为甲方素材台账）─────────────────

#: 样例语料目录（脚本同目录 sample_corpus；生成器 build_samples.py 幂等重建）
DEFAULT_CORPUS_DIR = Path(__file__).resolve().parent / "sample_corpus"

#: 样例评测集（≥20 条；chunk_id 为编排器确定性分配，随样例语料版本锁定）
DEFAULT_EVAL_SET = DEFAULT_CORPUS_DIR / "eval_set_phase0.json"

#: 默认报告输出（out/ 为工作区草稿与输出目录）
DEFAULT_REPORT = Path("out") / "phase0_kpi_report.md"

#: 样例清单（顺序即入库顺序 = chunk_id 分配顺序，评测集锚点依赖该顺序冻结）
SAMPLE_SOURCES: tuple[tuple[str, str], ...] = (
    ("product_产品手册_ST3200.docx", "product"),
    ("product_参数表.xlsx", "product"),
    ("product_产品目录.pdf", "product"),
    ("customer_分级标准.docx", "customer"),
    ("skill_议价话术.docx", "skill"),
    ("process_报价SOP.docx", "process"),
    ("negative_丢单复盘.docx", "negative_case"),
    ("training_新人第一课.docx", "training"),
)

#: KPI 目标口径（SRS §16.1）
TARGET_COVERAGE = 1.0          # 覆盖率 100%
TARGET_P95_MS = 500.0          # 检索响应 ≤500ms
EXPECTED_STAGES = ("parse", "dedup", "correct", "mask", "chunk", "embed", "index")

#: 数据完整性抽样上限（样例语料全量核对；大规模语料按确定性步长抽样）
INTEGRITY_SAMPLE_MAX = 10
INTEGRITY_SAMPLE_MIN = 5

TAG = "[KPI 预跑]"

# ── 内存检索链路（KPI-②/③ 的被测对象：embedding → 向量库混合检索）────────────


class MemorySearchFn:
    """内存链路 search_fn（runner 鸭子契约）：查询向量化 → hybrid_search（kb_type 过滤）。

    生产形态替换为统一检索 API（FR-KB-030）的 httpx 包装（tools/regression
    已提供）；本预跑在进程内直连内存向量库，量测口径与 API 侧 latency_ms 同源
    （NFR-PER-02 基线采集的开发自验形态）。
    """

    def __init__(self, embedder: FakeEmbeddingClient, store: InMemoryVectorStore) -> None:
        self._embedder = embedder
        self._store = store

    def __call__(
        self,
        *,
        query: str,
        kb_types: Sequence[str] | None = None,
        filters: Any = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """执行一次混合检索，返回 runner 契约形态 {"results": [{chunk_id, score}]}。"""
        result = self._embedder.embed_batch([query])
        dense, sparse = next(result.rows())
        expr = None
        if kb_types:
            listing = ", ".join(f"'{kb}'" for kb in kb_types)
            expr = f"kb_type in ({listing})"
        hits = self._store.hybrid_search(dense=dense, sparse=sparse, expr=expr, top_k=top_k)
        return {"results": [{"chunk_id": hit.chunk_id, "score": hit.score} for hit in hits]}


# ── 四项检查 ────────────────────────────────────────────────────────────────


def run_ingestion(corpus_dir: Path) -> tuple[BatchManifest, IngestionOrchestrator, InMemoryVectorStore]:
    """样例语料全链入库：解析→清洗→分块→条目化→嵌入→向量库（内存实现）。"""
    sources: list[SourceMaterial] = []
    missing: list[str] = []
    for file_name, kb_type in SAMPLE_SOURCES:
        path = corpus_dir / file_name
        if not path.is_file():
            missing.append(file_name)
            continue
        sources.append(SourceMaterial(str(path), kb_type))
    if missing:
        raise SystemError(
            f"样例语料缺失 {len(missing)} 个文件：{missing}；"
            f"请先执行 python3 {DEFAULT_CORPUS_DIR / 'build_samples.py'} 重建样例"
        )
    store = InMemoryVectorStore()
    orchestrator = IngestionOrchestrator(embedder=FakeEmbeddingClient(), vector_store=store)
    manifest = orchestrator.run(sources)
    return manifest, orchestrator, store


def check_coverage(manifest: BatchManifest) -> dict[str, Any]:
    """KPI-① 覆盖率：材料清单核对（coverage 模块核算，缺口清单直出）。"""
    plan = material_plan_from_sources(manifest.sources)
    report = compute_coverage(plan, manifest)
    return {
        "name": "知识库覆盖率",
        "target": "100%（材料清单核对）",
        "actual": f"{report.coverage_pct:.2f}%",
        "detail": (
            f"清单素材 {report.expected_total} 份 / 成功入库 {report.ingested_total} 份；"
            + ("；".join(
                f"{kb} 库 {counts['ingested']}/{counts['expected']}"
                for kb, counts in report.per_kb.items()
            ))
        ),
        "passed": report.passed,
        "gaps": [gap.to_dict() for gap in report.gaps],
        "payload": report.to_dict(),
    }


def check_accuracy(
    store: InMemoryVectorStore, eval_set_path: Path, floor_pct: float
) -> dict[str, Any]:
    """KPI-② 检索准确率：evaluation runner 跑内置评测集，≥floor_pct 判定（Top5）。"""
    dataset = EvalDataset.load(eval_set_path)
    runner = EvalRunner(search_fn=MemorySearchFn(FakeEmbeddingClient(), store), top_k=TOP_K)
    results = runner.run_dataset(dataset)
    metrics: RunMetrics = aggregate(results, dataset.cases)
    gate = evaluate_gate(metrics, baseline=None, floor_pct=floor_pct)
    return {
        "name": "检索准确率（Top5）",
        "target": f"≥{floor_pct:.0f}%（人工测试 100 条口径的样例预跑）",
        "actual": f"{metrics.top5_acc * 100.0:.2f}%（{metrics.hit_count}/{metrics.n} 条）",
        "detail": (
            f"评测集 {dataset.name} v{dataset.version}（{dataset.case_count} 条，样例集；"
            f"生产验收须 ≥100 条人工评测集）；MRR={metrics.mrr:.3f}"
        ),
        "passed": gate.passed,
        "gaps": list(gate.reasons),
        "payload": metrics.to_dict(),
    }


def check_latency(accuracy_metrics: dict[str, Any]) -> dict[str, Any]:
    """KPI-③ 检索响应：内存链路逐条计时 P95 ≤500ms（runner.latency_ms 在线测量）。"""
    latency: dict[str, float] = accuracy_metrics["latency"]
    return {
        "name": "检索响应速度（P95）",
        "target": "≤500ms（系统监控）",
        "actual": f"P95={latency['p95_ms']:.2f}ms（P50={latency['p50_ms']:.2f}ms，"
                  f"max={latency['max_ms']:.2f}ms，{accuracy_metrics['n']} 次）",
        "detail": "内存链路计时（开发自验口径）；生产以统一检索 API + 系统监控复测",
        "passed": latency["p95_ms"] <= TARGET_P95_MS,
        "gaps": [] if latency["p95_ms"] <= TARGET_P95_MS else [
            f"P95 {latency['p95_ms']:.2f}ms 超过 500ms 预算（NFR-PER-02）"
        ],
        "payload": dict(latency),
    }


def _sample_results(manifest: BatchManifest) -> list[Any]:
    """数据完整性抽样：样例规模全量；大规模按确定性步长取 ≥5 个（含首尾）。"""
    results = manifest.results
    if len(results) <= INTEGRITY_SAMPLE_MAX:
        return list(results)
    step = max(1, len(results) // INTEGRITY_SAMPLE_MIN)
    sampled = results[::step]
    if results[-1] is not sampled[-1]:
        sampled.append(results[-1])
    return sampled


def check_integrity(manifest: BatchManifest, orchestrator: IngestionOrchestrator) -> dict[str, Any]:
    """KPI-④ 数据完整性：分块账目对账（quality.reconcile 口径）抽样 + 留痕七段齐全核对。"""
    sampled = _sample_results(manifest)
    problems: list[str] = []
    for result in sampled:
        label = Path(result.source).name
        if result.status != "success":
            problems.append(f"{label}：编排终态非 success（{result.error}）")
            continue
        quality = result.quality or {}
        for key, label_cn in (
            ("lossless_blocks", "账目对账（block 不重不漏 + 文本拼接一致）"),
            ("block_ledger_ok", "block 账目"),
            ("text_ledger_ok", "文本账目"),
            ("table_integrity", "表格完整性"),
        ):
            if not quality.get(key):
                problems.append(f"{label}：{label_cn} 未通过（{key}=False）")
    # 留痕齐全性：抽样文件七个阶段各至少一条 success 留痕（失败文件不适用）
    log_rows = list(orchestrator.log_store.iter_logs()) if hasattr(orchestrator.log_store, "iter_logs") else []
    by_file: dict[str, set[str]] = {}
    for row in log_rows:
        if str(row.action_detail.get("status", "success")) == "success":
            by_file.setdefault(row.source_file, set()).add(row.stage)
    for result in sampled:
        if result.status != "success":
            continue
        stages = by_file.get(result.source, set())
        absent = [stage for stage in EXPECTED_STAGES if stage not in stages]
        if absent:
            problems.append(f"{Path(result.source).name}：ingest 留痕缺阶段 {absent}")
    return {
        "name": "数据完整性",
        "target": "无关键信息丢失（抽样核对）",
        "actual": f"抽样 {len(sampled)}/{len(manifest.results)} 个素材，账目对账全通过"
                  if not problems else f"抽样 {len(sampled)} 个素材，{len(problems)} 项问题",
        "detail": "分块账目对账口径：quality.reconcile（lossless/blocks/text/table）+ 七段留痕齐全性",
        "passed": not problems,
        "gaps": problems,
        "payload": {"sampled": len(sampled), "problems": problems},
    }


# ── 报告渲染 ────────────────────────────────────────────────────────────────


def render_report(checks: Sequence[dict[str, Any]], manifest: BatchManifest) -> str:
    """渲染中文 Markdown 报告（四项 KPI 表 + 缺口明细 + 口径说明）。"""
    all_pass = all(check["passed"] for check in checks)
    lines: list[str] = []
    lines.append("# Phase 0 §16.1 KPI 预跑报告")
    lines.append("")
    lines.append(f"- 批次号：`{manifest.batch_id}`（{manifest.started_at} ~ {manifest.finished_at}）")
    lines.append(
        f"- 批次统计：素材 {manifest.total_files} 份 / 成功 {manifest.success_files} 份 / 失败 "
        f"{manifest.failed_files} 份；分块 {manifest.total_chunks} 块 / 向量入库 {manifest.indexed_chunks} 块"
    )
    lines.append(f"- 总体结论：**{'全绿（四项 KPI 均达标）' if all_pass else '未达标（见下表）'}**")
    lines.append("")
    lines.append("> 口径说明：本报告为**开发自验预跑**（样例语料 + 内存实现）。")
    lines.append("> **生产验收须替换为甲方真实素材与不少于 100 条人工评测集**（SRS §5.3），")
    lines.append("> 并以生产部署形态（BGE-M3/TEI + Milvus + 统一检索 API + 系统监控）复测。")
    lines.append("")
    lines.append("## 四项 KPI 结论")
    lines.append("")
    lines.append("| # | KPI | 目标值 | 实测 | 结论 |")
    lines.append("|---|-----|--------|------|------|")
    for index, check in enumerate(checks, start=1):
        verdict = "通过" if check["passed"] else "未通过"
        lines.append(f"| {index} | {check['name']} | {check['target']} | {check['actual']} | {verdict} |")
    lines.append("")
    for check in checks:
        lines.append(f"### {check['name']}")
        lines.append("")
        lines.append(f"- 实测：{check['actual']}")
        lines.append(f"- 口径：{check['detail']}")
        if check["gaps"]:
            lines.append("- 缺口/问题：")
            for gap in check["gaps"]:
                text = json.dumps(gap, ensure_ascii=False) if isinstance(gap, dict) else str(gap)
                lines.append(f"  - {text}")
        else:
            lines.append("- 缺口/问题：无")
        lines.append("")
    lines.append("## 分文件入库明细")
    lines.append("")
    lines.append("| 素材 | 库 | 终态 | 解析块 | 保留块 | 分块 | 入库块 | chunk_id |")
    lines.append("|------|----|------|--------|--------|------|--------|----------|")
    for result in manifest.results:
        lines.append(
            f"| {Path(result.source).name} | {result.kb_type} | {result.status} "
            f"| {result.blocks} | {result.kept_blocks} | {result.chunks} | {result.indexed_chunks} "
            f"| {','.join(str(cid) for cid in result.chunk_ids) or '-'} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


# ── CLI 入口 ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """参数解析（仓库根运行；全部参数可缺省）。"""
    parser = argparse.ArgumentParser(
        prog="phase0_kpi.py",
        description="§16.1 Phase 0 KPI 预跑（覆盖率/准确率/响应/完整性，内存链路开发自验）",
    )
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS_DIR), help="样例语料目录")
    parser.add_argument("--eval-set", default=str(DEFAULT_EVAL_SET), help="评测集 JSON（≥20 条）")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="Markdown 报告输出路径")
    parser.add_argument(
        "--floor-pct", type=float, default=GATE_FLOOR_PCT_DEFAULT, help="准确率绝对门槛（默认 90）"
    )
    parser.add_argument(
        "--p95-budget-ms", type=float, default=TARGET_P95_MS, help="检索响应 P95 预算（默认 500ms）"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行四项检查 → 写报告 → 退出码 0/1（全绿/未达标或失败）。"""
    args = build_parser().parse_args(argv)
    try:
        manifest, orchestrator, store = run_ingestion(Path(args.corpus))
    except Exception as exc:  # noqa: BLE001 —— 入库失败本身就是预跑不通过
        print(f"{TAG} 入库编排失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    checks: list[dict[str, Any]] = [check_coverage(manifest)]
    accuracy = check_accuracy(store, Path(args.eval_set), args.floor_pct)
    checks.append(accuracy)
    checks.append(check_latency(accuracy["payload"]))
    checks.append(check_integrity(manifest, orchestrator))

    all_pass = all(check["passed"] for check in checks)
    for index, check in enumerate(checks, start=1):
        verdict = "通过" if check["passed"] else "未通过"
        print(f"{TAG} {check['name']}：{check['actual']} —— {verdict}")
    print(
        f"{TAG} 结论：{'§16.1 四项 KPI 预跑全绿' if all_pass else '§16.1 KPI 预跑存在未达标项'}"
        "（样例口径；生产验收须替换甲方真实素材与 ≥100 条人工评测集）"
    )

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(checks, manifest), encoding="utf-8")
    print(f"{TAG} 报告已写入：{report_path}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
