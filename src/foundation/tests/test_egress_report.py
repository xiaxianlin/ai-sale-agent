"""知识产权归甲方所有（COM-IP-01）

NFR-SEC-06 数据不出域——月度出域审计报告生成器单元测试。

覆盖（SR-NFR-SEC-06 §6.5 / §9 集成③ 的平台层等价形态）：
- 按「目的地 × 用途」聚合调用量（放行/拦截/字节/最近调用时间）；
- 月份过滤（跨月记录不串账）；空月与非法月份口径；
- 中文 Markdown 输出（标题/总览/聚合表/拦截原因分布/对账说明）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from foundation.net.egress import EgressDestination, EgressGuard, EgressViolation
from foundation.net.report import EgressReportGenerator, build_monthly_report

_LLM_DEST = EgressDestination(
    code="deepseek_api",
    host="api.deepseek.com",
    port=443,
    purposes=("llm_chat", "reply_generate"),
    purpose_desc="DeepSeek-V3 API 调用",
    owner="AI 平台组",
)
_SMTP_DEST = EgressDestination(
    code="smtp",
    host="smtp.example.com",
    port=465,
    schemes=("https", "http"),
    purposes=("mail_send",),
    purpose_desc="告警与业务邮件外发",
    owner="应用组",
)

_SEP_TIMES = [
    datetime(2026, 9, 1, 1, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 15, 8, 30, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 28, 23, 59, 59, tzinfo=timezone.utc),
    datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc),
]
_OCT_TIME = datetime(2026, 10, 2, 9, 0, 0, tzinfo=timezone.utc)


def _seeded_guard() -> EgressGuard:
    """构造带固定时钟的 guard，并造 9 月 4 条 + 10 月 1 条出域留痕。

    9 月样本：deepseek×llm_chat 2 条（1 放行 1 拦截：IP 直连）、
    deepseek×reply_generate 1 条（放行）、smtp×mail_send 1 条（放行）；
    10 月样本：deepseek×llm_chat 1 条（放行，不应进入 9 月报表）。
    """
    times = iter([*_SEP_TIMES, _OCT_TIME])
    guard = EgressGuard(clock=lambda: next(times))
    guard.register_all([_LLM_DEST, _SMTP_DEST])
    guard.assert_egress("https://api.deepseek.com", "llm_chat", content="A" * 100)
    with pytest.raises(EgressViolation):  # IP 直连拦截（未注册目的地）
        guard.assert_egress("https://110.242.5.12/v1", "llm_chat", content="B" * 50)
    guard.assert_egress(
        "https://api.deepseek.com/v1/chat/completions", "reply_generate", content="C" * 10
    )
    guard.assert_egress("https://smtp.example.com:465", "mail_send", content="D" * 5)
    guard.assert_egress("https://api.deepseek.com", "llm_chat", content="E" * 20)  # 10 月
    return guard


# --------------------------------------------------------------------------- 聚合与过滤


class TestMonthlyAggregation:
    def test_month_filter_excludes_other_months(self) -> None:
        guard = _seeded_guard()
        report = EgressReportGenerator(guard).monthly(
            "2026-09", generated_at=datetime(2026, 10, 1, 3, 0, 0, tzinfo=timezone.utc)
        )
        assert "| 出域调用总数 | 4 |" in report
        assert "2026-10-02" not in report  # 10 月记录（最近调用时间列）不串账

    def test_aggregate_by_dest_and_purpose(self) -> None:
        """按目的地 × 用途聚合：调用量/放行/拦截/字节逐行正确。"""
        report = EgressReportGenerator(_seeded_guard()).monthly("2026-09")
        header = "| 目的地 | 用途 | 调用量 | 放行 | 拦截 | 出域字节 | 最近调用（UTC） |"
        assert header in report
        # 已注册目的地的放行行；被拦截的 IP 直连尝试归入「未注册目的地」行（不冒名归属）
        assert "| deepseek_api | llm_chat | 1 | 1 | 0 | 100 | 2026-09-01 01:00:00 |" in report
        assert "| deepseek_api | reply_generate | 1 | 1 | 0 | 10 | 2026-09-28 23:59:59 |" in report
        assert "| smtp | mail_send | 1 | 1 | 0 | 5 | 2026-09-30 12:00:00 |" in report
        assert "| （未注册目的地） | llm_chat | 1 | 0 | 1 | 0 | 2026-09-15 08:30:00 |" in report

    def test_blocked_reason_distribution(self) -> None:
        """拦截原因分布：IP 直连拦截计入 EGRESS-1001。"""
        report = EgressReportGenerator(_seeded_guard()).monthly("2026-09")
        assert "| 拦截 | 1 |" in report
        assert "| EGRESS-1001 | 1 |" in report

    def test_unregistered_dest_row_label(self) -> None:
        """未注册目的地的留痕在聚合表中以中文占位展示。"""
        report = build_monthly_report(
            _seeded_guard().iter_calls(), "2026-09"
        )
        assert "（未注册目的地）" in report


# --------------------------------------------------------------------------- 输出形态


class TestReportFormat:
    def test_markdown_headers_chinese(self) -> None:
        report = EgressReportGenerator(_seeded_guard()).monthly("2026-09")
        assert report.startswith("# 出域审计月报（2026-09）")
        for section in ("## 一、总览", "## 二、按目的地 × 用途聚合", "## 三、拦截原因分布", "## 四、审计说明"):
            assert section in report
        assert "egress.rules" in report  # 与防火墙白名单同源对账说明（决策 D2）
        assert "生成时间" in report

    def test_empty_month_zero_totals(self) -> None:
        report = EgressReportGenerator(_seeded_guard()).monthly("2026-08")
        assert "| 出域调用总数 | 0 |" in report
        assert "本月无出域调用记录" in report
        assert "本月无拦截记录" in report

    def test_invalid_month_format_rejected(self) -> None:
        generator = EgressReportGenerator(_seeded_guard())
        for bad in ("2026-9", "2026-13", "202609", "abc"):
            with pytest.raises(ValueError, match="YYYY-MM"):
                generator.monthly(bad)

    def test_generated_at_override(self) -> None:
        fixed = datetime(2026, 10, 1, 3, 0, 0, tzinfo=timezone.utc)
        report = build_monthly_report(
            _seeded_guard().iter_calls(), "2026-09", generated_at=fixed
        )
        assert "2026-10-01 03:00:00" in report

    def test_records_argument_overrides_guard(self) -> None:
        """直接传记录集合：不依赖 guard 实例（服务层从 PG 取数后的渲染路径）。"""
        records = _seeded_guard().iter_calls()
        report = build_monthly_report(records, "2026-10")
        assert "| 出域调用总数 | 1 |" in report
        assert "| deepseek_api | llm_chat | 1 | 1 | 0 | 20 |" in report
