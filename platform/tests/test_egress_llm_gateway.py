"""知识产权归甲方所有（COM-IP-01）

NFR-SEC-06 数据不出域——LLMEgressGateway 单元测试。

覆盖（SR-NFR-SEC-06 §6.2 / §9 的平台层等价形态）：
- 内容过滤管线：warn 级敏感词替换 [REDACTED] 计数、block 级整块剔除、全剔除 EGRESS-1004；
- ContentFilter 协议 + 注册机制：PII 过滤器桩经 register_filter 注入且先于内置词表执行；
- 过滤后内容快照留存（内存存储接口：sha256 关联、去重）；
- fail-closed：目标未注册（EGRESS-1001）/ OI-09 未确认（EGRESS-1003）时 HTTP 客户端不被调用；
- 放行链路：断言留痕 + HTTP 发出 + 返回结果汇总。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

import pytest

from platform.core.net.egress import (
    EGRESS_NOT_CONFIRMED,
    EGRESS_UNREGISTERED,
    EgressDestination,
    EgressGuard,
    EgressViolation,
)
from platform.core.net.llm_gateway import (
    EGRESS_SENSITIVE_BLOCKED,
    REDACTED_MARK,
    FilterOutcome,
    InMemorySnapshotStore,
    LlmGatewayConfig,
    LLMEgressGateway,
)


class StubHttpClient:
    """HTTP 客户端桩：记录每次调用（URL + 请求体），返回固定响应。"""

    def __init__(self, response: Mapping[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._response = dict(response or {"text": "好的，已生成回复。"})

    def post(self, url: str, body: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((url, dict(body)))
        return dict(self._response)


class StubPiiFilter:
    """PII 过滤器桩（模拟 services 层 cleaning.masking 注入的真实现）：邮箱脱敏。"""

    name = "pii_mask"
    _EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    def __init__(self) -> None:
        self.seen: list[str] = []

    def apply(self, text: str) -> FilterOutcome:
        self.seen.append(text)
        cleaned, hits = self._EMAIL.subn("[PII-EMAIL]", text)
        return FilterOutcome(text=cleaned, hits=hits)


def _guard_with_llm_dest(status: str = "enabled") -> EgressGuard:
    guard = EgressGuard()
    guard.register(
        EgressDestination(
            code="deepseek_api",
            host="api.deepseek.com",
            port=443,
            purposes=("llm_chat", "reply_generate"),
            purpose_desc="DeepSeek-V3 API 调用",
            owner="AI 平台组",
            status=status,  # type: ignore[arg-type]
        )
    )
    return guard


def _gateway(
    guard: EgressGuard | None = None,
    http: StubHttpClient | None = None,
    **config: object,
) -> tuple[LLMEgressGateway, StubHttpClient]:
    kwargs: dict[str, object] = {"sensitive_words": {"warn": ["机密报价"], "block": ["客户名单"]}}
    kwargs.update(config)
    cfg = LlmGatewayConfig(**kwargs)  # type: ignore[arg-type]
    http = http or StubHttpClient()
    guard = guard if guard is not None else _guard_with_llm_dest()
    return LLMEgressGateway(guard, http, config=cfg), http


# --------------------------------------------------------------------------- 内容过滤（词表）


class TestSensitiveWords:
    def test_warn_word_replaced_and_counted(self) -> None:
        """warn 级：命中替换 [REDACTED] 并计数，不阻断发送。"""
        gateway, http = _gateway()
        result = gateway.send("请基于以下机密报价生成回复：机密报价见附件")
        assert len(http.calls) == 1
        sent = http.calls[0][1]["content"]
        assert "机密报价" not in sent
        assert sent.count(REDACTED_MARK) == 2
        assert result.filter_hits["sensitive_words"] == 2

    def test_block_word_drops_whole_block_only(self) -> None:
        """block 级：命中块整块剔除，其余块正常出域。"""
        gateway, http = _gateway()
        result = gateway.send(["正常的知识片段", "这份客户名单不能外发"])
        assert len(http.calls) == 1
        assert http.calls[0][1]["content"] == "正常的知识片段"
        assert result.filter_hits["sensitive_words"] >= 1

    def test_all_blocks_blocked_raises_egress_1004(self) -> None:
        """全 block：整段剔除后无有效内容 → EGRESS-1004，HTTP 不被调用。"""
        gateway, http = _gateway()
        with pytest.raises(EgressViolation) as excinfo:
            gateway.send(["客户名单 A", "客户名单 B"])
        assert excinfo.value.code == EGRESS_SENSITIVE_BLOCKED
        assert http.calls == []
        record = gateway._guard.iter_calls()[-1]
        assert record.verdict == "blocked"
        assert record.block_reason.startswith(EGRESS_SENSITIVE_BLOCKED)

    def test_blank_content_rejected_without_egress(self) -> None:
        gateway, http = _gateway()
        with pytest.raises(ValueError, match="出域内容为空"):
            gateway.send("   ")
        assert http.calls == []

    def test_builtin_word_list_configurable(self) -> None:
        """词表可配：清空词表后原 warn 词不再命中（NFR-MAI-02）。"""
        gateway, http = _gateway(sensitive_words={"warn": (), "block": ()})
        gateway.send("机密报价 原样外发")
        assert http.calls[0][1]["content"] == "机密报价 原样外发"  # 不再命中


# --------------------------------------------------------------------------- 过滤器注册机制


class TestFilterRegistration:
    def test_pii_filter_injected_and_runs_before_builtin(self) -> None:
        """ContentFilter 协议注册：PII 桩注入后先于内置词表执行（SR §6.2 a→c 顺序）。"""
        gateway, http = _gateway()
        pii = StubPiiFilter()
        gateway.register_filter(pii)
        result = gateway.send("买家 buyer@acme.com 询问机密报价")
        sent = http.calls[0][1]["content"]
        assert "buyer@acme.com" not in sent
        assert "[PII-EMAIL]" in sent
        assert REDACTED_MARK in sent
        assert result.filter_hits["pii_mask"] == 1
        assert result.filter_hits["sensitive_words"] == 1
        # PII 过滤器看到的是原始文本（先执行），内置词表看到的是脱敏后文本
        assert pii.seen[0] == "买家 buyer@acme.com 询问机密报价"

    def test_filters_property_exposes_pipeline(self) -> None:
        gateway, _http = _gateway()
        names = [f.name for f in gateway.filters]
        assert names == ["sensitive_words"]  # 未注入外部过滤器时仅内置词表
        pii = StubPiiFilter()
        gateway.register_filter(pii)
        assert [f.name for f in gateway.filters] == ["pii_mask", "sensitive_words"]


# --------------------------------------------------------------------------- 快照留存


class TestSnapshot:
    def test_snapshot_of_filtered_content_retained(self) -> None:
        """过滤后内容快照留存：sha256 与实际出域载荷一致，可按 hash 取回。"""
        snapshots = InMemorySnapshotStore()
        guard = _guard_with_llm_dest()
        http = StubHttpClient()
        gateway = LLMEgressGateway(
            guard, http,
            config=LlmGatewayConfig(sensitive_words={"warn": ["机密报价"], "block": []}),
            snapshot_store=snapshots,
        )
        result = gateway.send("买家邮箱 buyer@acce.com 询问机密报价")
        sent = http.calls[0][1]["content"]
        snapshot = snapshots.get(result.content_sha256)
        assert snapshot is not None
        assert snapshot.content == sent
        assert snapshot.sha256 == hashlib.sha256(sent.encode("utf-8")).hexdigest()
        # 调用留痕的内容摘要与快照关联（SR §4 content_sha256）
        record = guard.iter_calls()[-1]
        assert record.verdict == "allowed"
        assert record.content_sha256 == snapshot.sha256

    def test_snapshot_dedup_by_hash(self) -> None:
        """同内容多次出域：按 sha256 去重留存（SR §4 content_sha256 去重口径）。"""
        snapshots = InMemorySnapshotStore()
        guard = _guard_with_llm_dest()
        gateway = LLMEgressGateway(guard, StubHttpClient(), snapshot_store=snapshots)
        gateway.send("相同内容")
        gateway.send("相同内容")
        assert snapshots.count() == 1


# --------------------------------------------------------------------------- fail-closed 链路


class TestFailClosedBeforeHttp:
    def test_unregistered_dest_no_http_call(self) -> None:
        """目标不在白名单：EGRESS-1001，HTTP 客户端零调用（无先发后审）。"""
        guard = EgressGuard()  # 空注册表
        http = StubHttpClient()
        gateway = LLMEgressGateway(guard, http)
        with pytest.raises(EgressViolation) as excinfo:
            gateway.send("正常内容")
        assert excinfo.value.code == EGRESS_UNREGISTERED
        assert http.calls == []
        # 拦截仍留痕（blocked），且快照已按管线顺序留存（过滤→快照→断言）
        assert gateway._guard.iter_calls()[-1].verdict == "blocked"

    def test_unconfirmed_dest_no_http_call(self) -> None:
        """OI-09 未确认（pending_reeval）：EGRESS-1003 拒绝。"""
        guard = _guard_with_llm_dest(status="pending_reeval")
        http = StubHttpClient()
        gateway = LLMEgressGateway(guard, http)
        with pytest.raises(EgressViolation) as excinfo:
            gateway.send("正常内容")
        assert excinfo.value.code == EGRESS_NOT_CONFIRMED
        assert http.calls == []

    def test_purpose_not_declared_no_http_call(self) -> None:
        gateway, http = _gateway()
        with pytest.raises(EgressViolation, match="用途越界"):
            gateway.send("正常内容", purpose="mail_send")
        assert http.calls == []


# --------------------------------------------------------------------------- 放行链路


class TestHappyPath:
    def test_send_returns_result_and_records(self) -> None:
        gateway, http = _gateway()
        result = gateway.send(
            "请生成回复", purpose="reply_generate", caller="agent-core", trace_id="t-001"
        )
        assert result.text == "好的，已生成回复。"
        assert result.response["text"] == "好的，已生成回复。"
        assert result.content_sha256
        record = gateway._guard.iter_calls()[-1]
        assert record.verdict == "allowed"
        assert record.dest_code == "deepseek_api"
        assert record.purpose == "reply_generate"
        assert record.caller == "agent-core"
        assert record.trace_id == "t-001"
        assert record.content_sha256 == result.content_sha256
        assert record.bytes_out == len("请生成回复".encode("utf-8"))
        assert result.log_seq == record.seq

    def test_default_purpose_from_config(self) -> None:
        gateway, http = _gateway(purpose="llm_chat")
        gateway.send("内容")
        assert gateway._guard.iter_calls()[-1].purpose == "llm_chat"

    def test_multi_block_payload_joined(self) -> None:
        gateway, http = _gateway()
        gateway.send(["块一", "块二"])
        assert http.calls[0][1]["content"] == "块一\n块二"
