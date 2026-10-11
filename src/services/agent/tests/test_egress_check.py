# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""出口 IP 自检纯函数单元测试（FR-AG-001，SR-AG-001 §6.3/§9）。

覆盖：
- 实测 = 期望：ok、连续失败计数清零；
- 漂移：EGRESS_IP_DRIFT 告警（expected/actual 入 detail）、计数 +1；
- 探测失败（actual=None）：EGRESS_PROBE_FAILED 告警、计数 +1；
- 连续失败 ≥ 阈值：blocked=True（实例暂停，不换 IP 硬切）；
- 恢复一致：blocked 解除、计数归零；
- 纯函数性：同入参恒同出参（无网络/无时钟/无副作用）。

实现方案：docs/07-实现方案/AG/SR-AG-001-基础设施.md §6.3、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

from services.agent.core.egress_check import (
    EGRESS_ALERT_DRIFT,
    EGRESS_ALERT_PROBE_FAILED,
    evaluate_egress,
)

EXPECTED = "203.0.113.7"  # 绑定的住宅出口 IP（文档占位 <PROXY-IP-01> 形态）


def test_egress_match_resets_fail_count() -> None:
    """实测 = 期望：一致放行，连续失败计数清零。"""
    result = evaluate_egress(EXPECTED, EXPECTED, fail_count=2)
    assert result.ok is True
    assert result.drifted is False
    assert result.fail_count == 0
    assert result.blocked is False
    assert result.alert is None


def test_egress_drift_alert_with_detail() -> None:
    """漂移即告警：EGRESS_IP_DRIFT，detail 携带 expected/actual/fail_count。"""
    result = evaluate_egress(EXPECTED, "198.51.100.9", fail_count=0)
    assert result.ok is False and result.drifted is True
    assert result.fail_count == 1
    assert result.alert is not None
    assert result.alert["event"] == EGRESS_ALERT_DRIFT
    assert result.alert["level"] == "critical"
    assert result.alert["detail"]["expected"] == EXPECTED
    assert result.alert["detail"]["actual"] == "198.51.100.9"


def test_egress_probe_failure_counts() -> None:
    """探测失败（actual=None）：按失败计数 +1，事件 EGRESS_PROBE_FAILED。"""
    result = evaluate_egress(EXPECTED, None, fail_count=1)
    assert result.ok is False
    assert result.probe_failed is True
    assert result.drifted is False
    assert result.fail_count == 2
    assert result.alert is not None
    assert result.alert["event"] == EGRESS_ALERT_PROBE_FAILED


def test_egress_blocked_after_threshold() -> None:
    """连续失败 ≥3：blocked=True（agent-runner 读到即安全暂停，IP 不漂移地停住）。"""
    result = evaluate_egress(EXPECTED, "198.51.100.9", fail_count=2, fail_threshold=3)
    assert result.blocked is True
    assert result.fail_count == 3


def test_egress_recovery_unblocks() -> None:
    """恢复一致：blocked 解除、计数归零（自检通过自动解封口径）。"""
    blocked = evaluate_egress(EXPECTED, "198.51.100.9", fail_count=2)
    assert blocked.blocked is True
    recovered = evaluate_egress(EXPECTED, EXPECTED, fail_count=blocked.fail_count)
    assert recovered.blocked is False and recovered.fail_count == 0


def test_egress_pure_function_deterministic() -> None:
    """纯函数：无网络无时钟，同入参两次调用结果全等。"""
    a = evaluate_egress(EXPECTED, "198.51.100.9", fail_count=1)
    b = evaluate_egress(EXPECTED, "198.51.100.9", fail_count=1)
    assert a == b
