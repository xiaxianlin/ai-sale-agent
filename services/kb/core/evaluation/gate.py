# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""回归门禁（FR-KB-016 / SR-KB-016 §6.4，迭代 0.3）。

相对门槛（04-测试文档 §2.3「准确率下降 >2% 阻断发布」）：总体 Top5 命中率与
任一可比维度的较基线回落均须 ≤ threshold_pp（默认 2.0pp）——恰好等于阈值
视为通过（阻断条件是「>2pp」），浮点容差 EPS 防边界抖动（0.92-0.90 的
二进制误差不至于误判为 2.0000000000000018pp>2pp）。

绝对门槛（SRS §16.1 验收口径 Top5 ≥90%）为可选参数 floor_pct：传入时一并
判定总体命中率下限；不传（None）则只做相对门槛（与基线同水位的小集冒烟
场景适用）。首次运行无基线时相对门槛跳过（SR-KB-016 §6.4 口径）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .metrics import RunMetrics

#: 相对门槛默认值（KB_EVAL_GATE_MAX_DROP_PP，04-测试文档 §2.3：>2pp 阻断发布）
GATE_THRESHOLD_PP_DEFAULT: float = 2.0

#: 绝对门槛默认参照（KB_EVAL_GATE_FLOOR_PCT，SRS §16.1 验收口径；evaluate_gate
#: 缺省不启用，由调用方（CLI/API）按场景显式传入）
GATE_FLOOR_PCT_DEFAULT: float = 90.0

#: 浮点容差：回落恰等于阈值时判通过（阻断条件严格大于）
EPS: float = 1e-9


@dataclass(frozen=True)
class GateResult:
    """门禁结论：passed=True 当且仅当 reasons 为空（reasons 仅承载阻断原因）。"""

    passed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        """序列化（API 响应 / 报告数据口径）。"""
        return {"passed": self.passed, "reasons": list(self.reasons)}


def _drop_reason(label: str, baseline_acc: float, current_acc: float, threshold_pp: float) -> str | None:
    """单点回落判定：回落超过阈值（含容差）返回中文阻断原因，否则 None。"""
    drop_pp = (baseline_acc - current_acc) * 100.0
    if drop_pp > threshold_pp + EPS:
        return (
            f"{label} Top5 命中率较基线回落 {drop_pp:.2f}pp（基线 "
            f"{baseline_acc * 100:.2f}% → 本期 {current_acc * 100:.2f}%），"
            f"超过最大允许回落 {threshold_pp}pp"
        )
    return None


def evaluate_gate(
    current: RunMetrics,
    baseline: RunMetrics | None,
    threshold_pp: float = GATE_THRESHOLD_PP_DEFAULT,
    *,
    floor_pct: float | None = None,
) -> GateResult:
    """双阈值门禁判定：任一可比维度（含总体）下降超 threshold_pp 即 fail。

    - 可比维度 = 总体 + current 与 baseline 的 per_dimension 交集键（基线没有
      的维度无历史可比，不参与回落判定）；
    - floor_pct 传入时先判绝对门槛（总体命中率百分比下限）；
    - baseline=None（首次运行）只做绝对门槛（若有），相对门槛跳过；
    - reasons 仅含阻断原因（中文，逐条定位到维度与回落值），passed ⟺ reasons 空。
    """
    reasons: list[str] = []

    if floor_pct is not None:
        current_pct = current.top5_acc * 100.0
        if current_pct < floor_pct - EPS:
            reasons.append(
                f"总体 Top5 命中率 {current_pct:.2f}% 低于绝对门槛 {floor_pct}%（SRS §16.1 验收口径）"
            )

    if baseline is not None:
        overall = _drop_reason("总体", baseline.top5_acc, current.top5_acc, threshold_pp)
        if overall is not None:
            reasons.append(overall)
        for key in sorted(set(current.per_dimension) & set(baseline.per_dimension)):
            dimension_reason = _drop_reason(
                f"维度[{key}]", baseline.per_dimension[key].top5_acc,
                current.per_dimension[key].top5_acc, threshold_pp,
            )
            if dimension_reason is not None:
                reasons.append(dimension_reason)

    return GateResult(passed=not reasons, reasons=tuple(reasons))
