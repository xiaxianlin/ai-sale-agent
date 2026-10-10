# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""LLM 客户端层测试（FR-TR-001：确定性教学桩 / DeepSeek 出域断言与 SSE 解析）。"""
from __future__ import annotations

import json

import httpx
import pytest
from foundation.net.egress import EgressDestination, EgressGuard, EgressViolation

from services.training.core.tutor.llm import (
    ENV_API_KEY,
    DeepSeekLlmClient,
    FakeTutorLlm,
    LlmConfigError,
    LlmDownstreamError,
)

# --------------------------------------------------------------------------- FakeTutorLlm


class TestFakeTutorLlm:
    def test_answer_cites_all_sources_and_socratic_ending(self) -> None:
        """命中回答：每个事实句带 [来源N]，结尾苏格拉底式追问。"""
        llm = FakeTutorLlm()
        messages = [
            {
                "role": "system",
                "content": (
                    "【知识资料】以下为本次检索命中的知识块：\n"
                    "[来源1] 支架参数表（知识库：product，相关度：0.87）\n"
                    "ST-3200 壁挂支架最大承载 45 千克。本体冷轧钢板。\n"
                    "[来源2] 认证清单（知识库：product，相关度：0.71）\n"
                    "该型号已通过 CE 与 SGS 认证。适用于展厅吊装。"
                ),
            },
            {"role": "user", "content": "ST-3200 能承多重？"},
        ]
        answer = "".join(llm.generate_stream(messages, system="教学提示词"))
        assert "[来源1]" in answer and "[来源2]" in answer
        # 事实句取自检索块正文（可溯源，不编造）
        assert "壁挂支架最大承载 45 千克" in answer
        # 苏格拉底式结尾追问
        assert answer.rstrip().endswith("。") and "你会先向客户确认" in answer
        assert llm.calls == 1

    def test_stream_is_chunked_and_concat_equals_answer(self) -> None:
        """流式切块聚合 = 完整回答（块大小恒定，确定性）。"""
        llm = FakeTutorLlm()
        messages = [
            {
                "role": "system",
                "content": (
                    "【知识资料】\n[来源1] 标题甲（知识库：skill，相关度：0.80）\n让步必须换取对等条件。"
                ),
            }
        ]
        chunks = list(llm.generate_stream(messages, system="s"))
        assert len(chunks) > 1
        assert all(len(c) <= 16 for c in chunks)
        assert "".join(chunks) == "".join(llm.generate_stream(messages, system="s"))  # 确定性

    def test_no_blocks_defensive_answer(self) -> None:
        """防御分支：无知识条目时只说暂无，不编造。"""
        llm = FakeTutorLlm()
        answer = "".join(llm.generate_stream([{"role": "user", "content": "q"}], system="s"))
        assert "知识库暂无该内容" in answer
        assert "[来源" not in answer


# --------------------------------------------------------------------------- DeepSeekLlmClient


def _guarded_client(transport: httpx.BaseTransport, api_key: str = "sk-demo") -> DeepSeekLlmClient:
    """构造带白名单守卫 + MockTransport 的离线客户端（注册 api.deepseek.com）。"""
    guard = EgressGuard()
    guard.register(
        EgressDestination(
            code="deepseek_api",
            host="api.deepseek.com",
            port=443,
            purposes=("llm_chat",),
            owner="ai-sale-agent",
        )
    )
    return DeepSeekLlmClient(api_key=api_key, guard=guard, transport=transport)


def _sse_response(*deltas: str) -> httpx.MockTransport:
    """构造 DeepSeek SSE 流响应桩（data: {json} 行 + [DONE]）。"""
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': d}}]})}" for d in deltas]
    lines.append("data: [DONE]")
    body = "\n".join(lines)
    return httpx.MockTransport(
        lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    )


class TestDeepSeekLlmClient:
    def test_missing_api_key_chinese_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """未配置 KB_DEEPSEEK_API_KEY：中文报错（demo 口径，真实调用需运维注入）。"""
        monkeypatch.delenv(ENV_API_KEY, raising=False)
        client = DeepSeekLlmClient()
        with pytest.raises(LlmConfigError, match="KB_DEEPSEEK_API_KEY"):
            list(client.generate_stream([{"role": "user", "content": "q"}], system="s"))

    def test_sse_deltas_parsed_offline(self) -> None:
        """已配置 + 白名单放行：离线解析 SSE delta 流。"""
        client = _guarded_client(_sse_response("你好", "，", "导师"))
        tokens = list(
            client.generate_stream([{"role": "user", "content": "问"}], system="教学")
        )
        assert tokens == ["你好", "，", "导师"]

    def test_egress_violation_blocks_before_request(self) -> None:
        """白名单外目标：出域断言先拒（fail-closed），HTTP 请求不发起。"""
        requested: list[str] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(200, text="data: [DONE]")

        client = DeepSeekLlmClient(api_key="sk-demo", transport=httpx.MockTransport(_handler))
        with pytest.raises(EgressViolation):
            list(client.generate_stream([{"role": "user", "content": "q"}], system="s"))
        assert requested == []  # 拦截发生在连接前（无「先发后审」）

    def test_http_error_raises_downstream(self) -> None:
        """非 200 响应：转 LlmDownstreamError（上层映射 50002）。"""
        transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
        client = _guarded_client(transport)
        with pytest.raises(LlmDownstreamError, match="500"):
            list(client.generate_stream([{"role": "user", "content": "q"}], system="s"))

    def test_default_guard_is_fail_closed(self) -> None:
        """未注入守卫：空注册表 fail-closed，任何目标都拒绝。"""
        client = DeepSeekLlmClient(api_key="sk-demo")
        with pytest.raises(EgressViolation):
            list(client.generate_stream([{"role": "user", "content": "q"}], system="s"))
