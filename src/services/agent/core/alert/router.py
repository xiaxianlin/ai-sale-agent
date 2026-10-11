# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""告警路由引擎（FR-AG-007，SR-AG-007 §6；迭代 2.3 demo 口径）。

职责（全部「需要人立即知道」事件的**唯一汇聚与分发点**）：
- level × type 双维路由：level 决定渠道（high→钉钉机器人+微信即时；mid→钉钉群；
  low→后台待办），type 决定默认级别（big_customer/risk_hit→high、system_error→mid）；
- 风暴防护第一层：同源去重聚合（dedup_key + 5min 窗口）——重复事件不推送、
  仅对首条 agg_count 累加（后台可见风暴规模）；
- 处置闭环状态机：firing→acknowledged→resolved→archived，非法迁移拒绝（全留痕）；
- 升级机制：high firing 超 15min 未确认 → 升级主管（幂等，确认后停止）；
- 渠道韧性：5s 超时 ×3 重试 → 全部失败时备用通道兜底（仅 high）；
- 大客户判定复核：customer.grade=='A' 强制 high；复核不过降级 low 并留痕（防误报）。

demo 形态边界：钉钉/企微推送为 Protocol+Fake（生产 webhook 随部署配置）；Redis
Stream 削峰队列由生产形态承载——本模块保持「受理→去重→分发→回写」同语义。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Callable, Mapping, Protocol


class AlertLevel(StrEnum):
    """告警级别（SR-AG-007 §6.1）。"""

    HIGH = "high"    # 钉钉+微信即时 + 回执要求 + 15min 未确认升级
    MID = "mid"      # 钉钉群消息
    LOW = "low"      # 后台待办


#: type → 默认 level（04 §3.5 注释映射；调用方可显式指定 level）
TYPE_DEFAULT_LEVEL: dict[str, str] = {
    "big_customer": AlertLevel.HIGH,
    "risk_hit": AlertLevel.HIGH,
    "system_error": AlertLevel.MID,
}

#: level → 渠道路由（04 §3.5 路由原文）
ROUTE_TABLE: dict[str, tuple[str, ...]] = {
    AlertLevel.HIGH: ("dingtalk_robot", "wechat_app"),
    AlertLevel.MID: ("dingtalk_group",),
    AlertLevel.LOW: ("backend_todo",),
}

#: 处置闭环状态机（SR-AG-007 §6.5）
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "firing": frozenset({"acknowledged"}),
    "acknowledged": frozenset({"resolved"}),
    "resolved": frozenset({"archived"}),
    "archived": frozenset(),
}


class InvalidTransitionError(ValueError):
    """非法状态迁移（firing→resolved 等）：API 层映射 40901。"""


class AlertChannel(Protocol):
    """告警渠道适配器协议（生产 = 钉钉/企微 webhook；demo = Fake 通道）。"""

    name: str

    def send(self, alert: "Alert") -> None: ...


@dataclass
class DeliveryRecord:
    """渠道送达明细（alert_delivery 行语义：逐渠道状态回写与重试留痕）。"""

    channel: str
    status: str = "queued"          # queued / sent / failed / fallback
    attempts: int = 0
    last_error: str | None = None


@dataclass
class Alert:
    """告警事件（alert_event 行语义：受理即落库，全生命周期可查）。"""

    uid: str
    level: str
    type: str
    title: str
    body: str
    dedup_key: str
    created_at: datetime
    status: str = "firing"
    agg_count: int = 1              # 5min 窗口内同源聚合次数（风暴规模）
    escalated: bool = False
    acked_by: str | None = None
    acked_at: datetime | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    deliveries: list[DeliveryRecord] = field(default_factory=list)


@dataclass(frozen=True)
class SubmitResult:
    """上报结果（§5-1 响应的进程内形态）。"""

    uid: str
    aggregated: bool
    level: str
    channels: tuple[str, ...]


class FakeChannel:
    """记录型 Fake 渠道（demo/测试：断言触达与级别；生产 webhook 同协议替换）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.sent.append(alert)


class FlakyChannel:
    """间歇失败渠道：前 fail_times 次抛错（重试链路测试入口），之后成功。"""

    def __init__(self, name: str, *, fail_times: int = 2) -> None:
        self.name = name
        self.fail_times = fail_times
        self.attempts = 0
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise TimeoutError(f"{self.name} 第 {self.attempts} 次推送超时")
        self.sent.append(alert)


class AlwaysFailingChannel:
    """恒失败渠道（兜底链路测试入口：3 次重试耗尽 → 备用通道）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.attempts = 0

    def send(self, alert: Alert) -> None:
        self.attempts += 1
        raise ConnectionError(f"{self.name} 不可达")


