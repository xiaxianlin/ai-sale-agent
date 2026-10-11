# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""§16.2 Phase 1 KPI 预跑 CLI（迭代 1.3 收口：四项 KPI 的内存链路预跑口径）。

四项检查（SRS §16.2 Phase 1 验收 KPI 的 demo 预跑映射，口径声明见下）：
  ① AI 导师回答可溯源 100%：复用 tools/training/qa_smoke 冻结链路（import 其函数）——
     合成语料全链入库 → TutorPipeline 零编造三道闸 → 27 命中 + 3 零命中抽检，
     命中回答 100% 含有效 [来源N] 且对回锚点 chunk（可溯源判定）；
  ② 模拟客户结构逼真度：复用 sim_smoke 断言——10 类型状态演化/类型差异化/
     结束三分支/复盘/幂等全部通过（**正式口径为老销售盲测人评，本项为工程代理口径**）；
  ③ 评分一致性 ≥85%：复用 drill_smoke 校准回路——100 合成案例评分 + 10 组模拟
     主管校准采样，滚动一致率 ≥85% 且不冻结（**正式口径为销售主管真人对比采样**）；
  ④ 上岗拦截实测：FR-TR-007 require_certified 系统级强制——未认证 403(40301
     E_CERT_REQUIRED) → 考试通过授予 certified → 同请求放行 → 过期再拦截。

输出：中文 KPI 报告（Markdown，demo 口径声明置顶，默认 out/training/phase1_kpi_report.md）
+ 退出码 0（全绿）/ 1（任一不达标或运行失败）。

**demo 口径声明**：本预跑全部使用确定性合成语料/案例与 Fake LLM 桩（无真实甲方素材、
无 DeepSeek API Key、无真人评委），为工程自证证据。正式验收必须按 SRS §16.2 以
甲方真实素材、DeepSeek-V3 生产链路、老销售盲测与销售主管真人采样复测；正式盲测
人评占位（≥80% 逼真度）与成单率对比（需 ≥1 个真实业务月）不在本脚本可脚本化范围。

用法（仓库根）：
  python3 tools/training/phase1_kpi.py [--qa-files 30] [--qa-hit 27] [--qa-zero 3]
                                       [--report out/training/phase1_kpi_report.md]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 仓库根自举：直接以文件路径执行时补齐仓库根（tools 包）与 src（services 包）
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402

from tools.training import drill_smoke, qa_smoke, sim_smoke  # noqa: E402
from services.training.api.exam_api import (  # noqa: E402
    CERT_PATH,
    EXAM_START_PATH,
    EXAM_SUBMIT_PATH,
    PROTECTED_DEMO_PATH,
    create_exam_app,
)
from services.training.core.exam.paper import InMemoryQuestionBank, build_demo_questions  # noqa: E402
from services.training.core.exam.service import ExamService, InMemoryExamStore  # noqa: E402

TAG = "[Phase1 KPI]"

DEFAULT_REPORT = Path("out") / "training" / "phase1_kpi_report.md"
DEFAULT_QA_FILES = qa_smoke.DEFAULT_FILES
DEFAULT_QA_SEED = qa_smoke.DEFAULT_SEED
DEFAULT_QA_HIT = qa_smoke.DEFAULT_HIT_QUESTIONS
DEFAULT_QA_ZERO = qa_smoke.DEFAULT_ZERO_HITS

#: §16.2 KPI 判定阈值
TRACEABILITY_TARGET = 100.0  # ① 可溯源率 100%（qa_smoke 冻结口径）
REALISM_STRUCTURAL_ITEMS = 6  # ② 结构逼真度断言组数（目录/演化/差异化/三分支/复盘/幂等）
CONSISTENCY_TARGET = 85.0  # ③ 评分一致性 ≥85%（SRS §16.2）


# ── KPI ①：AI 导师回答可溯源（复用 qa_smoke 冻结链路）──────────────────────────


