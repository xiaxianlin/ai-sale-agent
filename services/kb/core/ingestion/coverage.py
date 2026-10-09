# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""知识库覆盖率核算（§16.1「知识库覆盖率 100%，材料清单核对」的实现，迭代 0.3）。

口径（SRS §16.1 + 开发计划 §4.1 迭代 0.3 退出条件）：
- 材料清单 = 期望素材 × 六库分配（MaterialExpectation 序列，甲方素材台账的
  代码侧形态；生产替换为甲方真实清单）；
- 实际入库 = 批次台账（BatchManifest）逐文件结果；
- 覆盖率 = 成功入库素材数 / 期望素材总数；成功口径 = 文件编排终态 success
  且至少一块完成向量入库（未入向量的素材检索不可见，不计覆盖）；
- 缺口四类（CoverageGap.reason）：清单素材未出现在批次（missing）/ 编排失败
  （failed，附中文原因）/ 成功但零入库块（no_chunks）/ 清单分配的库整库
  无成功条目（kb_empty，§16.1 六库建设齐备性）。

依据文档：docs/01-需求文档/AI外贸销售智能体系统_需求规格说明书.md §16.1；
         docs/05-项目管理/03-开发计划.md §4.1。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from services.kb.core.ingestion.orchestrator import BatchManifest
from services.kb.core.vectorstore.base import KB_TYPES

# ── 缺口原因枚举（中文说明供报告直出）───────────────────────────────────────

REASON_MISSING = "missing"        # 清单素材未出现在批次结果中（漏跑/路径漂移）
REASON_FAILED = "failed"          # 编排终态失败（解析/清洗/分块/条目化/嵌入）
REASON_NO_CHUNKS = "no_chunks"    # 终态成功但零入库块（空素材或全部判重）
REASON_KB_EMPTY = "kb_empty"      # 清单分配的库整库无成功条目

REASON_LABELS: dict[str, str] = {
    REASON_MISSING: "素材未入库批次",
    REASON_FAILED: "编排失败",
    REASON_NO_CHUNKS: "成功但零向量入库",
    REASON_KB_EMPTY: "整库无成功条目",
}


@dataclass(frozen=True)
class MaterialExpectation:
    """材料清单行：期望素材 + 目标知识库分配（覆盖率分母的最小单元）。"""

    source: str
    kb_type: str

    def __post_init__(self) -> None:
        """构造期校验：kb_type 必须命中六大知识库枚举。"""
        if self.kb_type not in KB_TYPES:
            raise ValueError(
                f"材料清单 {self.source} 的 kb_type={self.kb_type!r} 不在六大知识库枚举 {list(KB_TYPES)}"
            )


def material_plan_from_sources(
    sources: Sequence[Any],
) -> list[MaterialExpectation]:
    """从素材序列构造材料清单（兼容 SourceMaterial / 二元组两种形态）。"""
    plan: list[MaterialExpectation] = []
    for item in sources:
        if isinstance(item, MaterialExpectation):
            plan.append(item)
        elif isinstance(item, tuple) and len(item) == 2:
            plan.append(MaterialExpectation(source=str(item[0]), kb_type=str(item[1])))
        else:
            plan.append(
                MaterialExpectation(source=str(item.path), kb_type=str(item.kb_type))
            )
    return plan


@dataclass(frozen=True)
class CoverageGap:
    """单条覆盖缺口（中文说明供报告与看板直出）。"""

    source: str
    kb_type: str
    reason: str        # REASON_* 枚举
    detail: str = ""   # 失败原因等补充信息

    def to_dict(self) -> dict[str, str]:
        """序列化（报告数据口径）。"""
        return {
            "source": self.source,
            "kb_type": self.kb_type,
            "reason": self.reason,
            "reason_label": REASON_LABELS.get(self.reason, self.reason),
            "detail": self.detail,
        }


