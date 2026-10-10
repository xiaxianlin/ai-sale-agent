# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""批量评测执行器（FR-KB-016 / SR-KB-016 §6.2，迭代 0.3）。

被测对象经 search_fn 鸭子类型注入，评测内核不绑定任何具体实现：
- 测试/联调：传入确定性桩（query/kb_types/filters/top_k → results[{chunk_id, score}]）；
- 生产：传入 RetrievalPipeline（FR-KB-015）的包装——把 async search 适配为同步
  callable 并返回统一检索 API 的 data 段形态（{"results": [...], "rewritten_query": …}），
  评测只黑盒调用完整三段式链路，不直连 Milvus/TEI（SR-KB-016 §2 决策）。

返回值兼容两种形态：
- list[{chunk_id, score}]（元素亦可携带 chunk_id 属性的任意对象）；
- {"results": 上述列表, "rewritten_query": str}（统一检索 API data 段，改写文本
  是未命中归因 rewrite 类的判定依据，FR-KB-015 可观测口径）。

并发采用线程池（ThreadPoolExecutor，可配，默认 8 = KB_EVAL_CONCURRENCY）：
评测调用同步形态的检索包装；单条异常降级为未命中结果计入分母（SR-KB-016 §6.1
保守口径：不允许靠剔除坏样本抬高指标），不中断整批。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from .dataset import EvalCase, EvalDataset
from .metrics import TOP_K, first_hit_rank

#: search_fn 鸭子类型：关键字入参 query/kb_types/filters/top_k → 检索结果（模块 docstring 契约）
SearchFn = Callable[..., Any]

#: 单条错误信息截断长度（防异常串污染报告/日志）
ERROR_MESSAGE_MAX_LENGTH: int = 200

#: 批量评测默认并发（KB_EVAL_CONCURRENCY，SR-KB-016 §7）
DEFAULT_CONCURRENCY: int = 8


def _chunk_id_of(item: Any) -> int | None:
    """单条检索结果的 chunk_id 规整为 int（映射键或属性皆可）；不可解析返回 None。"""
    raw: Any = item.get("chunk_id") if isinstance(item, Mapping) else getattr(item, "chunk_id", None)
    try:
        return int(raw)  # type: ignore[arg-type]  # bool 形态极罕见，int() 语义已足够
    except (TypeError, ValueError):
        return None


def parse_search_response(payload: Any) -> tuple[list[int], str]:
    """search_fn 返回值规整：(实际返回的 chunk_id 全序列, 改写查询文本)。

    序列不截断——first_hit_rank 自带 TopK 窗口，全集保留供归因的扩展窗口观察
    （SR-KB-016 §6.3 规则③的邻居命中判定）；不可解析的结果条目跳过
    （异常数据不占名次，与 API 层「回表缺失丢弃」同一兜底口径）。
    """
    if isinstance(payload, Mapping):
        raw_results: Any = payload.get("results") or []
        rewritten = str(payload.get("rewritten_query") or "")
    else:
        raw_results = payload or []
        rewritten = ""
    chunk_ids = [cid for cid in (_chunk_id_of(item) for item in raw_results) if cid is not None]
    return chunk_ids, rewritten


@dataclass(frozen=True)
class EvalCaseResult:
    """单条评测结果（评测指标的最小计算单元）。

    核心三字段（任务契约）：hit_at_5（Top5 是否命中）/ mrr（1/首次命中名次，
    未命中为 0.0）/ latency_ms（评测侧在线测量）；其余字段为报告下钻与
    归因判定的观测留痕（对应 SR-KB-016 §4 eval_run_item 各列）。
    """

    case_id: str
    hit_at_5: bool
    mrr: float
    latency_ms: int
    hit_rank: int | None = None
    retrieved_ids: tuple[int, ...] = ()
    rewritten_query: str = ""
    error: str | None = None

    @classmethod
    def from_rank(
        cls,
        case_id: str,
        hit_rank: int | None,
        latency_ms: int,
        retrieved_ids: Sequence[int] = (),
        rewritten_query: str = "",
        top_k: int = TOP_K,
    ) -> "EvalCaseResult":
        """按首次命中名次构造（名次窗口外或 None 均记未命中）。"""
        hit = hit_rank is not None and hit_rank <= top_k
        return cls(
            case_id=case_id,
            hit_at_5=hit,
            mrr=1.0 / hit_rank if hit else 0.0,
            latency_ms=latency_ms,
            hit_rank=hit_rank if hit else None,
            retrieved_ids=tuple(retrieved_ids),
            rewritten_query=rewritten_query,
        )

    @classmethod
    def failed(cls, case_id: str, message: str, latency_ms: int) -> "EvalCaseResult":
        """单条执行失败（超时/下游故障）：按未命中计入分母（保守口径），error 留痕。"""
        return cls(
            case_id=case_id,
            hit_at_5=False,
            mrr=0.0,
            latency_ms=latency_ms,
            error=message[:ERROR_MESSAGE_MAX_LENGTH],
        )


@dataclass
class EvalRunner:
    """批量评测执行器：线程池并发调用注入的 search_fn，逐条记录命中/延迟/改写。

    timer 可注入（测试用假时钟精确构造 latency_ms；生产为 perf_counter）。
    """

    search_fn: SearchFn
    concurrency: int = DEFAULT_CONCURRENCY
    top_k: int = TOP_K
    timer: Callable[[], float] = field(default=perf_counter, repr=False)

    def __post_init__(self) -> None:
        """并发与窗口参数校验（KB_EVAL_CONCURRENCY 1~32、TopK 为 SRS Top5 口径）。"""
        if not 1 <= self.concurrency <= 32:
            raise ValueError(f"concurrency 须在 1~32: {self.concurrency}")
        if not 1 <= self.top_k <= 10:
            raise ValueError(f"top_k 须在 1~10（SRS §16.1 Top5 口径，勿随意调整）: {self.top_k}")

    def run(self, cases: Sequence[EvalCase]) -> list[EvalCaseResult]:
        """整批执行（结果与入参同序）；单条异常在 _run_case 内降级，不中断整批。"""
        if not cases:
            return []
        with ThreadPoolExecutor(
            max_workers=self.concurrency, thread_name_prefix="kb-eval"
        ) as pool:
            return list(pool.map(self._run_case, cases))

    def run_dataset(self, dataset: EvalDataset) -> list[EvalCaseResult]:
        """便捷入口：按数据集执行（数据集构造已保证非空且 case_id 唯一）。"""
        return self.run(dataset.cases)

    def _run_case(self, case: EvalCase) -> EvalCaseResult:
        """执行单条：按用例库类型隔离发起检索，测量评测侧延迟，计算首次命中名次。"""
        started = self.timer()
        try:
            payload = self.search_fn(
                query=case.query,
                kb_types=[case.kb_type],
                filters=dict(case.filters) if case.filters is not None else None,
                top_k=self.top_k,
            )
        except Exception as exc:  # noqa: BLE001  # 单条降级口径：任何异常按未命中计
            latency_ms = int((self.timer() - started) * 1000.0)
            return EvalCaseResult.failed(case.case_id, f"{type(exc).__name__}: {exc}", latency_ms)
        latency_ms = int((self.timer() - started) * 1000.0)
        retrieved, rewritten = parse_search_response(payload)
        hit_rank = first_hit_rank(case.expected_chunk_ids, retrieved, self.top_k)
        return EvalCaseResult.from_rank(
            case_id=case.case_id,
            hit_rank=hit_rank,
            latency_ms=latency_ms,
            retrieved_ids=retrieved,
            rewritten_query=rewritten,
            top_k=self.top_k,
        )
