# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""销售策略 V2 分级驱动跟进单元测试（FR-SC-003②，SR-SC-003 §6.2/§7/§9；P4 第二批 TDD）。

五类覆盖（开发规范 §9.1）：等价类（A/B/C/D 节奏差异化、变体解析链）/ 边界（轮次
上限、越界取值）/ 异常（未知分级）/ 并发（分级变更重排：旧任务摘除、已发轮次不
补发）/ 降级（热更新新旧隔离不丢任务、渲染失败跳过、A 级主管可见、D 级防骚扰）。
结构性断言：V2 调度体复用 V1 FollowupScheduler 队列语义（扩展而非重写）。

实现方案：docs/07-实现方案/SC/SR-SC-003-销售策略引擎.md §6.2、§7、§9
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.agent.core.strategy.queue import FollowupScheduler
from services.agent.core.strategyv2.cadence import (
    DEFAULT_CADENCE,
    DEFAULT_MAX_ROUNDS,
    CadenceError,
    GradeCadence,
    GradeDrivenFollowup,
)

NOW = datetime(2026, 10, 11, 10, 0, 0)


def build_driven(grade: str = "B", **kw: object) -> tuple[GradeDrivenFollowup, GradeCadence, list[str], list[str]]:
    """标准装配：固定分级 + 记录型发送/A 级可见性钩子。"""
    sent: list[str] = []
    visible: list[str] = []
    cad = kw.pop("cadence", GradeCadence())
    base: dict = dict(
        render=lambda round_no: f"跟进模板第{round_no}轮",
        send=lambda uid, text: sent.append(uid) or True,
        on_a_visible=visible.append,
        clock=lambda: NOW,
    )
    base.update(kw)
    driven = GradeDrivenFollowup(cad, grade_of=lambda uid: grade, **base)  # type: ignore[arg-type]
    return driven, driven.cadence, sent, visible  # type: ignore[attr-defined]


class TestCadenceTable:
    """等价类：分级节奏表与变体解析链。"""

    def test_default_table_matches_spec(self) -> None:
        """默认表 = SR-SC-003 §7：A[1,2,5] B[1,3,7] C[2,5,10] D[3,7,14] + 轮次上限。"""
        assert DEFAULT_CADENCE == {"A": (1, 2, 5), "B": (1, 3, 7), "C": (2, 5, 10), "D": (3, 7, 14)}
        assert DEFAULT_MAX_ROUNDS == {"A": 5, "B": 4, "C": 4, "D": 3}

    def test_a_vs_d_delay_differentiated(self) -> None:
        """A级 1/2/5d 高频 vs D 级低频：首轮间隔 A=1d、D=3d（§9 差异化断言）。"""
        cad = GradeCadence()
        assert cad.delay_days("A", 1) == 1 and cad.delay_days("D", 1) == 3

    def test_variant_resolver_overrides_grade_key(self) -> None:
        """配置解析链：A/B 实验变体 > 分级键（仅处理组命中）。"""
        cad = GradeCadence(
            variant_resolver=lambda key, account_id: (2, 4) if key.endswith(".A") else None)
        assert cad.cadence_for("A", account_id=101) == (2, 4)
        assert cad.cadence_for("B", account_id=101) == (1, 3, 7)


class TestBoundaries:
    """边界/异常：越界取值与未知分级。"""

    def test_delay_out_of_range_and_unknown_grade_raise(self) -> None:
        """round 超 cadence 长度 / 未知分级（X）→ 异常（不静默取值）。"""
        cad = GradeCadence()
        with pytest.raises(CadenceError):
            cad.delay_days("A", 4)
        with pytest.raises(CadenceError):
            cad.cadence_for("X")

    def test_next_fire_at_cumulative_and_exhausted(self) -> None:
        """边界：A 级 done=1 → +2d；done=3（cadence 走完）→ None。"""
        cad = GradeCadence()
        assert cad.next_fire_at("A", done_rounds=1, from_time=NOW) == NOW + timedelta(days=2)
        assert cad.next_fire_at("A", done_rounds=3, from_time=NOW) is None


