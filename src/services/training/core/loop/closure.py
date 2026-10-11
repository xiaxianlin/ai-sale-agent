# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""培训效果闭环（FR-TR-008，SR-TR-008 §6.1~§6.5：三源缺口 → 归一化聚合 → 审核回流 → 闭环率）。

事件-清单两级模型（决策 D1）与人工确认红线（决策 D3，与 §7.4 敏感操作红线同源的审慎原则）：
- **三源采集**（§6.1）：mentor_zero_hit（FR-TR-001 导师零命中）/ drill_low_score
  （FR-TR-003 演练低分归因）/ exam_wrong_point（FR-TR-007 考试错题知识点）统一入口；
  ``agent_zero_hit`` 为 P2 预留枚举（本方案不实现）；dedup_key = sha256(source, ref_type,
  ref_id, 归一化原文) 幂等去重，重复上报不落新行；
- **归一化聚合**（§6.2）：知识点标签归一（context.point_hint 精确匹配优先、原文归一化
  兜底——同知识点稳定合并同一 point_tag）→ 缺口清单 UPSERT（频次/最近发生/样本上限）；
  resolved 缺口再现计 hit 不计频次（闭环第三段「再命中」信号）；
- **审核回流**（§6.4）：open → drafted →(批准) resolved / (驳回) rejected；批准必经
  reflow_hook 注入点（demo 确定性 uid；生产复用 FR-KB-020 创建 + FR-KB-017 增量索引，
  LLM 起草稿仅存 draft_item **永不直接入库**）；驳回 reason 必填留痕；version 乐观锁
  防并发审核冲突（40901）；
- **话术沉淀**（§6.3）：drill_supervisor_marked / review_report 两条来源，入库前经
  mask_fn 脱敏注入（生产复用 FR-KB-011 规则引擎），批准回流 kb_type='skill'；
- **闭环率**（§6.5）：closure_rate = 窗口内识别且 resolved 且再命中 / 窗口内识别。

依据文档：docs/07-实现方案/TR/SR-TR-008-培训效果闭环.md §6.1~§6.5、§7、§9。
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from services.training.core.tutor.gap import normalize_question

# --------------------------------------------------------------------------- 常量（SR §4/§6 枚举）

#: 缺口来源枚举（SR §4 gap_event.source CHECK；agent_zero_hit 为 P2 预留，本方案不实现）
GAP_SOURCE_MENTOR: str = "mentor_zero_hit"
GAP_SOURCE_DRILL: str = "drill_low_score"
GAP_SOURCE_EXAM: str = "exam_wrong_point"
GAP_SOURCE_AGENT: str = "agent_zero_hit"
GAP_SOURCES: tuple[str, ...] = (GAP_SOURCE_MENTOR, GAP_SOURCE_DRILL, GAP_SOURCE_EXAM, GAP_SOURCE_AGENT)

#: 缺口状态机：open →(起草) drafted →(批准) resolved / (驳回) rejected
GAP_STATUS_OPEN: str = "open"
GAP_STATUS_DRAFTED: str = "drafted"
GAP_STATUS_RESOLVED: str = "resolved"
GAP_STATUS_REJECTED: str = "rejected"
GAP_REVIEWABLE: tuple[str, ...] = (GAP_STATUS_OPEN, GAP_STATUS_DRAFTED)

#: 候选话术来源与状态（SR §4 candidate_talk CHECK）
TALK_SOURCE_MARK: str = "drill_supervisor_marked"
TALK_SOURCE_REVIEW: str = "review_report"
TALK_SOURCES: tuple[str, ...] = (TALK_SOURCE_MARK, TALK_SOURCE_REVIEW)
TALK_STATUS_PENDING: str = "pending"
TALK_STATUS_APPROVED: str = "approved"
TALK_STATUS_REJECTED: str = "rejected"

#: 来源 → 建议归属知识库（缺口默认 kb_type 按目标库选择；话术固定 'skill'）
KB_HINT_BY_SOURCE: dict[str, str] = {
    GAP_SOURCE_MENTOR: "product",
    GAP_SOURCE_DRILL: "skill",
    GAP_SOURCE_EXAM: "product",
    GAP_SOURCE_AGENT: "product",
}

#: 归一化标签截断长度（防超长原文撑爆聚合键）
_TAG_MAX_LEN: int = 60


