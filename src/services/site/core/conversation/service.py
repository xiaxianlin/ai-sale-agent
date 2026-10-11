# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""独立站对话引擎（FR-SITE-003，SR-SITE-003 §6.1~§6.3）。

三个引擎，依赖全部协议注入（按 agent/core/brain 的协议形态复用，不直接 import 其具体类）：

- :class:`SiteChatService` —— 对话主链路：画像注入（FR-SITE-002 hint_fn）→ 检索
  （kb_types=product,skill + scene_tag site_* 过滤，SR-SITE-003 §2.4）→ LLM 生成 →
  幻觉防线（R-03：占位符结构化填充/价格残留硬拦截/引用合法性/零命中不生成）→ 兜底。
  独立站话术差异（§2.2）：主动销售导向、短句轻快、每轮带行动召唤；无平台敏感拦截
  （法律威胁外），但价格/交期仍仅来自结构化产品卡。
- :class:`InviteEngine` —— 主动邀聊：停留/重复访问/加购三触发源 + 每访客每日 ≤N 次
  频率上限 + 冷却窗 + consent 门控（FR-SITE-007）+ 模板化开场（零 LLM 时延）。
- :class:`LeadCapture` —— 留资：email/WhatsApp 结构化校验（E.164）→ CustomerWriter 协议
  写入 Customer（渠道 site，FR-SITE-005 合并入口）→ lead 事件 + 漏斗回填。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Protocol
from dataclasses import dataclass
from typing import Any

from services.site.core.errors import SiteServiceError
from services.site.core.session.service import SiteSessionRecord
from services.site.core.tracking.service import TrackedEvent

# --------------------------------------------------------------------------- 常量

#: site 检索范围（对齐 SR-KB-030 注册默认：kb_types=product,skill）
DEFAULT_KB_TYPES_SITE: tuple[str, ...] = ("product", "skill")
#: site 场景话术标签（skill 库 metadata.scene_tag 扩展，不新增 kb_type）
DEFAULT_SCENE_TAGS: tuple[str, ...] = ("site_chat", "site_invite", "site_lead")
#: agent-core 调用超时（SITE_REPLY_TIMEOUT_S）
REPLY_TIMEOUT_S: float = 20.0
#: WhatsApp E.164（+ 国家码 + 8~15 位）
WHATSAPP_RE = re.compile(r"^\+\d{8,15}$")
#: email 词面校验（demo 口径：本地@域名.顶级域）
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.\-]+$")

#: 兜底原因码（SR-SITE-003 §6.1 _resolve）
REASON_ZERO_HIT = "kb_no_hit"
REASON_PRICE_LEAK = "price_leak"
REASON_BAD_REFS = "invalid_refs"
REASON_KEY_MISSING = "key_missing"
REASON_DOWNSTREAM = "downstream"
REASON_LLM_OUTPUT = "llm_output_error"

#: 多语言兜底话术（转人工 + 留邮箱引导；site 侧 need_human 处置，§2.2）
FALLBACK_TEMPLATES: dict[str, str] = {
    "en": (
        "Thanks for your message! I want to make sure you get an accurate answer, so I'll have "
        "a specialist follow up shortly. Meanwhile, leave your email and we'll send you the "
        "full spec sheet: support@demo-trade.com"
    ),
    "zh": (
        "感谢您的留言！为保障答复准确，我已将您的问题转给专员稍后跟进；方便的话请留下邮箱，"
        "我们会第一时间发送完整资料：support@demo-trade.com"
    ),
}


class ConversationError(SiteServiceError):
    """对话域业务错误。"""


class LlmDownstreamError(ConversationError):
    """下游（agent-core / LLM）故障：50002，触发固定话术降级。"""

    def __init__(self, message: str = "下游生成服务暂不可用") -> None:
        super().__init__(50002, message)


# --------------------------------------------------------------------------- 依赖协议（注入形态同 agent brain）


class ChatLlm(Protocol):
    """LLM 客户端协议（形态同 brain BrainLlmClient：结构化 JSON 输出）。"""

    def complete(
        self, prompt: str, *, timeout_s: float, max_tokens: int, temperature: float
    ) -> str: ...


class SearchFn(Protocol):
    """检索协议（FR-KB-030 契约信封）。"""

    def __call__(
        self,
        *,
        query: str,
        kb_types: list[str] | None = None,
        filters: Any = None,
        top_k: int = 5,
    ) -> dict[str, Any]: ...


class ProductCardFn(Protocol):
    """产品结构化取数协议（SR-KB-001 get_product_card：sku → 卡或 None）。"""

    def __call__(self, sku: str) -> Mapping[str, Any] | None: ...


class HintFn(Protocol):
    """画像注入协议（FR-SITE-002 ContextInjector.prompt_hint）。"""

    def __call__(self, visitor_uid: str, round_no: int) -> str | None: ...


