# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""LLM 客户端层（FR-TR-001，SR-TR-001 §6.2 生成侧；NFR-SEC-06 数据边界）。

三个实现围绕同一个可插拔协议 :class:`LlmClient`：

- :class:`DeepSeekLlmClient`——生产形态（RFP 指定技术栈 DeepSeek-V3）。OpenAI
  兼容 ``/chat/completions`` 端点的 SSE 流式调用；api_key/base_url 一律读环境
  变量（KB_DEEPSEEK_API_KEY / KB_DEEPSEEK_BASE_URL），未配置时给出中文报错；
  **发起连接前必须经 foundation 出域白名单断言**（NFR-SEC-06 fail-closed，
  白名单外 / OI-09 未确认即拒，无「先发后审」路径）。本仓库为 demo 环境
  （无真实 API Key），该客户端代码写全但默认不会被触发；
- :class:`FakeTutorLlm`——确定性教学桩（demo/测试）：从消息序列中的
  【知识资料】编号条目生成教学化回答，每个事实句携带 [来源N] 引用标记，
  并以苏格拉底式追问收束；不发起任何网络请求。

协议签名（与 pipeline.py 的调用形态一致）：
    generate_stream(messages, *, system) -> Iterator[str]
    messages: [{"role": "user"|"assistant"|"system", "content": str}, ...]
    system:   教学化 System Prompt（SR-TR-001 §6.2，单独参数承载）
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Protocol

from foundation.net.egress import EgressGuard

# --------------------------------------------------------------------------- 常量与环境变量

#: DeepSeek API Key 环境变量（demo 环境未配置即视为不可用，调用时中文报错）
ENV_API_KEY: str = "KB_DEEPSEEK_API_KEY"
#: DeepSeek 服务入口环境变量（OpenAI 兼容端点；缺省官方域名）
ENV_BASE_URL: str = "KB_DEEPSEEK_BASE_URL"
#: 缺省服务入口（04-接口设计 §4：DeepSeek-V3 官方 OpenAI 兼容端点）
DEFAULT_BASE_URL: str = "https://api.deepseek.com"
#: chat completions 端点路径（OpenAI 兼容）
CHAT_COMPLETIONS_PATH: str = "/chat/completions"
#: 出域用途编码（foundation 出域目的地注册表 purposes 口径，llm_chat）
EGRESS_PURPOSE_LLM_CHAT: str = "llm_chat"
#: 出域调用方标识（egress_call_log 留痕 caller 字段）
EGRESS_CALLER: str = "training.tutor"


class LlmConfigError(RuntimeError):
    """LLM 配置缺失（如未配置 API Key）：调用前即失败，不属于下游故障。"""


class LlmDownstreamError(RuntimeError):
    """LLM 下游故障（HTTP 非 200 / 流中断 / 响应不可解析）：上层映射 50002。"""


# --------------------------------------------------------------------------- 可插拔协议


class LlmClient(Protocol):
    """LLM 客户端协议（可插拔；pipeline 仅依赖本协议，不绑定具体实现）。

    SR-TR-001 §2 架构规则：tutor 不直连模型 API，生产经统一出口注入本协议
    实现（DeepSeek），demo/测试注入确定性 Fake 实现。
    """

    def generate_stream(
        self, messages: Sequence[Mapping[str, Any]], *, system: str
    ) -> Iterator[str]:
        """流式生成：逐段产出回答文本片段（token/短句块），聚合即为完整回答。"""
        ...


# --------------------------------------------------------------------------- DeepSeek 真实客户端


