# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""实例注册表（FR-SC-001，SR-SC-001 §4/§6.1；集群唯一事实源，demo 进程内形态）。

职责：
- 绑定不变式（SR §6.1 决策 1，三层强制的应用层）：account_id ↔ container_name ↔
  cookie_volume ↔ proxy_ip 一一对应——proxy_ip/账号唯一 + 容器/卷命名与序号一致；
  DB 层 UNIQUE 由生产形态承载，本层为第二道双保险；
- 观测态状态机门禁：offline/running/paused/error 四态，非法迁移在应用层拒绝并留审计
  （防状态漂移）；error 保留容器与卷待人工（error→offline 非法）；
- 出口 IP 漂移检测（§6.6，NFR-SEC-10）：观测 IP≠绑定 IP → 立即暂停 + 高级告警 +
  审计，**不自动换 IP 硬切**，绑定保留待人工走配置变更流程重绑；
- 故障窗口重启计数（§6.5）：窗口内累加、窗口恰过期清零重计（供编排器退避判定）。

demo 形态边界：PG agent_instance（UNIQUE 约束）/ Redis cl:hb 心跳由生产承载；本类以
RLock 串行化的内存字典等价实现，接口（register/get/mark/check_drift/bump_restart）与
生产一致，替换存储层时协议不变。状态迁移全量写审计（NFR-SEC-03）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

#: 实例序号域（容器名/卷名后缀 agent-runner-01…28，SRS §11.6 容量基线）
SEQ_MIN, SEQ_MAX = 1, 28

#: 合法状态（SR-SC-001 §4 risk CHECK 约束同源）
STATUSES = ("running", "paused", "error", "offline")

#: 观测态状态机合法迁移集（SR-SC-001 §4；error↔offline 不互通——error 保留现场待人工）
LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("offline", "running"), ("running", "paused"), ("paused", "running"),
    ("running", "error"), ("paused", "error"), ("error", "running"), ("error", "paused"),
    ("running", "offline"), ("paused", "offline"),
})


class ClusterError(Exception):
    """集群管理面基础异常（携带机器可读 code，供 API 层映射 HTTP 状态）。"""

    code = "CLUSTER_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvariantViolation(ClusterError):
    """绑定不变式违反（proxy_ip/账号重复、序号越界、命名错位）。"""

    code = "INVARIANT_VIOLATION"


class IllegalTransition(ClusterError):
    """非法状态迁移（状态机门禁拒绝）。"""

    code = "ILLEGAL_TRANSITION"


class InstanceNotFound(ClusterError):
    """实例不存在（404 语义）。"""

    code = "NOT_FOUND"


@dataclass
class InstanceRecord:
    """实例注册行（agent_instance 行的进程内形态，SR-SC-001 §4 逐字段对齐）。

    container_name / cookie_volume 由 seq 派生（命名不变式，不可独立赋值）——
    「agent-runner-NN ↔ cookie-NN ↔ seq」由结构保证而非约定。
    """

    seq: int
    account_id: int
    proxy_ip: str
    host_node: str = "runner-01"
    account_type: str = "sub"            # main | sub（OI-06 清单口径：4 主 + 24 子）
    proxy_provider: str = "A"            # 双供应商（R-04），demo 默认 A
    gray_batch: int = 0                  # 灰度批次：0=未放量 1=试点 2=全量（FR-SC-007）
    group_tag: str = "control"           # 灰度分组标签（pilot/wave2/full/control）
    status: str = "offline"              # 观测态
    desired_status: str = "offline"      # 期望态（人工/灰度设定）
    restart_count: int = 0               # 故障窗口内自动重启次数
    window_start: datetime | None = None  # 故障窗口起点（过期计数清零）
    last_probe: dict | None = None       # 最近探针快照 {proc, login, ip_ok, egress_ip}
    updated_at: datetime | None = None

    @property
    def container_name(self) -> str:
        """容器名（命名不变式派生：agent-runner-{seq:02d}）。"""
        return f"agent-runner-{self.seq:02d}"

    @property
    def cookie_volume(self) -> str:
        """独立 Cookie 卷名（一账号一卷：cookie-{seq:02d}）。"""
        return f"cookie-{self.seq:02d}"


