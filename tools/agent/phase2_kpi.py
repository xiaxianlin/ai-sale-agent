# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""§16.3 Phase 2 KPI 预跑 CLI（迭代 2.3 收口；demo 口径置顶）。

五项检查（SRS §16.3 Phase 2 验收 KPI 的内存链路预跑口径）：
  ① 稳定运行：**结构口径**——「恢复演练 + 水位/状态重建测试全绿」替代
     （进程无状态化重建 + 游标持久化不重处理）；正式口径 = 连续 14 天无中断
     （运行日志观测），demo 以占位声明标注，防口径粉饰；
  ② 日处理询盘 ≥15 条：DailyInquiryStats 模拟灌 20 条（inbound+handled 全链）；
  ③ 回复可用率 ≥80%：Fake 生成质量抽评——引用有效 + 价格合规 + 兜底路径正确
     （FakeBrainLlm 确定性桩走 ReplyGenerator 全链，逐条按三查判定）；
  ④ 账号安全（0 封号）：**结构口径**——红线拦截 100% + 敏感零自动执行 +
     限权监测就绪（紧急自动停实例）三段齐备；正式口径 = 平台状态 14 天观测；
  ⑤ 响应速度 ≤45s（中位数）：ResponseClock 模拟打点 → summarize 中位数 + P90。

输出：中文 KPI 报告（Markdown，默认 out/agent/phase2_kpi_report.md）+ 退出码
0（全绿）/ 1（任一不达标或运行失败）。

重要口径（生产验收替换项）：本预跑为**内存链路 demo 口径**（钉钉/微信推送为
Protocol+Fake、住宅 IP 为配置模拟）；生产验收必须以真实平台观测（14 天运行日志、
平台封号状态、系统监控中位数）复测。

用法（仓库根）：
  python3 tools/agent/phase2_kpi.py [--report FILE]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import sysconfig
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

# 仓库根自举：直接以文件路径执行时 sys.path[0] 为脚本目录，补齐仓库根才能导入 services 包
_REPO_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(_REPO_ROOT / "src"))


