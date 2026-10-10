# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""AI 导师问答编排（FR-TR-001，SR-TR-001 §6.1~§6.3 的工程落地）。

链路（零编造三道闸贯穿，事件流形态供 SSE 直出）：

    ask(question, session)
      ① 检索前置闸：search_fn（FR-KB-030 契约注入）→ Top-K 知识块
      ② 零命中判定（闸 1）：结果为空 → **不调 LLM**，固定话术 + 缺口登记
      ③ 教学化生成：System Prompt（强制引用格式 + 禁编造）+ 多轮历史
         （窗口 N 轮可配，超出压缩为摘要行）+ 编号知识块
      ④ 生成引用闸（闸 2）：聚合回答中的 [来源N] 必须对应实际检索块，
         缺失/越界 → 判生成失败 → 降级为「仅来源列表」回答
      ⑤ 落库审计闸（闸 3）：QaRecord 落库（question/answer/knowledge_refs/
         session/created_at），knowledge_refs 即 100% 可溯源的抽检凭证

检索依赖契约（与 src/services/kb/core/evaluation/runner.py 的 search_fn
鸭子契约同源）：``search_fn(query=…, kb_types=[…], filters=…, top_k=…) →
{"results": [{"chunk_id": …, "score": …}, …]}``；结果条目可增带 title/
kb_type/text（供引用展示与 Fake 桩接地），缺失时以空串兜底。

