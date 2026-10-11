# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""零代码守护工具元测试（NFR-SCA-02 §6.1-6.3/§9；TDD 阶段①②，五类场景齐备）。

覆盖：zero_code_check 干净仓库通过（依赖方向 + 出厂种子 + 覆盖链三节）、
依赖方向违例检出（foundation import services）、出厂种子关键词命中检出、
隔离套件 CLI 可独立运行出 MD 报告。
验收口径映射：SRS §16.6「新租户纯配置接入演练（无代码变更）」+「剥离验证」。

实现方案：docs/07-实现方案/NFR/SR-NFR-SCA-02-多租户扩展.md §6.1-6.3/§9
"""

from __future__ import annotations

from pathlib import Path

from tools.saas.isolation_suite import main as isolation_main
from tools.saas.zero_code_check import (
    REPO_ROOT,
    SeedRow,
    render_markdown,
    run_checks,
)


def test_zero_code_check_clean_repo_passes() -> None:
    """干净仓库（当前工作区）→ 零违例；报告含三节：依赖方向/出厂种子/覆盖链。"""
    report = run_checks(REPO_ROOT)
    assert report.violations == [], "仓库出现剥离违例（真实回归）"
    assert report.passed is True
    text = render_markdown(report)
    for section in ("依赖方向", "出厂种子", "覆盖链"):
        assert section in text
    assert report.chain_checked >= 1  # 覆盖链检查真实遍历了内置模板键


def test_zero_code_check_detects_dependency_violation(tmp_path: Path) -> None:
    """依赖方向违例：伪造仓库根内 foundation 文件 import services → 检出且不通过。"""
    pkg = tmp_path / "foundation" / "bad"
    pkg.mkdir(parents=True)
    (pkg / "leak.py").write_text(
        "# 知识产权归甲方所有（COM-IP-01）\nfrom services.saas import api\n", encoding="utf-8"
    )
    report = run_checks(tmp_path)
    assert report.passed is False
    assert any(v.rule == "D1-foundation-imports-services" for v in report.violations)


def test_zero_code_check_seed_keyword_hit(tmp_path: Path) -> None:
    """出厂种子关键词命中：注入脏种子 + 甲方关键词清单（COM-IP-06 参数注入）→ 检出。"""
    dirty = [SeedRow(scope="prompt", key="reply.system", body="甲公司专供 模型X-900 参数")]
    report = run_checks(tmp_path, seeds=dirty, keywords=("模型X-900",))
    assert report.passed is False
    assert any(v.rule == "keyword-hit" and "模型X-900" in v.detail for v in report.violations)


def test_isolation_cli_main_writes_report(tmp_path: Path) -> None:
    """隔离套件 CLI：可独立运行，退出码 0=PASS，MD 报告落盘（NFR-SEC-07 §5 可复现命令）。"""
    out_dir = tmp_path / "iso-reports"
    exit_code = isolation_main(["--report-out", str(out_dir)])
    assert exit_code == 0
    reports = list(out_dir.glob("*.md"))
    assert reports, "CLI 未产出 MD 报告"
    assert "覆盖矩阵" in reports[0].read_text(encoding="utf-8")
