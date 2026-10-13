# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""集群 API 契约测试（FR-SC-001/007 对外接口面，SR-SC-001 §5、SR-SC-007 §5；Phase 4 TDD 阶段①）。

覆盖（FastAPI TestClient 端到端）：
- GET  /api/v1/agent/cluster/instances                     集群视图（total/by_status/items）；
- POST /api/v1/agent/cluster/instances/{id}/start|stop|restart   实例级启停重启；
- POST /api/v1/agent/cluster/batch                         批量操作对账（accepted/failed/report_id）；
- GET  /api/v1/agent/cluster/gray/status                   灰度状态视图；
- POST /api/v1/agent/cluster/gray/promote | /rollback      放量/回滚；
- 错误契约：404 NOT_FOUND / 409 PLAN_STATE_INVALID / 422 ILLEGAL_TRANSITION / 503 DOCKER_UNREACHABLE。

实现方案：docs/07-实现方案/SC/SR-SC-001-Agent集群.md §5、SR-SC-007 §5
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from services.agent.api.cluster_api import create_cluster_app
from services.agent.core.cluster.gray import GateMetrics, GrayReleaseManager
from services.agent.core.cluster.orchestrator import ClusterOrchestrator, FakeContainerOps
from services.agent.core.cluster.registry import InstanceRegistry
from services.agent.core.engine import InMemoryAlerts

NOW = datetime(2026, 10, 11, 9, 0, 0)
AFTER = NOW + timedelta(days=15)
PREFIX = "/api/v1/agent/cluster"


def build_client():
    """标准装配：28 实例 + 编排器 + 灰度管理器 + TestClient。"""
    registry = InstanceRegistry()
    for seq in range(1, 29):
        registry.register(seq=seq, account_id=7000 + seq,
                          proxy_ip=f"10.8.0.{seq}", host_node="runner-01", gray_batch=1)
    ops = FakeContainerOps()
    alerts = InMemoryAlerts()
    orch = ClusterOrchestrator(registry, ops, alerts=alerts)
    metrics = GateMetrics(ban_or_restricted=0, reply_usable_rate=0.86,
                          sessions_per_account_day=16.2, instance_alive_rate=1.0)
    gray = GrayReleaseManager(
        registry=registry,
        metrics=lambda plan: metrics,
        alerts=alerts,
    )
    app = create_cluster_app(registry=registry, orchestrator=orch, gray=gray, alerts=alerts)
    return TestClient(app), registry, orch, gray, ops


def seed_pilot_gate_pass(gray: GrayReleaseManager) -> str:
    """造一个已过门禁的灰度计划（放量接口的前置态）。"""
    plan = gray.create_plan(
        plan_no="GRAY-P4-2026-001", target_version="strategy:v2", baseline_version="strategy:v1",
        pilot_account_ids=[7000 + s for s in range(1, 11)],
        observe_window_days=14, target_config={"strategy.version": "v2"}, now=NOW)
    gray.start_observing(plan.plan_no, now=NOW)
    gray.evaluate_gate(plan.plan_no, now=AFTER)
    return plan.plan_no


# ───────────────────────── 集群视图与实例操作 ─────────────────────────


def test_instances_view_shape() -> None:
    """集群视图：total/by_status/items 字段契约（SR-SC-001 §5 出参节选口径）。"""
    client, registry, orch, _, _ = build_client()
    orch.provision(gray_batch=1)
    body = client.get(f"{PREFIX}/instances").json()
    assert body["total"] == 28
    assert body["by_status"] == {"running": 28}
    item = body["items"][0]
    for key in ("account_id", "container_name", "proxy_ip", "cookie_volume", "status",
                "gray_batch", "group_tag"):
        assert key in item


def test_instance_stop_start_roundtrip() -> None:
    """实例级启停：stop → paused、start → running（HTTP 200 语义）。"""
    client, _, orch, _, _ = build_client()
    orch.provision(gray_batch=1)
    assert client.post(f"{PREFIX}/instances/7001/stop").json()["status"] == "paused"
    assert client.post(f"{PREFIX}/instances/7001/start").json()["status"] == "running"


