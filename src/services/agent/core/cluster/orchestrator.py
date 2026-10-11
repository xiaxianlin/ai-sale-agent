# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""集群编排器（FR-SC-001，SR-SC-001 §6.2/§6.3/§6.5；管理面唯一写入口，demo 进程内形态）。

职责：
- **compose override 模板渲染**（§6.2）：容器名 agent-runner-NN / 独立 Cookie 卷
  cookie-NN / 代理 env 注入（凭证一律 <占位>，NFR-SEC-04 零明文）/ 资源配额（§6.7）；
- **批量供给**（§6.8）：按 gray_batch 圈定、batch_max_per_round 分轮、轮间观察窗——
  N 由注册表数据驱动（NFR-SCA-01，1→28 无代码变更）；
- **实例级启停/重启**（§6.3）：实例互斥锁（调谐循环 vs 人工操作）——start 为管理面
  状态翻转（容器拉起走 launch_container/调谐循环，漂移场景零 start 调用，
  NFR-SEC-10）；stop/restart 直达容器操作；
- **调谐与分级恢复**（§6.5）：期望态 vs 观测态——健康无动作；进程级故障先隔离
  （暂停+告警）再退避重启（base×2^(n-1)，超限转 error 人工）；IP 漂移/登录失效
  不自动重启（立即隔离转 error，NFR-SEC-10 严禁硬切）；逐实例异常就地捕获
  （NFR-REL-02 单实例故障不影响其余）。

容器操作经 :class:`ContainerOps` Protocol 注入：生产 = Docker Engine API 封装；
demo/测试 = :class:`FakeContainerOps` 内存编排模拟（kill 演练 = SRS §16.5 单点故障
演练的进程内等价）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from services.agent.core.cluster.registry import (
    ClusterError,
    InstanceRecord,
    InstanceRegistry,
)

#: 代理接入串占位（凭证运行时经 Docker secrets 注入，compose/日志零明文——NFR-SEC-04）
PROXY_URL_TEMPLATE = "http://<PROXY-USER>:<PROXY-PASS>@{proxy_ip}:<PORT>"


class InstanceBusy(ClusterError):
    """实例管理操作互斥冲突（409 语义：调谐循环与人工操作并发）。"""

    code = "INST_BUSY"


@dataclass(frozen=True)
class ClusterConfig:
    """编排器参数（app_config scope='cluster' 的冻结快照形态，NFR-MAI-02 配置外置）。"""

    restart_max_attempts: int = 3      # 故障窗口内自动重启上限（SR §7，超限转 error）
    backoff_base_s: float = 30.0       # 退避基数：30/60/120 指数（SR §6.5）
    batch_max_per_round: int = 5       # 批量供给每轮实例数（SR §7 cl.batch.max_per_round）
    rollout_gap_s: float = 0.0         # 轮间观察窗（生产默认 300s；demo 0 便于测试）
    runner_image: str = "ai-sale/openclaw-runner:0.1.0"  # 版本化 tag（回滚依据，部署 §7）


@dataclass(frozen=True)
class OpResult:
    """实例操作结果（API 层直接映射：ok=False 时 code → HTTP 状态码）。"""

    ok: bool
    status: str = ""                   # 操作后的观测态（失败时为空）
    code: str = "OK"                   # OK / ILLEGAL_TRANSITION / DOCKER_UNREACHABLE


class ContainerOps(Protocol):
    """容器操作协议（编排器对容器运行时的唯一依赖缝，生产 = Docker Engine API 封装）。"""

    def start(self, host: str, container: str) -> bool: ...

    def stop(self, host: str, container: str) -> bool: ...

    def restart(self, host: str, container: str) -> bool: ...

    def is_running(self, host: str, container: str) -> bool: ...

    def kill(self, host: str, container: str) -> None:
        """故障演练注入（SRS §16.5 单点故障演练；生产环境仅供演练脚本使用）。"""