@dataclass(frozen=True)
class RetrievedHit:
    """检索命中块（R1..Rn 由组装期按位次指派）。"""

    chunk_id: int
    score: float
    title: str
    text: str


def parse_hits(payload: Mapping[str, Any] | None) -> list[RetrievedHit]:
    """解析检索信封 → 命中块列表（容忍缺字段，缺省记 0/空）。"""
    results = (payload or {}).get("results") or []
    return [
        RetrievedHit(
            chunk_id=int(row.get("chunk_id", 0)),
            score=float(row.get("score", 0.0)),
            title=str(row.get("title", "")),
            text=str(row.get("text", "")),
        )
        for row in results
    ]


# --------------------------------------------------------------------------- System Prompt（site 风格画像，§2.2）

SITE_SYSTEM_PROMPT = """你是独立站在线销售助理，代表公司用{language}回复访客。
【话术风格】短句轻快、口语化（在线聊天体感）；每轮尽量带行动召唤（留邮箱/WhatsApp、限时优惠、库存提示）。
【访客画像】{visitor_hint}
【知识片段】以下为唯一事实来源，块前 [R#] 为引用编号：
{chunk_block}
【引用格式】每个事实性陈述句末标注来源编号（如 [R1]）；只允许引用上述编号。
【禁止编造】一、价格/MOQ/交期一律写占位符 {{{{PRICE:SKU:QTY}}}}、{{{{MOQ:SKU}}}}、{{{{LEAD_TIME:SKU}}}}，严禁自己填写任何数字；
二、知识片段没有的内容回复 "I'll confirm with our team and revert shortly"，不得猜测。
【会话历史】{history_block}
【访客消息】{text}
仅输出 JSON：{{"reply_draft":"…","knowledge_refs":["R1"],"missing_info":[],"intent":"…","confidence":0.0}}"""


def _history_block(messages: list[dict[str, Any]], *, max_messages: int = 6) -> str:
    """近端历史块（原文逐字，条数封顶）。"""
    recent = messages[-max_messages:]
    return "\n".join(f"[{m.get('role', 'buyer')}] {m.get('content', '')}" for m in recent) or "（新会话）"


# --------------------------------------------------------------------------- 幻觉防线（R-03，复用 brain 断言思路）


class ReplyGuards:
    """关键字段保护：占位符结构化填充 + 价格残留扫描（与 brain.PriceGuard 同思路）。"""

    #: 占位符形态：{{PRICE:SKU:QTY}} / {{MOQ:SKU}} / {{LEAD_TIME:SKU}}
    PLACEHOLDER_RE = re.compile(r"\{\{(PRICE|MOQ|LEAD_TIME):([A-Za-z0-9_-]+)(?::(\d+))?\}\}")
    #: 货币/关键数字残留模式
    CURRENCY_RE = re.compile(
        r"[$€£]\s*\d[\d,]*(?:\.\d+)?"
        r"|(?:USD|EUR|GBP|美元|欧元|英镑)\s*\d[\d,]*(?:\.\d+)?"
        r"|\d[\d,]*(?:\.\d+)?\s*(?:USD|EUR|GBP|美元|欧元|英镑)",
        re.IGNORECASE,
    )

    @staticmethod
    def fill_placeholders(
        draft: str, card_fn: ProductCardFn | None
    ) -> tuple[str, list[str], list[str]]:
        """占位符填充：卡缺失/无匹配档 → unresolved（绝不猜数字）；合法填充值回传供泄漏扫描剥除。"""
        unresolved: list[str] = []
        filled_values: list[str] = []

        def _replace(match: re.Match[str]) -> str:
            kind, sku, qty_text = match.group(1), match.group(2), match.group(3)
            card = card_fn(sku) if card_fn is not None else None
            value: str | None = None
            if card is not None:
                if kind == "PRICE" and qty_text is not None:
                    price = ReplyGuards._ladder_price(card, int(qty_text))
                    if price is not None:
                        value = f"USD {price}/pc"
                elif kind == "MOQ" and card.get("moq") is not None:
                    value = f"a minimum order of {card['moq']} pcs"
                elif kind == "LEAD_TIME" and card.get("lead_time_days") is not None:
                    value = f"lead time {card['lead_time_days']} days"
            if value is None:
                unresolved.append(f"{kind}:{sku}" + (f":{qty_text}" if qty_text else ""))
                return match.group(0)  # 悬空占位符保留原样 → KEY_MISSING 兜底
            filled_values.append(value)
            return value

        return ReplyGuards.PLACEHOLDER_RE.sub(_replace, draft), unresolved, filled_values

    @staticmethod
    def scan_leak(draft: str, legit_values: list[str]) -> list[str]:
        """价格残留扫描：先剥合法填充值，再以货币模式扫残留。"""
        cleaned = draft
        for legit in legit_values:
            cleaned = cleaned.replace(legit, "")
        return ReplyGuards.CURRENCY_RE.findall(cleaned)

    @staticmethod
    def _ladder_price(card: Mapping[str, Any], qty: int) -> float | None:
        """阶梯价取档：数量覆盖的最大档位价。"""
        tiers = [
            t for t in (card.get("ladder") or [])
            if isinstance(t, dict) and t.get("qty") is not None and qty >= t["qty"]
        ]
        return float(max(tiers, key=lambda t: t["qty"])["price"]) if tiers else None


