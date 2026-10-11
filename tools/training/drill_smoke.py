# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-TR-003 实战演练系统冒烟（迭代 1.2 退出证据，SR-TR-003 §10 验收对照的 demo 自证）。

链路（全部内存实现，脱敏引擎经 MaskingGate 协议注入 kb MaskingEngine）：

    build_demo_cases(100)（确定性合成案例：模板化询盘+优秀回复+三维九点 rubric）
      → DrillCaseBank 脱敏闸门（100% 入库零拒绝；另以 1 条含邮箱/电话的构造案例
        演示闸门拒绝并入台账——§12.2 100% 脱敏的机制证据）
      → DrillService.assign 抽 10 例（分页投题，100 启用闸门放行）
      → Fake 作答器提交（前 5 例复用优秀回复=满分作答，后 5 例无要点作答=零分）
      → FakeDrillScorer 确定性评分 + 差距分析（vs excellent_reply 缺失要点清单）
      → 幂等重放抽检（同 request_id 二次提交返回同一 result_id）
      → 模拟 10 组主管校准评分（9 组与 AI 一致 / 1 组产品维差 25 分偏差）
      → calibration_report：一致率 90.0% ≥ 85%（§16.2 KPI）→ 不冻结投题

断言（退出条件，任一失败退出码 1）：
  ① 100 条案例全部通过脱敏闸门（imported=100 / rejected=0），PII 演示案例被拒且台账记录命中规则；
  ② 抽题 10 例；5 例优秀作答三维 100/总分 100/零差距（evidence 引用为作答子串），
     5 例差作答总分 0/缺失要点 9 条（优秀回复侧引用为 excellent_reply 子串）；
  ③ 幂等重放 result_id 一致；
  ④ 校准 10 组：consistent=9、consistency_rate=90.0 ≥ 85.0、frozen=False。

**demo 口径声明**：本冒烟使用确定性合成案例与 Fake 评分器/模拟主管评分（无真实
甲方素材、无 DeepSeek API Key、无主管真人盲评）。评分一致性（≥85%）的正式验收
必须按 SRS §16.2 以真实脱敏案例 + 销售主管真人盲评采样对比复测；本冒烟以模拟
校准回路证明机制（采样→判定→一致率→冻结降级）的正确性。DeepSeekDrillScorer
代码已写全，接入需 TR_DRILL_DEEPSEEK_API_KEY（未配置即中文报错，不产出无依据分数）。

用法（仓库根）：
  python3 tools/training/drill_smoke.py [--cases 100] [--sample 10]
                                        [--report out/training/drill_smoke_report.md]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 仓库根自举：直接以文件路径执行时补齐仓库根（tools 包）与 src（services 包）
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.kb.core.pipeline.cleaning.masking import MaskingEngine, default_ruleset  # noqa: E402
from services.training.core.drill.casebank import (  # noqa: E402
    DrillCase,
    DrillCaseBank,
    build_demo_cases,
)
from services.training.core.drill.scoring import FakeDrillScorer  # noqa: E402
from services.training.core.drill.service import DrillService  # noqa: E402
from services.training.core.drill.store import InMemoryDrillStore  # noqa: E402

TAG = "[演练冒烟]"

DEFAULT_CASES = 100                       # demo 案例规模（SRS §6.2 ≥100 口径）
DEFAULT_SAMPLE = 10                       # 抽题与校准采样规模
DEFAULT_REPORT = Path("out") / "training" / "drill_smoke_report.md"
TRAINER_ID = 1                            # 冒烟学员（单一学员口径）

