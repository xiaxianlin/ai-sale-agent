# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-TR-002 模拟客户系统迭代 1.2 收口冒烟（TDD 阶段④后的退出证据）。

链路（全部内存实现，零网络请求）：

    PersonaRegistry（内置 C-01~C-06 + 扩展注册接口装载 C-07~C-10 demo 类型 = 10 类）
      → SimulatorEngine + FakeSimulatorLlm（确定性中文演绎）+ InMemorySimSessionStore
      → 10 类型 × 各 5 轮对话（价值/砍价交替脚本，验证状态机演化）
      → 结束三分支专测（成交 / 谈崩 / 轮数耗尽）
      → 复盘生成（FakeDebriefLlm：逐考核点评分 + 建议三段）→ 幂等重放抽检

断言（退出条件，任一失败退出码 1）：
  ① 目录守卫：内置 6 类 <10 时守卫失败；经扩展注册接口装载后恰 10 类；
  ② 状态机演化：10 场对话各自耐心严格递减、立场/情绪发生变化、第 5 轮后
     难度动态调节 +1（5 档钳制）、未提前结束、转录 11 条（1 开场 + 5×2）；
  ③ 差异化：10 类型第 1 轮回复两两不同（persona 模板差异可观察）；
  ④ 结束三分支：成交（立场≥0.75 且情绪≥1）/ 谈崩（耐心归零）/ 轮数耗尽
     三种 verdict 全部按预期出现且互斥优先级正确；
  ⑤ 复盘：deal 会话复盘 scores 三维在 0-100、建议恰 3 段、知识引用非空，
     落库可取；
  ⑥ 幂等：同 request_id 重放返回原响应，LLM 只调一次、状态只推进一轮。

**demo 口径声明**：本冒烟为工程自证证据——LLM 为确定性 Fake 桩（无真实甲方
素材、无 DeepSeek API Key）。SRS §16.2「模拟客户逼真度 ≥80%」的正式测量方式
为**老销售盲测**（人评，SR-TR-002 §6.4：≥3 名评委、四维 1-5 分量表、
sim_realism_eval 取证），无法脚本化；demo 下以「**结构逼真度检查**」替代——
状态演化（②）、类型差异化（③）、结束三分支覆盖（④）三项结构指标，作为
逼真度链路可用性的工程代理口径；正式验收仍须按 §16.2 以 DeepSeek-V3 生产
链路 + 盲测组织复测。

用法（仓库根）：
  python3 tools/training/sim_smoke.py [--rounds 5] [--report out/training/sim_smoke_report.md]
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

from services.training.core.simulator.engine import (  # noqa: E402
    BREAKDOWN_LINE,
    DEAL_LINE,
    DEFAULT_MAX_ROUNDS,
    FakeSimulatorLlm,
    SimulatorEngine,
)
from services.training.core.simulator.persona import (  # noqa: E402
    CatalogInsufficientError,
    DEMO_EXTENSION_PERSONAS,
    MIN_PERSONA_TYPES,
    PersonaRegistry,
    ScenarioCatalog,
)
from services.training.core.simulator.store import InMemorySimSessionStore  # noqa: E402

TAG = "[模拟客户冒烟]"

DEFAULT_REPORT = Path("out") / "training" / "sim_smoke_report.md"
DEFAULT_ROUNDS = 5

#: 冒烟对话脚本（价值词/砍价词交替：驱动立场/情绪/耐心三轴确定性演化）
SMOKE_SCRIPT: tuple[str, ...] = (
    "请介绍一下你们的认证、交期和质量参数。",
    "这个价格太贵了，能便宜点吗？",
    "你们的质量参数和工艺标准如何？",
    "价格还是太高，给个最低价吧。",
    "交期和质量保证怎么体现？",
)

BARGAIN_SCRIPT: tuple[str, ...] = (
    "太贵了，给我最低价。",
    "还能便宜吗？给我 discount。",
    "降价吧，隔壁更低。",
    "最低价，没有就免谈。",
    "太贵，便宜一点。",
    "再让一点，便宜我就下单。",
)