与 SR-TR-001 §6.1 的偏差标注：引用校验失败后本迭代**直接降级**为来源列表
（不重试重生成）——流式场景下失败 token 已外发，重试会造成内容重叠；
重试开关随联调迭代接入，降级话术自身携带合法 [来源N]，可溯源性不受影响。
"""
from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from services.kb.core.vectorstore.base import KB_TYPES

from .gap import (
    REASON_CITE_FAIL,
    REASON_ZERO_HIT,
    KnowledgeGap,
    KnowledgeGapRecorder,
)
from .llm import LlmClient, LlmDownstreamError

# --------------------------------------------------------------------------- 常量与话术

#: 检索默认返回块数（SR-TR-001 §7 strategy/tutor.retrieval_top_k，Top5 口径）
DEFAULT_TOP_K: int = 5
#: 多轮历史原文窗口（轮 = 一问一答；SR-TR-001 §7 max_turns 仓库内取 6）
DEFAULT_HISTORY_WINDOW: int = 6
#: 单条问题长度上限（04-接口设计 §1.3 40001 口径：空问题/超长）
MAX_QUESTION_LEN: int = 500
#: 历史答案截断长度（控制上下文 token 成本，SR-TR-001 §6.3）
ANSWER_TRUNCATE: int = 400

#: 零命中固定话术（05-核心模块详设 §7.1 原文「知识库暂无该内容，已记录」置首，
#: 其后为教学化扩展；语义一致、学员可读性优先——SR-TR-001 §2 话术细化口径）
NO_ANSWER_TEXT: str = (
    "知识库暂无该内容，已记录。这个问题我暂时没有在知识库中找到可靠依据，"
    "不能编造答案误导你；已登记为知识缺口，知识库补充后会给出准确解答。"
    "你可以先换个问法，或先看看相关的产品参数与流程知识。"
)

#: 引用校验失败（生成闸）降级话术模板：仅来源列表，条目占位 {sources}
NO_EVIDENCE_TEXT: str = "生成的回答未通过引用校验，为避免误导已弃用。以下是与问题最相关的知识来源，请直接查阅："

#: 教学化 System Prompt（SR-TR-001 §6.2；引用格式统一为 [来源N]）
SYSTEM_PROMPT: str = """你是企业的外贸销售 AI 导师，职责是引导新人深入学习。
规则（违反即回答无效）：
1. 你的全部事实性内容只能来自【知识资料】区的编号条目，每个事实句后必须标注 [来源N]；
2. 【历史摘要】仅用于理解对话上下文，禁止作为回答依据，禁止引用；
3. 教学法（苏格拉底式）：先点拨思考方向，再基于知识资料讲解，结尾以一个引导性追问收束，并给出深入学习建议（指向带教素材库相关主题）；
4. 知识资料未覆盖的内容，直接说明「知识库暂无该内容」，不得推测、不得补全；
5. 不输出知识资料区之外的任何来源、链接或数据。"""

#: 引用标记词法：[来源N]（N 为 1 起始的检索块编号）
CITATION_RE: re.Pattern[str] = re.compile(r"\[来源(\d+)\]")


# --------------------------------------------------------------------------- 异常体系


class TutorError(RuntimeError):
    """导师链路业务异常基类。"""


class TutorBadRequest(TutorError):
    """入参非法（空问题/超长）→ API 层映射 40001（HTTP 400）。"""


class TutorDownstreamError(TutorError):
    """下游依赖故障（检索/LLM 不可用）→ API 层映射 50002。

    系统故障不等于知识缺口：本异常路径**不登记缺口**（SR-TR-001 §6.1）。
    """


# --------------------------------------------------------------------------- 引用校验（闸 2）


def validate_citations(answer: str, n_refs: int) -> tuple[bool, set[int]]:
    """校验回答引用：[来源N] 至少出现 1 个，且全部落在 1..n_refs 检索块编号内。

    返回 (是否通过, 引用的编号集合)；缺失引用或越界引用均判失败
    （越界即「编造了未下发的来源」，零容忍）。
    """
    cited = {int(n) for n in CITATION_RE.findall(answer)}
    return (bool(cited) and cited <= set(range(1, n_refs + 1))), cited


# --------------------------------------------------------------------------- 问答记录（闸 3 落库）


@dataclass
class QaRecord:
    """单轮问答落库记录（SR-TR-001 §4 tutor_qa 表的进程内形态）。

    knowledge_refs 为本轮实际引用的知识块清单（含 no/chunk_id/kb_type/
    title/score），是「回答 100% 可溯源」验收的落库凭证。
    """

    question: str
    answer: str
    session: str
    knowledge_refs: list[dict[str, Any]] = field(default_factory=list)
    is_gap: bool = False
    """本轮是否走了零编造兜底（零命中或引用降级）。"""

    gap_fingerprint: str = ""
    """关联缺口指纹（is_gap=true 时非空）。"""

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    idempotency_key: str = ""
    """前端幂等键（同 session 内重复请求不重复落库）。"""

    record_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """序列化形态（历史接口 / 抽检报告）。"""
        return {
            "record_id": self.record_id,
            "session": self.session,
            "question": self.question,
            "answer": self.answer,
            "knowledge_refs": [dict(ref) for ref in self.knowledge_refs],
            "is_gap": self.is_gap,
            "gap_fingerprint": self.gap_fingerprint,
            "trace_id": self.trace_id,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at.isoformat(timespec="seconds"),
        }


class DuplicateQaRecordError(TutorError):
    """幂等键冲突（并发竞态兜底；正常路径由 find_by_idempotency 前置拦截）。"""


class QaStore(Protocol):
    """问答落库存储协议（生产 PG：tutor_qa + (session, client_msg_id) 唯一约束）。"""

    def append(self, record: QaRecord) -> None:
        """追加一条问答记录（幂等键冲突抛 DuplicateQaRecordError）。"""
        ...

    def list_by_session(self, session: str) -> list[QaRecord]:
        """取会话全部记录，按创建时间升序（多轮历史构建输入）。"""
        ...

    def find_by_idempotency(self, session: str, idempotency_key: str) -> QaRecord | None:
        """按幂等键取首次落库记录（重复请求重放 done 事件）。"""
        ...


class InMemoryQaStore:
    """内存问答存储（demo/测试）：线程安全，(session, idempotency_key) 唯一。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[QaRecord] = []
        self._idem_index: dict[tuple[str, str], QaRecord] = {}

    def append(self, record: QaRecord) -> None:
        """追加记录；非空幂等键重复时拒绝（对齐 PG 唯一约束语义）。"""
        with self._lock:
            if record.idempotency_key:
                key = (record.session, record.idempotency_key)
                if key in self._idem_index:
                    raise DuplicateQaRecordError(
                        f"幂等键重复：session={record.session} key={record.idempotency_key}"
                    )
                self._idem_index[key] = record
            self._records.append(record)

    def list_by_session(self, session: str) -> list[QaRecord]:
        """按会话过滤、创建时间（再按 record_id）稳定升序。"""
        with self._lock:
            rows = [r for r in self._records if r.session == session]
        return sorted(rows, key=lambda r: (r.created_at, r.record_id))

    def find_by_idempotency(self, session: str, idempotency_key: str) -> QaRecord | None:
        with self._lock:
            return self._idem_index.get((session, idempotency_key))

    def count(self, session: str | None = None) -> int:
        """记录总数（可按会话过滤；测试断言幂等不重复落库用）。"""
        with self._lock:
            if session is None:
                return len(self._records)
            return sum(1 for r in self._records if r.session == session)


