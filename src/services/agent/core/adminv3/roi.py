# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V3 ROI 统计：询盘明细账本 + metric_daily 预聚合 + 账号/整体只读视图
（FR-SC-005，SR-SC-005 §6.4）。

- 口径映射（§9「预聚合对账」基准，与手工口径逐项可对）：inquiry_cnt（询盘）、
  reply_rate（回复率）、reply_within45s（45s 内回复达标数，边界 ≤45s）、
  human_touch_rate（人工介入率）、gmv_total（成交额 = 平台确认 + 人工补录）；
- 幂等可重跑：明细按（日期,账号,会话）覆盖写（同会话重复登记不重复计数）；
  rollup_day 按（日期,账号）UPSERT——补录/修正后无脑重跑不漂移；
- 降级读路径：预聚合未覆盖的明细日（补账后未重跑）自动按明细直算兜底；
  空窗口返回零值视图与空趋势；窗口内全局有数据而该账号全程无数据 → 判定
  「无数据账号」返回 None（API 层映射 40401）。

demo 形态：进程内字典账本（生产 = PG inquiry_fact + metric_daily，UPSERT SQL 见 §6.4）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date
from typing import Any

#: 45s 响应达标边界（SRS §16.5 响应速度口径；≤45s 达标）
REPLY_TARGET_SECONDS: float = 45.0

#: 账号/整体视图的计量字段（比率字段由计数派生）
_COUNT_FIELDS: tuple[str, ...] = (
    "inquiry_cnt", "reply_cnt", "reply_within45s", "human_touch_cnt",
)
_RATE_FIELDS: tuple[str, ...] = ("reply_rate", "human_touch_rate")
_GMV_FIELDS: tuple[str, ...] = ("gmv_total", "gmv_manual")


@dataclass(frozen=True)
class InquiryFact:
    """询盘明细事实（inquiry_fact 行语义；同会话重复登记覆盖写）。"""

    account_id: int
    conversation_id: str
    stat_date: date
    replied: bool = False
    reply_sec: float | None = None                # 首响时长（秒；未回复为 None）
    human_touched: bool = False
    gmv_confirmed: float = 0.0                    # 平台确认成交额（USD）


def _empty_counts() -> dict[str, Any]:
    """零值计量（空窗口/无数据账号的降级视图底座）。"""
    counts: dict[str, Any] = {field: 0 for field in _COUNT_FIELDS}
    counts.update({field: 0.0 for field in _RATE_FIELDS})
    counts.update({field: 0.0 for field in _GMV_FIELDS})
    return counts


def _aggregate_facts(facts: list[InquiryFact], gmv_manual: float) -> dict[str, Any]:
    """按明细直算一个计量单元（预聚合缺失时的兜底口径，与 rollup 同公式）。"""
    row = _empty_counts()
    for fact in facts:
        row["inquiry_cnt"] += 1
        if fact.replied:
            row["reply_cnt"] += 1
            if fact.reply_sec is not None and fact.reply_sec <= REPLY_TARGET_SECONDS:
                row["reply_within45s"] += 1
        if fact.human_touched:
            row["human_touch_cnt"] += 1
        row["gmv_confirmed_sum"] = row.get("gmv_confirmed_sum", 0.0) + fact.gmv_confirmed
    confirmed = float(row.pop("gmv_confirmed_sum", 0.0))
    row["gmv_manual"] = gmv_manual
    row["gmv_total"] = confirmed + gmv_manual
    row["reply_rate"] = row["reply_cnt"] / row["inquiry_cnt"] if row["inquiry_cnt"] else 0.0
    row["human_touch_rate"] = (
        row["human_touch_cnt"] / row["inquiry_cnt"] if row["inquiry_cnt"] else 0.0)
    return row


def _finalize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """计量单元归并：计数累加、比率按合计重算（防比率的比率失真）。"""
    total = _empty_counts()
    for row in rows:
        for field in (*_COUNT_FIELDS, *_GMV_FIELDS):
            total[field] += row[field]
    total["reply_rate"] = total["reply_cnt"] / total["inquiry_cnt"] if total["inquiry_cnt"] else 0.0
    total["human_touch_rate"] = (
        total["human_touch_cnt"] / total["inquiry_cnt"] if total["inquiry_cnt"] else 0.0)
    return total


