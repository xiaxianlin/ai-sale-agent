# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""FR-SITE-006 管理后台 V2·指标口径/预聚合/漏斗/钻取（SR-SITE-006 §6.1-6.3，Phase 3 第二批）。

模块职责：
- :class:`SiteEventSource`——看板事件源协议（访客事件/会话/邮件任务/客户绑定/成交），
  demo 以内存 Fake 注入，生产替换 PG 查询形态、协议不变（不 import 兄弟模块实现）；
- :class:`SiteMetrics`——指标口径单点定义 + 日预聚合（site_metrics_daily / site_mail_daily
  形态）+ 漏斗 + 钻取注册表：
  * 五指标口径：访客量 UV（仅 consent=true）/ 会话量 / 留资率=留资÷会话 /
    挽回率=挽回成交÷弃购 / 邮件效果（与 FR-SITE-004 同源同口径）；
  * 预聚合幂等：同日重算覆盖写（先清零再归因，对齐 ON CONFLICT DO UPDATE）；
    空数据日补零（主管打开看板永远有数）；
  * 邮件列回溯重算：送达/打开滞后回填由 recalc_window_days 窗口整段重算消化；
  * 挽回归因双条件：窗口内成交 且 收到 ≥1 封已送达 cart_recover 邮件；
  * 钻取：指标 → 明细列表（列定义注册表驱动），与看板值同源一致。

率值口径：核心层一律返回小数（0.5），百分比展示由 API/前端层换算。
依据文档：docs/07-实现方案/SITE/SR-SITE-006-管理后台V2.md §6.1/§6.2/§6.3。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

#: 邮件三类序列（site_mail_daily.job_type CHECK 口径）
JOB_TYPES: tuple[str, ...] = ("cart_recover", "quote_follow", "festival")

#: 邮件状态映射（与 FR-SITE-004 同源口径，SR §6.1④）：
#: sent=已离开队列（failed 计入分母并单列失败率）；delivered={delivered,opened}（单调推进）；
#: queued（未发出）/cancelled（已取消）不入任何分子分母。
MAIL_SENT_STATUSES = frozenset({"delivered", "opened", "failed"})
MAIL_DELIVERED_STATUSES = frozenset({"delivered", "opened"})

#: 钻取注册表：指标 → 列定义（后端注册表驱动，前端通用渲染，SR §6.3 DRILL）
DRILL_COLUMNS: dict[str, list[dict]] = {
    "visitors": [{"key": "visitor_uid", "label": "访客标识", "masked": True},
                 {"key": "channel", "label": "渠道"}, {"key": "first_at", "label": "首次访问"}],
    "sessions": [{"key": "conversation_id", "label": "会话"},
                 {"key": "customer_id", "label": "客户", "masked": True},
                 {"key": "created_at", "label": "创建时间"}],
    "leads": [{"key": "visitor_uid", "label": "访客", "masked": True},
              {"key": "created_at", "label": "留资时间"}],
    "abandons": [{"key": "visitor_uid", "label": "访客", "masked": True},
                 {"key": "created_at", "label": "弃购时间"}],
    "recovered": [{"key": "abandon_at", "label": "弃购时间"},
                  {"key": "deal_at", "label": "成交时间"},
                  {"key": "window_days", "label": "归因窗口"}],
    "mail_sent": [{"key": "job_type", "label": "序列"},
                  {"key": "recipient", "label": "收件人", "masked": True},
                  {"key": "status", "label": "状态"}],
}


class DrillUnknown(Exception):
    """未注册的下钻指标（40001 族，SR §6.3）。"""

    code = 40001


@dataclass(frozen=True)
class VisitorFact:
    """访客事件事实（visitor_event 形态：page_view/lead/abandon/source_visit + consent）。"""

    visitor_uid: str
    event_type: str
    referrer: str = "direct"
    consent: bool = True
    created_at: datetime | None = None
    page: str = ""


# arch-guard: exempt 独立站会话无阿里账号维度（account_id 恒为默认 0，03-数据库设计 §2 语义）
@dataclass(frozen=True)
class SessionFact:
    """会话事实（conversation 形态：channel='site' 当日新建）。"""

    conversation_id: int
    created_at: datetime
    customer_id: int | None = None
    channel: str = "site"


@dataclass(frozen=True)
class MailFact:
    """邮件任务事实（mail_job 形态：归属日=发送日，状态终值计数）。"""

    job_type: str
    status: str
    recipient: str
    sent_at: datetime


@dataclass(frozen=True)
class DealFact:
    """成交事实（customer.deal_records 形态）。"""

    deal_at: datetime
    amount_usd: float = 0.0


