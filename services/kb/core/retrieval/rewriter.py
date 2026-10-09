# 知识产权归甲方所有（COM-IP-01）
"""① 查询改写（FR-KB-015 / SR-KB-015 §6.2）：规则层先行 + 可选 LLM 钩子。

三步规则（本地执行，微秒级，独立可用——KB_SEARCH_LLM_REWRITE_ENABLED 关闭时
仅此路径，NFR-SEC-06 / OI-09 数据边界评估兜底）：
    规则① 纠错：NFKC 全半角规整（ＳＴ３２００→ST3200）+ 错拼词典（长词优先）；
    规则② 别名扩展：SKU 别名词典（口语/俗称/错拼 → 标准 SKU），词典可注册
        （对应 sku_alias 表，FR-KB-020 管理后台维护，本迭代以进程内注册承接）；
    规则③ 意图对齐：口语问句 → 标准维度词（最大承载/报价/交期/MOQ/物流/…），
        同时产出意图槽位（slots），供召回段结构化路构建 expr。

LLM 快速通道（可选钩子，None 时仅规则）：接受 async 或 sync 可调用，经
asyncio.wait_for 施加硬超时（默认 115ms，判定点 A）——超时/异常均降级为规则
结果直出，链路不阻塞；合并采用「保真优先」：SKU/数字槽位不被 LLM 输出改写。

留痕口径（kb_search_log.rewrite_actions 数据源）：actions 为字符串列表，
格式 correct:{误}->{正} / alias:{口语}->{标准SKU} / intent:{口语}->{标准词} /
llm:{规则文}->{合并文}，按规则执行顺序追加，可重放审计（§6.5 改写命中率观测）。

偏差标注：SR-KB-015 §6.2 的「规则零命中才进 LLM」启发式（_need_llm）暂未启用
——本迭代 LLM 参与与否由「钩子已注入 + 预算允许」决定，接统一 LLM 网关
（app/core/llm/gateway.py，后续迭代）时再按 §6.2 开启启发式。
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .base import maybe_await

# LLM 改写系统提示词（SR-KB-015 §6.2 逐字）：只改写、不回答，保真优先
REWRITE_LLM_SYSTEM_PROMPT = (
    "你是外贸企业知识库的查询改写器，只改写、不回答。将口语化查询改写为检索友好的关键词串："
    "1) 纠错：修正明显错别字与错误单位；"
    "2) 扩展：补充同义词与标准术语（如「能扛多重」→「最大承载 load capacity」）；"
    "3) 意图对齐：映射到标准维度词（询价/报价/认证/交期/MOQ/物流/投诉…）；"
    "4) 保真：SKU、型号、数字、单位、认证名一律原样保留，禁止编造未出现的实体。"
    "只输出一行改写结果。"
)

# LLM 钩子形态：sync 或 async 均可（maybe_await 兼容）；入参为规则改写后的查询文本
LlmHook = Callable[[str], "str | Awaitable[str]"]

# 意图对齐模板：pattern（正则，IGNORECASE）→ 标准检索词 + 槽位键值
IntentTemplate = tuple[str, str, Mapping[str, str]]

# 默认意图对齐模板（SRS §5.3「意图对齐」；标准维度词对齐 05-核心模块详细设计 §3.1）
DEFAULT_INTENT_TEMPLATES: tuple[IntentTemplate, ...] = (
    (r"能扛多重|能承重|承重多少|最大载荷|load capacity", "最大承载 load capacity",
     {"intent": "load_capacity"}),
    (r"多少钱|价格|报价|quotation|quote", "报价 price quote", {"intent": "price"}),
    (r"认证|certificate|certification", "认证 certification", {"intent": "certification"}),
    (r"交期|交货期|多久到货|多久发货|lead ?time|delivery", "交期 lead time", {"intent": "lead_time"}),
    (r"起订量|最小起订|最少订|moq", "起订量 MOQ minimum order quantity", {"intent": "moq"}),
    (r"运费|物流|发货方式|shipping|freight", "物流 shipping freight", {"intent": "shipping"}),
    (r"投诉|售后|质量反馈|complaint|after.?sales", "投诉 after-sales complaint", {"intent": "after_sales"}),
)

# 默认纠错词典（错拼/易混词；长词优先替换，NFR-MAI-02 配置化由装配层注入覆盖）
DEFAULT_CORRECTIONS: Mapping[str, str] = {
    "帐号": "账号",
    "登陆": "登录",
    "stabilzer": "stabilizer",
    "gimble": "gimbal",
}

# 槽位保真数字提取：整数/小数（含型号与数量，LLM 合并时不允许丢失）
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")

# 改写来源标记（RewriteResult.source 取值口径）
SOURCE_RULE = "rule"
SOURCE_RULE_LLM = "rule+llm"
SOURCE_RULE_LLM_TIMEOUT = "rule(llm_timeout)"
SOURCE_RULE_LLM_ERROR = "rule(llm_error)"
SOURCE_RULE_BUDGET_SKIP = "rule(budget_skip)"


@dataclass
class RewriteResult:
    """改写段输出（SR-KB-015 §5）：rewritten_query + 命中规则留痕 + 意图槽位。

    slots 供召回段结构化路使用：sku（别名命中的标准 SKU 列表）与 intent
    （意图名）；键集受召回段 expr 字段白名单约束，未知槽位被忽略。
    """

    rewritten_query: str
    actions: list[str] = field(default_factory=list)
    slots: dict[str, Any] = field(default_factory=dict)
    source: str = SOURCE_RULE


class QueryRewriter:
    """查询改写器：规则层常驻，LLM 钩子可选（None 时仅规则）。"""

    def __init__(
        self,
        *,
        corrections: Mapping[str, str] | None = None,
        sku_aliases: Mapping[str, str] | None = None,
        intent_templates: Sequence[IntentTemplate] | None = None,
        llm_hook: LlmHook | None = None,
        llm_timeout_ms: float = 115.0,
    ) -> None:
        """构造改写器；None 采用默认表，空映射/空序列表示显式清空。

        llm_timeout_ms：LLM 快速通道硬超时（判定点 A，KB_SEARCH_LLM_TIMEOUT_MS）。
        """
        self._corrections: dict[str, str] = dict(DEFAULT_CORRECTIONS if corrections is None else corrections)
        self._aliases: dict[str, str] = dict(sku_aliases or {})
        self._intents: list[CompiledIntent] = _compile_intents(
            intent_templates if intent_templates is not None else DEFAULT_INTENT_TEMPLATES
        )
        self._llm_hook = llm_hook
        self._llm_timeout_s = llm_timeout_ms / 1000.0

    # ---- 注册接口（别名/纠错/意图模板免改码扩展，NFR-MAI-02）----

    @property
    def has_llm(self) -> bool:
        """是否注入了 LLM 钩子。"""
        return self._llm_hook is not None

    def register_alias(self, alias: str, canonical_sku: str) -> None:
        """注册单条 SKU 别名（对应 sku_alias 表 upsert，FR-KB-020）。"""
        self._aliases[alias] = canonical_sku

    def register_aliases(self, aliases: Mapping[str, str]) -> None:
        """批量注册 SKU 别名。"""
        self._aliases.update(aliases)

    def register_correction(self, wrong: str, right: str) -> None:
        """注册单条纠错映射。"""
        self._corrections[wrong] = right

    def register_intent_template(self, pattern: str, standard: str, slot: Mapping[str, str]) -> None:
        """注册单条意图对齐模板（追加在默认表之后）。"""
        self._intents.extend(_compile_intents([(pattern, standard, slot)]))

    # ---- 主入口 ----

    async def rewrite(
        self,
        query: str,
        kb_types: Sequence[str] | None = None,
        *,
        use_llm: bool = True,
    ) -> RewriteResult:
        """执行三步规则改写；钩子存在且 use_llm 时叠加 LLM 快速通道。

        use_llm=False 为预算守卫降级入口（判定点 A2：剩余预算不足时流水线
        传入 False，source 记 budget_skip，钩子不被调用）。
        """
        text = query.strip()
        actions: list[str] = []
        slots: dict[str, Any] = {}

        # 规则① 纠错：NFKC 全半角规整 + 错拼词典（长词优先防子串误替换）
        text = self._apply_corrections(text, actions)
        # 规则② SKU 别名扩展：口语/错拼 → 标准 SKU，命中即入结构化槽位
        text, hit_skus = self._expand_alias(text, actions)
        if hit_skus:
            slots["sku"] = hit_skus
        # 规则③ 意图对齐：口语问句 → 标准维度词 + 意图槽位
        text, intent_slots = self._align_intent(text, actions)
        for key, value in intent_slots.items():
            slots.setdefault(key, value)

        source = SOURCE_RULE
        if self._llm_hook is None or not use_llm:
            # 仅规则路径：钩子缺省=能力未接入；use_llm=False=预算降级（A2）
            source = SOURCE_RULE if self._llm_hook is None else SOURCE_RULE_BUDGET_SKIP
            return RewriteResult(text, actions, slots, source)

        # LLM 快速通道：硬超时（判定点 A），超时/异常降级规则结果直出
        try:
            llm_text = str(await asyncio.wait_for(
                maybe_await(self._llm_hook(text)), timeout=self._llm_timeout_s,
            )).strip()
        except TimeoutError:
            return RewriteResult(text, actions, slots, SOURCE_RULE_LLM_TIMEOUT)
        except Exception:  # noqa: BLE001 —— LLM 网关任意异常都不得阻塞检索链路
            return RewriteResult(text, actions, slots, SOURCE_RULE_LLM_ERROR)
        if not llm_text or llm_text == text:
            return RewriteResult(text, actions, slots, SOURCE_RULE_LLM)

        merged = self._merge_keep_slots(text, llm_text, slots)
        if merged != text:
            actions.append(f"llm:{text}->{merged}")
        return RewriteResult(merged, actions, slots, SOURCE_RULE_LLM)

    async def rewrite_rules_only(
        self, query: str, kb_types: Sequence[str] | None = None,
    ) -> RewriteResult:
        """仅规则改写的语义别名（等价 use_llm=False）。"""
        return await self.rewrite(query, kb_types, use_llm=False)

    # ---- 规则实现 ----

    def _apply_corrections(self, text: str, actions: list[str]) -> str:
        """规则① 纠错：NFKC 规整（留痕一次）+ 词典替换（长词优先，逐词留痕）。"""
        normalized = unicodedata.normalize("NFKC", text)
        if normalized != text:
            actions.append(f"correct:{text}->{normalized}")
            text = normalized
        for wrong in sorted(self._corrections, key=len, reverse=True):
            right = self._corrections[wrong]
            text, replaced = re.subn(re.escape(wrong), lambda _m, r=right: r, text, flags=re.IGNORECASE)
            if replaced:
                actions.append(f"correct:{wrong}->{right}")
        return text

    def _expand_alias(self, text: str, actions: list[str]) -> tuple[str, list[str]]:
        """规则② 别名扩展：长别名优先、大小写不敏感；返回改写文本与命中标准 SKU。"""
        hit_skus: list[str] = []
        for alias in sorted(self._aliases, key=len, reverse=True):
            canonical = self._aliases[alias]
            text, replaced = re.subn(re.escape(alias), lambda _m, c=canonical: c, text, flags=re.IGNORECASE)
            if replaced:
                actions.append(f"alias:{alias}->{canonical}")
                if canonical not in hit_skus:
                    hit_skus.append(canonical)
        return text, hit_skus

    def _align_intent(self, text: str, actions: list[str]) -> tuple[str, dict[str, Any]]:
        """规则③ 意图对齐：命中即记录槽位；仅追加缺失的标准词（幂等不堆叠）。

        口语原词保留（稀疏路仍可命中），标准维度词补齐检索友好形态；
        逐词判缺可保证重复执行/已对齐查询不产生堆叠。
        """
        slots: dict[str, Any] = {}
        for pattern, standard, slot in self._intents:
            matched = pattern.search(text)
            if matched is None:
                continue
            actions.append(f"intent:{matched.group(0)}->{standard}")
            for key, value in slot.items():
                slots.setdefault(key, value)
            missing = [term for term in standard.split() if len(term) >= 2 and term not in text]
            if missing:
                text = f"{text} {' '.join(missing)}".strip()
        return text, slots

    def _merge_keep_slots(self, rule_text: str, llm_text: str, slots: Mapping[str, Any]) -> str:
        """保真合并（SR-KB-015 §6.2）：以 LLM 输出为底，规则侧 SKU/数字槽位不可丢。

        LLM 改动/丢失的 SKU 与数字（保真红线第 4 条）按长度降序逐一检查补回，
        即时追加即时判重——长槽位先补（如 ST-3200），其内含数字（3200）因
        子串在场不再重复补。
        """
        protected = [str(value) for value in (slots.get("sku") or [])]
        protected.extend(_NUMBER_PATTERN.findall(rule_text))
        for value in sorted(dict.fromkeys(protected), key=len, reverse=True):
            if value not in llm_text:
                llm_text = f"{llm_text} {value}"
        return llm_text.strip()


# 预编译后的意图模板形态
CompiledIntent = tuple[re.Pattern[str], str, Mapping[str, str]]


def _compile_intents(templates: Sequence[IntentTemplate]) -> list[CompiledIntent]:
    """预编译意图模板正则（IGNORECASE；非法正则立即失败属配置错误）。"""
    return [(re.compile(p, re.IGNORECASE), standard, dict(slot)) for p, standard, slot in templates]