class RoiStats:
    """ROI 统计器：明细账本 → 按日预聚合（UPSERT）→ 账号/整体只读视图（§6.4）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 明细账本：(stat_date, account_id, conversation_id) → fact（覆盖写幂等）
        self._facts: dict[tuple[date, int, str], InquiryFact] = {}
        # 人工补录成交额：(account_id, stat_date) → USD（record_gmv_manual 累加）
        self._gmv_manual: dict[tuple[int, date], float] = {}
        # metric_daily 预聚合行（dict 形态；rollup_day 按 (stat_date, account_id) UPSERT）
        self.rows: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- 登记与补录

    def record(self, fact: InquiryFact) -> None:
        """登记询盘明细（同会话重复登记覆盖写——幂等，不重复计数）。"""
        with self._lock:
            self._facts[(fact.stat_date, fact.account_id, fact.conversation_id)] = fact

    def record_gmv_manual(self, account_id: int, stat_date: date, amount: float) -> None:
        """人工补录成交额（线下成交/对账修正；同日累加，重跑 rollup 即并入）。"""
        with self._lock:
            key = (account_id, stat_date)
            self._gmv_manual[key] = self._gmv_manual.get(key, 0.0) + amount

    # ---------------------------------------------------------------- 预聚合

    def rollup_day(self, stat_date: date) -> list[dict[str, Any]]:
        """按日预聚合（UPSERT 口径）：同日重跑行数不增、指标不漂移（§9 用例）。"""
        with self._lock:
            by_account: dict[int, list[InquiryFact]] = {}
            for (day, account_id, _conv), fact in self._facts.items():
                if day == stat_date:
                    by_account.setdefault(account_id, []).append(fact)
            fresh = []
            for account_id in sorted(by_account):
                facts = by_account[account_id]
                row = _aggregate_facts(facts, self._gmv_manual.get((account_id, stat_date), 0.0))
                fresh.append({"stat_date": stat_date, "account_id": account_id, **row})
            self.rows = [r for r in self.rows if r["stat_date"] != stat_date] + fresh
            return [dict(r) for r in fresh]

    # ---------------------------------------------------------------- 只读视图

    def account_view(self, account_id: int, date_from: date, date_to: date) -> dict[str, Any] | None:
        """账号级视图（metric_daily 只读聚合）。

        - 窗口倒置 → ValueError；无数据且窗口内全局有数据 → None（无数据账号，
          API 映射 40401）；窗口内全局无数据 → 零值降级视图（前端展示 0/-）。
        """
        if date_from > date_to:
            raise ValueError(f"窗口起止倒置：{date_from} > {date_to}")
        with self._lock:
            rows = [r for r in self.rows
                    if r["account_id"] == account_id and date_from <= r["stat_date"] <= date_to]
            facts = [f for (day, acct, _conv), f in self._facts.items()
                     if acct == account_id and date_from <= day <= date_to]
            if not rows and not facts:
                global_has_data = (
                    any(date_from <= day <= date_to for (day, _a, _c) in self._facts)
                    or any(date_from <= r["stat_date"] <= date_to for r in self.rows))
                if global_has_data:
                    return None                                # 无数据账号（异常口径）
                return {"account_id": account_id,              # 空窗口零值降级
                        **_empty_counts()}
            # 预聚合未覆盖的明细日（补账后未重跑）→ 明细直算兜底
            covered = {r["stat_date"] for r in rows}
            extra_days = {f.stat_date for f in facts} - covered
            units = [dict(r) for r in rows]
            for day in sorted(extra_days):
                day_facts = [f for f in facts if f.stat_date == day]
                units.append(_aggregate_facts(
                    day_facts, self._gmv_manual.get((account_id, day), 0.0)))
            return {"account_id": account_id, **_finalize(units)}

    def overview(self, date_from: date, date_to: date) -> dict[str, Any]:
        """整体视图：totals 汇总 + trend 按日 + ranking 账号降序（§6.4 出参形态）。"""
        if date_from > date_to:
            raise ValueError(f"窗口起止倒置：{date_from} > {date_to}")
        with self._lock:
            accounts = sorted(
                {r["account_id"] for r in self.rows if date_from <= r["stat_date"] <= date_to}
                | {acct for (day, acct, _c) in self._facts if date_from <= day <= date_to})
            days = sorted(
                {r["stat_date"] for r in self.rows if date_from <= r["stat_date"] <= date_to}
                | {day for (day, _a, _c) in self._facts if date_from <= day <= date_to})
            trend = []
            for day in days:
                day_units = [self._account_unit(account_id, day, day) for account_id in accounts]
                day_row = _finalize([u for u in day_units if u is not None])  # type: ignore[arg-type]
                trend.append({"stat_date": day.isoformat(), **day_row})
            ranking = []
            for account_id in accounts:
                unit = self._account_unit(account_id, date_from, date_to)
                if unit is not None:
                    ranking.append({"account_id": account_id, **unit})
            ranking.sort(key=lambda r: (-float(r["gmv_total"]), int(r["account_id"])))
            totals = _finalize([r for r in ranking]) if ranking else _empty_counts()
            return {"totals": totals, "trend": trend, "ranking": ranking}

    def _account_unit(self, account_id: int, date_from: date, date_to: date) -> dict[str, Any] | None:
        """单账号单窗口计量单元（无数据返回 None，不进 trend/ranking）。"""
        view = self.account_view(account_id, date_from, date_to)
        if view is None:
            return None
        return {k: v for k, v in view.items() if k != "account_id"}


__all__ = ["REPLY_TARGET_SECONDS", "InquiryFact", "RoiStats"]
