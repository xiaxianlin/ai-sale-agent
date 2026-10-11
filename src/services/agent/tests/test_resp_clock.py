# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""询盘响应时钟单元测试（NFR-PER-03，SR-NFR-PER-03 §6.1/§6.5；迭代 2.2 TDD）。

覆盖（开发规范 §9.1 五类）：
- 等价类：T0→T3 七段差分与超预算归因（a=实际/b=预算）；中位数与 P90 统计；
- 边界类：T_arr 缺失 detect=0、中间打点缺失段记 0 不崩溃、预算表合计 ≤45s；
- 异常类：落库存储抛异常 → finish best-effort 不阻断主链路（观测旁路化）；
- 降级类：转人工/延迟收口不计中位数分母、报表单列（防口径粉饰）；
- 告警类：单例 >60s 即时告警事件、≤60s 不告警。

实现方案：docs/07-实现方案/NFR/SR-NFR-PER-03-询盘响应速度.md §6.1、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from typing import Any

import pytest

from services.agent.core.brain.clock import (
    DEFAULT_BUDGETS_SEC,
    PHASES,
    ResponseClock,
    finish,
    summarize,
)


class FakeClock:
    """确定性时钟：按预置刻度序列逐次返回（单调秒）。"""

    def __init__(self, ticks: list[float]) -> None:
        self._ticks = list(ticks)
        self._i = 0

    def __call__(self) -> float:
        value = self._ticks[min(self._i, len(self._ticks) - 1)]
        self._i += 1
        return value


def make_clock(ticks: list[float], t_arr: float | None = 0.0) -> ResponseClock:
    """打点序列：t0→t1→intent→retrieval→t2→typing→t3（t_arr 经构造参数传入）。"""
    clock = ResponseClock("tr-1", account_id=1, conv_uid="c-1", clock=FakeClock(ticks), t_arr=t_arr)
    for name in ("t0_detect", "t1_read", "intent_done", "retrieval_done",
                 "t2_ai", "typing_done", "t3_sent"):
        clock.mark(name)
    return clock


def test_segments_diff_seven_phases() -> None:
    """等价类：七段差分正确——detect=10/read=2/intent=1.5/retrieval=1.5/generate=5/typing=16/send=1。"""
    clock = make_clock([10.0, 12.0, 13.5, 15.0, 20.0, 36.0, 37.0], t_arr=0.0)
    seg, overrun = clock.segments_ms(DEFAULT_BUDGETS_SEC)
    assert seg["detect"] == 10000.0
    assert seg["read"] == 2000.0
    assert seg["intent"] == 1500.0
    assert seg["retrieval"] == 1500.0
    assert seg["generate"] == 5000.0
    assert seg["typing"] == 16000.0
    assert seg["send"] == 1000.0
    assert seg["total"] == 10000.0 + (37.0 - 10.0) * 1000.0  # total = detect + T0→T3
    assert overrun == {}


def test_t_arr_missing_detect_zero() -> None:
    """边界类：平台时间戳不可得 → detect 记 0（报表标注口径，不臆造检测窗）。"""
    clock = make_clock([10.0, 12.0, 13.5, 15.0, 20.0, 36.0, 37.0], t_arr=None)
    seg, _ = clock.segments_ms(DEFAULT_BUDGETS_SEC)
    assert seg["detect"] == 0.0
    assert seg["total"] == 27000.0


def test_missing_intermediate_mark_zero_without_crash() -> None:
    """边界类：仅 T0/T3 两个必打点 → 中间段记 0、total 仍正确（不崩溃）。"""
    clock = ResponseClock("tr-2", account_id=1, conv_uid="c-1", clock=FakeClock([5.0, 40.0]))
    clock.mark("t0_detect")
    clock.mark("t3_sent")
    seg, _ = clock.segments_ms(DEFAULT_BUDGETS_SEC)
    assert seg["read"] == 0.0
    assert seg["typing"] == 0.0
    assert seg["total"] == 35000.0


