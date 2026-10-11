# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略 V2 流失挽回引擎单元测试（FR-SC-003④，SR-SC-003 §6.4/§9；P4 第二批 TDD）。

五类覆盖（开发规范 §9.1）：等价类（静默判定→建序列、渠道映射、激励递进 0→1→2）/
边界（恰好静默天数两侧、序列走完 exhausted）/ 异常（渲染缺模板跳过、同会话同轮
唯一键幂等）/ 并发（触发时状态复核：已回复/人工接管/明确拒绝三停止条件）/ 降级
（tier2 无主管批准等待重排、日发送上限顺延、风控拒绝重排不丢失——双保险防骚扰）。

实现方案：docs/07-实现方案/SC/SR-SC-003-销售策略引擎.md §6.4、§9
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.agent.core.strategyv2.winback import ConversationRef, InMemoryWinbackSource, WinbackEngine

NOW = datetime(2026, 10, 11, 10, 0, 0)


def make_source(**convs: ConversationRef) -> InMemoryWinbackSource:
    """标准数据源：一个阿里站内静默会话（15 天前最后入站）+ 可追加。"""
    source = InMemoryWinbackSource()
    source.upsert(ConversationRef(
        conversation_uid="c-silent", account_id=101, customer_id=9001,
        channel="alibaba", last_inbound_at=NOW - timedelta(days=15)))
    for ref in convs.values():
        source.upsert(ref)
    return source


def make_engine(source: InMemoryWinbackSource | None = None, **kw: object) -> WinbackEngine:
    """标准引擎：默认发送/渲染桩 + 固定时钟（参数可注入）。"""
    base: dict = dict(render=lambda round_no, tier: f"唤醒第{round_no}轮",
                      send=lambda uid, text: True, clock=lambda: NOW)
    base.update(kw)
    return WinbackEngine(source if source is not None else make_source(), **base)  # type: ignore[arg-type]


def scan_and_fire(engine: WinbackEngine, *, rounds: int = 1) -> None:
    """建单并推进 N 轮（sequence_days 默认 [0,4,10] 的触发时刻）。"""
    engine.scan_silent(now=NOW)
    moments = (NOW, NOW + timedelta(days=4, minutes=1), NOW + timedelta(days=10, minutes=1))
    for i in range(rounds):
        engine.fire_due(now=moments[i])


class TestSilentScan:
    """等价类/边界：静默判定与序列建单（§6.4 scan_silent）。"""

    def test_scan_creates_first_round_inmail(self) -> None:
        """静默 15 天（>14）→ 建 round=1 任务：阿里渠道映射 inmail、tier=0。"""
        engine = make_engine()
        assert engine.scan_silent(now=NOW) == 1
        (task,) = engine.tasks_of("c-silent")
        assert (task.round, task.channel, task.incentive_tier) == (1, "inmail", 0)

    def test_scan_boundary_and_email_channel(self) -> None:
        """边界：恰好 14 天不判静默；非阿里渠道 → email（FR-SITE-004 复用）。"""
        source = make_source(c_edge=ConversationRef(
            conversation_uid="c-edge", account_id=101, customer_id=9002,
            channel="alibaba", last_inbound_at=NOW - timedelta(days=14)))
        source.upsert(ConversationRef(
            conversation_uid="c-mail", account_id=101, customer_id=9003,
            channel="site", last_inbound_at=NOW - timedelta(days=30)))
        engine = make_engine(source)
        engine.scan_silent(now=NOW)
        assert engine.tasks_of("c-edge") == ()
        assert engine.tasks_of("c-mail")[0].channel == "email"

    def test_scan_idempotent_unique_conv_round(self) -> None:
        """并发/幂等：同会话重复扫描不重复建单（UNIQUE conv+round）。"""
        engine = make_engine()
        assert engine.scan_silent(now=NOW) == 1
        assert engine.scan_silent(now=NOW + timedelta(days=1)) == 0
        assert len(engine.tasks_of("c-silent")) == 1


