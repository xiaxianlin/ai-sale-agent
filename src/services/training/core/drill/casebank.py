# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""实战演练案例库（FR-TR-003，SR-TR-003 §6.1 案例构建 / §12.2 脱敏闸门）。

- :class:`DrillCase`——演练案例（询盘/上下文/优秀回复/rubric 三维九点评分口径）；
- :class:`DrillCaseBank`——案例库：结构校验 → 脱敏闸门 → 入库，≥100 启用闸门
  （TR-1003：published 案例数不足时拒绝投题）、weak_dim 薄弱维过滤参数预留；
- :func:`build_demo_cases`——demo 合成案例生成器（确定性：模板化询盘 + 覆盖全部
  rubric 关键词的优秀回复，锚点可断言；零 PII，全量通过闸门）。

脱敏耦合口径（SR-TR-003 决策 D1）：核心仅依赖 :class:`MaskingGate` 协议
（``scan_only(text, kb_type)``），kb-service 的 MaskingEngine 由装配层/测试注入，
本模块不直接 import kb 实现——同一规则、同一版本，闸门命中即拒绝入队并记录。

依据文档：docs/07-实现方案/TR/SR-TR-003-实战演练系统.md §6.1、§7（tr.drill.case.min_published）、§9；
         SRS §6.2（≥100 脱敏案例）、§12.2（培训案例 100% 脱敏）。