class SiteEventSource(Protocol):
    """看板事件源协议（只读；预聚合与下钻共用同一事实源——口径单点）。"""

    def visitor_events(self, day: date) -> Sequence[VisitorFact]:
        """当日访客事件流。"""
        ...

    def sessions(self, day: date) -> Sequence[SessionFact]:
        """当日新建会话流。"""
        ...

    def mail_jobs(self, day: date) -> Sequence[MailFact]:
        """当日（发送日归属）邮件任务终值流。"""
        ...

    def customer_by_visitor(self, visitor_uid: str) -> int | None:
        """访客 → 绑定客户（FR-SITE-005 channels.external_id=visitor_uid）。"""
        ...

    def deals_of(self, customer_id: int) -> Sequence[DealFact]:
        """客户成交记录。"""
        ...

    def delivered_cart_recover_between(self, customer_id: int, start: datetime,
                                       end: datetime) -> bool:
        """窗口内是否收到 ≥1 封已送达 cart_recover 邮件（挽回归因条件 b）。"""
        ...

    def drill_rows(self, metric: str, day: date) -> Sequence[dict]:
        """指标明细行（下钻直查明细表；consent 过滤由源侧保证）。"""
        ...


def _div(numerator: float, denominator: float) -> float | None:
    """率计算：分母 0 → None（前端显示「—」，不显示 0%，SR §6.1）。"""
    return (numerator / denominator) if denominator else None


def _empty_row() -> dict:
    """空渠道行（预聚合补零基数）。"""
    return {"visitors": set(), "leads": set(), "abandons": 0, "sessions": 0,
            "orders": 0, "recovered": 0}