class InstanceRegistry:
    """实例注册表：绑定不变式校验 + 状态机门禁 + 漂移检测 + 故障窗口计数。

    :param restart_window_s: 故障窗口时长（秒，SR §7 cl.restart.window_sec 默认 3600）；
    :param on_alert: 告警回调（level, type_, title, body；生产 = FR-AG-007 POST /alerts）。

    线程安全：RLock 串行化全部读写（28 实例并发注册/标记的 demo 等价——生产由 PG 事务
    与 UNIQUE 约束兜底）。
    """

    def __init__(
        self,
        *,
        restart_window_s: float = 3600.0,
        on_alert: Callable[[str, str, str, str], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._by_account: dict[int, InstanceRecord] = {}
        self._audit: list[dict] = []
        self._restart_window_s = restart_window_s
        self.on_alert = on_alert  # 测试/装配注入告警通道（None = 静默）

    # ---- 查询 ----

    def get(self, account_id: int) -> InstanceRecord:
        """按账号读取实例（不存在抛 InstanceNotFound）。"""
        with self._lock:
            inst = self._by_account.get(account_id)
            if inst is None:
                raise InstanceNotFound(f"实例不存在：account_id={account_id}")
            return inst

    def all(self) -> list[InstanceRecord]:
        """全部实例（按 seq 升序——供给与巡检的稳定顺序）。"""
        with self._lock:
            return sorted(self._by_account.values(), key=lambda r: r.seq)

    def count(self) -> int:
        """实例总数。"""
        with self._lock:
            return len(self._by_account)

    def by_status(self) -> dict[str, int]:
        """按观测态分组计数（集群视图 by_status 出参）。"""
        with self._lock:
            counts: dict[str, int] = {}
            for inst in self._by_account.values():
                counts[inst.status] = counts.get(inst.status, 0) + 1
            return counts

    def list_by_gray(self, gray_batch: int) -> list[InstanceRecord]:
        """按灰度批次圈定实例（分轮供给范围，SR §6.8）。"""
        with self._lock:
            return [i for i in self.all() if i.gray_batch == gray_batch]

    def group_counts(self) -> dict[str, int]:
        """按灰度分组标签计数（FR-SC-007 看板出参）。"""
        with self._lock:
            counts: dict[str, int] = {}
            for inst in self._by_account.values():
                counts[inst.group_tag] = counts.get(inst.group_tag, 0) + 1
            return counts

    @property
    def audit(self) -> list[dict]:
        """审计台账（只追加；生产 = audit_log 哈希链）。"""
        with self._lock:
            return list(self._audit)

    # ---- 写路径 ----

    def assert_invariants(self, inst: InstanceRecord) -> None:
        """绑定不变式校验（供给/注册前置，违反即中止操作——SR §6.1 决策 1）。

        ① proxy_ip 未被其他实例占用；② account_id 唯一；③ 容器/卷命名与序号一致
        （派生属性结构保证，此处复核防手工构造绕过）；④ 序号在 01-28 域内。
        """
        if not (SEQ_MIN <= inst.seq <= SEQ_MAX):
            raise InvariantViolation(
                f"实例序号 {inst.seq} 越界（{SEQ_MIN:02d}-{SEQ_MAX:02d} 之外）")
        for other in self._by_account.values():
            if other.account_id == inst.account_id:
                continue
            if other.proxy_ip == inst.proxy_ip:
                raise InvariantViolation(f"proxy_ip {inst.proxy_ip} 已被其他实例绑定")
            if other.seq == inst.seq:
                raise InvariantViolation(f"实例序号 {inst.seq} 已被账号 {other.account_id} 占用")
        if inst.container_name != f"agent-runner-{inst.seq:02d}" or \
                inst.cookie_volume != f"cookie-{inst.seq:02d}":
            raise InvariantViolation(f"实例 {inst.seq} 容器/卷命名与序号不一致")

    def register(
        self,
        *,
        seq: int,
        account_id: int,
        proxy_ip: str,
        host_node: str = "runner-01",
        account_type: str = "sub",
        gray_batch: int = 0,
    ) -> InstanceRecord:
        """注册实例绑定行（不变式校验通过方可落表——供给对账的第一道闸）。"""
        with self._lock:
            if account_id in self._by_account:
                raise InvariantViolation(f"账号 {account_id} 已绑定实例（一账号一实例）")
            inst = InstanceRecord(seq=seq, account_id=account_id, proxy_ip=proxy_ip,
                                  host_node=host_node, account_type=account_type,
                                  gray_batch=gray_batch)
            self.assert_invariants(inst)
            self._by_account[account_id] = inst
            self._audit.append({"event": "instance_registered", "account_id": account_id,
                                "detail": f"{inst.container_name}@{proxy_ip}"})
            return inst

    def mark(
        self,
        account_id: int,
        *,
        status: str | None = None,
        desired: str | None = None,
        reason: str = "",
        now: datetime | None = None,
    ) -> None:
        """状态/期望态迁移（状态机门禁：非法迁移抛 IllegalTransition，状态不变并留审计）。"""
        with self._lock:
            inst = self.get(account_id)
            now = now or datetime.now()
            if status is not None and status != inst.status:
                prev = inst.status  # 审计 detail 须记迁移前状态（先截取后变更，防 running->running 错账）
                if (prev, status) not in LEGAL_TRANSITIONS:
                    self._audit.append({"event": "transition_rejected", "account_id": account_id,
                                        "detail": f"{prev}->{status}", "reason": reason})
                    raise IllegalTransition(
                        f"非法状态迁移 {prev}->{status}（account={account_id}）")
                inst.status = status
                self._audit.append({"event": "instance_state_change", "account_id": account_id,
                                    "detail": f"{prev}->{status}", "reason": reason})
            if desired is not None:
                inst.desired_status = desired
            inst.updated_at = now

    def set_gray(self, account_id: int, *, gray_batch: int) -> None:
        """设置灰度批次（供给圈定范围用）。"""
        with self._lock:
            self.get(account_id).gray_batch = gray_batch

    def set_group(self, account_id: int, *, group: str) -> None:
        """设置灰度分组标签（FR-SC-007 tag_instances 落点；同值幂等覆盖）。"""
        with self._lock:
            self.get(account_id).group_tag = group

    def save_probe(self, account_id: int, probe: dict) -> None:
        """保存最近探针快照（cl:probe 热镜像键语义）。"""
        with self._lock:
            self.get(account_id).last_probe = dict(probe)

    def check_drift(self, account_id: int, *, observed_ip: str, now: datetime) -> bool:
        """出口 IP 漂移检测（SR §6.6）：漂移即暂停 + 告警，**绑定保留待人工重绑**。

        :returns: 是否发生漂移。paused/error 态实例只留痕不重复暂停（幂等）。
        """
        with self._lock:
            inst = self.get(account_id)
            if observed_ip == inst.proxy_ip:
                return False  # 一致：无动作
            inst.last_probe = {"ip_ok": False, "egress_ip": observed_ip, "checked_at": now}
            self._audit.append({"event": "ip_drift", "account_id": account_id,
                                "detail": f"observed={observed_ip} bound={inst.proxy_ip}"})
            if inst.status == "running":  # 立即暂停；不自动换 IP（严禁硬切）
                inst.status = "paused"
                self._audit.append({"event": "instance_state_change", "account_id": account_id,
                                    "detail": "running->paused", "reason": "ip_drift"})
            self._alert("high", "system_error",
                        f"实例 {inst.container_name} 出口 IP 漂移已暂停",
                        f"观测 {observed_ip} ≠ 绑定 {inst.proxy_ip}；其余实例不受影响，"
                        "待人工核实后走配置变更流程重绑")
            return True

    def bump_restart(self, account_id: int, *, now: datetime) -> int:
        """故障窗口重启计数：窗口内 +1、恰过期清零重计（返回本次是第几次）。"""
        with self._lock:
            inst = self.get(account_id)
            window_open = (
                inst.window_start is not None
                and now - inst.window_start <= timedelta(seconds=self._restart_window_s)
            )
            if window_open:
                inst.restart_count += 1
            else:
                inst.restart_count = 1  # 窗口过期（或首次）：计数重置
                inst.window_start = now
            return inst.restart_count

    # ---- 内部 ----

    def _alert(self, level: str, type_: str, title: str, body: str) -> None:
        """告警投递（best-effort：通道故障不影响注册表主流程）。"""
        if self.on_alert is None:
            return
        try:
            self.on_alert(level, type_, title, body)
        except Exception:  # noqa: BLE001 —— 告警为旁路，不阻断
            pass
