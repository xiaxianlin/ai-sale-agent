# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""出口 IP 自检纯函数（FR-AG-001，SR-AG-001 §6.3；迭代 2.1 脚本逻辑纯函数化）。

check-egress-ip.sh 的判定逻辑收敛为本模块纯函数（可测、可复用）：
- 实测出口 IP == 绑定期望 IP → 放行、连续失败计数清零；
- 实测 != 期望 → EGRESS_IP_DRIFT（critical 告警，FR-AG-007 通道）；
- 探测失败（actual=None，curl 超时等）→ EGRESS_PROBE_FAILED，同样计入失败；
- 连续失败 ≥ fail_threshold → blocked=True：agent-runner 轮询循环读到即进入
  安全暂停（IP 不漂移地停住），代理失效/漂移**不换 IP 硬切**（防关联风险，
  部署与运维方案 §3 账号安全红线）。

纯函数边界：无网络访问、无时钟、无副作用——同入参恒同出参；网络探测
（curl ipify）与 Redis 计数由调用方（healthcheck 脚本/巡检任务）承载。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 告警事件名（与 SR-AG-001 §5/§6.3 告警契约一致）
EGRESS_ALERT_DRIFT = "EGRESS_IP_DRIFT"
EGRESS_ALERT_PROBE_FAILED = "EGRESS_PROBE_FAILED"

#: 默认连续失败阈值（proxy.check.fail_threshold，达到即暂停实例不硬切 IP）
DEFAULT_FAIL_THRESHOLD = 3


@dataclass(frozen=True)
class EgressCheckResult:
    """单次自检结果（healthcheck exit code 与告警决策的依据）。"""

    ok: bool                    # 实测 == 期望（放行）
    drifted: bool               # 实测 != 期望（漂移）
    probe_failed: bool          # 探测失败（actual 为 None）
    fail_count: int             # 本轮累计连续失败次数
    blocked: bool               # 是否达到暂停阈值（agent-runner 读到即安全暂停）
    alert: dict[str, object] | None  # 告警载荷（FR-AG-007 POST /api/v1/alerts body）


def evaluate_egress(
    expected_ip: str,
    actual_ip: str | None,
    *,
    fail_count: int = 0,
    fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
) -> EgressCheckResult:
    """出口 IP 自检判定（纯函数）。

    入参：
        expected_ip:    绑定的住宅出口 IP（proxy_binding.expected_ip，变更须走 CD/CR）；
        actual_ip:      实测出口 IP（curl ipify；None = 探测失败）；
        fail_count:     进入本轮前的连续失败计数（Redis proxy:egress:{id}:fail_count）；
        fail_threshold: 连续失败暂停阈值（proxy.check.fail_threshold，默认 3）。

    出参：EgressCheckResult——ok/漂移/探测失败三分支 + 累计计数 + blocked 判定 +
    告警载荷（仅漂移与探测失败产生，一致放行时为 None）。
    """
    if actual_ip == expected_ip:
        return EgressCheckResult(
            ok=True, drifted=False, probe_failed=False,
            fail_count=0, blocked=False, alert=None,      # 一致放行，计数清零
        )
    if actual_ip is None:
        drifted, probe_failed = False, True
        event = EGRESS_ALERT_PROBE_FAILED
        detail: dict[str, object] = {"expected": expected_ip, "actual": None}
    else:
        drifted, probe_failed = True, False
        event = EGRESS_ALERT_DRIFT
        detail = {"expected": expected_ip, "actual": actual_ip}
    new_count = fail_count + 1
    detail["fail_count"] = new_count
    return EgressCheckResult(
        ok=False,
        drifted=drifted,
        probe_failed=probe_failed,
        fail_count=new_count,
        blocked=new_count >= fail_threshold,              # 连续失败 → 暂停（不硬切）
        alert={
            "level": "critical",
            "source": "infra",
            "event": event,
            "detail": detail,
        },
    )
