# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
# zero_code_check.py —— 零代码守护工具（NFR-SCA-02，SR-NFR-SCA-02 §2/§6.1-6.3）：
# 「新租户接入无代码变更」不是一次性表演，而是三条不变式的持续守护，全部机检：
#   ① 依赖方向：foundation 禁止反向 import services；产品侧禁止引用 tenant-content；
#   ② 出厂种子：全部挂默认域 tenant_id=0、登记于产品默认集、甲方关键词零命中；
#   ③ 覆盖链：内置模板键在默认域全部可达，租户覆盖 > 平台默认 即时生效。
"""零代码审计工具：依赖方向 + 出厂种子 + 覆盖链三节检查，输出 MD 报告。

可独立运行（FR-SAAS-007 剥离验证阶段 A 与 NFR-SCA-02 守护三件套的收敛入口）：
    python3 tools/saas/zero_code_check.py --repo . \\
        --keywords tenant-content/security/tenant_keywords.txt \\
        --report-out out/zero_code_report.md
退出码：0=全部通过；1=存在违例（CI 阻断，saas.zero_code.ci_blocking 口径）。
甲方关键词清单经 --keywords 参数注入，不入产品包（COM-IP-06）。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
# 独立运行引导：把 <repo>/src 加入 sys.path（工具目录不依赖安装）
REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from services.saas.core.separation.guard import (  # noqa: E402
    BUILTIN_TEMPLATES,
    SeedRow,
    TemplateKeyUnknownError,
    TemplateLoader,
    Violation,
    audit_factory_seeds,
    builtin_seed_rows,
    scan_dependency_direction,
)


@dataclass
class ZeroCodeReport:
    """三节检查结果汇总（CI 阻断与 MD 报告的统一载体）。"""

    repo_root: str
    checked_at: str
    violations: list[Violation] = field(default_factory=list)
    chain_checked: int = 0
    seed_scanned: int = 0

    @property
    def passed(self) -> bool:
        """任一违例即不通过（无豁免；修复后全量重跑，SR-SAAS-007 §6.4 口径）。"""
        return not self.violations


def _check_override_chain() -> tuple[list[Violation], int]:
    """③ 覆盖链检查（NFR-SCA-02 扩展点外置 + FR-SAAS-007 加载链）：
    每个内置键经「默认域 → 租户覆盖」链路可加载，覆盖变更即时生效。"""
    violations: list[Violation] = []
    loader = TemplateLoader(defaults=dict(_seed_map()), clock=None, cache_ttl_sec=60)
    checked = 0
    probe_tenant = 2  # 探针租户（非 0/1：验证覆盖链对新租户生效）
    for (scope, key), body in sorted(BUILTIN_TEMPLATES.items()):
        checked += 1
        try:
            if loader.load(probe_tenant, scope, key) != body:
                violations.append(Violation(
                    "chain-default-missing", f"{scope}/{key}", 0,
                    "默认域加载结果与内置模板不一致（两套默认漂移，SR §6.3 预检口径）",
                ))
            marker = f"__租户覆盖探针__{key}"
            loader.put_override(probe_tenant, scope, key, marker)
            if loader.load(probe_tenant, scope, key) != marker:
                violations.append(Violation(
                    "chain-override-inert", f"{scope}/{key}", 0,
                    "租户覆盖未生效（缓存失效缺失或优先级错误）",
                ))
        except (TemplateKeyUnknownError, Exception) as exc:  # noqa: B014 - 单键异常不中断
            violations.append(Violation(
                "chain-error", f"{scope}/{key}", 0, f"覆盖链检查异常: {exc}",
            ))
    return violations, checked


def _seed_map() -> dict[tuple[str, str], str]:
    """出厂种子 → 默认域映射（覆盖链检查的默认域数据源）。"""
    return {(row.scope, row.key): row.body for row in builtin_seed_rows()}


def run_checks(
    repo_root: Path | str,
    *,
    keywords: tuple[str, ...] | list[str] = (),
    seeds: list[SeedRow] | None = None,
) -> ZeroCodeReport:
    """三节检查入口：①依赖方向（对仓库根实扫）②出厂种子审计 ③覆盖链。

    seeds/keywords 参数供测试与验收注入（COM-IP-06：关键词清单不入产品包）；
    缺省对内置出厂种子做零关键词回归扫描。
    """
    root = Path(repo_root)
    report = ZeroCodeReport(
        repo_root=str(root),
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
    # ① 依赖方向（foundation 禁 import services；产品侧禁 tenant-content）
    report.violations.extend(scan_dependency_direction(root))
    # ② 出厂种子审计（归属 tenant_id=0 + 登记完备 + 关键词零命中）
    rows = builtin_seed_rows() if seeds is None else seeds
    report.seed_scanned = len(rows)
    report.violations.extend(
        audit_factory_seeds(rows, set(BUILTIN_TEMPLATES), keywords=tuple(keywords))
    )
    # ③ 覆盖链（默认域可达 + 租户覆盖即时生效）
    chain_violations, checked = _check_override_chain()
    report.chain_checked = checked
    report.violations.extend(chain_violations)
    return report


def render_markdown(report: ZeroCodeReport) -> str:
    """审计报告（MD）：三节结论 + 违例清单 + 复跑命令（验收证据随材料交付）。"""
    verdict = "PASS" if report.passed else "FAIL"
    lines = [
        "# 零代码守护审计报告（NFR-SCA-02 / FR-SAAS-007）",
        "",
        "## 1. 执行概要",
        "",
        f"- 被审计仓库根：`{report.repo_root}`",
        f"- 执行时间：{report.checked_at}",
        f"- 结论：**{verdict}**（违例 {len(report.violations)} 项）",
        f"- 出厂种子扫描：{report.seed_scanned} 行；覆盖链检查：{report.chain_checked} 键",
        "",
        "## 2. 依赖方向（不变式①）",
        "",
        "规则：foundation 禁止反向 import services；产品侧禁止静态/动态引用 tenant-content。",
        "",
        "## 3. 出厂种子（不变式②）",
        "",
        "规则：全部种子挂默认域 tenant_id=0、登记于产品默认集、甲方关键词零命中"
        "（清单经 --keywords 参数注入，COM-IP-06 不入产品包）。",
        "",
        "## 4. 覆盖链（不变式③）",
        "",
        "规则：内置模板键在默认域全部可达；租户覆盖 > 平台默认，变更即时生效"
        "（新租户差异全走配置/模板覆盖，代码零分叉）。",
        "",
        "## 5. 违例清单",
        "",
    ]
    if not report.violations:
        lines.append("- 无违例（三条不变式全部成立）")
    else:
        for v in report.violations:
            lines.append(f"- [{v.rule}] {v.file}:{v.line} — {v.detail}")
    lines += [
        "",
        "## 6. 复跑命令",
        "",
        "```bash",
        "python3 tools/saas/zero_code_check.py --repo . --report-out out/zero_code_report.md",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：--repo 仓库根 / --keywords 关键词文件（一行一词）/ --report-out 报告路径。"""
    parser = argparse.ArgumentParser(description="零代码守护审计（NFR-SCA-02）")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="被审计仓库根")
    parser.add_argument("--keywords", default="", help="甲方关键词清单文件（一行一词，参数注入）")
    parser.add_argument("--report-out", default=str(REPO_ROOT / "out" / "zero_code_report.md"))
    args = parser.parse_args(argv)

    keywords: tuple[str, ...] = ()
    if args.keywords:
        kw_path = Path(args.keywords)
        keywords = tuple(
            line.strip() for line in kw_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    report = run_checks(args.repo, keywords=keywords)
    out = Path(args.report_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(report), encoding="utf-8")
    print(f"零代码审计结论: {'PASS' if report.passed else 'FAIL'}"
          f"（违例 {len(report.violations)} 项，报告: {out}）")
    return 0 if report.passed else 1


if __name__ == "__main__":  # 独立运行入口（CI 门禁与验收复跑）
    raise SystemExit(main())
