# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""管理后台 V3 集群状态墙：心跳汇聚 + 健康判定 + WS 增量语义（FR-SC-005，SR-SC-005 §6.1）。

- 监控读写分离：状态墙不直连 28 个容器——agent-runner 定时上报心跳（Redis HASH
  `inst:hb:{account_id}`，TTL=3×上报间隔），汇聚器周期扫描建快照；
- 健康三态：ok（新鲜心跳）/ warn（ip 异常或容器 error）/ down（心跳超时，红色）；
- WS 帧语义（/ws/cluster）：连接即发全量帧；此后增量为「版本号 + delta」——
  数据无变化不增版本；resume 携本地版本号，最新 → ack，落后且在增量日志覆盖内
  → 合并 delta，过于落后 → 重发全量（断线恢复兜底）；
- 失联告警：连续 2 个汇聚周期 down 才触发 mid 告警（§3.5 alerts 路由，防抖动）；
  心跳恢复即回 ok 并清零连击；
- 影响面（NFR-REL-02）：单实例失联仅该卡片变红，其余 27 张不受影响。

demo 形态：进程内快照与版本日志（生产 = Redis `cluster:snapshot`/`:ver` 原子更新
+ asyncio 常驻汇聚任务，§6.1 aggregate_loop）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

#: 实例类型（4 主账号 + 24 子账号）
ACCOUNT_TYPES: tuple[str, ...] = ("main", "sub")
#: IP 绑定异常态（warn 判定；凭证永不回显，NFR-SEC-04）
IP_ABNORMAL: tuple[str, ...] = ("stale", "switched")
#: 健康三态
HEALTH_STATES: tuple[str, ...] = ("ok", "warn", "down")


@dataclass(frozen=True)
class InstanceMeta:
    """实例档案（agent_instance 行语义；FR-SC-001 部署态 + 监控锚点）。"""

    account_id: int
    account_name: str
    account_type: str = "sub"                     # main / sub
    container_name: str = ""
    proxy_ip: str = ""                            # 绑定占位 <PROXY-IP-NN>，一实例一 IP
    gray_group: str = "full"                      # 灰度分组（pilot / full，FR-SC-007）


@dataclass(frozen=True)
class Heartbeat:
    """心跳帧（agent-runner 上报；`inst:hb:{account_id}` HASH 语义）。"""

    ts: datetime
    state: str = "running"                        # running / stopped / error / migrating
    sessions_today: int = 0
    ops_today: int = 0
    ip_status: str = "ok"                         # ok / stale / switched


