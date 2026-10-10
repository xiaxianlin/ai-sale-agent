"""知识产权归甲方所有（COM-IP-01）

NFR-SEC-06 数据不出域——月度出域审计报告生成器（平台基座实现）。

依据：docs/07-实现方案/NFR/SR-NFR-SEC-06-数据不出域.md §6.5（EgressAuditWorker
的报表形态）：① 调用量统计（按「目的地 × 用途」聚合）② 拦截/违规统计 ③ 与防火墙
白名单同源对账说明。输出中文 Markdown，供月度安全审计、甲方抽检与归档
（OSS 加密归档路径随服务层落地）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from foundation.net.egress import EgressCallRecord, EgressGuard

_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
"""月份口径：YYYY-MM（cron 每月 1 日按上月生成，SR §7 egress.audit.report_cron）。"""


@dataclass
class _AggRow:
    """「目的地 × 用途」聚合行（报表主表的一行；聚合过程中的内部可变累加器）。"""

    dest_code: str
    purpose: str
    total: int
    allowed: int
    blocked: int
    bytes_out: int
    last_at: datetime | None

    @property
    def dest_label(self) -> str:
        """目的地展示名（未注册目标无编码，展示为中文占位）。"""
        return self.dest_code or "（未注册目的地）"

    @property
    def purpose_label(self) -> str:
        return self.purpose or "（未声明）"


def _aggregate(records: Iterable[EgressCallRecord]) -> list[_AggRow]:
    """按「目的地 × 用途」聚合：调用量 / 放行 / 拦截 / 出域字节 / 最近调用时间。"""
    rows: dict[tuple[str, str], _AggRow] = {}
    for record in records:
        key = (record.dest_code, record.purpose)
        row = rows.get(key)
        if row is None:
            row = _AggRow(
                dest_code=record.dest_code,
                purpose=record.purpose,
                total=0,
                allowed=0,
                blocked=0,
                bytes_out=0,
                last_at=None,
            )
            rows[key] = row
        row.total += 1
        if record.verdict == "allowed":
            row.allowed += 1
            row.bytes_out += record.bytes_out
        else:
            row.blocked += 1
        if row.last_at is None or record.created_at > row.last_at:
            row.last_at = record.created_at
    return sorted(rows.values(), key=lambda r: (r.dest_label, r.purpose_label))


def _fmt_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class EgressReportGenerator:
    """月度出域审计报告生成器：聚合 guard 调用账本，渲染中文 Markdown 月报。

    用法：``EgressReportGenerator(guard).monthly("2026-09")``；
    亦可直接传入记录集合（``records=``）渲染（报表互斥锁与 OSS 归档随服务层落地）。
    """

    def __init__(self, guard: EgressGuard | None = None) -> None:
        self._guard = guard

    def monthly(
        self,
        month: str,
        *,
        records: Iterable[EgressCallRecord] | None = None,
        generated_at: datetime | None = None,
    ) -> str:
        """生成指定月份（YYYY-MM）的出域审计月报（中文 Markdown）。"""
        if not _MONTH_PATTERN.match(month):
            raise ValueError(f"月份格式应为 YYYY-MM（如 2026-09），收到 {month!r}")
        source: tuple[EgressCallRecord, ...]
        if records is not None:
            source = tuple(records)
        elif self._guard is not None:
            source = self._guard.iter_calls()
        else:
            source = ()
        selected = [r for r in source if r.created_at.strftime("%Y-%m") == month]

        total = len(selected)
        allowed = sum(1 for r in selected if r.verdict == "allowed")
        blocked = total - allowed
        bytes_out = sum(r.bytes_out for r in selected if r.verdict == "allowed")
        rows = _aggregate(selected)

        reasons: dict[str, int] = {}
        for record in selected:
            if record.verdict == "blocked" and record.block_reason:
                code = record.block_reason.split(" ", 1)[0]
                reasons[code] = reasons.get(code, 0) + 1
        reason_rows = sorted(reasons.items())

        now = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        lines: list[str] = [
            f"# 出域审计月报（{month}）",
            "",
            f"- 生成时间（UTC）：{_fmt_time(now)}",
            "- 统计口径：NFR-SEC-06 出域调用留痕（每次出域尝试均记录：内容摘要 SHA-256 + 目的地 + 时间）",
            f"- 数据范围：{month} 内放行与拦截的全部出域尝试，共 {total} 条",
            "",
            "## 一、总览",
            "",
            "| 指标 | 数值 |",
            "|---|---|",
            f"| 出域调用总数 | {total} |",
            f"| 放行 | {allowed} |",
            f"| 拦截 | {blocked} |",
            f"| 出域总字节（仅放行） | {bytes_out} |",
            "",
            "## 二、按目的地 × 用途聚合",
            "",
        ]
        if rows:
            lines += [
                "| 目的地 | 用途 | 调用量 | 放行 | 拦截 | 出域字节 | 最近调用（UTC） |",
                "|---|---|---|---|---|---|---|",
            ]
            for row in rows:
                lines.append(
                    f"| {row.dest_label} | {row.purpose_label} | {row.total} | {row.allowed} "
                    f"| {row.blocked} | {row.bytes_out} | {_fmt_time(row.last_at)} |"
                )
        else:
            lines.append("本月无出域调用记录（含 Embedding 本地部署零出域核查项，见第四节）。")
        lines += [
            "",
            "## 三、拦截原因分布",
            "",
        ]
        if reason_rows:
            lines += [
                "| 错误码 | 次数 |",
                "|---|---|",
            ]
            for code, count in reason_rows:
                lines.append(f"| {code} | {count} |")
        else:
            lines.append("本月无拦截记录。")
        lines += [
            "",
            "## 四、审计说明",
            "",
            "- 每次放行调用均含内容摘要 SHA-256，可关联出域内容快照留证（决策 D4）；",
            "- 本报表须与 `deploy/firewall/egress.rules` 放行条目**逐条对账**（决策 D2：代码层注册表"
            "与网络层规则同源，新增目的地必须双侧同步并走变更评审）；",
            "- Embedding 核查：出域注册表不含任何 embedding 类目标，外部 Embedding API 调用数应为 0",
            "  （SR §6.3 第四道兜底；TEI 容器出网流量另以网络监控证据核对）；",
            "- 拦截原因码口径：EGRESS-1001 白名单外目标（含 IP 直连）/ EGRESS-1002 用途越界 /",
            "  EGRESS-1003 OI-09 未获甲方确认 / EGRESS-1004 敏感词整段剔除。",
            "",
        ]
        return "\n".join(lines)


def build_monthly_report(
    records: Iterable[EgressCallRecord],
    month: str,
    *,
    generated_at: datetime | None = None,
) -> str:
    """便捷函数：直接由记录集合生成月报（不依赖 guard 实例）。"""
    return EgressReportGenerator().monthly(month, records=records, generated_at=generated_at)
