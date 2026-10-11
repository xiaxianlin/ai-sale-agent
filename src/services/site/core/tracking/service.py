# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""访客行为追踪（FR-SITE-002，SR-SITE-002 §6.2~§6.5）。

三件套：
- :class:`EventIngest` —— 批量上报接入：consent 双端门控（40302）/单批上限（40001）/
  ev_id 幂等（Redis SETNX 语义）/PII 脱敏（mask_fn 协议注入，NFR-SEC-01）/日限流（42901）；
- :class:`ProfileBuilder` + :class:`ContextInjector` —— 兴趣画像：访问次数×1.0 + 停留分钟×0.5
  加权、跨会话 0.8^ago 衰减、TOP N、轨迹环形；``render_prompt_hint`` 输出 ≤ 上限字符的
  「[访客画像] 访客浏览过 …」精简串供 FR-SITE-003 注入 Prompt；
- :class:`RetentionCleaner` —— 留存清理：恰 N 天保留 / N+1 清除（边界语义 created < cutoff），
  清理审计计数（NFR-SEC-03），画像冷存连带。

PII 脱敏经协议注入：生产注入 kb-service :class:`MaskingEngine` 词面级能力（SR-NFR-SEC-01），
本模块不直接 import 其具体类；demo 缺省 :func:`default_mask`（邮箱/电话 [redacted]）。
"""
from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from typing import Protocol
from dataclasses import dataclass, field
from typing import Any

from services.site.core.errors import SiteServiceError

# --------------------------------------------------------------------------- 常量与配置缺省

#: 事件类型枚举（03-数据库设计 §3.4 CHECK；lead 由留资写入）
EV_TYPES: tuple[str, ...] = (
    "page_view", "dwell", "product_view", "repeat_view",
    "source_visit", "add_cart", "abandon", "lead",
)
#: 单批上限（超限整批 40001）
BATCH_MAX_DEFAULT: int = 50
#: 单访客事件日上限（42901）
EVENTS_DAILY_MAX_DEFAULT: int = 2000
#: page 脱敏后截断长度 / referrer 截断长度
PAGE_MAX_LEN: int = 2048
REFERRER_MAX_LEN: int = 1024

# 画像权重（SR-SITE-002 §7：SITE_W_VIEW / SITE_W_DWELL / SITE_PROFILE_DECAY 等）
WEIGHT_VIEW: float = 1.0        # 每次访问
WEIGHT_DWELL: float = 0.5       # 每停留分钟
DECAY: float = 0.8              # 旧会话衰减系数（0.8^ago）
TOP_N: int = 5                  # 画像保留产品数
HIGH_BAR: float = 3.5           # 强兴趣阈值（level=high）
SEQ_LEN: int = 20               # 轨迹环形长度
SESSION_GAP_S: float = 1800.0   # 会话窗口切分间隔（>30min 切分）

DAY_S: float = 86400.0
#: 留存期钳制（天）：配置防呆下/上界
RETENTION_DAYS_MIN: int = 1
RETENTION_DAYS_MAX: int = 730


class TrackingError(SiteServiceError):
    """追踪域业务错误（40001/40302/42901）。"""


# --------------------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class TrackedEvent:
    """行为事件（visitor_event 行的进程内形态，SRS §12.1 VisitorEvent）。"""

    ev_id: str
    type: str
    page: str
    ts: float
    dwell_ms: int = 0
    referrer: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestResult:
    """批量接入结果（POST /site/events data 段）。"""

    accepted: int
    dropped: int


class ConsentGate(Protocol):
    """consent 门控协议（FR-SITE-007 ConsentManager 实现之；测试注入桩）。"""

    def allows_tracking(self, visitor_uid: str) -> bool: ...


class EventStore(Protocol):
    """事件存储协议（生产 = PG visitor_event）。"""

    def append(self, uid: str, tenant_id: int, events: list[TrackedEvent]) -> int: ...

    def events_of(self, uid: str) -> list[TrackedEvent]: ...

    def delete_uids(self, uids: tuple[str, ...] | list[str]) -> int: ...

    def delete_older_than(self, cutoff_ts: float) -> int: ...


class MemoryEventStore:
    """内存事件存储（demo）：uid → [TrackedEvent]。"""

    def __init__(self) -> None:
        self._rows: dict[str, list[TrackedEvent]] = {}

    def append(self, uid: str, tenant_id: int, events: list[TrackedEvent]) -> int:
        self._rows.setdefault(uid, []).extend(events)
        return len(events)

    def events_of(self, uid: str) -> list[TrackedEvent]:
        return list(self._rows.get(uid, ()))

    def delete_uids(self, uids: tuple[str, ...] | list[str]) -> int:
        removed = 0
        for uid in uids:
            removed += len(self._rows.pop(uid, ()))
        return removed

    def delete_older_than(self, cutoff_ts: float) -> int:
        """删除 created < cutoff（恰等于 cutoff 保留——留存期边界语义）。"""
        removed = 0
        for uid, rows in self._rows.items():
            kept = [e for e in rows if e.ts >= cutoff_ts]
            removed += len(rows) - len(kept)
            self._rows[uid] = kept
        return removed

    def all_rows(self) -> list[tuple[str, TrackedEvent]]:
        return [(uid, e) for uid, rows in self._rows.items() for e in rows]


# --------------------------------------------------------------------------- 脱敏（协议注入点）


_PII_RE = re.compile(r"([\w.+-]+@[\w-]+\.[\w.]+|\+\d{7,})")


def default_mask(text: str) -> str:
    """内置脱敏（demo）：邮箱/电话 → [redacted]（URL query 常见泄漏点，NFR-SEC-01）。

    生产注入 kb-service MaskingEngine 的词面级替换（SR-NFR-SEC-01 §6.1），协议形态一致。
    """
    return _PII_RE.sub("[redacted]", text)


def _sanitize(text: str, mask_fn: Callable[[str], str], limit: int) -> str:
    """脱敏 + 长度截断（单处统一入口）。"""
    return mask_fn(text or "")[:limit]


# --------------------------------------------------------------------------- 批量接入


class EventIngest:
    """批量上报接入（SR-SITE-002 §6.2）：门控 → 限流 → 枚举/幂等/脱敏 → 批量落库。"""

    def __init__(
        self,
        store: EventStore,
        gate: ConsentGate,
        *,
        mask_fn: Callable[[str], str] = default_mask,
        batch_max: int = BATCH_MAX_DEFAULT,
        daily_max: int = EVENTS_DAILY_MAX_DEFAULT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._gate = gate
        self._mask = mask_fn
        self._batch_max = batch_max
        self._daily_max = daily_max
        self._clock = clock
        self._seen_ev_ids: set[str] = set()   # ev_id 幂等（生产 = Redis SETNX，24h TTL）
        self._daily_counts: dict[tuple[str, str], int] = {}  # (uid, ymd) → 计数（生产 = Redis counter）

    def ingest(
        self, visitor_uid: str, tenant_id: int, consent_flag: bool, raw_events: list[dict[str, Any]]
    ) -> IngestResult:
        """接入一批原始事件：双端 consent 门控/批量上限先拒，枚举与幂等逐条消化。"""
        # ① consent 双端门控（06-安全 §9 红线：未同意不写 visitor_event）
        if not consent_flag or not self._gate.allows_tracking(visitor_uid):
            raise TrackingError(40302, "consent 未同意，拒绝采集")
        # ② 单批上限（超限整批拒绝，SDK 侧应自行分批）
        if len(raw_events) > self._batch_max:
            raise TrackingError(40001, f"events 超单批上限 {self._batch_max} 条")
        # ③ 日限流（防刷；按天计数，超限 42901）
        day_key = (visitor_uid, time.strftime("%Y%m%d", time.gmtime(self._clock())))
        if self._daily_counts.get(day_key, 0) >= self._daily_max:
            raise TrackingError(42901, "事件日限流")
        # ④ 逐条：枚举校验（dropped）→ ev_id 幂等（静默跳过）→ 脱敏截断 → 落库
        rows: list[TrackedEvent] = []
        dropped = 0
        for raw in raw_events:
            ev_type = str(raw.get("type", ""))
            if ev_type not in EV_TYPES:
                dropped += 1
                continue
            ev_id = str(raw.get("ev_id") or uuid.uuid4())
            if ev_id in self._seen_ev_ids:
                continue  # 客户端重试重放：仅首次写入
            self._seen_ev_ids.add(ev_id)
            rows.append(TrackedEvent(
                ev_id=ev_id,
                type=ev_type,
                page=_sanitize(str(raw.get("page", "")), self._mask, PAGE_MAX_LEN),
                ts=float(raw.get("ts") or self._clock()),
                dwell_ms=int(raw.get("dwell_ms") or 0),
                referrer=_sanitize(str(raw.get("referrer", "")), self._mask, REFERRER_MAX_LEN),
                payload=dict(raw.get("payload") or {}),
            ))
        if rows:
            self._store.append(visitor_uid, tenant_id, rows)
            self._daily_counts[day_key] = self._daily_counts.get(day_key, 0) + len(rows)
        return IngestResult(accepted=len(rows), dropped=dropped)


# --------------------------------------------------------------------------- 兴趣画像


def normalize_path(url: str) -> str:
    """URL → 站内路径（去 query/fragment；轨迹环形与「当前页」统一口径）。"""
    path = (url or "").split("?", 1)[0].split("#", 1)[0]
    if path.startswith(("http://", "https://")):
        rest = path.split("://", 1)[1]
        path = rest[rest.find("/"):] if "/" in rest else "/"
    return path or "/"


def sku_of_event(event: TrackedEvent) -> str | None:
    """事件 SKU 提取：payload.sku 优先，URL /products/{sku} 兜底（SDK 同法）。"""
    sku = event.payload.get("sku")
    if sku:
        return str(sku)
    matched = re.search(r"/products/([\w-]+)", event.page)
    return matched.group(1) if matched else None


@dataclass(frozen=True)
class Interest:
    """单 SKU 兴趣项（visitor_profile.interests 条目）。"""

    sku: str
    score: float
    views: int
    dwell_s: int
    level: str  # high | mid


@dataclass(frozen=True)
class VisitorProfile:
    """访客兴趣画像（visitor_profile 行的进程内形态，SRS §12.1/§14.4）。"""

    visitor_uid: str
    source: dict[str, Any] = field(default_factory=dict)
    interests: tuple[Interest, ...] = ()
    page_seq: tuple[str, ...] = ()
    flags: dict[str, Any] = field(default_factory=dict)
    sessions: int = 0
    last_active_at: float = 0.0

    def to_tags(self) -> tuple[str, ...]:
        """画像 → 客户档案标签（customer.profile_tags 契约，SR-SITE-003 §2.3）。"""
        tags = [f"interest:{i.sku}" for i in self.interests[:3]]
        if self.flags.get("cart_no_order"):
            tags.append("cart:1")
        return tuple(tags)


class ProfileBuilder:
    """画像构建器（SR-SITE-002 §6.3）：事件流 → 会话窗口聚合 → 加权衰减 → TOP N。"""

    def __init__(
        self,
        *,
        w_view: float = WEIGHT_VIEW,
        w_dwell: float = WEIGHT_DWELL,
        decay: float = DECAY,
        top_n: int = TOP_N,
        high_bar: float = HIGH_BAR,
        seq_len: int = SEQ_LEN,
        session_gap_s: float = SESSION_GAP_S,
    ) -> None:
        self._w_view = w_view
        self._w_dwell = w_dwell
        self._decay = decay
        self._top_n = top_n
        self._high_bar = high_bar
        self._seq_len = seq_len
        self._gap_s = session_gap_s

    def build(self, visitor_uid: str, events: list[TrackedEvent]) -> VisitorProfile:
        """全量重算（观察窗内单访客事件量小，毫秒级；生产另落 Redis 热存 + PG 冷存）。"""
        ordered = sorted(events, key=lambda e: e.ts)
        sessions = self._split_sessions(ordered)
        stats: dict[str, dict[str, int]] = {}
        scores: dict[str, float] = {}
        for ago, session in enumerate(reversed(sessions)):  # ago=0 最近会话；越旧衰减越狠
            per_sku = self._aggregate_sku(session)
            for sku, (views, dwell_s) in per_sku.items():
                acc = stats.setdefault(sku, {"views": 0, "dwell_s": 0})
                acc["views"] += views
                acc["dwell_s"] += dwell_s
                scores[sku] = scores.get(sku, 0.0) + (self._decay ** ago) * (
                    views * self._w_view + (dwell_s / 60.0) * self._w_dwell
                )
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[: self._top_n]
        interests = tuple(
            Interest(
                sku=sku,
                score=round(score, 1),
                views=stats[sku]["views"],
                dwell_s=stats[sku]["dwell_s"],
                level="high" if score >= self._high_bar else "mid",
            )
            for sku, score in ranked
        )
        page_views = [e for e in ordered if e.type == "page_view"]
        return VisitorProfile(
            visitor_uid=visitor_uid,
            source=self._last_source(ordered),
            interests=interests,
            page_seq=tuple(normalize_path(e.page) for e in page_views[-self._seq_len:]),
            flags={"cart_no_order": self._cart_no_order(ordered)},
            sessions=len(sessions),
            last_active_at=ordered[-1].ts if ordered else 0.0,
        )

    def _split_sessions(self, ordered: list[TrackedEvent]) -> list[list[TrackedEvent]]:
        """相邻事件间隔 > gap 即切分会话窗口。"""
        sessions: list[list[TrackedEvent]] = []
        current: list[TrackedEvent] = []
        for event in ordered:
            if current and event.ts - current[-1].ts > self._gap_s:
                sessions.append(current)
                current = []
            current.append(event)
        if current:
            sessions.append(current)
        return sessions

    @staticmethod
    def _aggregate_sku(session: list[TrackedEvent]) -> dict[str, tuple[int, int]]:
        """会话内按 SKU 聚合 product_view 次数与 dwell 秒。"""
        per_sku: dict[str, list[list[int]]] = {}
        for event in session:
            sku = sku_of_event(event)
            if sku is None:
                continue
            bucket = per_sku.setdefault(sku, [0, 0])
            if event.type == "product_view":
                bucket[0] += 1
            elif event.type == "dwell":
                bucket[1] += event.dwell_ms // 1000
            elif event.type == "repeat_view":
                # repeat_view.nth 为该 SKU 会话内累计次位（SDK 第 N 次访问时发）：取大者防重复计数
                nth = int(event.payload.get("nth", 0) or 0)
                if nth > bucket[0]:
                    bucket[0] = nth
        return {sku: (v[0], v[1]) for sku, v in per_sku.items() if v[0] or v[1]}

    @staticmethod
    def _last_source(ordered: list[TrackedEvent]) -> dict[str, Any]:
        """来源渠道：最后一次 source_visit 的 utm/referrer（每会话首报语义）。"""
        for event in reversed(ordered):
            if event.type == "source_visit":
                source = {"referrer": event.referrer}
                source.update({f"utm_{k}": v for k, v in event.payload.items()})
                utm_source = event.payload.get("source")
                if utm_source:
                    source["utm_source"] = str(utm_source)
                return source
        return {}

    @staticmethod
    def _cart_no_order(ordered: list[TrackedEvent]) -> list[str]:
        """加购未下单 SKU：add_cart 后无 lead（order 回执）抵消（销售信号 flags）。"""
        cart: list[str] = []
        for event in ordered:
            sku = sku_of_event(event)
            if sku is None:
                continue
            if event.type == "add_cart" and sku not in cart:
                cart.append(sku)
            elif event.type == "lead" and sku in cart:
                cart.remove(sku)
        return cart


class ProfileStore(Protocol):
    """画像存储协议（生产 = Redis 热存 + PG visitor_profile 冷存）。"""

    def save(self, profile: VisitorProfile) -> None: ...

    def load(self, uid: str) -> VisitorProfile | None: ...

    def delete_uids(self, uids: tuple[str, ...] | list[str]) -> int: ...


class MemoryProfileStore:
    """内存画像存储（demo）。"""

    def __init__(self) -> None:
        self._rows: dict[str, VisitorProfile] = {}

    def save(self, profile: VisitorProfile) -> None:
        self._rows[profile.visitor_uid] = profile

    def load(self, uid: str) -> VisitorProfile | None:
        return self._rows.get(uid)

    def delete_uids(self, uids: tuple[str, ...] | list[str]) -> int:
        removed = 0
        for uid in uids:
            if self._rows.pop(uid, None) is not None:
                removed += 1
        return removed

    def all(self) -> list[VisitorProfile]:
        return list(self._rows.values())


# --------------------------------------------------------------------------- 注入渲染


def render_prompt_hint(profile: VisitorProfile, *, max_chars: int = 200) -> str:
    """画像 → Prompt 注入串（SR-SITE-002 §6.4 render）：只保留销售可立即引用的事实。

    形态：``[访客画像] 访客浏览过 ST-3200(3次/8分钟,强)、MX-100(1次/2分钟); 来源:google;
    当前页:/products/st-3200; 加购未下单:ST-3200``——截断保护（画像让位于对话主上下文）。
    """
    source = profile.source or {}
    utm = str(source.get("utm_source") or "")
    referrer = str(source.get("referrer") or "")
    src = utm or (referrer.split("://")[-1].split("/")[0] if referrer else "") or "direct"
    parts: list[str] = ["[访客画像]"]
    if profile.interests:
        items = "、".join(
            f"{i.sku}({i.views}次/{i.dwell_s // 60}分钟{',强' if i.level == 'high' else ''})"
            for i in profile.interests[:3]
        )
        parts.append(f"访客浏览过 {items}")
    parts.append(f"来源:{src}")
    parts.append(f"当前页:{profile.page_seq[-1] if profile.page_seq else '-'}")
    if profile.flags.get("cart_no_order"):
        parts.append(f"加购未下单:{profile.flags['cart_no_order'][0]}")
    return "; ".join(parts)[:max_chars]


class ContextInjector:
    """上下文注入器（FR-SITE-003 消费）：会话建立必注入 + 每 N 轮刷新。"""

    def __init__(self, profiles: ProfileStore, *, inject_every_n: int = 3, max_chars: int = 200) -> None:
        self._profiles = profiles
        self._every_n = max(int(inject_every_n), 1)
        self._max_chars = max_chars

    def prompt_hint(self, visitor_uid: str, round_no: int) -> str | None:
        """画像缺失/非注入轮 → None（降级无画像应答，不阻塞对话）。"""
        if round_no != 0 and round_no % self._every_n != 0:
            return None
        profile = self._profiles.load(visitor_uid)
        if profile is None:
            return None
        return render_prompt_hint(profile, max_chars=self._max_chars)


# --------------------------------------------------------------------------- 审计与留存清理


class AuditTrail(Protocol):
    """审计轨迹协议（生产 = audit_log 表，NFR-SEC-03）。"""

    def append(self, event: str, **fields: Any) -> None: ...


class MemoryAuditTrail:
    """内存审计轨迹（demo）。"""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def append(self, event: str, **fields: Any) -> None:
        self.entries.append({"event": event, **fields})

    def filter(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["event"] == event]


class RetentionCleaner:
    """留存清理（SR-SITE-002 §6.5 / FR-SITE-007）：边界语义 + 审计计数 + 幂等重跑。

    边界：``created_at < cutoff`` 清除、恰等于 cutoff 保留（「恰 N 天保留 / N+1 清除」）。
    """

    def __init__(
        self,
        store: EventStore,
        audit: AuditTrail,
        *,
        profile_store: ProfileStore | None = None,
        retention_days: int = 365,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._audit = audit
        self._profiles = profile_store
        self._days = retention_days
        self._clock = clock

    def sweep(self, *, now: float | None = None) -> dict[str, Any]:
        """执行一次清理：返回 {cutoff, deleted(事件数), visitor_profile(画像数)} 并写审计。"""
        timestamp = self._clock() if now is None else now
        days = min(max(int(self._days), RETENTION_DAYS_MIN), RETENTION_DAYS_MAX)
        cutoff = timestamp - days * DAY_S
        deleted = self._store.delete_older_than(cutoff)
        swept_profiles = 0
        if self._profiles is not None:
            for profile in self._profiles.all():
                if profile.last_active_at < cutoff:
                    swept_profiles += self._profiles.delete_uids([profile.visitor_uid])
        self._audit.append(
            "tracking.retention_sweep", cutoff=cutoff, deleted=deleted,
            visitor_profile=swept_profiles,
        )
        return {"cutoff": cutoff, "deleted": deleted, "visitor_profile": swept_profiles}


__all__ = [
    "BATCH_MAX_DEFAULT",
    "ContextInjector",
    "DECAY",
    "EVENTS_DAILY_MAX_DEFAULT",
    "EV_TYPES",
    "EventIngest",
    "EventStore",
    "HIGH_BAR",
    "IngestResult",
    "Interest",
    "MemoryAuditTrail",
    "MemoryEventStore",
    "MemoryProfileStore",
    "PAGE_MAX_LEN",
    "ProfileBuilder",
    "ProfileStore",
    "REFERRER_MAX_LEN",
    "RetentionCleaner",
    "SEQ_LEN",
    "SESSION_GAP_S",
    "TOP_N",
    "TrackingError",
    "TrackedEvent",
    "VisitorProfile",
    "AuditTrail",
    "ConsentGate",
    "WEIGHT_DWELL",
    "WEIGHT_VIEW",
    "default_mask",
    "normalize_path",
    "render_prompt_hint",
    "sku_of_event",
]
