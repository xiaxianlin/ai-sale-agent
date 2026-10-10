# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""检索质量回归门禁 CLI（FR-KB-016 / SR-KB-016 §6.6，迭代 0.3）。

CI 门禁作业入口（知识增量更新、检索参数调整、模型升级均触发，04-测试文档
§2.3）：跑冻结评测集 → 与保存的基线 JSON 比对 → 门禁判定（任一维度较基线
下降 >2pp 即阻断）→ 退出码 0/1 供流水线判定（blocked → exit 1 → 阻断发布）。
门禁通过且带 --update-baseline 时，本期指标刷新为新基线（「修复后复跑 →
新 run 成为基线」口径）。

用法（仓库根；凭证经环境变量注入、不入 Git——NFR-SEC-06）：
  python3 tools/regression/retrieval_regression.py \
      --dataset eval/sets/frozen_L1_v3.json \
      --baseline out/eval/baseline.json \
      --url http://127.0.0.1:8000 --api-key "$KB_EVAL_API_KEY" \
      [--threshold-pp 2.0] [--floor-pct 90] [--concurrency 8] \
      [--report-dir out/eval/reports] [--update-baseline]
退出码：0=门禁通过；1=阻断（回落超阈/低于绝对门槛）或参数与数据错误。

被测链路经 --url 指向统一检索 API（FR-KB-030，黑盒调用完整三段式链路，
SR-KB-016 §2）；单条失败按未命中计入分母（保守口径）。工具仅依赖标准库
与 services/kb/core/evaluation（纯标准库包），直接以文件路径执行亦可运行。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import sysconfig
import time
import uuid
from pathlib import Path
from typing import Any, Sequence, TextIO

# 仓库根自举：直接以文件路径执行时（python3 tools/regression/*.py），
# sys.path[0] 为脚本目录，仓库根不在路径上——补齐后才能导入 services 包
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from services.kb.core.evaluation import (  # noqa: E402  (路径自举后导入)
    ATTRIBUTION_CATEGORIES,
    ATTRIBUTION_LABELS,
    DatasetError,
    EvalDataset,
    EvalRunner,
    EvaluationReport,
    RunMetrics,
    aggregate,
    attribution_distribution,
    classify_misses,
    evaluate_gate,
    write_markdown,
    write_xlsx,
)

# ── 默认参数（SR-KB-016 §7 配置口径；运维可经参数覆盖）──
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_THRESHOLD_PP = 2.0  # 相对门槛（04-测试文档 §2.3：>2pp 阻断发布）
DEFAULT_FLOOR_PCT = 90.0  # 绝对门槛（SRS §16.1 验收口径；传 0 停用）
DEFAULT_CONCURRENCY = 8  # KB_EVAL_CONCURRENCY
DEFAULT_TIMEOUT_S = 10.0  # 单用例检索超时（评测宽于线上 500ms 预算）
DEFAULT_REPORT_DIR = "out/eval/reports"
API_KEY_ENV = "KB_EVAL_API_KEY"  # 凭证环境变量（不入 Git）
TAG = "[评测门禁]"


def _restore_stdlib_platform() -> None:
    """仓库根包名 platform 与标准库 platform 冲突的自愈（同 tools/loadtest）。

    以 ``python3 -m tools.regression.retrieval_regression`` 从仓库根运行时，
    顶层包目录 platform 先于标准库被解析，httpx→rich→attrs 链路上的
    ``platform.python_implementation()`` 会 AttributeError；在导入 httpx 前
    恢复标准库语义。直接以文件路径运行时本地包不在 sys.path，幂等跳过。
    """
    cached = sys.modules.get("platform")
    if cached is not None and hasattr(cached, "python_implementation"):
        return
    std_path = os.path.join(sysconfig.get_paths()["stdlib"], "platform.py")
    spec = importlib.util.spec_from_file_location("platform", std_path)
    if spec is None or spec.loader is None:  # 标准库定位失败属环境损坏，应显式失败
        raise RuntimeError("无法定位标准库 platform 模块（运行环境损坏）")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if cached is None:
        sys.modules["platform"] = module
        return
    for name, value in vars(module).items():
        if not name.startswith("__"):
            setattr(cached, name, value)


