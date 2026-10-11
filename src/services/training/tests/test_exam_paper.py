# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""AI 组卷单元测试（FR-TR-007，SR-TR-007 §6.1；TDD 阶段①用例设计）。

用例分组（开发规范 §9.1 五类）：
- 等价类：薄弱维排序配额 / 中性雷达默认 / 客观主观配比；
- 边界：配额求和恰等总题数 / 难度分布守恒修复 / 近 N 次去重排除；
- 异常：非法蓝图拒绝（配比不守恒 / 未知维度）；
- 降级：题库不足降级规则模板卷 / 题库彻底不足报错。
"""
from __future__ import annotations

import pytest

from services.training.core.exam.paper import (
    DIMENSIONS,
    BlueprintInvalidError,
    Blueprint,
    PaperGenerator,
    QuestionBankItem,
    build_blueprint,
    build_demo_questions,
    InMemoryQuestionBank,
    validate_and_repair,
)

# --------------------------------------------------------------------------- 夹具


def radar_weak_first() -> dict[str, int]:
    """雷达样例：negotiation 最薄弱（22）→ communication（48）→ platform（66）→ product（90）。"""
    return {"product": 90, "communication": 48, "negotiation": 22, "platform": 66}


@pytest.fixture()
def bank() -> InMemoryQuestionBank:
    """demo 题库：每维 15 题（客观 11 + 主观 4，难度全覆盖），共 60 题。"""
    return InMemoryQuestionBank(build_demo_questions(per_dim_obj=11, per_dim_subj=4))


# --------------------------------------------------------------------------- 等价类


class TestBlueprintEquivalence:
    """蓝图构造等价类：薄弱维配额 / 中性默认 / 总量守恒。"""

    def test_weak_dim_gets_largest_quota(self, bank: InMemoryQuestionBank) -> None:
        """第 1 薄弱维配额 = round(total×0.4)，为各维最大（SR §6.1-c 配额公式）。"""
        bp = build_blueprint(radar_weak_first(), total=20)
        assert bp.quotas["negotiation"] == 8  # 20×0.4
        assert bp.quotas["communication"] == 6  # 20×0.3
        assert bp.quotas["platform"] == 4  # 20×0.2
        assert bp.quotas["product"] == 2  # 20×0.1
        assert bp.weak_rank[0] == "negotiation"

    def test_quota_sum_equals_total(self) -> None:
        """边界：各维配额之和恰等于总题数（最大余数法补齐，不丢题位）。"""
        bp = build_blueprint(radar_weak_first(), total=22)
        assert sum(bp.quotas.values()) == 22

    def test_neutral_radar_without_snapshot(self) -> None:
        """无雷达新员工 → 中性默认分（60），薄弱序按维度名稳定排序（确定性）。"""
        bp = build_blueprint(None, total=20)
        assert set(bp.quotas) == set(DIMENSIONS)
        assert sum(bp.quotas.values()) == 20

    def test_obj_subj_ratio_split(self) -> None:
        """客观/主观配比 0.6：各节 obj = round(quota×0.6)，obj+subj = quota。"""
        bp = build_blueprint(radar_weak_first(), total=20)
        for dim, quota in bp.quotas.items():
            obj, subj = bp.obj_subj[dim]
            assert obj + subj == quota
            assert obj == round(quota * 0.6)


# --------------------------------------------------------------------------- 异常


class TestBlueprintInvalid:
    """非法蓝图拒绝：违反硬约束即 BlueprintInvalidError（LLM 输出不可信的防线）。"""

    def test_invalid_weak_quota_sum_rejected(self) -> None:
        """薄弱配比之和 ≠ 1 → 拒绝（SR §7 取值约束：各维和=1）。"""
        with pytest.raises(BlueprintInvalidError, match="薄弱配比"):
            build_blueprint(radar_weak_first(), total=20, weak_quota=(0.5, 0.3, 0.2, 0.1))

    def test_unknown_dimension_rejected(self) -> None:
        """蓝图含未知维度 → 拒绝（四维口径外取值非法）。"""
        with pytest.raises(BlueprintInvalidError, match="未知维度"):
            Blueprint(
                quotas={"product": 20, "ghost": 0},
                obj_subj={"product": (12, 8), "ghost": (0, 0)},
                difficulty_dist={"product": (10, 6, 4), "ghost": (0, 0, 0)},
                weak_rank=("ghost", "product"),
                total=20,
            )


# --------------------------------------------------------------------------- 边界与修复


class TestValidateAndRepair:
    """蓝图确定性校验修复：分布不守恒按公式重排（SR §9 用例：修复-分布不守恒）。"""

    def test_repair_difficulty_dist_not_conserved(self) -> None:
        """LLM 返回难度分布和 ≠ 节配额 → 修复后各节守恒（和 == quota）。"""
        bp = build_blueprint(radar_weak_first(), total=20)
        broken = Blueprint(
            quotas=dict(bp.quotas),
            obj_subj=dict(bp.obj_subj),
            difficulty_dist={dim: (1, 1, 1) for dim in DIMENSIONS},  # 和=3 ≠ 各节配额
            weak_rank=bp.weak_rank,
            total=bp.total,
        )
        repaired = validate_and_repair(broken, total=20)
        for dim in DIMENSIONS:
            assert sum(repaired.difficulty_dist[dim]) == repaired.quotas[dim]

    def test_repair_keeps_weak_monotonicity(self) -> None:
        """强维题量 > 第 1 薄弱维（被 LLM 打乱）→ 重排回 quota 配额（SR §9 用例）。"""
        bp = build_blueprint(radar_weak_first(), total=20)
        broken = Blueprint(
            quotas={"product": 10, "communication": 4, "negotiation": 2, "platform": 4},  # 倒挂
            obj_subj=dict(bp.obj_subj),
            difficulty_dist=dict(bp.difficulty_dist),
            weak_rank=bp.weak_rank,
            total=20,
        )
        repaired = validate_and_repair(broken, total=20)
        assert repaired.quotas["negotiation"] == 8
        assert repaired.quotas["product"] == 2


# --------------------------------------------------------------------------- 降级与确定性抽题


class TestGenerate:
    """组卷引擎：确定性种子 / 约束满足 / 去重 / 降级链。"""

    def test_generate_deterministic_by_seed(self, bank: InMemoryQuestionBank) -> None:
        """同 (trainee_id, attempt_no) 重放 → 题目序列完全一致（SR §6.1 审计复现口径）。"""
        gen = PaperGenerator(bank)
        paper_a = gen.generate(101, 1, radar=radar_weak_first())
        paper_b = gen.generate(101, 1, radar=radar_weak_first())
        assert [q.question_id for q in paper_a.questions] == [
            q.question_id for q in paper_b.questions
        ]
        assert paper_a.seed == paper_b.seed

    def test_generate_respects_section_quotas(self, bank: InMemoryQuestionBank) -> None:
        """组卷结果各节题量与蓝图配额一致；题库快照含答案与 rubric（判分复现依据）。"""
        gen = PaperGenerator(bank)
        paper = gen.generate(101, 1, radar=radar_weak_first())
        counts: dict[str, int] = {}
        for q in paper.questions:
            counts[q.dim] = counts.get(q.dim, 0) + 1
        bp = paper.blueprint
        assert counts == bp.quotas
        assert len(paper.questions) == bp.total

    def test_generate_excludes_recent_questions(self, bank: InMemoryQuestionBank) -> None:
        """防背题：排除清单内题目绝不入卷（SR §6.5 近 2 次去重）。"""
        gen = PaperGenerator(bank)
        first = gen.generate(101, 1, radar=radar_weak_first())
        seen = {q.question_id for q in first.questions}
        second = gen.generate(101, 2, radar=radar_weak_first(), exclude=seen)
        assert seen.isdisjoint(q.question_id for q in second.questions)

    def test_generate_degrades_to_template_when_bank_lacks_hard(self) -> None:
        """降级：题库无难题 → 蓝图约束无法满足，降级规则模板卷（degraded=True 且足量）。"""
        small_bank = InMemoryQuestionBank(build_demo_questions(per_dim_obj=11, per_dim_subj=4))
        only_easy = [q for q in small_bank.items if q.difficulty == 1]
        gen = PaperGenerator(InMemoryQuestionBank(only_easy))
        paper = gen.generate(101, 1, radar=radar_weak_first())
        assert paper.degraded is True
        assert paper.degrade_reason != ""
        assert len(paper.questions) == paper.blueprint.total

    def test_generate_bank_insufficient_raises(self) -> None:
        """异常：题库连模板卷都填不满 → BankInsufficientError（42201 透传口径）。"""
        from services.training.core.exam.paper import BankInsufficientError

        gen = PaperGenerator(InMemoryQuestionBank(build_demo_questions(per_dim_obj=2, per_dim_subj=1)))
        with pytest.raises(BankInsufficientError):
            gen.generate(101, 1, radar=radar_weak_first())

    def test_paper_view_hides_answers(self, bank: InMemoryQuestionBank) -> None:
        """考生视图不含 answer/model_answer/rubric（开考下发面与判分快照分离）。"""
        gen = PaperGenerator(bank)
        paper = gen.generate(101, 1, radar=radar_weak_first())
        view = paper.questions_view()
        assert view and all("answer" not in q and "model_answer" not in q and "rubric" not in q for q in view)
        # 内部快照仍带答案（快照固化：题库改版不影响本场判分）
        assert all(q.answer for q in paper.questions)


# --------------------------------------------------------------------------- 题库条目等价类


class TestQuestionBankItem:
    """题库条目：主观/客观题型归类与答案形态等价类。"""

    def test_qtype_classification(self) -> None:
        """single/multi/judge 归客观，short_answer/case 归主观（自动判分占比约束）。"""
        from services.training.core.exam.paper import is_objective

        assert is_objective("single") and is_objective("multi") and is_objective("judge")
        assert not is_objective("short_answer") and not is_objective("case")

    def test_demo_bank_shape(self) -> None:
        """demo 题库形态：四维齐备、题型齐备、答案非空。"""
        items = build_demo_questions(per_dim_obj=11, per_dim_subj=4)
        assert len(items) == 60
        assert {i.dim for i in items} == set(DIMENSIONS)
        assert all(i.answer for i in items)
        assert all(i.rubric for i in items if i.qtype in ("short_answer", "case"))