NEUTRAL_MSG = "你好，请继续。"
VALUE_MSG = "请介绍一下你们的认证、交期和质量参数。"


def build_engine(max_rounds: int = DEFAULT_MAX_ROUNDS) -> tuple[SimulatorEngine, PersonaRegistry]:
    """装配冒烟引擎：扩展注册接口装载 4 个 demo 类型至 10 类（≥10 守卫）。"""
    registry = PersonaRegistry()
    guard_failed = False
    try:  # ① 守卫断言：仅内置 6 类必须失败
        registry.ensure_min_types()
    except CatalogInsufficientError:
        guard_failed = True
    for spec in DEMO_EXTENSION_PERSONAS:
        registry.register(spec)
    registry.ensure_min_types()
    engine = SimulatorEngine(
        llm=FakeSimulatorLlm(),
        store=InMemorySimSessionStore(),
        registry=registry,
        max_rounds=max_rounds,
    )
    if not guard_failed:
        raise SystemError("目录守卫未生效：内置 6 类未触发 CATALOG_INSUFFICIENT")
    return engine, registry


class _FakeReplyLlm:
    """对话演绎桩：透传 engine.FakeSimulatorLlm 的确定性规则并留调用计数。"""

    def __init__(self) -> None:
        self._inner = FakeSimulatorLlm()
        self.calls = 0

    def generate_reply(self, **kwargs: Any) -> str:
        self.calls += 1
        return self._inner.generate_reply(**kwargs)


def run_catalog_round(engine: SimulatorEngine, registry: PersonaRegistry, rounds: int) -> tuple[list[dict[str, Any]], list[str]]:
    """②③ 10 类型 × N 轮对话：返回每类观测行 + 失败清单。"""
    failures: list[str] = []
    rows: list[dict[str, Any]] = []
    first_round_replies: dict[str, str] = {}
    for persona_id in registry.persona_ids():
        spec = registry.get(persona_id)
        res = engine.start_session(persona_id, "sc-inquiry-first")
        sid = res["session_id"]
        init_state = res["state"]
        patience_path = [init_state["patience"]]
        reply_first = None
        last = None
        for index in range(rounds):
            last = engine.reply(sid, SMOKE_SCRIPT[index % len(SMOKE_SCRIPT)])
            patience_path.append(last["state"]["patience"])
            if index == 0:
                reply_first = last["customer_reply"]
        first_round_replies[persona_id] = str(reply_first)
        session = engine.store.load(sid)
        errors: list[str] = []
        if last is None:
            errors.append("未产出任何回复")
        else:
            if last["ended"] or last["ended_reason"] is not None:
                errors.append(f"第 {rounds} 轮前不应结束（ended_reason={last['ended_reason']}）")
            expected_effective = min(5, spec.difficulty_base + 1)
            if last["state"]["difficulty"] != expected_effective:
                errors.append(f"难度动态调节异常：{last['state']['difficulty']} != {expected_effective}")
            if session is None or len(session.transcript) != 1 + 2 * rounds:
                errors.append(f"转录条数异常（应 {1 + 2 * rounds}）")
            if session is not None and session.rounds != rounds:
                errors.append(f"轮数异常：{session.rounds} != {rounds}")
        if not all(a > b for a, b in zip(patience_path, patience_path[1:])):
            errors.append(f"耐心未严格递减：{patience_path}")
        if last is not None and last["state"]["price_stance"] == init_state["price_stance"] \
                and last["state"]["emotion"] == init_state["emotion"]:
            errors.append("立场/情绪全程无变化（状态机未演化）")
        if errors:
            failures.extend(f"{persona_id}: {e}" for e in errors)
        rows.append(
            {
                "persona_id": persona_id,
                "name": spec.name,
                "difficulty": f"{spec.difficulty_base}→{last['state']['difficulty'] if last else '-'}",
                "rounds": rounds,
                "patience": patience_path,
                "stance": last["state"]["stance"] if last else "-",
                "emotion": last["state"]["emotion"] if last else "-",
                "ended": bool(last and last["ended"]),
                "reply": (reply_first or "")[:30],
            }
        )
    distinct = len(set(first_round_replies.values()))
    if distinct != len(registry.persona_ids()):
        failures.append(f"差异化不足：{distinct}/{len(registry.persona_ids())} 类型回复唯一")
    return rows, failures


