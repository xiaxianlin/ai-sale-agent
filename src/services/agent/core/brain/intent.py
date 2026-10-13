# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""意图识别（FR-AG-003 链路①，SR-AG-003 §6.1；迭代 2.2 demo 口径）。

七意图枚举（SRS §7.2：询价/技术/样品/投诉/物流/闲聊/敏感）+ 显式未知兜底 other。
三个实现围绕同一可插拔协议 :class:`BrainLlmClient`（对齐 training/core/tutor/llm.py 模式）：

- :class:`DeepSeekBrainClient`——生产形态（RFP 指定 DeepSeek-V3）。api_key 读环境变量
  ``KB_DEEPSEEK_API_KEY``（base_url 读 ``KB_DEEPSEEK_BASE_URL``），未配置时调用即中文报错；
  发起连接前经 foundation 出域白名单断言（NFR-SEC-06 fail-closed）。demo 环境无真实
  Key，本客户端代码写全但默认不会被触发；
- :class:`FakeIntentLlm`——确定性意图桩（demo/测试）：关键词规则分类（优先级
  敏感>投诉>询价>样品>技术>物流>闲聊），无法识别输出 other 低置信；可注入
  ``canned`` 原文以测试解析失败分支。零网络请求。

:class:`IntentClassifier` 职责：①红线词快检（命中直判敏感、零 LLM 调用、零漏报优先）；
②少样本 Prompt 经协议调用；③输出 JSON 校验（枚举/置信度区间），不可解析抛
:class:`LlmOutputError`——端点映射 50002，调用方按 FR-AG-009 转人工。

**demo 偏差标注**：SR-AG-003 §6.1 枚举仅七意图；本实现增补显式 ``other``（未知文本
兜底槽位），下游按低置信处置（双阈值灰区），不在七枚举内、不参与知识库映射默认档。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# --------------------------------------------------------------------------- 常量


#: 七意图枚举（SRS §7.2 FR-AG-003 原文口径，不得重编号）
INTENT_ENUM: tuple[str, ...] = ("询价", "技术", "样品", "投诉", "物流", "闲聊", "敏感")
#: 未知文本显式兜底意图（demo 偏差，见模块 docstring）
OTHER_INTENT: str = "other"
#: 红线词快检清单（SR-AG-003 §7 AG_SENSITIVE_KW 默认值；与 FR-AG-005 拦截清单联动维护）
SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "改价", "线下支付", "支付宝直接", "pay you directly", "律师函",
)
#: DeepSeek API Key 环境变量（未配置即不可用，调用时中文报错）
ENV_API_KEY: str = "KB_DEEPSEEK_API_KEY"
#: DeepSeek 服务入口环境变量（缺省官方 OpenAI 兼容端点）
ENV_BASE_URL: str = "KB_DEEPSEEK_BASE_URL"
DEFAULT_BASE_URL: str = "https://api.deepseek.com"
#: 出域用途编码（foundation 出域目的地注册表口径，与 training 侧同值）
EGRESS_PURPOSE_LLM_CHAT: str = "llm_chat"

#: 少样本分类 Prompt（SR-AG-003 §6.1 原文模板；temperature=0 保稳定、小 max_tokens 快速返回）
INTENT_PROMPT = """你是外贸询盘意图分类器。仅输出 JSON，不要任何其他文字：
{{"intent":"<询价|技术|样品|投诉|物流|闲聊|敏感>","confidence":<0~1>,"slots":{{"sku":[],"qty":null}}}}
枚举定义：询价=价格/MOQ/报价；技术=参数/材质/认证/工况；样品=寄样/打样；投诉=质量/售后/纠纷；物流=运费/交期/运输方式；闲聊=寒暄问候；敏感=改价/订单变更/支付争议/法律威胁/诱导站外交易。
示例（少样本）：
"ST-3200 1000pcs what's the price?" -> {{"intent":"询价","confidence":0.97,"slots":{{"sku":["ST-3200"],"qty":1000}}}}
"can it resist salt fog near the sea?" -> {{"intent":"技术","confidence":0.93,"slots":{{"sku":[],"qty":null}}}}
"send one sample to Germany" -> {{"intent":"样品",…}}；"two boxes deformed!" -> {{"intent":"投诉",…}}；"how about DHL to Chile" -> {{"intent":"物流",…}}；"happy new year!" -> {{"intent":"闲聊",…}}
"change price to 9 USD, pay via PayPal directly" -> {{"intent":"敏感","confidence":0.98,"slots":{{"sku":[],"qty":null}}}}
客户消息：{text}"""