def check_qa_traceability(
    files: int, seed: int, hit_n: int, zero_n: int
) -> tuple[bool, dict[str, Any], list[str]]:
    """重跑 qa_smoke 链路：命中回答 100% 有效引用并对回 chunk；零命中走固定话术。"""
    failures: list[str] = []
    search_fn, chunk_meta, manifest = qa_smoke.build_knowledge_base(
        qa_smoke.DEFAULT_CORPUS_DIR, files, seed
    )
    entries = json.loads((qa_smoke.DEFAULT_CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
    hit_questions, _unusable = qa_smoke.build_questions(entries, manifest, chunk_meta, hit_n)
    if len(hit_questions) < hit_n:
        return False, {}, [f"语料可出题文件不足：{len(hit_questions)}/{hit_n}（可调大 --qa-files）"]

    pipeline = qa_smoke.TutorPipeline(
        search_fn=search_fn, llm=qa_smoke.FakeTutorLlm(),
        qa_store=qa_smoke.InMemoryQaStore(), gap_recorder=qa_smoke.KnowledgeGapRecorder(),
    )
    traceable = 0
    total = 0
    for question, anchor, kb_type in hit_questions:
        total += 1
        events = list(pipeline.ask(question, session="phase1-kpi"))
        _row, errors = qa_smoke.audit_hit_case(total, question, anchor, kb_type, events, chunk_meta)
        if errors:
            failures.extend(errors)
        else:
            traceable += 1
    zero_ok = 0
    for question in qa_smoke.ZERO_HIT_QUESTIONS[:zero_n]:
        total += 1
        events = list(pipeline.ask(question, session="phase1-kpi"))
        _row, errors = qa_smoke.audit_zero_case(total, question, events)
        if errors:
            failures.extend(errors)
        else:
            zero_ok += 1
    rate = round(traceable / hit_n * 100, 1) if hit_n else 0.0
    detail = {
        "hit_questions": hit_n,
        "traceable": traceable,
        "traceability_rate": rate,
        "zero_hit_fixed_reply": zero_ok,
        "corpus_chunks": manifest.indexed_chunks,
    }
    return rate >= TRACEABILITY_TARGET and not failures, detail, failures


# ── KPI ②：模拟客户结构逼真度（复用 sim_smoke 断言；正式盲测人评占位）──────────


def check_sim_realism() -> tuple[bool, dict[str, Any], list[str]]:
    """sim_smoke 结构逼真度口径：目录/演化/差异化/三分支/复盘/幂等六组断言全过。"""
    engine, registry = sim_smoke.build_engine()
    rows, failures = sim_smoke.run_catalog_round(engine, registry, 5)
    branches, branch_failures = sim_smoke.run_ending_branches(engine)
    failures.extend(branch_failures)
    _debrief, debrief_failures = sim_smoke.run_debrief_and_idempotency(engine)
    failures.extend(debrief_failures)
    detail = {
        "persona_types": len(rows),
        "branches": sorted(branches),
        "structural_items": REALISM_STRUCTURAL_ITEMS,
        "note": "正式口径为老销售盲测人评（≥3 名评委四维量表，SR-TR-002 §6.4）——demo 以结构逼真度替代",
    }
    return not failures, detail, failures


# ── KPI ③：评分一致性（复用 drill_smoke 校准回路）──────────────────────────────


def check_drill_consistency() -> tuple[bool, dict[str, Any], list[str]]:
    """drill 校准模拟：10 组采样一致率 ≥85% 且投题闸门未冻结。"""
    service, bank, stats = drill_smoke.build_service(drill_smoke.DEFAULT_CASES)
    rows = drill_smoke.run_drill_round(service, bank, drill_smoke.DEFAULT_SAMPLE)
    drill_smoke.run_calibration(service, rows)
    report = service.calibration_report()
    rate = report.consistency_rate or 0.0
    ok = rate >= CONSISTENCY_TARGET and not report.frozen
    detail = {
        "samples": report.samples,
        "consistent": report.consistent,
        "consistency_rate": rate,
        "threshold": report.threshold,
        "frozen": report.frozen,
        "cases_imported": stats["imported"],
        "note": "正式口径为销售主管真人盲评对比采样（SRS §16.2）——demo 为模拟校准回路",
    }
    return ok, detail, [] if ok else [f"评分一致性 {rate}% < {CONSISTENCY_TARGET}% 或已冻结"]


# ── KPI ④：上岗拦截实测（FR-TR-007 require_certified 系统级强制）──────────────


def check_cert_gate() -> tuple[bool, dict[str, Any], list[str]]:
    """拦截实测：未认证 403 → 全对考试授予 certified → 放行 → 过期再拦截。"""
    failures: list[str] = []
    bank = InMemoryQuestionBank(build_demo_questions())
    clock_start = datetime(2026, 10, 11, 9, 0, 0, tzinfo=timezone.utc)
    clock_now = {"now": clock_start}

    def clock() -> datetime:
        return clock_now["now"]

    service = ExamService(bank, InMemoryExamStore(), clock=clock)
    client = TestClient(create_exam_app(exam_service=service))
    trainee_id = 9001

    def protected() -> Any:
        return client.get(PROTECTED_DEMO_PATH, headers={"X-Trainee-Id": str(trainee_id)})

    # ① 未认证：403 + 40301 + E_CERT_REQUIRED
    blocked = protected()
    if not (blocked.status_code == 403 and blocked.json()["code"] == 40301
            and blocked.json()["data"]["biz_code"] == "E_CERT_REQUIRED"):
        failures.append(f"未认证拦截异常：HTTP {blocked.status_code} body={blocked.json()}")
    # ② 全对考试 → certified
    started = client.post(EXAM_START_PATH, json={"trainee_id": trainee_id})
    if started.status_code != 200:
        failures.append(f"开考失败：{started.text}")
        return False, {}, failures
    exam_uid = started.json()["data"]["exam_uid"]
    answers = {
        q.question_id: list(q.answer) if isinstance(q.answer, tuple) else q.answer
        for q in service.get_paper(exam_uid).questions
    }
    submitted = client.post(
        EXAM_SUBMIT_PATH, json={"exam_uid": exam_uid, "trainee_id": trainee_id, "answers": answers}
    )
    result = submitted.json().get("data", {})
    if result.get("passed") is not True or result.get("cert_status") != "certified":
        failures.append(f"考试通过后未授予认证：{result.get('passed')}/{result.get('cert_status')}")
    # ③ 认证后同请求放行
    granted = protected()
    if granted.status_code != 200 or granted.json()["data"].get("granted") is not True:
        failures.append(f"认证后放行异常：HTTP {granted.status_code}")
    # ④ 有效期届满 → expired 再拦截
    clock_now["now"] = clock_start + timedelta(days=366)
    if service.expire_sweep() != [trainee_id]:
        failures.append("有效期扫描未将学员置 expired")
    expired_blocked = protected()
    if not (expired_blocked.status_code == 403
            and expired_blocked.json()["data"].get("cert_status") == "expired"):
        failures.append(f"过期拦截异常：HTTP {expired_blocked.status_code} body={expired_blocked.json()}")
    # ⑤ 审计证据链：CERT_BLOCKED → CERT_GRANT → CERT_EXPIRE 全链留痕
    actions = [e["action"] for e in service.audit_log()]
    for required in ("CERT_BLOCKED", "CERT_GRANT", "CERT_EXPIRE"):
        if required not in actions:
            failures.append(f"审计缺少 {required} 留痕")
        elif actions.index(required) == 0 and required != "CERT_BLOCKED":
            failures.append(f"审计 {required} 应晚于 CERT_BLOCKED")
    cert_view = client.get(CERT_PATH.format(trainee_id=trainee_id)).json()["data"]
    detail = {
        "block_uncertified": "403/40301/E_CERT_REQUIRED",
        "grant_after_pass": result.get("cert_status"),
        "pass_through": granted.status_code,
        "block_expired": expired_blocked.json()["data"].get("cert_status"),
        "cert_status_final": cert_view["cert_status"],
        "audit_chain": " → ".join(sorted(set(actions))),
    }
    return not failures, detail, failures


# ── 报告 ──────────────────────────────────────────────────────────────────────


def write_report(
    report_path: Path,
    *,
    results: dict[str, tuple[bool, dict[str, Any], list[str]]],
) -> None:
    """写中文 KPI 报告（Markdown，demo 口径声明置顶 + §16.2 判定表）。"""
    order = ["qa_traceability", "sim_realism", "drill_consistency", "cert_gate"]
    labels = {
        "qa_traceability": "① AI 导师回答可溯源（KPI：回答准确率 ≥90% 的溯源口径）",
        "sim_realism": "② 模拟客户逼真度 ≥80%（demo：结构逼真度；正式：老销售盲测）",
        "drill_consistency": "③ 评分一致性 ≥85%（demo：模拟校准；正式：主管真人对比）",
        "cert_gate": "④ 上岗拦截实测（FR-TR-007 系统级强制，SRS §6.2/§16.2）",
    }
    all_ok = all(results[key][0] for key in order)
    lines: list[str] = []
    lines.append("# Phase 1 KPI 预跑报告（SRS §16.2，迭代 1.3 收口）")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}（UTC）")
    lines.append("- 需求依据：SRS §16.2 Phase 1 验收 KPI；SR-TR-007/008；开发规范 §9.1（TDD）")
    lines.append("")
    lines.append("> **demo 口径声明**：本预跑为工程自证证据——全部使用确定性合成语料/案例与")
    lines.append("> Fake LLM 桩（无真实甲方素材、无 DeepSeek API Key、无真人评委/主管）。SRS")
    lines.append("> §16.2 四项 KPI 的正式测量方式（导师抽检/老销售盲测/主管真人对比/真实业务月")
    lines.append("> 成单率对比）须在甲方真实素材与 DeepSeek-V3 生产链路下复测；本报告②的盲测")
    lines.append("> 人评与「培训周期缩短 ≥50%」「新人首月成单率」为**正式验收占位项**，不在")
    lines.append("> 脚本化范围内。")
    lines.append("")
    lines.append("## 一、§16.2 判定表")
    lines.append("")
    lines.append("| # | KPI 项 | 目标 | 预跑实测 | 结论 |")
    lines.append("|---|---|---|---|---|")
    qa = results["qa_traceability"][1]
    sim = results["sim_realism"][1]
    drill = results["drill_consistency"][1]
    gate = results["cert_gate"][1]
    qa_ok = results["qa_traceability"][0]
    sim_ok = results["sim_realism"][0]
    drill_ok = results["drill_consistency"][0]
    gate_ok = results["cert_gate"][0]
    lines.append(
        f"| ① | 导师回答可溯源 | 100% | {qa.get('traceable')}/{qa.get('hit_questions')} 命中回答有效引用并对回 chunk"
        f"（{qa.get('traceability_rate')}%）；零命中 {qa.get('zero_hit_fixed_reply')} 条固定话术 "
        f"| {'通过' if qa_ok else '未通过'} |"
    )
    lines.append(
        f"| ② | 模拟逼真度（结构口径） | 6 组断言全过 | {sim.get('persona_types')} 类型 × 5 轮演化/差异化、"
        f"结束三分支 {sim.get('branches')}、复盘/幂等通过 | {'通过' if sim_ok else '未通过'} |"
    )
    lines.append(
        f"| ③ | 评分一致性 | ≥85% | {drill.get('consistency_rate')}%"
        f"（{drill.get('consistent')}/{drill.get('samples')} 组一致，frozen={drill.get('frozen')}）"
        f"| {'通过' if drill_ok else '未通过'} |"
    )
    lines.append(
        f"| ④ | 上岗拦截实测 | 未认证/过期 403 | 未认证 {gate.get('block_uncertified')}；认证后 {gate.get('pass_through')} 放行；"
        f"过期 {gate.get('block_expired')} 再拦截；审计链 {gate.get('audit_chain')} "
        f"| {'通过' if gate_ok else '未通过'} |"
    )
    lines.append(f"| **总体判定** | — | — | — | **{'通过（exit 0）' if all_ok else '未通过（exit 1）'}** |")
    lines.append("")
    lines.append("## 二、口径映射说明（demo → 正式验收）")
    lines.append("")
    lines.append("| KPI（SRS §16.2） | demo 预跑口径 | 正式验收口径（占位说明） |")
    lines.append("|---|---|---|")
    lines.append(
        "| AI 导师回答准确率 ≥90% | 30 条抽检 100% 可溯源（qa_smoke 冻结链路重跑） "
        "| 双方联合抽检 ≥100 条、甲方真实素材、DeepSeek-V3 生产链路 |"
    )
    lines.append(
        "| 模拟客户逼真度 ≥80% | 结构逼真度 6 组断言（状态演化/差异化/三分支/复盘/幂等） "
        "| 老销售盲测人评（≥3 名评委、四维 1-5 分量表，sim_realism_eval 取证） |"
    )
    lines.append(
        "| 评分一致性 ≥85% | drill 模拟校准 10 组一致率 | 销售主管真人盲评 vs AI 对比采样（同 rubric 口径） |"
    )
    lines.append(
        "| 上岗拦截（认证强制） | protected-demo 403/40301 实测 + 审计链 "
        "| 同机制在生产业务域写操作上验证（Phase 2 接管） |"
    )
    lines.append("| 培训周期缩短 ≥50% / 首月成单率 | 不适用（需真实业务月数据） | 正式验收占位：合同口径的对比组统计 |")
    lines.append("")
    lines.append("## 三、各项明细")
    lines.append("")
    for key in order:
        ok, detail, failures = results[key]
        lines.append(f"### {labels[key]}——{'通过' if ok else '未通过'}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(detail, ensure_ascii=False, indent=2, sort_keys=True))
        lines.append("```")
        if failures:
            lines.append("")
            lines.append("失败项：")
            lines.extend(f"- {msg}" for msg in failures)
        lines.append("")
    if not all_ok:
        lines.append("## 失败清单")
        lines.append("")
        for key in order:
            for msg in results[key][2]:
                lines.append(f"- [{key}] {msg}")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """四项 KPI 顺序预跑：任一失败退出码 1，全绿退出码 0（报告必写）。"""
    parser = argparse.ArgumentParser(description="SRS §16.2 Phase 1 KPI 预跑（demo 口径）")
    parser.add_argument("--qa-files", type=int, default=DEFAULT_QA_FILES, help="QA 合成语料文件数")
    parser.add_argument("--qa-seed", type=int, default=DEFAULT_QA_SEED, help="确定性种子")
    parser.add_argument("--qa-hit", type=int, default=DEFAULT_QA_HIT, help="命中抽检题数")
    parser.add_argument("--qa-zero", type=int, default=DEFAULT_QA_ZERO, help="零命中题数")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="报告输出路径")
    args = parser.parse_args(argv)

    results: dict[str, tuple[bool, dict[str, Any], list[str]]] = {}

    print(f"{TAG} ① 导师可溯源抽检：重跑 qa_smoke 链路（{args.qa_files} 文件 / {args.qa_hit}+{args.qa_zero} 题）")
    try:
        results["qa_traceability"] = check_qa_traceability(
            args.qa_files, args.qa_seed, args.qa_hit, args.qa_zero
        )
    except Exception as exc:  # 预跑链路异常即失败（不吞错）
        results["qa_traceability"] = (False, {}, [f"链路异常：{type(exc).__name__}: {exc}"])
    rate = results["qa_traceability"][1].get("traceability_rate", "-")
    print(f"{TAG} ① 结论：{'通过' if results['qa_traceability'][0] else '未通过'}（{rate}%）")

    print(f"{TAG} ② 模拟逼真度（结构口径）：重跑 sim_smoke 断言")
    try:
        results["sim_realism"] = check_sim_realism()
    except Exception as exc:
        results["sim_realism"] = (False, {}, [f"链路异常：{type(exc).__name__}: {exc}"])
    print(f"{TAG} ② 结论：{'通过' if results['sim_realism'][0] else '未通过'}")

    print(f"{TAG} ③ 评分一致性：重跑 drill_smoke 校准回路")
    try:
        results["drill_consistency"] = check_drill_consistency()
    except Exception as exc:
        results["drill_consistency"] = (False, {}, [f"链路异常：{type(exc).__name__}: {exc}"])
    drill_rate = results["drill_consistency"][1].get("consistency_rate", "-")
    print(f"{TAG} ③ 结论：{'通过' if results['drill_consistency'][0] else '未通过'}（{drill_rate}%）")

    print(f"{TAG} ④ 上岗拦截实测：require_certified 三态 + 审计链")
    try:
        results["cert_gate"] = check_cert_gate()
    except Exception as exc:
        results["cert_gate"] = (False, {}, [f"链路异常：{type(exc).__name__}: {exc}"])
    print(f"{TAG} ④ 结论：{'通过' if results['cert_gate'][0] else '未通过'}")

    write_report(args.report, results=results)
    print(f"{TAG} 报告 → {args.report}")

    all_ok = all(v[0] for v in results.values())
    if not all_ok:
        for key, (_ok, _detail, failures) in results.items():
            for msg in failures:
                print(f"{TAG} 失败 [{key}] {msg}")
        print(f"{TAG} §16.2 判定：未通过（exit 1）")
        return 1
    print(f"{TAG} §16.2 判定：通过（可溯源 100% / 结构逼真 6 组 / 一致性 ≥85% / 拦截 403 实测，exit 0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