# --------------------------------------------------------------------------- 事件流


@dataclass(frozen=True)
class TutorEvent:
    """问答链路事件（SSE 直出单元）：token（增量文本）/ gap（兜底话术）/ done（终态）。"""

    type: str
    """事件类型：token | gap | done。"""

    data: dict[str, Any]
    """事件载荷（done 携带 knowledge_refs，即本轮引用凭证）。"""


# --------------------------------------------------------------------------- 检索结果规整


def parse_search_payload(payload: Any) -> list[dict[str, Any]]:
    """search_fn 返回值 → 规整结果块列表（兼容信封/裸列表、Mapping/对象条目）。

    契约（evaluation/runner.py 同源）：``{"results": [{chunk_id, score, …}]}``；
    条目额外字段（title/kb_type/text）原样透传，缺失以空串兜底——引用编号
    以下标+1 为准（与【知识资料】下发顺序一致），不依赖额外字段。
    """
    if isinstance(payload, Mapping):
        raw: Any = payload.get("results") or []
    else:
        raw = payload or []
    items: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            items.append(dict(item))
        else:  # 对象形态（SearchHit 等）：按属性规整
            items.append(
                {
                    "chunk_id": getattr(item, "chunk_id", None),
                    "score": getattr(item, "score", 0.0),
                    "title": str(getattr(item, "title", "") or ""),
                    "kb_type": str(getattr(item, "kb_type", "") or ""),
                    "text": str(getattr(item, "text", "") or ""),
                }
            )
    return items


