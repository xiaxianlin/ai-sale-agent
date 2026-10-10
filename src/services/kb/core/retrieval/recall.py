# 知识产权归甲方所有（COM-IP-01）
"""② 多路召回（FR-KB-015 / SR-KB-015 §6.3）：三路并发 + 应用层 RRF 融合。

三路形态（04-接口设计 §2 检索入参 → 各路过滤口径）：
    路① 向量路：查询向量化（embed_batch，dense）→ hybrid_search(dense=…)；
    路② 关键词路：稀疏向量（sparse）→ hybrid_search(sparse=…)；
    路③ 结构化路：改写槽位（意图槽位 → expr）→ hybrid_search(expr-only)。
    路①②共用入参过滤 expr（filters ∪ kb_types ∪ item_status="published"），
    路③在入参过滤之上叠加槽位精确条件（sku in [...] 等）。

RRF 融合（与 FR-KB-014 Milvus 库内 RRF 一致，k=60）：
    score(d) = Σ_route 1/(k + rank + 1)，rank 从 0 起；命中路记入 Hit.routes
    供各路召回贡献观测（SR-KB-015 §6.5：任一路贡献连续为 0 即该路失效告警）。

降级口径（§6.1 判定点 C 与路级容错）：
    - 向量化失败 → 本地分词稀疏兜底（local_sparse_fallback，判定点 C），
      向量路放弃、关键词路继续；
    - 单路检索失败 → degrade 留痕不阻断（结构化路失败尤其不阻断主链路）；
    - 全部已发起路失败 → RetrievalDownstreamError（50002 语义，无可用降级）。

expr 防注入：字段白名单 + 字符串值转义（反斜杠与双引号），入参 filters 与
槽位值一律按字面量渲染（SR-KB-015 §9「expr 注入防护」用例）。

偏差标注：正式部署中 chunk 正文/来源由 PG 回表批量补全（03-数据库设计 §3.1
职责边界，_attach_content）；本迭代向量库桩直接携带 snippet，回表由后续迭代
接入 kb-service 数据访问层时补齐。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any, Protocol, runtime_checkable

from .base import Hit, RetrievalDownstreamError, maybe_await
from .metrics import MetricsRegistry
from .rewriter import RewriteResult

# 应用层 RRF 融合常数（KB_SEARCH_RRF_K：与 KB-014 Milvus 库内 RRF 一致取 60）
RRF_K_DEFAULT = 60
# 三路融合候选池深度默认值（KB_SEARCH_RECALL_DEPTH：Top50 → Top5）
RECALL_DEPTH_DEFAULT = 50

# 召回路名（route_stats 与 Hit.routes 的取值口径）
ROUTE_DENSE = "dense"
ROUTE_SPARSE = "sparse"
ROUTE_STRUCTURED = "structured"

# expr 字段白名单（03-数据库设计 §4.2 knowledge_chunks 标量字段；防注入第一道闸）
EXPR_FIELDS: frozenset[str] = frozenset(
    {"kb_type", "sku", "scene_tag", "process_node", "reason_type", "customer_ref", "item_status"}
)
# 可作为结构化路条件的改写槽位键（intent 等意图槽位不参与 expr）
SLOT_EXPR_FIELDS: frozenset[str] = frozenset(
    {"sku", "scene_tag", "process_node", "reason_type", "customer_ref"}
)


# ---- 上游契约（鸭子类型协议，接口冻结：向量库与向量化模块由并行任务建设）------


@runtime_checkable
class EmbeddingResultLike(Protocol):
    """向量化结果契约（FR-KB-013）：单条查询的稠密/稀疏向量，缺省以 None 表示不可用。"""

    dense: Sequence[float] | None
    sparse: Mapping[int, float] | None


@runtime_checkable
class EmbedderLike(Protocol):
    """向量化器契约：embed_batch(texts) -> EmbeddingResultLike（同步/异步均可）。"""

    def embed_batch(self, texts: Sequence[str]) -> Any: ...


@runtime_checkable
class VectorStoreLike(Protocol):
    """向量库契约（FR-KB-014）：混合检索 dense+sparse+expr（同步/异步均可）。

    dense/sparse 允许单边为 None（本模块据此区分路①/路②/路③的调用形态）；
    返回候选序列，元素为含 chunk_id/score（及可选 snippet 等）的映射或对象。
    """

    def hybrid_search(self, dense: Any, sparse: Any, expr: Any, top_k: int) -> Any: ...


# ---- 结果模型 ----


@dataclass
class RecallResult:
    """召回段输出（SR-KB-015 §5）：融合候选 + 各路统计 + 段内降级留痕。"""

    hits: list[Hit]
    route_stats: dict[str, int] = field(default_factory=dict)  # 各路返回条数 + fused 融合后条数
    elapsed_ms: float = 0.0
    degrade: list[str] = field(default_factory=list)


# ---- RRF 融合 ----


def rrf_fuse(
    routes: Sequence[tuple[str, Sequence[Hit]]],
    k: int = RRF_K_DEFAULT,
    depth: int | None = None,
) -> list[Hit]:
    """应用层 RRF 融合：score(d) = Σ_route 1/(k + rank + 1)，rank 从 0 起。

    入参为 (路名, 该路有序命中) 序列；同 chunk_id 多路命中合并为单一候选，
    routes 记录全部命中路（供各路召回贡献观测）。融合候选拷贝构建，不污染
    各路原始 Hit 对象；并列得分按首次出现顺序稳定排序，结果确定性可重放。
    """
    if k < 1:
        raise ValueError(f"RRF 常数 k 必须 ≥1（收到 {k}）")
    scores: dict[Any, float] = {}
    merged: dict[Any, Hit] = {}
    for route_name, hits in routes:
        for rank, hit in enumerate(hits):
            key = hit.chunk_id
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            entry = merged.get(key)
            if entry is None:
                entry = replace(hit, routes=[])  # 新列表：切断与原 Hit 的共享引用
                merged[key] = entry
            entry.routes.append(route_name)
    for key, score in scores.items():
        merged[key].rrf_score = score
        merged[key].score = score  # 生效得分切换为融合分（重排段再覆盖）
    fused = sorted(merged.values(), key=lambda h: -h.rrf_score)
    return fused[:depth] if depth is not None else fused


# ---- expr 构建（白名单 + 转义防注入）----


def escape_expr_value(value: Any) -> str:
    """字符串字面量转义：反斜杠与双引号（Milvus expr 字符串语法）。"""
    text = str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"')


def build_expr(
    filters: Mapping[str, Any] | None = None,
    kb_types: Sequence[str] | None = None,
    *,
    published_only: bool = True,
) -> str:
    """构建检索过滤 expr：入参 filters ∪ kb_types，强制 published 状态。

    确定性口径：item_status 条件在最前，其余字段按名排序——同输入必同输出
    （缓存键与断言友好）。非白名单字段直接拒绝（ValueError），值一律按
    转义后的字符串字面量渲染，杜绝 expr 注入语义。
    """
    merged: dict[str, list[Any]] = {}
    for name, value in (filters or {}).items():
        if name not in EXPR_FIELDS:
            raise ValueError(f"filters 含非白名单字段: {name!r}（允许: {sorted(EXPR_FIELDS)}）")
        values = value if isinstance(value, (list, tuple)) else [value]
        merged.setdefault(name, []).extend(values)
    if kb_types:
        merged.setdefault("kb_type", []).extend(kb_types)

    conditions: list[str] = []
    if published_only:
        conditions.append('item_status == "published"')
    for name in sorted(merged):
        values = merged[name]
        if not values:
            continue
        rendered = ", ".join(f'"{escape_expr_value(v)}"' for v in values)
        condition = f"{name} in [{rendered}]" if len(values) > 1 else f"{name} == {rendered}"
        conditions.append(condition)
    return " and ".join(conditions)


def build_structured_expr(
    slots: Mapping[str, Any],
    base_expr: str,
) -> str | None:
    """在基础过滤之上叠加改写槽位条件；无可用槽位返回 None（结构化路不发起）。

    sku 槽位为列表（别名多命中）→ in 条件；其余槽位为标量 → == 条件；
    槽位值同样走白名单 + 转义（与入参过滤同一防注入口径）。
    """
    conditions: list[str] = []
    for name in sorted(slots):
        if name not in SLOT_EXPR_FIELDS:
            continue
        value = slots[name]
        if isinstance(value, (list, tuple)):
            values = [v for v in value if v is not None]
            if not values:
                continue
            rendered = ", ".join(f'"{escape_expr_value(v)}"' for v in values)
            conditions.append(f"{name} in [{rendered}]" if len(values) > 1 else f"{name} == {rendered}")
        elif value is not None:
            conditions.append(f'{name} == "{escape_expr_value(value)}"')
    if not conditions:
        return None
    return f"{base_expr} and {' and '.join(conditions)}" if base_expr else " and ".join(conditions)


# ---- 本地稀疏兜底（判定点 C）----


def local_sparse_fallback(text: str, *, space: int = 1_048_576) -> dict[int, float]:
    """向量化服务不可用时的本地分词稀疏兜底（SR-KB-015 §6.3 判定点 C）。

    纯 Python 实现：拉丁/数字连续段整词 + CJK 字符 bigram，词元 id 取
    md5 哈希截断（跨进程稳定，不受 PYTHONHASHSEED 影响）。

    偏差标注：正式部署沿用 SR-KB-013 的 XLM-R 分词器词元空间（与文档侧
    sparse_vec 同空间，精确词元仍可命中）；本实现为迭代 0.2 的无重依赖占位，
    装配层接入真实向量化模块后由其降级策略接管。
    """
    tokens: list[str] = re.findall(r"[0-9A-Za-z]+|[\u4e00-\u9fff]", text.lower())
    bigrams = [a + b for a, b in zip(tokens, tokens[1:])]  # CJK bigram + 拉丁邻接词对
    counts: dict[str, float] = {}
    for feature in tokens + bigrams:
        counts[feature] = counts.get(feature, 0.0) + 1.0
    sparse: dict[int, float] = {}
    for token, count in counts.items():
        digest = hashlib.md5(token.encode("utf-8")).hexdigest()[:8]
        token_id = int(digest, 16) % space
        sparse[token_id] = sparse.get(token_id, 0.0) + count
    return sparse


# ---- 候选规整（映射/对象双形态鸭子类型）----


def _pick(item: Any, key: str, default: Any = None) -> Any:
    """从映射或对象候选中取字段（向量库桩两种返回形态均兼容）。"""
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def coerce_hit(item: Any, route: str) -> Hit:
    """单条候选 → Hit；缺 chunk_id 的候选属下游数据损坏，立即失败。"""
    chunk_id = _pick(item, "chunk_id")
    if chunk_id is None:
        raise ValueError(f"召回候选缺少 chunk_id: {item!r}")
    score = _pick(item, "score", 0.0)
    return Hit(
        chunk_id=chunk_id,
        score=float(score) if score is not None else 0.0,
        route=route,
        routes=[route],
        snippet=str(_pick(item, "snippet", "") or ""),
        title=str(_pick(item, "title", "") or ""),
        kb_type=str(_pick(item, "kb_type", "") or ""),
        source=str(_pick(item, "source", "") or ""),
    )


# ---- 多路召回主类 ----


class MultiRouteRecall:
    """三路并发召回 + RRF 融合（依赖注入 store/embedder，见模块 docstring）。"""

    def __init__(
        self,
        store: VectorStoreLike,
        embedder: EmbedderLike,
        *,
        rrf_k: int = RRF_K_DEFAULT,
        depth: int = RECALL_DEPTH_DEFAULT,
        sparse_space: int = 1_048_576,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._rrf_k = rrf_k
        self._depth = depth
        self._sparse_space = sparse_space
        self._metrics = metrics

    async def recall(
        self,
        q: RewriteResult,
        filters: Mapping[str, Any] | None = None,
        kb_types: Sequence[str] | None = None,
        depth: int | None = None,
    ) -> RecallResult:
        """执行三路召回并融合；depth 缺省取构造深度（Top50 候选池）。"""
        started = perf_counter()
        effective_depth = depth if depth is not None else self._depth
        degrade: list[str] = []

        base_expr = build_expr(filters, kb_types)

        # 查询向量化（判定点 C：失败 → 本地稀疏兜底，向量路放弃不阻断）
        dense: Any = None
        sparse: Any = None
        try:
            embedded = await maybe_await(self._embedder.embed_batch([q.rewritten_query]))
        except Exception:  # noqa: BLE001 —— 向量化服务不可用必须降级而非中断（判定点 C）
            degrade.append("embed_failed_local_sparse_fallback")
            self._bump("kb_search_embed_fallback_total")
        else:
            dense = getattr(embedded, "dense", None) if embedded is not None else None
            sparse = getattr(embedded, "sparse", None) if embedded is not None else None
            if dense is None and sparse is None:
                degrade.append("embed_empty_local_sparse_fallback")
                self._bump("kb_search_embed_fallback_total")
        if dense is None and sparse is None:
            sparse = local_sparse_fallback(q.rewritten_query, space=self._sparse_space)

        # 三路并发发起（结构化路仅在槽位条件存在时发起）
        routes: dict[str, Any] = {}
        if dense is not None:
            routes[ROUTE_DENSE] = self._search_route(ROUTE_DENSE, dense=dense, sparse=None,
                                                     expr=base_expr, top_k=effective_depth)
        if sparse is not None:
            routes[ROUTE_SPARSE] = self._search_route(ROUTE_SPARSE, dense=None, sparse=sparse,
                                                      expr=base_expr, top_k=effective_depth)
        structured_expr = build_structured_expr(q.slots, base_expr)
        if structured_expr is not None:
            routes[ROUTE_STRUCTURED] = self._search_route(ROUTE_STRUCTURED, dense=None, sparse=None,
                                                          expr=structured_expr, top_k=effective_depth)

        results = await asyncio.gather(*routes.values(), return_exceptions=True)
        fused_routes: list[tuple[str, list[Hit]]] = []
        failures: list[str] = []
        for (route_name, _), result in zip(routes.items(), results):
            if isinstance(result, BaseException):
                failures.append(route_name)
                degrade.append(f"route_failed:{route_name}")
                self._bump("kb_search_recall_route_fail_total", route=route_name)
                continue
            fused_routes.append((route_name, result))

        if routes and len(failures) == len(routes):
            # 全部已发起路失败：无可用降级，映射 50002（SR-KB-015 §6.1）
            raise RetrievalDownstreamError("vector_store", f"全部召回路失败: {failures}")

        hits = rrf_fuse(fused_routes, k=self._rrf_k, depth=effective_depth)
        route_stats = {name: 0 for name in routes}
        route_stats.update({name: len(route_hits) for name, route_hits in fused_routes})
        route_stats["fused"] = len(hits)
        return RecallResult(
            hits=hits,
            route_stats=route_stats,
            elapsed_ms=(perf_counter() - started) * 1000.0,
            degrade=degrade,
        )

    async def _search_route(self, route: str, *, dense: Any, sparse: Any, expr: str, top_k: int) -> list[Hit]:
        """发起单路检索并规整候选（store 同步/异步实现均可）。"""
        items = await maybe_await(
            self._store.hybrid_search(dense=dense, sparse=sparse, expr=expr, top_k=top_k)
        )
        return [coerce_hit(item, route) for item in (items or [])]

    def _bump(self, name: str, **labels: str) -> None:
        """召回段内部指标（未注入 registry 时静默跳过，流水线层仍统一计数）。"""
        if self._metrics is not None:
            self._metrics.counter(name, **labels).inc()
