# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""实例注册表单元测试（FR-SC-001，SR-SC-001 §4/§6.1/§9；Phase 4 TDD 阶段①用例）。

五类场景齐备（等价类 / 边界 / 异常 / 并发 / 降级）：
- 等价类：注册 28 实例绑定（account↔container↔cookie↔proxy_ip 一一对应）、
  状态机合法迁移全路径（offline→running→paused→running / error 人工恢复）、
  漂移检测正常路（IP 一致无动作）；
- 边界：seq=1 与 seq=28 首尾合法（0/29/负数域外拒绝）、恰等重复绑定拒绝、
  故障窗口恰过期计数清零；
- 异常：非法迁移拒绝且状态不变并留审计、proxy_ip/账号重复绑定抛 InvariantViolation、
  IP 漂移 → 立即暂停 + 高级告警 + 审计（绝不自动换 IP——NFR-SEC-10）；
- 并发：28 线程并发注册无竞态（注册表为唯一事实源）；
- 降级：漂移后绑定关系保留待人工重绑（不丢不换，配置变更流程承接）。

验收口径映射：SRS §16.5「28 实例稳定运行 / 集群单点故障演练」数据面基座；§17 R-06。
实现方案：docs/07-实现方案/SC/SR-SC-001-Agent集群.md §4、§6.1、§9
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from services.agent.core.cluster.registry import (
    IllegalTransition,
    InstanceNotFound,
    InstanceRegistry,
    InvariantViolation,
)

NOW = datetime(2026, 10, 11, 9, 0, 0)


def make_registry(n: int = 28) -> InstanceRegistry:
    """标准装配：n 个实例（默认 28）全部注册为 offline。"""
    registry = InstanceRegistry()
    for seq in range(1, n + 1):
        registry.register(seq=seq, account_id=7000 + seq,
                          proxy_ip=f"10.8.{seq // 256}.{seq % 256}",
                          host_node=f"runner-{((seq - 1) // 3) + 1:02d}")
    return registry


# ───────────────────────── 等价类：绑定不变式与合法迁移 ─────────────────────────


class TestBindingInvariants:
    """account↔container↔cookie↔proxy_ip 一一对应（SR-SC-001 §6.1 决策 1）。"""

    def test_register_28_instances_binding_consistent(self) -> None:
        """28 实例注册：容器名/卷名与序号一致，proxy_ip 一实例一 IP。"""
        registry = make_registry(28)
        assert registry.count() == 28
        inst = registry.get(7007)
        assert inst.container_name == "agent-runner-07"
        assert inst.cookie_volume == "cookie-07"
        assert inst.proxy_ip == "10.8.0.7"
        assert inst.status == "offline"


class TestStateMachine:
    """观测态状态机 running/paused/error/offline 合法迁移（SR-SC-001 §4）。"""

    def test_legal_lifecycle_offline_running_paused_running(self) -> None:
        """合法生命周期：offline→running→paused→running。"""
        registry = make_registry(3)
        registry.mark(7001, status="running", reason="provision")
        registry.mark(7001, status="paused", reason="manual_stop")
        registry.mark(7001, status="running", reason="resume")
        assert registry.get(7001).status == "running"

    def test_legal_paths_to_error_and_manual_recovery(self) -> None:
        """error 转人工：running→error→paused / error→running 均合法（人工处置）。"""
        registry = make_registry(2)
        registry.mark(7001, status="running")
        registry.mark(7001, status="error", reason="restart_exhausted")
        registry.mark(7001, status="running", reason="human_recovered")
        registry.mark(7002, status="running")
        registry.mark(7002, status="error")
        registry.mark(7002, status="paused")
        assert registry.get(7001).status == "running"
        assert registry.get(7002).status == "paused"

    def test_state_change_writes_audit(self) -> None:
        """状态迁移全量留审计（NFR-SEC-03）：含迁移前后与原因。"""
        registry = make_registry(1)
        registry.mark(7001, status="running", reason="provision")
        rows = [r for r in registry.audit if r["event"] == "instance_state_change"]
        assert len(rows) == 1
        assert rows[0]["detail"] == "offline->running"
        assert rows[0]["reason"] == "provision"


# ───────────────────────── 边界：序号极值 / 恰等冲突 / 窗口过期 ─────────────────────────