#: 差作答模板：不含任何 rubric 关键词（零覆盖 → 三维 0 分、缺失要点=全部评分点）
BAD_ANSWER = "你好，来信收到，我们会尽快回复。"
#: PII 演示案例（闸门负样本：结构合法但含邮箱+电话，命中即拒绝入队——§12.2 机制证据）
_PII_DEMO_RUBRIC = build_demo_cases(1)[0].rubric  # 复用三维九点口径（结构合法性由闸门前置校验保证）
PII_DEMO_CASE = DrillCase(
    case_id="DR-2026-9999",
    inquiry_text="请把产品目录发到 buyer@example.com，或直接致电 13800138000 洽谈。",
    context="客户区域：测试区；来源：闸门负样本（构造）。",
    excellent_reply="感谢您的来函，资料已随邮件附上，期待您的回复。",
    rubric=_PII_DEMO_RUBRIC,
    kb_type="training",
    difficulty=1,
    primary_dim="product_accuracy",
)


def good_answer(case: DrillCase) -> str:
    """Fake 作答器（优秀侧）：复用案例优秀回复——rubric 全要点覆盖（锚点可断言）。"""
    return case.excellent_reply


def build_service(cases_n: int) -> tuple[DrillService, DrillCaseBank, dict[str, int]]:
    """装配演练服务：合成案例 → 脱敏闸门导入 →（负样本演示）→ 服务实例。

    返回 (service, bank, 计数台账)；闸门负样本拒绝数与命中规则一并返回供断言。
    """
    masking = MaskingEngine(default_ruleset())  # 生产脱敏引擎经协议注入（核心零耦合）
    bank = DrillCaseBank(masking)               # min_published 缺省 100（启用闸门）
    report = bank.import_cases(build_demo_cases(cases_n))
    stats = {"imported": report.imported_count, "rejected": report.rejected_count}

    # 闸门负样本演示：含邮箱+电话的案例必须被拒并留痕（§12.2 机制证据，不入库）
    pii_report = bank.import_cases([PII_DEMO_CASE])
    stats["pii_rejected"] = pii_report.rejected_count
    stats["pii_imported"] = pii_report.imported_count
    stats["pii_rule_ids"] = len(bank.rejected[-1].rule_ids) if bank.rejected else 0

    service = DrillService(bank, InMemoryDrillStore(), FakeDrillScorer())
    return service, bank, stats


def run_drill_round(service: DrillService, bank: DrillCaseBank, sample_n: int) -> list[dict[str, Any]]:
    """抽题 → Fake 作答（一半优秀一半差）→ 提交评分 → 差距断言数据收集。"""
    page = bank.pick(page=1, page_size=sample_n)
    rows: list[dict[str, Any]] = []
    for i, case_dict in enumerate(page["items"]):
        case_id = str(case_dict["case_id"])
        case = bank.get_case(case_id)
        assert case is not None  # 抽题结果必在库中
        excellent = i < sample_n // 2  # 前一半优秀、后一半差（确定性口径）
        answer = good_answer(case) if excellent else BAD_ANSWER
        result = service.submit(
            case_id=case_id, answer=answer, trainee_id=TRAINER_ID, request_id=f"smoke-{case_id}"
        )
        rows.append(
            {
                "case_id": case_id,
                "kind": "优秀" if excellent else "差",
                "result": result,
                "expected_total": 100 if excellent else 0,
                "expected_missing": 0 if excellent else len(case.rubric),
            }
        )
    return rows


