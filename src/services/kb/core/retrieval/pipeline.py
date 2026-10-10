# 知识产权归甲方所有（COM-IP-01）
"""三段编排（FR-KB-015 §6.4 / SR-NFR-PER-02 §6.1、§6.3）：预算守卫 + 降级阶梯 + 分段计时。

主链路（唯一串行主链路，SRS §5.3「必须包含」）：
    ① 改写（规则常驻，LLM 受预算闸门）→ ② 三路召回 + RRF 融合
    → ③ 重排 Top50→TopK（rerank=False 仅跳过第③段）→ 组装 RetrievalResult。

预算守卫（NFR-PER-02 ≤500ms，总量可配）与分段预算表（默认合计 500ms）：
    | 段   | 预算  | 进入前判定（剩余预算不足即降级）                          |
    | 改写 | 80ms  | A2：remaining < 改写+召回+组装（必经路径不保则 LLM 不进）|
    | 召回 | 250ms | 必经段不降级（向量化失败由召回段内部判定点 C 兜底）      |
    | 重排 | 150ms | B：remaining < 重排+组装 → 跳过重排                      |
    | 组装 | 20ms  | 尾段                                                    |

降级阶梯（剩余预算不足即触发，先跳过重排→再跳过改写）：
    - 第一级（判定点 B，最常触发）：召回段末剩余不足以承载重排+组装 →
      跳过重排，RRF 序直出 TopK，degrade 记 budget_skip_rerank；
    - 第二级（判定点 A2，早期预算紧张）：进 LLM 前剩余不足以覆盖
      「改写+召回+组装」必经路径 → 仅规则改写，degrade 记 budget_skip_llm_rewrite；
    - 附带观测：组装后全链超总预算记 over_total_budget（50002 硬截止的
      模块内前兆口径，API 层后续迭代承接错误映射）；重排器超时/不可达直出
      记 rerank_degraded（判定点 D，以 rerank_score 未写回为判据）。

分段计时（可观测，§6.5）：stage_ms = {rewrite, recall, rerank, assemble, total}，
经注入的 MetricsRegistry 打点（kb_search_stage_ms / kb_search_request_ms /
kb_search_degrade_total 等），与 kb_search_log.stage_ms 同源口径。

注：双级缓存（SR-KB-015 §6.6）与 kb_search_log 异步落盘属后续迭代
（cache.py / api 层），本迭代 RetrievalResult.cache_hit 字段为其预留占位。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable, Mapping, Sequence

from .base import Hit
from .metrics import MetricsRegistry
from .recall import MultiRouteRecall, RecallResult
from .reranker import PassthroughReranker, Reranker
from .rewriter import QueryRewriter, RewriteResult

# 总预算默认值（NFR-PER-02：≤500ms；KB_SEARCH_BUDGET_TOTAL_MS 取 480 硬截止，
# 本迭代按任务口径 500 可配，装配层落配置）
TOTAL_BUDGET_MS_DEFAULT = 500.0
# 默认分段预算表（改写 80 / 召回 250 / 重排 150 / 组装 20，合计 500）
REWRITE_BUDGET_MS_DEFAULT = 80.0
RECALL_BUDGET_MS_DEFAULT = 250.0
RERANK_BUDGET_MS_DEFAULT = 150.0
ASSEMBLE_BUDGET_MS_DEFAULT = 20.0
# 默认召回候选池深度（Top50 → Top5）
RECALL_DEPTH_DEFAULT = 50

# 降级链 token（degrade 字符串前缀 / kb_search_degrade_total 的 reason 标签）
DEGRADE_SKIP_RERANK = "budget_skip_rerank"
DEGRADE_SKIP_LLM_REWRITE = "budget_skip_llm_rewrite"
DEGRADE_RERANK_FAILED = "rerank_degraded"
DEGRADE_OVER_TOTAL = "over_total_budget"

# 分段计时打点的段名（stage_ms 键集，与 kb_search_log.stage_ms 同源）
STAGE_NAMES: tuple[str, ...] = ("rewrite", "recall", "rerank", "assemble")


@dataclass(frozen=True)
class StageBudgets:
    """分段预算表（毫秒）：总量守恒由装配层配置校验（合计 ≤ 总预算）。"""

    rewrite_ms: float = REWRITE_BUDGET_MS_DEFAULT
    recall_ms: float = RECALL_BUDGET_MS_DEFAULT
    rerank_ms: float = RERANK_BUDGET_MS_DEFAULT
    assemble_ms: float = ASSEMBLE_BUDGET_MS_DEFAULT


class BudgetGuard:
    """预算守卫：以总预算为不变量，提供剩余预算查询与进入判定（可注入时钟）。"""

    def __init__(
        self,
        total_ms: float = TOTAL_BUDGET_MS_DEFAULT,
        budgets: StageBudgets = StageBudgets(),
        *,
        timer: Callable[[], float] = perf_counter,
    ) -> None:
        self.total_ms = total_ms
        self.budgets = budgets
        self._timer = timer
        self._started = self._timer()

    def elapsed_ms(self) -> float:
        """已耗时（毫秒）。"""
        return (self._timer() - self._started) * 1000.0

    def remaining_ms(self) -> float:
        """剩余预算（毫秒，允许为负表示已超）。"""
        return self.total_ms - self.elapsed_ms()

    def allows(self, need_ms: float) -> bool:
        """剩余预算是否足以承载 need_ms（进入可选段前的判定）。"""
        return self.remaining_ms() >= need_ms


@dataclass
class RetrievalResult:
    """统一检索结果（04-接口设计 §2 响应 data 的模块内形态）。

    stage_ms 各段毫秒 + total 全链；degrade 为降级原因链（token(detail) 格式，
    token 即 kb_search_degrade_total 的 reason 标签值）。
    """

    hits: list[Hit] = field(default_factory=list)
    rewritten_query: str = ""
    rewrite_actions: list[str] = field(default_factory=list)
    rewrite_source: str = ""
    slots: dict[str, Any] = field(default_factory=dict)
    route_stats: dict[str, int] = field(default_factory=dict)
    stage_ms: dict[str, float] = field(default_factory=dict)
    degrade: list[str] = field(default_factory=list)
    total_ms: float = 0.0
    cache_hit: bool = False  # 双级缓存（§6.6）后续迭代接入的预留字段


class RetrievalPipeline:
    """三段式编排：注入改写器/召回器/重排器与指标注册中心组装主链路。"""

    def __init__(
        self,
        *,
        rewriter: QueryRewriter,
        recall: MultiRouteRecall,
        reranker: Reranker | None = None,
        metrics: MetricsRegistry | None = None,
        total_budget_ms: float = TOTAL_BUDGET_MS_DEFAULT,
        budgets: StageBudgets = StageBudgets(),
        depth: int = RECALL_DEPTH_DEFAULT,
    ) -> None:
        self._rewriter = rewriter
        self._recall = recall
        self._reranker: Reranker = reranker if reranker is not None else PassthroughReranker()
        self._metrics = metrics if metrics is not None else MetricsRegistry()
        self._total_budget_ms = total_budget_ms
        self._budgets = budgets
        self._depth = depth

    @property
    def metrics(self) -> MetricsRegistry:
        """指标注册中心（装配层可继续注册其他序列）。"""
        return self._metrics

    async def search(
        self,
        query: str,
        filters: Mapping[str, Any] | None = None,
        kb_types: Sequence[str] | None = None,
        top_k: int = 5,
        rerank: bool = True,
    ) -> RetrievalResult:
        """执行三段式检索；预算不足按降级阶梯裁剪可选段（先重排后改写）。"""
        guard = BudgetGuard(self._total_budget_ms, self._budgets)
        budgets = self._budgets
        degrade: list[str] = []
        stage_ms: dict[str, float] = {}

        # ① 查询改写：规则常驻；LLM 通道进入前判定（A2）——剩余预算须覆盖
        #    「改写 + 召回 + 组装」必经路径，不足即仅规则改写（降级第二级）
        started = perf_counter()
        use_llm = self._rewriter.has_llm and guard.allows(
            budgets.rewrite_ms + budgets.recall_ms + budgets.assemble_ms
        )
        if self._rewriter.has_llm and not use_llm:
            self._add_degrade(degrade, DEGRADE_SKIP_LLM_REWRITE,
                              f"remaining_ms={guard.remaining_ms():.1f}")
        rewrite = await self._rewriter.rewrite(query, kb_types, use_llm=use_llm)
        stage_ms["rewrite"] = (perf_counter() - started) * 1000.0

        # ② 多路召回（必经段）：向量化失败由召回段判定点 C 本地稀疏兜底
        started = perf_counter()
        recalled = await self._recall.recall(rewrite, filters, kb_types, depth=self._depth)
        stage_ms["recall"] = (perf_counter() - started) * 1000.0
        for reason in recalled.degrade:
            self._add_degrade(degrade, reason)

        # ③ 重排：进入前判定（B，降级第一级）——剩余须覆盖「重排 + 组装」
        started = perf_counter()
        if rerank and guard.allows(budgets.rerank_ms + budgets.assemble_ms):
            hits = await self._reranker.rerank(rewrite.rewritten_query, recalled.hits, top_k)
            if recalled.hits and not any(h.rerank_score is not None for h in hits):
                # 判定点 D：重排器超时/不可达直出（rerank_score 未写回即判据）
                self._add_degrade(degrade, DEGRADE_RERANK_FAILED)
            stage_ms["rerank"] = (perf_counter() - started) * 1000.0
        else:
            if rerank:
                self._add_degrade(degrade, DEGRADE_SKIP_RERANK,
                                  f"remaining_ms={guard.remaining_ms():.1f}")
            hits = recalled.hits[:top_k]
            stage_ms["rerank"] = 0.0  # 未执行该段：显式记 0 保持 stage_ms 键集完整

        # 组装（第四段）+ 总量观测（超总预算记前兆，50002 映射属 API 层）
        started = perf_counter()
        stage_ms["assemble"] = (perf_counter() - started) * 1000.0
        total_ms = guard.elapsed_ms()
        stage_ms["total"] = total_ms
        if total_ms > self._total_budget_ms:
            self._add_degrade(degrade, DEGRADE_OVER_TOTAL,
                              f"total_ms={total_ms:.1f} > budget={self._total_budget_ms:.1f}")

        result = RetrievalResult(
            hits=hits,
            rewritten_query=rewrite.rewritten_query,
            rewrite_actions=list(rewrite.actions),
            rewrite_source=rewrite.source,
            slots=dict(rewrite.slots),
            route_stats=dict(recalled.route_stats),
            stage_ms=stage_ms,
            degrade=degrade,
            total_ms=total_ms,
        )
        self._report(result)
        return result

    # ---- 指标打点 ----

    def _add_degrade(self, degrade: list[str], token: str, detail: str = "") -> None:
        """降级入链并计数（token 即 reason 标签；detail 为可读明细）。"""
        degrade.append(f"{token}({detail})" if detail else token)
        self._metrics.counter("kb_search_degrade_total", reason=token).inc()

    def _report(self, result: RetrievalResult) -> None:
        """分段计时与总量打点（§6.5：与 kb_search_log.stage_ms 同源口径）。"""
        metrics = self._metrics
        metrics.counter("kb_search_total").inc()
        for stage in STAGE_NAMES:
            metrics.histogram("kb_search_stage_ms", stage=stage).observe(result.stage_ms.get(stage, 0.0))
        metrics.histogram("kb_search_request_ms").observe(result.total_ms)
        if not result.hits:
            metrics.counter("kb_search_zero_hit_total").inc()