@dataclass
class CoverageReport:
    """覆盖率核算结论：总体/分库计数 + 覆盖率 + 缺口清单。"""

    expected_total: int = 0
    ingested_total: int = 0
    per_kb: dict[str, dict[str, int]] = field(default_factory=dict)
    gaps: list[CoverageGap] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """覆盖率 = 成功入库素材数 / 期望素材数（期望为 0 时恒 1.0，空集不设缺口）。"""
        if self.expected_total == 0:
            return 1.0
        return self.ingested_total / self.expected_total

    @property
    def coverage_pct(self) -> float:
        """覆盖率百分数（报告展示口径，两位小数内由格式化承担）。"""
        return self.coverage * 100.0

    @property
    def passed(self) -> bool:
        """达标口径：覆盖率 100% 且无任何缺口（§16.1 目标值 100%）。"""
        return not self.gaps and self.coverage >= 1.0

    def to_dict(self) -> dict[str, Any]:
        """序列化（报告数据口径）。"""
        return {
            "expected_total": self.expected_total,
            "ingested_total": self.ingested_total,
            "coverage": round(self.coverage, 4),
            "coverage_pct": round(self.coverage_pct, 2),
            "passed": self.passed,
            "per_kb": {kb: dict(counts) for kb, counts in self.per_kb.items()},
            "gaps": [gap.to_dict() for gap in self.gaps],
        }


def compute_coverage(
    plan: Sequence[MaterialExpectation],
    manifest: BatchManifest | None,
) -> CoverageReport:
    """材料清单 vs 实际入库结果比对 → 覆盖率与缺口清单。

    manifest 允许为 None（批次未跑）：全部清单素材记 missing 缺口，
    覆盖率 0%（显式失败优于静默通过）。
    """
    report = CoverageReport(expected_total=len(plan))
    # 分库计数初始化：仅清单实际分配到的库（未分配的库不设缺口）
    assigned_kbs = {expectation.kb_type for expectation in plan}
    for kb_type in sorted(assigned_kbs):
        report.per_kb[kb_type] = {"expected": 0, "ingested": 0}

    for expectation in plan:
        counts = report.per_kb[expectation.kb_type]
        counts["expected"] += 1
        result = manifest.result_of(expectation.source) if manifest is not None else None
        if result is None:
            report.gaps.append(
                CoverageGap(
                    source=expectation.source,
                    kb_type=expectation.kb_type,
                    reason=REASON_MISSING,
                    detail="清单素材未出现在批次结果中（漏跑或路径漂移）",
                )
            )
            continue
        if result.status != "success" or result.indexed_chunks <= 0:
            reason = REASON_FAILED if result.status != "success" else REASON_NO_CHUNKS
            detail = result.error or (
                "编排成功但没有任何块写入向量库（检索不可见）" if reason == REASON_NO_CHUNKS else "编排失败"
            )
            report.gaps.append(
                CoverageGap(
                    source=expectation.source,
                    kb_type=expectation.kb_type,
                    reason=reason,
                    detail=detail,
                )
            )
            continue
        # 成功入库：总体与分库分子推进
        report.ingested_total += 1
        counts["ingested"] += 1

    # 整库齐备性：清单分配的库每库须 ≥1 成功条目（§16.1 六库建设口径）
    for kb_type, counts in report.per_kb.items():
        if counts["expected"] > 0 and counts["ingested"] == 0:
            report.gaps.append(
                CoverageGap(
                    source="-",
                    kb_type=kb_type,
                    reason=REASON_KB_EMPTY,
                    detail=f"清单为 {kb_type} 库分配了 {counts['expected']} 份素材，成功入库 0 份",
                )
            )
    return report


__all__ = [
    "REASON_MISSING",
    "REASON_FAILED",
    "REASON_NO_CHUNKS",
    "REASON_KB_EMPTY",
    "REASON_LABELS",
    "MaterialExpectation",
    "material_plan_from_sources",
    "CoverageGap",
    "CoverageReport",
    "compute_coverage",
]