class SiteMetrics:
    """独立站看板指标：口径单点 + 日预聚合 + 漏斗 + 钻取（读写分离：查询只扫预聚合）。"""

    def __init__(self, source: SiteEventSource, *, clock: Callable[[], datetime],
                 attribution_window_days: int = 7, recalc_window_days: int = 14) -> None:
        self.source = source
        self.clock = clock
        self.attribution_window_days = attribution_window_days
        self.recalc_window_days = recalc_window_days
        self._daily: dict[date, dict[str, dict]] = {}       # 日 × 渠道 预聚合行
        self._mail_daily: dict[date, dict[str, dict]] = {}  # 日 × 序列 邮件预聚合行

    # ---------------------------------------------------------------- 预聚合

    def aggregate_day(self, day: date) -> dict:
        """单日聚合（幂等）：访客/留资/弃购 → 会话/成交 → 挽回归因 → 邮件回溯窗口重算。"""
        rows: dict[str, dict] = {}

        def row_of(channel: str) -> dict:
            return rows.setdefault(channel or "direct", _empty_row())

        # ① 访客/留资/弃购（仅 consent=true，FR-SITE-007；渠道=referrer）
        for v in self.source.visitor_events(day):
            if not v.consent:
                continue
            row = row_of(v.referrer)
            if v.event_type == "page_view":
                row["visitors"].add(v.visitor_uid)
            elif v.event_type == "lead":
                row["leads"].add(v.visitor_uid)
            elif v.event_type == "abandon":
                row["abandons"] += 1
        # ② 会话/成交（渠道细分在源侧借访客绑定完成；未绑定归 other，总量=各行求和）
        for s in self.source.sessions(day):
            row_of("other")["sessions"] += 1
        for cust_id in {s.customer_id for s in self.source.sessions(day) if s.customer_id}:
            row_of("other")["orders"] += sum(
                1 for d in self.source.deals_of(cust_id) if d.deal_at.date() == day)
        # ③ 挽回归因（先清零当日 recovered 再累加——重算即幂等；双条件见 SR §6.2③）
        window = timedelta(days=self.attribution_window_days)
        for v in self.source.visitor_events(day):
            if v.event_type != "abandon" or not v.consent or v.created_at is None:
                continue
            cust_id = self.source.customer_by_visitor(v.visitor_uid)
            if cust_id is None:
                continue
            dealt = any(v.created_at < d.deal_at <= v.created_at + window
                        for d in self.source.deals_of(cust_id))
            mailed = self.source.delivered_cart_recover_between(
                cust_id, v.created_at, v.created_at + window)
            if dealt and mailed:
                row_of(v.referrer)["recovered"] += 1
        rows.setdefault("direct", _empty_row())  # 空数据日补零（至少一行，主管永远有数）
        self._daily[day] = rows  # 整日覆盖写（先清零语义）
        # ④ 邮件列：回溯窗口内整段重算（送达/打开滞后回填由窗口消化，SR §6.1③）
        for offset in range(self.recalc_window_days):
            self._aggregate_mail_day(day - timedelta(days=offset))
        return self._summary(rows)

    def _aggregate_mail_day(self, day: date) -> None:
        """单日邮件列重算（按 job_type 细分，状态终值计数）。"""
        out = {jt: {"sent": 0, "delivered": 0, "opened": 0, "failed": 0} for jt in JOB_TYPES}
        for m in self.source.mail_jobs(day):
            if m.job_type not in out:
                continue
            row = out[m.job_type]
            if m.status in MAIL_SENT_STATUSES:
                row["sent"] += 1
            if m.status in MAIL_DELIVERED_STATUSES:
                row["delivered"] += 1
            if m.status == "opened":
                row["opened"] += 1
            if m.status == "failed":
                row["failed"] += 1
        self._mail_daily[day] = out

    # ---------------------------------------------------------------- 查询（只扫预聚合）

    def _iter_rows(self, date_from: date, date_to: date) -> list[dict]:
        """区间内渠道行展平（渠道名并入行）。"""
        out = []
        for day, channels in self._daily.items():
            if date_from <= day <= date_to:
                for channel, row in channels.items():
                    out.append({"date": day, "channel": channel, **{k: v for k, v in row.items()}})
        return out

    def _totals(self, date_from: date, date_to: date) -> dict:
        """区间总量（UV 去重按行计数求和——渠道间访客不重叠的 demo 口径）。"""
        visitors = leads = abandons = sessions = orders = recovered = 0
        for row in self._iter_rows(date_from, date_to):
            visitors += len(row["visitors"])
            leads += len(row["leads"])
            abandons += row["abandons"]
            sessions += row["sessions"]
            orders += row["orders"]
            recovered += row["recovered"]
        return {"visitors": visitors, "sessions": sessions, "leads": leads,
                "abandons": abandons, "recovered": recovered, "orders": orders}

    def _mail_totals(self, date_from: date, date_to: date, job_type: str | None = None) -> dict:
        """区间邮件总量（跨列求和）。"""
        keys = [job_type] if job_type else list(JOB_TYPES)
        total = {"sent": 0, "delivered": 0, "opened": 0, "failed": 0}
        for day, by_type in self._mail_daily.items():
            if date_from <= day <= date_to:
                for jt in keys:
                    for k in total:
                        total[k] += by_type.get(jt, {}).get(k, 0)
        return total

    def overview(self, date_from: date, date_to: date) -> dict:
        """看板总览：五指标 KPI（访客量/会话量/留资率/弃购挽回率/邮件效果）。"""
        t = self._totals(date_from, date_to)
        mail = self._mail_totals(date_from, date_to)
        return {
            "visitors": t["visitors"],
            "sessions": t["sessions"],
            "leads": t["leads"],
            "lead_rate": _div(t["leads"], t["sessions"]),          # 留资率 = 留资 ÷ 会话
            "abandons": t["abandons"],
            "recovered": t["recovered"],
            "recover_rate": _div(t["recovered"], t["abandons"]),   # 挽回率 = 挽回成交 ÷ 弃购
            "orders": t["orders"],
            "mail": {"sent": mail["sent"], "delivered": mail["delivered"],
                     "opened": mail["opened"], "failed": mail["failed"],
                     "delivery_rate": _div(mail["delivered"], mail["sent"]),
                     "open_rate": _div(mail["opened"], mail["delivered"])},
        }

    def funnel(self, date_from: date, date_to: date) -> dict:
        """漏斗视图：访问 → 会话 → 留资 → 成交 四层及层间转化率（SR §6.3）。"""
        t = self._totals(date_from, date_to)
        return {
            "layers": [("访问", t["visitors"]), ("会话", t["sessions"]),
                       ("留资", t["leads"]), ("成交", t["orders"])],
            "rates": {"会话率": _div(t["sessions"], t["visitors"]),
                      "留资率": _div(t["leads"], t["sessions"]),
                      "成交率": _div(t["orders"], t["leads"])},
        }

    def mail_effect(self, date_from: date, date_to: date,
                    job_type: str | None = None) -> dict:
        """邮件效果表：三类序列 × 送达/打开/失败率（与 FR-SITE-004 同源同口径）。"""
        keys = [job_type] if job_type else list(JOB_TYPES)
        by_type: dict[str, dict] = {}
        for jt in keys:
            m = self._mail_totals(date_from, date_to, jt)
            by_type[jt] = {**m,
                           "delivery_rate": _div(m["delivered"], m["sent"]),
                           "open_rate": _div(m["opened"], m["delivered"]),
                           "failed_rate": _div(m["failed"], m["sent"])}
        return {"by_type": by_type,
                "mail_backfill_until": self.clock().date() - timedelta(days=self.recalc_window_days)}

    def drill(self, metric: str, day: date, channel: str | None = None,
              page: int = 1, page_size: int = 20) -> dict:
        """指标下钻：看板指标 → 明细列表（列定义注册表驱动；与看板值同源一致）。"""
        if metric not in DRILL_COLUMNS:
            raise DrillUnknown(f"未注册的下钻指标: {metric}")
        rows = [r for r in self.source.drill_rows(metric, day)
                if channel is None or r.get("channel") in (channel, None)]
        start = (page - 1) * page_size
        return {"metric": metric, "date": day.isoformat(), "columns": DRILL_COLUMNS[metric],
                "items": list(rows[start:start + page_size]), "total": len(rows)}

    # ---------------------------------------------------------------- 汇总辅助

    def _summary(self, rows: dict[str, dict]) -> dict:
        """聚合摘要（aggregate_day 返回值；集合转计数，供幂等比较）。"""
        return {ch: {"visitors": len(r["visitors"]), "leads": len(r["leads"]),
                     "abandons": r["abandons"], "sessions": r["sessions"],
                     "orders": r["orders"], "recovered": r["recovered"]}
                for ch, r in sorted(rows.items())}