class FakeContainerOps:
    """内存容器编排模拟（demo/测试）：容器状态机 + 调用留痕 + 故障注入三件套。

    - 状态机：absent →（start）running ⇄（stop/kill）stopped；
    - calls：全部操作留痕 (op, host, container)，供「其余 27 实例零波及」断言；
    - 故障注入：``fail_next_start``（start 返回 False n 次）、``boom``（列出的容器
      任何操作抛 RuntimeError——模拟 Docker 不可达/探针异常）。
    """

    def __init__(self) -> None:
        self._states: dict[str, str] = {}          # container → absent/running/stopped
        self._calls: list[tuple[str, str, str]] = []
        self._fail_starts: dict[str, int] = {}     # container → 剩余失败次数
        self.boom: set[str] = set()                # 强制抛异常的容器集合
        self._lock = threading.Lock()

    # ---- 状态查询 ----

    def is_running(self, host: str, container: str) -> bool:
        """容器进程是否存活（探针①进程要素）。"""
        self._maybe_boom(container)
        self._record("is_running", host, container)
        return self._states.get(container, "absent") == "running"

    def calls_of(self, op: str) -> list[tuple[str, str]]:
        """取某类操作的全部留痕 (host, container)。"""
        with self._lock:
            return [(h, c) for o, h, c in self._calls if o == op]

    def clear_calls(self) -> None:
        """清空调用留痕（演练窗口隔离用）。"""
        with self._lock:
            self._calls.clear()

    # ---- 故障注入 ----

    def fail_next_start(self, container: str, *, times: int) -> None:
        """注入 start 失败 n 次（Docker 不可达演练）。"""
        self._fail_starts[container] = times

    def _maybe_boom(self, container: str) -> None:
        if container in self.boom:
            raise RuntimeError(f"容器 {container} 操作通道异常（演练注入）")

    def _record(self, op: str, host: str, container: str) -> None:
        with self._lock:
            self._calls.append((op, host, container))

    # ---- 容器操作 ----

    def start(self, host: str, container: str) -> bool:
        """启动容器（重启恢复/供给）。"""
        self._maybe_boom(container)
        self._record("start", host, container)
        if self._fail_starts.get(container, 0) > 0:
            self._fail_starts[container] -= 1
            return False
        self._states[container] = "running"
        return True

    def stop(self, host: str, container: str) -> bool:
        """停止容器（隔离/人工暂停）。"""
        self._maybe_boom(container)
        self._record("stop", host, container)
        self._states[container] = "stopped"
        return True

    def restart(self, host: str, container: str) -> bool:
        """重启容器（实例级 restart）。"""
        self._maybe_boom(container)
        self._record("restart", host, container)
        self._states[container] = "running"
        return True

    def kill(self, host: str, container: str) -> None:
        """进程击杀（单点故障演练注入：等价 docker kill）。"""
        self._maybe_boom(container)
        self._record("kill", host, container)
        self._states[container] = "stopped"


@dataclass
class _ReconcileSummary:
    """reconcile_all 汇总（按动作分组，供演练对账与测试断言）。"""

    healthy: list[int] = field(default_factory=list)
    restarted: list[int] = field(default_factory=list)
    isolated: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """出参字典（列表字段按账号升序稳定输出）。"""
        return {
            "healthy": sorted(self.healthy),
            "restarted": sorted(self.restarted),
            "isolated": sorted(self.isolated),
            "skipped": sorted(self.skipped),
            "errors": sorted(self.errors),
        }


