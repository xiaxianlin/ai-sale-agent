# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""Phase 2 §16.3 KPI 预跑 CLI 单元测试（迭代 2.3 收口；demo 口径）。

覆盖：
- run_checks() 五项检查结构齐全且全绿（demo 口径）；
- main() 退出码 0 + 判定表报告落盘（含五项 KPI 与结论）。

实现方案：docs/07-实现方案/AG/SR-AG-004~007、SR-NFR-SEC-10~12；SRS §16.3
"""

from __future__ import annotations

from pathlib import Path

from tools.agent.phase2_kpi import KPI_NAMES, main, render_report, run_checks


class TestPhase2Kpi:
    """五项 KPI 预跑（demo 口径置顶：结构口径替代观测口径，正式口径占位声明）。"""

    def test_run_checks_five_all_passed(self) -> None:
        """五项检查：名称与 §16.3 对齐、结构齐全、demo 口径全部达标。"""
        checks = run_checks()
        assert len(checks) == 5
        assert tuple(check["name"] for check in checks) == KPI_NAMES
        for check in checks:
            assert {"name", "target", "actual", "detail", "passed"} <= set(check)
            assert check["passed"], f"{check['name']} 未达标：{check['actual']}"

    def test_main_exit_zero_and_report(self, tmp_path: Path) -> None:
        """main()：退出码 0，判定表报告落盘且含五项 KPI 与口径声明。"""
        report = tmp_path / "phase2_kpi_report.md"
        code = main(["--report", str(report)])
        assert code == 0
        text = report.read_text(encoding="utf-8")
        for name in KPI_NAMES:
            assert name in text
        assert "判定" in text
        assert "14 天" in text        # 正式口径占位声明必须在场（防口径粉饰）