"""
from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

# --------------------------------------------------------------------------- 维度口径常量

#: 演练评分三维（源自 SRS §6.2：产品准确性 / 话术 / 商务策略）
DIM_PRODUCT: str = "product_accuracy"
DIM_SCRIPT: str = "script"
DIM_STRATEGY: str = "strategy"
#: 三维固定顺序（评分输出与校准判定的遍历口径）
DRILL_DIMS: tuple[str, ...] = (DIM_PRODUCT, DIM_SCRIPT, DIM_STRATEGY)
#: 维度中文标签（API 与前端共用）
DIM_LABELS: dict[str, str] = {
    DIM_PRODUCT: "产品准确性",
    DIM_SCRIPT: "话术",
    DIM_STRATEGY: "商务策略",
}

#: 演练库族 kb_type（06-安全与合规设计 §2：该族价格策略=mask）
ALLOWED_KB_TYPES: tuple[str, ...] = ("training", "negative_case", "skill")
#: 案例状态（demo 两态简化；生产四段状态机 draft→desensitized→annotated→published 见 SR §6.1）
CASE_STATUSES: tuple[str, ...] = ("draft", "published")
#: 难度取值域（SR §4 drill_case.difficulty CHECK 约束）
DIFFICULTY_RANGE: tuple[int, int] = (1, 3)


# --------------------------------------------------------------------------- 异常


class CaseValidationError(ValueError):
    """案例结构校验失败（编号/四要素/口径字段非法）。"""


class PiiRejectedError(ValueError):
    """脱敏闸门拒绝：案例文本命中 PII 规则，拒绝入队（§12.2 100% 脱敏）。"""

    def __init__(self, case_id: str, rule_ids: tuple[str, ...]) -> None:
        super().__init__(
            f"案例 {case_id} 命中 PII 规则 {list(rule_ids)}，拒绝入队（TR-1004 语义：未过闸不可发布）"
        )
        self.case_id = case_id
        self.rule_ids = rule_ids


class CaseCapacityError(RuntimeError):
    """启用闸门拒绝：published 案例数不足（TR-1003，SR §7 tr.drill.case.min_published）。"""


# --------------------------------------------------------------------------- 脱敏闸门协议


class MaskingGate(Protocol):
    """脱敏闸门协议（SR-TR-003 决策 D1 的注入接缝）。

    生产注入 kb-service ``MaskingEngine``（scan_only 语义：只检测不改写，
    whitelist_keep 不计为残留）；测试/演示可注入桩实现。
    """

    def scan_only(self, text: str, kb_type: str = "product") -> Sequence[Any]:
        """只读扫描：返回命中 Finding 序列（零命中 = 通过闸门）。"""
        ...


# --------------------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class RubricPoint:
    """单个评分点：所属维度 + 描述 + 命中关键词（Fake 评分器按关键词覆盖度打分）。"""

    point_id: str
    dim: str
    desc: str
    keywords: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "point_id": self.point_id,
            "dim": self.dim,
            "desc": self.desc,
            "keywords": list(self.keywords),
        }


@dataclass(frozen=True)
class DrillCase:
    """演练案例（SR §4 drill_case 表的应用层形态，demo 内存实现）。

    status 口径：demo 简化为 draft/published 两态——过脱敏闸门即 published；
    生产四段状态机与发布前二次扫描由 SR-TR-003 §6.1 CaseWorkflow 承接。
    """

    case_id: str
    inquiry_text: str
    context: str
    excellent_reply: str
    rubric: tuple[RubricPoint, ...]
    kb_type: str = "training"
    difficulty: int = 2
    primary_dim: str = DIM_PRODUCT
    status: str = "published"
    source_ref: str = ""

    def validate(self) -> None:
        """结构校验（SR §4 列约束的应用层等价）：任一非法即 CaseValidationError。"""
        if not self.case_id.strip():
            raise CaseValidationError("案例编号 case_id 不能为空")
        if not self.inquiry_text.strip():
            raise CaseValidationError("询盘文本 inquiry_text 不能为空")
        if not self.excellent_reply.strip():
            raise CaseValidationError("优秀回复 excellent_reply 不能为空")
        if not self.rubric:
            raise CaseValidationError("rubric 评分点不能为空")
        for point in self.rubric:
            if not point.point_id.strip() or not point.desc.strip() or not point.keywords:
                raise CaseValidationError(f"评分点 {point.point_id!r} 字段不完整")
            if point.dim not in DRILL_DIMS:
                raise CaseValidationError(
                    f"评分点 {point.point_id!r} 维度 {point.dim!r} 非法，允许值 {DRILL_DIMS}"
                )
        if self.kb_type not in ALLOWED_KB_TYPES:
            raise CaseValidationError(f"kb_type {self.kb_type!r} 非法，允许值 {ALLOWED_KB_TYPES}")
        low, high = DIFFICULTY_RANGE
        if not isinstance(self.difficulty, int) or not low <= self.difficulty <= high:
            raise CaseValidationError(f"难度必须在 {low}..{high} 内，当前 {self.difficulty!r}")
        if self.primary_dim not in DRILL_DIMS:
            raise CaseValidationError(f"主考核维度 {self.primary_dim!r} 非法，允许值 {DRILL_DIMS}")
        if self.status not in CASE_STATUSES:
            raise CaseValidationError(f"案例状态 {self.status!r} 非法，允许值 {CASE_STATUSES}")

    def to_dict(self) -> dict[str, Any]:
        """JSON 安全字典（API 信封 data 直接承载）。"""
        return {
            "case_id": self.case_id,
            "inquiry_text": self.inquiry_text,
            "context": self.context,
            "excellent_reply": self.excellent_reply,
            "rubric": [point.to_dict() for point in self.rubric],
            "kb_type": self.kb_type,
            "difficulty": self.difficulty,
            "primary_dim": self.primary_dim,
            "status": self.status,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class GateRejection:
    """闸门拒绝台账（只记规则 id，不落 PII 明文——脱敏口径）。"""

    index: int
    case_id: str
    rule_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "case_id": self.case_id, "rule_ids": list(self.rule_ids)}


@dataclass
class ImportReport:
    """批量导入报告：入库清单 + 拒绝台账。"""

    imported: list[DrillCase]
    rejected: list[GateRejection]

    @property
    def imported_count(self) -> int:
        return len(self.imported)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "imported_count": self.imported_count,
            "rejected_count": self.rejected_count,
            "imported": [case.to_dict() for case in self.imported],
            "rejected": [item.to_dict() for item in self.rejected],
        }


# --------------------------------------------------------------------------- 案例库


class DrillCaseBank:
    """演练案例库：结构校验 → 脱敏闸门 → 入库 → 分页抽题（线程安全）。"""

    def __init__(self, masking: MaskingGate, *, min_published: int = 100) -> None:
        self._masking = masking
        self.min_published = min_published  # 启用闸门阈值（SR §7 缺省 100）
        self._cases: dict[str, DrillCase] = {}
        self._rejections: list[GateRejection] = []
        self._lock = threading.Lock()

    # ── 入库链路 ────────────────────────────────────────────────────────────
    def add_case(self, case: DrillCase, *, index: int = 0) -> DrillCase:
        """单案例入库：①结构校验 → ②去重 → ③脱敏闸门（命中 PII 拒绝并记录）。"""
        case.validate()
        with self._lock:
            if case.case_id in self._cases:
                raise CaseValidationError(f"案例编号重复：{case.case_id}")
        rule_ids = self._gate_rule_ids(case)
        if rule_ids:
            rejection = GateRejection(index=index, case_id=case.case_id, rule_ids=tuple(rule_ids))
            with self._lock:
                self._rejections.append(rejection)
            raise PiiRejectedError(case.case_id, rejection.rule_ids)
        with self._lock:
            self._cases[case.case_id] = case
        return case

    def import_cases(self, cases: Sequence[DrillCase]) -> ImportReport:
        """批量导入：单条被闸门拒绝不中断整批（拒绝逐条入台账，供导入报告定位）。"""
        imported: list[DrillCase] = []
        rejected: list[GateRejection] = []
        for index, case in enumerate(cases):
            try:
                imported.append(self.add_case(case, index=index))
            except PiiRejectedError as exc:  # 拒绝不中断整批，台账随导入报告返回
                rejected.append(GateRejection(index=index, case_id=exc.case_id, rule_ids=exc.rule_ids))
        return ImportReport(imported=imported, rejected=rejected)

    def _gate_rule_ids(self, case: DrillCase) -> list[str]:
        """对询盘/上下文/优秀回复三段文本全量扫描，返回去重命中规则 id（空 = 通过）。"""
        rule_ids: list[str] = []
        for text in (case.inquiry_text, case.context, case.excellent_reply):
            for finding in self._masking.scan_only(text, kb_type=case.kb_type):
                rule_id = str(getattr(finding, "rule_id", ""))
                if rule_id and rule_id not in rule_ids:
                    rule_ids.append(rule_id)
        return rule_ids

    # ── 查询与投题 ──────────────────────────────────────────────────────────
    def get_case(self, case_id: str) -> DrillCase | None:
        with self._lock:
            return self._cases.get(case_id)

    def count_published(self) -> int:
        with self._lock:
            return sum(1 for case in self._cases.values() if case.status == "published")

    def list_cases(
        self, *, offset: int = 0, limit: int = 20, primary_dim: str | None = None
    ) -> tuple[list[DrillCase], int]:
        """分页列举 published 案例（primary_dim 过滤即 weak_dim 薄弱维参数预留）。"""
        with self._lock:
            ordered = sorted(self._cases.values(), key=lambda c: c.case_id)
        filtered = [c for c in ordered if c.status == "published" and (primary_dim is None or c.primary_dim == primary_dim)]
        return filtered[offset : offset + limit], len(filtered)

    def pick(self, *, page: int = 1, page_size: int = 10, weak_dim: str | None = None) -> dict[str, Any]:
        """分页投题：启用闸门（TR-1003）→ 分页/薄弱维过滤（返回 JSON 安全分页结构）。"""
        if self.count_published() < self.min_published:
            raise CaseCapacityError(
                f"published 案例数 {self.count_published()} 未达启用闸门 {self.min_published}，拒绝投题（TR-1003）"
            )
        offset = (page - 1) * page_size
        items, total = self.list_cases(offset=offset, limit=page_size, primary_dim=weak_dim)
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [case.to_dict() for case in items],
        }

    @property
    def rejected(self) -> tuple[GateRejection, ...]:
        """拒绝台账快照（闸门审计用）。"""
        with self._lock:
            return tuple(self._rejections)


# --------------------------------------------------------------------------- demo 合成案例生成器

#: demo 客户区域轮换池（确定性选取，无 PII）
DEMO_REGIONS: tuple[str, ...] = ("欧洲", "北美", "东南亚", "中东")


def _demo_rubric() -> tuple[RubricPoint, ...]:
    """demo 三维九点 rubric（每维 3 点；关键词在优秀回复中全覆盖，锚点可断言）。"""
    return (
        RubricPoint("P1", DIM_PRODUCT, "准确回应产品核心参数（材质/承重等）", ("参数",)),
        RubricPoint("P2", DIM_PRODUCT, "说明产品认证情况（如 CE）", ("认证",)),
        RubricPoint("P3", DIM_PRODUCT, "给出报价与有效期说明", ("报价",)),
        RubricPoint("S1", DIM_SCRIPT, "开头称呼并感谢客户来函", ("感谢",)),
        RubricPoint("S2", DIM_SCRIPT, "结构化分点陈述", ("第一", "第二")),
        RubricPoint("S3", DIM_SCRIPT, "以明确行动号召收尾", ("期待",)),
        RubricPoint("G1", DIM_STRATEGY, "探询客户采购背景与用途", ("采购",)),
        RubricPoint("G2", DIM_STRATEGY, "提供阶梯价格或选项引导", ("阶梯",)),
        RubricPoint("G3", DIM_STRATEGY, "合规表达，不承诺未认证资质", ("合规",)),
    )


def build_demo_cases(n: int = 100) -> list[DrillCase]:
    """确定性合成 n 条演练案例（demo 口径：模板化询盘 + 优秀回复 + 三维 rubric）。

    - 案例编号 ``DR-2026-{i:04d}``、询盘 SKU 锚点 ``QA-{1000+i}`` 均为纯函数推导，
      两次生成逐字段一致（无随机源）；数字段均 ≤5 位，不含任何 PII 规则形态，
      100% 通过脱敏闸门（闸门本身仍逐条执行，不因来源豁免）；
    - 优秀回复覆盖全部 rubric 关键词（Fake 评分器对优秀作答可得满分，差距分析可断言）。
    """
    rubric = _demo_rubric()
    cases: list[DrillCase] = []
    for i in range(1, n + 1):
        case_id = f"DR-2026-{i:04d}"
        sku = f"QA-{1000 + i}"
        region = DEMO_REGIONS[(i - 1) % len(DEMO_REGIONS)]
        inquiry = (
            f"关于型号 {sku} 的询盘：我司为{region}批发商，请提供 {sku} 的材质与承重参数、"
            f"认证情况及 MOQ 报价；年采购量约 50000 件。"
        )
        context = f"客户区域：{region}；历史成交：无；来源：展会线索（已脱敏）；案例编号 {case_id}。"
        excellent_reply = (
            f"尊敬的客户，感谢您的来函。第一，参数与认证：{sku} 采用航空铝材，承重 35 千克，"
            f"已通过 CE 认证，完整参数表随函附上；第二，报价与交期：MOQ 500 件，阶梯价格随量递减，"
            f"报价单有效期 15 天，证书扫描件按合规口径提供；第三，为精准推荐选项，"
            f"请告知贵司的采购用途与目标市场。期待您的回复。"
        )
        cases.append(
            DrillCase(
                case_id=case_id,
                inquiry_text=inquiry,
                context=context,
                excellent_reply=excellent_reply,
                rubric=rubric,
                kb_type="training",
                difficulty=(i - 1) % 3 + 1,
                primary_dim=DRILL_DIMS[(i - 1) % 3],
                status="published",
                source_ref="demo://simcorp/synthetic-inquiry",
            )
        )
    return cases


__all__ = [
    "DIM_PRODUCT",
    "DIM_SCRIPT",
    "DIM_STRATEGY",
    "DRILL_DIMS",
    "DIM_LABELS",
    "ALLOWED_KB_TYPES",
    "CASE_STATUSES",
    "DIFFICULTY_RANGE",
    "CaseValidationError",
    "PiiRejectedError",
    "CaseCapacityError",
    "MaskingGate",
    "RubricPoint",
    "DrillCase",
    "GateRejection",
    "ImportReport",
    "DrillCaseBank",
    "build_demo_cases",
]
