# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""各 Phase KPI 预跑 CLI「失败注入」元测试（CR P2-b 修复）。

口径：喂一个必败检查（不可能达标的阈值/参数，或注入失败的子进程桩），
断言 KPI 判定不被「全绿假象」吞掉——报告判「未通过/❌ 不通过」且退出码 1。
phase2 已有同款元测试（tools/tests/test_phase2_kpi.py），本文件补齐
phase0/phase1/phase4 三个 CLI 的失败路径覆盖（phase3/phase5 证据在本次
CR T1/T2 中已改为实跑 pytest/metrics 取退出码，失败即传导）。
"""

from __future__ import annotations

from pathlib import Path


def test_phase0_kpi_fail_injection_floor_above_100(tmp_path: Path) -> None:
    """CR P2-b：phase0 注入必败检查——floor-pct=101（准确率物理上限 100%）→ exit 1。"""
    from tools.precheck.phase0_kpi import main

    report = tmp_path / "phase0_kpi_report.md"
    code = main(["--floor-pct", "101", "--report", str(report)])
    assert code == 1  # 必败检查不被吞掉：退出码 1
    text = report.read_text(encoding="utf-8")
    assert "检索准确率（Top5）" in text and "未通过" in text  # 报告判定不通过
    assert "全绿" not in text.split("## 四项 KPI 结论")[0]  # 总体结论不是全绿


def test_phase1_kpi_fail_injection_impossible_hit_quota(tmp_path: Path) -> None:
    """CR P2-b：phase1 注入必败检查——qa-hit=9999（语料出题上限远低）→ exit 1。"""
    from tools.training.phase1_kpi import main

    report = tmp_path / "phase1_kpi_report.md"
    code = main(["--qa-hit", "9999", "--report", str(report)])
    assert code == 1
    text = report.read_text(encoding="utf-8")
    assert "未通过" in text  # 判定表含未通过行
    assert "语料可出题文件不足" in text  # 失败清单直出注入原因


def test_phase4_kpi_fail_injection_subprocess_stub(tmp_path: Path, monkeypatch) -> None:
    """CR P2-b：phase4 注入必败检查——_run 桩返回退出码 1 → 全部判定 ❌ + exit 1。"""
    from tools.agent import phase4_kpi

    (tmp_path / "out" / "agent").mkdir(parents=True)  # main() 写报告前不建目录（仓库内已存在）
    monkeypatch.setattr(phase4_kpi, "_REPO_ROOT", tmp_path)  # 报告落 tmp_path，不写仓库 out/
    monkeypatch.setattr(phase4_kpi, "_run", lambda cmd: (1, "注入失败：子进程退出码 1"))

    code = phase4_kpi.main()
    assert code == 1
    report = (tmp_path / "out" / "agent" / "phase4_kpi_report.md").read_text(encoding="utf-8")
    assert "❌ 不通过" in report  # 判定表逐行 ❌
    assert "存在不通过项" in report  # 结论拒绝「全部 KPI 通过」