class DeepSeekLlmClient:
    """DeepSeek-V3 流式客户端（OpenAI 兼容 SSE；demo 环境代码写全、按需触发）。

    - api_key / base_url 解析顺序：构造参数 → 环境变量 →（base_url）缺省值；
    - 未配置 api_key 时构造不报错（便于应用工厂先行装配），但
      :meth:`generate_stream` 首次取流即抛 :class:`LlmConfigError`（中文提示）；
    - 每次请求前经 ``guard.assert_egress`` 出域断言（NFR-SEC-06）：guard 未注入
      时使用空注册表守卫（fail-closed，全部拒绝）——生产必须注入已注册
      ``api.deepseek.com``（purposes 含 llm_chat、OI-09 已确认）的守卫实例；
    - transport 可注入（httpx.MockTransport），单测离线验证 SSE 解析。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        model: str = "deepseek-v3",
        guard: EgressGuard | None = None,
        timeout_s: float = 30.0,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        transport: Any | None = None,
    ) -> None:
        import os

        self._api_key = api_key if api_key is not None else os.environ.get(ENV_API_KEY, "")
        self._base_url = (
            base_url
            if base_url is not None
            else os.environ.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL
        ).rstrip("/")
        self._model = model
        # 未注入守卫时使用空注册表守卫：fail-closed，未注册目的地一律拒绝出域
        self._guard = guard if guard is not None else EgressGuard()
        self._timeout_s = timeout_s
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._transport = transport

    @property
    def endpoint(self) -> str:
        """完整端点 URL（出域断言与请求的目标）。"""
        return f"{self._base_url}{CHAT_COMPLETIONS_PATH}"

    def generate_stream(
        self, messages: Sequence[Mapping[str, Any]], *, system: str
    ) -> Iterator[str]:
        """流式生成（SSE）：逐段 yield delta 文本；出域断言失败/下游故障即抛错。"""
        import httpx

        if not self._api_key:
            raise LlmConfigError(
                f"未配置环境变量 {ENV_API_KEY}，无法调用 DeepSeek-V3；"
                "demo 环境请改用 FakeTutorLlm，生产环境由运维注入密钥后再启用真实调用"
            )
        chat_messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for message in messages:
            chat_messages.append(
                {"role": str(message.get("role", "user")), "content": str(message.get("content", ""))}
            )
        payload: dict[str, Any] = {
            "model": self._model,
            "stream": True,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "messages": chat_messages,
        }
        url = self.endpoint
        # ── 出域白名单断言（NFR-SEC-06 fail-closed；拦截前守卫已留痕）──
        self._guard.assert_egress(
            url,
            EGRESS_PURPOSE_LLM_CHAT,
            content=json.dumps(payload, ensure_ascii=False),
            caller=EGRESS_CALLER,
        )
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "text/event-stream"}
        with httpx.Client(timeout=self._timeout_s, transport=self._transport) as client:
            with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    raise LlmDownstreamError(
                        f"DeepSeek 接口返回 HTTP {response.status_code}，本轮生成失败"
                    )
                yield from self._iter_deltas(response)

    @staticmethod
    def _iter_deltas(response: Any) -> Iterator[str]:
        """解析 SSE 行流：``data: {json}`` → choices[0].delta.content；[DONE] 终止。"""
        for line in response.iter_lines():
            text = line.strip() if isinstance(line, str) else line.decode("utf-8", "replace").strip()
            if not text or not text.startswith("data:"):
                continue
            data = text[len("data:"):].strip()
            if data == "[DONE]":
                return
            try:
                chunk = json.loads(data)
                content = chunk["choices"][0].get("delta", {}).get("content", "")
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                raise LlmDownstreamError(f"DeepSeek SSE 响应不可解析：{exc}") from exc
            if content:
                yield content


# --------------------------------------------------------------------------- 确定性教学桩（demo/测试）

#: 知识条目标记行（与 pipeline._knowledge_block 的下发格式同源）：
#: [来源N] 标题（知识库：xx，相关度：0.87）\n 正文
SOURCE_HEADER_RE: re.Pattern[str] = re.compile(r"^\[来源(\d+)\]\s*(.+)$", re.MULTILINE)
#: 单块正文最大取用长度（演示桩的确定性截断，防长块撑爆回答）
_BLOCK_TEXT_MAX: int = 90
#: 单块正文取用前的分句切分符（中文句号）
_SENTENCE_SPLIT: str = "。"
#: 模拟流式切分的块大小（字符）
_STREAM_CHUNK_SIZE: int = 16


class FakeTutorLlm:
    """确定性教学桩：基于检索上下文生成教学化回答（demo 口径，零网络请求）。

    生成规则（保证「零编造 + 可溯源」的确定性演示）：
    - 从 messages 中的【知识资料】区解析 [来源N] 编号条目（标题/正文）；
    - 逐条摘取正文首个完整句（截断到 90 字）作为事实句，句尾携带 [来源N]；
    - 开头点拨思考方向、结尾苏格拉底式追问 + 深入学习建议（FR-KB-006 引导）；
    - 以固定 16 字符块模拟流式 token 输出。

    记录 ``calls``（调用次数）与 ``last_messages``（最近一次入参快照），
    供测试断言「零命中不调 LLM」与消息组装形态。
    """

    def __init__(self) -> None:
        self.calls: int = 0
        self.last_messages: list[dict[str, Any]] = []

    def generate_stream(
        self, messages: Sequence[Mapping[str, Any]], *, system: str
    ) -> Iterator[str]:
        """确定性生成并按固定块大小流式产出（入参快照留痕供测试断言）。"""
        self.calls += 1
        self.last_messages = [dict(m) for m in messages]
        blocks = self.parse_knowledge_blocks(messages)
        answer = self.compose_answer(blocks, system=system)
        for i in range(0, len(answer), _STREAM_CHUNK_SIZE):
            yield answer[i : i + _STREAM_CHUNK_SIZE]

    # --------------------------------------------------------------- 解析与组答（纯函数，可单测）

    @staticmethod
    def parse_knowledge_blocks(
        messages: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """从消息序列解析【知识资料】编号条目 → [{no, title, text}]。

        格式契约（pipeline._knowledge_block 下发）：每个条目一行
        ``[来源N] 标题（…）``，其后紧跟正文行。无知识条目时返回空列表
        （零命中路径本就不应调 LLM，此为防御性兜底）。
        """
        blocks: list[dict[str, Any]] = []
        for message in messages:
            content = str(message.get("content", ""))
            if "【知识资料】" not in content:
                continue
            lines = content.splitlines()
            current: dict[str, Any] | None = None
            body: list[str] = []
            for line in lines:
                matched = SOURCE_HEADER_RE.match(line.strip())
                if matched:
                    if current is not None:
                        current["text"] = " ".join(body).strip()
                        blocks.append(current)
                    current = {"no": int(matched.group(1)), "title": matched.group(2).strip()}
                    body = []
                elif current is not None and line.strip():
                    body.append(line.strip())
            if current is not None:
                current["text"] = " ".join(body).strip()
                blocks.append(current)
        return blocks

    @classmethod
    def compose_answer(cls, blocks: list[dict[str, Any]], *, system: str = "") -> str:
        """按教学化模板组装回答：每个事实句带 [来源N]，结尾苏格拉底式追问。

        模板本身不含任何事实性内容（遵守 System Prompt 禁编造指令）；
        全部事实句逐字取自检索块正文（可溯源），最多引用前三块。
        """
        if not blocks:
            # 防御分支：无知识条目时只说「暂无」，绝不编造
            return "知识库暂无该内容，已记录。这个问题我暂时没有可靠依据，不能编造答案。"
        cited = blocks[:3]
        fact_lines: list[str] = []
        for block in cited:
            sentence = cls._first_sentence(str(block.get("text", "")))
            fact_lines.append(f"· {sentence} [来源{block['no']}]")
        top_title = str(cited[0].get("title", "相关资料"))
        top_no = cited[0]["no"]
        parts: list[str] = [
            "这一问很关键，我们结合知识库资料来梳理思路。",
            "",
            "先看知识库里的依据：",
            *fact_lines,
            "",
            f"建议你先读一读《{top_title.split('（')[0]}》 [来源{top_no}]，边读边对照上面的要点。",
            "",
            "再往深一层想：如果把这个问题放到真实客户的询盘场景里，你会先向客户确认哪一个前提条件？"
            "想清楚这一点，答案就浮出来了（可以先在带教素材库里找同类案例对照演练）。",
        ]
        return "\n".join(parts)

    @staticmethod
    def _first_sentence(text: str) -> str:
        """取正文首个完整句（无句号则截断到上限长度），并补回句读。"""
        cleaned = text.strip()
        if not cleaned:
            return "（该知识块正文为空，请直接查阅原文）"
        head = cleaned.split(_SENTENCE_SPLIT, 1)[0]
        if len(head) > _BLOCK_TEXT_MAX:
            head = head[:_BLOCK_TEXT_MAX]
        return head + ("。" if not head.endswith(_SENTENCE_SPLIT) else "")


__all__ = [
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "DEFAULT_BASE_URL",
    "EGRESS_PURPOSE_LLM_CHAT",
    "LlmConfigError",
    "LlmDownstreamError",
    "LlmClient",
    "DeepSeekLlmClient",
    "FakeTutorLlm",
]
