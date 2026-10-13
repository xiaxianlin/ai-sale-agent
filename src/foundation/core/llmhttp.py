# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""DeepSeek HTTP 骨架（CR A3 修复：三套手写客户端的公共部分收敛单点）。

收敛范围（保守路径）：配置解析（参数→env→缺省）+ 出域断言 + 非流式
``/chat/completions`` POST——drill 评分与 brain 意图分类两处委托本模块；
tutor 的 SSE 流式调用仅一处使用、无重复，维持域内实现。

各域异常类与协议签名保持不变（薄委托，行为逐位一致、测试零改动）。
"""
from __future__ import annotations

import json
import os
from typing import Any

from foundation.net.egress import EgressGuard

DEFAULT_BASE_URL: str = "https://api.deepseek.com"
EGRESS_PURPOSE_LLM_CHAT = "llm_chat"


class DeepSeekConfigError(RuntimeError):
    """api_key 未配置（构造可先行，调用即报——各域子类化或直接使用）。"""


def resolve_deepseek_config(
    api_key: str | None,
    base_url: str | None,
    *,
    env_prefix: str = "KB_DEEPSEEK",
) -> tuple[str, str]:
    """统一配置解析：显式参数 → 环境变量（``{PREFIX}_API_KEY``/``{PREFIX}_BASE_URL``）→ 报错/缺省。

    返回 (api_key, base_url)；api_key 无来源时抛 DeepSeekConfigError（中文）。
    """
    key = api_key or os.environ.get(f"{env_prefix}_API_KEY", "").strip()
    if not key:
        raise DeepSeekConfigError(
            f"未配置 DeepSeek API Key：请经构造参数注入或设置环境变量 {env_prefix}_API_KEY"
        )
    url = base_url or os.environ.get(f"{env_prefix}_BASE_URL", "").strip() or DEFAULT_BASE_URL
    return key, url


def post_chat_completions(
    *,
    api_key: str,
    base_url: str,
    payload: dict[str, Any],
    timeout_s: float,
    guard: EgressGuard | None,
    caller: str,
    transport: Any | None = None,
) -> dict[str, Any]:
    """非流式 POST ``/chat/completions``（出域断言前置；200 返回 JSON，其余抛下游异常）。

    异常语义由调用方包装（本函数抛 httpx 原生异常与 ValueError，各域转译为
    自身错误族）——保持各域既有错误文案与测试期望不变。
    """
    import httpx

    url = f"{base_url}/chat/completions"
    if guard is not None:
        guard.assert_egress(
            url,
            EGRESS_PURPOSE_LLM_CHAT,
            content=json.dumps(payload, ensure_ascii=False),
            caller=caller,
        )
    with httpx.Client(timeout=timeout_s, transport=transport) as client:
        response = client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}")
    return response.json()
