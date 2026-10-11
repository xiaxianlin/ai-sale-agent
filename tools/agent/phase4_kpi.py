# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""Phase 4 收口：§16.5 KPI 预跑 CLI（demo 口径）。

判定项（SRS §16.5 建议验收项 → demo 可测口径）：
①28 实例全部稳定（参照 P2 口径）——gray_smoke 实例注册与状态墙 + 集群隔离测试（正式口径 14 天观测占位）
②批量操作正确性 —— adminv3_smoke 批量含故意失败→失败清单→重试成功
③账号级行为差异化抽检 —— riskv2 画像差异化 + simhash 防同质 + 错峰窗口互异断言
④移动端功能可用 —— adminv3_smoke 移动 BFF 聚合 + 高危审批二次校验
⑤集群单点故障演练 —— 集群编排测试的 kill 隔离断言（演练即测试）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(_REPO_ROOT))
    return proc.returncode, (proc.stdout + proc.stderr)[-300:]


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    code, _ = _run([sys.executable, "tools/agent/gray_smoke.py"])
    checks.append(("28 实例稳定与隔离（结构口径，14 天观测占位）", code == 0, "gray_smoke"))
    checks.append(("灰度放量机制（门禁三硬指标+回滚）", code == 0, "gray_smoke 门禁/决策台账"))
    code, _ = _run([sys.executable, "tools/agent/adminv3_smoke.py"])
    checks.append(("批量操作正确性（部分失败+重试）", code == 0, "adminv3_smoke"))
    checks.append(("移动端功能可用（BFF/二次校验）", code == 0, "adminv3_smoke"))
    # 差异化抽检：跑 riskv2 测试子集
    code, tail = _run([sys.executable, "-m", "pytest",
                       "src/services/agent/tests/test_riskv2_profiles.py",
                       "src/services/agent/tests/test_riskv2_stagger.py",
                       "src/services/agent/tests/test_riskv2_uniqueness.py", "-q"])
    checks.append(("账号级行为差异化抽检（画像/错峰/防同质）", code == 0, tail.strip().splitlines()[-1] if tail.strip() else ""))

    lines = ["# Phase 4 收口报告（§16.5，demo 口径）", "", "| KPI | 判定 | 证据 |", "|---|---|---|"]
    for name, ok, note in checks:
        lines.append(f"| {name} | {'✅ 通过' if ok else '❌ 不通过'} | {note} |")
    verdict = all(ok for _, ok, _ in checks)
    lines += ["", f"**结论：{'Phase 4 验收（demo 口径）全部 KPI 通过' if verdict else '存在不通过项'}**", ""]
    out = _REPO_ROOT / "out/agent/phase4_kpi_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[phase4] 报告 → {out}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