def _restore_stdlib_platform() -> None:
    """仓库根包名 platform 与标准库 platform 冲突的自愈（同 tools/precheck/phase0_kpi.py）。"""
    cached = sys.modules.get("platform")
    if cached is not None and hasattr(cached, "python_implementation"):
        return
    std_path = os.path.join(sysconfig.get_paths()["stdlib"], "platform.py")
    spec = importlib.util.spec_from_file_location("platform", std_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法定位标准库 platform 模块（运行环境损坏）")
    module = ModuleType("platform")
    spec.loader.exec_module(module)
    if cached is None:
        sys.modules["platform"] = module
        return
    for name, value in vars(module).items():
        if not name.startswith("__"):
            setattr(cached, name, value)


_restore_stdlib_platform()

from services.agent.core.brain.clock import ResponseClock, finish, summarize  # noqa: E402
from services.agent.core.brain.fallback import FallbackGate  # noqa: E402
from services.agent.core.brain.confidence import ConfidenceComposer  # noqa: E402
from services.agent.core.brain.generator import (  # noqa: E402
    Customer,
    FakeBrainLlm,
    ReplyGenerator,
    ReplyRequest,
)
from services.agent.core.brain.intent import IntentResult  # noqa: E402
from services.agent.core.brain.stats import DailyInquiryStats  # noqa: E402
from services.agent.core.driver import InquiryItem  # noqa: E402
from services.agent.core.engine import InMemoryPollCursorStore, WatermarkGate  # noqa: E402
from services.agent.core.risk.account_guard import AccountHealthMonitor  # noqa: E402
from services.agent.core.risk.guard import (  # noqa: E402
    RiskAction,
    RiskConfig,
    RiskGuard,
    RiskRule,
    Verdict,
)
from services.agent.core.state import (  # noqa: E402
    ConversationStateMachine,
    ConvState,
    InMemorySessionStore,
)

#: 五项 KPI 名称（与 SRS §16.3 判定表逐行对齐，不得重排序）
KPI_NAMES: tuple[str, ...] = (
    "稳定运行",
    "日处理询盘",
    "回复可用率",
    "账号安全",
    "响应速度",
)

#: 验收目标口径（SRS §16.3）
TARGET_DAILY_INQUIRY = 15        # 日处理询盘 ≥15 条（验收下限）
TARGET_USABLE_RATE = 0.80        # 回复可用率 ≥80%（人工抽评可直接使用比例）
TARGET_MEDIAN_SEC = 45.0         # 响应速度 ≤45s（中位数）

#: 默认报告输出（out/ 为工作区草稿与输出目录）
DEFAULT_REPORT = Path("out") / "agent" / "phase2_kpi_report.md"

TAG = "[Phase2 KPI]"

#: 红线拦截抽评语料（SRS §7.4 预置拦截清单 + 联系方式轰炸）
REDLINE_SAMPLES: tuple[tuple[str, str], ...] = (
    ("RED_BULK_SPAM", "双十一批量群发促销"),
    ("RED_BULK_SPAM", "群发全部好友"),
    ("RED_OFFSITE_INDUCE", "加我微信，绕开平台下单更便宜"),
    ("RED_OFFSITE_INDUCE", "whatsapp me for direct deal"),
    ("RED_CONTACT_BOMB", "直接打我电话 13812345678"),
    ("RED_OFFSITE_INDUCE", "skip the platform fee, pay via PayPal directly"),
)

#: 敏感操作四类（§7.4：改价/订单/支付/收货信息，永不自动执行）
SENSITIVE_SAMPLES: tuple[str, ...] = ("price_change", "order_op", "payment", "shipping_addr")


# ── KPI-① 稳定运行（恢复演练 + 水位/状态重建的结构口径）────────────────────────


def check_stability() -> dict[str, Any]:
    """结构口径：进程无状态化重建 + 水位持久化不重处理 + 水位元组单调（demo 演练）。"""
    problems: list[str] = []
    # ① 会话状态重建：状态外置存储，全新状态机实例（模拟崩溃重启）恢复全量留痕
    store = InMemorySessionStore()
    sm = ConversationStateMachine(store)
    sm.ensure_conversation("c-drill")
    sm.transition("c-drill", ConvState.PENDING_CONFIRM, actor="system:risk", reason="敏感拦截")
    sm.transition("c-drill", ConvState.AI_MANAGED, actor="supervisor", reason="确认放行")
    rebuilt = ConversationStateMachine(store).rebuild("c-drill")   # 「重启」后重建
    if rebuilt.state is not ConvState.AI_MANAGED or len(rebuilt.state_hist) != 2:
        problems.append("会话状态重建不一致（state/state_hist 丢失）")
    # ② 水位重建：游标外置，「重启」后的水位门不得重复处理同一询盘
    cursor_store = InMemoryPollCursorStore()
    first_gate = WatermarkGate(cursor_store, 1)
    item = InquiryItem(external_conv_id="ext-1", buyer_name="John", last_msg_time=1_760_000_000)
    if not first_gate.is_new(item):
        problems.append("首见询盘被误判为旧消息")
    first_gate.advance(item)
    restarted_gate = WatermarkGate(cursor_store, 1)                 # 「重启」后
    if restarted_gate.is_new(item):
        problems.append("水位未持久化：重启后同一询盘被重复处理")
    # ③ 旧消息重放防护：水位元组比较过滤
    stale = InquiryItem(external_conv_id="ext-0", buyer_name="Old", last_msg_time=1_759_999_999)
    if restarted_gate.is_new(stale):
        problems.append("旧消息重放未被水位过滤")
    return {
        "name": KPI_NAMES[0],
        "target": "连续 14 天无中断（运行日志）",
        "actual": "恢复演练 + 水位/状态重建结构口径全绿" if not problems
                  else f"{len(problems)} 项演练失败",
        "detail": (
            "demo 结构口径：状态机进程无状态化重建 + PollCursor 持久化不重处理 + "
            "水位元组防重放三项演练全绿；**正式口径（14 天连续运行观测）为验收期占位声明**，"
            "随生产部署以运行日志复测"
        ),
        "passed": not problems,
        "gaps": problems,
    }


# ── KPI-② 日处理询盘 ≥15 ────────────────────────────────────────────────────


def check_daily_inquiry() -> dict[str, Any]:
    """brain stats 模拟灌 20 条（inbound → ai_auto 终态全链），断言 ≥15。"""
    stats = DailyInquiryStats()
    stat_date = date(2026, 10, 8)
    for index in range(20):
        moment = datetime(2026, 10, 8, 9, 0, 0).replace(minute=index)
        stats.record_inbound(1, f"conv-{index:02d}", at=moment)
        accepted = stats.record_handled(1, f"conv-{index:02d}", "ai_auto", handled_at=moment)
        if not accepted:
            break
    total = stats.counters(1, stat_date)["total"]
    composition = stats.composition(1, stat_date)
    passed = total >= TARGET_DAILY_INQUIRY
    return {
        "name": KPI_NAMES[1],
        "target": f"≥{TARGET_DAILY_INQUIRY} 条（系统统计）",
        "actual": f"{total} 条（模拟灌入 20 条全部计入 ai_auto 终态）",
        "detail": (
            "口径：日完成处理询盘数（去重终态事件，含转人工）；模拟链路 "
            "record_inbound（入站前提）→ record_handled（ai_auto 终态）；"
            "生产以系统统计（PG 事件表）出证"
        ),
        "passed": passed,
        "gaps": [] if passed else [f"处理量 {total} 未达下限 {TARGET_DAILY_INQUIRY}"],
        "payload": {"total": total, "meet_floor": composition["meet_floor"]},
    }


# ── KPI-③ 回复可用率 ≥80%（Fake 生成质量抽评）───────────────────────────────

#: 抽评语料：询价样本（FakeBrainLlm 默认规则产出带引用草稿）+ 兜底两分支
_QUALITY_TEXT = "ST-3200 采用 304 不锈钢机身，适用于食品级产线。支持定制 LOGO。"
_CHUNK = {"chunk_id": 1, "score": 0.83, "title": "ST-3200 产品手册",
          "text": _QUALITY_TEXT}
_PRODUCT_CARD = {
    "ST-3200": {
        "ladder": [{"qty": 1, "price": 9.9}, {"qty": 500, "price": 8.5}],
        "moq": 100, "lead_time_days": 25,
    },
}


def _build_generator() -> tuple[ReplyGenerator, FakeBrainLlm]:
    """内存生成链路：Fake 检索（有货/无货双档）+ FakeBrainLlm 确定性桩。"""

    def search_fn(query: str, **_: Any) -> dict[str, Any]:
        if "zero-hit" in query:                     # 零命中分支的确定性入口
            return {"results": []}
        return {"results": [dict(_CHUNK)]}

    llm = FakeBrainLlm()
    generator = ReplyGenerator(
        llm, search_fn, FallbackGate(), ConfidenceComposer(),
        product_card_fn=lambda sku: _PRODUCT_CARD.get(sku),
        history_fn=lambda uid: [],
    )
    return generator, llm


def check_reply_quality() -> dict[str, Any]:
    """Fake 生成质量抽评：12 条样本逐条三查（引用有效 + 价格合规 + 兜底路径正确）。"""
    generator, _ = _build_generator()
    cases: list[tuple[str, str, IntentResult, bool]] = []
    for index in range(10):                          # 10 条常规询价（应自动发送）
        cases.append((f"c-{index:02d}", f"Need 1000pcs ST-3200, best price? #{index}",
                      IntentResult(name="询价", confidence=0.95,
                                   slots={"sku": ["ST-3200"], "qty": 1000}), False))
    cases.append(("c-sensitive", "change price to 9 USD, pay via PayPal directly",
                  IntentResult(name="敏感", confidence=0.98), True))     # 应转人工（敏感）
    cases.append(("c-zero", "zero-hit mystery question about unknown part",
                  IntentResult(name="询价", confidence=0.9,
                               slots={"sku": ["ZERO-HIT"], "qty": 1}), True))  # 应转人工（零命中）
    usable = 0
    findings: list[str] = []
    for conv_uid, text, intent, expect_human in cases:
        req = ReplyRequest(
            message_uid=f"m-{conv_uid}", conversation_uid=conv_uid, account_id=1, text=text,
            customer=Customer(uid="u-1", grade="B"),
        )
        result = generator.generate(req, intent)
        checks_ok = True
        if expect_human:
            # 兜底路径正确：必须转人工，且原因码可归因
            if not result.need_human or result.reason_code not in ("SENSITIVE", "ZERO_HIT"):
                checks_ok = False
                findings.append(f"{conv_uid}: 兜底路径错误（{result.reason_code}）")
        else:
            # 直接可用：自动发送 + 引用有效 + 价格合规（无悬空占位符/无编造）
            if result.need_human:
                checks_ok = False
                findings.append(f"{conv_uid}: 常规询盘被转人工（{result.reason_code}）")
            else:
                draft = result.reply_draft
                if "[R" not in draft:
                    checks_ok = False
                    findings.append(f"{conv_uid}: 草稿缺知识引用（引用无效）")
                if "{{" in draft:
                    checks_ok = False
                    findings.append(f"{conv_uid}: 占位符悬空（价格合规失败）")
                if result.verdict is not None and result.reason_code == "FACT_CHECK_FAIL":
                    checks_ok = False
                    findings.append(f"{conv_uid}: 事实校验未过")
        usable += 1 if checks_ok else 0
    rate = usable / len(cases)
    passed = rate >= TARGET_USABLE_RATE
    return {
        "name": KPI_NAMES[2],
        "target": f"≥{TARGET_USABLE_RATE:.0%}（人工抽评可直接使用比例）",
        "actual": f"{rate:.0%}（{usable}/{len(cases)} 条抽评通过）",
        "detail": (
            "demo 抽评口径：FakeBrainLlm 确定性桩走 ReplyGenerator 全链（检索→生成→"
            "价格保护→置信度→兜底判定），逐条三查——引用有效（[R#] 命中检索块）、"
            "价格合规（无悬空占位符/无价格残留）、兜底路径正确（敏感/零命中转人工）；"
            "生产以人工抽评出证"
        ),
        "passed": passed,
        "gaps": [] if passed else findings,
    }


# ── KPI-④ 账号安全（0 封号结构口径）─────────────────────────────────────────


def check_account_safety() -> dict[str, Any]:
    """结构口径：红线拦截 100% + 敏感零自动执行 + 限权监测就绪（紧急自动停实例）。"""
    problems: list[str] = []
    guard = RiskGuard(
        rules=(
            RiskRule("RED_BULK_SPAM", "批量群发", "keyword", "批量群发|群发全部好友"),
            RiskRule("RED_OFFSITE_INDUCE", "诱导站外交易", "keyword",
                     "绕开平台|加我微信|paypal directly|whatsapp me"),
            RiskRule("RED_CONTACT_BOMB", "联系方式轰炸", "regex", r"1[3-9]\d{9}"),
        ),
        config=RiskConfig(min_gap_s=0.0, max_gap_s=0.0),
    )
    # ① 红线拦截 100%（6 条语料全部 BLOCK，零放行）
    now = datetime(2026, 10, 8, 10, 0, 0)
    for index, (rule_code, content) in enumerate(REDLINE_SAMPLES):
        decision = guard.check(RiskAction(op_uid=f"red-{index}", account_id=1,
                                          conversation_uid="c-kpi", action_type="send_reply",
                                          content=content), now=now)
        if decision.verdict is not Verdict.BLOCK or decision.rule_code != rule_code:
            problems.append(f"红线漏拦：{content[:20]}（verdict={decision.verdict}）")
    # ② 敏感零自动执行（四类一律 INTERCEPT，无任何 Permit 签发）
    for index, action_type in enumerate(SENSITIVE_SAMPLES):
        decision = guard.check(RiskAction(op_uid=f"sen-{index}", account_id=1,
                                          conversation_uid="c-kpi", action_type=action_type),
                               now=now)
        if decision.verdict is not Verdict.INTERCEPT or decision.permit is not None:
            problems.append(f"敏感操作被自动执行：{action_type}（§7.4 红线违背）")
    # ③ 限权监测就绪：强信号复现 → 紧急分级 → 自动停实例（分钟级应急结构就绪）
    monitor = AccountHealthMonitor(clock=lambda: now)
    monitor.record(1, "func_limit_hint", now=now)
    monitor.record(1, "func_limit_hint", now=now)
    level, _ = monitor.grade(1, now=now)
    if level != "critical" or not monitor.is_stopped(1):
        problems.append("限权监测未就绪：强信号未触发紧急停实例")
    # ④ fail-safe：风控组件故障 → 一律停发（宁可停发，不冒封号风险）
    broken_guard = RiskGuard(rules=(), state_lookup=lambda uid: (_ for _ in ()).throw(
        RuntimeError("risk store down")))
    failsafe = broken_guard.check(RiskAction(op_uid="fs", account_id=1,
                                             conversation_uid="c-kpi",
                                             action_type="send_reply"), now=now)
    if failsafe.verdict is not Verdict.FAILSAFE or failsafe.permit is not None:
        problems.append("fail-safe 失效：风控故障时仍可能放行")
    passed = not problems
    return {
        "name": KPI_NAMES[3],
        "target": "0 封号（平台状态）",
        "actual": "红线拦截 100% + 敏感零自动执行 + 限权监测就绪 + fail-safe 停发"
                  if passed else f"{len(problems)} 项结构检查失败",
        "detail": (
            "demo 结构口径（预防段四道闸全绿）：①红线拦截 100%（6 语料全 BLOCK）；"
            "②敏感操作零自动执行（改价/订单/支付/收货一律 INTERCEPT）；"
            "③限权监测就绪（强信号→紧急→自动停实例）；④fail-safe（风控故障一律停发）；"
            "**正式口径（验收期 14 天平台封号状态观测）为验收期占位声明**"
        ),
        "passed": passed,
        "gaps": problems,
    }


# ── KPI-⑤ 响应速度 ≤45s（中位数）───────────────────────────────────────────

#: 模拟七段打点值（秒，FakeClock 逐点供给）：检测 2s + 处理链 29s = 31s 总时长
_SIMULATED_MARKS: tuple[float, ...] = (2.0, 3.5, 4.5, 5.5, 10.0, 30.0, 31.0)


def check_response_speed() -> dict[str, Any]:
    """ResponseClock 模拟数据：20 条首响 trace → summarize 中位数/P90 ≤45s。"""
    traces = []
    for index in range(20):
        marks = iter(_SIMULATED_MARKS)
        clock = lambda: next(marks)  # noqa: E731 —— FakeClock：逐打点供给模拟值
        response = ResponseClock(f"trace-{index:02d}", account_id=1,
                                 conv_uid=f"c-{index:02d}", clock=clock, t_arr=0.0)
        for mark in ("t0_detect", "t1_read", "intent_done", "retrieval_done",
                     "t2_ai", "typing_done", "t3_sent"):
            response.mark(mark)
        traces.append(finish(response, "sent"))
    summary = summarize(traces)
    median = summary["median_sec"]
    p90 = summary["p90_sec"]
    passed = median <= TARGET_MEDIAN_SEC and p90 <= TARGET_MEDIAN_SEC
    return {
        "name": KPI_NAMES[4],
        "target": f"≤{TARGET_MEDIAN_SEC:.0f} 秒（中位数）",
        "actual": f"中位数 {median:.1f}s / P90 {p90:.1f}s（{summary['sent_cnt']} 条模拟 trace）",
        "detail": (
            "demo 口径：ResponseClock 七段打点（检测 2s + 读取/意图/检索/生成/输入/发送 "
            "29s = 31s 总时长）模拟 20 条首响；分母 = outcome=sent；"
            "生产以系统监控（segment 埋点真实值）复测"
        ),
        "passed": passed,
        "gaps": [] if passed else [f"中位数 {median:.1f}s / P90 {p90:.1f}s 超过 45s 预算"],
        "payload": {"median_sec": median, "p90_sec": p90},
    }


# ── 汇总与报告 ──────────────────────────────────────────────────────────────


def run_checks() -> list[dict[str, Any]]:
    """执行五项检查（判定表数据源；顺序与 §16.3 一致）。"""
    return [
        check_stability(),
        check_daily_inquiry(),
        check_reply_quality(),
        check_account_safety(),
        check_response_speed(),
    ]


def render_report(checks: Sequence[dict[str, Any]]) -> str:
    """渲染中文 Markdown 报告（五项判定表 + 口径说明）。"""
    all_pass = all(check["passed"] for check in checks)
    lines: list[str] = []
    lines.append("# Phase 2 §16.3 KPI 预跑报告")
    lines.append("")
    lines.append(f"- 批次：迭代 2.3 收口（{datetime.now().isoformat(timespec='seconds')}）")
    lines.append(f"- 总体结论：**{'全绿（五项 KPI 均达标，demo 口径）' if all_pass else '未达标（见下表）'}**")
    lines.append("")
    lines.append("> **demo 口径置顶**：本预跑为内存链路开发自验（钉钉/微信推送为 "
                 "Protocol+Fake、住宅 IP 为配置模拟、14 天观测以结构口径替代）。")
    lines.append("> **生产验收必须替换**：稳定运行/账号安全以验收期 14 天真实观测"
                 "（运行日志 + 平台封号状态）复测，回复可用率以人工抽评出证。")
    lines.append("")
    lines.append("## 五项 KPI 判定表")
    lines.append("")
    lines.append("| # | KPI | 目标值 | 实测 | 结论 |")
    lines.append("|---|-----|--------|------|------|")
    for index, check in enumerate(checks, start=1):
        verdict = "通过" if check["passed"] else "未通过"
        lines.append(f"| {index} | {check['name']} | {check['target']} | {check['actual']} | {verdict} |")
    lines.append("")
    for check in checks:
        lines.append(f"### {check['name']}")
        lines.append("")
        lines.append(f"- 实测：{check['actual']}")
        lines.append(f"- 口径：{check['detail']}")
        if check.get("gaps"):
            lines.append("- 缺口/问题：")
            for gap in check["gaps"]:
                text = json.dumps(gap, ensure_ascii=False) if isinstance(gap, dict) else str(gap)
                lines.append(f"  - {text}")
        else:
            lines.append("- 缺口/问题：无")
        lines.append("")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    """参数解析（仓库根运行；全部参数可缺省）。"""
    parser = argparse.ArgumentParser(
        prog="phase2_kpi.py",
        description="§16.3 Phase 2 KPI 预跑（稳定运行/日询盘/可用率/账号安全/响应速度，demo 口径）",
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="Markdown 报告输出路径")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行五项检查 → 写判定表报告 → 退出码 0/1（全绿/未达标或失败）。"""
    args = build_parser().parse_args(argv)
    try:
        checks = run_checks()
    except Exception as exc:  # noqa: BLE001 —— 预跑本身失败即不通过
        print(f"{TAG} 预跑执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    all_pass = all(check["passed"] for check in checks)
    for index, check in enumerate(checks, start=1):
        verdict = "通过" if check["passed"] else "未通过"
        print(f"{TAG} {check['name']}：{check['actual']} —— {verdict}")
    print(f"{TAG} 结论：{'§16.3 五项 KPI 预跑全绿（demo 口径）' if all_pass else '§16.3 KPI 预跑存在未达标项'}"
          "（生产验收须以 14 天观测/人工抽评复测）")
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(checks), encoding="utf-8")
    print(f"{TAG} 报告已写入：{report_path}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
