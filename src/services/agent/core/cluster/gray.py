# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""灰度上线机制（FR-SC-007，SR-SC-007 §6/§7；放量决策唯一入口，demo 进程内形态）。

职责（SR-SC-007 六步闭环的管理面等价实现）：
- **分组与配置隔离**（§6-1/1b）：账号打标 pilot/wave2/full/control——灰度组解析计划级
  覆盖键（target_config），对照组回落全局键（set_global_config）；回滚后覆盖摘除、
  全局回落基线版本；
- **放量门禁三硬指标**（§6-2，SRS §16.3 口径）：封号/限权信号 = 0（一票否决）、
  回复可用率 ≥ 0.80、单账号日均会话量 ≥ 15（≥ 达标口径）；实例存活率 < 1.0 为
  前置门禁（extra_fail 直接判失败）；观察窗未满抢跑判定拦截（OBSERVE_NOT_FINISHED）；
- **放量**（§6-3）：仅 gate_passed 态可放量（PLAN_STATE_INVALID 状态机拦截，兼防
  并发重复提交）——pilot→wave2（对照组转入）、wave2→full（全量）；转入账号按
  restart_batch 分批错峰重启（每批 ≤ batch 上限），随后开启新观察窗；
- **一键回滚**（§6-4）：分钟级——灰度覆盖摘除 + 全局回落基线版本，试点组（灰度
  首批）实例暂停打回 control，wave2/full 实例仅摘标签零感知回落基线配置，P1 告警
  + 决策留痕；重复回滚幂等拦截；
- **决策台账**（§15.3 付款节点证据链）：每次动作落决策记录（时间/动作/依据=门禁
  快照/审批人），只追加、时序单调（审计哈希链语义）。

指标来源经 :class:`GateMetrics` 快照注入（生产 = 观测流水聚合）；demo/测试以
callable(plan) 注入。告警走 alerts 通道（生产 = FR-AG-007），重启动作经 restart_hook
注入（生产 = ClusterOrchestrator 分批重启）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

#: 放量门禁三硬指标阈值（SRS §16.3：封号 0 / 可用率 ≥80% / 会话量 ≥15，≥ 达标口径）
BAN_LIMIT = 0
REPLY_USABLE_RATE_MIN = 0.80
SESSIONS_PER_ACCOUNT_DAY_MIN = 15.0

#: 前置门禁：实例存活率必须满额（SR-SC-007 §6-2；不满直接判失败）
INSTANCE_ALIVE_RATE_MIN = 1.0

#: 灰度分组标签（group_tag 域）与其中的覆盖组（resolve_config 命中计划覆盖键的组）
GRAY_GROUPS = ("pilot", "wave2", "full", "control")
OVERRIDE_GROUPS = ("pilot", "wave2", "full")

#: 放量分批重启批次上限（SR §7 cl.batch.max_per_round 默认值，分批错峰）
RESTART_BATCH_SIZE = 5


