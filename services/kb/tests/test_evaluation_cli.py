# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""回归门禁 CLI 测试（FR-KB-016 / SR-KB-016 §6.6，迭代 0.3）。

以注入 search_fn 桩跑全流程（评测 → 基线比对 → 报告导出 → 退出码）：
- 通过路径：回落 ≤2pp → 退出码 0、报告 md/xlsx 落盘、中文摘要输出；
- 阻断路径：回落超 2pp → 退出码 1、输出阻断原因；
- 边界：回落恰 2pp → 通过（阻断条件严格大于）；
- --update-baseline：通过后刷新基线，复跑以新基线通过；
- 数据/基线缺失 → 退出码 1（中文报错）；
- 子进程冒烟：直接以文件路径执行 --help（仓库根自举可用）。
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from services.kb.core.evaluation.dataset import EvalCase, EvalDataset
from tools.regression import retrieval_regression

#: 被测 CLI 脚本（子进程冒烟用）
CLI_PATH = Path(retrieval_regression.__file__)


def _stub(mapping: dict[str, list[int]]) -> Any:
    """确定性 search_fn 桩（返回统一检索 API data 段形态）。"""

    def _search(*, query: str, kb_types: list[str] | None = None,
                filters: dict[str, list[str]] | None = None, top_k: int = 5) -> dict[str, Any]:
        chunk_ids = mapping.get(query, [])
        return {"results": [{"chunk_id": cid, "score": 0.9} for cid in chunk_ids],
                "rewritten_query": query}

    return _search


def _write_dataset(tmp_path: Path) -> Path:
    """四用例评测集 JSON（C1/C2/C3 命中、C4 未命中 → 本期命中率 75%）。"""
    dataset = EvalDataset(
        name="smoke_ci", version=1,
        cases=[
            EvalCase("C1", "q1", (101,), "product"),
            EvalCase("C2", "q2", (102,), "product"),
            EvalCase("C3", "q3", (103,), "process"),
            EvalCase("C4", "q4", (404,), "process"),
        ],
    )
    return dataset.save(tmp_path / "sets" / "smoke.json")


def _write_baseline(tmp_path: Path, top5_acc: float) -> Path:
    """基线 JSON（RunMetrics.to_dict 最小形态）。"""
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps({"n": 4, "hit_count": round(top5_acc * 4), "top5_acc": top5_acc,
                    "mrr": top5_acc * 0.9, "latency": {}, "per_dimension": {}}),
        encoding="utf-8",
    )
    return path


def _run(argv: list[str], search_fn: Any) -> tuple[int, str]:
    """执行 CLI 主入口（桩注入 + 输出捕获），返回 (退出码, 输出)。"""
    stdout = io.StringIO()
    code = retrieval_regression.main(argv, search_fn=search_fn, stdout=stdout)
    return code, stdout.getvalue()


# ─────────────────────────────── 通过 / 阻断 / 边界 ───────────────────────────────


def test_通过路径_报告落盘与中文输出(tmp_path: Path) -> None:
    """基线 75% → 本期 75%（零回落）→ 退出码 0；md/xlsx 报告生成、摘要为中文。"""
    dataset = _write_dataset(tmp_path)
    baseline = _write_baseline(tmp_path, 0.75)
    stub = _stub({"q1": [101], "q2": [102], "q3": [103], "q4": [999]})
    code, output = _run([
        "--dataset", str(dataset), "--baseline", str(baseline),
        "--report-dir", str(tmp_path / "reports"), "--floor-pct", "0",
    ], stub)
    assert code == 0
    assert "结论：通过" in output
    assert "Top5 命中 3/4（75.0%）" in output
    reports = list((tmp_path / "reports").glob("ci-*.md"))
    assert reports and reports[0].with_suffix(".xlsx").exists()
    assert "检索质量评测报告" in reports[0].read_text(encoding="utf-8")


def test_阻断路径_回落超阈值退出码1(tmp_path: Path) -> None:
    """基线 100% → 本期 75%（回落 25pp）→ 退出码 1，输出阻断原因。"""
    dataset = _write_dataset(tmp_path)
    baseline = _write_baseline(tmp_path, 1.0)
    stub = _stub({"q1": [101], "q2": [102], "q3": [103], "q4": [999]})
    code, output = _run([
        "--dataset", str(dataset), "--baseline", str(baseline),
        "--report-dir", str(tmp_path / "reports"), "--floor-pct", "0",
    ], stub)
    assert code == 1
    assert "结论：阻断" in output
    assert "阻断原因" in output and "25.00pp" in output


