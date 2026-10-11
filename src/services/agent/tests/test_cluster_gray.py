# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""灰度上线机制单元测试（FR-SC-007，SR-SC-007 §6/§9；Phase 4 TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：组级配置生效隔离（试点新策略/对照基线）、门禁三硬指标全过 → gate_passed、
  放量 pilot→wave2→full、决策记录四要素（时间/动作/依据/审批人）；
- 边界：观察窗恰满（=14 天）可判定、7 天短窗可配、可用率/会话量恰等阈值 PASS（≥ 口径）、
  重启分批 ≤ batch 大小；
- 异常：任一硬指标失败 → 试点组暂停 + gate_failed + 告警、存活率前置失败、
  观察窗未满抢跑拦截、状态机非法迁移拦截（未过门禁放量/重复放量）、重复回滚幂等拦截；
- 并发：放量后状态机流转使重复放量被拦截（分布式锁语义的进程内等价）；
- 降级：一键回滚——组配置摘除回落全局默认、灰度组实例暂停打回 control、对照组零感知。

验收口径映射：SRS §16.3/§16.5（门禁三指标=封号 0/可用率 ≥80%/会话量达标）、
§15.3（决策记录=付款节点证据链）。
实现方案：docs/07-实现方案/SC/SR-SC-007-灰度上线机制.md §6、§7、§9
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from services.agent.core.cluster.gray import (
    GateMetrics,
    GrayError,
    GrayReleaseManager,
)
from services.agent.core.cluster.registry import InstanceRegistry
from services.agent.core.engine import InMemoryAlerts

NOW = datetime(2026, 10, 11, 9, 0, 0)
WINDOW_DAYS = 14
AFTER_WINDOW = NOW + timedelta(days=WINDOW_DAYS + 1)


def make_world(n: int = 28):
    """标准装配：28 实例注册表（全部 running）+ 可控指标源 + 告警 + 重启记录。"""
    registry = InstanceRegistry()
    for seq in range(1, n + 1):
        registry.register(seq=seq, account_id=7000 + seq,
                          proxy_ip=f"10.8.0.{seq}", host_node=f"runner-{((seq - 1) // 3) + 1:02d}")
        registry.mark(7000 + seq, status="running", reason="seed")
    metrics = GateMetrics(ban_or_restricted=0, reply_usable_rate=0.86,
                          sessions_per_account_day=16.2, instance_alive_rate=1.0)
    alerts = InMemoryAlerts()
    restarts: list[list[int]] = []
    manager = GrayReleaseManager(
        registry=registry,
        metrics=lambda plan: metrics,
        alerts=alerts,
        restart_hook=lambda ids: restarts.append(list(ids)),
    )
    manager.metrics_ref = metrics  # 测试改指标用
    manager.restarts_ref = restarts
    return manager, registry, alerts


def create_default(manager: GrayReleaseManager):
    """默认计划：前 10 账号试点，14 天观察窗，组级覆盖 strategy.version。"""
    pilot = [7000 + s for s in range(1, 11)]
    return manager.create_plan(
        plan_no="GRAY-P4-2026-001", target_version="strategy:v2",
        baseline_version="strategy:v1", pilot_account_ids=pilot,
        observe_window_days=WINDOW_DAYS,
        target_config={"strategy.version": "v2"}, now=NOW)


def pass_gate(manager: GrayReleaseManager, plan) -> None:
    """走完观察窗 + 门禁判定的标准前置。"""
    manager.start_observing(plan.plan_no, now=NOW)
    manager.evaluate_gate(plan.plan_no, now=AFTER_WINDOW)


# ───────────────────────── 等价类：分组与配置隔离 ─────────────────────────


class TestGrouping:
    """分组标签 pilot/wave2/full 配置生效隔离（SR-SC-007 §6-1/1b）。"""

    def test_group_config_isolation(self) -> None:
        """配置隔离：试点组解析到覆盖键（新版本），对照组回落全局键（基线版本）。"""
        manager, registry, _ = make_world()
        plan = create_default(manager)
        assert plan.state == "pilot_running"
        manager.set_global_config("strategy.version", "v1")
        assert registry.group_counts() == {"pilot": 10, "control": 18}  # 打标生效
        assert manager.resolve_config("strategy.version", "pilot") == "v2"
        assert manager.resolve_config("strategy.version", "control") == "v1"


# ───────────────────────── 门禁：三硬指标判定 ─────────────────────────


