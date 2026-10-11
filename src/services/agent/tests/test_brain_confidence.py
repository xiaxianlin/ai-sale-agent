# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物。
# 许可与再分发须依据双方合同约定执行（COM-IP-02 乙方零使用权）。
"""置信度合成单元测试（FR-AG-003 链路⑥，SR-AG-003 §6.4；迭代 2.2 TDD）。

覆盖（开发规范 §9.1 五类）：
- 等价类：三维（检索得分分布+生成自评+事实校验）加权合成精确复算；
- 边界类：单元素得分陡度定义、空命中、置信度上限 0.99 封顶；
- 异常类：权重之和≠1 拒绝（配置校验）；忠实度=0 乘法门一票压低；
- 降级类：自评缺失（None）→ 权重重分配至检索；事实校验失败 → 分量清零。

实现方案：docs/07-实现方案/AG/SR-AG-003-AI大脑.md §6.4、§9
"""
# 知识产权归甲方所有（COM-IP-01）

from __future__ import annotations

import pytest

from services.agent.core.brain.confidence import ConfidenceComposer, ConfidenceWeights, SelfEval


def _ret(scores: list[float]) -> float:
    """测试内复算检索得分分布（Top1 0.6 + 均值 0.25 + 陡度 0.15；单元素陡度定义 1）。"""
    if not scores:
        return 0.0
    steep = 1.0 if len(scores) == 1 else min(1.0, max(0.0, (scores[0] - scores[-1]) / 0.2))
    return round(0.6 * scores[0] + 0.25 * (sum(scores) / len(scores)) + 0.15 * steep, 3)


def test_default_weights_sum_to_one() -> None:
    """等价类：默认权重 {0.4, 0.3, 0.3} 和为 1（SR-AG-003 §7 配置域校验）。"""
    weights = ConfidenceWeights()
    assert weights.retrieval + weights.self_eval + weights.fact_check == pytest.approx(1.0)


@pytest.mark.parametrize(
    "weights",
    [
        pytest.param((0.5, 0.5, 0.3), id="和大于1"),
        pytest.param((0.4, 0.4, 0.1), id="和小于1"),
    ],
)
def test_weights_sum_not_one_rejected(weights: tuple[float, float, float]) -> None:
    """异常类：权重之和≠1 → 构造期拒绝（配置校验，宁失败不静默）。"""
    with pytest.raises(ValueError):
        ConfidenceWeights(*weights)
    with pytest.raises(ValueError):
        ConfidenceComposer(ConfidenceWeights(*weights))


@pytest.mark.parametrize(
    "scores,sev,fact_ok",
    [
        pytest.param([0.9, 0.8], SelfEval(1.0, 1.0, 1.0), True, id="高命中满分自评"),
        pytest.param([0.6, 0.59], SelfEval(0.8, 1.0, 0.9), True, id="中命中自评折扣"),
        pytest.param([0.9], SelfEval(1.0, 1.0, 1.0), True, id="单元素陡度定义1"),
    ],
)
def test_compose_exact_math(scores: list[float], sev: SelfEval, fact_ok: bool) -> None:
    """等价类：三维加权精确复算——conf = w_r·检索 + w_s·自评 + w_f·事实。"""
    conf, comps = ConfidenceComposer().compose(scores, sev, fact_ok=fact_ok)
    self_score = round(sev.faithfulness * (0.6 + 0.2 * sev.completeness + 0.2 * sev.tone), 3)
    expected = round(0.4 * _ret(scores) + 0.3 * self_score + 0.3 * (1.0 if fact_ok else 0.0), 3)
    assert conf == min(expected, 0.99)
    assert comps["retrieval"] == _ret(scores)
    assert comps["self_eval"] == self_score
    assert comps["fact_check"] == (1.0 if fact_ok else 0.0)



def test_faithfulness_zero_vetoes_self_eval() -> None:
    """异常类：忠实度=0（编造一票否低）→ 自评分量 0。"""
    _, comps = ConfidenceComposer().compose([0.9, 0.8], SelfEval(0.0, 1.0, 1.0), fact_ok=True)
    assert comps["self_eval"] == 0.0


def test_self_eval_none_redistributes_to_retrieval() -> None:
    """降级类：自评缺失 → 分量=检索分量，权重并档（w_r'=0.7）。"""
    conf, comps = ConfidenceComposer().compose([0.9, 0.8], None, fact_ok=True)
    assert comps["self_eval"] == comps["retrieval"]
    assert conf == round(0.7 * _ret([0.9, 0.8]) + 0.3 * 1.0, 3)


def test_fact_fail_zeroes_component_and_lowers_conf() -> None:
    """降级类：事实校验失败 → 分量 0，置信度显著低于通过路径。"""
    composer = ConfidenceComposer()
    ok_conf, _ = composer.compose([0.9, 0.8], SelfEval(1.0, 1.0, 1.0), fact_ok=True)
    bad_conf, comps = composer.compose([0.9, 0.8], SelfEval(1.0, 1.0, 1.0), fact_ok=False)
    assert comps["fact_check"] == 0.0
    assert bad_conf < ok_conf


def test_confidence_capped_at_099() -> None:
    """边界类：合成满分突破 0.99 时封顶（保留不确定性地带，SRS §16.3 门控口径）。

    构造说明：retrieval 分量理论上限 1.0 需「Top1=均值=1 且陡度=1」不可同时成立，
    取 [1.0, 1.0, 0.8]（陡度满档 1.0、均值 0.933）→ 合成 0.993 → 封顶 0.99。
    """
    conf, _ = ConfidenceComposer().compose([1.0, 1.0, 0.8], SelfEval(1.0, 1.0, 1.0), fact_ok=True)
    assert conf == 0.99


def test_empty_hits_retrieval_zero() -> None:
    """边界类：空命中 → 检索分量 0（零命中路径的置信度证据基线）。"""
    conf, comps = ConfidenceComposer().compose([], None, fact_ok=True)
    assert comps["retrieval"] == 0.0
    assert conf == 0.3