class TestGradeDrivenFollowup:
    """复用 V1 队列语义的分级驱动调度（§6.2 FollowupV2Mixin 语义）。"""

    def test_first_round_fire_a_vs_d(self) -> None:
        """A 级首跟进 = +1d 即触发，D 级 +2d 未到期（低频防骚扰）。"""
        driven_a, _, sent_a, _ = build_driven("A")
        driven_d, _, sent_d, _ = build_driven("D")
        driven_a.schedule_first("conv-A", now=NOW, grade="A")
        driven_d.schedule_first("conv-D", now=NOW, grade="D")
        assert [o.conversation_uid for o in driven_a.fire_due(now=NOW + timedelta(days=1, minutes=1))] == ["conv-A"]
        driven_d.fire_due(now=NOW + timedelta(days=2))
        assert sent_d == []

    def test_grade_a_visible_to_supervisor_only(self) -> None:
        """A 级任务主管可见（digest 钩子）；B 级不触发。"""
        driven_b, _, _, visible_b = build_driven("B")
        driven_b.schedule_first("conv-1", now=NOW, grade="B")
        assert visible_b == []
        driven_a, _, _, visible_a = build_driven("A")
        driven_a.schedule_first("conv-1", now=NOW, grade="A")
        assert visible_a == ["conv-1"]

    def test_full_chain_uses_grade_cadence(self) -> None:
        """A 级三轮触发时刻 = now+1d/+3d/+8d（1/2/5 累计），共发 3 轮。"""
        driven, _, sent, _ = build_driven("A")
        driven.schedule_first("conv-A", now=NOW, grade="A")
        driven.fire_due(now=NOW + timedelta(days=1, minutes=1))
        driven.fire_due(now=NOW + timedelta(days=3, minutes=1))
        driven.fire_due(now=NOW + timedelta(days=8, minutes=1))
        assert sent.count("conv-A") == 3

    def test_max_rounds_exhausted_goes_human(self) -> None:
        """边界：D 级 3 轮上限 → 第 3 轮后静默转人工（on_exhausted）。"""
        exhausted: list[str] = []
        driven, _, _, _ = build_driven("D", on_exhausted=exhausted.append)
        driven.schedule_first("conv-D", now=NOW, grade="D")
        driven.fire_due(now=NOW + timedelta(days=3, minutes=1))
        driven.fire_due(now=NOW + timedelta(days=10, minutes=1))
        driven.fire_due(now=NOW + timedelta(days=24, minutes=1))
        assert exhausted == ["conv-D"]

    def test_regrade_reschedules_pending(self) -> None:
        """并发：B→A 变更 → 旧 pending 摘除、按 A 节奏（cadence[1]=2d）重排、已发不补发。"""
        driven, _, sent, _ = build_driven("B")
        driven.schedule_first("conv-1", now=NOW, grade="B")
        driven.fire_due(now=NOW + timedelta(days=1, minutes=1))          # 第 1 轮已发
        plan = driven.reschedule_for_grade("conv-1", "A", now=NOW + timedelta(days=1, minutes=2))
        assert (plan.cancelled, plan.done_rounds, plan.delay_days) == (1, 1, 2)
        outcomes = driven.fire_due(now=NOW + timedelta(days=3, minutes=3))
        assert any(o.conversation_uid == "conv-1" and o.round_no == 2 for o in outcomes)

    def test_hot_update_applies_to_new_only(self) -> None:
        """降级/热更新：新会话取新 cadence；在途任务按旧节奏继续（不丢任务）。"""
        cad = GradeCadence()
        driven, _, _, _ = build_driven("A", cadence=cad)
        driven.schedule_first("conv-old", now=NOW, grade="A")            # 旧节奏 +1d
        cad.update_cadence("A", (5, 10, 20))                             # 热更新
        driven.schedule_first("conv-new", now=NOW, grade="A")            # 新节奏 +5d
        assert any(o.conversation_uid == "conv-old"
                   for o in driven.fire_due(now=NOW + timedelta(days=1, minutes=1)))
        assert all(o.conversation_uid != "conv-new"
                   for o in driven.fire_due(now=NOW + timedelta(days=2)))
        assert any(o.conversation_uid == "conv-new"
                   for o in driven.fire_due(now=NOW + timedelta(days=5, minutes=1)))

    def test_render_failure_skips_round(self) -> None:
        """降级：渲染失败本轮 skipped 不外发残文（复用 V1 语义）。"""
        driven, _, sent, _ = build_driven("A", render=lambda round_no: None)
        driven.schedule_first("conv-1", now=NOW, grade="A")
        outcomes = driven.fire_due(now=NOW + timedelta(days=1, minutes=1))
        assert outcomes[0].result == "skipped_render" and sent == []

    def test_reuses_v1_scheduler_type(self) -> None:
        """结构性断言：V2 调度体内复用 V1 FollowupScheduler（扩展注入而非重写）。"""
        driven, _, _, _ = build_driven("A")
        driven.schedule_first("conv-1", now=NOW, grade="A")
        assert driven.schedulers and all(isinstance(s, FollowupScheduler) for s in driven.schedulers)