def normalize_point(raw_text: str, context: Mapping[str, Any] | None = None) -> str:
    """知识点标签归一化（SR §6.2 两步法的确定性子集）：point_hint 精确匹配优先。

    考试错题等带知识点标注的事件以标注为准（题面不同同知识点仍合并）；
    无标注回落原文归一化（去空白/标点、小写、截断，复用 FR-TR-001 同一口径）。
    """
    hint = str((context or {}).get("point_hint", "")).strip()
    source_text = hint or raw_text
    tag = normalize_question(source_text)[:_TAG_MAX_LEN]
    if not tag:
        raise ValueError("知识点归一化结果为空（原文与 point_hint 均为空白）")
    return tag


def dedup_key(source: str, ref_type: str, ref_id: int, raw_text: str) -> str:
    """事件幂等键：sha256(source | ref_type | ref_id | 归一化原文)，16 位截断。"""
    payload = f"{source}|{ref_type}|{ref_id}|{normalize_question(raw_text)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- 异常（SR §5 错误码）


class LoopError(RuntimeError):
    """闭环业务异常基类：biz_code / numeric_code / http_status（API 层映射信封）。"""

    biz_code: str = "E_LOOP_ERROR"
    numeric_code: int = 40001
    http_status: int = 400


class GapConflictError(LoopError):
    """状态机冲突/乐观锁版本不符（40901/409，SR §5 错误码）。"""

    biz_code = "E_LOOP_CONFLICT"
    numeric_code = 40901
    http_status = 409


class MissingFieldError(LoopError):
    """审核入参缺失（驳回缺 reason / 批准缺条目内容，40902/409）。"""

    biz_code = "E_LOOP_MISSING_FIELD"
    numeric_code = 40902
    http_status = 409


class ReflowUnavailableError(LoopError):
    """kb-service 不可达，审核挂起可重试（50301/503）。"""

    biz_code = "E_LOOP_REFLOW_UNAVAILABLE"
    numeric_code = 50301
    http_status = 503


# --------------------------------------------------------------------------- 数据模型


