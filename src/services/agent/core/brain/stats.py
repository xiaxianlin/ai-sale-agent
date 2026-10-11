# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""日处理询盘量统计（NFR-PER-04，SR-NFR-PER-04 §6.1-§6.4；迭代 2.2 demo 口径）。

统计口径（验收证据的度量衡，§6.1）：
- 什么算 1 条：一个会话在一个统计日（Asia/Shanghai 自然日）内**首次达到处理终态**计 1；
- 终态三选一均计入（口径字段化 process_type）：ai_auto（AI 自动回复）/ human_transfer
  （转人工）/ fallback（低置信兜底）；
- 计数前提（防刷）：当日必须存在买家入站消息——无入站即达终态一律拒绝计数；
- 跨日规则：同会话次日再有买家新消息并完成处理，次日再计 1（去重键按日换键）。

工程形态（PG 事实源 + Redis 加速的进程内等价）：事件表 = ``_events``（唯一键
(账号, 会话, 日) 幂等去重）；Redis 计数镜像 = ``_counters``（原子锁内累计）；
小时快照从事件表重算对账，漂移以事件表为准回修并告警 data_drift（Redis 只加速、不裁决）；
日终核查未达下限（默认 15）先归因分类再告警 stat_below_floor（下限只告警不拦截，
上限归口风控 NFR-SEC-12）。

归因二分（可提升闭环）：平台侧 traffic_low（接入量 < 下限）；系统侧 poll_miss /
risk_capped / fallback_excess / human_backlog / system_down；多因命中 shortfall_code=mixed。
demo 形态：同步单进程 + RLock（生产 = 多实例 PG 唯一约束 + Redis Lua 原子累计）。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

#: 处理终态三分类（口径字段化；SR-NFR-PER-04 §6.1）
PROCESS_TYPES: tuple[str, ...] = ("ai_auto", "human_transfer", "fallback")
#: Asia/Shanghai 时区（与风控工作窗口一致，SR-AG-005 §7）
SHANGHAI_TZ = timezone(timedelta(hours=8))
#: 归因码全集（平台侧 1 + 系统侧 5；mixed 为多因组合码）
ATTRIBUTION_FACTORS: tuple[str, ...] = (
    "traffic_low", "poll_miss", "risk_capped", "fallback_excess", "human_backlog", "system_down",
)


def shanghai_now() -> datetime:
    """当前 Asia/Shanghai 时间（默认统计时钟）。"""
    return datetime.now(SHANGHAI_TZ)


@dataclass(frozen=True)
class HandledEvent:
    """询盘处理事件（事实源一行；唯一键 = 账号 + 会话 + 统计日）。"""

    account_id: int
    conversation_id: str
    stat_date: date
    process_type: str
    first_in_at: datetime | None   # 当日买家首条入站时刻（计数前提的证据）
    handled_at: datetime
    response_sec: float | None     # 首入站 → 终态耗时（NFR-PER-03 联动）


@dataclass(frozen=True)
class AttributionResult:
    """归因结果：主码 + 全部命中因素 + 中文摘要（归因看板数据源）。"""

    shortfall_code: str            # "" = 达标；单因 = 因素码；多因 = mixed
    factors: tuple[str, ...]
    summary: str
    details: dict[str, Any]