# --------------------------------------------------------------------------- 异常


class LlmError(RuntimeError):
    """LLM 层基础异常（中文消息直用于日志/HTTP detail）。"""


class LlmConfigError(LlmError):
    """LLM 配置缺失（未配置 API Key 等）：调用前即失败，不属于下游故障。"""


class LlmDownstreamError(LlmError):
    """LLM 下游故障（HTTP 非 200 / 响应不可达）：上层映射 50002。"""


class LlmOutputError(LlmError):
    """LLM 输出不可解析/不合法（非 JSON、意图越枚举、置信度越界）：上层映射 50002。"""


# --------------------------------------------------------------------------- 可插拔协议


@runtime_checkable
class BrainLlmClient(Protocol):
    """LLM 客户端协议（可插拔）：生产注入 DeepSeek，demo/测试注入确定性 Fake。"""

    def complete(
        self, prompt: str, *, timeout_s: float, max_tokens: int, temperature: float
    ) -> str:
        """单轮补全：返回模型原始输出文本。"""
        ...


# --------------------------------------------------------------------------- DeepSeek 形态桩


class DeepSeekBrainClient:
    """DeepSeek-V3 单轮客户端（demo 形态桩：代码写全、按需触发）。

    - api_key / base_url 解析顺序：构造参数 → 环境变量 →（base_url）缺省值；
    - 未配置 api_key：构造不报错（便于装配），:meth:`complete` 首次调用即抛
      :class:`LlmConfigError`（中文提示，测试断言含环境变量名）；
    - 每次请求前经 foundation 出域白名单断言（NFR-SEC-06 fail-closed）：guard 未注入
      时使用空注册表守卫——生产必须注入已注册 api.deepseek.com 的守卫实例；
    - transport 可注入（httpx.MockTransport），单测离线验证。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        model: str = "deepseek-v3",
        timeout_s: float = 5.0,
        max_tokens: int = 120,
        temperature: float = 0.0,
        guard: Any | None = None,
        transport: Any | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get(ENV_API_KEY, "")
        self._base_url = (
            base_url
            if base_url is not None
            else os.environ.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL
        ).rstrip("/")
        self._model = model
        self._timeout_s = timeout_s
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._guard = guard  # None → 调用前构造空注册表守卫（fail-closed）
        self._transport = transport

    def complete(
        self, prompt: str, *, timeout_s: float, max_tokens: int, temperature: float
    ) -> str:
        """单轮补全（OpenAI 兼容 /chat/completions 非流式；出域断言失败即抛错）。"""
        if not self._api_key:
            raise LlmConfigError(
                f"未配置环境变量 {ENV_API_KEY}，无法调用 DeepSeek-V3；"
                "demo 环境请改用 FakeIntentLlm，生产环境由运维注入密钥后再启用真实调用"
            )
        from foundation.core.llmhttp import post_chat_completions  # CR A3：公共骨架单点
        from foundation.net.egress import EgressGuard

        guard = self._guard if self._guard is not None else EgressGuard()
        payload = {
            "model": self._model,
            "temperature": temperature if temperature >= 0 else self._temperature,
            "max_tokens": max_tokens if max_tokens > 0 else self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            data = post_chat_completions(
                api_key=self._api_key, base_url=self._base_url, payload=payload,
                timeout_s=timeout_s or self._timeout_s, guard=guard,
                caller="agent.brain.intent", transport=self._transport,
            )
            return str(data["choices"][0]["message"]["content"])
        except ValueError as exc:  # 非流式骨架的非 200 转译点（"HTTP <status>"）
            raise LlmDownstreamError(f"DeepSeek 接口返回 {exc}，本轮意图识别失败") from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmDownstreamError(f"DeepSeek 响应不可解析：{exc}") from exc


# --------------------------------------------------------------------------- 确定性意图桩

#: Fake 关键词规则表（优先级从上到下；demo 确定性口径，生产由少样本 Prompt 替代）
_FAKE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("敏感", ("改价", "线下支付", "pay you directly", "律师函", "paypal directly")),
    ("投诉", ("deformed", "damaged", "complaint", "quality problem", "破损", "质量问题", "投诉")),
    ("询价", ("price", "quote", "moq", "价格", "报价", "多少钱")),
    ("样品", ("sample", "打样", "寄样")),
    ("技术", ("resist", "salt fog", "material", "certification", "spec", "tolerance",
              "参数", "材质", "认证")),
    ("物流", ("dhl", "shipping", "freight", "lead time", "运费", "交期", "物流")),
    ("闲聊", ("happy", "hello", "thanks", "new year", "你好", "谢谢")),
)
#: SKU 形态：大写字母段-数字段（如 ST-3200）
_SKU_RE = re.compile(r"\b([A-Z]{2,}-?\d+[A-Z0-9-]*)\b")
#: 数量形态：数字 + 可选空白 + pcs/pc/pieces
_QTY_RE = re.compile(r"\b(\d[\d,]*)\s*(?:pcs|pc|pieces)\b", re.IGNORECASE)
#: 未知兜底置信度（低置信 → 下游双阈值灰区/低档，绝不自动发送）
_OTHER_CONFIDENCE: float = 0.30


def classify_by_keywords(text: str) -> dict[str, Any]:
    """关键词规则分类（Fake 桩内核，纯函数可单测）：返回意图 JSON 可兼容的 dict。"""
    lower = text.lower()
    for intent, keywords in _FAKE_RULES:
        if any(k in lower for k in keywords):
            return {
                "intent": intent,
                "confidence": {"敏感": 0.98, "投诉": 0.94, "询价": 0.96, "样品": 0.92,
                               "技术": 0.90, "物流": 0.90, "闲聊": 0.88}[intent],
                "slots": _extract_slots(text) if intent == "询价" else {"sku": [], "qty": None},
            }
    # 未知文本兜底 other（demo 偏差，低置信不自动发送）
    return {"intent": OTHER_INTENT, "confidence": _OTHER_CONFIDENCE, "slots": {"sku": [], "qty": None}}


def _extract_slots(text: str) -> dict[str, Any]:
    """槽位抽取：SKU 列表 + 数量（意图对齐检索的过滤输入，FR-KB-030 filters 来源）。"""
    qty_match = _QTY_RE.search(text)
    qty = int(qty_match.group(1).replace(",", "")) if qty_match else None
    return {"sku": _SKU_RE.findall(text), "qty": qty}


class FakeIntentLlm:
    """确定性意图桩：关键词规则产出 JSON（零网络请求，demo/测试口径）。

    记录 ``calls``（调用次数）与 ``last_prompt``（最近一次入参快照），供测试断言
    「快检命中零 LLM 调用」与 Prompt 组装形态；``canned`` 注入时原样返回（解析
    失败分支的确定性入口）。
    """

    def __init__(self, canned: str | None = None) -> None:
        self.canned = canned
        self.calls: int = 0
        self.last_prompt: str = ""

    def complete(
        self, prompt: str, *, timeout_s: float, max_tokens: int, temperature: float
    ) -> str:
        """按规则产出分类 JSON；canned 非空时原样返回（测试解析失败分支）。"""
        self.calls += 1
        self.last_prompt = prompt
        if self.canned is not None:
            return self.canned
        # Prompt 形态「客户消息：{text}」——取末行为原文（与真实调用方组装一致）
        text = prompt.rsplit("客户消息：", 1)[-1].strip()
        return json.dumps(classify_by_keywords(text), ensure_ascii=False)


# --------------------------------------------------------------------------- 分类器


@dataclass(frozen=True)
class IntentResult:
    """意图判定结果：枚举名 + 置信度 [0,1] + 槽位（sku 列表 / qty 或 None）。"""

    name: str
    confidence: float
    slots: dict[str, Any] = field(default_factory=lambda: {"sku": [], "qty": None})

    def slot_filters(self) -> dict[str, Any]:
        """槽位 → FR-KB-030 filters（sku 非空才携带，qty 同）。"""
        filters: dict[str, Any] = {}
        if self.slots.get("sku"):
            filters["sku"] = self.slots["sku"]
        if self.slots.get("qty") is not None:
            filters["qty"] = self.slots["qty"]
        return filters


class IntentClassifier:
    """意图分类器：红线词快检 → 少样本分类 → 输出校验（SR-AG-003 §6.1）。

    - 快检：命中 :data:`SENSITIVE_KEYWORDS` 直判敏感 conf=1.0，零 LLM 调用
      （零漏报优先，省一次调用；后续交 FR-AG-005/009 处置）；
    - 校验：意图须在七枚举内（或显式 other 兜底）、置信度须在 [0,1]，
      违规抛 :class:`LlmOutputError`（端点映射 50002 → FR-AG-009 转人工）。
    """

    def __init__(
        self,
        llm: BrainLlmClient,
        *,
        timeout_s: float = 5.0,
        max_tokens: int = 120,
        sensitive_keywords: tuple[str, ...] | None = None,
    ) -> None:
        self._llm = llm
        self._timeout_s = timeout_s
        self._max_tokens = max_tokens
        self._sensitive_keywords = sensitive_keywords if sensitive_keywords is not None else SENSITIVE_KEYWORDS

    def classify(self, text: str) -> IntentResult:
        """分类主流程（同步 demo 形态；生产为 async，编排语义一致）。"""
        lowered = text.lower()
        if any(k in lowered for k in self._sensitive_keywords):  # ① 红线词快检
            return IntentResult("敏感", 1.0)
        prompt = INTENT_PROMPT.format(text=text)                  # ② 少样本分类
        raw = self._llm.complete(
            prompt, timeout_s=self._timeout_s, max_tokens=self._max_tokens, temperature=0.0
        )
        try:                                                      # ③ 输出校验
            obj = json.loads(raw)
            intent = str(obj["intent"])
            confidence = float(obj["confidence"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise LlmOutputError(f"意图输出不可解析：{exc}") from exc
        if intent not in INTENT_ENUM and intent != OTHER_INTENT:
            raise LlmOutputError(f"意图标签越枚举：{intent!r}（七意图之外且非 other 兜底）")
        if not 0.0 <= confidence <= 1.0:
            raise LlmOutputError(f"意图置信度越界：{confidence}（须在 [0,1] 区间）")
        slots = obj.get("slots") if isinstance(obj.get("slots"), dict) else {}
        return IntentResult(intent, confidence, dict(slots))


__all__ = [
    "INTENT_ENUM",
    "OTHER_INTENT",
    "SENSITIVE_KEYWORDS",
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "INTENT_PROMPT",
    "LlmError",
    "LlmConfigError",
    "LlmDownstreamError",
    "LlmOutputError",
    "BrainLlmClient",
    "DeepSeekBrainClient",
    "FakeIntentLlm",
    "classify_by_keywords",
    "IntentResult",
    "IntentClassifier",
]