class ClusterWall:
    """集群状态墙：心跳汇聚 → 快照/版本/delta → 帧构造（SR-SC-005 §6.1 工程级形态）。

    :param hb_timeout_s: 心跳超时判 down（默认 45s ≈ 3 个上报周期）；
    :param down_alert_cycles: 连续 down 周期数达阈值触发告警（默认 2）；
    :param max_delta_log: 增量日志保留版本数（超出即全量兜底）；
    :param clock: 时钟注入（拔线演练拨钟）。
    """

    def __init__(
        self,
        *,
        hb_timeout_s: float = 45.0,
        down_alert_cycles: int = 2,
        max_delta_log: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._hb_timeout = hb_timeout_s
        self._down_alert_cycles = down_alert_cycles
        self._max_delta_log = max_delta_log
        self._clock = clock or (lambda: datetime.now())
        self._lock = threading.RLock()
        self._instances: dict[int, InstanceMeta] = {}
        self._heartbeats: dict[int, Heartbeat] = {}
        self._snapshot: dict[int, dict[str, object]] = {}
        self._ver = 0
        self._delta_log: list[tuple[int, dict[int, dict[str, object]]]] = []  # (ver, changes)
        self._down_streak: dict[int, int] = {}
        self.alerts: list[dict[str, object]] = []   # 进程内告警事件（生产 = §3.5 alerts）

    # ---------------------------------------------------------------- 注册与上报

    def register(self, meta: InstanceMeta) -> None:
        """注册实例档案（重复注册覆盖）。"""
        with self._lock:
            self._instances[meta.account_id] = meta

    def heartbeat(self, account_id: int, hb: Heartbeat) -> None:
        """心跳上报（未注册账号拒绝——40401 语义）。"""
        with self._lock:
            if account_id not in self._instances:
                raise KeyError(f"实例 {account_id} 未注册")
            self._heartbeats[account_id] = hb

    # ---------------------------------------------------------------- 汇聚

    def aggregate(self, *, now: datetime | None = None) -> dict[str, object]:
        """汇聚一个周期：全实例建卡 → 逐字段 diff → 版本号/delta 日志/失联告警。"""
        moment = now if now is not None else self._clock()
        with self._lock:
            snapshot: dict[int, dict[str, object]] = {}
            for account_id, meta in self._instances.items():
                snapshot[account_id] = self._build_card(account_id, moment)
            changes: dict[int, dict[str, object]] = {}
            for account_id, card in snapshot.items():
                previous = self._snapshot.get(account_id)
                if previous is None or _diff_fields(previous, card):
                    changes[account_id] = card
            if changes:
                self._ver += 1
                self._delta_log.append((self._ver, changes))
                if len(self._delta_log) > self._max_delta_log:
                    self._delta_log = self._delta_log[-self._max_delta_log:]
            self._snapshot = snapshot
            self._evaluate_down_alerts(moment)
            return {"ver": self._ver, "changed": len(changes), "changes": dict(changes)}

    def _build_card(self, account_id: int, now: datetime) -> dict[str, object]:
        """单实例卡片（健康/会话量/操作量/IP 状态；proxy_ip 凭证不回显）。"""
        meta = self._instances[account_id]
        hb = self._heartbeats.get(account_id)
        age = (now - hb.ts).total_seconds() if hb is not None else None
        if hb is None or age is None or age > self._hb_timeout:
            health = "down"
        elif hb.ip_status in IP_ABNORMAL or hb.state == "error":
            health = "warn"
        else:
            health = "ok"
        return {
            "account_id": account_id,
            "account_name": meta.account_name,
            "account_type": meta.account_type,
            "gray_group": meta.gray_group,
            "health": health,
            "state": hb.state if hb is not None else "unknown",
            "sessions_today": hb.sessions_today if hb is not None else 0,
            "ops_today": hb.ops_today if hb is not None else 0,
            "ip_status": hb.ip_status if hb is not None else "unknown",
            "last_hb_age_s": round(age, 1) if age is not None else None,
            "highlight": health != "ok",
        }

    def _evaluate_down_alerts(self, now: datetime) -> None:
        """失联告警：连续 N 周期 down 才告警；恢复清零连击（单实例故障不影响全局）。"""
        for account_id, card in self._snapshot.items():
            if card["health"] == "down":
                streak = self._down_streak.get(account_id, 0) + 1
                self._down_streak[account_id] = streak
                if streak == self._down_alert_cycles and not any(
                        a["type"] == "instance_down" and a["account_id"] == account_id
                        for a in self.alerts):
                    # 同一失联事件只告警一次（恢复清零后再次失联不重复轰炸，§3.5 防抖）
                    self.alerts.append({
                        "type": "instance_down", "level": "mid",
                        "account_id": account_id,
                        "account_name": self._instances[account_id].account_name,
                        "title": f"实例 {self._instances[account_id].account_name} 心跳失联",
                        "at": now.isoformat(), "link": "/cluster",
                    })
            else:
                self._down_streak[account_id] = 0

    # ---------------------------------------------------------------- 查询与帧

    def cards(self) -> list[dict[str, object]]:
        """状态墙视图：按当前实例/心跳即时建卡（未显式 aggregate 也可读），
        异常（down/warn）置顶 + 呼吸高亮标记（一眼定位）。"""
        with self._lock:
            cards = [self._build_card(account_id, self._clock())
                     for account_id in self._instances]
        return sorted(cards, key=lambda c: (c["health"] == "ok", int(c["account_id"])))

    def health_of(self, account_id: int) -> str | None:
        """单实例健康态（未汇聚/未注册返回 None）。"""
        with self._lock:
            card = self._snapshot.get(account_id)
            return str(card["health"]) if card is not None else None

    @property
    def abnormal_count(self) -> int:
        """异常卡片数（warn+down）。"""
        with self._lock:
            return sum(1 for card in self._snapshot.values() if card["health"] != "ok")

    @property
    def ver(self) -> int:
        """当前快照版本号。"""
        with self._lock:
            return self._ver

    def frame(self, client_ver: int | None = None) -> dict[str, object]:
        """WS 帧构造：None → full；最新 → ack；落后且可追 → 合并 delta；过旧 → full。"""
        with self._lock:
            if client_ver is None:
                return {"type": "full", "ver": self._ver,
                        "snapshot": {str(k): dict(v) for k, v in self._snapshot.items()}}
            if client_ver == self._ver:
                return {"type": "ack"}
            merged: dict[int, dict[str, object]] = {}
            for ver, changes in self._delta_log:
                if ver > client_ver:
                    merged.update(changes)
            if client_ver < self._ver - len(self._delta_log):
                return {"type": "full", "ver": self._ver,
                        "snapshot": {str(k): dict(v) for k, v in self._snapshot.items()}}
            # delta 键保持 int（账号 ID 可哈希集合断言；JSON 序列化由传输层处理）
            return {"type": "delta", "ver": self._ver, "changes": {k: dict(v) for k, v in merged.items()}}


def _diff_fields(previous: dict[str, object], current: dict[str, object]) -> bool:
    """逐字段比对（last_hb_age_s 等单调易变字段不计入 diff，防无变化假增量）。"""
    volatile = {"last_hb_age_s"}
    for key, value in current.items():
        if key in volatile:
            continue
        if previous.get(key) != value:
            return True
    return False


__all__ = [
    "ACCOUNT_TYPES", "IP_ABNORMAL", "HEALTH_STATES",
    "InstanceMeta", "Heartbeat", "ClusterWall",
]