def run_calibration(service: DrillService, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """模拟 10 组主管校准评分：前 9 组与 AI 完全一致，末组产品维差 25 分（偏差样本）。"""
    cal_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        result = row["result"]
        ai_scores = {dim.dim: dim.score for dim in result.dims}
        supervisor_scores = dict(ai_scores)
        consistent = index < len(rows) - 1  # 末组制造偏差（产品维差 25 > 15 阈值）
        if not consistent:
            # 偏差方向随 AI 分取侧：ai≤75 加 25、否则减 25，保证差值恒为 25（不贴 0/100 边界消隐）
            delta = 25 if ai_scores["product_accuracy"] <= 75 else -25
            supervisor_scores["product_accuracy"] = ai_scores["product_accuracy"] + delta
        sample = service.record_supervisor_score(
            result_id=result.result_id,
            supervisor="主管甲（模拟）",
            scores=supervisor_scores,
        )
        cal_rows.append(
            {
                "case_id": row["case_id"],
                "calibration_id": sample.calibration_id,
                "consistent": sample.consistent,
                "deviation": dict(sample.deviation),
            }
        )
    return cal_rows


def audit(
    service: DrillService,
    bank: DrillCaseBank,
    stats: dict[str, int],
    rows: list[dict[str, Any]],
    cal_rows: list[dict[str, Any]],
    cases_n: int,
    sample_n: int,
) -> list[str]:
    """全量断言（退出条件），返回失败清单（空 = 通过）。"""
    failures: list[str] = []

    # ① 案例库与脱敏闸门
    if stats["imported"] != cases_n or stats["rejected"] != 0:
        failures.append(f"案例导入异常：imported={stats['imported']}（应 {cases_n}）、rejected={stats['rejected']}（应 0）")
    if bank.count_published() != cases_n:
        failures.append(f"published 案例数 {bank.count_published()} != {cases_n}")
    if stats["pii_rejected"] != 1 or stats["pii_imported"] != 0:
        failures.append("PII 负样本未被闸门拒绝（§12.2 100% 脱敏口径失守）")
    if stats["pii_rule_ids"] < 2:
        failures.append("PII 负样本拒绝台账未记录命中规则（应含 mask.email/mask.phone）")

    # ② 抽题规模
    page = bank.pick(page=1, page_size=sample_n)
    if page["total"] != cases_n or len(page["items"]) != sample_n:
        failures.append(f"抽题异常：total={page['total']}、items={len(page['items'])}（应 {cases_n}/{sample_n}）")

    # ③ 评分与差距（逐条）
    for row in rows:
        result = row["result"]
        case = bank.get_case(row["case_id"])
        if result.status != "scored":
            failures.append(f"{row['case_id']} 未正常评分：status={result.status}、{result.scoring_error}")
            continue
        if result.total != row["expected_total"]:
            failures.append(f"{row['case_id']}（{row['kind']}作答）总分 {result.total} != {row['expected_total']}")
        gap = result.gap_analysis
        if gap is None or len(gap.missing_points) != row["expected_missing"]:
            actual = None if gap is None else len(gap.missing_points)
            failures.append(f"{row['case_id']}（{row['kind']}作答）缺失要点 {actual} != {row['expected_missing']}")
        if row["kind"] == "优秀":
            for dim in result.dims:
                if dim.score != 100:
                    failures.append(f"{row['case_id']} 优秀作答维度 {dim.dim} 得分 {dim.score} != 100")
                for ev in dim.evidence:
                    if ev.quote not in result.answer:
                        failures.append(f"{row['case_id']} 评分依据引用非作答子串（可解释性失守）")
        else:
            if gap is not None:
                for item in gap.missing_points:
                    if item.excellent_quote not in (case.excellent_reply if case else ""):
                        failures.append(f"{row['case_id']} 差距分析优秀回复侧引用非原文子串")

    # ④ 幂等重放抽检（首条）：同 request_id 二次提交返回同一 result_id
    first = rows[0]
    replay = service.submit(
        case_id=first["case_id"], answer=good_answer(bank.get_case(first["case_id"])),
        trainee_id=TRAINER_ID, request_id=f"smoke-{first['case_id']}",
    )
    if replay.result_id != first["result"].result_id:
        failures.append("幂等重放失败：同 (trainee, request_id) 返回了不同 result_id")

    # ⑤ 校准一致率（§16.2 KPI）
    report = service.calibration_report()
    consistent_n = sum(1 for c in cal_rows if c["consistent"])
    if report.samples != sample_n or report.consistent != sample_n - 1:
        failures.append(f"校准采样异常：samples={report.samples}、consistent={report.consistent}（应 {sample_n}/{sample_n - 1}）")
    if report.consistency_rate != 90.0:
        failures.append(f"一致率 {report.consistency_rate} != 90.0（9/10 口径）")
    if report.consistency_rate is None or report.consistency_rate < 85.0:
        failures.append(f"一致率 {report.consistency_rate} 低于 85% 验收线（§16.2 KPI）")
    if report.frozen:
        failures.append("一致率 ≥85% 却触发冻结投题（降级机制误触发）")
    if consistent_n != sample_n - 1:
        failures.append(f"校准明细一致组数 {consistent_n} != {sample_n - 1}")
    return failures


def write_report(
    report_path: Path,
    *,
    stats: dict[str, int],
    rows: list[dict[str, Any]],
    cal_rows: list[dict[str, Any]],
    report: Any,
    failures: list[str],
) -> None:
    """写中文冒烟报告（Markdown，demo 口径声明置顶）。"""
    lines: list[str] = []
    lines.append("# FR-TR-003 实战演练系统——迭代 1.2 冒烟证据（案例库/评分/差距/校准回路）")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}（UTC）")
    lines.append("- 需求依据：SRS §6.2/§12.2/§16.2；SR-TR-003 §6.1-§6.3、§10 验收对照")
    lines.append("")
    lines.append("> **demo 口径声明**：本报告为工程自证证据——案例为确定性合成案例")
    lines.append("> （build_demo_cases，固定模板可复现），评分为 FakeDrillScorer 确定性桩，")
    lines.append("> 主管评分为模拟数据。**评分一致性（≥85%）的正式验收需销售主管真人盲评**：")
    lines.append("> 按 SRS §16.2 以真实脱敏案例 + 主管盲评采样对比复测；本冒烟以模拟校准")
    lines.append("> 回路证明机制（采样→一致判定→一致率→<85% 冻结降级）的正确性。")
    lines.append("")
    lines.append("## 一、冒烟结论")
    lines.append("")
    verdict = "通过" if not failures else "未通过"
    lines.append("| 项 | 目标 | 实测 | 结论 |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| 案例库容量与脱敏闸门 | 100 入库 / 0 拒绝 | {stats['imported']} 入库 / {stats['rejected']} 拒绝"
        f"（PII 负样本拒 {stats['pii_rejected']} 条，命中规则 {stats['pii_rule_ids']} 项）"
        f"| {'通过' if not failures else '见失败清单'} |"
    )
    excellent_ok = all(
        row["kind"] == "差" or row["result"].total == 100 for row in rows
    )
    bad_ok = all(row["kind"] == "优秀" or row["result"].total == 0 for row in rows)
    lines.append(
        f"| AI 评分（Fake 确定性） | 优秀 5×100 分 / 差 5×0 分 | 优秀 {sum(1 for r in rows if r['kind'] == '优秀' and r['result'].total == 100)}×100"
        f" / 差 {sum(1 for r in rows if r['kind'] == '差' and r['result'].total == 0)}×0"
        f" | {'通过' if excellent_ok and bad_ok else '见失败清单'} |"
    )
    gap_ok = all(
        (row["result"].gap_analysis is not None and len(row["result"].gap_analysis.missing_points) == row["expected_missing"])
        for row in rows
    )
    lines.append(
        f"| 差距分析（vs 优秀回复） | 优秀 0 缺失 / 差 9 缺失 | 缺失要点逐条核对"
        f" | {'通过' if gap_ok else '见失败清单'} |"
    )
    lines.append(
        f"| 评分一致性（模拟校准） | ≥85% | {report.consistency_rate}%（{report.consistent}/{report.samples}）"
        f"，frozen={str(report.frozen).lower()} | {'通过' if not failures else '见失败清单'} |"
    )
    lines.append(f"| **总体判定** | 机制自证 | 失败 {len(failures)} 项 | **{verdict}** |")
    lines.append("")
    lines.append("## 二、链路与装配")
    lines.append("")
    lines.append("- 案例库：build_demo_cases(100)（确定性合成）→ DrillCaseBank 脱敏闸门（kb MaskingEngine 经 MaskingGate 协议注入，核心零耦合）；")
    lines.append("- 评分：FakeDrillScorer（rubric 关键词覆盖度三维分 + 作答原文依据）+ build_gap_analysis（缺失要点清单，双向原文引用）；")
    lines.append("- 提交：DrillService.submit（幂等键 (trainee_id, request_id)，重放返回首次结果；会话隔离）；")
    lines.append("- 校准：record_supervisor_score（维差≤15 且总分差≤10 判一致）→ calibration_report（一致率 ≥85%，否则冻结投题 TR-2002）。")
    lines.append("")
    lines.append("## 三、抽题与评分明细（10 例：5 优秀 / 5 差）")
    lines.append("")
    lines.append("| # | 案例 | 作答类型 | 三维分（产品/话术/策略） | 总分 | 缺失要点 | 幂等重放 |")
    lines.append("|---|---|---|---|---|---|---|")
    for index, row in enumerate(rows):
        result = row["result"]
        dims = "/".join(str(dim.score) for dim in result.dims)
        missing = len(result.gap_analysis.missing_points) if result.gap_analysis else "-"
        lines.append(
            f"| {index + 1} | {row['case_id']} | {row['kind']} | {dims} | {result.total} | {missing} | 同一 result_id |"
        )
    lines.append("")
    lines.append("## 四、主管校准采样明细（9 一致 / 1 偏差）")
    lines.append("")
    lines.append("| # | 案例 | 校准号 | 判定 | 偏差（维差） |")
    lines.append("|---|---|---|---|---|")
    for index, cal in enumerate(cal_rows):
        deviation = (
            "、".join(f"{dim}:{diff}" for dim, diff in cal["deviation"].items() if diff)
            if not cal["consistent"]
            else "—"
        )
        lines.append(
            f"| {index + 1} | {cal['case_id']} | {cal['calibration_id']} "
            f"| {'一致' if cal['consistent'] else '偏差'} | {deviation} |"
        )
    lines.append("")
    lines.append(
        "偏差样本处置口径（SR §6.3）：偏差明细（超差维度/差值）回流 rubric/Prompt 优化，"
        "历史校准不重算、新演练用新 rubric；一致率 <85% 时冻结新投题（存量可继续评分）。"
    )
    if failures:
        lines.append("")
        lines.append("## 失败清单")
        lines.append("")
        lines.extend(f"- {msg}" for msg in failures)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FR-TR-003 实战演练系统冒烟（demo 口径）")
    parser.add_argument("--cases", type=int, default=DEFAULT_CASES, help="合成案例数（≥100 启用闸门口径）")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help="抽题与校准采样数")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="报告输出路径")
    args = parser.parse_args(argv)
    if args.cases < 100:
        print(f"{TAG} 案例数 {args.cases} < 100，将被启用闸门拒绝（TR-1003），退出")
        return 1

    failures: list[str] = []
    try:
        service, bank, stats = build_service(args.cases)
        rows = run_drill_round(service, bank, args.sample)
        cal_rows = run_calibration(service, rows)
        failures = audit(service, bank, stats, rows, cal_rows, args.cases, args.sample)
    except Exception as exc:  # 冒烟链路任何异常即失败退出（不吞错）
        print(f"{TAG} 冒烟链路异常：{type(exc).__name__}: {exc}")
        return 1

    report = service.calibration_report()
    write_report(args.report, stats=stats, rows=rows, cal_rows=cal_rows, report=report, failures=failures)

    print(
        f"{TAG} 案例库 {stats['imported']} 条入库（脱敏闸门 0 拒绝，PII 负样本拒 {stats['pii_rejected']}）；"
        f"抽题 {args.sample} 例评分完成（优秀 5×100 / 差 5×0）"
    )
    print(
        f"{TAG} 校准 {report.samples} 组：一致 {report.consistent}、一致率 {report.consistency_rate}%"
        f"（≥85%：{'是' if report.consistency_rate is not None and report.consistency_rate >= 85 else '否'}），frozen={report.frozen}"
    )
    print(f"{TAG} 报告 → {args.report}")
    if failures:
        print(f"{TAG} 失败 {len(failures)} 项：")
        for msg in failures:
            print(f"  - {msg}")
        return 1
    print(f"{TAG} 退出证据判定：通过（案例库/脱敏闸门/评分/差距/幂等/校准回路全链自证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