def _refs_of(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """结果块 → knowledge_refs 落库清单（编号 = 下标+1，score 规整两位小数）。"""
    refs: list[dict[str, Any]] = []
    for index, item in enumerate(results, start=1):
        try:
            score = round(float(item.get("score") or 0.0), 4)
        except (TypeError, ValueError):
            score = 0.0
        refs.append(
            {
                "no": index,
                "chunk_id": item.get("chunk_id"),
                "kb_type": str(item.get("kb_type") or ""),
                "title": str(item.get("title") or ""),
                "score": score,
            }
        )
    return refs


# --------------------------------------------------------------------------- 编排器


class TutorPipeline:
    """AI 导师问答编排器：零编造三道闸的唯一入口（组件全部鸭子类型注入）。"""

    def __init__(
        self,
        *,
        search_fn: Callable[..., Any],
        llm: LlmClient,
        qa_store: QaStore | None = None,
        gap_recorder: KnowledgeGapRecorder | None = None,
        kb_types: Sequence[str] | None = None,
        top_k: int = DEFAULT_TOP_K,
        history_window: int = DEFAULT_HISTORY_WINDOW,
        no_answer_text: str = NO_ANSWER_TEXT,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        if top_k < 1:
            raise ValueError(f"top_k 必须 >= 1，收到 {top_k}")
        if history_window < 1:
            raise ValueError(f"history_window 必须 >= 1，收到 {history_window}")
        self.search_fn = search_fn
        self.llm = llm
        self.qa_store: QaStore = qa_store if qa_store is not None else InMemoryQaStore()
        self.gap_recorder = gap_recorder if gap_recorder is not None else KnowledgeGapRecorder()
        # kb_types 可配（FR-KB-030 契约：六大库子集）；缺省查全部六大库
        self.kb_types: tuple[str, ...] = tuple(kb_types) if kb_types is not None else KB_TYPES
        self.top_k = top_k
        self.history_window = history_window
        self.no_answer_text = no_answer_text
        self.system_prompt = system_prompt

    # --------------------------------------------------------------- 问答主链路

    def ask(
        self,
        question: str,
        session: str = "default",
        *,
        idempotency_key: str = "",
    ) -> Iterator[TutorEvent]:
        """执行一轮问答，产出事件流（token* → done；零命中路径 gap → done）。

        - 入参校验失败抛 :class:`TutorBadRequest`（不进检索、不落库）；
        - 幂等重放：同 (session, idempotency_key) 已落库 → 直接重放首次 done
          事件（不再检索、不再调 LLM、不重复落库）；
        - 下游故障抛 :class:`TutorDownstreamError`（不登记缺口）。
        """
        question = (question or "").strip()
        if not question:
            raise TutorBadRequest("问题不能为空")
        if len(question) > MAX_QUESTION_LEN:
            raise TutorBadRequest(f"问题长度超过上限 {MAX_QUESTION_LEN} 字符")

        # ── 幂等重放（SR-TR-001 §6.1 ask 幂等：重放首次 done 事件）──
        if idempotency_key:
            existed = self.qa_store.find_by_idempotency(session, idempotency_key)
            if existed is not None:
                yield self._done_event(existed, cited=sorted(self._cited_nos(existed)), replay=True)
                return

        # ── 闸 1：检索前置（FR-KB-030 唯一知识来源；系统故障≠知识缺口）──
        results = self._search(question)

        # ── 闸 1：零命中判定 → 不调 LLM，固定话术 + 缺口登记 ──
        if not results:
            gap = self.gap_recorder.record(question, session=session, reason=REASON_ZERO_HIT)
            record = QaRecord(
                question=question,
                answer=self.no_answer_text,
                session=session,
                knowledge_refs=[],
                is_gap=True,
                gap_fingerprint=gap.fingerprint,
                idempotency_key=idempotency_key,
            )
            self.qa_store.append(record)
            yield TutorEvent(
                "gap", {"message": self.no_answer_text, "gap_id": gap.gap_id, "reason": REASON_ZERO_HIT}
            )
            yield self._done_event(record, cited=[], replay=False)
            return

        # ── ③ 教学化生成：历史压缩 + 编号知识块 + System Prompt ──
        history = self.qa_store.list_by_session(session)
        messages = self.build_messages(question, results, history)

        # ── ④ 流式生成（token 直出）→ 聚合后引用校验 ──
        buffer: list[str] = []
        try:
            for token in self.llm.generate_stream(messages, system=self.system_prompt):
                buffer.append(token)
                yield TutorEvent("token", {"text": token})
        except LlmDownstreamError as exc:
            raise TutorDownstreamError(f"模型服务不可用：{exc}") from exc
        answer = "".join(buffer)

        is_gap = False
        gap: KnowledgeGap | None = None
        cited: set[int] = set()
        ok, cited = validate_citations(answer, len(results))
        if not ok:
            # ── 闸 2：引用缺失/越界 → 弃用生成内容，降级为仅来源列表 ──
            gap = self.gap_recorder.record(question, session=session, reason=REASON_CITE_FAIL)
            answer = self._sources_only_answer(results)
            is_gap = True
            # 降级后以最终展示回答重算引用编号（来源列表自带合法 [来源N]）
            cited = {int(n) for n in CITATION_RE.findall(answer)}
            yield TutorEvent(
                "gap",
                {
                    "message": "生成的回答未通过引用校验，已降级为知识来源列表",
                    "gap_id": gap.gap_id,
                    "reason": REASON_CITE_FAIL,
                },
            )

        # ── 闸 3：落库（knowledge_refs = 抽检可核对的溯源凭证）──
        record = QaRecord(
            question=question,
            answer=answer,
            session=session,
            knowledge_refs=_refs_of(results),
            is_gap=is_gap,
            gap_fingerprint=gap.fingerprint if gap is not None else "",
            idempotency_key=idempotency_key,
        )
        self.qa_store.append(record)
        yield self._done_event(record, cited=sorted(cited), replay=False)

    # --------------------------------------------------------------- 消息组装（§6.3）

    def build_messages(
        self,
        question: str,
        results: Sequence[Mapping[str, Any]],
        history: Sequence[QaRecord],
    ) -> list[dict[str, str]]:
        """构造多轮消息序列（system 由 LlmClient.generate_stream 的 system 参数承载）。

        组成：[历史摘要行(可选)] + 【知识资料】编号块 + 最近 N 轮原文（答案截断）
        + 本次问题。窗口外的早期轮次压缩为一行确定性摘要（不调 LLM，摘要行
        明令禁止作为回答依据——System Prompt 规则 2）。
        """
        messages: list[dict[str, str]] = []
        summary = self._compact_summary(history)
        if summary:
            messages.append({"role": "system", "content": summary})
        messages.append({"role": "system", "content": self._knowledge_block(results)})
        for record in list(history)[-self.history_window :]:
            messages.append({"role": "user", "content": record.question})
            messages.append({"role": "assistant", "content": record.answer[:ANSWER_TRUNCATE]})
        messages.append({"role": "user", "content": question})
        return messages

    def _compact_summary(self, history: Sequence[QaRecord]) -> str:
        """窗口外早期轮次 → 单行摘要（确定性，零 LLM 成本）。

        摘要仅服务对话连贯，不进入回答依据（见 System Prompt 规则 2）。
        """
        overflow = list(history)[: max(0, len(history) - self.history_window)]
        if not overflow:
            return ""
        topics = "；".join(r.question[:24] for r in overflow[:5])
        more = "" if len(overflow) <= 5 else f" 等 {len(overflow)} 轮"
        return (
            f"【历史摘要】此前共 {len(overflow)} 轮问答（超出窗口已压缩，"
            f"仅作对话连贯参考，禁止作为回答依据），涉及问题：{topics}{more}"
        )

    @staticmethod
    def _knowledge_block(results: Sequence[Mapping[str, Any]]) -> str:
        """结果块 → 【知识资料】编号条目（[来源N] 标记与引用校验同源）。"""
        lines: list[str] = [
            "【知识资料】以下为本次检索命中的知识块，回答的事实性内容必须且只能出自以下条目："
        ]
        for index, item in enumerate(results, start=1):
            try:
                score = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            title = str(item.get("title") or f"知识块 {item.get('chunk_id', index)}")
            kb_type = str(item.get("kb_type") or "")
            text = str(item.get("text") or "")
            lines.append(f"[来源{index}] {title}（知识库：{kb_type or '未知'}，相关度：{score:.2f}）")
            if text:
                lines.append(text)
        return "\n".join(lines)

    # --------------------------------------------------------------- 私有辅助

    def _search(self, question: str) -> list[dict[str, Any]]:
        """调用注入的 search_fn 并规整结果；任何异常统一转下游故障（50002）。"""
        try:
            payload = self.search_fn(
                query=question,
                kb_types=list(self.kb_types),
                filters=None,
                top_k=self.top_k,
            )
        except TutorError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 检索侧异常统一降级口径（不记缺口）
            raise TutorDownstreamError(f"知识服务暂不可用，请稍后重试（{type(exc).__name__}）") from exc
        return parse_search_payload(payload)

    def _sources_only_answer(self, results: Sequence[Mapping[str, Any]]) -> str:
        """引用降级话术：仅来源列表（条目带合法 [来源N]，仍可溯源）。"""
        lines = [NO_EVIDENCE_TEXT]
        for index, item in enumerate(results, start=1):
            title = str(item.get("title") or f"知识块 {item.get('chunk_id', index)}")
            lines.append(f"[来源{index}] {title}")
        return "\n".join(lines)

    @staticmethod
    def _cited_nos(record: QaRecord) -> set[int]:
        """从落库回答中提取引用编号（重放 done 事件时复算 cited）。"""
        return {int(n) for n in CITATION_RE.findall(record.answer)}

    def _done_event(self, record: QaRecord, *, cited: list[int], replay: bool) -> TutorEvent:
        """构造终态 done 事件（knowledge_refs 为可溯源凭证）。"""
        return TutorEvent(
            "done",
            {
                "record_id": record.record_id,
                "session": record.session,
                "question": record.question,
                "answer": record.answer,
                "knowledge_refs": [dict(ref) for ref in record.knowledge_refs],
                "is_gap": record.is_gap,
                "cited": cited,
                "trace_id": record.trace_id,
                "idempotent_replay": replay,
            },
        )


__all__ = [
    "DEFAULT_TOP_K",
    "DEFAULT_HISTORY_WINDOW",
    "MAX_QUESTION_LEN",
    "NO_ANSWER_TEXT",
    "NO_EVIDENCE_TEXT",
    "SYSTEM_PROMPT",
    "TutorError",
    "TutorBadRequest",
    "TutorDownstreamError",
    "validate_citations",
    "QaRecord",
    "DuplicateQaRecordError",
    "QaStore",
    "InMemoryQaStore",
    "TutorEvent",
    "parse_search_payload",
    "TutorPipeline",
]