class ClusterOrchestrator:
    """集群编排器：批量供给 + 实例级操作 + 调谐恢复（管理面唯一写入口）。

    :param registry: 实例注册表（唯一事实源）；
    :param ops: 容器操作通道（ContainerOps Protocol 注入）；
    :param alerts: 告警通道（level, type_, title, body；生产 = FR-AG-007）；
    :param config: 编排器参数（ClusterConfig 冻结快照）；
    :param sleeper: 退避/轮间等待注入点（测试记录延迟序列；生产 = asyncio.sleep）。
    """

    def __init__(
        self,
        registry: InstanceRegistry,
        ops: ContainerOps,
        *,
        alerts=None,
        config: ClusterConfig | None = None,
        sleeper=None,
    ) -> None:
        self._registry = registry
        self._ops = ops
        self._alerts = alerts
        self._config = config or ClusterConfig()
        self._sleeper = sleeper if sleeper is not None else (lambda _s: None)
        self.sleeps: list[float] = []          # 全部等待延迟留痕（退避序列断言用）
        self._locks: set[int] = set()          # 实例互斥锁持有集
        self._locks_guard = threading.Lock()

    # ---- 模板渲染（§6.2） ----

    def render_override(self, inst: InstanceRecord) -> dict:
        """渲染单实例 compose override 片段（纯数据，可 yaml.safe_dump）。

        三要素：容器名唯一 + 独立 Cookie 卷挂载与声明 + 代理 env（凭证占位）；
        附加资源配额（单容器 1.0 CPU / 2g，§6.7 密度规划）。
        """
        service = {
            "image": self._config.runner_image,
            "container_name": inst.container_name,
            "restart": "unless-stopped",
            "environment": [
                f"ACCOUNT_ID={inst.account_id}",
                f"ACCOUNT_TYPE={inst.account_type}",
                f"HOST_NODE={inst.host_node}",
                f"PROXY_URL={PROXY_URL_TEMPLATE.format(proxy_ip=inst.proxy_ip)}",
            ],
            "volumes": [f"{inst.cookie_volume}:/data/openclaw/cookies"],
            "deploy": {"resources": {"limits": {"cpus": "1.0", "memory": "2g"}}},
        }
        return {"services": {inst.container_name: service},
                "volumes": {inst.cookie_volume: {}}}

    # ---- 批量供给（§6.8） ----

    def provision(self, *, gray_batch: int, now: datetime | None = None) -> dict:
        """按灰度批次批量供给：不变式校验 → 渲染 → 启动 → 标记 running。

        分轮执行（batch_max_per_round），轮间等待 rollout_gap（观察窗，联动 FR-SC-007）。
        """
        rows = self._registry.list_by_gray(gray_batch)
        cfg = self._config
        created, batches = 0, 0
        for i in range(0, len(rows), cfg.batch_max_per_round):
            if batches > 0 and cfg.rollout_gap_s > 0:  # 首轮不等待；末轮不等待
                self._wait(cfg.rollout_gap_s)
            for inst in rows[i:i + cfg.batch_max_per_round]:
                self._registry.assert_invariants(inst)  # 供给前不变式校验（决策 1）
                if self._ops.start(inst.host_node, inst.container_name):
                    self._registry.mark(inst.account_id, status="running", desired="running",
                                        reason="provision", now=now)
                    created += 1
            batches += 1
        return {"created": created, "batches": batches}

    # ---- 实例级操作（§6.3） ----

    def start_instance(self, account_id: int, *, now: datetime | None = None) -> OpResult:
        """启动实例（offline/paused → running）。

        管理面语义：只做状态机翻转（期望态+观测态 → running），不直接调用容器——
        容器真实拉起由 :meth:`launch_container`（API 同步路径）或调谐循环（自愈路径）
        执行；IP 漂移/登录失效场景下**严禁**任何自动 start（NFR-SEC-10 硬切红线，
        SR §6.5「零 start 调用」验收口径）。
        """
        return self._guarded_action(account_id, "start", now=now)

    def launch_container(self, account_id: int, *, now: datetime | None = None) -> OpResult:
        """容器真实拉起（ops.start；Docker 失败返回 DOCKER_UNREACHABLE，不虚报状态）。

        API 同步启动路径的第二步：start_instance 翻状态 → 本方法拉容器 → 失败由
        调用方映射 503；状态收敛仍以调谐循环为准（期望态优先）。
        """
        inst = self._registry.get(account_id)
        with self._locks_guard:
            if account_id in self._locks:
                raise InstanceBusy(f"实例 {account_id} 管理操作进行中（互斥锁占用）")
            self._locks.add(account_id)
        try:
            ok = self._ops.start(inst.host_node, inst.container_name)
            if not ok:
                return OpResult(ok=False, code="DOCKER_UNREACHABLE")
            self._registry.mark(account_id, status="running", reason="launch", now=now)
            return OpResult(ok=True, status="running")
        except ClusterError:
            raise
        finally:
            with self._locks_guard:
                self._locks.discard(account_id)

    def stop_instance(self, account_id: int, *, now: datetime | None = None) -> OpResult:
        """停止实例（running/paused → paused；容器停止、会话保留）。"""
        return self._guarded_action(account_id, "stop", now=now)

    def restart_instance(self, account_id: int, *, now: datetime | None = None) -> OpResult:
        """重启实例（running → running）。"""
        return self._guarded_action(account_id, "restart", now=now)

    def acquire_lock(self, account_id: int) -> None:
        """持有实例互斥锁（cl:restart:lock 键语义；并发持有者触发 INST_BUSY）。"""
        with self._locks_guard:
            if account_id in self._locks:
                raise InstanceBusy(f"实例 {account_id} 管理操作进行中（互斥锁占用）")
            self._locks.add(account_id)

    def release_lock(self, account_id: int) -> None:
        """释放实例互斥锁。"""
        with self._locks_guard:
            self._locks.discard(account_id)

    def _guarded_action(
        self, account_id: int, action: str, *, now: datetime | None = None
    ) -> OpResult:
        """实例操作统一闸门：互斥锁 → 容器操作 → 状态机落账（§6.3 三道闸的 demo 等价）。"""
        inst = self._registry.get(account_id)
        with self._locks_guard:
            if account_id in self._locks:  # 已被并发持有（调谐循环 vs 人工操作）
                raise InstanceBusy(f"实例 {account_id} 管理操作进行中（互斥锁占用）")
            self._locks.add(account_id)
        try:
            target = {"start": "running", "stop": "paused", "restart": "running"}[action]
            if action in ("start", "restart") and inst.status == "error":
                # error 实例须经人工处置（注册表 error→running 显式放行），不接受直接 start
                return OpResult(ok=False, code="ILLEGAL_TRANSITION")
            self._registry.mark(account_id, desired=target, reason=f"op:{action}", now=now)
            if action == "start":
                # 管理面仅翻状态（容器拉起走 launch_container/调谐循环——见 start_instance）
                self._registry.mark(account_id, status=target, reason=f"op:{action}", now=now)
                return OpResult(ok=True, status=target)
            ok = getattr(self._ops, action)(inst.host_node, inst.container_name)
            if not ok:
                return OpResult(ok=False, code="DOCKER_UNREACHABLE")
            self._registry.mark(account_id, status=target, reason=f"op:{action}", now=now)
            return OpResult(ok=True, status=target)
        except ClusterError:
            raise
        finally:
            with self._locks_guard:
                self._locks.discard(account_id)

    # ---- 调谐与分级恢复（§6.5） ----

    def reconcile_one(
        self,
        account_id: int,
        *,
        now: datetime,
        egress_ip: str | None = None,
        login_ok: bool = True,
    ) -> str:
        """单实例调谐：期望态优先 → 三要素探针 → 分级处置。

        :returns: 动作——healthy / restarted / restart_exhausted / isolated_error / skipped。
        """
        inst = self._registry.get(account_id)
        if inst.desired_status != "running":
            return "skipped"  # 期望暂停/离线：调谐不干预
        proc = self._ops.is_running(inst.host_node, inst.container_name)
        observed = egress_ip if egress_ip is not None else inst.proxy_ip
        probe = {"proc": proc, "login": login_ok, "ip_ok": observed == inst.proxy_ip,
                 "egress_ip": observed}
        self._registry.save_probe(account_id, probe)
        if proc and login_ok and probe["ip_ok"]:
            return "healthy"
        # ① 先隔离：立即停止容器 + 暂停 + 高级告警（三层隔离不扩散，NFR-REL-02）
        self._ops.stop(inst.host_node, inst.container_name)
        self._registry.mark(account_id, status="paused", reason=f"probe_fault:{account_id}",
                            now=now)
        # ② IP 漂移 / 登录失效：不自动换 IP、不暴力重登 → 转 error 人工处置（NFR-SEC-10；
        #    判定先于进程级重启分支——漂移/登录域故障一律零 start 调用，与进程是否存活无关）
        if not probe["ip_ok"] or not login_ok:
            self._registry.mark(account_id, status="error",
                                reason="ip_drift" if not probe["ip_ok"] else "login_expired",
                                now=now)
            self._alert("high", "system_error",
                        f"实例 {inst.container_name} 故障已隔离（转人工）", f"探针 {probe}")
            return "isolated_error"
        # ③ 进程级故障：窗口内退避自动重启；超限转 error + 告警
        attempt = self._registry.bump_restart(account_id, now=now)
        if attempt > self._config.restart_max_attempts:
            self._registry.mark(account_id, status="error", reason="restart_exhausted", now=now)
            self._alert("high", "system_error",
                        f"实例 {inst.container_name} 重启超限，转人工",
                        f"窗口内第 {attempt} 次；其余实例不受影响")
            return "restart_exhausted"
        self._alert("mid", "system_error",
                    f"实例 {inst.container_name} 故障已隔离，退避重启（第 {attempt} 次）",
                    f"退避 {self.backoff_delay(attempt):.0f}s；其余实例不受影响")
        self._wait(self.backoff_delay(attempt))  # 指数退避：base×2^(n-1)
        self._ops.start(inst.host_node, inst.container_name)
        self._registry.mark(account_id, status="running", reason="auto_recovered", now=now)
        return "restarted"

    def reconcile_all(self, *, now: datetime) -> dict:
        """全集群调谐：逐实例独立处理，单实例异常就地捕获（NFR-REL-02 故障隔离）。"""
        summary = _ReconcileSummary()
        for inst in self._registry.all():
            try:
                action = self.reconcile_one(inst.account_id, now=now)
                bucket = {"isolated_error": "isolated",
                          "restart_exhausted": "isolated"}.get(action, action)
                getattr(summary, bucket).append(inst.account_id)
            except Exception:  # noqa: BLE001 —— 单实例故障不扩散，循环继续其余实例
                summary.errors.append(inst.container_name)
        return summary.as_dict()

    # ---- 辅助 ----

    def backoff_delay(self, attempt: int) -> float:
        """指数退避：base × 2^(n-1)（30/60/120s 序列，SR §6.5）。"""
        return self._config.backoff_base_s * (2 ** (attempt - 1))

    def _wait(self, seconds: float) -> None:
        """等待注入点：留痕后交给 sleeper（测试零真实等待，生产 asyncio.sleep）。"""
        if seconds <= 0:
            return
        self.sleeps.append(seconds)
        self._sleeper(seconds)

    def _alert(self, level: str, type_: str, title: str, body: str) -> None:
        """告警投递（best-effort，通道故障不影响编排主流程）。"""
        if self._alerts is None:
            return
        try:
            self._alerts.send(level=level, type_=type_, title=title, body=body)
        except Exception:  # noqa: BLE001
            pass
