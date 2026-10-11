# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""实战演练案例库测试（FR-TR-003，SR-TR-003 §6.1/§9；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类场景）：
- 等价类：demo 合成案例生成器的结构与容量等价类（TestDemoGenerator）；
- 边界：难度取值 1/3 与 0/4、容量闸门 99/100、min_published 覆盖（TestCaseValidation/TestCapacityGate）；
- 异常：案例结构非法、PII 命中拒绝、重复 case_id（TestCaseValidation/TestMaskingGate）；
- 注入：脱敏引擎经 MaskingGate 协议注入（核心不与 kb 模块直接耦合，TestMaskingGate）。

对应 SRS：§6.2（≥100 脱敏案例）、§12.2（100% 脱敏：含 PII 的案例拒绝入队并记录）。
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import pytest

from services.training.core.drill.casebank import (
    DIM_SCRIPT,
    DIM_STRATEGY,
    DRILL_DIMS,
    DrillCase,
    DrillCaseBank,
    CaseCapacityError,
    CaseValidationError,
    PiiRejectedError,
    build_demo_cases,
)


def make_engine() -> Any:
    """注入真实脱敏引擎（测试装配：kb MaskingEngine 仅在此注入，核心零耦合）。"""
    from services.kb.core.pipeline.cleaning.masking import MaskingEngine, default_ruleset

    return MaskingEngine(default_ruleset())


def make_bank(min_published: int = 100) -> DrillCaseBank:
    return DrillCaseBank(make_engine(), min_published=min_published)


def make_case(case_id: str = "DR-2026-9001", inquiry: str = "请提供型号 QA-9001 的参数与 MOQ 报价。") -> DrillCase:
    """构造结构合法的最小案例（测试基座；rubric 复用 demo 三维九点口径）。"""
    points = build_demo_cases(1)[0].rubric
    return DrillCase(
        case_id=case_id,
        inquiry_text=inquiry,
        context="客户区域：测试区；来源：合成（已脱敏）。",
        excellent_reply="感谢您的来函。参数与认证见附表，报价与 MOQ 随函说明，期待您的回复。",
        rubric=points,
        kb_type="training",
        difficulty=2,
        primary_dim=DRILL_DIMS[0],
    )


class TestDemoGenerator:
    """等价类：demo 合成案例生成器（确定性：模板化询盘+优秀回复+rubric，锚点可断言）。"""

    def test_demo_cases_count_100(self) -> None:
        """默认生成 100 条且 case_id 唯一（SRS §6.2 ≥100 容量口径）。"""
        cases = build_demo_cases(100)
        assert len(cases) == 100
        assert len({c.case_id for c in cases}) == 100

    def test_demo_cases_deterministic(self) -> None:
        """确定性：两次生成逐字段一致（无随机源，可复现）。"""
        assert [c.to_dict() for c in build_demo_cases(100)] == [
            c.to_dict() for c in build_demo_cases(100)
        ]

    def test_demo_cases_structure(self) -> None:
        """等价类：每条案例四要素齐备（询盘/上下文/优秀回复/rubric）+ 口径字段。"""
        for case in build_demo_cases(100):
            assert case.case_id and case.inquiry_text.strip()
            assert case.context.strip() and case.excellent_reply.strip()
            assert len(case.rubric) >= 3
            assert case.kb_type == "training"  # 演练库口径（价格遮蔽策略，06 §2）
            assert case.difficulty in (1, 2, 3)
            assert case.primary_dim in DRILL_DIMS

    def test_demo_cases_rubric_covers_three_dims(self) -> None:
        """rubric 三维齐备（产品准确性/话术/商务策略），每维 3 个评分点。"""
        by_dim: dict[str, int] = {}
        for point in build_demo_cases(100)[0].rubric:
            by_dim[point.dim] = by_dim.get(point.dim, 0) + 1
        assert set(by_dim) == set(DRILL_DIMS)
        assert all(count == 3 for count in by_dim.values())

    def test_demo_cases_excellent_reply_covers_all_keywords(self) -> None:
        """锚点可断言：优秀回复逐点覆盖 rubric 关键词（Fake 评分器可打满分）。"""
        for case in build_demo_cases(100):
            for point in case.rubric:
                assert any(kw in case.excellent_reply for kw in point.keywords), (
                    f"{case.case_id} 优秀回复未覆盖评分点 {point.point_id}"
                )

    def test_demo_cases_pass_gate(self) -> None:
        """合成案例 100% 通过脱敏闸门（demo 语料零 PII）。"""
        report = make_bank().import_cases(build_demo_cases(100))
        assert report.imported_count == 100
        assert report.rejected_count == 0

    def test_demo_cases_anchor_assertable(self) -> None:
        """案例编号与询盘 SKU 锚点格式固定（断言与冒烟对回共用口径）。"""
        cases = build_demo_cases(100)
        assert re.fullmatch(r"DR-2026-\d{4}", cases[41].case_id) is not None
        assert f"QA-{1000 + 42}" in cases[41].inquiry_text


