# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""§16.3 日处理询盘量报表 CLI（NFR-PER-04，SR-NFR-PER-04 §6.5；迭代 2.2 demo 口径）。

产出「系统统计」验收证据的中文报表：
- 逐账号处理量构成（AI 自动 / 转人工 / 兜底三终态，同会话当日计 1 防刷口径）；
- 下限（默认 15 条/账号）达标判定与未达归因分类（traffic_low / fallback_excess / …）；
- 小时快照对账演示（计数镜像漂移以事件表为准回修，data_drift 告警）。

**demo 口径声明**：本脚本运行于内存合成数据（无真实甲方询盘、无 PG/Redis），
为工程自证证据；正式验收必须按 SRS §16.3 以真实运行 14 天的系统统计报表导出复测。

用法（仓库根）：
  python3 tools/agent/inquiry_stats_cli.py [--floor 15] [--report out/agent/inquiry_stats_report.md]
退出码：0 = 演示数据全链自洽；1 = 断言失败或运行异常。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# 仓库根自举：直接以文件路径执行时补齐仓库根（tools 包）与 src（services 包）
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.agent.core.brain.stats import DailyInquiryStats  # noqa: E402

TAG = "[询盘统计]"
SH = ZoneInfo("Asia/Shanghai")
DEFAULT_REPORT = Path("out") / "agent" / "inquiry_stats_report.md"


def build_demo_day(stats: DailyInquiryStats, base: datetime, floor: int) -> None:
    """合成一个统计日：账号 1 达标（AI 自动 + 转人工 + 兜底构成）、账号 2 流量不足。"""
    # 账号 1：floor+2 条达标（17 = 14 AI 自动 + 1 转人工 + 2 兜底），含重复终态/无入站反例
    for i in range(floor - 1):
        stats.record_inbound(1, f"conv-{i:03d}", at=base + timedelta(minutes=5 * i))
        stats.record_handled(1, f"conv-{i:03d}", "ai_auto")
    stats.record_inbound(1, "conv-transfer", at=base + timedelta(hours=2))
    stats.record_handled(1, "conv-transfer", "human_transfer")
    for j in range(2):
        stats.record_inbound(1, f"conv-fb-{j}", at=base + timedelta(hours=3 + j))
        stats.record_handled(1, f"conv-fb-{j}", "fallback")
    # 口径反例（应被拒绝，不计入）：重复终态 / 无当日买家入站
    assert stats.record_handled(1, "conv-000", "fallback") is False, TAG + " 重复终态未被拒绝"
    assert stats.record_handled(1, "conv-ghost", "ai_auto") is False, TAG + " 无入站未被拒绝"
    # 账号 2：接入 10 < 15，零处理 → traffic_low 归因
    for i in range(10):
        stats.record_inbound(2, f"lead-{i:02d}", at=base + timedelta(minutes=7 * i))


def render_report(
    rows: list[dict], snapshot_rows: list[dict], *, stat_date: str, floor: int
) -> str:
    """渲染中文 Markdown 报表（口径声明置顶）。"""
    lines = [
        "# 日处理询盘量报表（NFR-PER-04，demo 口径）",
        "",
        f"- 统计日：{stat_date}（Asia/Shanghai 自然日）",
        f"- 下限：{floor} 条/账号（验收线，stat.floor.daily_per_account；上限归口风控 NFR-SEC-12）",
        "- 口径：同会话当日首终态计 1（三终态均计）；计数前提=当日存在买家入站消息（防刷）",
        "- **demo 口径声明**：内存合成数据自证证据；正式验收以真实运行 14 天系统统计复测",
        "",
        "## 一、逐账号日报",
        "",
        "| 账号 | 处理量 | AI 自动 | 转人工 | 兜底 | AI 占比 | 达标 | 归因码 | 归因因素 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        ratios = row["ratios"]
        lines.append(
            f"| {row['account_id']} | {row['handled_total']} | {row['cnt_ai_auto']} "
            f"| {row['cnt_human_transfer']} | {row['cnt_fallback']} "
            f"| {ratios['ai_auto']:.1%} | {'是' if row['meet_floor'] else '否'} "
            f"| {row['shortfall_code'] or '—'} | {'、'.join(row['factors']) or '—'} |"
        )
    lines += [
        "",
        "## 二、小时快照对账（事件表为事实源，计数镜像漂移回修）",
        "",
        "| 账号 | 处理量（事件表） | 快照漂移 |",
        "|---|---|---|",
    ]
    for row in snapshot_rows:
        lines.append(
            f"| {row['account_id']} | {row['handled_total']} | {'是（已回修+data_drift 告警）' if row['drift'] else '否'} |"
        )
    total = sum(row["handled_total"] for row in rows)
    meet = sum(1 for row in rows if row["meet_floor"])
    lines += [
        "",
        "## 三、结论",
        "",
        f"- 合计处理量：{total} 条；达标账号：{meet}/{len(rows)}",
        "- 未达标账号归因见上表（未达下限已产生 stat_below_floor 告警事件，供核查看板）",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：合成演示日 → 快照对账 → 中文报表（stdout + 可选落盘）。"""
    parser = argparse.ArgumentParser(description="日处理询盘量统计报表（NFR-PER-04，demo 口径）")
    parser.add_argument("--floor", type=int, default=15, help="单账号日处理下限（默认 15）")
    parser.add_argument("--date", default="2026-10-11", help="统计日（YYYY-MM-DD，默认演示日）")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报表落盘路径（Markdown）")
    args = parser.parse_args(argv)

    stat_day = datetime.fromisoformat(args.date).date()
    base = datetime(stat_day.year, stat_day.month, stat_day.day, 9, 0, 0, tzinfo=SH)
    stats = DailyInquiryStats(floor=args.floor, clock=lambda: base)
    build_demo_day(stats, base, args.floor)
    rows = stats.daily_check(stat_day)

    # 快照对账演示：人为压低账号 1 计数镜像（模拟 Redis 宕机丢数）→ 快照回修
    acct1_total = stats.counters(1, stat_day)["total"]
    stats.override_counter(1, stat_day, total=max(0, acct1_total - 5))
    snapshot_rows = stats.snapshot(stat_day)

    report = render_report(rows, snapshot_rows, stat_date=stat_day.isoformat(), floor=args.floor)
    print(report)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    # 自洽断言：账号 1 达标（floor+2）、账号 2 归因 traffic_low、漂移已回修
    by_acct = {row["account_id"]: row for row in rows}
    assert by_acct[1]["handled_total"] == args.floor + 2, TAG + " 账号1 构成不符"
    assert by_acct[1]["meet_floor"] is True, TAG + " 账号1 应达标"
    assert by_acct[2]["shortfall_code"] == "traffic_low", TAG + " 账号2 归因应为 traffic_low"
    assert any(a["type"] == "stat_below_floor" for a in stats.alerts), TAG + " 缺少下限告警"
    assert any(a["type"] == "data_drift" for a in stats.alerts), TAG + " 缺少漂移告警"
    drift_row = next(row for row in snapshot_rows if row["account_id"] == 1)
    assert drift_row["handled_total"] == args.floor + 2, TAG + " 快照未按事件表回修"
    print(f"{TAG} 中文报表已写入 {report_path}；自洽断言全部通过（demo 合成数据口径）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
