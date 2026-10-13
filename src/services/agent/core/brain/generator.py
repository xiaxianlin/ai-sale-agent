# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""回复生成链路（FR-AG-003 链路②③④⑤，SR-AG-003 §6.2/§6.3；迭代 2.2 demo 口径）。

主流程（:class:`ReplyGenerator.generate`）：意图对齐检索 → 上下文组装 → 生成 → 后校验 →
自评 → 置信度合成 → 兜底判定，输出 {reply_draft, confidence, knowledge_refs, verdict}。

关键设计（R-03 幻觉防护四道闸）：
① **价格/交期不进自由生成**——LLM 只写占位符 ``{{PRICE:SKU:QTY}}`` 等，仅由注入的
   :class:`product_card_fn`（结构化产品库，SR-KB-001 权威源）填充；卡缺失/无匹配档 →
   unresolved，绝不猜数字；
② **价格残留硬拦截**——先剥离合法填充值，再以货币模式正则扫描残留，命中即
   FACT_CHECK_FAIL；
③ **引用合法性校验**——LLM 报告的引用必须全部命中检索块 [R#] 集合；空引用/伪造引用
   → 不允许自动发送；
④ **零命中不生成**——检索零命中直接 ZERO_HIT 转人工+缺口记录，LLM 零调用。

依赖全部注入：检索走 ``search_fn``（FR-KB-030 契约：query/kb_types/filters/top_k →
``{"results": [{chunk_id, score, ...}]}``），LLM 走 :class:`BrainLlmClient` 协议
（生产 DeepSeek / demo :class:`FakeBrainLlm` 确定性桩），历史走 ``history_fn``。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from foundation.core.textblocks import parse_chunk_blocks
from services.agent.core.brain.confidence import ConfidenceComposer, SelfEval
from services.agent.core.brain.fallback import (
    FallbackEvidence,
    FallbackGate,
    FallbackVerdict,
)
from services.agent.core.brain.intent import OTHER_INTENT, IntentResult, LlmOutputError

# --------------------------------------------------------------------------- 请求/命中模型


@dataclass(frozen=True)
class Customer:
    """客户档案（API 请求体 customer 段的进程内形态）。"""

    uid: str
    grade: str = "B"
    profile_tags: tuple[str, ...] = ()
    lang: str = "en"


@dataclass(frozen=True)
class ReplyRequest:
    """生成请求：消息 + 客户档案（检索在本模块内完成，SR-AG-003 §5 (2)）。"""

    message_uid: str
    conversation_uid: str
    account_id: int
    text: str
    customer: Customer


@dataclass(frozen=True)
class RetrievedHit:
    """检索命中块（FR-KB-030 results 条目的模块内形态；ref 由组装期按位次指派 R1..Rn）。"""

    chunk_id: int
    score: float
    title: str
    text: str


#: 检索函数契约（FR-KB-030 冻结 v1 的进程内形态）
SearchFn = Callable[..., Mapping[str, Any]]
#: 产品结构化取数契约（SR-KB-001 get_product_card 的进程内形态；sku → 卡或 None）
ProductCardFn = Callable[[str], Mapping[str, Any] | None]
#: 会话历史取数契约（生产 = PG message 窗口；[{role, content}] 最旧→最新）
HistoryFn = Callable[[str], list[dict[str, str]]]


def parse_hits(payload: Mapping[str, Any] | None) -> list[RetrievedHit]:
    """解析 FR-KB-030 results 载荷 → RetrievedHit 列表（容忍缺字段，缺省记 0/空）。"""
    results = (payload or {}).get("results") or []
    hits: list[RetrievedHit] = []
    for row in results:
        hits.append(
            RetrievedHit(
                chunk_id=int(row.get("chunk_id", 0)),
                score=float(row.get("score", 0.0)),
                title=str(row.get("title", "")),
                text=str(row.get("text", "")),
            )
        )
    return hits


#: 意图 → 知识库类型映射（agent-core 注册默认口径，SR-AG-003 §2.3）
INTENT_KB_MAP: dict[str, tuple[str, ...]] = {
    "询价": ("product", "customer"),
    "技术": ("product", "process"),
    "样品": ("process", "skill"),
    "投诉": ("customer", "process"),
    "物流": ("process", "skill"),
    "闲聊": ("skill",),
    OTHER_INTENT: ("product", "process", "skill"),
}
#: 未映射意图的兜底知识库档（other 等走全库检索）
DEFAULT_KB_TYPES: tuple[str, ...] = ("product", "process", "skill")

# --------------------------------------------------------------------------- 上下文组装

#: 分块字符预算（demo 口径：token 预算按 1 token≈1 中文字符简化；生产按 tokenizer 计）
DEFAULT_BLOCK_BUDGETS: dict[str, int] = {"chunk": 600, "customer": 400, "history": 800, "summary": 200}


@dataclass(frozen=True)
class PromptContext:
    """四块上下文（System Prompt 组装输入；各自按预算裁剪）。"""

    customer_block: str
    history_block: str
    summary_block: str
    chunk_block: str


class ContextBuilder:
    """上下文组装：近端 N 轮原文逐字保留 + 远端压缩摘要（SR-AG-003 §6.2）。

    demo 口径：生产远端由 LLM 滚动摘要（Redis 缓存、可从 PG 重建）；demo 为保持
    确定性、零额外 LLM 调用，以「消息指纹摘要」替代——远端原文绝不进入 Prompt，
    仅留逐条 SHA1 短指纹供回溯对账（压缩语义等价：省 Prompt、可追溯）。
    """

    def __init__(
        self,
        history_fn: HistoryFn | None = None,
        *,
        recent_turns: int = 8,
        history_window: int = 32,
        budgets: Mapping[str, int] | None = None,
    ) -> None:
        self._history_fn = history_fn
        self._recent_turns = recent_turns
        self._history_window = history_window
        self._budgets = dict(budgets) if budgets is not None else dict(DEFAULT_BLOCK_BUDGETS)

    def build(self, req: ReplyRequest, hits: list[RetrievedHit]) -> PromptContext:
        """组装四块：客户档案 / 近期对话（原文）/ 历史摘要（压缩）/ 知识片段（预算截断）。"""
        # ① 知识片段：块预算截断（块头 [R#]+标题 保留，正文长尾丢弃）
        chunk_budget = self._budgets["chunk"]
        chunk_lines: list[str] = []
        for i, hit in enumerate(hits, start=1):
            chunk_lines.append(f"[R{i}] {hit.title}\n{hit.text[:chunk_budget]}")
        # ② 客户档案：PII 二次过滤挂点（生产在组装前脱敏，06-安全与合规 §6；demo 字段全非敏感）
        customer_block = (
            f"uid={req.customer.uid}；等级 {req.customer.grade}；"
            f"画像 {'、'.join(req.customer.profile_tags) or '（无）'}；语言 {req.customer.lang}"
        )
        # ③ 历史：近端 recent_turns*2 条原文逐字（每条按预算截断）；更早压缩为指纹摘要
        messages = list(self._history_fn(req.conversation_uid)) if self._history_fn else []
        messages = messages[-self._history_window:]
        k = self._recent_turns * 2  # 一轮 = 买卖各一条
        recent, older = messages[-k:], messages[:-k]
        history_budget = self._budgets["history"]
        history_block = "\n".join(
            f"[{m.get('role', 'buyer')}] {m.get('content', '')[:history_budget]}" for m in recent
        ) or "（无近期对话）"
        if older:
            fingerprints = ",".join(_content_fingerprint(str(m.get("content", ""))) for m in older)
            summary_block = (
                f"（较早 {len(older)} 条消息的滚动摘要，指纹：{fingerprints[:self._budgets['summary']]}）"
            )
        else:
            summary_block = "（无更早历史）"
        return PromptContext(
            customer_block=customer_block[: self._budgets["customer"]],
            history_block=history_block,
            summary_block=summary_block,
            chunk_block="\n".join(chunk_lines),
        )


def _content_fingerprint(content: str) -> str:
    """消息内容指纹（SHA1 前 8 位）：压缩摘要的确定性载体，原文不回传。"""
    import hashlib

    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------------- System Prompt（集中管理，便于评审与回归）

SYSTEM_PROMPT = """你是一名专业外贸销售，代表公司用{language}回复客户消息。
【企业话术风格】专业、简洁、礼貌；落款以公司名义。
【知识片段】以下为唯一事实来源，块前 [R#] 为引用编号：
{chunk_block}
【引用格式】每个事实性陈述句末标注来源编号（如 [R1]）；只允许引用上述编号。
【禁止编造】一、价格/MOQ/交期一律写占位符 {{{{PRICE:SKU:QTY}}}}、{{{{MOQ:SKU}}}}、{{{{LEAD_TIME:SKU}}}}，严禁自己填写任何数字；
二、知识片段没有的内容写 "I'll confirm with our factory and revert shortly"，不得猜测；三、不承诺赔偿、法律责任或站外交易。
【客户背景】{customer_block}
【会话历史摘要】{summary_block}　【近期对话】{history_block}　【客户消息】{text}
仅输出 JSON：{{"reply_draft":"…","knowledge_refs":["R1"],"missing_info":["…"]}}"""

# --------------------------------------------------------------------------- 关键字段保护（R-03）

#: 占位符形态：{{PRICE:SKU:QTY}} / {{MOQ:SKU}} / {{LEAD_TIME:SKU}}
_PLACEHOLDER_RE = re.compile(r"\{\{(PRICE|MOQ|LEAD_TIME):([A-Za-z0-9_-]+)(?::(\d+))?\}\}")
#: 货币/关键数字残留模式（价格残留硬拦截的扫描面）
_CURRENCY_RE = re.compile(
    r"[$€£]\s*\d[\d,]*(?:\.\d+)?"                                  # $12.5 / €9
    r"|(?:USD|EUR|GBP|美元|欧元|英镑)\s*\d[\d,]*(?:\.\d+)?"          # USD 9.5 / 美元 12
    r"|\d[\d,]*(?:\.\d+)?\s*(?:USD|EUR|GBP|美元|欧元|英镑)"          # 12.5 USD / 12.5美元
    r"|MOQ\D{0,3}\d+"                                              # MOQ 数字
    r"|(?:lead\s*time|交货|交期)\D{0,10}\d+"                        # lead time 25 / 交期 25
    r"|\d+(?:\.\d+)?\s*(?:天|days?)\s*(?:交货|交期|lead)",           # 25天交货 / 25 days lead
    re.IGNORECASE,
)


def _select_ladder_price(card: Mapping[str, Any], qty: int) -> float | None:
    """阶梯价取档：数量覆盖的最大档位价（qty ≥ 档位量的档中取最高档）；无覆盖档返回 None。"""
    ladder = card.get("ladder") or []
    tiers = [t for t in ladder if isinstance(t, dict) and t.get("qty") is not None and qty >= t["qty"]]
    if not tiers:
        return None
    return float(max(tiers, key=lambda t: t["qty"])["price"])


class PriceGuard:
    """关键字段保护（R-03）：占位符结构化填充 + 价格残留扫描（SR-AG-003 §6.3）。"""

    @staticmethod
    def fill_placeholders(
        draft: str, card_fn: ProductCardFn | None
    ) -> tuple[str, list[str], list[str]]:
        """占位符填充。

        :return: (填充后草稿, unresolved 列表, 合法填充值列表)
            产品卡缺失 / 数量无匹配档 → 记 unresolved 且占位符原样保留（绝不猜数字）；
            合法填充值回传供泄漏扫描剥除（防误报）。
        """
        unresolved: list[str] = []
        filled_values: list[str] = []

        def _replace(match: re.Match[str]) -> str:
            kind, sku, qty_text = match.group(1), match.group(2), match.group(3)
            card = card_fn(sku) if card_fn is not None else None
            value: str | None = None
            if card is not None:
                if kind == "PRICE" and qty_text is not None:
                    price = _select_ladder_price(card, int(qty_text))
                    if price is not None:
                        value = f"USD {price}/pc"
                elif kind == "MOQ" and card.get("moq") is not None:
                    value = f"a minimum order of {card['moq']} pcs"
                elif kind == "LEAD_TIME" and card.get("lead_time_days") is not None:
                    value = f"lead time {card['lead_time_days']} days"
            if value is None:
                unresolved.append(f"{kind}:{sku}" + (f":{qty_text}" if qty_text else ""))
                return match.group(0)  # 悬空占位符保留原样，交 KEY_FIELD_MISSING 兜底
            filled_values.append(value)
            return value

        return _PLACEHOLDER_RE.sub(_replace, draft), unresolved, filled_values

    @staticmethod
    def scan_leak(draft: str, legit_values: list[str]) -> list[str]:
        """价格残留扫描：先剥除合法填充值（结构化取数），再以货币模式正则扫残留。"""
        cleaned = draft
        for legit in legit_values:
            cleaned = cleaned.replace(legit, "")
        return _CURRENCY_RE.findall(cleaned)


class RuleBasedSelfEvaluator:
    """规则化自评（demo 口径；生产 = SELF_EVAL_PROMPT 的 LLM 质检输出）。

    确定性规则：草稿携带至少一个合法 [R#] 引用 → 忠实度 1.0，否则 0.0（乘法门一票否低）；
    missing_info 为空 → 完整性 1.0，否则 0.6；语气恒 1.0（模板话术保底）。
    """

    _REF_MARK_RE = re.compile(r"\[(R\d+)\]")

    def evaluate(self, draft: str, valid_refs: list[str], missing_info: list[str]) -> SelfEval:
        """按引用覆盖与缺项清单产出三维自评。"""
        valid_set = set(valid_refs)
        marks = self._REF_MARK_RE.findall(draft)
        faithfulness = 1.0 if any(m in valid_set for m in marks) else 0.0
        completeness = 1.0 if not missing_info else 0.6
        return SelfEval(faithfulness=faithfulness, completeness=completeness, tone=1.0)


# --------------------------------------------------------------------------- 确定性生成桩


class FakeBrainLlm:
    """确定性生成桩（demo/测试，零网络请求）。

    默认行为：解析 Prompt 【知识片段】区的 [R#] 块，取首块正文首句构造带引用草稿
    （零编造、可溯源——与 prompt 禁编造指令一致）；``script`` 注入时按「键 ⊆ Prompt」
    匹配返回 canned JSON（价格泄漏/占位符/伪造引用等分支的确定性入口）。
    记录 ``calls`` 与 ``last_prompt`` 供组装形态断言。
    """

    _BLOCK_HEADER_RE = re.compile(r"^\[(R\d+)\]\s*(.+)$", re.MULTILINE)

    def __init__(self, script: dict[str, str] | None = None) -> None:
        self.script = dict(script) if script else {}
        self.calls: int = 0
        self.last_prompt: str = ""

    def complete(
        self, prompt: str, *, timeout_s: float, max_tokens: int, temperature: float
    ) -> str:
        """按脚本/默认规则产出结构化 JSON（与生产 System Prompt 契约同形）。"""
        self.calls += 1
        self.last_prompt = prompt
        for key, raw in self.script.items():
            if key in prompt:
                return raw
        blocks = self.parse_chunk_blocks(prompt)
        return json.dumps(self.default_payload(blocks), ensure_ascii=False)

    @classmethod
    def parse_chunk_blocks(cls, prompt: str) -> list[dict[str, str]]:
        """委托 foundation 单一实现（CR Q1：跨服务拷贝收敛，格式变更单点生效）。"""
        return parse_chunk_blocks(prompt)

    @staticmethod
    def default_payload(blocks: list[dict[str, str]]) -> dict[str, Any]:
        """默认结构化输出：首块首句 + [R#] 引用（无块时仅承诺跟进，绝不编造）。"""
        if not blocks:
            return {
                "reply_draft": "I'll confirm with our factory and revert shortly.",
                "knowledge_refs": [],
                "missing_info": ["知识块缺失"],
            }
        first = blocks[0]
        text = str(first.get("text", "")).strip()
        if "。" in text:
            sentence = text.split("。", 1)[0] + "。"  # 首个完整句（保留句读）
        else:
            sentence = text[:90]
        draft = (
            f"Thanks for your inquiry. {sentence} [{first['ref']}] "
            "We will confirm more details shortly."
        )
        return {"reply_draft": draft, "knowledge_refs": [first["ref"]], "missing_info": []}


# --------------------------------------------------------------------------- 生成主流程


@dataclass(frozen=True)
class ReplyResult:
    """生成结果：草稿 + 置信度明细 + 知识来源 + 兜底判定（全链决策明细）。"""

    intent: IntentResult
    reply_draft: str
    confidence: float
    conf_components: dict[str, float] = field(default_factory=dict)
    knowledge_refs: list[dict[str, Any]] = field(default_factory=list)
    verdict: FallbackVerdict | None = None

    @property
    def need_human(self) -> bool:
        """是否转人工（判定在生成之后、发送之前——末端门）。"""
        return self.verdict.need_human if self.verdict is not None else True

    @property
    def reason_code(self) -> str:
        """兜底原因码（自动发送为空串）。"""
        return self.verdict.reason_code if self.verdict is not None else "GATE_ERROR"

    @property
    def action(self) -> str:
        """处置动作（auto_send / human_review / human_review_gap）。"""
        return self.verdict.action if self.verdict is not None else "human_review"


class ReplyGenerator:
    """生成主流程：检索 → 组装 → 生成 → 后校验 → 自评 → 置信度 → 兜底判定（SR-AG-003 §6.2）。"""

    def __init__(
        self,
        llm: Any,
        search_fn: SearchFn,
        gate: FallbackGate,
        composer: ConfidenceComposer,
        *,
        context_builder: ContextBuilder | None = None,
        history_fn: HistoryFn | None = None,
        product_card_fn: ProductCardFn | None = None,
        self_eval_enabled: bool = True,
        top_k: int = 5,
    ) -> None:
        self._llm = llm
        self._search = search_fn
        self._gate = gate
        self._composer = composer
        self._context = context_builder if context_builder is not None else ContextBuilder(history_fn)
        self._card_fn = product_card_fn
        self._self_eval_enabled = self_eval_enabled
        self._top_k = top_k
        self._self_evaluator = RuleBasedSelfEvaluator()

    # ---- 对外主流程 ----

    def generate(self, req: ReplyRequest, intent: IntentResult) -> ReplyResult:
        """全链生成（同步 demo 形态；编排语义与 SR-AG-003 §2.1 一致）。"""
        if intent.name == "敏感":
            # 敏感不检索不生成，交 FR-AG-005/009 处置（R-03 + §7.4）
            verdict = self._gate.decide(self._evidence(intent, hits=[], fact_ok=True, key_ok=True,
                                                       self_eval=0.0, confidence=0.0, sensitive=True))
            return ReplyResult(intent, "", 0.0, {}, [], verdict)
        hits = parse_hits(self._search(
            self._intent_aligned_query(req, intent),
            kb_types=list(INTENT_KB_MAP.get(intent.name, DEFAULT_KB_TYPES)),
            filters=(intent.slot_filters() or None),
            top_k=self._top_k,
        ))
        if not hits:
            # 零命中不得强行生成（R-03）：ZERO_HIT 转人工 + 缺口记录（判定权在门）
            conf, comps = self._composer.compose([], None, fact_ok=True)
            verdict = self._gate.decide(self._evidence(intent, hits=[], fact_ok=True, key_ok=True,
                                                       self_eval=comps["self_eval"], confidence=conf))
            return ReplyResult(intent, "", conf, comps, [], verdict)
        # ① 组装（预算裁剪）→ ② 生成（结构化输出）
        ctx = self._context.build(req, hits)
        prompt = SYSTEM_PROMPT.format(
            language=req.customer.lang,
            chunk_block=ctx.chunk_block,
            customer_block=ctx.customer_block,
            summary_block=ctx.summary_block,
            history_block=ctx.history_block,
            text=req.text,
        )
        raw = self._llm.complete(prompt, timeout_s=30.0, max_tokens=800, temperature=0.3)
        obj = self._parse_json(raw)
        draft_text = str(obj.get("reply_draft", ""))
        refs_reported = [str(r) for r in (obj.get("knowledge_refs") or [])]
        missing_info = [str(m) for m in (obj.get("missing_info") or [])]
        ref_map = {f"R{i}": hit for i, hit in enumerate(hits, start=1)}
        refs_valid = [r for r in refs_reported if r in ref_map]
        # ③ 关键字段保护：占位符结构化填充 + 价格残留扫描（R-03 核心）
        draft, unresolved, filled_values = PriceGuard.fill_placeholders(draft_text, self._card_fn)
        leaks = PriceGuard.scan_leak(draft, filled_values)
        # ④ 后校验：引用合法（全部命中且非空）+ 无价格残留 → 事实校验通过
        fact_ok = bool(refs_reported) and len(refs_valid) == len(refs_reported) and not leaks
        # ⑤ 自评（关闭则权重重分配至检索，compose 内承载）
        self_eval = (
            self._self_evaluator.evaluate(draft, refs_valid, missing_info)
            if self._self_eval_enabled
            else None
        )
        conf, comps = self._composer.compose([h.score for h in hits], self_eval, fact_ok=fact_ok)
        # ⑥ 兜底判定（生成之后、发送之前的末端门；执行归 FR-AG-009）
        verdict = self._gate.decide(self._evidence(
            intent, hits, fact_ok=fact_ok, key_ok=not unresolved,
            self_eval=comps["self_eval"], confidence=conf,
        ))
        refs_payload = [
            {"ref": ref, "chunk_id": ref_map[ref].chunk_id,
             "title": ref_map[ref].title, "score": ref_map[ref].score}
            for ref in refs_valid
        ]
        return ReplyResult(intent, draft, conf, comps, refs_payload, verdict)

    # ---- 内部 ----

    @staticmethod
    def _evidence(
        intent: IntentResult,
        hits: list[RetrievedHit],
        *,
        fact_ok: bool,
        key_ok: bool,
        self_eval: float,
        confidence: float,
        sensitive: bool = False,
    ) -> FallbackEvidence:
        """汇编判定输入（FallbackEvidence，SR-AG-009 §6.1 契约）。"""
        return FallbackEvidence(
            intent=intent.name,
            hit_count=len(hits),
            top_score=hits[0].score if hits else 0.0,
            self_eval=self_eval,
            fact_check_ok=fact_ok,
            key_fields_complete=key_ok,
            sensitive=sensitive or intent.name == "敏感",
            confidence=confidence,
        )

    @staticmethod
    def _intent_aligned_query(req: ReplyRequest, intent: IntentResult) -> str:
        """意图对齐查询：原文 + 槽位 SKU（词面改写归检索内核，SR-AG-003 §2.2 对齐标注）。"""
        skus = intent.slots.get("sku") or []
        return f"{req.text} {' '.join(skus)}".strip()

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """解析生成输出；不可解析/结构非对象 → LlmOutputError（端点映射 50002）。"""
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LlmOutputError(f"生成输出不可解析：{exc}") from exc
        if not isinstance(obj, dict):
            raise LlmOutputError("生成输出结构非法：须为 JSON 对象")
        return obj


__all__ = [
    "Customer",
    "ReplyRequest",
    "RetrievedHit",
    "SearchFn",
    "ProductCardFn",
    "HistoryFn",
    "INTENT_KB_MAP",
    "DEFAULT_KB_TYPES",
    "DEFAULT_BLOCK_BUDGETS",
    "PromptContext",
    "ContextBuilder",
    "SYSTEM_PROMPT",
    "PriceGuard",
    "RuleBasedSelfEvaluator",
    "FakeBrainLlm",
    "ReplyResult",
    "ReplyGenerator",
    "parse_hits",
]