class GrayError(Exception):
    """灰度管理面异常（携带机器可读 code，供 API 层映射 409 语义）。"""

    def __init__(self, message: str, *, code: str = "GRAY_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class GateMetrics:
    """门禁指标快照（观测窗口聚合值；demo 由测试/演示注入确定性数值）。"""

    ban_or_restricted: int          # 观察窗内封号/限权账号数（一票否决项）
    reply_usable_rate: float        # 回复可用率（人工抽评 + 合规校验复合口径）
    sessions_per_account_day: float  # 单账号日均会话量
    instance_alive_rate: float = 1.0  # 实例存活率（前置门禁）


@dataclass
class GrayPlan:
    """灰度计划（cl_gray_plan 行的进程内形态；state 可迁移、其余为计划事实）。"""

    plan_no: str
    target_version: str
    baseline_version: str
    pilot_account_ids: list[int]
    observe_window_days: int
    target_config: dict[str, str]
    created_at: datetime
    state: str = "pilot_running"          # pilot_running→observing→gate_passed/gate_failed→…
    gray_group: str = "pilot"             # 当前灰度推进阶段（pilot/wave2/full）
    observing_since: datetime | None = None  # 当前观察窗起点（恰满 N 天方可判定）
    last_gate_checks: list[dict] = field(default_factory=list)  # 最近一次门禁快照（放量依据）


@dataclass(frozen=True)
class GrayDecision:
    """放量决策记录（§15.3 付款节点证据链：时间/动作/依据/审批人，只追加）。"""

    decided_at: datetime
    action: str                  # plan_create/gate_pass/gate_fail/rollout/rollback
    approver: str
    evidence: dict               # 依据快照（门禁 checks、目标分组、回滚原因等）


class GrayReleaseManager:
    """灰度发布管理器：分组打标 → 门禁判定 → 放量/回滚 → 决策台账（§6 六步闭环）。

    :param registry: 实例注册表（分组标签与实例状态的落点）；
    :param metrics: 指标快照源 callable(plan) -> GateMetrics（生产 = 观测流水聚合）；
    :param alerts: 告警通道（send(level, type_, title, body)；生产 = FR-AG-007）；
    :param restart_hook: 放量分批重启回调 callable(list[int])（生产 = 编排器；None = 仅记录）。

    线程安全：RLock 串行化计划状态迁移（放量/回滚互斥——分布式锁的进程内等价）。
    """

    def __init__(
        self,
        *,
        registry,
        metrics: Callable[[GrayPlan], GateMetrics],
        alerts=None,
        restart_hook: Callable[[list[int]], None] | None = None,
    ) -> None:
        self._registry = registry
        self._metrics = metrics
        self._alerts = alerts
        self._restart_hook = restart_hook
        self._lock = threading.RLock()
        self.plans: dict[str, GrayPlan] = {}      # plan_no → 计划（插入序 = 创建序）
        self.decisions: list[GrayDecision] = []   # 决策台账（只追加、时序单调）
        self._global_config: dict[str, str] = {}  # 全局配置键（对照组回落基线）

    # ---- 查询 ----

    def plan(self, plan_no: str) -> GrayPlan:
        """按计划号读取计划（不存在抛 GrayError PLAN_NOT_FOUND）。"""
        plan = self.plans.get(plan_no)
        if plan is None:
            raise GrayError(f"灰度计划不存在：{plan_no}", code="PLAN_NOT_FOUND")
        return plan

    def latest_plan(self) -> GrayPlan | None:
        """最近创建的计划（看板/灰度状态接口的数据源）。"""
        return list(self.plans.values())[-1] if self.plans else None

    def resolve_config(self, key: str, group: str) -> str:
        """组级配置解析：灰度组命中计划覆盖键，对照组/未覆盖键回落全局键。

        回滚后覆盖视为摘除（rolled_back 计划不再生效），全局回落基线版本。
        """
        with self._lock:
            plan = self.latest_plan()
            if (plan is not None and plan.state != "rolled_back"
                    and group in OVERRIDE_GROUPS
                    and key in plan.target_config):
                return plan.target_config[key]
            return self._global_config.get(key, "")

    def set_global_config(self, key: str, value: str) -> None:
        """设置全局配置键（对照组基线；生产 = app_config scope='global'）。"""
        with self._lock:
            self._global_config[key] = value

    # ---- 计划与分组（§6-1） ----

    def create_plan(
        self,
        *,
        plan_no: str,
        target_version: str,
        baseline_version: str,
        pilot_account_ids: list[int],
        observe_window_days: int,
        target_config: dict[str, str],
        now: datetime,
    ) -> GrayPlan:
        """创建灰度计划：试点账号打标 pilot（其余保持 control），落 plan_create 决策。"""
        with self._lock:
            if plan_no in self.plans:
                raise GrayError(f"灰度计划号重复：{plan_no}", code="PLAN_DUPLICATED")
            plan = GrayPlan(
                plan_no=plan_no, target_version=target_version,
                baseline_version=baseline_version,
                pilot_account_ids=list(pilot_account_ids),
                observe_window_days=observe_window_days,
                target_config=dict(target_config), created_at=now)
            for account_id in plan.pilot_account_ids:  # 打标生效（SR §6-1b 组级隔离）
                self._registry.set_group(account_id, group="pilot")
            self.plans[plan_no] = plan
            self._decide(now, "plan_create", "system", {
                "plan_no": plan_no, "target_version": target_version,
                "baseline_version": baseline_version,
                "pilot_accounts": len(plan.pilot_account_ids),
                "observe_window_days": observe_window_days})
            return plan

    def start_observing(self, plan_no: str, *, now: datetime) -> None:
        """开启观察窗（pilot_running → observing；放量后再次调用 = 新观察窗）。"""
        with self._lock:
            plan = self.plan(plan_no)
            plan.observing_since = now
            plan.state = "observing"

    # ---- 门禁判定（§6-2） ----

    def evaluate_gate(self, plan_no: str, *, now: datetime) -> dict:
        """放量门禁判定：前置存活率 + 三硬指标；通过 → gate_passed，否则暂停试点组。

        :raises GrayError: OBSERVE_NOT_STARTED / OBSERVE_NOT_FINISHED（观察窗未满抢跑拦截）。
        """
        with self._lock:
            plan = self.plan(plan_no)
            if plan.observing_since is None:
                raise GrayError("OBSERVE_NOT_STARTED：观察窗未开启（先 start_observing）",
                                code="OBSERVE_NOT_STARTED")
            window_end = plan.observing_since + timedelta(days=plan.observe_window_days)
            if now < window_end:  # 恰满（=）允许判定，未满抢跑拦截（SR §9 边界口径）
                raise GrayError(
                    f"OBSERVE_NOT_FINISHED：观察窗未满（{plan.observe_window_days} 天，"
                    f"至 {window_end.isoformat()}），禁止抢跑判定", code="OBSERVE_NOT_FINISHED")
            metrics = self._metrics(plan)
            checks = [
                {"metric": "ban_signal", "value": metrics.ban_or_restricted,
                 "threshold": BAN_LIMIT, "passed": metrics.ban_or_restricted <= BAN_LIMIT},
                {"metric": "reply_usable_rate", "value": metrics.reply_usable_rate,
                 "threshold": REPLY_USABLE_RATE_MIN,
                 "passed": metrics.reply_usable_rate >= REPLY_USABLE_RATE_MIN},
                {"metric": "sessions_per_account_day", "value": metrics.sessions_per_account_day,
                 "threshold": SESSIONS_PER_ACCOUNT_DAY_MIN,
                 "passed": metrics.sessions_per_account_day >= SESSIONS_PER_ACCOUNT_DAY_MIN},
            ]
            extra_fail: str | None = None
            if metrics.instance_alive_rate < INSTANCE_ALIVE_RATE_MIN:  # 前置门禁
                extra_fail = "instance_alive_rate"
            passed = extra_fail is None and all(c["passed"] for c in checks)
            plan.last_gate_checks = checks
            report: dict = {
                "plan_no": plan_no, "passed": passed, "checks": checks,
                "metrics": {"ban_or_restricted": metrics.ban_or_restricted,
                            "reply_usable_rate": metrics.reply_usable_rate,
                            "sessions_per_account_day": metrics.sessions_per_account_day,
                            "instance_alive_rate": metrics.instance_alive_rate},
                "evaluated_at": now,
            }
            if passed:
                plan.state = "gate_passed"
                self._decide(now, "gate_pass", "system",
                             {"plan_no": plan_no, "gate_checks": checks})
            else:
                plan.state = "gate_failed"
                report["extra_fail"] = extra_fail
                for account_id in plan.pilot_account_ids:  # 试点组立即暂停（止血；offline 实例仅置期望）
                    inst = self._registry.get(account_id)
                    if inst.status == "running":
                        self._registry.mark(account_id, status="paused",
                                            reason="gate_fail", now=now)
                self._alert("P1", "gray_release",
                            f"灰度门禁未通过：{plan_no} 试点组已暂停",
                            f"checks={checks} extra_fail={extra_fail}")
                self._decide(now, "gate_fail", "system",
                             {"plan_no": plan_no, "gate_checks": checks,
                              "extra_fail": extra_fail})
            return report

    # ---- 放量（§6-3） ----

    def rollout(self, plan_no: str, *, approver: str, now: datetime) -> dict:
        """放量：pilot→wave2（对照组转入）/ wave2→full（全量），分批错峰重启 + 新观察窗。

        :raises GrayError: PLAN_STATE_INVALID（未过门禁放量/重复放量——状态机拦截）。
        """
        with self._lock:
            plan = self.plan(plan_no)
            if plan.state != "gate_passed":  # 防抢跑放量与并发重复提交（进程内等价分布式锁）
                raise GrayError(
                    f"PLAN_STATE_INVALID：计划 {plan_no} 状态为 {plan.state}，"
                    "仅 gate_passed 可放量", code="PLAN_STATE_INVALID")
            if plan.gray_group == "pilot":
                to_group = "wave2"
                movers = [i.account_id for i in self._registry.all()
                          if i.group_tag == "control"]
            else:  # wave2 → full：全量（pilot+wave2 一并转正）
                to_group = "full"
                movers = [i.account_id for i in self._registry.all()
                          if i.group_tag in OVERRIDE_GROUPS]
            for account_id in movers:
                self._registry.set_group(account_id, group=to_group)
            batches = [movers[i:i + RESTART_BATCH_SIZE]
                       for i in range(0, len(movers), RESTART_BATCH_SIZE)]
            for batch in batches:  # 分批错峰重启（每批 ≤ RESTART_BATCH_SIZE）
                if self._restart_hook is not None:
                    self._restart_hook(batch)
            plan.gray_group = to_group
            plan.state = "observing"  # 新观察窗开启（转入组按同口径观察）
            plan.observing_since = now
            self._decide(now, "rollout", approver, {
                "plan_no": plan_no, "to_group": to_group, "accounts": len(movers),
                "batches": len(batches), "gate_checks": plan.last_gate_checks})
            return {"to_group": to_group, "accounts": len(movers),
                    "batches": len(batches), "state": plan.state}

    # ---- 回滚（§6-4） ----

    def rollback(self, plan_no: str, *, approver: str, reason: str,
                 now: datetime) -> dict:
        """一键回滚：覆盖摘除 + 全局回落基线 + 试点组暂停打回 control（对照组零感知）。

        :raises GrayError: PLAN_STATE_INVALID（已回滚计划幂等拦截，不重复执行）。
        """
        with self._lock:
            plan = self.plan(plan_no)
            if plan.state == "rolled_back":
                raise GrayError(f"PLAN_STATE_INVALID：计划 {plan_no} 已回滚（幂等拦截）",
                                code="PLAN_STATE_INVALID")
            paused: list[int] = []
            for inst in self._registry.all():
                if inst.group_tag == "pilot":  # 试点组（灰度首批）：暂停 + 打回对照
                    self._registry.set_group(inst.account_id, group="control")
                    if inst.status == "running":  # offline/error 实例仅摘标签（非法迁移拦截）
                        self._registry.mark(inst.account_id, status="paused",
                                            reason=f"rollback:{plan_no}", now=now)
                    paused.append(inst.account_id)
                elif inst.group_tag in ("wave2", "full"):  # 已放量组：仅摘标签零感知回落
                    self._registry.set_group(inst.account_id, group="control")
            for key in plan.target_config:  # 全局回落基线版本（覆盖已摘除）
                self._global_config[key] = _baseline_value(key, plan.baseline_version)
            plan.state = "rolled_back"
            elapsed = (now - plan.created_at).total_seconds()
            self._alert("P1", "gray_release", f"灰度计划已回滚：{plan_no}",
                        f"原因：{reason}；试点组 {len(paused)} 实例暂停，配置回落基线")
            self._decide(now, "rollback", approver, {
                "plan_no": plan_no, "reason": reason, "elapsed_sec": elapsed,
                "paused_accounts": paused})
            return {"plan_no": plan_no, "elapsed_sec": elapsed,
                    "paused": paused, "state": plan.state}

    # ---- 内部 ----

    def _decide(self, now: datetime, action: str, approver: str,
                evidence: dict) -> None:
        """追加决策记录（台账只追加、时序单调——审计哈希链语义）。"""
        self.decisions.append(GrayDecision(decided_at=now, action=action,
                                           approver=approver, evidence=dict(evidence)))

    def _alert(self, level: str, type_: str, title: str, body: str) -> None:
        """告警投递（best-effort：通道故障不影响灰度主流程）。"""
        if self._alerts is None:
            return
        try:
            self._alerts.send(level, type_, title, body)
        except Exception:  # noqa: BLE001 —— 告警为旁路
            pass


def _baseline_value(key: str, baseline_version: str) -> str:
    """回滚回落值：版本类键取基线版本的末段（strategy:v1 → v1），其余回落基线全串。"""
    if key.endswith("version") and ":" in baseline_version:
        return baseline_version.rsplit(":", 1)[-1]
    return baseline_version
