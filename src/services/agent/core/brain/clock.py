# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""询盘响应时钟（NFR-PER-03，SR-NFR-PER-03 §6.1/§6.5；迭代 2.2 demo 口径）。

统一埋点口径（SRS §7.3 UC-06 全链路）：T_arr（平台消息时间戳，检测归因）→ T0（检测到
询盘）→ T1（读取完成）→ T2（AI 处理完成）→ T3（发送完成）；七段耗时
detect/read/intent/retrieval/generate/typing/send 随 message 生命周期落库（trace_id 关联）。

45s 预算分配表（中位达成路径）：检测 ≤10s + 读取 ≤2s + 意图 ≤1.5s + 检索 ≤1.5s +
生成 ≤5s + 输入模拟 ≤20s（拟人节奏由风控注入，NFR-SEC-11）+ 发送 ≤1s → 合计 41s，
留 ≈4s 重试/抖动缓冲（总预算 45s）。

统计口径（验收测量依据，防口径粉饰）：
- 响应时长 R = T3 − T_arr（T_arr 不可得时以 T0 起算并在报表标注，detect 记 0）；
- 中位数分母 = outcome=sent 的 AI 自动响应；转人工（handover）/夜间延迟（deferred）
  不计入分母但单列计数；
- 单例 >60s 即时告警（inquiry_slow_single，部署方案 §4 口径对齐）。

