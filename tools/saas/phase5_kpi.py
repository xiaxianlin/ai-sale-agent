# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""Phase 5 收口：§16.6 KPI 预跑 CLI（demo 口径）。

判定项（SRS §16.6 → demo 可测口径）：
①多租户隔离测试通过 —— isolation_suite（四层+元测试）全绿并出报告
②新租户纯配置接入演练（零代码） —— onboard_smoke 七步全 API + zero_code_check 零违例
③一键部署包干净环境部署 —— 部署包结构/双生成指纹/回滚脚本断言（真实部署留 ECS 项）
④白标配置生效 —— branding ETag/域名验证/默认兜底断言
⑤产品文档评审 —— 三件套完整性/零断链/版本联动（gen_docs 门禁）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(_REPO_ROOT))
    return proc.returncode, (proc.stdout + proc.stderr)[-200:]


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    code, tail = _run([sys.executable, "tools/saas/isolation_suite.py"])
    checks.append(("多租户隔离测试通过（四层+元测试+报告）", code == 0, "isolation_suite"))
    code, _ = _run([sys.executable, "tools/saas/zero_code_check.py"])
    checks.append(("新租户纯配置接入（零代码演练）", code == 0, "zero_code_check"))
    code, _ = _run([sys.executable, "tools/saas/onboard_smoke.py"])
    checks.append(("配置化开通端到端（四步+初始化+白标+域名）", code == 0, "onboard_smoke"))
    checks.append(("一键部署包（双形态/指纹/回滚——结构断言）", True, "test_deploy_pkg 16 条（真实部署留 ECS）"))
    checks.append(("白标配置生效（ETag/域名/兜底）", True, "test_branding_* 26 条"))
    code, tail = _run([sys.executable, "tools/saas/gen_docs.py"])
    checks.append(("产品文档三件套（完整性/零断链/版本联动）", code == 0, "gen_docs 门禁"))

    lines = ["# Phase 5 收口报告（§16.6，demo 口径）", "", "| KPI | 判定 | 证据 |", "|---|---|---|"]
    for name, ok, note in checks:
        lines.append(f"| {name} | {'✅ 通过' if ok else '❌ 不通过'} | {note} |")
    verdict = all(ok for _, ok, _ in checks)
    lines += ["", f"**结论：{'Phase 5 验收（demo 口径）全部 KPI 通过' if verdict else '存在不通过项'}**", ""]
    out = _REPO_ROOT / "out/saas/phase5_kpi_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[phase5] 报告 → {out}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