class TestGate:
    """放量门禁三硬指标（封号=0 / 可用率 ≥0.80 / 会话量 ≥15）任一失败暂停。"""

    def test_gate_all_pass(self) -> None:
        """等价类：三指标全达标 → passed、状态 gate_passed、决策 gate_pass 落账。"""
        manager, registry, _ = make_world()
        plan = create_default(manager)
        manager.start_observing(plan.plan_no, now=NOW)
        report = manager.evaluate_gate(plan.plan_no, now=AFTER_WINDOW)
        assert report["passed"] is True
        assert plan.state == "gate_passed"
        assert [c["metric"] for c in report["checks"]] == [
            "ban_signal", "reply_usable_rate", "sessions_per_account_day"]
        assert manager.decisions[-1].action == "gate_pass"

    @pytest.mark.parametrize(
        ("field", "value"), [
            ("ban_or_restricted", 1),            # 封号信号出现 → 一票否决
            ("reply_usable_rate", 0.79),         # 可用率差 0.01
            ("sessions_per_account_day", 14.9),  # 会话量未达标
        ])
    def test_gate_any_failure_pauses_pilot(self, field: str, value) -> None:
        """异常：任一硬指标失败 → passed=False、试点组实例暂停、告警、gate_failed。"""
        manager, registry, alerts = make_world()
        plan = create_default(manager)
        manager.start_observing(plan.plan_no, now=NOW)
        setattr(manager.metrics_ref, field, value)
        report = manager.evaluate_gate(plan.plan_no, now=AFTER_WINDOW)
        assert report["passed"] is False
        assert plan.state == "gate_failed"
        pilot_statuses = {registry.get(a).status for a in plan.pilot_account_ids}
        assert pilot_statuses == {"paused"}  # 试点组已全部暂停
        assert alerts.last(level="P1") is not None
        assert manager.decisions[-1].action == "gate_fail"

    def test_gate_alive_rate_precheck_fail(self) -> None:
        """前置门禁：实例存活率 <1.0 直接判失败（extra_fail=instance_alive_rate）。"""
        manager, registry, _ = make_world()
        plan = create_default(manager)
        manager.start_observing(plan.plan_no, now=NOW)
        manager.metrics_ref.instance_alive_rate = 0.95
        report = manager.evaluate_gate(plan.plan_no, now=AFTER_WINDOW)
        assert report["passed"] is False
        assert report["extra_fail"] == "instance_alive_rate"

    def test_gate_before_window_ends_rejected(self) -> None:
        """边界/异常：观察窗未满（第 13 天）抢跑判定 → OBSERVE_NOT_FINISHED。"""
        manager, _, _ = make_world()
        plan = create_default(manager)
        manager.start_observing(plan.plan_no, now=NOW)
        with pytest.raises(GrayError, match="OBSERVE_NOT_FINISHED"):
            manager.evaluate_gate(plan.plan_no, now=NOW + timedelta(days=13))

    def test_gate_window_boundary_exact_and_short_window(self) -> None:
        """边界：观察窗恰满（=start+N 天）允许判定；窗长可配（7 天档，SR §7 配置外置）。"""
        manager, _, _ = make_world()
        plan = manager.create_plan(
            plan_no="GRAY-P4-2026-007", target_version="strategy:v2",
            baseline_version="strategy:v1", pilot_account_ids=[7000 + s for s in range(1, 11)],
            observe_window_days=7, target_config={}, now=NOW)
        manager.start_observing(plan.plan_no, now=NOW)
        report = manager.evaluate_gate(plan.plan_no, now=NOW + timedelta(days=7))
        assert report["passed"] is True

    def test_gate_boundary_thresholds_equal_pass(self) -> None:
        """边界：可用率恰 0.80、会话量恰 15.0 → 均 PASS（≥ 达标口径，§16.3）。"""
        manager, _, _ = make_world()
        plan = create_default(manager)
        manager.start_observing(plan.plan_no, now=NOW)
        manager.metrics_ref.reply_usable_rate = 0.80
        manager.metrics_ref.sessions_per_account_day = 15.0
        report = manager.evaluate_gate(plan.plan_no, now=AFTER_WINDOW)
        assert report["passed"] is True


# ───────────────────────── 放量与决策记录 ─────────────────────────