# ------------------------------------------------------- 确定性生成桩（demo，形态同 brain.FakeBrainLlm）


class FakeSiteLlm:
    """确定性生成桩（demo/测试，零网络）：默认取首知识块构造带 [R1] 引用草稿。

    ``script`` 按「键 ⊆ Prompt」匹配返回 canned JSON（价格泄漏/占位符等分支的确定性入口）；
    记录 calls 与 last_prompt 供注入形态断言。
    """

    _BLOCK_HEADER_RE = re.compile(r"^\[(R\d+)\]\s*(.+)$", re.MULTILINE)

    def __init__(self, script: dict[str, str] | None = None) -> None:
        self.script = dict(script) if script else {}
        self.calls = 0
        self.last_prompt = ""

    def complete(self, prompt: str, *, timeout_s: float, max_tokens: int, temperature: float) -> str:
        self.calls += 1
        self.last_prompt = prompt
        for key, raw in self.script.items():
            if key in prompt:
                return raw
        blocks = self.parse_chunk_blocks(prompt)
        return json.dumps(self.default_payload(blocks), ensure_ascii=False)

    @classmethod
    def parse_chunk_blocks(cls, prompt: str) -> list[dict[str, str]]:
        """解析 【知识片段】…【引用格式】 之间的编号块。"""
        if "【知识片段】" not in prompt:
            return []
        section = prompt.split("【知识片段】", 1)[1].split("【引用格式】", 1)[0]
        blocks: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        body: list[str] = []
        for line in section.splitlines():
            matched = cls._BLOCK_HEADER_RE.match(line.strip())
            if matched:
                if current is not None:
                    current["text"] = " ".join(body).strip()
                    blocks.append(current)
                current = {"ref": matched.group(1), "title": matched.group(2).strip()}
                body = []
            elif current is not None and line.strip():
                body.append(line.strip())
        if current is not None:
            current["text"] = " ".join(body).strip()
            blocks.append(current)
        return blocks

    @staticmethod
    def default_payload(blocks: list[dict[str, str]]) -> dict[str, Any]:
        """默认结构化输出：首块首句 + [R#] 引用（无块时仅承诺跟进，绝不编造）。"""
        if not blocks:
            return {
                "reply_draft": "I'll confirm with our team and revert shortly.",
                "knowledge_refs": [], "missing_info": ["知识块缺失"],
                "intent": "general", "confidence": 0.2,
            }
        first = blocks[0]
        text = str(first.get("text", "")).strip()
        sentence = text.split("。", 1)[0] + "。" if "。" in text else text[:90]
        return {
            "reply_draft": f"Thanks for asking! {sentence} [{first['ref']}] Shall I email you the full spec?",
            "knowledge_refs": [first["ref"]], "missing_info": [],
            "intent": "询价", "confidence": 0.9,
        }


# --------------------------------------------------------------------------- 存储协议与内存实现


class GapRecorder(Protocol):
    """知识缺口登记协议（FR-TR-008 回流入口）。"""

    def record(self, sample_question: str, reason: str) -> None: ...


class MemoryGapRecorder:
    """内存缺口登记（demo）。"""

    def __init__(self) -> None:
        self.gaps: list[dict[str, Any]] = []

    def record(self, sample_question: str, reason: str) -> None:
        self.gaps.append({"sample_question": sample_question, "reason": reason})


class ConversationRepo(Protocol):
    """会话消息仓储协议（生产 = PG conversation/message，channel='site'）。"""

    def ensure_conversation(self, visitor_uid: str, lang: str = "en") -> str: ...

    def append(self, conversation_id: str, role: str, speaker: str, content: str, **meta: Any) -> None: ...

    def messages_of(self, conversation_id: str) -> list[dict[str, Any]]: ...

    def conversation_ids_of(self, visitor_uid: str) -> list[str]: ...

    def delete_by_visitors(self, uids: tuple[str, ...] | list[str]) -> dict[str, int]: ...


