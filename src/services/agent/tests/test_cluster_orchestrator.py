# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""集群编排器单元测试（FR-SC-001，SR-SC-001 §6.2/§6.3/§6.5/§9；Phase 4 TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：compose override 模板渲染（容器名/Cookie 卷/代理 env 占位）、批量供给 28 实例
  全路径与灰度批次圈定、实例级 restart、kill 单实例演练其余 27 实例零波及（内存编排模拟，
  对应 SRS §16.5 单点故障演练验收）；
- 边界：批量供给按轮切分（batch_max_per_round 首尾轮）、重启退避序列 30/60/120s、
  恰超上限（第 4 次）转 error、期望非 running 实例调谐跳过；
- 异常：IP 漂移/登录失效不自动重启（转 error 人工）、重启超限转 error + 高级告警；
- 并发：互斥锁防重复操作（并发持有者期间 restart 被拒 INST_BUSY）；
- 降级：reconcile_all 单实例异常就地捕获，其余实例继续调谐（故障隔离 NFR-REL-02）。

验收口径映射：SRS §16.5「kill 单实例 → 其余 27 实例零中断 + 自动恢复」。
实现方案：docs/07-实现方案/SC/SR-SC-001-Agent集群.md §6.2/§6.3/§6.5、§9
"""

from __future__ import annotations

from datetime import datetime

import pytest

from services.agent.core.cluster.orchestrator import (
    ClusterConfig,
    ClusterOrchestrator,
    FakeContainerOps,
    InstanceBusy,
)
from services.agent.core.cluster.registry import InstanceRegistry
from services.agent.core.engine import InMemoryAlerts

NOW = datetime(2026, 10, 11, 9, 0, 0)


def make_orchestrator(
    n: int = 28, **cfg_overrides
) -> tuple[ClusterOrchestrator, InstanceRegistry, FakeContainerOps, InMemoryAlerts]:
    """标准装配：n 实例注册表 + 内存容器操作模拟 + 告警记录。"""
    registry = InstanceRegistry()
    for seq in range(1, n + 1):
        registry.register(seq=seq, account_id=7000 + seq,
                          proxy_ip=f"10.8.0.{seq}", host_node=f"runner-{((seq - 1) // 3) + 1:02d}",
                          gray_batch=1)  # 默认批次 1（未分批的实例均属首批供给范围）
    ops = FakeContainerOps()
    alerts = InMemoryAlerts()
    config = ClusterConfig(**cfg_overrides) if cfg_overrides else ClusterConfig()
    orch = ClusterOrchestrator(registry, ops, alerts=alerts, config=config)
    return orch, registry, ops, alerts


# ───────────────────────── 等价类：模板渲染与批量供给 ─────────────────────────


class TestOverrideRendering:
    """compose override 模板：容器名/Cookie 卷/代理 env 三要素（SR-SC-001 §6.2）。"""

    def test_render_contains_container_and_volume(self) -> None:
        """seq=07 渲染：服务名/容器名 agent-runner-07、独立卷 cookie-07 挂载。"""
        orch, registry, _, _ = make_orchestrator(7)
        inst = registry.get(7007)
        override = orch.render_override(inst)
        svc = override["services"]["agent-runner-07"]
        assert svc["container_name"] == "agent-runner-07"
        assert "cookie-07:/data/openclaw/cookies" in svc["volumes"]
        assert "cookie-07" in override["volumes"]

    def test_render_proxy_env_with_placeholder(self) -> None:
        """代理 env 注入绑定 IP，凭证一律占位（NFR-SEC-04 零明文）。"""
        orch, registry, _, _ = make_orchestrator(7)
        override = orch.render_override(registry.get(7007))
        env = "|".join(override["services"]["agent-runner-07"]["environment"])
        assert "ACCOUNT_ID=7007" in env
        assert "10.8.0.7" in env
        assert "<PROXY-USER>" in env and "<PROXY-PASS>" in env


class TestBatchProvision:
    """批量创建 28 实例配置生成与分轮供给（SR-SC-001 §6.2/§6.8）。"""

    def test_provision_28_instances_all_running(self) -> None:
        """批量供给 28 实例：全部 running、ops 全部 start、批次数 = ceil(28/5)=6。"""
        orch, registry, ops, _ = make_orchestrator(28, batch_max_per_round=5)
        report = orch.provision(gray_batch=1)
        assert report["created"] == 28
        assert registry.by_status() == {"running": 28}
        assert len(ops.calls_of("start")) == 28
        assert report["batches"] == 6

    def test_provision_respects_gray_batch_scope(self) -> None:
        """供给按 gray_batch 圈定范围：batch=1 只供给批次 1；批次 2 实例保持 offline。"""
        orch, registry, ops, _ = make_orchestrator(28, batch_max_per_round=5)
        for seq in range(11, 29):
            registry.set_gray(7000 + seq, gray_batch=2)  # 其余 18 实例归入批次 2
        report = orch.provision(gray_batch=1)
        assert report["created"] == 10
        assert registry.get(7010).status == "running"
        assert registry.get(7011).status == "offline"

    def test_provision_waits_between_rounds(self) -> None:
        """轮间观察窗：轮与轮之间等待 rollout_gap（分轮错峰，联动 FR-SC-007）。"""
        orch, registry, ops, _ = make_orchestrator(6, batch_max_per_round=2, rollout_gap_s=7.5)
        report = orch.provision(gray_batch=1)
        assert report["batches"] == 3
        assert orch.sleeps == [7.5, 7.5]  # 轮间隔 2 次（末轮不等待）


# ───────────────────────── 等价类：实例级操作 ─────────────────────────


class TestInstanceLifecycle:
    """实例级启停与重启（FR-SC-001 条款；API 底座）。"""

    def test_restart_keeps_running(self) -> None:
        """restart：running → running（重启后状态恢复）。"""
        orch, registry, ops, _ = make_orchestrator(1)
        orch.start_instance(7001)
        result = orch.restart_instance(7001)
        assert result.ok and result.status == "running"
        assert ops.is_running("runner-01", "agent-runner-01")


# ───────────────────────── 单点故障演练：kill 单实例其余 27 不受影响 ─────────────────────────


class TestKillDrill:
    """kill 单实例演练（SRS §16.5 验收：其余 27 实例零中断 + 自动恢复；内存编排模拟）。"""

    def test_kill_one_other_27_untouched_and_recovered(self) -> None:
        """kill agent-runner-07 → 调谐仅触碰 07 并自动恢复：其余 27 实例 running 零操作。"""
        orch, registry, ops, _ = make_orchestrator(28)
        orch.provision(gray_batch=1)
        ops.clear_calls()  # 只考察演练窗口内的容器操作
        ops.kill("runner-02", "agent-runner-07")  # 宿主机演练：单实例进程死亡
        summary = orch.reconcile_all(now=NOW)
        touched = {c for _, c in ops.calls_of("stop") + ops.calls_of("start")}
        assert touched == {"agent-runner-07"}
        assert summary["restarted"] == [7007]
        assert registry.by_status() == {"running": 28}

    def test_kill_drill_backoff_sequence_recorded(self) -> None:
        """退避序列：连续 3 次故障退避 30/60/120s（base×2^(n-1)，SR §6.5）。"""
        orch, registry, ops, _ = make_orchestrator(1, backoff_base_s=30)
        orch.start_instance(7001)
        for expected_delay in (30.0, 60.0, 120.0):
            ops.kill("runner-01", "agent-runner-01")
            assert orch.reconcile_one(7001, now=NOW) == "restarted"
            assert orch.sleeps[-1] == expected_delay
        assert registry.get(7001).restart_count == 3


# ───────────────────────── 异常：重启超限 / 漂移与登录失效不自动重启 ─────────────────────────


class TestFailureEscalation:
    """故障分级恢复：超限转 error；IP 漂移/登录失效立即隔离转人工（SR-SC-001 §6.5）。"""

    def test_restart_exhausted_transfers_to_error_with_alert(self) -> None:
        """第 4 次故障（max=3）→ 不再重启，转 error + high 告警（人工接管）。"""
        orch, registry, ops, alerts = make_orchestrator(1, restart_max_attempts=3)
        orch.start_instance(7001)
        for _ in range(3):
            ops.kill("runner-01", "agent-runner-01")
            assert orch.reconcile_one(7001, now=NOW) == "restarted"
        ops.kill("runner-01", "agent-runner-01")
        assert orch.reconcile_one(7001, now=NOW) == "restart_exhausted"
        assert registry.get(7001).status == "error"
        assert alerts.last(level="high") is not None

    def test_ip_drift_isolates_without_restart(self) -> None:
        """IP 漂移：立即隔离转 error，零 start 调用（严禁自动换 IP 硬切，NFR-SEC-10）。"""
        orch, registry, ops, alerts = make_orchestrator(1)
        orch.start_instance(7001)
        action = orch.reconcile_one(7001, now=NOW, egress_ip="6.6.6.6")
        assert action == "isolated_error"
        assert registry.get(7001).status == "error"
        assert ops.calls_of("start") == []
        assert alerts.last(level="high") is not None

    def test_login_expired_isolates_without_restart(self) -> None:
        """进程在但登录失效：不暴力重登，隔离转 error（§6.5 决策②）。"""
        orch, registry, ops, _ = make_orchestrator(1)
        orch.start_instance(7001)
        action = orch.reconcile_one(7001, now=NOW, login_ok=False)
        assert action == "isolated_error"
        assert registry.get(7001).status == "error"
        assert ops.calls_of("start") == []

    def test_desired_not_running_skipped(self) -> None:
        """期望非 running（人工暂停）实例：调谐跳过不干预（期望态优先）。"""
        orch, registry, ops, _ = make_orchestrator(1)
        orch.start_instance(7001)
        orch.stop_instance(7001)
        ops.kill("runner-01", "agent-runner-01")
        assert orch.reconcile_one(7001, now=NOW) == "skipped"
        assert registry.get(7001).restart_count == 0


# ───────────────────────── 并发与降级 ─────────────────────────


class TestConcurrencyAndDegradation:
    """并发互斥与单实例异常隔离（NFR-REL-02 调谐循环逐实例捕获）。"""

    def test_concurrent_restart_mutual_exclusion(self) -> None:
        """并发：同实例互斥锁——第二次操作被拒 INST_BUSY（管理操作互斥）。"""
        orch, _, _, _ = make_orchestrator(1)
        orch.start_instance(7001)
        orch.acquire_lock(7001)  # 模拟并发持有者（调谐循环 vs 人工操作）
        with pytest.raises(InstanceBusy):
            orch.restart_instance(7001)
        orch.release_lock(7001)
        assert orch.restart_instance(7001).ok

    def test_reconcile_all_swallows_single_instance_error(self) -> None:
        """降级：单实例调谐抛错（探针/容器接口异常）就地捕获，其余 27 实例照常。"""
        orch, registry, ops, _ = make_orchestrator(28)
        orch.provision(gray_batch=1)
        ops.boom.add("agent-runner-12")  # 注入单点故障：该实例探针抛异常
        summary = orch.reconcile_all(now=NOW)
        assert summary["errors"] == ["agent-runner-12"]
        assert len(summary["healthy"]) == 27