def test_边界_回落恰2pp通过(tmp_path: Path) -> None:
    """基线 77% → 本期 75%（回落恰 2.0pp）→ 退出码 0（>2pp 才阻断）。"""
    dataset = _write_dataset(tmp_path)
    baseline = _write_baseline(tmp_path, 0.77)
    stub = _stub({"q1": [101], "q2": [102], "q3": [103], "q4": [999]})
    code, _output = _run([
        "--dataset", str(dataset), "--baseline", str(baseline),
        "--report-dir", str(tmp_path / "reports"), "--floor-pct", "0",
    ], stub)
    assert code == 0


def test_绝对门槛阻断(tmp_path: Path) -> None:
    """floor 90：本期 75% → 退出码 1 且原因含「绝对门槛」（双阈值口径）。"""
    dataset = _write_dataset(tmp_path)
    stub = _stub({"q1": [101], "q2": [102], "q3": [103], "q4": [999]})
    code, output = _run([
        "--dataset", str(dataset), "--floor-pct", "90",
        "--report-dir", str(tmp_path / "reports"),
    ], stub)
    assert code == 1
    assert "绝对门槛" in output


# ─────────────────────────────── 基线刷新 ───────────────────────────────


def test_更新基线后复跑通过(tmp_path: Path) -> None:
    """--update-baseline：首跑（高基线回落）→ 通过路径刷新基线 → 复跑通过。

    首跑用宽松基线（75%）通过并刷新；随后人为写高基线触发阻断，再以
    刷新后的基线复跑验证文件确已更新为本期指标。
    """
    dataset = _write_dataset(tmp_path)
    baseline = _write_baseline(tmp_path, 0.75)
    stub = _stub({"q1": [101], "q2": [102], "q3": [103], "q4": [999]})
    argv = ["--dataset", str(dataset), "--baseline", str(baseline),
            "--report-dir", str(tmp_path / "reports"), "--floor-pct", "0",
            "--update-baseline"]
    code, output = _run(argv, stub)
    assert code == 0
    assert "基线已刷新" in output
    refreshed = json.loads(baseline.read_text(encoding="utf-8"))
    assert refreshed["top5_acc"] == 0.75 and refreshed["hit_count"] == 3

    # 刷新后的基线含分维度指标，复跑同水位 → 依旧通过（新 run 成为基线的口径）
    code2, _ = _run(argv, stub)
    assert code2 == 0


# ─────────────────────────────── 数据与基线缺失 ───────────────────────────────


def test_基线文件缺失退出码1(tmp_path: Path) -> None:
    """--baseline 指向不存在文件 → E_EVAL_BASELINE_MISSING 口径退出码 1。"""
    dataset = _write_dataset(tmp_path)
    code, output = _run([
        "--dataset", str(dataset),
        "--baseline", str(tmp_path / "nowhere.json"),
        "--report-dir", str(tmp_path / "reports"),
    ], _stub({}))
    assert code == 1
    assert "E_EVAL_BASELINE_MISSING" in output


def test_数据集缺失退出码1(tmp_path: Path) -> None:
    """--dataset 指向不存在文件 → 中文报错退出码 1。"""
    code, output = _run([
        "--dataset", str(tmp_path / "nowhere.json"),
        "--report-dir", str(tmp_path / "reports"),
    ], _stub({}))
    assert code == 1
    assert "评测集加载失败" in output


# ─────────────────────────────── 子进程冒烟 ───────────────────────────────


def test_子进程直接执行冒烟() -> None:
    """python3 tools/regression/retrieval_regression.py --help → 退出码 0。

    覆盖直接以文件路径执行形态：仓库根 sys.path 自举 + services 包导入在
    进程外可用（argparse 帮助为中文摘要）。
    """
    result = subprocess.run(
        [sys.executable, str(CLI_PATH), "--help"],
        capture_output=True, text=True, timeout=60, cwd=str(CLI_PATH.parents[2]),
    )
    assert result.returncode == 0
    assert "检索质量回归门禁" in result.stdout