class MemoryConversationRepo:
    """内存会话仓储（demo）：conv_id → {visitor_uid, lang, messages}。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    def ensure_conversation(self, visitor_uid: str, lang: str = "en") -> str:
        """按访客取/建会话（同访客单会话口径，幂等）。"""
        for conv_id, row in self._rows.items():
            if row["visitor_uid"] == visitor_uid:
                return conv_id
        conv_id = f"c-{uuid.uuid4().hex[:12]}"
        self._rows[conv_id] = {"visitor_uid": visitor_uid, "lang": lang, "messages": []}
        return conv_id

    def append(self, conversation_id: str, role: str, speaker: str, content: str, **meta: Any) -> None:
        row = self._rows.setdefault(
            conversation_id, {"visitor_uid": "", "lang": "en", "messages": []}
        )
        row["messages"].append({"role": role, "speaker": speaker, "content": content, **meta})

    def messages_of(self, conversation_id: str) -> list[dict[str, Any]]:
        return list(self._rows.get(conversation_id, {}).get("messages", ()))

    def conversation_ids_of(self, visitor_uid: str) -> list[str]:
        return [cid for cid, row in self._rows.items() if row["visitor_uid"] == visitor_uid]

    def delete_by_visitors(self, uids: tuple[str, ...] | list[str]) -> dict[str, int]:
        """按访客级联删（DSR）：返回 {conversation, message} 计数。"""
        conv_n = msg_n = 0
        for cid in [c for c, r in self._rows.items() if r["visitor_uid"] in set(uids)]:
            msg_n += len(self._rows.pop(cid)["messages"])
            conv_n += 1
        return {"conversation": conv_n, "message": msg_n}


# --------------------------------------------------------------------------- 对话引擎


@dataclass(frozen=True)
class SiteReply:
    """对话引擎出参（WS text 下行 + message 落库的进程内形态）。"""

    content: str
    intent: str
    confidence: float
    knowledge_refs: list[dict[str, Any]]
    fallback: bool
    reason_code: str
    prompt: str = ""


class SiteChatService:
    """独立站对话主链路（SR-SITE-003 §6.1）：共用生成内核，按 site 分流风格/话术/处置。"""

    FALLBACK_TEMPLATES = FALLBACK_TEMPLATES

    def __init__(
        self,
        llm: ChatLlm,
        search_fn: SearchFn,
        hint_fn: HintFn | None,
        *,
        card_fn: ProductCardFn | None = None,
        repo: ConversationRepo | None = None,
        gaps: GapRecorder | None = None,
        invites: InviteLogStore | None = None,
        kb_types: tuple[str, ...] = DEFAULT_KB_TYPES_SITE,
        scene_tags: tuple[str, ...] = DEFAULT_SCENE_TAGS,
        top_k: int = 3,
    ) -> None:
        self._llm = llm
        self._search = search_fn
        self._hint = hint_fn
        self._card_fn = card_fn
        self._repo = repo
        self._gaps = gaps
        self._invites = invites
        self._kb_types = kb_types
        self._scene_tags = scene_tags
        self._top_k = top_k

    def reply(
        self, session: SiteSessionRecord, text: str, *, round_no: int = 0, msg_id: str = ""
    ) -> SiteReply:
        """WS text → 画像注入 → 生成 → 防线 → 下行（消息双向落库 + 漏斗回填）。"""
        conv_id = self._repo.ensure_conversation(session.uid, session.lang) if self._repo else ""
        if conv_id:
            self._repo.append(conv_id, "buyer", "buyer", text, msg_id=msg_id or None)
        if self._invites is not None:
            self._invites.mark_replied(session.uid)  # 漏斗：对话阶段回填（幂等列）
        hits = parse_hits(self._search(
            query=text,
            kb_types=list(self._kb_types),
            filters={"scene_tag": list(self._scene_tags)},
            top_k=self._top_k,
        ))
        if not hits:
            # 零命中不生成（R-03 闸 4）：LLM 零调用 + 缺口登记（FR-TR-008 回流）
            if self._gaps is not None:
                self._gaps.record(text, REASON_ZERO_HIT)
            reply = self._fallback_reply(session.lang, REASON_ZERO_HIT)
            if self._repo and conv_id:
                self._repo.append(conv_id, "ai", "ai", reply.content, knowledge_refs=[])
            return reply
        hint = self._hint(session.uid, round_no) if self._hint else None
        prompt = SITE_SYSTEM_PROMPT.format(
            language=session.lang,
            visitor_hint=hint or "（无画像）",
            chunk_block="\n".join(f"[R{i}] {h.title}\n{h.text}" for i, h in enumerate(hits, 1)),
            history_block=_history_block(self._repo.messages_of(conv_id)[:-1] if conv_id else []),
            text=text,
        )
        try:
            raw = self._llm.complete(prompt, timeout_s=REPLY_TIMEOUT_S, max_tokens=800, temperature=0.4)
        except LlmDownstreamError:
            reply = self._fallback_reply(session.lang, REASON_DOWNSTREAM, prompt)
            if self._repo and conv_id:
                self._repo.append(conv_id, "ai", "ai", reply.content, knowledge_refs=[])
            return reply
        content, refs_payload, reason, intent, confidence = self._resolve(raw, hits, session.lang)
        if self._repo and conv_id:
            self._repo.append(
                conv_id, "ai", "ai", content,
                msg_id=msg_id or None, intent=intent or None, knowledge_refs=refs_payload,
            )
        return SiteReply(
            content=content, intent=intent, confidence=confidence,
            knowledge_refs=refs_payload, fallback=bool(reason), reason_code=reason, prompt=prompt,
        )

    def _fallback_reply(self, lang: str, reason: str, prompt: str = "") -> SiteReply:
        """兜底出参：多语言固定话术（访客侧永不静默），引用为空。"""
        return SiteReply(
            content=FALLBACK_TEMPLATES.get(lang) or FALLBACK_TEMPLATES["en"],
            intent="", confidence=0.0, knowledge_refs=[],
            fallback=True, reason_code=reason, prompt=prompt,
        )

    def _resolve(
        self, raw: str, hits: list[RetrievedHit], lang: str
    ) -> tuple[str, list[dict[str, Any]], str, str, float]:
        """处置分流（SR-SITE-003 §6.1 _resolve）：防线命中 → 兜底；否则直发。

        :return: (内容, 引用载荷, 原因码, 意图, 置信度)
        """
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("not a json object")
        except (json.JSONDecodeError, ValueError):
            return self._fallback_text(lang), [], REASON_LLM_OUTPUT, "", 0.0
        draft = str(obj.get("reply_draft", ""))
        # ① 关键字段保护：占位符结构化填充 + 价格残留扫描（R-03）
        draft, unresolved, filled = ReplyGuards.fill_placeholders(draft, self._card_fn)
        if unresolved:
            return self._fallback_text(lang), [], REASON_KEY_MISSING, "", 0.0
        if ReplyGuards.scan_leak(draft, filled):
            return self._fallback_text(lang), [], REASON_PRICE_LEAK, "", 0.0
        # ② 引用合法性：报告引用必须全部命中 [R#] 集合且非空
        ref_map = {f"R{i}": hit for i, hit in enumerate(hits, 1)}
        reported = [str(r) for r in (obj.get("knowledge_refs") or [])]
        if not reported or any(r not in ref_map for r in reported):
            return self._fallback_text(lang), [], REASON_BAD_REFS, "", 0.0
        refs_payload = [
            {"ref": r, "chunk_id": ref_map[r].chunk_id, "title": ref_map[r].title, "score": ref_map[r].score}
            for r in reported
        ]
        intent = str(obj.get("intent", "general"))
        try:
            confidence = float(obj.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return draft, refs_payload, "", intent, confidence

    @classmethod
    def _fallback_text(cls, lang: str) -> str:
        """多语言兜底话术（en 兜底）。"""
        return FALLBACK_TEMPLATES.get(lang) or FALLBACK_TEMPLATES["en"]


# --------------------------------------------------------------------------- 主动邀聊引擎


@dataclass(frozen=True)
class InviteDecision:
    """邀聊判定出参（POST /site/invite/check data 段 / WS invite 下行的进程内形态）。"""

    triggered: bool
    trigger_type: str  # dwell | repeat_view | add_cart | ""
    reason: str        # "" | no_rule | daily_cap | cooldown | consent | disabled
    invite: dict[str, Any] | None = None


@dataclass
class InviteLog:
    """邀聊记录（site_invite_log 行；漏斗四阶段时间戳，条件写天然幂等）。"""

    invite_id: str
    visitor_uid: str
    trigger_type: str
    trigger_ctx: dict[str, Any]
    created_at: float
    opened_at: float | None = None
    replied_at: float | None = None
    lead_at: float | None = None


class InviteLogStore(Protocol):
    """邀聊记录存储协议。"""

    def append(self, log: InviteLog) -> None: ...

    def logs_of(self, visitor_uid: str) -> list[InviteLog]: ...


class MemoryInviteLogStore:
    """内存邀聊记录（demo）：含漏斗回填与 dwell 桩位。"""

    def __init__(self) -> None:
        self._rows: list[InviteLog] = []

    def append(self, log: InviteLog) -> None:
        self._rows.append(log)

    def logs_of(self, visitor_uid: str) -> list[InviteLog]:
        return [r for r in self._rows if r.visitor_uid == visitor_uid]

    def append_dwell_log(self, visitor_uid: str, ts: float) -> InviteLog:
        """测试桩位：直接落一条 dwell 触发记录。"""
        log = InviteLog(f"i-{uuid.uuid4().hex[:8]}", visitor_uid, "dwell", {}, ts)
        self._rows.append(log)
        return log

    def delete_by_visitors(self, uids: tuple[str, ...] | list[str]) -> int:
        """按访客级联删（DSR 连带）。"""
        uid_set = set(uids)
        before = len(self._rows)
        self._rows = [r for r in self._rows if r.visitor_uid not in uid_set]
        return before - len(self._rows)

    # ---- 漏斗回填（条件写：仅首次生效，天然幂等） ----

    def mark_opened(self, visitor_uid: str, ts: float) -> None:
        for log in self.logs_of(visitor_uid):
            if log.opened_at is None:
                log.opened_at = ts

    def mark_replied(self, visitor_uid: str, ts: float | None = None) -> None:
        for log in self.logs_of(visitor_uid):
            if log.replied_at is None:
                log.replied_at = ts if ts is not None else time.time()

    def mark_lead(self, visitor_uid: str, ts: float | None = None) -> None:
        for log in self.logs_of(visitor_uid):
            if log.lead_at is None:
                log.lead_at = ts if ts is not None else time.time()


class InviteGate(Protocol):
    """邀聊门控协议（FR-SITE-007 ConsentManager 实现之）。"""

    def allows_invite(self, visitor_uid: str) -> bool: ...


#: 默认开场白（kb 无 site_invite 模板时回落 en，SR-SITE-003 §6.2 _compose_opener）
DEFAULT_OPENER = {"title": "Hi there!", "content": "Need a hand? I can check price and lead time for you."}


class InviteEngine:
    """主动邀聊引擎（SR-SITE-003 §6.2）：三触发源 → 频率上限（原子）→ 模板化开场 → 下发。"""

    def __init__(
        self,
        kb_search: SearchFn | None,
        gate: InviteGate,
        logs: InviteLogStore,
        *,
        enabled: bool = True,
        dwell_s: float = 45.0,
        repeat_view_n: int = 3,
        daily_max: int = 2,
        cooldown_s: float = 600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._kb = kb_search
        self._gate = gate
        self._logs = logs
        self._enabled = enabled
        self._dwell_s = dwell_s
        self._repeat_n = repeat_view_n
        self._daily_max = daily_max
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._daily: dict[tuple[str, str], int] = {}  # (uid, ymd) → 已下发数（生产 = Redis INCR）

    @property
    def logs(self) -> InviteLogStore:
        """漏斗回填入口（测试与留资引擎使用）。"""
        return self._logs

    def on_event(self, visitor_uid: str, event: TrackedEvent, *, lang: str = "en") -> InviteDecision:
        """行为事件 → 邀聊判定（短路顺序：开关 → consent → 规则 → 日限 → 冷却）。"""
        if not self._enabled:
            return InviteDecision(False, "", "disabled")
        if not self._gate.allows_invite(visitor_uid):
            return InviteDecision(False, "", "consent")
        trigger = self._match(event)
        if trigger is None:
            return InviteDecision(False, "", "no_rule")
        if not self._frequency_ok(visitor_uid):
            return InviteDecision(False, "", "daily_cap")
        if self._in_cooldown(visitor_uid):
            return InviteDecision(False, "", "cooldown")
        opener = self._compose_opener(trigger, lang)
        log = InviteLog(
            invite_id=f"i-{uuid.uuid4().hex[:12]}", visitor_uid=visitor_uid,
            trigger_type=trigger[0], trigger_ctx=trigger[1], created_at=self._clock(),
        )
        self._logs.append(log)
        day_key = (visitor_uid, time.strftime("%Y%m%d", time.gmtime(self._clock())))
        self._daily[day_key] = self._daily.get(day_key, 0) + 1  # INCR（生产 Redis 原子；多副本安全）
        return InviteDecision(True, trigger[0], "", invite={
            "invite_id": log.invite_id, "trigger_type": trigger[0],
            "title": opener["title"], "content": opener["content"],
        })

    # ---- 漏斗回填（委托存储；条件写幂等） ----

    def mark_opened(self, visitor_uid: str, ts: float | None = None) -> None:
        """访客点击邀聊气泡（WS invite_open 上行）→ opened_at。"""
        self._logs.mark_opened(visitor_uid, ts if ts is not None else self._clock())

    def mark_replied(self, visitor_uid: str, ts: float | None = None) -> None:
        """访客回复（对话阶段回填）→ replied_at。"""
        self._logs.mark_replied(visitor_uid, ts if ts is not None else self._clock())

    def mark_lead(self, visitor_uid: str, ts: float | None = None) -> None:
        """留资成功（留资阶段回填）→ lead_at。"""
        self._logs.mark_lead(visitor_uid, ts if ts is not None else self._clock())

    # ---- 规则 ----

    def _match(self, event: TrackedEvent) -> tuple[str, dict[str, Any]] | None:
        """三规则（05 §8）：停留阈值 / 同品重复访问 / 加购未结算。"""
        if event.type == "dwell" and event.dwell_ms >= self._dwell_s * 1000:
            return "dwell", {"page": event.page, "dwell_s": event.dwell_ms // 1000}
        nth = int(event.payload.get("nth", 0))
        if event.type == "repeat_view" and nth >= self._repeat_n:
            return "repeat_view", {"sku": event.payload.get("sku", ""), "view_count": nth}
        if event.type == "add_cart":
            return "add_cart", {"sku": event.payload.get("sku", "")}
        return None

    def _frequency_ok(self, visitor_uid: str) -> bool:
        """每访客每日 ≤ SITE_INVITE_DAILY_MAX（默认 2，防骚扰）。"""
        key = (visitor_uid, time.strftime("%Y%m%d", time.gmtime(self._clock())))
        return self._daily.get(key, 0) < self._daily_max

    def _in_cooldown(self, visitor_uid: str) -> bool:
        """冷却窗：下发后 cooldown_s 内不再触发。"""
        logs = self._logs.logs_of(visitor_uid)
        last = max((log.created_at for log in logs), default=None)
        return last is not None and self._clock() - last < self._cooldown_s

    def _compose_opener(self, trigger: tuple[str, dict[str, Any]], lang: str) -> dict[str, str]:
        """个性化开场：检索 skill 库 scene_tag=site_invite 模板 + 槽位填充；无匹配回落 en 默认。"""
        slots = dict(trigger[1])
        template_text = ""
        if self._kb is not None:
            hits = parse_hits(self._kb(
                query=f"site invite opener {trigger[0]} {slots.get('sku', '')}",
                kb_types=["skill"], filters={"scene_tag": ["site_invite"]}, top_k=1,
            ))
            if hits:
                template_text = hits[0].text
        if not template_text:
            return dict(DEFAULT_OPENER)
        body = template_text
        for key, value in slots.items():
            body = body.replace("{" + str(key) + "}", str(value))
        body = body.replace("{sku}", str(slots.get("sku", "our product")))
        return {"title": DEFAULT_OPENER["title"], "content": body}


# --------------------------------------------------------------------------- 留资引擎


class CustomerWriter(Protocol):
    """客户写入协议（生产 = FR-SITE-005 客户主档服务；渠道 site 合并入口）。"""

    def upsert(
        self,
        email: str,
        whatsapp: str,
        *,
        channel: str,
        external_id: str = "",
        profile_tags: tuple[str, ...] = (),
    ) -> dict[str, Any]: ...

    def find_by_email(self, email: str) -> dict[str, Any] | None: ...


class MemoryCustomerWriter:
    """内存客户主档（demo）：CITEXT 归一（邮箱小写）+ channels 补渠道。"""

    def __init__(self) -> None:
        self._by_email: dict[str, dict[str, Any]] = {}
        self._next_id = 1

    def upsert(
        self,
        email: str,
        whatsapp: str,
        *,
        channel: str,
        external_id: str = "",
        profile_tags: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """同邮箱已存在（阿里/邮件渠道）→ 不新建，channels 补 {channel: external_id}。"""
        key = email.strip().lower()
        record = self._by_email.get(key)
        created = False
        if record is None:
            record = {
                "customer_id": self._next_id, "email": key, "whatsapp": whatsapp,
                "channels": {}, "profile_tags": tuple(profile_tags),
            }
            self._next_id += 1
            self._by_email[key] = record
            created = True
        if external_id:
            record["channels"][channel] = external_id
        if profile_tags and not record["profile_tags"]:
            record["profile_tags"] = tuple(profile_tags)
        return {"customer_id": record["customer_id"], "created": created}

    def find_by_email(self, email: str) -> dict[str, Any] | None:
        return self._by_email.get(email.strip().lower())

    def delete_ids(self, ids: tuple[int, ...] | list[int]) -> int:
        """按 id 删除（DSR 连带；生产为线索软删+主档勾选，SR-SITE-007 §6.4）。"""
        id_set = set(ids)
        removed = 0
        for key in [k for k, r in self._by_email.items() if r["customer_id"] in id_set]:
            self._by_email.pop(key)
            removed += 1
        return removed

    def all(self) -> list[dict[str, Any]]:
        return list(self._by_email.values())


class LeadStore(Protocol):
    """留资记录存储协议（site_lead）。"""

    def add(self, record: dict[str, Any]) -> None: ...

    def leads_of(self, visitor_uid: str) -> list[dict[str, Any]]: ...

    def delete_by_visitors(self, uids: tuple[str, ...] | list[str]) -> int: ...


class MemoryLeadStore:
    """内存留资记录（demo）。"""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def add(self, record: dict[str, Any]) -> None:
        self._rows.append(record)

    def leads_of(self, visitor_uid: str) -> list[dict[str, Any]]:
        return [r for r in self._rows if r["visitor_uid"] == visitor_uid]

    def delete_by_visitors(self, uids: tuple[str, ...] | list[str]) -> int:
        uid_set = set(uids)
        before = len(self._rows)
        self._rows = [r for r in self._rows if r["visitor_uid"] not in uid_set]
        return before - len(self._rows)


class LeadCapture:
    """留资引擎（SR-SITE-003 §6.3）：结构化校验 → Customer(渠道 site) → 事件/漏斗回填。"""

    def __init__(
        self,
        customers: CustomerWriter,
        leads: LeadStore,
        *,
        profiles: Any | None = None,
        invites: InviteLogStore | None = None,
        events_sink: Callable[[TrackedEvent], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._customers = customers
        self._leads = leads
        self._profiles = profiles
        self._invites = invites
        self._events = events_sink
        self._clock = clock

    def capture(
        self,
        session: SiteSessionRecord,
        fields: dict[str, Any],
        *,
        source: str = "chat_form",
        trigger_event: str = "quote",
    ) -> dict[str, Any]:
        """表单提交（或 AI 抽取）→ 校验 → 落库；校验失败返回字段级错误（WS lead_ack ok=false）。"""
        errors = self._validate(fields)
        if errors:
            return {"ok": False, "errors": errors}
        email = fields.get("email", "").strip()
        whatsapp = fields.get("whatsapp", "").strip()
        tags: tuple[str, ...] = ()
        if self._profiles is not None:
            profile = self._profiles.load(session.uid)
            if profile is not None:
                tags = profile.to_tags()
        ref = self._customers.upsert(
            email, whatsapp, channel="site", external_id=session.uid, profile_tags=tags
        )
        lead_id = f"L-{uuid.uuid4().hex[:12]}"
        self._leads.add({
            "lead_id": lead_id, "visitor_uid": session.uid, "customer_id": ref["customer_id"],
            "email": email.lower(), "whatsapp": whatsapp, "source": source,
            "trigger_event": trigger_event, "created_at": self._clock(),
        })
        if self._events is not None:  # visitor_event 枚举已含 lead（03 §3.4）
            self._events(TrackedEvent(
                ev_id=f"lead-{lead_id}", type="lead", page=session.page, ts=self._clock(),
                payload={"sku": "", "lead_id": lead_id},
            ))
        if self._invites is not None:  # 漏斗：留资阶段回填
            self._invites.mark_lead(session.uid)
        return {
            "ok": True, "lead_id": lead_id, "customer_id": ref["customer_id"],
            "customer_created": ref["created"],
        }

    @staticmethod
    def _validate(fields: dict[str, Any]) -> dict[str, str]:
        """结构化校验：email 词面 + whatsapp E.164；至少一项（二者皆空双报错）。"""
        errors: dict[str, str] = {}
        email = str(fields.get("email", "")).strip()
        whatsapp = str(fields.get("whatsapp", "")).strip()
        if email and not EMAIL_RE.match(email):
            errors["email"] = "invalid format"
        if whatsapp and not WHATSAPP_RE.match(whatsapp):
            errors["whatsapp"] = "invalid E.164 format"
        if not email and not whatsapp:
            errors["email"] = "required"
            errors["whatsapp"] = "required"
        return errors


__all__ = [
    "DEFAULT_KB_TYPES_SITE",
    "DEFAULT_OPENER",
    "DEFAULT_SCENE_TAGS",
    "FALLBACK_TEMPLATES",
    "InviteDecision",
    "InviteEngine",
    "InviteGate",
    "InviteLog",
    "InviteLogStore",
    "LeadCapture",
    "LeadStore",
    "LlmDownstreamError",
    "ChatLlm",
    "ConversationError",
    "ConversationRepo",
    "CustomerWriter",
    "FakeSiteLlm",
    "HintFn",
    "MemoryConversationRepo",
    "MemoryCustomerWriter",
    "MemoryGapRecorder",
    "MemoryInviteLogStore",
    "MemoryLeadStore",
    "GapRecorder",
    "ProductCardFn",
    "REPLY_TIMEOUT_S",
    "REASON_BAD_REFS",
    "REASON_DOWNSTREAM",
    "REASON_KEY_MISSING",
    "REASON_LLM_OUTPUT",
    "REASON_PRICE_LEAK",
    "REASON_ZERO_HIT",
    "ReplyGuards",
    "RetrievedHit",
    "SITE_SYSTEM_PROMPT",
    "SearchFn",
    "SiteChatService",
    "SiteReply",
    "TrackedEvent",
    "parse_hits",
]
