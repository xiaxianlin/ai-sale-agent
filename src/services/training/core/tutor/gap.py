# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""知识缺口登记与回流（FR-TR-001 §6.3 / FR-TR-008；SR-TR-001 §4 knowledge_gap 表）。

职责链（零编造闸 1 的数据出口）：
- 学员提问在知识库零命中（或生成引用校验失败）→ :meth:`KnowledgeGapRecorder.record`
  登记缺口：问题归一化 → 指纹哈希 → **同指纹开放态缺口 UPSERT 聚合**
  （命中则 hit_count+1、last_asked_at/最近提问刷新；未命中新开行）；
- 频次聚合即「热度」：FR-TR-008 按 hit_count 降序出清单 → 管理员审核 → 补录知识库；
- 补录入库后经 :meth:`KnowledgeGapRecorder.on_knowledge_ingested` 回填钩子将缺口置
  resolved（携带 resolved_item_uid），resolved 退出聚合（同问题再现时开新缺口行）。

存储协议 :class:`GapStore`（append/upsert/find_open/list_all）+ 内存实现
:class:`InMemoryGapStore`；生产替换为 PG 形态（t1 缺口表 + 部分唯一索引，
03-数据库设计 §3.3 增量，见 SR-TR-001 §4 DDL），协议不变。
"""
from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

# --------------------------------------------------------------------------- 常量

#: 缺口产生原因（SR-TR-001 §4 reason 枚举；low_score 归并到 zero_hit 判定口径）
REASON_ZERO_HIT: str = "zero_hit"
REASON_CITE_FAIL: str = "cite_fail"
GAP_REASONS: tuple[str, ...] = (REASON_ZERO_HIT, REASON_CITE_FAIL)

#: 缺口状态机：pending（开放，参与频次聚合）→ resolved（已回流闭环）
GAP_STATUS_PENDING: str = "pending"
GAP_STATUS_RESOLVED: str = "resolved"

#: 归一化时剔除的空白与标点（中英文常见符号；保留字母数字与 CJK 字符）
_STRIP_RE: re.Pattern[str] = re.compile(r"[\s\W_]+", re.UNICODE)

#: 问题归一化后的最大长度（防超长问题撑爆指纹聚合键）
_NORM_MAX_LEN: int = 200


def normalize_question(question: str) -> str:
    """问题归一化（频次聚合键的前置）：去空白/标点、小写、截断。

    同一问题的空白、大小写、标点差异应聚合到同一缺口（SR-TR-001 §6.3
    「去空白/繁简/大小写」口径的仓库内子集；繁简转换与同义改写随生产
    NLP 组件接入，本迭代保持确定性可测）。
    """
    lowered = (question or "").strip().lower()
    kept = _STRIP_RE.sub("", lowered)
    return kept[:_NORM_MAX_LEN]


def fingerprint_of(question: str) -> str:
    """归一化问题 → SHA-256 指纹（hex，16 位截断足够聚合且便于展示）。"""
    return hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()[:16]


def _utcnow() -> datetime:
    """UTC 当前时间（可注入假时钟便于测试断言时间推进）。"""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- 数据模型


@dataclass
class KnowledgeGap:
    """单条知识缺口（SR-TR-001 §4 knowledge_gap 表的进程内形态，字段同名同义）。"""

    fingerprint: str
    """归一化问题指纹（开放态聚合键）。"""

    question_norm: str
    """归一化问题文本。"""

    sample_question: str
    """最近一次原始提问（回流审核时的人读样本）。"""

    reason: str = REASON_ZERO_HIT
    """产生原因：zero_hit（零命中）/ cite_fail（引用校验失败）。"""

    source_session: str = ""
    """最近一次提问所在会话（观测留痕）。"""

    hit_count: int = 1
    """同指纹聚合提问次数（FR-TR-008 热度排序依据）。"""

    status: str = GAP_STATUS_PENDING
    """状态：pending（开放聚合）→ resolved（已回流闭环）。"""

    resolved_item_uid: str = ""
    """回流后生成的知识条目 uid（FR-TR-008 闭环凭证）。"""

    gap_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    first_asked_at: datetime = field(default_factory=_utcnow)
    last_asked_at: datetime = field(default_factory=_utcnow)

    @property
    def is_open(self) -> bool:
        """是否开放态（参与同指纹聚合）。"""
        return self.status == GAP_STATUS_PENDING

    def to_dict(self) -> dict[str, str | int]:
        """序列化形态（API 缺口清单 / 报告输出）。"""
        return {
            "gap_id": self.gap_id,
            "fingerprint": self.fingerprint,
            "question_norm": self.question_norm,
            "sample_question": self.sample_question,
            "reason": self.reason,
            "source_session": self.source_session,
            "hit_count": self.hit_count,
            "status": self.status,
            "resolved_item_uid": self.resolved_item_uid,
            "first_asked_at": self.first_asked_at.isoformat(timespec="seconds"),
            "last_asked_at": self.last_asked_at.isoformat(timespec="seconds"),
        }


# --------------------------------------------------------------------------- 存储协议


class GapStore(Protocol):
    """缺口存储协议（生产 PG：upsert + 部分唯一索引；内存实现见下）。"""

    def upsert(self, gap: KnowledgeGap) -> None:
        """按 gap_id 写入/覆盖一条缺口。"""
        ...

    def find_open_by_fingerprint(self, fingerprint: str) -> KnowledgeGap | None:
        """取同指纹的开放态（pending）缺口；无则 None（resolved 不参与聚合）。"""
        ...

    def find_by_id(self, gap_id: str) -> KnowledgeGap | None:
        """按主键取缺口（回流回填定位）。"""
        ...

    def list_all(self) -> Sequence[KnowledgeGap]:
        """全量缺口快照（清单接口按 hit_count 排序后输出）。"""
        ...


class InMemoryGapStore:
    """内存缺口存储（demo/测试）：线程安全，gap_id 主键 + 指纹开放态索引。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, KnowledgeGap] = {}

    def upsert(self, gap: KnowledgeGap) -> None:
        """按 gap_id 覆盖写入（同一对象引用更新后重新 upsert 亦幂等）。"""
        with self._lock:
            self._by_id[gap.gap_id] = gap

    def find_open_by_fingerprint(self, fingerprint: str) -> KnowledgeGap | None:
        """线性扫描取唯一的开放态同指纹缺口（开放态唯一由 Recorder 保证）。"""
        with self._lock:
            for gap in self._by_id.values():
                if gap.fingerprint == fingerprint and gap.is_open:
                    return gap
        return None

    def find_by_id(self, gap_id: str) -> KnowledgeGap | None:
        with self._lock:
            return self._by_id.get(gap_id)

    def list_all(self) -> list[KnowledgeGap]:
        """全量快照副本（排序由调用方决定）。"""
        with self._lock:
            return list(self._by_id.values())


