# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""Phase 3 收口：§16.4 KPI 预跑 CLI（demo 口径）。

四项判定（SRS §16.4 建议验收项 → demo 可测口径）：
①聊天组件可用性与加载性能 —— SDK 静态结构断言全绿 + 站点 API 冒烟（site_smoke）
②留资率基线 —— site 指标体系产出留资率数值（demo 判"基线建立"而非绝对值）
③弃购挽回邮件送达率 ≥95% —— crm_smoke 送达率判定
④CRM 互通正确性 —— crm_smoke 五级瀑布合并 + 360 断言
⑤隐私合规检查 —— DSR 删除零残留 + consent 门控断言
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from services.site.core.admin_v2.metrics import (  # noqa: E402
    SessionFact,
    SiteMetrics,
    VisitorFact,
)


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(_REPO_ROOT))
    return proc.returncode, (proc.stdout + proc.stderr)[-400:]


class _KpiEventSource:
    """留资率预跑的内存事实源（SiteEventSource 协议最小实现，demo 合成事实）。

    CR T2 修复配套：留资率判定从「报告关键词匹配」改为调 site metrics 纯函数——
    4 访客 / 4 会话 / 2 留资，经 aggregate_day → overview 同一生产口径产出 lead_rate。
    """

    _DAY0 = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)

    def __init__(self) -> None:
        self._events = [
            VisitorFact(vid, "page_view", created_at=self._DAY0) for vid in ("v1", "v2", "v3", "v4")
        ] + [VisitorFact(vid, "lead", created_at=self._DAY0) for vid in ("v1", "v2")]
        self._sessions = [
            SessionFact(cid, self._DAY0 + timedelta(minutes=cid)) for cid in (1, 2, 3, 4)
        ]

    def visitor_events(self, day: date):
        return self._events

    def sessions(self, day: date):
        return self._sessions

    def mail_jobs(self, day: date):
        return []

    def customer_by_visitor(self, visitor_uid: str):
        return None

    def deals_of(self, customer_id: int):
        return []

    def delivered_cart_recover_between(self, customer_id: int, start: datetime, end: datetime):
        return False

    def drill_rows(self, metric: str, day: date):
        return []


def check_lead_rate_baseline() -> tuple[bool, str]:
    """CR T2 修复：留资率基线 = SiteMetrics 纯函数真实产出数值（非关键词匹配）。"""
    day = date(2026, 10, 11)
    metrics = SiteMetrics(
        _KpiEventSource(), clock=lambda: datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    )
    metrics.aggregate_day(day)
    overview = metrics.overview(day, day)
    rate = overview["lead_rate"]
    ok = rate is not None and 0.0 <= rate <= 1.0 and overview["leads"] == 2 and overview["sessions"] == 4
    note = (
        f"SiteMetrics 纯函数产出 lead_rate={rate:.2f}（{overview['leads']}/{overview['sessions']}，"
        "demo 判基线建立，非绝对值）"
    ) if rate is not None else "SiteMetrics 未产出留资率数值（分母会话为 0）"
    return ok, note


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    # ① 组件与站点链路：跑 site_smoke（含 SDK 静态断言的测试文件由全量回归覆盖，此处跑冒烟）
    code, tail = _run([sys.executable, "tools/site/site_smoke.py"])
    checks.append(("聊天组件可用性与站点链路", code == 0, tail.strip().splitlines()[-1] if tail.strip() else ""))

    # ③④ 邮件送达率与 CRM 互通：跑 crm_smoke（内部含 ≥95% 判定与瀑布断言）
    code, tail = _run([sys.executable, "tools/site/crm_smoke.py"])
    ok = code == 0
    checks.append(("弃购挽回邮件送达率 ≥95%", ok, "crm_smoke 判定含送达率" ))
    checks.append(("CRM 互通正确性（瀑布合并+客户360）", ok, ""))

    # ② 留资率基线：CR T2 修复——改调 site metrics 纯函数（结构化判定，弃关键词匹配）
    lead_ok, lead_note = check_lead_rate_baseline()
    checks.append(("留资率基线建立", lead_ok, lead_note))

    # ⑤ 隐私合规：CR T2 修复——改跑 privacy 服务单测取退出码（结构化判定，弃关键词匹配）
    # 不另传 -q：pyproject addopts 已含 -q，双重 -q 会吞掉"N passed"汇总行（证据需要该行）
    code, tail = _run([sys.executable, "-m", "pytest",
                       "src/services/site/tests/test_privacy_service.py"])
    checks.append(("隐私合规检查（consent+DSR 零残留）", code == 0,
                   tail.strip().splitlines()[-1] if tail.strip() else "test_privacy_service"))

    lines = ["# Phase 3 收口报告（§16.4，demo 口径）", "", "> 本项目为 demo：正式口径中服务商对账/真人盲测项以机制证明替代，已在各行注明。", "", "| KPI | 判定 | 证据 |", "|---|---|---|"]
    for name, ok, note in checks:
        lines.append(f"| {name} | {'✅ 通过' if ok else '❌ 不通过'} | {note} |")
    verdict = all(ok for _, ok, _ in checks)
    lines += ["", f"**结论：{'Phase 3 验收（demo 口径）全部 KPI 通过' if verdict else '存在不通过项'}**", ""]
    out = _REPO_ROOT / "out/site/phase3_kpi_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[phase3] 报告 → {out}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