class TestRollout:
    """放量动作：调组 → 配置生效 → 分批重启 → 新观察窗（SR-SC-007 §6-3）。"""

    def test_rollout_pilot_to_wave2(self) -> None:
        """放量第一批：pilot → wave2（余 18 账号）、分批重启 ≤5、进入新观察窗。"""
        manager, registry, _ = make_world()
        plan = create_default(manager)
        pass_gate(manager, plan)
        result = manager.rollout(plan.plan_no, approver="王审核", now=AFTER_WINDOW)
        assert result["to_group"] == "wave2"
        assert result["accounts"] == 18
        assert registry.group_counts()["wave2"] == 18
        assert all(len(b) <= 5 for b in manager.restarts_ref)  # 分批错峰重启
        assert plan.state == "observing"  # 新观察窗开启
        last = manager.decisions[-1]
        assert last.action == "rollout" and last.approver == "王审核"

    def test_rollout_wave2_to_full(self) -> None:
        """放量第二批：wave2 门禁再过 → full（28 账号全量）。"""
        manager, registry, _ = make_world()
        plan = create_default(manager)
        pass_gate(manager, plan)
        manager.rollout(plan.plan_no, approver="王审核", now=AFTER_WINDOW)
        manager.metrics_ref.instance_alive_rate = 1.0
        manager.evaluate_gate(plan.plan_no, now=AFTER_WINDOW + timedelta(days=WINDOW_DAYS + 1))
        result = manager.rollout(plan.plan_no, approver="李批准",
                                 now=AFTER_WINDOW + timedelta(days=15))
        assert result["to_group"] == "full"
        assert registry.group_counts()["full"] == 28

    def test_rollout_requires_gate_passed(self) -> None:
        """异常：未过门禁放量 → PLAN_STATE_INVALID（状态机拦截，兼防重复提交）。"""
        manager, _, _ = make_world()
        plan = create_default(manager)
        pass_gate(manager, plan)
        plan.state = "observing"  # 模拟放量后状态回退/并发重复请求
        with pytest.raises(GrayError, match="PLAN_STATE_INVALID"):
            manager.rollout(plan.plan_no, approver="王审核", now=AFTER_WINDOW)


class TestDecisionLedger:
    """放量决策记录：决策时间/依据/审批人（§15.3 付款节点证据链）。"""

    def test_decision_record_fields_complete_and_ordered(self) -> None:
        """等价类：决策记录含时间/动作/依据（门禁快照）/审批人；台账只追加时序单调。"""
        manager, _, _ = make_world()
        plan = create_default(manager)
        pass_gate(manager, plan)
        manager.rollout(plan.plan_no, approver="王审核", now=AFTER_WINDOW)
        rec = manager.decisions[-1]
        assert rec.decided_at == AFTER_WINDOW
        assert rec.approver == "王审核"
        assert rec.evidence["to_group"] == "wave2"
        assert "gate_checks" in rec.evidence  # 依据=门禁快照
        times = [d.decided_at for d in manager.decisions]
        assert times == sorted(times)  # 只追加、时序单调（审计哈希链语义）


# ───────────────────────── 回滚：分钟级 + 对照组零感知 ─────────────────────────


class TestRollback:
    """一键回滚：组配置摘除 + 灰度组暂停 + 对照组零感知（SR-SC-007 §6-4）。"""

    def test_rollback_removes_overrides_and_pauses_gray(self) -> None:
        """降级主路径：配置回落全局、灰度组实例暂停打回 control、对照组不动、告警+决策。"""
        manager, registry, alerts = make_world()
        plan = create_default(manager)
        pass_gate(manager, plan)
        manager.rollout(plan.plan_no, approver="王审核", now=AFTER_WINDOW)
        report = manager.rollback(plan.plan_no, approver="值班管理员",
                                  reason="试点组可用率跌破 80%", now=AFTER_WINDOW)
        assert report["elapsed_sec"] >= 0
        assert plan.state == "rolled_back"
        assert manager.resolve_config("strategy.version", "pilot") == "v1"  # 覆盖已摘除
        assert registry.get(7001).group_tag == "control"
        assert registry.get(7001).status == "paused"
        assert registry.get(7028).group_tag == "control"  # 对照组标签不变
        assert registry.get(7028).status == "running"     # 对照组零感知
        assert manager.decisions[-1].action == "rollback"
        assert manager.decisions[-1].evidence["reason"] == "试点组可用率跌破 80%"
        assert alerts.last(level="P1") is not None

    def test_rollback_idempotent_intercept(self) -> None:
        """异常/幂等：已回滚计划再回滚 → PLAN_STATE_INVALID（不重复执行）。"""
        manager, _, _ = make_world()
        plan = create_default(manager)
        manager.start_observing(plan.plan_no, now=NOW)
        manager.rollback(plan.plan_no, approver="值班管理员", reason="演练", now=NOW)
        with pytest.raises(GrayError, match="PLAN_STATE_INVALID"):
            manager.rollback(plan.plan_no, approver="值班管理员", reason="再演一次", now=NOW)