def http_search_fn(base_url: str, api_key: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> Any:
    """生产 search_fn：同步 httpx 调用统一检索 API，返回其 data 段（黑盒）。

    返回形态 {"results": [{chunk_id, score}], "rewritten_query": str}——
    runner.parse_search_response 的鸭子契约；httpx 延迟导入（仅生产模式需要，
    且须先恢复标准库 platform 语义）。
    """

    def _search(
        *,
        query: str,
        kb_types: Sequence[str] | None = None,
        filters: Any = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        _restore_stdlib_platform()
        import httpx  # noqa: PLC0415  (须在标准库 platform 语义恢复之后导入)

        payload: dict[str, Any] = {"query": query, "top_k": top_k, "rerank": True}
        if kb_types:
            payload["kb_types"] = list(kb_types)
        if filters:
            payload["filters"] = {key: list(values) for key, values in filters.items()}
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(
                f"{base_url.rstrip('/')}/api/v1/knowledge/search",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
        return body.get("data", body) if isinstance(body, dict) else {}

    return _search


def build_arg_parser() -> argparse.ArgumentParser:
    """参数表（默认值对齐 SR-KB-016 §7 配置项）。"""
    parser = argparse.ArgumentParser(
        prog="retrieval_regression.py",
        description="检索质量回归门禁（FR-KB-016）：跑评测集并与基线比对，回落 >2pp 退出码 1 阻断发布",
    )
    parser.add_argument("--dataset", required=True, help="评测集 JSON 文件路径（EvalDataset.save 产物）")
    parser.add_argument("--baseline", default=None, help="基线指标 JSON（RunMetrics.to_dict 产物）；"
                        "指定但文件不存在按 E_EVAL_BASELINE_MISSING 退出 1")
    parser.add_argument("--threshold-pp", type=float, default=DEFAULT_THRESHOLD_PP,
                        help="相对基线最大回落（百分点，默认 2.0）")
    parser.add_argument("--floor-pct", type=float, default=DEFAULT_FLOOR_PCT,
                        help="总体命中率绝对门槛（百分比，默认 90；传 0 停用）")
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help="被测统一检索 API 入口")
    parser.add_argument("--api-key", default=os.environ.get(API_KEY_ENV, ""),
                        help=f"API Key（缺省取环境变量 {API_KEY_ENV}）")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="批量评测并发")
    parser.add_argument("--top-k", type=int, default=5, help="命中判定窗口（Top5 口径勿改）")
    parser.add_argument("--report-dir", default=DEFAULT_REPORT_DIR, help="报告输出目录（md/xlsx）")
    parser.add_argument("--update-baseline", action="store_true",
                        help="门禁通过后将本期指标刷新为新基线")
    return parser


def _load_baseline(path: str) -> RunMetrics:
    """加载基线 JSON（结构非法抛 ValueError，由 main 统一转为退出码 1）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("基线文件顶层须为 JSON 对象（RunMetrics.to_dict 产物）")
    return RunMetrics.from_dict(data)


def _print_summary(
    metrics: RunMetrics,
    baseline: RunMetrics | None,
    gate: Any,
    distribution: dict[str, int],
    out: TextIO,
) -> None:
    """中文摘要输出（CI 日志即读即判；明细见报告文件）。"""
    print(f"{TAG} 用例数 {metrics.n}｜Top5 命中 {metrics.hit_count}/{metrics.n}"
          f"（{_pct(metrics.top5_acc)}）｜MRR {metrics.mrr:.3f}", file=out)
    print(f"{TAG} 延迟 P50 {metrics.latency.p50_ms:.0f}ms｜P95 {metrics.latency.p95_ms:.0f}ms", file=out)
    if baseline is not None:
        delta_pp = (metrics.top5_acc - baseline.top5_acc) * 100.0
        print(f"{TAG} 基线对比：基线 {_pct(baseline.top5_acc)} → 本期 {_pct(metrics.top5_acc)}"
              f"（{delta_pp:+.2f}pp）", file=out)
    for key, dim in metrics.per_dimension.items():
        print(f"{TAG}   维度 {key}：{dim.hit_count}/{dim.n}（{_pct(dim.top5_acc)}）"
              f"｜MRR {dim.mrr:.3f}", file=out)
    missed = metrics.n - metrics.hit_count
    if missed:
        parts = "、".join(
            f"{ATTRIBUTION_LABELS[c]} {distribution.get(c, 0)} 条" for c in ATTRIBUTION_CATEGORIES
        )
        print(f"{TAG} 未命中 {missed} 条归因：{parts}", file=out)


def _pct(value: float) -> str:
    """命中率百分串（一位小数）。"""
    return f"{value * 100:.1f}%"


def main(
    argv: Sequence[str] | None = None,
    *,
    search_fn: Any = None,
    stdout: TextIO | None = None,
) -> int:
    """门禁入口：评测 → 基线比对 → 报告导出 → 退出码 0/1。

    search_fn/stdout 为测试注入口（确定性桩与输出捕获）；生产路径经 --url
    构造 httpx 客户端。任何数据/参数错误统一中文报错 + 退出码 1（CI 语义：
    门禁工具不可用同样阻断发布）。
    """
    out = stdout if stdout is not None else sys.stdout
    args = build_arg_parser().parse_args(argv)

    try:
        dataset = EvalDataset.load(args.dataset)
    except DatasetError as exc:
        print(f"{TAG} 评测集加载失败：{exc}", file=out)
        return 1

    baseline: RunMetrics | None = None
    if args.baseline:
        baseline_path = Path(args.baseline)
        if not baseline_path.exists():
            print(f"{TAG} 基线文件不存在：{baseline_path}（E_EVAL_BASELINE_MISSING："
                  "指定基线策略但无 gate=pass 运行记录）", file=out)
            return 1
        try:
            baseline = _load_baseline(str(baseline_path))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(f"{TAG} 基线文件解析失败：{exc}", file=out)
            return 1

    runner = EvalRunner(
        search_fn if search_fn is not None else http_search_fn(args.url, args.api_key),
        concurrency=args.concurrency,
        top_k=args.top_k,
    )
    results = runner.run(dataset.cases)
    metrics = aggregate(results, dataset.cases)
    attributions = classify_misses(results, dataset.cases)
    gate = evaluate_gate(
        metrics, baseline, args.threshold_pp,
        floor_pct=args.floor_pct if args.floor_pct > 0 else None,
    )

    run_id = f"ci-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    report = EvaluationReport.build(
        run_id=run_id,
        dataset=dataset,
        results=results,
        metrics=metrics,
        attributions=attributions,
        baseline=baseline,
        baseline_label=str(args.baseline or ""),
        gate=gate,
        trigger="ci",
    )
    report_dir = Path(args.report_dir)
    markdown_path = write_markdown(report, report_dir / f"{run_id}.md")
    xlsx_path = write_xlsx(report, report_dir / f"{run_id}.xlsx")

    _print_summary(metrics, baseline, gate, attribution_distribution(attributions), out)
    print(f"{TAG} 报告已导出：{markdown_path}｜{xlsx_path}", file=out)

    if gate.passed:
        if args.update_baseline and args.baseline:
            Path(args.baseline).write_text(
                json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"{TAG} 门禁通过，基线已刷新为本期指标：{args.baseline}", file=out)
        print(f"{TAG} 结论：通过（退出码 0）", file=out)
        return 0
    for reason in gate.reasons:
        print(f"{TAG} 阻断原因：{reason}", file=out)
    print(f"{TAG} 结论：阻断（退出码 1）——按 04-测试文档 §2.3，准确率下降超阈值阻止发布", file=out)
    return 1


if __name__ == "__main__":
    sys.exit(main())
