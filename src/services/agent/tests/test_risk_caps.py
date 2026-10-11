# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""会话量约束单元测试（NFR-SEC-12，SR-NFR-SEC-12 §6.1/§6.3/§9；迭代 2.3 TDD 阶段①用例）。

覆盖（上下限并存语义）：
- 上限保护（风控闸门，强制执行）：达上限自动转人工 + 高级告警 + 迁移回调；
- 梯度软限制：80% 预警（当日每阶段至多 1 条）/ 90% 仅 A 级自动（B 级与未知分级保守转人工）；
- 计数幂等：同会话重复 admit 不重复计费；
- 次日自动恢复：日期键换日自然清零；
- 下限 ≥15（能力验收线，只提示不拦截）：未达下限提示核查（当日至多 1 条）；
- fail-safe：时钟/内部故障 → 一律转人工（宁保守不冒险）。

实现方案：docs/07-实现方案/NFR/SR-NFR-SEC-12-会话量约束.md §6.1、§9
"""

from __future__ import annotations

from datetime import datetime

from services.agent.core.engine import InMemoryAlerts
from services.agent.core.risk.caps import ConversationQuotaGate

DAY1 = datetime(2026, 10, 8, 10, 0, 0)
DAY2 = datetime(2026, 10, 9, 9, 0, 0)


def make_gate(**overrides):
    """标准装配：cap=10 便于小步进触发梯度；记录迁移回调与告警。"""
    alerts = overrides.pop("alerts", None) or InMemoryAlerts()
    transfers: list[tuple[str, str]] = []
    gate = ConversationQuotaGate(
        daily_cap=overrides.pop("daily_cap", 10),
        floor=overrides.pop("floor", 15),
        alerts=alerts,
        on_transfer=lambda uid, stage: transfers.append((uid, stage)),
        **overrides,
    )
    return gate, alerts, transfers


def fill(gate: ConversationQuotaGate, count: int, *, day: datetime = DAY1, cap: int = 10) -> None:
    """灌入 count 个不同新会话（AA 级客户，绕过 90% 梯度干扰）。"""
    for i in range(count):
        gate.admit(1, f"conv-{i}", "A", now=day)


class TestUpperCap:
    """上限保护（默认 60、配置化；演示用 cap=10）。"""

    def test_first_admit_ok(self) -> None:
        """首会话 ADMIT：stage=ok，used=1。"""
        gate, _, _ = make_gate()
        decision = gate.admit(1, "conv-0", "B", now=DAY1)
        assert decision.verdict == "ADMIT"
        assert decision.stage == "ok"
        assert decision.used == 1

    def test_repeat_conversation_idempotent(self) -> None:
        """同会话重复 admit：幂等放行，used 不变（SET 去重语义）。"""
        gate, _, _ = make_gate()
        gate.admit(1, "conv-0", "B", now=DAY1)
        decision = gate.admit(1, "conv-0", "B", now=DAY1)
        assert decision.verdict == "ADMIT"
        assert decision.stage == "repeat"
        assert decision.used == 1

    def test_cap_reached_transfers_human(self) -> None:
        """达上限：第 cap+1 个新会话 TRANSFER_HUMAN（stage=cap_reached），used 不涨。"""
        gate, _, transfers = make_gate()
        fill(gate, 10)
        decision = gate.admit(1, "conv-overflow", "B", now=DAY1)
        assert decision.verdict == "TRANSFER_HUMAN"
        assert decision.stage == "cap_reached"
        assert decision.used == 10
        assert transfers == [("conv-overflow", "cap_reached")]

    def test_cap_alert_high_once(self) -> None:
        """达上限高级告警当日恰一条（梯度告警去重）。"""
        gate, alerts, _ = make_gate()
        fill(gate, 10)
        gate.admit(1, "c-a", "B", now=DAY1)
        gate.admit(1, "c-b", "B", now=DAY1)
        caps = [a for a in alerts.items if a.type == "conv_cap_reached"]
        assert len(caps) == 1 and caps[0].level == "high"

    def test_next_day_recovery(self) -> None:
        """次日自动恢复：日期键换日计数自然清零，无需显式重置。"""
        gate, _, _ = make_gate()
        fill(gate, 10, day=DAY1)
        decision = gate.admit(1, "conv-new-day", "B", now=DAY2)
        assert decision.verdict == "ADMIT"
        assert decision.used == 1


class TestGradient:
    """梯度软限制：80% 预警 / 90% 仅 A 级 / 100% 全转人工。"""

    def test_warn_at_80_percent(self) -> None:
        """80% 梯度：第 8 个（cap=10，warn_th=8）ADMIT + stage=warn。"""
        gate, _, _ = make_gate()
        for i in range(7):
            gate.admit(1, f"conv-{i}", "B", now=DAY1)
        decision = gate.admit(1, "conv-8th", "B", now=DAY1)
        assert decision.verdict == "ADMIT"
        assert decision.stage == "warn"

    def test_warn_alert_once_per_day(self) -> None:
        """预警当日每阶段至多 1 条告警（风暴防护）。"""
        gate, alerts, _ = make_gate()
        for i in range(9):
            gate.admit(1, f"conv-{i}", "A", now=DAY1)
        warns = [a for a in alerts.items if a.type == "conv_cap_warn"]
        assert len(warns) == 1

    def test_vip_only_blocks_non_a(self) -> None:
        """90% 梯度：>90%（第 10 个，cap=10）B 级客户 TRANSFER_HUMAN（stage=vip_only）。"""
        gate, _, transfers = make_gate()
        fill(gate, 9)                                          # A 级占至 90%
        decision = gate.admit(1, "conv-vip-b", "B", now=DAY1)
        assert decision.verdict == "TRANSFER_HUMAN"
        assert decision.stage == "vip_only"
        assert transfers == [("conv-vip-b", "vip_only")]

    def test_vip_only_admits_a_grade(self) -> None:
        """90% 梯度下 A 级客户仍自动接入（额度 +1，used=10=cap 恰等）。"""
        gate, _, _ = make_gate()
        fill(gate, 9)
        decision = gate.admit(1, "conv-vip-a", "A", now=DAY1)
        assert decision.verdict == "ADMIT"
        assert decision.used == 10

    def test_unknown_grade_conservative_transfer(self) -> None:
        """未知分级按非 A 保守转人工（fail-safe 默认）。"""
        gate, _, _ = make_gate()
        fill(gate, 9)
        decision = gate.admit(1, "conv-unk", "", now=DAY1)
        assert decision.verdict == "TRANSFER_HUMAN"
        assert decision.stage == "vip_only"


class TestFloorAndFailSafe:
    """下限 ≥15 提示核查（只告警不拦截）+ fail-safe 保守转人工。"""

    def test_floor_below_hint(self) -> None:
        """完成处理 12 < 15：提示核查告警（当日至多 1 条），不拦截任何动作。"""
        gate, alerts, _ = make_gate()
        result = gate.daily_floor_check(1, processed=12, now=DAY1)
        assert result["meet"] is False
        hints = [a for a in alerts.items if a.type == "quota_floor_hint"]
        assert len(hints) == 1
        gate.daily_floor_check(1, processed=13, now=DAY1)
        assert len([a for a in alerts.items if a.type == "quota_floor_hint"]) == 1

    def test_floor_met_no_hint(self) -> None:
        """≥15 达标：不产生提示告警。"""
        gate, alerts, _ = make_gate()
        result = gate.daily_floor_check(1, processed=15, now=DAY1)
        assert result["meet"] is True
        assert not [a for a in alerts.items if a.type == "quota_floor_hint"]

    def test_floor_check_zero_traffic_silent(self) -> None:
        """零流量（processed=0）不告警——归因平台侧流量，非系统核查项。"""
        gate, alerts, _ = make_gate()
        gate.daily_floor_check(1, processed=0, now=DAY1)
        assert not [a for a in alerts.items if a.type == "quota_floor_hint"]

    def test_failsafe_clock_failure_transfers(self) -> None:
        """时钟故障（内部异常）→ 一律转人工（stage=failsafe）+ 高级告警。"""
        gate, alerts, transfers = make_gate()

        def broken_clock() -> datetime:
            raise RuntimeError("clock down")

        gate._clock = broken_clock  # type: ignore[assignment]
        decision = gate.admit(1, "conv-fs", "A")               # 时钟故障即触发 fail-safe
        assert decision.verdict == "TRANSFER_HUMAN"
        assert decision.stage == "failsafe"
        assert alerts.last(level="high", type_="system_error") is not None

    def test_used_query(self) -> None:
        """used 查询面：看板实时值（当日 AI 接入数）。"""
        gate, _, _ = make_gate()
        fill(gate, 3)
        assert gate.used(1, now=DAY1) == 3
        assert gate.used(1, now=DAY2) == 0