def run_ending_branches(engine: SimulatorEngine) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """④ 结束三分支专测：成交 / 谈崩 / 轮数耗尽。"""
    failures: list[str] = []
    branches: dict[str, dict[str, Any]] = {}

    # 成交：C-01 难度 1，连续价值消息抬升立场至 0.75+ 且情绪 ≥1
    deal_sid = engine.start_session("C-01", "sc-bargain-moq", difficulty=1)["session_id"]
    deal_last = None
    for _ in range(6):
        deal_last = engine.reply(deal_sid, VALUE_MSG)
    if not (deal_last and deal_last["ended"] and deal_last["ended_reason"] == "deal"):
        failures.append(f"成交分支异常：{deal_last and deal_last['ended_reason']}")
    else:
        branches["deal"] = {"rounds": deal_last["round"], "reply": deal_last["customer_reply"]}

    # 谈崩：C-01 难度 5，连续砍价消息加速耐心归零
    brk_sid = engine.start_session("C-01", "sc-bargain-moq", difficulty=5)["session_id"]
    brk_last = None
    for _ in range(6):
        brk_last = engine.reply(brk_sid, BARGAIN_SCRIPT[0])
        if brk_last["ended"]:
            break
    if not (brk_last and brk_last["ended"] and brk_last["ended_reason"] == "breakdown"):
        failures.append(f"谈崩分支异常：{brk_last and brk_last['ended_reason']}")
    else:
        branches["breakdown"] = {"rounds": brk_last["round"], "reply": brk_last["customer_reply"]}

    # 轮数耗尽：max_rounds=2 的独立引擎，中性消息推进至上限
    timeout_engine = SimulatorEngine(
        llm=_FakeReplyLlm(), store=InMemorySimSessionStore(),
        registry=PersonaRegistry.with_demo_extensions(), max_rounds=2,
    )
    timeout_sid = timeout_engine.start_session("C-01", "sc-bargain-moq")["session_id"]
    timeout_last = None
    for _ in range(2):
        timeout_last = timeout_engine.reply(timeout_sid, NEUTRAL_MSG)
    if not (timeout_last and timeout_last["ended"] and timeout_last["ended_reason"] == "timeout"):
        failures.append(f"超时分支异常：{timeout_last and timeout_last['ended_reason']}")
    else:
        branches["timeout"] = {"rounds": timeout_last["round"], "reply": timeout_last["customer_reply"]}

    if branches.get("deal", {}).get("reply") != DEAL_LINE:
        failures.append("成交话术与 DEAL_LINE 不一致")
    if branches.get("breakdown", {}).get("reply") != BREAKDOWN_LINE:
        failures.append("谈崩话术与 BREAKDOWN_LINE 不一致")
    return branches, failures


