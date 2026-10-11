# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""置信度合成（FR-AG-003 链路⑥，SR-AG-003 §6.4；迭代 2.2 demo 口径）。

三维加权：检索得分分布（默认 0.4）+ 生成自评（默认 0.3）+ 关键事实校验（默认 0.3）；
权重配置化且**和必须为 1**（构造期校验，NFR-MAI-02）。

- 检索分量 = Top1×0.6 + 均值×0.25 + 分布陡度×0.15（重排后得分，FR-KB-030）；
  单元素得分无分布可言，陡度定义 1.0（工程细化：不惩罚权威单源命中）；
- 自评分量 = 忠实度 ×（0.6 + 0.2·完整性 + 0.2·语气）——忠实度为乘法门：
  编造一票否低（faithfulness=0 → 分量 0）；自评缺失（关闭/失败）→ 权重并档至检索；
- 事实分量 = 1.0/0.0 硬开关（价格/交期仅可来自结构化库，R-03）；
- 合成结果封顶 0.99：保留不确定性地带，杜绝「满分即放行」的口径漏洞（SRS §16.3 门控）。

判定（need_human/原因码）不在本模块——统一归 brain/fallback.py 的 FallbackGate。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 置信度封顶（合成满分仍保留 0.01 不确定性地带）
CONF_CAP: float = 0.99
#: 权重求和容差（浮点和校验）
_WEIGHT_SUM_EPS = 1e-9


@dataclass(frozen=True)
class ConfidenceWeights:
    """三维合成权重（和必须为 1，构造期校验；app_config AG_CONF_WEIGHTS 的进程内形态）。"""

    retrieval: float = 0.4
    self_eval: float = 0.3
    fact_check: float = 0.3

    def __post_init__(self) -> None:
        values = (self.retrieval, self.self_eval, self.fact_check)
        if any(not 0.0 <= v <= 1.0 for v in values):
            raise ValueError(f"权重须在 [0,1] 区间：{values}")
        if abs(sum(values) - 1.0) > _WEIGHT_SUM_EPS:
            raise ValueError(f"权重之和必须为 1：{values}（和={sum(values)}）")


@dataclass(frozen=True)
class SelfEval:
    """生成自评三维（LLM 质检 Prompt 的结构化输出；demo 由规则评估器确定性产出）。"""

    faithfulness: float   # 忠实度：是否存在知识片段之外的事实性陈述或数字（0~1）
    completeness: float   # 完整性：是否逐一回应全部诉求（0~1）
    tone: float           # 语气：是否符合企业话术风格与外贸礼仪（0~1）


def retrieval_score(scores: list[float]) -> float:
    """检索得分分布分量：Top1 0.6 + 均值 0.25 + 陡度 0.15（空集为 0，结果保留 3 位）。"""
    if not scores:
        return 0.0
    top = scores[0]
    mean = sum(scores) / len(scores)
    # 陡度：头尾差归一（0.2 为满陡参考差）；单元素无分布 → 定义 1.0（工程细化，见模块 docstring）
    steepness = 1.0 if len(scores) == 1 else min(1.0, max(0.0, (top - scores[-1]) / 0.2))
    return round(0.6 * top + 0.25 * mean + 0.15 * steepness, 3)


class ConfidenceComposer:
    """三维加权合成器：compose() 输出 (置信度, 分量明细)（SR-AG-003 §6.4）。"""

    def __init__(self, weights: ConfidenceWeights | None = None) -> None:
        self._weights = weights if weights is not None else ConfidenceWeights()

    @property
    def weights(self) -> ConfidenceWeights:
        """当前权重（只读）。"""
        return self._weights

    def compose(
        self,
        scores: list[float],
        self_eval: SelfEval | None,
        *,
        fact_ok: bool,
    ) -> tuple[float, dict[str, float]]:
        """合成置信度。

        :param scores: 检索得分（重排后降序，FR-KB-030 hits 的 score 列）；
        :param self_eval: 生成自评（None = 自评关闭/失败 → 权重重分配至检索）；
        :param fact_ok: 关键事实校验结果（引用合法且无价格残留且占位符全解析）。
        :return: (置信度 [0, 0.99], 分量明细 {retrieval, self_eval, fact_check})
        """
        retrieval = retrieval_score(scores)
        if self_eval is None:
            # 自评关闭 → 权重重分配至检索（SR-AG-003 §6.4），分量=检索分量、自评权重清零
            w_retrieval = self._weights.retrieval + self._weights.self_eval
            w_self = 0.0
            self_score = retrieval
        else:
            w_retrieval = self._weights.retrieval
            w_self = self._weights.self_eval
            # 忠实度乘法门：编造一票否低（faithfulness=0 → 自评分量 0）
            self_score = round(
                self_eval.faithfulness * (0.6 + 0.2 * self_eval.completeness + 0.2 * self_eval.tone), 3
            )
        fact = 1.0 if fact_ok else 0.0
        conf = round(
            w_retrieval * retrieval + w_self * self_score + self._weights.fact_check * fact, 3
        )
        return min(conf, CONF_CAP), {
            "retrieval": retrieval,
            "self_eval": self_score,
            "fact_check": fact,
        }


__all__ = ["CONF_CAP", "ConfidenceWeights", "SelfEval", "retrieval_score", "ConfidenceComposer"]
