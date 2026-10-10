"""知识产权归甲方所有（COM-IP-01）

NFR-SEC-06 数据不出域——EgressGuard 出域白名单引擎单元测试。

覆盖（SR-NFR-SEC-06 §9 测试要点的平台层等价形态）：
- fail-closed：注册表为空时一切目标默认拒绝；
- 未注册目的地拒绝 / 注册后放行（默认端口、显式端口、带路径 URL）；
- IP 直连绕过域名的拦截（URL 主机为 IP 字面量一律拒绝；注册表禁止注册 IP 主机）；
- 端口/协议不匹配、OI-09 未确认（disabled/pending_reeval）、用途越界拒绝；
- assert_egress 抛 EgressViolation 且拦截留痕；调用记录含内容摘要 hash + 目的地 + 时间；
- 审计钩子回调（放行与拦截均触发）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from foundation.net.egress import (
    EGRESS_NOT_CONFIRMED,
    EGRESS_PURPOSE_OVERFLOW,
    EGRESS_UNREGISTERED,
    EgressCallRecord,
    EgressDestination,
    EgressGuard,
    EgressRegistryError,
    EgressViolation,
    content_digest,
)


def _llm_dest(**overrides: object) -> EgressDestination:
    """构造 DeepSeek 目的地注册项（测试基线：443/https/llm 用途）。"""
    fields: dict[str, object] = {
        "code": "deepseek_api",
        "host": "api.deepseek.com",
        "port": 443,
        "purposes": ("llm_chat", "reply_generate"),
        "purpose_desc": "DeepSeek-V3 API 调用（已脱敏文本 + 知识片段）",
        "owner": "AI 平台组",
    }
    fields.update(overrides)
    return EgressDestination(**fields)  # type: ignore[arg-type]


def _smtp_dest() -> EgressDestination:
    return EgressDestination(
        code="smtp",
        host="smtp.example.com",
        port=465,
        schemes=("https", "http"),
        purposes=("mail_send",),
        purpose_desc="告警与业务邮件外发",
        owner="应用组",
    )


# --------------------------------------------------------------------------- fail-closed


class TestFailClosed:
    def test_empty_registry_rejects_everything(self) -> None:
        """注册表为空：任意 URL 全部拒绝（fail-closed，宁拒勿漏）。"""
        guard = EgressGuard()
        for url in ("https://api.deepseek.com", "https://oss.example.com/bucket", "http://x.y"):
            assert guard.is_allowed(url) is False

    def test_empty_registry_assert_raises_with_blocked_record(self) -> None:
        """空注册表断言：抛 EGRESS-1001 且拦截尝试已留痕。"""
        guard = EgressGuard()
        with pytest.raises(EgressViolation) as excinfo:
            guard.assert_egress("https://api.deepseek.com", "llm_chat", content="你好")
        assert excinfo.value.code == EGRESS_UNREGISTERED
        assert "fail-closed" in excinfo.value.detail
        records = guard.iter_calls()
        assert len(records) == 1
        assert records[0].verdict == "blocked"
        assert records[0].block_reason.startswith(EGRESS_UNREGISTERED)


# --------------------------------------------------------------------------- 注册与放行


class TestRegisteredAllow:
    def test_registered_host_allowed(self) -> None:
        """注册后放行：默认端口、显式 443、带路径均命中同一注册项。"""
        guard = EgressGuard()
        guard.register(_llm_dest())
        assert guard.is_allowed("https://api.deepseek.com") is True
        assert guard.is_allowed("https://api.deepseek.com/v1/chat/completions") is True
        assert guard.is_allowed("https://api.deepseek.com:443") is True

    def test_unregistered_host_rejected(self) -> None:
        """未注册目的地（含端口仅差一位）默认拒绝。"""
        guard = EgressGuard()
        guard.register(_llm_dest())
        assert guard.is_allowed("https://api.openai.com") is False
        assert guard.is_allowed("https://api.deepseek.com:8443") is False

    def test_port_and_scheme_mismatch_rejected(self) -> None:
        guard = EgressGuard()
        guard.register(_llm_dest())  # 443 / 仅 https
        assert guard.is_allowed("http://api.deepseek.com") is False

    def test_smtp_dest_allowed_on_declared_port(self) -> None:
        guard = EgressGuard()
        guard.register(_smtp_dest())
        assert guard.is_allowed("https://smtp.example.com:465") is True
        assert guard.is_allowed("https://smtp.example.com") is False  # 默认 443 未注册


class TestAssertEgress:
    def test_allowed_returns_record_with_digest_and_time(self) -> None:
        """放行留痕：内容摘要 hash + 目的地 + 时间（审计最小三元组）。"""
        guard = EgressGuard()
        guard.register(_llm_dest())
        content = "已脱敏的询盘回复草稿"
        record = guard.assert_egress(
            "https://api.deepseek.com/v1/chat/completions",
            "reply_generate",
            content=content,
            caller="agent-core",
            trace_id="trace-1",
        )
        assert isinstance(record, EgressCallRecord)
        assert record.verdict == "allowed"
        assert record.dest_code == "deepseek_api"
        assert record.content_sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert record.bytes_out == len(content.encode("utf-8"))
        assert record.purpose == "reply_generate"
        assert record.created_at.tzinfo is not None
        assert guard.iter_calls()[-1].seq == record.seq

    def test_purpose_overflow_rejected(self) -> None:
        """用途越界：声明用途不在目的地 purposes 内 → EGRESS-1002。"""
        guard = EgressGuard()
        guard.register(_llm_dest())
        with pytest.raises(EgressViolation) as excinfo:
            guard.assert_egress("https://api.deepseek.com", "mail_send")
        assert excinfo.value.code == EGRESS_PURPOSE_OVERFLOW
        assert guard.iter_calls()[-1].verdict == "blocked"

    def test_empty_purpose_rejected(self) -> None:
        guard = EgressGuard()
        guard.register(_llm_dest())
        with pytest.raises(EgressViolation) as excinfo:
            guard.assert_egress("https://api.deepseek.com", "  ")
        assert excinfo.value.code == EGRESS_PURPOSE_OVERFLOW


# --------------------------------------------------------------------------- IP 直连拦截


class TestIpDirectBypass:
    def test_ipv4_literal_url_rejected(self) -> None:
        """IP 直连绕过域名：即使该 IP 属已注册域名解析结果，也一律拒绝。"""
        guard = EgressGuard()
        guard.register(_llm_dest())
        assert guard.is_allowed("https://110.242.5.12/v1") is False
        result = guard.check("https://110.242.5.12/v1")
        assert result.code == EGRESS_UNREGISTERED
        assert "IP 直连" in result.detail

    def test_ipv6_literal_url_rejected(self) -> None:
        guard = EgressGuard()
        guard.register(_llm_dest())
        assert guard.is_allowed("http://[2001:db8::1]/") is False

    def test_registering_ip_host_forbidden(self) -> None:
        """注册表禁止注册 IP 字面量主机（防火墙 CIDR 属网络层视图，代码层必须域名）。"""
        with pytest.raises(ValidationError, match="IP 直连"):
            _llm_dest(host="110.242.5.12")


# --------------------------------------------------------------------------- OI-09 状态


class TestDestStatus:
    def test_pending_reeval_rejected_as_not_confirmed(self) -> None:
        """OI-09 未确认/待重评：EGRESS-1003 拒绝；确认（置 enabled）后放行。"""
        guard = EgressGuard()
        guard.register(_llm_dest(status="pending_reeval"))
        with pytest.raises(EgressViolation) as excinfo:
            guard.assert_egress("https://api.deepseek.com", "llm_chat")
        assert excinfo.value.code == EGRESS_NOT_CONFIRMED

        guard.set_status("deepseek_api", "enabled")
        assert guard.is_allowed("https://api.deepseek.com") is True

    def test_disabled_rejected(self) -> None:
        guard = EgressGuard()
        guard.register(_llm_dest(status="disabled"))
        assert guard.is_allowed("https://api.deepseek.com") is False

    def test_unknown_code_on_set_status_raises(self) -> None:
        guard = EgressGuard()
        with pytest.raises(EgressRegistryError):
            guard.set_status("no_such", "enabled")


# --------------------------------------------------------------------------- 注册校验与留痕


class TestRegistryValidation:
    def test_duplicate_code_rejected(self) -> None:
        guard = EgressGuard()
        guard.register(_llm_dest())
        with pytest.raises(EgressRegistryError, match="编码已注册"):
            guard.register(_llm_dest(host="other.deepseek.com"))

    def test_duplicate_host_port_rejected(self) -> None:
        guard = EgressGuard()
        guard.register(_llm_dest())
        with pytest.raises(EgressRegistryError, match="主机/端口已被"):
            guard.register(
                EgressDestination(
                    code="deepseek_mirror",
                    host="api.deepseek.com",
                    port=443,
                    purposes=("llm_chat",),
                    owner="AI 平台组",
                )
            )

    def test_invalid_destinations_rejected_by_model(self) -> None:
        with pytest.raises(ValidationError):
            _llm_dest(port=0)
        with pytest.raises(ValidationError):
            _llm_dest(purposes=())
        with pytest.raises(ValidationError):
            _llm_dest(host="https://api.deepseek.com")  # host 不得携带 scheme
        with pytest.raises(ValidationError):
            _llm_dest(schemes=("ftp",))

    def test_malformed_urls_rejected(self) -> None:
        guard = EgressGuard()
        guard.register(_llm_dest())
        for url in ("", "   ", "api.deepseek.com", "ftp://api.deepseek.com", "https://:443/"):
            assert guard.is_allowed(url) is False


class TestCallLedgerAndHook:
    def test_audit_hook_invoked_for_allowed_and_blocked(self) -> None:
        """审计钩子：放行与拦截各回调一次（对接 NFR-SEC-03 审计链的接口验证）。"""
        seen: list[EgressCallRecord] = []
        guard = EgressGuard(audit_hook=_Recorder(seen))
        guard.register(_llm_dest())
        guard.assert_egress("https://api.deepseek.com", "llm_chat", content="a")
        with pytest.raises(EgressViolation):
            guard.assert_egress("https://unknown.example.com", "llm_chat", content="b")
        assert [r.verdict for r in seen] == ["allowed", "blocked"]
        assert seen[1].dest_code == ""  # 未注册目的地无编码

    def test_blocked_record_carries_reason_and_digest(self) -> None:
        guard = EgressGuard()
        guard.register(_llm_dest())
        with pytest.raises(EgressViolation):
            guard.assert_egress("https://evil.example.com", "llm_chat", content="机密")
        record = guard.iter_calls()[-1]
        assert record.verdict == "blocked"
        assert record.block_reason.startswith(EGRESS_UNREGISTERED)
        assert record.content_sha256 == content_digest("机密")
        assert record.host == "evil.example.com"

    def test_content_digest_stable_for_bytes_and_str(self) -> None:
        assert content_digest("数据") == content_digest("数据".encode("utf-8"))


class _Recorder:
    """审计钩子桩：收集回调记录（模拟 NFR-SEC-03 审计链接入）。"""

    def __init__(self, sink: list[EgressCallRecord]) -> None:
        self._sink = sink

    def on_egress_call(self, record: EgressCallRecord) -> None:
        self._sink.append(record)


# --------------------------------------------------------------------------- 时间戳口径


class TestClock:
    def test_clock_injection_for_report_month_bucketing(self) -> None:
        """clock 可注入：留痕时间走注入时钟（供月度报表按月分桶测试复用）。"""
        fixed = datetime(2026, 9, 15, 8, 0, 0, tzinfo=timezone.utc)
        guard = EgressGuard(clock=lambda: fixed)
        guard.register(_llm_dest())
        guard.assert_egress("https://api.deepseek.com", "llm_chat")
        assert guard.iter_calls()[0].created_at == fixed