class DailyInquiryStats:
    """日处理询盘量统计：计数 / 去重 / 构成 / 快照对账 / 归因（SR-NFR-PER-04 §6.2-§6.4）。

    :param floor: 单账号日处理下限（默认 15，验收线；stat.floor.daily_per_account）；
    :param clock: 统计时钟注入（默认 shanghai_now；测试拨钟）。
    """

    def __init__(
        self,
        *,
        floor: int = 15,
        fallback_ratio_max: float = 0.4,
        human_pending_max: int = 5,
        downtime_minutes: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._floor = floor
        self._fallback_ratio_max = fallback_ratio_max
        self._human_pending_max = human_pending_max
        self._downtime_minutes = downtime_minutes
        self._clock = clock if clock is not None else shanghai_now
        self._lock = threading.RLock()
        self._events: dict[tuple[int, str, date], HandledEvent] = {}
        self._inbound: dict[tuple[int, str, date], datetime] = {}  # 键 → 首条入站时刻
        self._counters: dict[tuple[int, date], dict[str, int]] = {}  # Redis 计数镜像
        self.alerts: list[dict[str, Any]] = []  # 进程内告警事件（生产 = FR-AG-007 通道）

    # ---------------------------------------------------------------- 计数入口

    def record_inbound(
        self, account_id: int, conversation_id: str, *, at: datetime | None = None
    ) -> None:
        """登记买家入站消息（计数前提证据；同会话同日仅记首条时刻）。"""
        moment = at if at is not None else self._clock()
        key = (account_id, conversation_id, moment.date())
        with self._lock:
            self._inbound.setdefault(key, moment)

    def record_handled(
        self,
        account_id: int,
        conversation_id: str,
        process_type: str,
        *,
        handled_at: datetime | None = None,
    ) -> bool:
        """处理完成埋点（唯一计数入口）：前提校验 → 幂等去重 → 事件落表 → 计数累计。

        :return: True = 本次计 1；False = 被拒绝（无入站前提/重复终态/非法类型）。
        """
        if process_type not in PROCESS_TYPES:
            return False
        moment = handled_at if handled_at is not None else self._clock()
        stat_date = moment.date()
        key = (account_id, conversation_id, stat_date)
        with self._lock:
            first_in_at = self._inbound.get(key)
            if first_in_at is None:
                return False  # 防刷：当日无买家入站，杜绝系统自发行为刷量
            if key in self._events:
                return False  # 幂等：同会话当日第二类终态不重复计（口径=首终态）
            response_sec = (
                round((moment - first_in_at).total_seconds(), 3)
                if moment >= first_in_at
                else None
            )
            self._events[key] = HandledEvent(
                account_id=account_id, conversation_id=conversation_id,
                stat_date=stat_date, process_type=process_type,
                first_in_at=first_in_at, handled_at=moment, response_sec=response_sec,
            )
            counters = self._counters.setdefault(
                (key[0], key[2]),  # (账号, 统计日) —— Redis HASH 键 stat:handled:{acct}:d:{date}
                {"total": 0, "ai_auto": 0, "human_transfer": 0, "fallback": 0},
            )
            counters["total"] += 1  # 锁内累计 = Redis HINCRBY 原子语义的进程内等价
            counters[process_type] += 1
            return True

    # ---------------------------------------------------------------- 查询面

    def counters(self, account_id: int, stat_date: date) -> dict[str, int]:
        """当日实时计数（Redis 镜像读；缺省四字段零值）。"""
        with self._lock:
            found = self._counters.get((account_id, stat_date))
            return dict(found) if found else {
                "total": 0, "ai_auto": 0, "human_transfer": 0, "fallback": 0,
            }

    def received(self, account_id: int, stat_date: date) -> int:
        """当日接入询盘会话数（归因分母；含未完成处理）。"""
        with self._lock:
            return sum(
                1 for key in self._inbound if key[0] == account_id and key[2] == stat_date
            )

    def event_of(
        self, account_id: int, conversation_id: str, stat_date: date
    ) -> HandledEvent | None:
        """按唯一键读取事件（审计/抽样复核入口）。"""
        with self._lock:
            return self._events.get((account_id, conversation_id, stat_date))

    def composition(self, account_id: int, stat_date: date) -> dict[str, Any]:
        """构成报表行：处理量 + 三分类计数 + 占比 + 达标（§5-1 日报表出参形态）。"""
        counters = self.counters(account_id, stat_date)
        total = counters["total"]
        ratios = {
            name: (round(counters[name] / total, 3) if total else 0.0)
            for name in PROCESS_TYPES
        }
        return {
            "account_id": account_id,
            "stat_date": stat_date.isoformat(),
            "handled_total": total,
            "cnt_ai_auto": counters["ai_auto"],
            "cnt_human_transfer": counters["human_transfer"],
            "cnt_fallback": counters["fallback"],
            "ratios": ratios,
            "meet_floor": total >= self._floor,
        }

    # ---------------------------------------------------------------- 快照对账（Redis 漂移自愈）

    def snapshot(self, stat_date: date) -> list[dict[str, Any]]:
        """小时快照对账：从事件表按（账号, 日）重算 → 与计数镜像比对 → 漂移即回修+告警。

        漂移只可能少计（宕机丢数）不可能多计（PG 先行原则）；回修以事件表为准，
        告警 data_drift 供核查（Redis 只加速、不裁决）。
        """
        rows: list[dict[str, Any]] = []
        with self._lock:
            accounts = sorted({key[0] for key in self._events if key[2] == stat_date})
            for account_id in accounts:
                day_events = [
                    ev for ev in self._events.values()
                    if ev.account_id == account_id and ev.stat_date == stat_date
                ]
                truth = {
                    "total": len(day_events),
                    "ai_auto": sum(1 for ev in day_events if ev.process_type == "ai_auto"),
                    "human_transfer": sum(1 for ev in day_events if ev.process_type == "human_transfer"),
                    "fallback": sum(1 for ev in day_events if ev.process_type == "fallback"),
                }
                before = self._counters.get((account_id, stat_date))
                drifted = before is not None and (
                    before.get("total", 0) != truth["total"]
                    or any(before.get(name, 0) != truth[name] for name in PROCESS_TYPES)
                )
                if drifted:
                    self._counters[(account_id, stat_date)] = dict(truth)  # 以 PG 事实源回修
                    self.alerts.append({
                        "type": "data_drift",
                        "account_id": account_id,
                        "stat_date": stat_date.isoformat(),
                        "before": dict(before or {}),
                        "after": dict(truth),
                    })
                rows.append({
                    "account_id": account_id,
                    "stat_date": stat_date.isoformat(),
                    "handled_total": truth["total"],
                    "cnt_ai_auto": truth["ai_auto"],
                    "cnt_human_transfer": truth["human_transfer"],
                    "cnt_fallback": truth["fallback"],
                    "drift": drifted,
                })
        return rows

    def override_counter(self, account_id: int, stat_date: date, *, total: int) -> None:
        """计数镜像注入钩子：模拟 Redis 漂移（快照对账测试/故障演练专用）。"""
        with self._lock:
            counters = self._counters.setdefault(
                (account_id, stat_date),
                {"total": 0, "ai_auto": 0, "human_transfer": 0, "fallback": 0},
            )
            counters["total"] = total

    # ---------------------------------------------------------------- 归因与日终核查

    def attribution(
        self,
        account_id: int,
        stat_date: date,
        *,
        poll_read: set[str] | None = None,
        risk_capped: int = 0,
        human_pending: int = 0,
        downtime_minutes: float = 0.0,
    ) -> AttributionResult:
        """归因分类器（§6.4）：未达下限先二分平台侧/系统侧，再细分因素。

        因素序（可多因命中）：traffic_low → poll_miss → risk_capped → fallback_excess
        → human_backlog → system_down；单因 = 该因素码，多因 = mixed。
        """
        comp = self.composition(account_id, stat_date)
        received = self.received(account_id, stat_date)
        factors: list[str] = []
        if received < self._floor:
            factors.append("traffic_low")  # 平台侧：询盘流量不足（核查曝光/店铺运营）
        else:
            unread = received - comp["handled_total"]
            if poll_read is not None and unread > 0 and unread > len(poll_read):
                factors.append("poll_miss")  # 系统侧：轮询遗漏（FR-AG-002 日志对账）
            if risk_capped > 0:
                factors.append("risk_capped")  # 系统侧：风控限额停发波及
        if comp["handled_total"] > 0 and comp["cnt_fallback"] / comp["handled_total"] > self._fallback_ratio_max:
            factors.append("fallback_excess")  # 系统侧：知识库/置信度拖累（联动 FR-AG-009）
        if human_pending > self._human_pending_max:
            factors.append("human_backlog")  # 系统侧：人工侧积压
        if downtime_minutes > self._downtime_minutes:
            factors.append("system_down")  # 系统侧：当日中断累计超阈（NFR-REL-03）
        code = "" if not factors else (factors[0] if len(factors) == 1 else "mixed")
        summary = (
            "达标（≥ 下限）" if code == ""
            else f"未达下限（处理 {comp['handled_total']} < {self._floor}），归因：{'、'.join(factors)}"
        )
        return AttributionResult(code, tuple(factors), summary, {
            "received_total": received, "handled_total": comp["handled_total"],
        })

    def daily_check(
        self,
        stat_date: date,
        *,
        poll_read: set[str] | None = None,
        risk_capped: dict[int, int] | None = None,
        human_pending: dict[int, int] | None = None,
        downtime_minutes: dict[int, float] | None = None,
    ) -> list[dict[str, Any]]:
        """日终下限核查（§6.4）：逐账号判定达标 → 未达标落归因码并告警（只告警不拦截）。"""
        risk_capped = risk_capped or {}
        human_pending = human_pending or {}
        downtime_minutes = downtime_minutes or {}
        rows: list[dict[str, Any]] = []
        with self._lock:
            accounts = sorted(
                {key[0] for key in self._inbound if key[2] == stat_date}
                | {key[0] for key in self._events if key[2] == stat_date}
            )
        for account_id in accounts:
            comp = self.composition(account_id, stat_date)
            result = self.attribution(
                account_id, stat_date,
                poll_read=poll_read,
                risk_capped=risk_capped.get(account_id, 0),
                human_pending=human_pending.get(account_id, 0),
                downtime_minutes=downtime_minutes.get(account_id, 0.0),
            )
            rows.append({
                "account_id": account_id,
                "stat_date": stat_date.isoformat(),
                "handled_total": comp["handled_total"],
                "cnt_ai_auto": comp["cnt_ai_auto"],
                "cnt_human_transfer": comp["cnt_human_transfer"],
                "cnt_fallback": comp["cnt_fallback"],
                "ratios": comp["ratios"],
                "meet_floor": comp["meet_floor"],
                "shortfall_code": result.shortfall_code,
                "factors": list(result.factors),
            })
            if not comp["meet_floor"]:
                self.alerts.append({
                    "type": "stat_below_floor",
                    "level": "mid",
                    "account_id": account_id,
                    "stat_date": stat_date.isoformat(),
                    "handled_total": comp["handled_total"],
                    "floor": self._floor,
                    "shortfall_code": result.shortfall_code,
                    "summary": result.summary,
                })
        return rows


__all__ = [
    "PROCESS_TYPES",
    "SHANGHAI_TZ",
    "ATTRIBUTION_FACTORS",
    "shanghai_now",
    "HandledEvent",
    "AttributionResult",
    "DailyInquiryStats",
]