class TestCaseValidation:
    """异常/边界：案例结构校验（CaseValidationError 统一拒绝）。"""

    def test_structure_field_rejections(self) -> None:
        """异常：空 case_id/询盘/优秀回复/rubric、评分点维度非法 → 一律拒绝。"""
        base = make_case()
        mutations: dict[str, Any] = {
            "case_id": "",
            "inquiry_text": "   ",
            "excellent_reply": "",
            "rubric": (),
        }
        for field, value in mutations.items():
            with pytest.raises(CaseValidationError):
                make_bank(min_published=1).add_case(replace(base, **{field: value}))
        bad_point = replace(base.rubric[0], dim="product")  # 口径为 product_accuracy
        with pytest.raises(CaseValidationError):
            make_bank(min_published=1).add_case(replace(base, rubric=(bad_point,)))

    def test_invalid_primary_dim_rejected(self) -> None:
        with pytest.raises(CaseValidationError):
            make_bank(min_published=1).add_case(replace(make_case(), primary_dim="product"))

    def test_difficulty_boundary(self) -> None:
        """边界：难度越界（0/4）拒绝；边界值（1/3）合法。"""
        bank = make_bank(min_published=1)
        for difficulty in (0, 4):
            with pytest.raises(CaseValidationError):
                bank.add_case(replace(make_case(f"DR-2026-90{difficulty}"), difficulty=difficulty))
        for difficulty in (1, 3):
            assert bank.add_case(
                replace(make_case(f"DR-2026-90{difficulty}"), difficulty=difficulty)
            ).difficulty == difficulty

    def test_duplicate_case_id_rejected(self) -> None:
        bank = make_bank(min_published=1)
        bank.add_case(make_case())
        with pytest.raises(CaseValidationError):
            bank.add_case(make_case())


class TestMaskingGate:
    """异常：脱敏闸门（§12.2 100% 脱敏——含 PII 的案例拒绝入队并记录）。"""

    def test_pii_case_rejected_and_recorded(self) -> None:
        """含邮箱+电话的案例拒绝入队，拒绝台账记录命中规则 id（不落 PII 明文）。"""
        bank = make_bank(min_published=1)
        with pytest.raises(PiiRejectedError):
            bank.add_case(make_case(inquiry="请把目录发到 buyer@example.com 或致电 13800138000。"))
        assert len(bank.rejected) == 1
        assert set(bank.rejected[0].rule_ids) == {"mask.email", "mask.phone"}
        assert bank.get_case("DR-2026-9001") is None  # 未入队

    def test_gate_records_idcard(self) -> None:
        """身份证号命中 mask.idnum 拒绝。"""
        bank = make_bank(min_published=1)
        with pytest.raises(PiiRejectedError):
            bank.add_case(make_case(inquiry="我的身份证号是 110101199003077512，请查收。"))
        assert bank.rejected[0].rule_ids == ("mask.idnum",)

    def test_clean_case_passes(self) -> None:
        bank = make_bank(min_published=1)
        assert bank.add_case(make_case()).case_id == "DR-2026-9001"
        assert bank.count_published() == 1

    def test_import_report_mixed(self) -> None:
        """批量导入：干净 2 条 + PII 1 条 → 2 入库 1 拒绝（拒绝带索引定位）。"""
        clean_a, clean_b = build_demo_cases(2)
        dirty = make_case(case_id="DR-2026-9002", inquiry="来信请发 buyer@example.com。")
        report = make_bank(min_published=2).import_cases([clean_a, clean_b, dirty])
        assert report.imported_count == 2
        assert report.rejected_count == 1
        assert report.rejected[0].case_id == "DR-2026-9002"
        assert report.rejected[0].index == 2
        assert "mask.email" in report.rejected[0].rule_ids

    def test_gate_uses_injected_masking(self) -> None:
        """注入验证：闸门只依赖 MaskingGate 协议——桩引擎命中即拒绝（不耦合 kb 实现）。"""

        class StubGate:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def scan_only(self, text: str, kb_type: str = "product") -> list[Any]:
                self.calls.append(text)
                return [type("F", (), {"rule_id": "mask.email", "raw": "x"})()]  # 恒命中

        gate = StubGate()
        bank = DrillCaseBank(gate, min_published=1)  # type: ignore[arg-type]
        with pytest.raises(PiiRejectedError):
            bank.add_case(make_case())
        assert len(gate.calls) >= 1  # 询盘/上下文/优秀回复均过闸


class TestCapacityGate:
    """边界：≥100 启用闸门（TR-1003：published <100 拒绝投题）+ 薄弱维参数预留。"""

    def test_pick_below_100_rejected(self) -> None:
        bank = make_bank()  # 默认 min_published=100
        bank.import_cases(build_demo_cases(99))
        with pytest.raises(CaseCapacityError):
            bank.pick(page=1, page_size=10)

    def test_pick_at_100_ok(self) -> None:
        bank = make_bank()
        bank.import_cases(build_demo_cases(100))
        page = bank.pick(page=1, page_size=10)
        assert page["total"] == 100
        assert len(page["items"]) == 10

    def test_pick_weak_dim_filter(self) -> None:
        """薄弱维参数预留：weak_dim 过滤 primary_dim（i≡0 mod 3 → strategy 共 33 条）。"""
        assert DIM_SCRIPT == "script" and len(DRILL_DIMS) == 3  # 三维口径常量
        bank = make_bank()
        bank.import_cases(build_demo_cases(100))
        page = bank.pick(page=1, page_size=50, weak_dim=DIM_STRATEGY)
        assert page["total"] == 33
        assert all(item["primary_dim"] == DIM_STRATEGY for item in page["items"])

    def test_min_published_override(self) -> None:
        """配置边界：min_published 覆盖为 2 时 2 条即可投题。"""
        bank = make_bank(min_published=2)
        bank.import_cases(build_demo_cases(2))
        assert bank.pick(page=1, page_size=10)["total"] == 2