def test_unknown_instance_404() -> None:
    """错误契约：不存在实例 → 404 NOT_FOUND（中文消息）。"""
    client, *_ = build_client()
    resp = client.post(f"{PREFIX}/instances/9999/start")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_illegal_transition_422() -> None:
    """错误契约：error 实例直接 start → 422 ILLEGAL_TRANSITION。"""
    client, registry, orch, _, _ = build_client()
    orch.provision(gray_batch=1)
    registry.mark(7003, status="paused")
    registry.mark(7003, status="error", reason="drill")
    resp = client.post(f"{PREFIX}/instances/7003/start")
    assert resp.status_code == 422
    assert resp.json()["code"] == "ILLEGAL_TRANSITION"


def test_batch_operation_report() -> None:
    """批量操作：accepted/failed 对账 + report_id（SR-SC-005 消费通道）。"""
    client, _, orch, _, _ = build_client()
    orch.provision(gray_batch=1)
    resp = client.post(f"{PREFIX}/batch", json={"action": "stop", "account_ids": [7001, 7002, 9999]})
    body = resp.json()
    assert resp.status_code == 200
    assert body["accepted"] == 2
    assert [f["account_id"] for f in body["failed"]] == [9999]
    assert body["report_id"].startswith("br-")


def test_docker_failure_503() -> None:
    """错误契约：容器操作失败 → 503 DOCKER_UNREACHABLE。"""
    client, _, orch, _, ops = build_client()
    ops.fail_next_start("agent-runner-01", times=1)
    resp = client.post(f"{PREFIX}/instances/7001/start")
    assert resp.status_code == 503
    assert resp.json()["code"] == "DOCKER_UNREACHABLE"


# ───────────────────────── 灰度接口 ─────────────────────────


def test_gray_status_view() -> None:
    """灰度状态：计划号/状态/分组计数/决策数（看板数据源契约）。"""
    client, _, _, gray, _ = build_client()
    plan_no = seed_pilot_gate_pass(gray)
    body = client.get(f"{PREFIX}/gray/status").json()
    assert body["plan_no"] == plan_no
    assert body["state"] == "gate_passed"
    assert body["groups"]["pilot"] == 10
    assert body["decision_count"] >= 1


def test_gray_promote_requires_gate_pass() -> None:
    """放量门禁：观察窗未满直接 promote → 409（未过门禁/状态非法）。"""
    client, _, _, gray, _ = build_client()
    gray.create_plan(
        plan_no="GRAY-P4-2026-002", target_version="strategy:v2", baseline_version="strategy:v1",
        pilot_account_ids=[7000 + s for s in range(1, 11)],
        observe_window_days=14, target_config={}, now=NOW)
    gray.start_observing("GRAY-P4-2026-002", now=NOW)
    resp = client.post(f"{PREFIX}/gray/promote", json={"approver": "王审核"})
    assert resp.status_code == 409


def test_gray_promote_and_rollback_roundtrip() -> None:
    """放量→回滚：promote 200 返回目标分组；rollback 200 返回耗时；重复回滚 409。"""
    client, registry, _, gray, _ = build_client()
    seed_pilot_gate_pass(gray)
    promoted = client.post(f"{PREFIX}/gray/promote", json={"approver": "王审核"})
    assert promoted.status_code == 200
    assert promoted.json()["to_group"] == "wave2"
    rolled = client.post(f"{PREFIX}/gray/rollback",
                         json={"approver": "值班管理员", "reason": "演练回滚"})
    assert rolled.status_code == 200
    assert "elapsed_sec" in rolled.json()
    again = client.post(f"{PREFIX}/gray/rollback",
                        json={"approver": "值班管理员", "reason": "重复回滚"})
    assert again.status_code == 409
    assert again.json()["code"] == "PLAN_STATE_INVALID"