观测旁路化：打点/落库/告警全部 best-effort，失败只吞不抛，不阻断响应主链路（NFR-REL-01）。
demo 形态：同步进程内（生产 = agent-runner 异步旁路 + Prometheus Histogram 推送）。
"""
from __future__ import annotations
import logging

_LOGGER = logging.getLogger(__name__)

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

#: 七段名（监控观测预算，SR-NFR-PER-03 §2 预算分配表）
PHASES: tuple[str, ...] = ("detect", "read", "intent", "retrieval", "generate", "typing", "send")

#: 45s 总预算的七段分配（秒）；合计 41s，余 ≈4s 为重试/风控抖动缓冲
DEFAULT_BUDGETS_SEC: dict[str, float] = {
    "detect": 10.0,
    "read": 2.0,
    "intent": 1.5,
    "retrieval": 1.5,
    "generate": 5.0,
    "typing": 20.0,
    "send": 1.0,
}
#: 总预算（中位达标线）
TOTAL_BUDGET_SEC: float = 45.0
#: 单例告警阈值（部署方案 §4：中位数 >60s 告警；单例劣化即时告警阈值同值）
SINGLE_ALERT_SEC: float = 60.0

#: 合法打点名（mark 校验；t_arr 经构造参数传入，其余按处理时序逐个 mark）
MARKS: tuple[str, ...] = (
    "t0_detect", "t1_read", "intent_done", "retrieval_done", "t2_ai", "typing_done", "t3_sent",
)
#: 段 → (起点打点, 终点打点)；detect 段起点为构造参数 t_arr
_SEG_BOUNDS: dict[str, tuple[str, str]] = {
    "detect": ("t_arr", "t0_detect"),
    "read": ("t0_detect", "t1_read"),
    "intent": ("t1_read", "intent_done"),
    "retrieval": ("intent_done", "retrieval_done"),
    "generate": ("retrieval_done", "t2_ai"),
    "typing": ("t2_ai", "typing_done"),
    "send": ("typing_done", "t3_sent"),
}


class ResponseClock:
    """询盘响应时钟：打点 → 七段差分 → 超预算归因（SR-NFR-PER-03 §6.1）。

    :param clock: 单调时钟注入（默认 time.monotonic，秒）；测试注入确定性 FakeClock。
    :param t_arr: 平台消息时间戳（单调秒；None = 不可得，detect 记 0 并在报表标注）。
    """

    def __init__(
        self,
        trace_id: str,
        *,
        account_id: int,
        conv_uid: str,
        clock: Callable[[], float] | None = None,
        t_arr: float | None = None,
    ) -> None:
        self.trace_id = trace_id
        self.account_id = account_id
        self.conv_uid = conv_uid
        self._clock = clock if clock is not None else time.monotonic
        self.t_arr = t_arr
        self.marks: dict[str, float] = {}

    def mark(self, name: str) -> None:
        """记录打点（墙钟/单调秒）；非法打点名即刻报错（埋点拼写错误尽早暴露）。"""
        if name not in MARKS:
            raise ValueError(f"非法打点名：{name}（合法集合 {MARKS}）")
        self.marks[name] = self._clock()

    def segments_ms(self, budgets: dict[str, float] | None = None) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        """七段差分 + 超预算归因。

        - 段值毫秒；端点打点缺失的段记 0（不崩溃，报表可见）；
        - detect = T0 − T_arr（t_arr 缺失记 0）；
        - total = detect + (T3 − T0)；
        - overrun = {segment: {a: 实际ms, b: 预算ms}}（仅超预算段；超时归因报表数据源）。
        """
        budgets = budgets if budgets is not None else DEFAULT_BUDGETS_SEC
        seg: dict[str, float] = {}
        for phase, (start_mark, end_mark) in _SEG_BOUNDS.items():
            start = self.t_arr if start_mark == "t_arr" else self.marks.get(start_mark)
            end = self.marks.get(end_mark)
            seg[phase] = max(0.0, (end - start) * 1000.0) if start is not None and end is not None else 0.0
        t0, t3 = self.marks.get("t0_detect"), self.marks.get("t3_sent")
        seg["total"] = seg["detect"] + (max(0.0, (t3 - t0) * 1000.0) if t0 is not None and t3 is not None else 0.0)
        overrun = {
            key: {"a": value, "b": budgets[key] * 1000.0}
            for key, value in seg.items()
            if key in budgets and value > budgets[key] * 1000.0
        }
        return seg, overrun

    @property
    def total_ms(self) -> float:
        """当前累计总时长（毫秒；detect 段计入）。"""
        return self.segments_ms()[0]["total"]


@dataclass(frozen=True)
class TraceRecord:
    """询盘响应打点记录（生产 = PG inquiry_response_trace 行的进程内形态）。"""

    trace_id: str
    account_id: int
    conv_uid: str
    outcome: str                       # sent / handover / deferred / failed
    seg_ms: dict[str, float] = field(default_factory=dict)
    overrun: dict[str, dict[str, float]] = field(default_factory=dict)
    total_ms: float = 0.0
    message_id: str | None = None      # 首响 outbound 消息（sent 时回填）


def finish(
    clock: ResponseClock,
    outcome: str,
    *,
    store: Any | None = None,
    message_id: str | None = None,
    budgets: dict[str, float] | None = None,
    alert_sink: Callable[[dict[str, Any]], None] | None = None,
    single_alert_sec: float = SINGLE_ALERT_SEC,
) -> TraceRecord:
    """T3 后收口：差分 → 落库 → 单例 >60s 即时告警。

    观测旁路化（NFR-REL-01）：落库/告警任何异常只吞不抛——收口自身永不阻断主链路；
    生产在此外推 Prometheus Histogram/Gauge（demo 略，统计走 summarize）。
    """
    seg, overrun = clock.segments_ms(budgets)
    record = TraceRecord(
        trace_id=clock.trace_id, account_id=clock.account_id, conv_uid=clock.conv_uid,
        outcome=outcome, seg_ms=seg, overrun=overrun,
        total_ms=seg.get("total", 0.0), message_id=message_id,
    )
    if store is not None:
        try:
            store.append(record)  # 生产 = 与首响 message 同事务写 inquiry_response_trace
        except Exception as exc:  # noqa: BLE001 —— best-effort：仅记日志不阻断主链路（CR Q2 补日志）
            _LOGGER.warning("响应时钟留痕失败（已容忍）: %s", type(exc).__name__, exc_info=True)
    if (
        outcome == "sent"
        and alert_sink is not None
        and record.total_ms > single_alert_sec * 1000.0
    ):
        try:
            alert_sink({
                "type": "inquiry_slow_single",
                "trace_id": clock.trace_id,
                "account_id": clock.account_id,
                "total_sec": round(record.total_ms / 1000.0, 3),
                "overrun": overrun,
            })
        except Exception:  # noqa: BLE001
            pass
    return record


def summarize(traces: Iterable[TraceRecord]) -> dict[str, Any]:
    """窗口统计（看板数据源，§5 GET /perf/response/summary 的模块内形态）。

    分母 = outcome=sent（§1 统计口径）；handover/deferred/failed 单列 excluded 计数。
    """
    records = list(traces)
    sent_totals = sorted(r.total_ms / 1000.0 for r in records if r.outcome == "sent")
    excluded = {
        "handover": sum(1 for r in records if r.outcome == "handover"),
        "deferred": sum(1 for r in records if r.outcome == "deferred"),
        "failed": sum(1 for r in records if r.outcome == "failed"),
    }
    sent_records = [r for r in records if r.outcome == "sent"]
    seg_median: dict[str, float] = {}
    for phase in (*PHASES, "total"):
        values = sorted(r.seg_ms.get(phase, 0.0) / 1000.0 for r in sent_records)
        if values:
            seg_median[phase] = _median(values)
    return {
        "sent_cnt": len(sent_totals),
        "median_sec": _median(sent_totals),
        "p90_sec": _percentile90(sent_totals),
        "seg_median_sec": seg_median,
        "excluded": excluded,
    }


def _median(values: list[float]) -> float:
    """中位数（偶数个取均值；空集 0.0）。"""
    if not values:
        return 0.0
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _percentile90(values: list[float]) -> float:
    """P90（近邻法：ceil(0.9·n)-1 位；空集 0.0）。"""
    if not values:
        return 0.0
    index = max(0, math.ceil(0.9 * len(values)) - 1)
    return values[index]


__all__ = [
    "PHASES",
    "DEFAULT_BUDGETS_SEC",
    "TOTAL_BUDGET_SEC",
    "SINGLE_ALERT_SEC",
    "MARKS",
    "ResponseClock",
    "TraceRecord",
    "finish",
    "summarize",
]