class TestSequenceProgression:
    """等价类/边界：唤醒序列多轮 + 递进激励 + 停止条件④。"""

    def test_three_rounds_with_tier_escalation_and_offsets(self) -> None:
        """递进激励 tier=[0,1,2]；轮次按 sequence_days [0,4,10]：+3d 未到期、+4d 到期。"""
        engine = make_engine()
        engine.scan_silent(now=NOW)
        engine.fire_due(now=NOW)
        assert engine.pending_due(now=NOW + timedelta(days=3)) == []
        assert engine.pending_due(now=NOW + timedelta(days=4, minutes=1))
        engine.fire_due(now=NOW + timedelta(days=4, minutes=1))
        engine.fire_due(now=NOW + timedelta(days=10, minutes=1))
        assert [t.incentive_tier for t in engine.tasks_of("c-silent")] == [0, 1, 2]
        assert engine.stop_reason_of("c-silent") == "exhausted"          # 序列走完归档

    def test_render_missing_round_skipped(self) -> None:
        """异常：模板渲染失败本轮 skipped 不外发残文（序列挂起交由知识缺口回流）。"""
        engine = make_engine(render=lambda round_no, tier: None)
        engine.scan_silent(now=NOW)
        outcomes = engine.fire_due(now=NOW)
        assert outcomes[0].result == "skipped_render"
        assert engine.fire_due(now=NOW + timedelta(days=4, minutes=1)) == []


class TestStopConditions:
    """并发/竞态：触发时状态复核（三停止条件优先于发送，§6.4）。"""

    def test_stop_on_reply(self) -> None:
        """停止条件①：客户已回复 → 序列 finished，stop_reason=replied（§9 用例）。"""
        engine = make_engine()
        engine.scan_silent(now=NOW)
        engine.fire_due(now=NOW)
        engine.source.record_inbound("c-silent", at=NOW + timedelta(hours=1))
        engine.fire_due(now=NOW + timedelta(days=4, minutes=1))
        assert engine.stop_reason_of("c-silent") == "replied"

    def test_stop_on_human_and_refusal(self) -> None:
        """停止条件②③：人工接管 → human；明确拒绝意图 → refused（不再打扰）。"""
        engine = make_engine()
        engine.scan_silent(now=NOW)
        engine.fire_due(now=NOW)
        engine.source.set_state("c-silent", "human_managed")
        engine.fire_due(now=NOW + timedelta(days=4, minutes=1))
        assert engine.stop_reason_of("c-silent") == "human"
        engine2 = make_engine()
        engine2.scan_silent(now=NOW)
        engine2.fire_due(now=NOW)
        engine2.source.set_refusal(9001, True)
        engine2.fire_due(now=NOW + timedelta(days=4, minutes=1))
        assert engine2.stop_reason_of("c-silent") == "refused"


class TestApprovalAndCaps:
    """降级：tier2 审批门禁与日上限（叠加风控双保险，§8）。"""

    def test_tier2_gated_by_approval(self) -> None:
        """tier=2 无主管批准 → 等待重排（§9 用例）；批准后正常让利发送。"""
        engine = make_engine(approval_check=lambda token: False, approval_retry_minutes=30)
        scan_and_fire(engine, rounds=2)
        blocked = engine.fire_due(now=NOW + timedelta(days=10, minutes=1))
        assert blocked[0].result == "rescheduled_approval"
        assert engine.stop_reason_of("c-silent") is None                 # 序列仍在等待
        allowed = make_engine(approval_check=lambda token: True)
        scan_and_fire(allowed, rounds=2)
        allowed.fire_due(now=NOW + timedelta(days=10, minutes=1))
        third = allowed.tasks_of("c-silent")[2]
        assert third.incentive_tier == 2 and third.status == "finished"  # 让利轮外发后序列归档
        assert allowed.stop_reason_of("c-silent") == "exhausted"

    def test_daily_cap_and_risk_reject_reschedule(self) -> None:
        """日上限顺延（不硬发、到期再发）；风控拒绝 → 抖动重排不丢失。"""
        source = make_source(c2=ConversationRef(
            conversation_uid="c-2", account_id=101, customer_id=9004,
            channel="alibaba", last_inbound_at=NOW - timedelta(days=20)))
        engine = make_engine(source, daily_cap=1)
        engine.scan_silent(now=NOW)
        first = engine.fire_due(now=NOW)
        assert [o.result for o in first] == ["sent", "skipped_cap"]      # 同批两单：1 发 1 顺延
        retried = engine.fire_due(now=NOW + timedelta(days=1, hours=1, minutes=1))
        assert [o.result for o in retried] == ["sent"]
        calls = {"n": 0}

        def flaky_send(uid: str, text: str) -> bool:
            calls["n"] += 1
            return calls["n"] > 1                                        # 首次拒绝（风控）

        engine_risk = make_engine(send=flaky_send, risk_retry_minutes=30)
        engine_risk.scan_silent(now=NOW)
        assert engine_risk.fire_due(now=NOW)[0].result == "rescheduled_risk"
        assert engine_risk.fire_due(now=NOW + timedelta(minutes=31))[0].result == "sent"