# --------------------------------------------------------------------------- 登记器


class KnowledgeGapRecorder:
    """缺口登记器：登记（频次聚合）/ 回流回填（钩子）/ 热度清单。

    clock 可注入假时钟（测试断言 last_asked_at 推进）；store 可注入
    生产 PG 实现（协议不变）。
    """

    def __init__(
        self,
        *,
        store: GapStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store: GapStore = store if store is not None else InMemoryGapStore()
        self._clock = clock or _utcnow

    @property
    def store(self) -> GapStore:
        """底层存储（只读暴露供清单接口与测试断言）。"""
        return self._store

    def record(
        self,
        question: str,
        *,
        session: str = "",
        reason: str = REASON_ZERO_HIT,
    ) -> KnowledgeGap:
        """登记一次缺口提问：同指纹开放态聚合（hit_count+1），否则新开行。

        返回聚合/新建后的缺口对象（gap_id 供问答落库关联）。
        """
        if reason not in GAP_REASONS:
            raise ValueError(f"未知缺口原因 {reason!r}，合法值：{list(GAP_REASONS)}")
        question = (question or "").strip()
        if not question:
            raise ValueError("缺口登记的问题不能为空")
        fingerprint = fingerprint_of(question)
        now = self._clock()
        existing = self._store.find_open_by_fingerprint(fingerprint)
        if existing is not None:
            # 聚合：同指纹只累加频次与刷新最近发生，不新增行（频次即热度）
            existing.hit_count += 1
            existing.last_asked_at = now
            existing.sample_question = question
            existing.source_session = session or existing.source_session
            self._store.upsert(existing)
            return existing
        gap = KnowledgeGap(
            fingerprint=fingerprint,
            question_norm=normalize_question(question),
            sample_question=question,
            reason=reason,
            source_session=session,
            hit_count=1,
            status=GAP_STATUS_PENDING,
            first_asked_at=now,
            last_asked_at=now,
        )
        self._store.upsert(gap)
        return gap

    def resolve(self, gap: KnowledgeGap, *, resolved_item_uid: str) -> KnowledgeGap:
        """将指定缺口置 resolved（携带回流知识条目 uid），退出后续聚合。"""
        if not resolved_item_uid:
            raise ValueError("resolved_item_uid 不能为空（回流闭环必须挂接知识条目）")
        gap.status = GAP_STATUS_RESOLVED
        gap.resolved_item_uid = resolved_item_uid
        self._store.upsert(gap)
        return gap

    def on_knowledge_ingested(self, question: str, *, item_uid: str) -> KnowledgeGap | None:
        """知识入库回填钩子（FR-TR-008 闭环）：补录内容命中开放缺口即置 resolved。

        question 为本次补录覆盖的问题（与缺口指纹同归一化口径）；
        无匹配开放缺口返回 None（补录了新知识但无历史缺口，属正常）。
        """
        open_gap = self._store.find_open_by_fingerprint(fingerprint_of(question))
        if open_gap is None:
            return None
        return self.resolve(open_gap, resolved_item_uid=item_uid)

    def list_gaps(self, *, status: str | None = None) -> list[KnowledgeGap]:
        """缺口清单：按 hit_count 降序、并列按最近发生降序（FR-TR-008 热度口径）。"""
        gaps = [g for g in self._store.list_all() if status is None or g.status == status]
        return sorted(gaps, key=lambda g: (g.hit_count, g.last_asked_at), reverse=True)


__all__ = [
    "REASON_ZERO_HIT",
    "REASON_CITE_FAIL",
    "GAP_REASONS",
    "GAP_STATUS_PENDING",
    "GAP_STATUS_RESOLVED",
    "normalize_question",
    "fingerprint_of",
    "KnowledgeGap",
    "GapStore",
    "InMemoryGapStore",
    "KnowledgeGapRecorder",
]