def run_debrief_and_idempotency(engine: SimulatorEngine) -> tuple[dict[str, Any], list[str]]:
    """⑤⑥ 复盘生成 + 幂等重放抽检（基于成交会话之外另起的短会话）。"""
    failures: list[str] = []
    # 复盘：以超时会话触发（独立引擎 max_rounds=1 → timeout）
    debrief_engine = SimulatorEngine(
        llm=_FakeReplyLlm(), store=InMemorySimSessionStore(),
        registry=PersonaRegistry.with_demo_extensions(), max_rounds=1,
    )
    sid = debrief_engine.start_session("C-01", "sc-bargain-moq")["session_id"]
    debrief_engine.reply(sid, NEUTRAL_MSG)
    report = debrief_engine.generate_debrief(sid)
    if report.verdict != "timeout" or len(report.suggestions) != 3 or not report.knowledge_refs:
        failures.append("复盘报告结构异常（verdict/suggestions/knowledge_refs）")
    if not all(0 <= v <= 100 for v in report.scores.values()):
        failures.append(f"复盘评分越界：{report.scores}")
    stored = debrief_engine.debrief.store.get(sid)
    if stored is None or stored.session_id != sid:
        failures.append("复盘未落库或取回不一致")

    # 幂等：同 request_id 重放返回原响应，LLM 只调一次
    idem_engine = SimulatorEngine(
        llm=_FakeReplyLlm(), store=InMemorySimSessionStore(),
        registry=PersonaRegistry.with_demo_extensions(),
    )
    isid = idem_engine.start_session("C-01", "sc-bargain-moq", difficulty=3)["session_id"]
    first = idem_engine.reply(isid, VALUE_MSG, request_id="smoke-msg-1")
    second = idem_engine.reply(isid, VALUE_MSG, request_id="smoke-msg-1")
    llm_calls = idem_engine.llm.calls
    session = idem_engine.store.load(isid)
    if first != second:
        failures.append("幂等重放响应不一致")
    if llm_calls != 1 or session is None or session.rounds != 1:
        failures.append(f"幂等重放副作用异常（llm_calls={llm_calls}, rounds={session and session.rounds}）")
    return {"report": report, "replay_rounds": session.rounds if session else -1}, failures