class TestBoundaries:
    """边界：seq 首尾合法、越界拒绝、恰等重复绑定、窗口恰过期清零。"""

    @pytest.mark.parametrize("bad_seq", [0, 29, -1])
    def test_seq_out_of_range_rejected(self, bad_seq: int) -> None:
        """seq=0/29/负数拒绝（容器名命名域 agent-runner-01…28 之外；1/28 两端内含合法）。"""
        registry = InstanceRegistry()
        with pytest.raises(InvariantViolation):
            registry.register(seq=bad_seq, account_id=7001, proxy_ip="10.0.0.1",
                              host_node="runner-01")

    def test_duplicate_proxy_ip_rejected(self) -> None:
        """恰等冲突：第二个实例绑定同一 proxy_ip → InvariantViolation（DB UNIQUE 的应用层双保险）。"""
        registry = InstanceRegistry()
        registry.register(seq=1, account_id=7001, proxy_ip="10.0.0.1", host_node="runner-01")
        with pytest.raises(InvariantViolation, match="proxy_ip"):
            registry.register(seq=2, account_id=7002, proxy_ip="10.0.0.1", host_node="runner-01")

    def test_restart_window_expiry_resets_counter(self) -> None:
        """故障窗口恰过期：超过 window_sec 后计数清零重计（非无限累积）。"""
        registry = make_registry(1)
        n1 = registry.bump_restart(7001, now=NOW)
        n2 = registry.bump_restart(7001, now=NOW + timedelta(seconds=1800))
        assert (n1, n2) == (1, 2)
        n3 = registry.bump_restart(7001, now=NOW + timedelta(seconds=3601))
        assert n3 == 1  # 窗口（3600s）过期 → 计数重置


# ───────────────────────── 异常：非法迁移 / 漂移处置 ─────────────────────────


class TestIllegalTransitions:
    """非法迁移在应用层拒绝（防状态漂移，SR-SC-001 §4）。"""

    def test_offline_to_error_rejected_state_kept(self) -> None:
        """offline→error 非法（未运行实例无故障语义）；拒绝后状态不变。"""
        registry = make_registry(1)
        with pytest.raises(IllegalTransition):
            registry.mark(7001, status="error")
        assert registry.get(7001).status == "offline"

    def test_error_to_offline_rejected(self) -> None:
        """error→offline 非法（error 保留容器与卷待人工，不可直接销毁）。"""
        registry = make_registry(1)
        registry.mark(7001, status="running")
        registry.mark(7001, status="error")
        with pytest.raises(IllegalTransition):
            registry.mark(7001, status="offline")

    def test_get_unknown_instance_raises(self) -> None:
        """查询不存在实例 → InstanceNotFound（404 语义）。"""
        registry = make_registry(2)
        with pytest.raises(InstanceNotFound):
            registry.get(9999)


class TestDriftDetection:
    """出口 IP 漂移检测（SR-SC-001 §6.6：漂移即暂停，不自动换 IP——NFR-SEC-10）。"""

    def test_matching_ip_no_action(self) -> None:
        """等价类：观测 IP 与绑定一致 → 无动作（running 保持）。"""
        registry = make_registry(1)
        registry.mark(7001, status="running")
        drifted = registry.check_drift(7001, observed_ip="10.8.0.1", now=NOW)
        assert drifted is False
        assert registry.get(7001).status == "running"

    def test_ip_drift_pauses_and_alerts(self) -> None:
        """异常：观测 IP≠绑定 IP → 立即 paused + 告警回调 + 审计，不自动换绑。"""
        alerts: list[dict] = []
        registry = make_registry(1)
        registry.mark(7001, status="running")
        registry.on_alert = lambda level, type_, title, body: alerts.append(
            {"level": level, "type": type_, "title": title})
        drifted = registry.check_drift(7001, observed_ip="6.6.6.6", now=NOW)
        assert drifted is True
        assert registry.get(7001).status == "paused"
        assert alerts and alerts[0]["level"] == "high"
        assert any(r["event"] == "ip_drift" for r in registry.audit)

    def test_drift_keeps_binding_for_manual_rebind(self) -> None:
        """降级：漂移后绑定关系不变（proxy_ip 仍为原值），待人工走配置变更流程重绑。"""
        registry = make_registry(1)
        registry.mark(7001, status="running")
        registry.check_drift(7001, observed_ip="6.6.6.6", now=NOW)
        assert registry.get(7001).proxy_ip == "10.8.0.1"


# ───────────────────────── 并发：多线程注册 ─────────────────────────


class TestConcurrency:
    """并发：28 线程并发注册，无竞态（注册表为唯一事实源）。"""

    def test_concurrent_registration_all_land(self) -> None:
        """28 线程并发注册：28 条全部落表、绑定互不冲突。"""
        registry = InstanceRegistry()

        def worker(seq: int) -> None:
            registry.register(seq=seq, account_id=7000 + seq,
                              proxy_ip=f"10.8.0.{seq}", host_node="runner-01")

        threads = [threading.Thread(target=worker, args=(s,)) for s in range(1, 29)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert registry.count() == 28