class AlertRouter:
    """告警路由引擎：受理→去重聚合→分级分发→回写/重试→兜底→闭环/升级。

    :param channels: 渠道注册表（name → 适配器；路由表引用的渠道须已注册）；
    :param fallback_channel: 备用通道（仅 high 全部主渠道失败时兜底一次）；
    :param dedup_window_s: 同源去重窗口（默认 300s = 5min）；
    :param ack_timeout_s: 未确认升级阈值（默认 900s = 15min）；
    :param retry_max: 渠道重试次数（默认 3）；
    :param supervisor_channel: 升级通知渠道名（默认 "supervisor"）。
    """

    def __init__(
        self,
        *,
        channels: Mapping[str, AlertChannel],
        fallback_channel: AlertChannel | None = None,
        dedup_window_s: float = 300.0,
        ack_timeout_s: float = 900.0,
        retry_max: int = 3,
        audit: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        supervisor_channel: str = "supervisor",
    ) -> None:
        self._channels = dict(channels)
        self._fallback = fallback_channel
        self._dedup_window = dedup_window_s
        self._ack_timeout = ack_timeout_s
        self._retry_max = retry_max
        self._audit = audit
        self._clock = clock or (lambda: datetime.now())
        self._supervisor = supervisor_channel
        self._alerts: dict[str, Alert] = {}
        self._order: list[str] = []
        self._dedup: dict[str, tuple[str, datetime]] = {}   # dedup_key → (uid, 首条时刻)
        self._fallback_done: set[str] = set()

    # ---------------------------------------------------------------- 受理

    def submit(
        self,
        *,
        type: str,  # noqa: A002 —— 与接口契约字段名一致
        title: str,
        body: str,
        level: str | None = None,
        dedup_key: str | None = None,
        customer_grade: str | None = None,
        now: datetime | None = None,
    ) -> SubmitResult:
        """告警上报（§5-1）：大客户复核 → 去重聚合 → 落库 → 分发。"""
        moment = now if now is not None else self._clock()
        resolved_level = level or TYPE_DEFAULT_LEVEL.get(type, AlertLevel.MID)
        if type == "big_customer":
            resolved_level = self._recheck_big_customer(resolved_level, customer_grade)
        key = dedup_key or f"{type}:{hashlib.sha1(body.encode()).hexdigest()[:12]}"
        hit = self._dedup.get(key)
        if hit is not None and (moment - hit[1]).total_seconds() < self._dedup_window:
            first = self._alerts[hit[0]]                   # ② 风暴防护：仅聚合计数
            first.agg_count += 1
            return SubmitResult(first.uid, True, first.level,
                                ROUTE_TABLE.get(first.level, ()))
        alert = Alert(
            uid=uuid.uuid4().hex[:16], level=resolved_level, type=type,
            title=title, body=body, dedup_key=key, created_at=moment,
        )
        self._alerts[alert.uid] = alert
        self._order.append(alert.uid)
        self._dedup[key] = (alert.uid, moment)             # ③ 首条标记（窗口 TTL 语义）
        self._write_audit({"event": "alert_received", "alert_uid": alert.uid,
                           "level": alert.level, "type": type, "at": moment.isoformat()})
        self._dispatch(alert)                              # ④ 分级分发
        return SubmitResult(alert.uid, False, alert.level, ROUTE_TABLE.get(alert.level, ()))

    def _recheck_big_customer(self, level: str, grade: str | None) -> str:
        """大客户判定复核（§6.4）：grade=='A' 强制 high；提供且非 A 降级 low 并留痕。

        grade 未提供（None）= 调用方未携带分级，保持 type 默认级别（不因缺复核
        信息而误降级——复核降级须以「证据不过」为前提）。
        """
        if grade == "A":
            return AlertLevel.HIGH
        if grade is None:
            return level
        self._write_audit({"event": "alert_downgraded", "grade": grade,
                           "reason": "big_customer 复核不过（非 A 级），防误报降级"})
        return AlertLevel.LOW

    # ---------------------------------------------------------------- 分发与韧性

    def _dispatch(self, alert: Alert) -> None:
        """分级分发：按路由表逐渠道投递（重试 + 兜底在单渠道内收敛）。"""
        for channel_name in ROUTE_TABLE.get(alert.level, ()):
            self._dispatch_one(alert, channel_name)

    def _dispatch_one(self, alert: Alert, channel_name: str) -> None:
        """单渠道投递：×retry_max 重试；high 全败 → 备用通道兜底一次（每告警）。"""
        record = DeliveryRecord(channel=channel_name)
        alert.deliveries.append(record)
        channel = self._channels.get(channel_name)
        for attempt in range(1, self._retry_max + 1):
            record.attempts = attempt
            if channel is None:
                record.last_error = f"渠道 {channel_name} 未注册"
                break
            try:
                channel.send(alert)
                record.status = "sent"
                self._write_audit({"event": "alert_delivered", "alert_uid": alert.uid,
                                   "channel": channel_name, "attempts": attempt})
                return
            except Exception as exc:  # noqa: BLE001 —— 重试链路：失败记因再试
                record.last_error = f"{type(exc).__name__}: {exc}"
        record.status = "failed"
        # 备用通道兜底（仅 high；每告警至多一次——摘要脱敏由通道适配器承载）
        if alert.level == AlertLevel.HIGH and self._fallback is not None \
                and alert.uid not in self._fallback_done:
            self._fallback_done.add(alert.uid)
            try:
                self._fallback.send(alert)
                record.status = "fallback"
                self._write_audit({"event": "alert_fallback_sms", "alert_uid": alert.uid,
                                   "channel": self._fallback.name})
            except Exception:  # noqa: BLE001 —— 兜底再失败：failed 待人工巡检
                pass

    # ---------------------------------------------------------------- 闭环状态机

    def _transition(
        self, uid: str, to_status: str, *, operator: str, now: datetime,
    ) -> Alert:
        """闭环状态机内核：迁移表校验 + 操作人/时刻回填 + 审计留痕。"""
        alert = self._require(uid)
        if to_status not in VALID_TRANSITIONS[alert.status]:
            raise InvalidTransitionError(
                f"非法状态迁移：{alert.status} → {to_status}（处置闭环迁移表无此路径）"
            )
        alert.status = to_status
        if to_status == "acknowledged":
            alert.acked_by, alert.acked_at = operator, now
        elif to_status == "resolved":
            alert.resolved_by, alert.resolved_at = operator, now
        self._write_audit({"event": f"alert_{to_status}", "alert_uid": uid,
                           "operator": operator, "at": now.isoformat()})
        return alert

    def ack(self, uid: str, *, operator: str, now: datetime | None = None) -> Alert:
        """人工确认（响应时长统计终点）。"""
        return self._transition(uid, "acknowledged", operator=operator,
                                now=now or self._clock())

    def resolve(self, uid: str, *, operator: str, now: datetime | None = None) -> Alert:
        """处置完成。"""
        return self._transition(uid, "resolved", operator=operator,
                                now=now or self._clock())

    def archive(self, uid: str, *, now: datetime | None = None) -> Alert:
        """归档（resolve 后 7 天自动归档的进程内形态；系统操作者）。"""
        return self._transition(uid, "archived", operator="system:archive",
                                now=now or self._clock())

    def escalate_overdue(self, *, now: datetime) -> list[Alert]:
        """升级扫描（jobs/escalate_job 的进程内形态，幂等可重入）。

        high + firing + 超 ack_timeout 未确认 → escalated 置位 + 升级渠道通知 +
        审计留痕；已确认告警不再升级。
        """
        escalated: list[Alert] = []
        for uid in self._order:
            alert = self._alerts[uid]
            if alert.level != AlertLevel.HIGH or alert.status != "firing" or alert.escalated:
                continue
            if (now - alert.created_at).total_seconds() < self._ack_timeout:
                continue
            alert.escalated = True
            channel = self._channels.get(self._supervisor)
            if channel is not None:
                try:
                    channel.send(alert)
                except Exception:  # noqa: BLE001 —— 升级渠道故障不阻断扫描
                    pass
            self._write_audit({"event": "alert_escalated", "alert_uid": uid,
                               "at": now.isoformat()})
            escalated.append(alert)
        return escalated

    # ---------------------------------------------------------------- 查询面

    def get(self, uid: str) -> Alert:
        """按 uid 读取告警（不存在抛 KeyError，API 层映射 40401）。"""
        return self._require(uid)

    def list(
        self,
        *,
        status: str | None = None,
        level: str | None = None,
        type: str | None = None,  # noqa: A002 —— 与接口契约字段名一致
    ) -> list[Alert]:
        """告警列表（后台告警中心数据源；三维过滤）。"""
        rows = [self._alerts[uid] for uid in self._order]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        if level is not None:
            rows = [row for row in rows if row.level == level]
        if type is not None:
            rows = [row for row in rows if row.type == type]
        return rows

    # ---------------------------------------------------------------- 内部

    def _require(self, uid: str) -> Alert:
        alert = self._alerts.get(uid)
        if alert is None:
            raise KeyError(f"告警 {uid} 不存在")
        return alert

    def _write_audit(self, payload: dict[str, object]) -> None:
        """审计投递（best-effort：全量留审计，NFR-SEC-03）。"""
        if self._audit is None:
            return
        try:
            self._audit(payload)
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "AlertLevel",
    "TYPE_DEFAULT_LEVEL",
    "ROUTE_TABLE",
    "VALID_TRANSITIONS",
    "InvalidTransitionError",
    "AlertChannel",
    "DeliveryRecord",
    "Alert",
    "SubmitResult",
    "FakeChannel",
    "FlakyChannel",
    "AlwaysFailingChannel",
    "AlertRouter",
]