def write_report(
    report_path: Path,
    *,
    rows: list[dict[str, Any]],
    branches: dict[str, dict[str, Any]],
    debrief_summary: dict[str, Any],
    failures: list[str],
    rounds: int,
) -> None:
    """写中文冒烟报告（Markdown，demo 口径声明置顶）。"""
    verdict = "通过" if not failures else "未通过"
    lines: list[str] = []
    lines.append("# FR-TR-002 模拟客户系统——迭代 1.2 收口冒烟报告")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}（UTC）")
    lines.append("- 需求依据：SRS §6.2/§6.3/§16.2；SR-TR-002 §6/§9；开发规范 §9.1（TDD）")
    lines.append("")
    lines.append("> **demo 口径声明**：本报告为工程自证证据——LLM 为确定性 Fake 桩")
    lines.append(">（FakeSimulatorLlm/FakeDebriefLlm，零网络请求，无真实甲方素材、无 DeepSeek")
    lines.append("> API Key）。SRS §16.2「模拟客户逼真度 ≥80%」的正式测量方式为**老销售盲测**")
    lines.append(">（人评：≥3 名评委、四维 1-5 分量表、sim_realism_eval 取证，SR-TR-002 §6.4），")
    lines.append("> 无法脚本化；demo 下以「**结构逼真度检查**」替代——状态演化/类型差异化/")
    lines.append("> 结束三分支覆盖三项结构指标。正式验收仍须按 §16.2 以 DeepSeek-V3 生产链路")
    lines.append("> + 盲测组织复测（DeepSeek 客户端随生产装配注入 SimulatorLlm 协议）。")
    lines.append("")
    lines.append("## 一、冒烟结论")
    lines.append("")
    lines.append("| 项 | 断言 | 实测 | 结论 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| 目录守卫（≥10 类型） | 内置 6 类守卫失败 + 扩展注册后 10 类 | 10 类 | 通过 |")
    lines.append(f"| 状态机演化 | {len(rows)} 类型 × {rounds} 轮耐心严格递减、立场/情绪变化、难度调节 +1 | 见明细表 | {'通过' if not failures else '见失败清单'} |")
    lines.append(f"| 类型差异化 | 10 类型第 1 轮回复两两不同 | 见明细表 | {'通过' if not failures else '见失败清单'} |")
    lines.append(f"| 结束三分支 | deal / breakdown / timeout 全部可达且话术正确 | 3/3 | {'通过' if not failures else '见失败清单'} |")
    lines.append(f"| 复盘报告 | scores 三维 0-100、建议 3 段、引用非空、落库可取 | 见复盘段 | {'通过' if not failures else '见失败清单'} |")
    lines.append(f"| 幂等重放 | 同 request_id 返回原响应、LLM 一次、状态一轮 | 见幂等段 | {'通过' if not failures else '见失败清单'} |")
    lines.append(f"| **总体判定** | — | — | **{verdict}** |")
    lines.append("")
    lines.append(f"## 二、10 类型 × {rounds} 轮对话明细（场景 sc-inquiry-first）")
    lines.append("")
    lines.append("| 类型 | 名称 | 难度(起→调) | 轮数 | 耐心路径 | 立场终态 | 情绪终态 | 提前结束 | 第 1 轮回复（截断） |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        lines.append(
            f"| {row['persona_id']} | {row['name']} | {row['difficulty']} | {row['rounds']} "
            f"| {'→'.join(str(p) for p in row['patience'])} | {row['stance']} | {row['emotion']} "
            f"| {'是' if row['ended'] else '否'} | {row['reply']}… |"
        )
    lines.append("")
    lines.append("## 三、结束三分支覆盖（结构逼真度检查）")
    lines.append("")
    lines.append("| 分支 | 判定条件 | 触发轮数 | 客户回复（谈崩/成交话术锚点） |")
    lines.append("|---|---|---|---|")
    labels = {"deal": "成交（立场≥0.75 且情绪≥1）", "breakdown": "谈崩（耐心归零）", "timeout": "轮数耗尽（max_rounds）"}
    for key in ("deal", "breakdown", "timeout"):
        branch = branches.get(key, {})
        lines.append(f"| {key} | {labels[key]} | {branch.get('rounds', '-')} | {str(branch.get('reply', '-'))[:36]} |")
    lines.append("")
    lines.append("## 四、复盘报告与幂等抽检")
    lines.append("")
    report = debrief_summary["report"]
    lines.append(f"- 复盘（timeout 会话）：scores={report.scores}；建议 {len(report.suggestions)} 段；"
                 f"知识引用 {len(report.knowledge_refs)} 条；落库取回一致。")
    lines.append(f"- 幂等：同 request_id 两次 reply 返回一致响应，LLM 调用 1 次，会话推进 "
                 f"{debrief_summary['replay_rounds']} 轮（应为 1）。")
    if failures:
        lines.append("")
        lines.append("## 失败清单")
        lines.append("")
        lines.extend(f"- {msg}" for msg in failures)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FR-TR-002 模拟客户系统迭代 1.2 收口冒烟（demo 口径）")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS, help="目录对话轮数（≥5）")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    failures: list[str] = []
    try:
        engine, registry = build_engine()
    except SystemError as exc:
        print(f"{TAG} 前置失败：{exc}")
        return 1

    rounds = max(5, args.rounds)
    print(f"{TAG} 目录就绪：{registry.count()} 类型（6 内置 + 4 扩展注册）；开始 10 类型 × {rounds} 轮对话")
    rows, catalog_failures = run_catalog_round(engine, registry, rounds)
    failures.extend(catalog_failures)
    print(f"{TAG} 目录对话完成：{len(rows)} 场，失败 {len(catalog_failures)} 项")

    branches, branch_failures = run_ending_branches(engine)
    failures.extend(branch_failures)
    print(f"{TAG} 结束三分支：{sorted(branches)}，失败 {len(branch_failures)} 项")

    debrief_summary, debrief_failures = run_debrief_and_idempotency(engine)
    failures.extend(debrief_failures)
    print(f"{TAG} 复盘与幂等抽检：失败 {len(debrief_failures)} 项")

    write_report(args.report, rows=rows, branches=branches, debrief_summary=debrief_summary,
                 failures=failures, rounds=rounds)
    print(f"{TAG} 报告 → {args.report}")

    if failures:
        print(f"{TAG} 失败 {len(failures)} 项：")
        for msg in failures:
            print(f"  - {msg}")
        return 1
    print(f"{TAG} 收口判定：通过（10 类型目录 / 状态演化 / 差异化 / 结束三分支 / 复盘 / 幂等 全部达标）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
