# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""§16.1 KPI 预跑 CLI 冒烟测试（tools/precheck/phase0_kpi.py，迭代 0.3）。

子进程真实拉起 CLI：样例语料全链入库 → 四项 KPI 检查 → 中文 Markdown 报告
落盘 → 退出码 0。开发计划 §4.1 迭代 0.3 退出条件「§16.1 KPI 预跑全绿」的
自动化守护（生产验收替换口径的说明亦须出现在报告里）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: 仓库根（tests/ → kb/ → services/ → repo/）
REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_PATH = REPO_ROOT / "tools" / "precheck" / "phase0_kpi.py"


def _run_cli(*args: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    """子进程执行 CLI（仓库根工作目录；stdout/stderr 合并捕获）。"""
    return subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def test_cli冒烟_四项全绿退出码0(tmp_path: Path) -> None:
    """全链冒烟：退出码 0 + 四项 KPI 逐条「通过」+ 报告含生产替换口径说明。"""
    report_path = tmp_path / "phase0_kpi_report.md"
    completed = _run_cli("--report", str(report_path))

    assert completed.returncode == 0, f"CLI 未通过：\n{completed.stdout}\n{completed.stderr}"
    for kpi_name in ("知识库覆盖率", "检索准确率", "检索响应速度", "数据完整性"):
        assert kpi_name in completed.stdout, f"stdout 缺少 KPI 行：{kpi_name}"
    assert "全绿" in completed.stdout
    # 报告落盘且为中文 Markdown：四项 KPI 表 + 生产验收替换口径注明
    assert report_path.is_file()
    content = report_path.read_text(encoding="utf-8")
    assert "# Phase 0 §16.1 KPI 预跑报告" in content
    assert "| 1 | 知识库覆盖率 |" in content
    assert "生产验收须替换为甲方真实素材" in content
    assert "≥100 条人工评测集" in content or "不少于 100 条人工评测集" in content
    # 分文件明细：样例语料八素材全部 success
    assert "product_产品手册_ST3200.docx" in content
    assert "training_新人第一课.docx" in content


def test_cli冒烟_评测集规模与六库覆盖(tmp_path: Path) -> None:
    """评测集 ≥20 条且六库均有用例（样例口径的下限守护）。"""
    import json

    eval_path = REPO_ROOT / "tools" / "precheck" / "sample_corpus" / "eval_set_phase0.json"
    payload = json.loads(eval_path.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert len(cases) >= 20
    assert {case["kb_type"] for case in cases} == {
        "product", "customer", "skill", "process", "negative_case", "training",
    }
    # 样例素材八文件全部在位（覆盖率 100% 的分母前提）
    corpus = eval_path.parent
    expected_files = {
        "product_产品手册_ST3200.docx",
        "product_参数表.xlsx",
        "product_产品目录.pdf",
        "customer_分级标准.docx",
        "skill_议价话术.docx",
        "process_报价SOP.docx",
        "negative_丢单复盘.docx",
        "training_新人第一课.docx",
    }
    assert expected_files <= {p.name for p in corpus.iterdir() if p.is_file()}