@dataclass
class GapEvent:
    """缺口原始事件（append-only，gap_event 表内存形态；aggregated 为聚合水位标记）。"""

    event_id: str
    source: str
    ref_type: str
    ref_id: int
    raw_text: str
    trainee_id: int | None = None
    context: dict[str, Any] = field(default_factory=dict)
    dedup_key: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    aggregated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """序列化形态（上报出参 / 样本摘要）。"""
        return {
            "event_id": self.event_id,
            "source": self.source,
            "ref_type": self.ref_type,
            "ref_id": self.ref_id,
            "raw_text": self.raw_text,
            "trainee_id": self.trainee_id,
            "context": dict(self.context),
            "dedup_key": self.dedup_key,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }

    def brief(self) -> dict[str, Any]:
        """样本事件摘要（入缺口清单 sample_events，截断原文）。"""
        return {
            "event_id": self.event_id,
            "source": self.source,
            "ref_id": self.ref_id,
            "raw_text": self.raw_text[:40],
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


@dataclass
class KnowledgeGap:
    """缺口清单行（knowledge_gap 表内存形态：一知识点一行，频次/最近/样本聚合）。"""

    gap_id: str
    point_tag: str
    point_title: str
    kb_type: str
    status: str = GAP_STATUS_OPEN
    freq: int = 1
    sources: list[str] = field(default_factory=list)
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sample_events: list[dict[str, Any]] = field(default_factory=list)
    draft_item: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    reject_reason: str | None = None
    resolved_item_uid: str | None = None
    resolved_at: datetime | None = None
    hit_count: int = 0
    first_hit_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化形态（清单接口出参）。"""
        return {
            "gap_id": self.gap_id,
            "point_tag": self.point_tag,
            "point_title": self.point_title,
            "kb_type": self.kb_type,
            "status": self.status,
            "freq": self.freq,
            "sources": list(self.sources),
            "first_seen_at": self.first_seen_at.isoformat(timespec="seconds"),
            "last_seen_at": self.last_seen_at.isoformat(timespec="seconds"),
            "sample_events": [dict(s) for s in self.sample_events],
            "draft_item": dict(self.draft_item),
            "version": self.version,
            "reviewer": self.reviewer,
            "reject_reason": self.reject_reason,
            "resolved_item_uid": self.resolved_item_uid,
            "resolved_at": self.resolved_at.isoformat(timespec="seconds") if self.resolved_at else None,
            "hit_count": self.hit_count,
            "first_hit_at": self.first_hit_at.isoformat(timespec="seconds") if self.first_hit_at else None,
        }


@dataclass
class CandidateTalk:
    """候选话术（candidate_talk 表内存形态；入库前已过脱敏注入）。"""

    talk_id: str
    source: str
    ref_id: int
    talk_text: str
    scenario_key: str = ""
    trainee_id: int | None = None
    context: dict[str, Any] = field(default_factory=dict)
    status: str = TALK_STATUS_PENDING
    version: int = 1
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    reject_reason: str | None = None
    item_uid: str | None = None
    dedup_key: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """序列化形态（话术队列出参）。"""
        return {
            "talk_id": self.talk_id,
            "source": self.source,
            "ref_id": self.ref_id,
            "talk_text": self.talk_text,
            "scenario_key": self.scenario_key,
            "status": self.status,
            "reviewer": self.reviewer,
            "reject_reason": self.reject_reason,
            "item_uid": self.item_uid,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


# --------------------------------------------------------------------------- 回流钩子类型

#: 批准回流注入点：hook(point_tag, kb_type, item) -> 知识条目 uid
#: （生产复用 FR-KB-020 创建 + FR-KB-017 增量索引；demo 为确定性 uid 发号）
ReflowHook = Callable[[str, str, dict[str, Any]], str]

#: 脱敏注入：mask_fn(text) -> 脱敏后文本（生产复用 FR-KB-011 MaskingEngine）
MaskFn = Callable[[str], str]


# --------------------------------------------------------------------------- 存储


class InMemoryLoopStore:
    """内存闭环存储（demo/测试）：事件 append-only + 缺口/话术台账 + 审计，线程安全。

    生产替换为 PG 形态（gap_event UNIQUE(dedup_key) + knowledge_gap UPSERT +
    行锁审核，SR §4/§6），协议不变。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[GapEvent] = []
        self._events_by_key: dict[str, GapEvent] = {}
        self._gaps: dict[str, KnowledgeGap] = {}
        self._gaps_by_tag: dict[str, str] = {}
        self._talks: dict[str, CandidateTalk] = {}
        self._talks_by_key: dict[str, CandidateTalk] = {}
        self._audit: list[dict[str, Any]] = []
        self._gap_seq = 0
        self._talk_seq = 0
        self._uid_seq = 0

    # ── 事件 ────────────────────────────────────────────────────────────────
    def add_event(self, event: GapEvent) -> bool:
        """落事件（幂等）：dedup_key 已存在返回 False（不落新行）。"""
        with self._lock:
            if event.dedup_key in self._events_by_key:
                return False
            self._events.append(event)
            self._events_by_key[event.dedup_key] = event
            return True

    def find_event_by_key(self, key: str) -> GapEvent | None:
        with self._lock:
            return self._events_by_key.get(key)

    def list_events(self) -> list[GapEvent]:
        with self._lock:
            return list(self._events)

    # ── 缺口 ────────────────────────────────────────────────────────────────
    def next_gap_id(self) -> str:
        with self._lock:
            self._gap_seq += 1
            return f"GP-{self._gap_seq:04d}"

    def save_gap(self, gap: KnowledgeGap) -> None:
        with self._lock:
            self._gaps[gap.gap_id] = gap
            self._gaps_by_tag[gap.point_tag] = gap.gap_id

    def find_gap_by_tag(self, tag: str) -> KnowledgeGap | None:
        with self._lock:
            gap_id = self._gaps_by_tag.get(tag)
            return self._gaps.get(gap_id) if gap_id else None

    def find_gap_by_id(self, gap_id: str) -> KnowledgeGap | None:
        with self._lock:
            return self._gaps.get(gap_id)

    def list_gaps(self) -> list[KnowledgeGap]:
        with self._lock:
            return list(self._gaps.values())

    # ── 话术 ────────────────────────────────────────────────────────────────
    def next_talk_id(self) -> str:
        with self._lock:
            self._talk_seq += 1
            return f"CT-{self._talk_seq:04d}"

    def add_talk(self, talk: CandidateTalk) -> bool:
        """落话术（幂等）：dedup_key 已存在返回 False。"""
        with self._lock:
            if talk.dedup_key and talk.dedup_key in self._talks_by_key:
                return False
            self._talks[talk.talk_id] = talk
            if talk.dedup_key:
                self._talks_by_key[talk.dedup_key] = talk
            return True

    def find_talk_by_key(self, key: str) -> CandidateTalk | None:
        with self._lock:
            return self._talks_by_key.get(key)

    def find_talk(self, talk_id: str) -> CandidateTalk | None:
        with self._lock:
            return self._talks.get(talk_id)

    def list_talks(self) -> list[CandidateTalk]:
        with self._lock:
            return list(self._talks.values())

    # ── 审计与回流条目发号 ──────────────────────────────────────────────────
    def next_item_uid(self) -> str:
        """默认回流钩子的确定性条目 uid（ui-loop-0001 起）。"""
        with self._lock:
            self._uid_seq += 1
            return f"ui-loop-{self._uid_seq:04d}"

    def append_audit(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._audit.append(dict(entry))

    def audit_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._audit]


# ----------------------------------------------------------------=========== 组件


class GapCollector:
    """多源缺口事件采集（SR §6.1）：三源统一入口 + dedup_key 幂等（重复上报不落新行）。"""

    def __init__(self, store: InMemoryLoopStore, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._clock = clock

    def emit(
        self,
        source: str,
        ref_type: str,
        ref_id: int,
        raw_text: str,
        *,
        trainee_id: int | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> tuple[GapEvent, str]:
        """上报一条缺口事件：返回 (事件, "new"|"hit")；非法来源/空白原文拒绝。"""
        if source not in GAP_SOURCES:
            raise ValueError(f"未知缺口来源 {source!r}（合法值：{list(GAP_SOURCES)}）")
        if not (raw_text or "").strip():
            raise ValueError("缺口事件的原文不能为空")
        key = dedup_key(source, ref_type, ref_id, raw_text)
        existing = self._store.find_event_by_key(key)
        if existing is not None:
            return existing, "hit"
        event = GapEvent(
            event_id=f"GE-{key[:12]}", source=source, ref_type=ref_type, ref_id=ref_id,
            raw_text=raw_text.strip(), trainee_id=trainee_id, context=dict(context or {}),
            dedup_key=key, created_at=self._clock(),
        )
        self._store.add_event(event)
        return event, "new"


class GapAggregator:
    """缺口聚合器（SR §6.2）：新事件 → 知识点归一 → 清单 UPSERT（频次/最近/样本）。"""

    def __init__(
        self, store: InMemoryLoopStore, clock: Callable[[], datetime], *, sample_max: int = 5
    ) -> None:
        self._store = store
        self._clock = clock
        self._sample_max = sample_max

    def run(self) -> int:
        """聚合全部未处理事件：新缺口建行 / 开放缺口频次+1 / resolved 再现计 hit。

        生产以分布式锁 + 水位断点定时跑（SR §6.2）；demo 同步全量处理
        （事件带 aggregated 水位标记，重复 run 幂等返回 0）。
        """
        merged = 0
        now = self._clock()
        for event in self._store.list_events():
            if event.aggregated:
                continue
            tag = normalize_point(event.raw_text, event.context)
            gap = self._store.find_gap_by_tag(tag)
            if gap is None:
                gap = KnowledgeGap(
                    gap_id=self._store.next_gap_id(), point_tag=tag,
                    point_title=(event.context.get("point_hint") or event.raw_text)[:40],
                    kb_type=str(event.context.get("kb_hint")
                                or KB_HINT_BY_SOURCE.get(event.source, "product")),
                    sources=[event.source], first_seen_at=event.created_at,
                    last_seen_at=event.created_at, sample_events=[event.brief()],
                )
                self._store.save_gap(gap)
            elif gap.status == GAP_STATUS_RESOLVED:
                # 已入库知识点再现：计 hit（闭环第三段信号），频次不变（SR §9 test_aggregate_rehit）
                gap.hit_count += 1
                gap.first_hit_at = gap.first_hit_at or now
                self._store.save_gap(gap)
            else:
                gap.freq += 1
                gap.last_seen_at = max(gap.last_seen_at, event.created_at)
                if len(gap.sample_events) < self._sample_max:
                    gap.sample_events.append(event.brief())
                if event.source not in gap.sources:
                    gap.sources.append(event.source)
                self._store.save_gap(gap)
            event.aggregated = True
            merged += 1
        return merged


class ReviewWorkflow:
    """审核回流工作流（SR §6.4）：open → drafted → resolved/rejected；批准必经回流钩子。"""

    def __init__(
        self,
        store: InMemoryLoopStore,
        clock: Callable[[], datetime],
        reflow_hook: ReflowHook,
    ) -> None:
        self._store = store
        self._clock = clock
        self._hook = reflow_hook

    # ── 缺口审核 ────────────────────────────────────────────────────────────
    def submit_draft(self, gap_id: str, title: str, content: str) -> KnowledgeGap:
        """LLM/人工起草入库候选（仅存 draft_item，永不直接入库——决策 D3）。"""
        gap = self._require_gap(gap_id)
        if gap.status not in (GAP_STATUS_OPEN, GAP_STATUS_DRAFTED):
            raise GapConflictError(f"状态 {gap.status} 不可起草")
        gap.draft_item = {"title": title, "content": content}
        gap.status = GAP_STATUS_DRAFTED
        gap.version += 1
        self._store.save_gap(gap)
        self._audit("LOOP_GAP_DRAFT", target=f"gap:{gap_id}")
        return gap

    def review_gap(
        self,
        gap_id: str,
        *,
        approve: bool,
        reviewer: str,
        draft: Mapping[str, Any] | None = None,
        reason: str | None = None,
        version: int | None = None,
    ) -> KnowledgeGap:
        """缺口审核：批准 → reflow_hook 注入生成条目 uid（挂起可重试）；驳回 → reason 必填。"""
        gap = self._require_gap(gap_id)
        if version is not None and version != gap.version:
            raise GapConflictError(f"版本过期（乐观锁冲突）：期望 {version}，实际 {gap.version}")
        if gap.status not in GAP_REVIEWABLE:
            raise GapConflictError(f"状态 {gap.status} 不可审核")
        now = self._clock()
        if approve:
            item = dict(draft) if draft is not None else dict(gap.draft_item)
            if not item.get("content"):
                raise MissingFieldError("缺少条目内容，请先起草或手写（E_LOOP_MISSING_FIELD/40902）")
            uid = self._hook(gap.point_tag, gap.kb_type, {
                "title": str(item.get("title", gap.point_title)),
                "content": str(item["content"]),
                "gap_id": gap.gap_id,
                "sample_freq": gap.freq,
            })
            gap.status = GAP_STATUS_RESOLVED
            gap.resolved_item_uid = uid
            gap.resolved_at = now
            gap.reviewer, gap.reviewed_at = reviewer, now
        else:
            if not reason:
                raise MissingFieldError("驳回必须填写原因（E_LOOP_MISSING_FIELD/40902）")
            gap.status = GAP_STATUS_REJECTED
            gap.reject_reason = reason
            gap.reviewer, gap.reviewed_at = reviewer, now
        gap.version += 1
        self._store.save_gap(gap)
        self._audit("LOOP_GAP_REVIEW", target=f"gap:{gap_id}", approve=approve,
                    reviewer=reviewer, reason=reason)
        return gap

    # ── 话术审核（同一工作流，kb_type 固定 'skill'）──────────────────────────
    def review_talk(
        self, talk_id: str, *, approve: bool, reviewer: str, reason: str | None = None
    ) -> CandidateTalk:
        """话术审核：批准 → 回流话术库（kb_type='skill'）；驳回 → reason 必填留痕。"""
        talk = self._store.find_talk(talk_id)
        if talk is None:
            raise GapConflictError(f"候选话术不存在：{talk_id}")
        if talk.status != TALK_STATUS_PENDING:
            raise GapConflictError(f"话术状态 {talk.status} 不可审核")
        now = self._clock()
        if approve:
            talk.item_uid = self._hook(talk.scenario_key, "skill", {
                "title": f"话术沉淀：{talk.scenario_key or talk.talk_id}",
                "content": talk.talk_text,
                "talk_id": talk.talk_id,
            })
            talk.status = TALK_STATUS_APPROVED
            talk.reviewer, talk.reviewed_at = reviewer, now
        else:
            if not reason:
                raise MissingFieldError("驳回必须填写原因（E_LOOP_MISSING_FIELD/40902）")
            talk.status = TALK_STATUS_REJECTED
            talk.reject_reason = reason
            talk.reviewer, talk.reviewed_at = reviewer, now
        self._store.append_audit({
            "action": "LOOP_TALK_REVIEW", "target": f"talk:{talk_id}", "approve": approve,
            "reviewer": reviewer, "reason": reason,
            "at": now.isoformat(timespec="seconds"),
        })
        return talk

    def _require_gap(self, gap_id: str) -> KnowledgeGap:
        gap = self._store.find_gap_by_id(gap_id)
        if gap is None:
            raise GapConflictError(f"缺口不存在：{gap_id}")
        return gap

    def _audit(self, action: str, **detail: Any) -> None:
        self._store.append_audit({
            "action": action, **detail, "at": self._clock().isoformat(timespec="seconds")
        })


class TalkCollector:
    """话术沉淀采集（SR §6.3）：主管标注/复盘提取两条来源，入库前过脱敏注入。"""

    def __init__(
        self,
        store: InMemoryLoopStore,
        clock: Callable[[], datetime],
        mask_fn: MaskFn | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._mask_fn = mask_fn

    def collect(
        self,
        source: str,
        ref_id: int,
        talk_text: str,
        *,
        scenario_key: str = "",
        trainee_id: int | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> CandidateTalk:
        """沉淀一条候选话术（pending 待审核）；重复内容幂等返回既有行。"""
        if source not in TALK_SOURCES:
            raise ValueError(f"未知话术来源 {source!r}（合法值：{list(TALK_SOURCES)}）")
        if not (talk_text or "").strip():
            raise ValueError("话术原文不能为空")
        text = (talk_text or "").strip()
        if self._mask_fn is not None:
            text = self._mask_fn(text)  # 入库前脱敏（FR-KB-011 复用点）
        key = hashlib.sha256(f"{source}|{ref_id}|{text}".encode("utf-8")).hexdigest()[:16]
        existing = self._store.find_talk_by_key(key)
        if existing is not None:
            return existing
        talk = CandidateTalk(
            talk_id=self._store.next_talk_id(), source=source, ref_id=ref_id,
            talk_text=text, scenario_key=scenario_key, trainee_id=trainee_id,
            context=dict(context or {}), dedup_key=key, created_at=self._clock(),
        )
        self._store.add_talk(talk)
        return talk


class LoopMetrics:
    """缺口闭环率（SR §6.5）：识别 → 入库 → 再命中三段指标（工程过程指标）。"""

    def __init__(
        self, store: InMemoryLoopStore, clock: Callable[[], datetime], *, window_days: int = 30
    ) -> None:
        self._store = store
        self._clock = clock
        self._window_days = window_days

    def snapshot(self, window_days: int | None = None) -> dict[str, Any]:
        """闭环率快照：identified/resolved/rehit/closure_rate（空窗口 rate=None 不除零）。"""
        days = self._window_days if window_days is None else window_days
        since = self._clock() - timedelta(days=days)
        gaps = self._store.list_gaps()
        identified = sum(1 for g in gaps if g.first_seen_at >= since)
        resolved = sum(1 for g in gaps if g.resolved_at is not None and g.resolved_at >= since)
        rehit = sum(1 for g in gaps if g.resolved_at is not None and g.first_hit_at is not None)
        rate = round(rehit / identified, 4) if identified else None
        return {"identified": identified, "resolved": resolved, "rehit": rehit, "closure_rate": rate}


# --------------------------------------------------------------------------- 门面


class LoopService:
    """培训效果闭环门面：采集 / 聚合 / 审核 / 话术 / 指标 共用一个 store 与时钟。

    生产装配时注入真实回流钩子（kb-service FR-KB-020 适配器）与脱敏引擎
    （FR-KB-011 MaskingEngine）；demo 缺省为确定性 uid 发号 + 恒等脱敏。
    """

    def __init__(
        self,
        *,
        store: InMemoryLoopStore | None = None,
        clock: Callable[[], datetime] | None = None,
        reflow_hook: ReflowHook | None = None,
        mask_fn: MaskFn | None = None,
        sample_max: int = 5,
        rehit_window_days: int = 30,
    ) -> None:
        self._store = store if store is not None else InMemoryLoopStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        hook = reflow_hook if reflow_hook is not None else (lambda tag, kb, item: self._store.next_item_uid())
        self.collector = GapCollector(self._store, self._clock)
        self.aggregator = GapAggregator(self._store, self._clock, sample_max=sample_max)
        self.review = ReviewWorkflow(self._store, self._clock, hook)
        self.talks = TalkCollector(self._store, self._clock, mask_fn=mask_fn)
        self.metrics_engine = LoopMetrics(self._store, self._clock, window_days=rehit_window_days)

    @property
    def store(self) -> InMemoryLoopStore:
        """底层存储（清单/审计/测试断言面）。"""
        return self._store

    # ── 采集与聚合 ─────────────────────────────────────────────────────────
    def emit_gap(
        self,
        source: str,
        ref_type: str,
        ref_id: int,
        raw_text: str,
        *,
        trainee_id: int | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> tuple[str, str]:
        """三源缺口事件上报（透传采集器），返回 (event_id, dedup)。"""
        event, dedup = self.collector.emit(
            source, ref_type, ref_id, raw_text, trainee_id=trainee_id, context=context
        )
        return event.event_id, dedup

    def aggregate(self) -> int:
        """触发一轮聚合，返回本轮归并事件数。"""
        return self.aggregator.run()

    def list_gaps(self, status: str | None = None) -> list[KnowledgeGap]:
        """缺口清单：频次降序、并列按最近发生降序（FR-TR-008 热度口径）。"""
        gaps = [g for g in self._store.list_gaps() if status is None or g.status == status]
        return sorted(gaps, key=lambda g: (g.freq, g.last_seen_at), reverse=True)

    # ── 审核工作流 ─────────────────────────────────────────────────────────
    def submit_draft(self, gap_id: str, title: str, content: str) -> KnowledgeGap:
        """起草（LLM 起草 demo 形态：调用方直接给 title/content）。"""
        return self.review.submit_draft(gap_id, title, content)

    def review_gap(
        self,
        gap_id: str,
        *,
        approve: bool,
        reviewer: str,
        draft: Mapping[str, Any] | None = None,
        reason: str | None = None,
        version: int | None = None,
    ) -> KnowledgeGap:
        """缺口审核（批准回流 / 驳回留痕）。"""
        return self.review.review_gap(
            gap_id, approve=approve, reviewer=reviewer, draft=draft, reason=reason, version=version
        )

    def collect_talk(
        self,
        source: str,
        ref_id: int,
        talk_text: str,
        *,
        scenario_key: str = "",
        trainee_id: int | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> CandidateTalk:
        """话术沉淀（脱敏注入后入待审队列）。"""
        return self.talks.collect(
            source, ref_id, talk_text,
            scenario_key=scenario_key, trainee_id=trainee_id, context=context,
        )

    def review_talk(
        self, talk_id: str, *, approve: bool, reviewer: str, reason: str | None = None
    ) -> CandidateTalk:
        """话术审核（批准回流 kb_type='skill' / 驳回留痕）。"""
        return self.review.review_talk(talk_id, approve=approve, reviewer=reviewer, reason=reason)

    def metrics(self, window_days: int | None = None) -> dict[str, Any]:
        """闭环率快照（识别/入库/再命中/闭环率）。"""
        return self.metrics_engine.snapshot(window_days)

    def audit_log(self) -> list[dict[str, Any]]:
        """审计流水（采集/起草/审核全留痕，NFR-SEC-03）。"""
        return self._store.audit_log()


__all__ = [
    "GAP_SOURCE_MENTOR",
    "GAP_SOURCE_DRILL",
    "GAP_SOURCE_EXAM",
    "GAP_SOURCE_AGENT",
    "GAP_SOURCES",
    "GAP_STATUS_OPEN",
    "GAP_STATUS_DRAFTED",
    "GAP_STATUS_RESOLVED",
    "GAP_STATUS_REJECTED",
    "TALK_SOURCE_MARK",
    "TALK_SOURCE_REVIEW",
    "TALK_SOURCES",
    "TALK_STATUS_PENDING",
    "TALK_STATUS_APPROVED",
    "TALK_STATUS_REJECTED",
    "normalize_point",
    "dedup_key",
    "LoopError",
    "GapConflictError",
    "MissingFieldError",
    "ReflowUnavailableError",
    "GapEvent",
    "KnowledgeGap",
    "CandidateTalk",
    "ReflowHook",
    "MaskFn",
    "InMemoryLoopStore",
    "GapCollector",
    "GapAggregator",
    "ReviewWorkflow",
    "TalkCollector",
    "LoopMetrics",
    "LoopService",
]