def test_overrun_report_actual_vs_budget() -> None:
    """等价类：超预算段归因 {segment: {a=实际ms, b=预算ms}}——超时归因报表数据源。"""
    ticks = [10.0, 12.0, 13.5, 15.0, 22.0, 38.0, 39.0]  # generate=7s>5s、typing=16s≤20s
    clock = make_clock(ticks)
    seg, overrun = clock.segments_ms(DEFAULT_BUDGETS_SEC)
    assert overrun["generate"] == {"a": 7000.0, "b": 5000.0}
    assert "typing" not in overrun
    assert seg["generate"] == 7000.0


def test_budget_table_within_45s() -> None:
    """边界类：预算表七段齐全且合计 ≤45s（NFR-PER-03 中位达成路径，缓冲另留）。"""
    assert set(DEFAULT_BUDGETS_SEC) == set(PHASES) and len(PHASES) == 7
    assert sum(DEFAULT_BUDGETS_SEC.values()) <= 45.0


def test_median_and_p90_over_sent_only() -> None:
    """等价类：中位数/P90 仅统计 outcome=sent 的 AI 自动响应（§1 统计口径）。"""
    traces = [make_trace(f"tr-{i}", "sent", total_s) for i, total_s in
              enumerate([30.0, 40.0, 50.0, 80.0], start=1)]
    summary = summarize(traces)
    assert summary["sent_cnt"] == 4
    assert summary["median_sec"] == 45.0
    assert summary["p90_sec"] == 80.0


def test_handover_deferred_excluded_but_counted() -> None:
    """降级类：转人工/夜间延迟不计分母但单列计数（口径透明，防粉饰）。"""
    traces = [
        make_trace("tr-1", "sent", 30.0),
        make_trace("tr-2", "sent", 40.0),
        make_trace("tr-3", "handover", 10.0),
        make_trace("tr-4", "deferred", 20.0),
    ]
    summary = summarize(traces)
    assert summary["sent_cnt"] == 2
    assert summary["median_sec"] == 35.0  # 仅 sent 两条的中位数
    assert summary["excluded"]["handover"] == 1
    assert summary["excluded"]["deferred"] == 1


def test_single_slow_alert_over_60s() -> None:
    """降级类：sent 且 total>60s → inquiry_slow_single 即时告警事件（部署口径对齐）。"""
    clock = make_clock([10.0, 12.0, 13.5, 15.0, 20.0, 66.0, 72.0])  # total=10+62=72s
    alerts: list[dict[str, Any]] = []
    record = finish(clock, "sent", store=[], alert_sink=alerts.append)
    assert record.total_ms == 72000.0
    assert alerts and alerts[0]["type"] == "inquiry_slow_single"
    assert alerts[0]["trace_id"] == "tr-1"


def test_no_alert_under_60s() -> None:
    """边界类：total≤60s → 不告警。"""
    clock = make_clock([10.0, 12.0, 13.5, 15.0, 20.0, 36.0, 37.0])
    alerts: list[dict[str, Any]] = []
    finish(clock, "sent", store=[], alert_sink=alerts.append)
    assert alerts == []


def test_finish_best_effort_store_failure() -> None:
    """异常类：落库存储抛异常 → finish 吞掉不抛（观测旁路化，NFR-REL-01）。"""

    class BrokenStore:
        def append(self, _record: Any) -> None:
            raise RuntimeError("PG 不可用")

    clock = make_clock([10.0, 12.0, 13.5, 15.0, 20.0, 36.0, 37.0])
    record = finish(clock, "sent", store=BrokenStore())
    assert record.outcome == "sent"
    assert record.trace_id == "tr-1"


def make_trace(trace_id: str, outcome: str, total_s: float) -> Any:
    """统计输入构造：TraceRecord 只读形态。"""
    from services.agent.core.brain.clock import TraceRecord

    return TraceRecord(
        trace_id=trace_id, account_id=1, conv_uid=f"c-{trace_id}",
        outcome=outcome, seg_ms={"total": total_s * 1000.0}, overrun={},
        total_ms=total_s * 1000.0, message_id=None,
    )
